"""aiohttp 앱 팩토리 + 미들웨어 (dev-spec-web-console §5).

기존 Slack 데몬과 **같은 프로세스**에서 돈다. 데몬은 이미 asyncio 루프를 돌리고
있으므로 aiohttp 를 그 위에 올리면 되고, 별도 프로세스를 띄우면 SQLite 를 두
프로세스가 쓰게 되어 잠금 문제가 생긴다.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from autodeploy.db import connect
from autodeploy.masking import SecretMasker
from autodeploy.queue import JobQueue
from autodeploy.web import api, keys
from autodeploy.web.forwards import ForwardManager
from autodeploy.web.jobs import JobService
from autodeploy.web.sse import SseBroker
from autodeploy.web.auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    LoginThrottle,
    csrf_matches,
    purge_expired_sessions,
    resolve_session,
)

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# 세션 없이 접근할 수 있는 경로. 그 외 전부 인증이 필요하다.
PUBLIC_PATHS = frozenset({"/login", "/api/login", "/healthz"})
# 로그인 화면도 스타일시트를 받아야 한다. 정적 자산에는 비밀이 없다.
PUBLIC_PREFIX = "/static/"

# 상태를 바꾸지 않는 메서드는 CSRF 검사 대상이 아니다.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 승인 없이 방치된 patch 를 정리하는 주기와 기준 (§9).
HOUSEKEEPING_INTERVAL = 3600.0
AWAITING_TTL_HOURS = 24


@web.middleware
async def session_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """쿠키 → 세션을 붙인다. 인증 강제는 require_auth_middleware 가 한다."""
    token = request.cookies.get(SESSION_COOKIE)
    session = None
    if token:
        async with connect(request.app[keys.DB_PATH]) as db:
            session = await resolve_session(db, token)
    request["session"] = session
    return await handler(request)


@web.middleware
async def require_auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """미인증이면 API 는 401, 페이지는 /login 리다이렉트 (§F1)."""
    if (
        request.path in PUBLIC_PATHS
        or request.path.startswith(PUBLIC_PREFIX)
        or request.get("session") is not None
    ):
        return await handler(request)
    if request.path.startswith("/api/"):
        return api.json_error(401, "로그인이 필요합니다")
    raise web.HTTPFound("/login")


@web.middleware
async def csrf_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """변경 요청은 세션에서 파생된 토큰을 헤더로 함께 보내야 한다 (§7).

    `/api/login` 은 제외한다 — 아직 세션이 없어 파생할 토큰이 없다. 대신 쿠키가
    `SameSite=Lax` 라 다른 사이트의 폼 제출로는 세션이 실려가지 않는다.
    """
    if request.method in SAFE_METHODS or request.path == "/api/login":
        return await handler(request)
    session = request.get("session")
    if session is None:
        return api.json_error(401, "로그인이 필요합니다")
    if not csrf_matches(session.raw_token, request.headers.get(CSRF_HEADER)):
        return api.json_error(403, "CSRF 토큰이 올바르지 않습니다 — 새로고침 후 다시 시도하세요")
    return await handler(request)


def create_app(
    *,
    db_path: str | Path,
    hubctl_repo: str | Path,
    inventory_path: str | Path | None = None,
    queue=None,
    masker: SecretMasker | None = None,
    hubctl_env: dict[str, str] | None = None,
    hubctl_shell: tuple[str, ...] = ("zsh", "-lc"),
    become_password: str = "",
    log_dir: str | Path | None = None,
    notifier=None,
    console_url: str | None = None,
    session_ttl_days: int = 14,
    secure_cookie: bool = False,
    trust_forwarded: bool = False,
    static_dir: str | Path | None = None,
    forward_bind: str = "127.0.0.1",
) -> web.Application:
    """콘솔 앱. 실행은 `run_web` 또는 호출자의 AppRunner 가 맡는다."""
    hubctl_repo = Path(hubctl_repo).expanduser()
    app = web.Application(
        middlewares=[session_middleware, require_auth_middleware, csrf_middleware]
    )
    app[keys.DB_PATH] = Path(db_path).expanduser()
    app[keys.HUBCTL_REPO] = hubctl_repo
    app[keys.INVENTORY_PATH] = (
        Path(inventory_path).expanduser()
        if inventory_path is not None
        else hubctl_repo / "inventory" / "sites.yml"
    )
    app[keys.QUEUE] = queue
    app[keys.MASKER] = masker or SecretMasker()
    app[keys.HUBCTL_ENV] = dict(hubctl_env or {})
    app[keys.HUBCTL_SHELL] = tuple(hubctl_shell)
    app[keys.SESSION_TTL_DAYS] = session_ttl_days
    app[keys.SECURE_COOKIE] = secure_cookie
    app[keys.TRUST_FORWARDED] = trust_forwarded
    app[keys.STATIC_DIR] = (
        Path(static_dir).expanduser() if static_dir is not None
        else Path(__file__).with_name("static")
    )
    app[keys.THROTTLE] = LoginThrottle()
    # SSH 키 등록도 비밀번호를 받는다 — 여기도 무차별 대입을 막는다 (§9: 3회/60초).
    app[keys.SSH_THROTTLE] = LoginThrottle(max_failures=3, lock_seconds=60.0)
    app[keys.PREFLIGHT_LOCK] = asyncio.Lock()

    # 중계 리스너는 콘솔과 같은 주소에 연다. 노출 범위는 운영자가 WEB_HOST 로
    # 이미 내린 결정이고, 중계가 그보다 넓어지면 안 된다.
    app[keys.FORWARDS] = ForwardManager(bind_host=forward_bind)

    app[keys.QUEUE] = queue if queue is not None else JobQueue()
    app[keys.BROKER] = SseBroker()
    app[keys.JOB_SERVICE] = JobService(
        db_path=app[keys.DB_PATH],
        hubctl_repo=hubctl_repo,
        inventory_path=app[keys.INVENTORY_PATH],
        queue=app[keys.QUEUE],
        broker=app[keys.BROKER],
        become_password=become_password,
        masker=app[keys.MASKER],
        hubctl_env=app[keys.HUBCTL_ENV],
        hubctl_shell=app[keys.HUBCTL_SHELL],
        log_dir=log_dir,
        notifier=notifier,
        console_url=console_url,
    )

    app.on_startup.append(_start_forwards)
    app.on_cleanup.append(_stop_forwards)
    app.on_startup.append(_start_queue)
    app.on_startup.append(_start_housekeeping)
    app.on_cleanup.append(_stop_housekeeping)
    app.on_cleanup.append(_stop_queue)
    app.add_routes(api.routes)
    static_dir = app[keys.STATIC_DIR]
    if static_dir.is_dir():
        app.router.add_static("/static/", static_dir, name="static")
    return app


async def _start_forwards(app: web.Application) -> None:
    app[keys.FORWARDS].start()


async def _stop_forwards(app: web.Application) -> None:
    """앱이 내려가면 열린 중계도 전부 닫는다 — 사내망으로 가는 길을 남기지 않는다."""
    await app[keys.FORWARDS].stop()


async def _start_queue(app: web.Application) -> None:
    await app[keys.QUEUE].start()


async def _start_housekeeping(app: web.Application) -> None:
    app[keys.HOUSEKEEPER] = asyncio.create_task(_housekeeping(app), name="autodeploy-housekeeping")


async def _stop_housekeeping(app: web.Application) -> None:
    task = app.get(keys.HOUSEKEEPER)
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _housekeeping(app: web.Application) -> None:
    """방치된 승인 대기 정리 + 만료 세션 청소 (§9).

    승인 없이 남은 patch 는 큐를 차지하지 않지만 목록에서 영원히 '대기'로 보인다.
    24시간이면 사람이 잊은 것으로 본다 — 번들은 컨트롤러에 남으므로 되살릴 수 있다.
    """
    service = app[keys.JOB_SERVICE]
    while True:
        try:
            expired = await service.expire_awaiting(older_than_hours=AWAITING_TTL_HOURS)
            if expired:
                log.info("승인 없이 %d시간 지난 patch %d건 취소: %s",
                         AWAITING_TTL_HOURS, len(expired), expired)
            async with connect(app[keys.DB_PATH]) as db:
                removed = await purge_expired_sessions(db)
            if removed:
                log.info("만료 세션 %d건 정리", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("정리 작업 중 오류 — 다음 주기에 다시 시도합니다")
        await asyncio.sleep(HOUSEKEEPING_INTERVAL)


async def _stop_queue(app: web.Application) -> None:
    """앱이 내려가면 실행 중인 hubctl 도 세운다.

    `start_new_session=True` 로 띄운 자식은 부모가 죽어도 살아남는다. 감독자 없이
    도는 ansible 을 남기지 않으려면 여기서 취소를 내려야 한다.
    """
    await app[keys.QUEUE].stop()


async def run_web(
    app: web.Application, *, host: str, port: int
) -> tuple[web.AppRunner, web.TCPSite]:
    """앱을 띄우고 (runner, site) 를 돌려준다. 종료는 `runner.cleanup()`."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("웹 콘솔: http://%s:%d", host, port)
    return runner, site
