"""aiohttp 앱 팩토리 + 미들웨어 (dev-spec-web-console §5).

기존 Slack 데몬과 **같은 프로세스**에서 돈다. 데몬은 이미 asyncio 루프를 돌리고
있으므로 aiohttp 를 그 위에 올리면 되고, 별도 프로세스를 띄우면 SQLite 를 두
프로세스가 쓰게 되어 잠금 문제가 생긴다.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from autodeploy.db import connect
from autodeploy.masking import SecretMasker
from autodeploy.web import api, keys
from autodeploy.web.auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    LoginThrottle,
    csrf_matches,
    resolve_session,
)

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# 세션 없이 접근할 수 있는 경로. 그 외 전부 인증이 필요하다.
PUBLIC_PATHS = frozenset({"/login", "/api/login", "/healthz"})

# 상태를 바꾸지 않는 메서드는 CSRF 검사 대상이 아니다.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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
    if request.path in PUBLIC_PATHS or request.get("session") is not None:
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
    session_ttl_days: int = 14,
    secure_cookie: bool = False,
    trust_forwarded: bool = False,
    static_dir: str | Path | None = None,
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
    app[keys.PREFLIGHT_LOCK] = asyncio.Lock()
    app.add_routes(api.routes)
    return app


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
