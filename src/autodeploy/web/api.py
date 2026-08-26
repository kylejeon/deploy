"""JSON 엔드포인트 (dev-spec-web-console §7).

Phase C 범위: 인증 + 조회. 변경 계열(작업 생성·취소·승인, 서버 추가·수정·삭제,
SSH 키 등록, SSE)은 Phase D 에서 붙인다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from pathlib import Path

from aiohttp import web

from autodeploy import repository
from autodeploy.db import connect
from autodeploy.hubctl import HubctlRunner, build_preflight_command
from autodeploy.inventory import InventoryError, load_inventory
from autodeploy.web import keys
from autodeploy.web.auth import (
    SESSION_COOKIE,
    LOGIN_FAILED_MESSAGE,
    destroy_session,
    login,
)

log = logging.getLogger(__name__)

MAX_JOB_LIMIT = 200
DEFAULT_JOB_LIMIT = 50

routes = web.RouteTableDef()

# 응답 본문의 한글을 \uXXXX 로 이스케이프하지 않는다. 유효한 JSON 이지만
# 로그·curl 로 들여다볼 때 오류 메시지를 사람이 못 읽는다.
_dumps = partial(json.dumps, ensure_ascii=False)


def json_response(data, *, status: int = 200, **kw) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps, **kw)


def json_error(status: int, message: str, **extra) -> web.Response:
    return json_response({"error": message, **extra}, status=status)


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            text='{"error": "본문이 올바른 JSON 이 아닙니다"}',
            content_type="application/json",
        )
    return data if isinstance(data, dict) else {}


def client_ip(request: web.Request) -> str:
    """리버스 프록시 뒤에서도 실제 클라이언트를 집는다.

    `X-Forwarded-For` 는 클라이언트가 위조할 수 있으므로 신뢰하는 프록시를
    거쳤을 때만 쓴다. v1 은 LAN 직결이라 기본은 소켓 주소이고, 프록시를 앞에
    두는 시점에 `trust_forwarded` 를 켠다 (§11).
    """
    if request.app[keys.TRUST_FORWARDED]:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    peer = request.remote
    return peer or "unknown"


# ── 인증 ────────────────────────────────────────────────────────────


@routes.post("/api/login")
async def post_login(request: web.Request) -> web.Response:
    data = await _body(request)
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    if not username or not password:
        return json_error(400, LOGIN_FAILED_MESSAGE)

    async with connect(request.app[keys.DB_PATH]) as db:
        result = await login(
            db,
            username=username,
            password=password,
            client_ip=client_ip(request),
            throttle=request.app[keys.THROTTLE],
            ttl_days=request.app[keys.SESSION_TTL_DAYS],
        )

    if not result.ok:
        # 429 는 잠금, 401 은 자격 불일치. 문구는 둘 다 아이디 존재 여부를 안 흘린다.
        status = 429 if result.retry_after > 0 else 401
        payload: dict[str, object] = {}
        if result.retry_after > 0:
            payload["retry_after"] = int(result.retry_after) + 1
        return json_error(status, result.message, **payload)

    session = result.session
    assert session is not None
    response = json_response(
        {"username": session.user.username, "csrf_token": session.csrf_token}
    )
    _set_session_cookie(request, response, session.raw_token)
    return response


@routes.post("/api/logout")
async def post_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE)
    async with connect(request.app[keys.DB_PATH]) as db:
        await destroy_session(db, token)
    response = json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


@routes.get("/api/me")
async def get_me(request: web.Request) -> web.Response:
    session = request["session"]
    return json_response(
        {
            "username": session.user.username,
            "csrf_token": session.csrf_token,
            "last_login_at": session.user.last_login_at,
        }
    )


def _set_session_cookie(request: web.Request, response: web.Response, raw_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=request.app[keys.SESSION_TTL_DAYS] * 24 * 3600,
        httponly=True,
        samesite="Lax",
        secure=request.app[keys.SECURE_COOKIE],
        path="/",
    )


# ── 서버 (조회) ─────────────────────────────────────────────────────


@routes.get("/api/servers")
async def get_servers(request: web.Request) -> web.Response:
    # 저장소 경로가 틀린 것과 sites.yml 이 아직 없는 것은 다르다. 전자는 설정
    # 오류라 알려야 하고(§9), 후자는 서버를 한 대도 안 넣은 정상 초기 상태다.
    repo = request.app[keys.HUBCTL_REPO]
    if not repo.is_dir():
        return json_error(
            503,
            f"hub-provisioning 저장소를 찾을 수 없습니다: {repo}"
            " — HUBCTL_REPO_PATH 설정을 확인하세요",
        )

    try:
        inventory = load_inventory(request.app[keys.INVENTORY_PATH])
    except InventoryError as exc:
        return json_error(500, f"인벤토리를 읽을 수 없습니다: {exc}")

    async with connect(request.app[keys.DB_PATH]) as db:
        meta = await repository.get_server_meta(db)

    servers = [
        {
            "host": s.host,
            "ansible_host": s.ansible_host,
            "ansible_user": s.ansible_user,
            "site_name": s.site_name,
            "profile": s.profile,
            "memo": meta.get(s.host, {}).get("memo"),
            "key_installed_at": meta.get(s.host, {}).get("key_installed_at"),
        }
        for s in inventory.servers
    ]
    # mtime_ns 는 저장 시 낙관적 잠금 키로 되돌아온다 (§F2).
    return json_response({"servers": servers, "mtime_ns": inventory.mtime_ns})


# ── 작업 (조회) ─────────────────────────────────────────────────────


@routes.get("/api/jobs")
async def get_jobs(request: web.Request) -> web.Response:
    limit = _int_param(request, "limit", DEFAULT_JOB_LIMIT, 1, MAX_JOB_LIMIT)
    async with connect(request.app[keys.DB_PATH]) as db:
        jobs = await repository.list_jobs(db, limit=limit)
    queue = request.app.get(keys.QUEUE)
    if queue is not None:
        for job in jobs:
            position = queue.position(job["id"])
            if position:
                job["queue_position"] = position
    return json_response({"jobs": jobs})


@routes.get("/api/jobs/{job_id}")
async def get_job(request: web.Request) -> web.Response:
    job_id = _int_path(request, "job_id")
    async with connect(request.app[keys.DB_PATH]) as db:
        job = await repository.get_job_detail(db, job_id)
    if job is None:
        return json_error(404, f"작업 {job_id} 을(를) 찾을 수 없습니다")
    queue = request.app.get(keys.QUEUE)
    if queue is not None:
        position = queue.position(job_id)
        if position:
            job["queue_position"] = position
    return json_response(job)


@routes.get("/api/jobs/{job_id}/log")
async def get_job_log(request: web.Request) -> web.Response:
    """전체 로그 텍스트. 브라우저에서 파일로 저장하거나 grep 하기 위한 용도."""
    job_id = _int_path(request, "job_id")
    async with connect(request.app[keys.DB_PATH]) as db:
        if await repository.get_job_detail(db, job_id) is None:
            return json_error(404, f"작업 {job_id} 을(를) 찾을 수 없습니다")
        lines: list[str] = []
        after = 0
        while True:
            chunk = await repository.get_script_logs(db, job_id, after_id=after, limit=5000)
            if not chunk:
                break
            lines.extend(row["line"] for row in chunk)
            after = chunk[-1]["id"]

    return web.Response(
        text="\n".join(lines) + ("\n" if lines else ""),
        content_type="text/plain",
        charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}.log"'},
    )


# ── preflight ───────────────────────────────────────────────────────


@routes.get("/api/preflight")
async def get_preflight(request: web.Request) -> web.Response:
    """대시보드 자격 점검 스트립. 작업으로 기록하지 않는 읽기 전용 호출."""
    lock: asyncio.Lock = request.app[keys.PREFLIGHT_LOCK]
    if lock.locked():
        return json_error(409, "이미 자격 점검이 진행 중입니다")

    async with lock:
        runner = HubctlRunner(
            request.app[keys.HUBCTL_REPO],
            masker=request.app[keys.MASKER],
            env_overrides=request.app[keys.HUBCTL_ENV],
            login_shell=request.app[keys.HUBCTL_SHELL],
        )
        checks: list[dict] = []

        def on_line(stream: str, parsed) -> None:
            text = parsed.text.strip()
            if not text.startswith(("✔ ", "✘ ", "! ")):
                return
            message = text[2:].strip()
            # hubctl 의 finish() 가 마지막에 찍는 전체 요약("Preflight succeeded"/
            # "Preflight failed (exit N)")은 항목이 아니다. ok/exit_code 가 이미
            # 같은 내용을 담고 있어 스트립에 5번째 타일로 끼면 오해를 부른다.
            if message.startswith(("Preflight succeeded", "Preflight failed")):
                return
            checks.append({"level": parsed.kind.value, "message": message})

        try:
            result = await runner.run(build_preflight_command(), on_line=on_line)
        except Exception as exc:
            log.exception("preflight 실행 실패")
            return json_error(503, f"preflight 를 실행할 수 없습니다: {exc}")

    return json_response(
        {"ok": result.exit_code == 0, "exit_code": result.exit_code, "checks": checks}
    )


# ── 페이지 ──────────────────────────────────────────────────────────


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    if request.get("session") is None:
        raise web.HTTPFound("/login")
    return _page(request, "console.html")


@routes.get("/login")
async def login_page(request: web.Request) -> web.Response:
    if request.get("session") is not None:
        raise web.HTTPFound("/")
    return _page(request, "login.html")


@routes.get("/healthz")
async def healthz(request: web.Request) -> web.Response:
    return json_response({"ok": True})


def _page(request: web.Request, name: str) -> web.Response:
    static_dir: Path = request.app[keys.STATIC_DIR]
    path = static_dir / name
    if not path.is_file():
        # Phase E 에서 프로토타입 HTML 을 이 자리에 넣는다.
        return web.Response(
            text=f"{name} 이 아직 없습니다 (Phase E). API 는 /api/* 로 동작합니다.",
            content_type="text/plain",
            charset="utf-8",
            status=503,
        )
    return web.FileResponse(path)


# ── 파라미터 ────────────────────────────────────────────────────────


def _int_param(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise web.HTTPBadRequest(
            text=f'{{"error": "{name} 은 정수여야 합니다"}}', content_type="application/json"
        )
    return max(lo, min(hi, value))


def _int_path(request: web.Request, name: str) -> int:
    try:
        return int(request.match_info[name])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(
            text=f'{{"error": "{name} 이 올바르지 않습니다"}}', content_type="application/json"
        )
