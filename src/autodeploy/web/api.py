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

from autodeploy import __version__, repository
from autodeploy.db import connect
from autodeploy.hubctl import HubctlRunner, build_preflight_command
from autodeploy.inventory import (
    InventoryConflict,
    InventoryError,
    Server,
    load_inventory,
    remove_server,
    upsert_server,
    validate_server,
)
from autodeploy.models import JobKind
from autodeploy.node_info import fetch_serial
from autodeploy.ssh_keys import (
    DEFAULT_KEY_PATH,
    SSHKeyError,
    is_desktop_profile,
    register_key,
    steps_for,
)
from autodeploy.web import keys
from autodeploy.web.forwards import ForwardError
from autodeploy.web.forwards import plan as forward_plan
from autodeploy.web.jobs import JobConflict, JobError, JobRequest
from autodeploy.web.auth import (
    SESSION_COOKIE,
    LOGIN_FAILED_MESSAGE,
    destroy_session,
    login,
)

log = logging.getLogger(__name__)

MAX_JOB_LIMIT = 200
DEFAULT_JOB_LIMIT = 50
# 유휴 연결이 프록시에 끊기지 않도록 보내는 주석 간격 (§F4).
SSE_HEARTBEAT_SECONDS = 15.0

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
            # 콘솔은 이미 /api/me 를 부른다. 버전 하나 때문에 요청을 더 만들지 않는다.
            "version": __version__,
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
            "anydesk_id": meta.get(s.host, {}).get("anydesk_id"),
            "serial": meta.get(s.host, {}).get("serial"),
        }
        for s in inventory.servers
    ]
    # mtime_ns 는 저장 시 낙관적 잠금 키로 되돌아온다 (§F2).
    return json_response({"servers": servers, "mtime_ns": _version(inventory.mtime_ns)})


# ── 포트포워딩 (사내망 타겟 임시 중계) ──────────────────────────────


def _find_server(request: web.Request, host: str):
    """인벤토리에 있는 이름만 돌려준다. 없으면 None.

    임의의 host 를 그대로 받으면 콘솔이 사내망 프록시가 된다. 등록된 서버만
    통과시키는 것이 이 함수의 존재 이유다.
    """
    try:
        inventory = load_inventory(request.app[keys.INVENTORY_PATH])
    except InventoryError:
        return None
    for server in inventory.servers:
        if server.host == host:
            return server
    return None


@routes.get("/api/forwards")
async def get_forwards(request: web.Request) -> web.Response:
    """열려 있는 중계 목록. `host`(+`env`)를 주면 그 서버의 포트별 계획도 함께."""
    body: dict = {"forwards": request.app[keys.FORWARDS].list()}

    host = request.query.get("host", "").strip()
    if host:
        server = _find_server(request, host)
        if server is None:
            return json_error(404, f"인벤토리에 없는 서버입니다: {host}")
        env = request.query.get("env", "").strip() or None
        body["host"] = host
        body["profile"] = server.profile
        body["env"] = env
        # 터미널 줄이 `ssh://<사용자>@<콘솔주소>:<중계포트>` 를 만들 때 쓴다.
        # 인벤토리의 ansible_user 가 곧 그 서버에 들어갈 계정이다.
        body["user"] = server.ansible_user
        body["entries"] = forward_plan(profile=server.profile, env=env)
    return json_response(body)


@routes.post("/api/forwards")
async def open_forward(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_error(400, "JSON 본문이 필요합니다")

    host = str(data.get("host", "")).strip()
    try:
        port = int(data.get("port", 0))
    except (TypeError, ValueError):
        return json_error(400, "port 는 정수여야 합니다")
    env = str(data.get("env", "")).strip() or None

    server = _find_server(request, host)
    if server is None:
        return json_error(404, f"인벤토리에 없는 서버입니다: {host or '(비어 있음)'}")

    # 화면에서 안 보이게 하는 것만으로는 부족하다 — API 로는 여전히 열 수 있다.
    # hybrid 의 중앙 포트는 중계할 대상이 사이트에 없으므로 여기서 막는다.
    entry = next(
        (e for e in forward_plan(profile=server.profile, env=env) if e["port"] == port),
        None,
    )
    if entry is not None and entry["mode"] == "cloud":
        return json_error(
            400,
            f"{server.profile} 에서 {port} 는 중앙 주소로 접속합니다: {entry['url']}"
            " — 중계할 대상이 사이트에 없습니다",
        )
    if entry is not None and entry["mode"] == "unknown":
        return json_error(
            400,
            f"{server.profile} 는 환경(dev/stage/prod)에 따라 {port} 의 주소가 다릅니다."
            " 이 작업에는 환경 정보가 없어 주소를 정할 수 없습니다"
            " — 설치/구성 작업에서 열어주세요",
        )

    try:
        forward = await request.app[keys.FORWARDS].open(
            host=host, address=server.ansible_host, port=port
        )
    except ForwardError as exc:
        return json_error(400, str(exc))
    except OSError as exc:
        return json_error(500, f"중계를 열지 못했습니다: {exc}")
    return json_response(forward.as_dict(), status=201)


@routes.delete("/api/forwards/{key}")
async def close_forward(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    if not await request.app[keys.FORWARDS].close(key):
        return json_error(404, f"열려 있지 않습니다: {key}")
    return json_response({"ok": True})


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


@routes.delete("/api/jobs")
async def delete_jobs(request: web.Request) -> web.Response:
    """작업 기록 삭제. `{"all": true}` 면 끝난 것 전부, 아니면 `{"ids": [...]}`.

    진행 중인 작업은 지우지 않는다 (repository.delete_jobs 참고). 요청에
    섞여 있으면 건너뛴 id 를 그대로 돌려줘 화면이 이유를 말할 수 있게 한다.
    """
    data = await _body(request)
    ids: list[int] | None
    if data.get("all") is True:
        ids = None
    else:
        raw = data.get("ids")
        if not isinstance(raw, list) or not raw:
            return json_error(400, "지울 작업을 고르세요 (ids) 또는 all: true")
        try:
            ids = [int(x) for x in raw]
        except (TypeError, ValueError):
            return json_error(400, "ids 는 정수 목록이어야 합니다")

    async with connect(request.app[keys.DB_PATH]) as db:
        deleted, skipped = await repository.delete_jobs(db, job_ids=ids)
    log.info("작업 기록 삭제: %d건 (건너뜀 %d건)", len(deleted), len(skipped))
    return json_response({"deleted": deleted, "skipped": skipped})


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


# ── 서버 (변경) ─────────────────────────────────────────────────────


def _server_from_body(data: dict, *, host: str | None = None) -> Server:
    server = Server(
        host=str(data.get("host", host or "")).strip(),
        ansible_host=str(data.get("ansible_host", "")).strip(),
        ansible_user=str(data.get("ansible_user", "")).strip(),
        site_name=str(data.get("site_name", "")).strip(),
        profile=str(data.get("profile", "")).strip(),
    )
    try:
        validate_server(server)
    except InventoryError as exc:
        raise web.HTTPBadRequest(
            text=_dumps({"error": str(exc)}), content_type="application/json"
        )
    return server


async def _reject_if_busy(request: web.Request) -> web.Response | None:
    """진행 중 작업이 있으면 인벤토리를 못 고치게 한다 (§F2).

    실행 중인 playbook 의 `-l` 은 이미 정해져 있지만, 같은 파일을 다시 읽는
    역할(delegate 포함)이 있고 사람이 화면에서 본 목록과 실제 대상이 어긋나기
    시작한다. 작업이 끝난 뒤에 고치는 편이 안전하다.
    """
    async with connect(request.app[keys.DB_PATH]) as db:
        active = await repository.active_job_ids(db)
    if active:
        # 막고 있는 작업을 번호로 짚어준다. 그냥 "1건" 이라고만 하면 어디를 봐야
        # 하는지 알 수 없고, 특히 재시작을 넘긴 좀비 작업일 때 손을 못 쓴다.
        names = ", ".join(f"#{i}" for i in active)
        return json_error(
            409,
            f"진행 중인 작업이 {len(active)}건 있어 서버 목록을 바꿀 수 없습니다"
            f" ({names}) — 끝나기를 기다리거나 그 작업을 취소하세요",
        )
    return None


def _version(mtime_ns: int) -> str:
    """인벤토리 낙관적 잠금 키를 **문자열로** 내보낸다.

    `st_mtime_ns` 는 나노초라 61비트다. JSON 숫자로 내보내면 브라우저의
    `JSON.parse` 가 double 로 읽는데, double 은 정수를 2^53 까지만 정확히
    담는다. 이 크기에서는 값 사이 간격이 256ns 라 거의 항상 반올림된다.

    그러면 화면이 돌려준 값이 파일 시각과 달라지고, 서버는 "다른 곳에서
    수정됐다" 고 판단한다 — **서버 편집이 항상 409 로 실패한다.** 실제 mtime
    20개로 재봤더니 20개 전부 값이 바뀌었다.

    화면은 이 값을 되돌려주기만 하고 계산에 쓰지 않는다. 문자열이면 정밀도가
    깎일 일이 없다. `_mtime` 이 int() 로 되돌리므로 받는 쪽은 그대로다.
    """
    return str(mtime_ns)


def _mtime(data: dict) -> int | None:
    raw = data.get("mtime_ns")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(
            text=_dumps({"error": "mtime_ns 가 올바르지 않습니다"}),
            content_type="application/json",
        )


@routes.post("/api/servers")
async def post_server(request: web.Request) -> web.Response:
    busy = await _reject_if_busy(request)
    if busy is not None:
        return busy
    data = await _body(request)
    server = _server_from_body(data)
    path = request.app[keys.INVENTORY_PATH]

    if load_inventory(path).get(server.host) is not None:
        return json_error(409, f"이미 등록된 서버입니다: {server.host}")
    try:
        mtime_ns = upsert_server(path, server, expect_mtime_ns=_mtime(data))
    except InventoryConflict as exc:
        return json_error(409, str(exc))
    except InventoryError as exc:
        return json_error(400, str(exc))
    log.info("서버 추가: %s (by %s)", server.host, request["session"].user.username)
    return json_response({"host": server.host, "mtime_ns": _version(mtime_ns)}, status=201)


@routes.put("/api/servers/{host}")
async def put_server(request: web.Request) -> web.Response:
    busy = await _reject_if_busy(request)
    if busy is not None:
        return busy
    host = request.match_info["host"]
    data = await _body(request)
    path = request.app[keys.INVENTORY_PATH]

    existing = load_inventory(path).get(host)
    if existing is None:
        return json_error(404, f"등록되지 않은 서버입니다: {host}")

    server = _server_from_body({**data, "host": data.get("host", host)}, host=host)
    if server.host != host:
        # 이름이 바뀌면 job_hosts·server_meta 의 참조가 끊긴다. 삭제 후 추가로 유도한다.
        return json_error(400, "호스트명은 바꿀 수 없습니다 — 삭제 후 다시 추가하세요")

    try:
        mtime_ns = upsert_server(path, server, expect_mtime_ns=_mtime(data))
    except InventoryConflict as exc:
        return json_error(409, str(exc))
    except InventoryError as exc:
        return json_error(400, str(exc))

    if "memo" in data:
        memo = data["memo"]
        async with connect(request.app[keys.DB_PATH]) as db:
            await repository.set_server_memo(db, host, str(memo) if memo else None)
    return json_response({"host": host, "mtime_ns": _version(mtime_ns)})


@routes.delete("/api/servers/{host}")
async def delete_server(request: web.Request) -> web.Response:
    busy = await _reject_if_busy(request)
    if busy is not None:
        return busy
    host = request.match_info["host"]
    data = await _body(request)
    try:
        mtime_ns = remove_server(
            request.app[keys.INVENTORY_PATH], host, expect_mtime_ns=_mtime(data)
        )
    except InventoryConflict as exc:
        return json_error(409, str(exc))
    except InventoryError as exc:
        return json_error(404, str(exc))

    async with connect(request.app[keys.DB_PATH]) as db:
        await repository.delete_server_meta(db, host)
    log.info("서버 삭제: %s (by %s)", host, request["session"].user.username)
    return json_response({"host": host, "mtime_ns": _version(mtime_ns)})


@routes.post("/api/servers/{host}/ssh-key")
async def post_ssh_key(request: web.Request) -> web.Response:
    """F9. 비밀번호는 이 요청에서만 쓰고 DB·로그·응답 어디에도 남기지 않는다."""
    host = request.match_info["host"]
    data = await _body(request)
    password = str(data.get("password", ""))
    if not password:
        return json_error(400, "타겟 서버의 비밀번호를 입력하세요")

    server = load_inventory(request.app[keys.INVENTORY_PATH]).get(host)
    if server is None:
        return json_error(404, f"등록되지 않은 서버입니다: {host}")

    # 비밀번호를 계속 넣어보는 것도 무차별 대입이다 (§9: 3회 실패 시 60초).
    throttle = request.app[keys.SSH_THROTTLE]
    ip = client_ip(request)
    remaining = throttle.retry_after(ip, host)
    if remaining > 0:
        return json_error(429, f"시도가 너무 많습니다. {int(remaining) + 1}초 후 다시 시도하세요")

    # hybrid 사이트의 타겟은 사람이 앞에 앉는 데스크톱이라 원격 지원 준비가 필요하다
    # (Wayland 끄기 · 화면 잠금 해제 · AnyDesk). onprem 은 폐쇄망 서버라 해당 없다.
    prep = request.app[keys.NODE_PREP]
    prepare = is_desktop_profile(server.profile)

    # 이 요청은 몇 분이 걸릴 수 있다 (hybrid 는 apt 로 AnyDesk 를 깐다). 화면이
    # 멈춘 것처럼 보이지 않도록 진행 상태를 따로 적어두고, 화면은 그걸 폴링한다.
    # 준비 스크립트 출력에는 AnyDesk 비밀번호가 섞일 수 있으므로 **적기 전에** 가린다.
    board = request.app[keys.SSH_PROGRESS]
    masker = request.app[keys.MASKER]
    board.start(host, steps_for(prepare))
    try:
        registration = await register_key(
            host=server.ansible_host,
            username=server.ansible_user,
            password=password,
            prepare=prepare,
            anydesk_password=prep.anydesk_password,
            weekly_reboot=prep.weekly_reboot,
            weekly_reboot_cron=prep.weekly_reboot_cron,
            on_step=lambda step: board.step(host, step),
            on_line=lambda line: board.detail(host, masker(line.line)),
        )
    except SSHKeyError as exc:
        throttle.record_failure(ip, host)
        return json_error(400, str(exc))
    except Exception as exc:
        throttle.record_failure(ip, host)
        log.warning("SSH 키 등록 실패 (%s): %s", host, type(exc).__name__)
        return json_error(400, f"타겟에 접속할 수 없습니다: {exc}")
    finally:
        board.finish(host)

    throttle.reset(ip, host)
    async with connect(request.app[keys.DB_PATH]) as db:
        await repository.mark_key_installed(db, host)
        if registration.anydesk_id:
            # 메모는 사람이 쓴 글이라 덮어쓰지 않는다. AnyDesk ID 는 따로 두고
            # 화면의 메모 칸에 나란히 보여준다.
            await repository.set_anydesk_id(db, host, registration.anydesk_id)
        if registration.serial:
            await repository.set_server_serial(db, host, registration.serial)
        meta = await repository.get_server_meta(db)
    if not registration.sleep_masked:
        log.warning("절전 타겟 mask 실패 (%s): %s", host, registration.sleep_error)
    if registration.prep_error:
        log.warning("타겟 준비 실패 (%s): %s", host, registration.prep_error)

    # 스크립트 출력에는 AnyDesk 비밀번호가 섞일 수 있다. 화면으로 내보내기 전에
    # 반드시 가린다 — 이 응답은 그대로 브라우저 콘솔에도 남는다.
    return json_response(
        {
            "host": host,
            "key_installed_at": (meta.get(host) or {}).get("key_installed_at"),
            # 키는 됐는데 절전만 안 걸린 경우를 화면이 구분해 말할 수 있게 한다.
            "sleep_masked": registration.sleep_masked,
            "sleep_error": registration.sleep_error,
            "prep_ran": registration.prep_ran,
            "prep_error": registration.prep_error,
            "prep_log": [masker(line) for line in registration.prep_log],
            "anydesk_id": registration.anydesk_id,
            "serial": registration.serial,
            "serial_error": registration.serial_error,
        }
    )


@routes.get("/api/servers/{host}/ssh-key/progress")
async def get_ssh_key_progress(request: web.Request) -> web.Response:
    """등록이 지금 어느 단계인지. 화면이 1초에 한 번 물어본다.

    등록 중이 아니면 `running: false` 다 — 없는 것과 끝난 것을 구분할 이유가
    없다. 이 값은 프로세스 메모리에만 있어서 데몬을 다시 띄우면 사라진다.
    """
    board = request.app[keys.SSH_PROGRESS]
    item = board.get(request.match_info["host"])
    return json_response(item.as_dict() if item is not None else {"running": False})


@routes.post("/api/servers/{host}/serial")
async def post_server_serial(request: web.Request) -> web.Response:
    """본체 시리얼을 다시 읽는다 (`dmidecode -s system-serial-number`).

    키 등록 때 이미 한 번 읽는다. 이 경로는 **그 전에 등록된 서버**와 **본체를
    바꾼 서버**를 위한 것이다.

    비밀번호를 받지 않는다: 접속은 등록해둔 키로 하고, sudo 에는 설치가 쓰는
    것과 같은 `.env` 의 값을 쓴다. 사람이 다시 입력할 이유가 없고, 새 비밀을
    화면에 통과시키지 않는 편이 낫다.
    """
    host = request.match_info["host"]
    server = load_inventory(request.app[keys.INVENTORY_PATH]).get(host)
    if server is None:
        return json_error(404, f"등록되지 않은 서버입니다: {host}")

    async with connect(request.app[keys.DB_PATH]) as db:
        meta = await repository.get_server_meta(db)
    if not (meta.get(host) or {}).get("key_installed_at"):
        return json_error(
            400,
            f"{host} 은(는) SSH 키가 등록되지 않아 접속할 수 없습니다"
            " — 먼저 키 등록을 실행하세요",
        )

    sudo_password = request.app[keys.BECOME_PASSWORD]
    if not sudo_password:
        return json_error(
            400,
            "타겟에서 sudo 를 쓸 비밀번호가 설정돼 있지 않습니다"
            " — .env 의 SSH_PASSWORD (또는 BECOME_PASSWORD) 를 채운 뒤 데몬을 다시 시작하세요",
        )

    masker = request.app[keys.MASKER]
    try:
        read = await fetch_serial(
            host=server.ansible_host,
            username=server.ansible_user,
            key_path=DEFAULT_KEY_PATH,
            sudo_password=sudo_password,
        )
    except Exception as exc:
        log.info("시리얼 조회 실패 (%s): %s", host, type(exc).__name__)
        return json_error(400, masker(f"타겟에 접속할 수 없습니다: {exc}"))

    if not read.ok:
        # 실패 이유에는 타겟의 출력이 섞인다. 비밀이 묻어 나가지 않게 가린다.
        return json_error(400, masker(read.error or "시리얼을 읽지 못했습니다"))

    async with connect(request.app[keys.DB_PATH]) as db:
        await repository.set_server_serial(db, host, read.serial)
    log.info("시리얼 조회: %s (by %s)", host, request["session"].user.username)
    return json_response({"host": host, "serial": read.serial})


# ── 작업 (변경) ─────────────────────────────────────────────────────


@routes.post("/api/jobs")
async def post_job(request: web.Request) -> web.Response:
    data = await _body(request)
    raw_kind = str(data.get("kind", "")).strip()
    try:
        kind = JobKind(raw_kind)
    except ValueError:
        return json_error(400, f"알 수 없는 작업 종류입니다: {raw_kind!r}")

    hosts = data.get("hosts") or []
    if not isinstance(hosts, list):
        return json_error(400, "hosts 는 배열이어야 합니다")

    service = request.app[keys.JOB_SERVICE]
    req = JobRequest(
        kind=kind,
        started_by=f"web:{request['session'].user.username}",
        hosts=tuple(str(h) for h in hosts),
        env=_opt(data, "env"),
        ref=_opt(data, "ref"),
        ref_type=_opt(data, "ref_type"),
        clean_mode=_opt(data, "clean_mode"),
        only=_opt(data, "only"),
        sync_branch=_opt(data, "sync_branch"),
        confirm=_opt(data, "confirm"),
    )
    try:
        job_id = await service.create(req)
    except JobError as exc:
        return json_error(400, str(exc))
    except JobConflict as exc:
        return json_error(409, str(exc))

    return json_response(
        {"id": job_id, "queue_position": request.app[keys.QUEUE].position(job_id)},
        status=201,
    )


@routes.post("/api/jobs/{job_id}/cancel")
async def post_job_cancel(request: web.Request) -> web.Response:
    job_id = _int_path(request, "job_id")
    service = request.app[keys.JOB_SERVICE]
    try:
        outcome = await service.cancel(job_id, by=request["session"].user.username)
    except JobError as exc:
        return json_error(404, str(exc))
    except JobConflict as exc:
        return json_error(409, str(exc))
    return json_response({"id": job_id, "outcome": outcome})


@routes.post("/api/jobs/{job_id}/approve")
async def post_job_approve(request: web.Request) -> web.Response:
    job_id = _int_path(request, "job_id")
    service = request.app[keys.JOB_SERVICE]
    try:
        await service.approve(job_id, by=request["session"].user.username)
    except JobError as exc:
        return json_error(404, str(exc))
    except JobConflict as exc:
        return json_error(409, str(exc))
    return json_response({"id": job_id, "status": "queued"})


@routes.post("/api/jobs/{job_id}/reject")
async def post_job_reject(request: web.Request) -> web.Response:
    job_id = _int_path(request, "job_id")
    service = request.app[keys.JOB_SERVICE]
    try:
        await service.reject(job_id, by=request["session"].user.username)
    except JobError as exc:
        return json_error(404, str(exc))
    except JobConflict as exc:
        return json_error(409, str(exc))
    return json_response({"id": job_id, "status": "cancelled"})


# ── SSE ─────────────────────────────────────────────────────────────


@routes.get("/api/jobs/{job_id}/stream")
async def get_job_stream(request: web.Request) -> web.StreamResponse:
    """`after` 이후의 줄부터 흘린다 (§F4).

    구독을 **먼저** 걸고 나서 DB 를 읽는다. 반대로 하면 그 사이에 적재된 줄이
    양쪽 어디에도 안 걸려 사라진다. 겹치는 구간은 id 로 걸러낸다.
    """
    job_id = _int_path(request, "job_id")
    after = _int_param(request, "after", 0, 0, 2**62)

    async with connect(request.app[keys.DB_PATH]) as db:
        job = await repository.get_job_detail(db, job_id)
    if job is None:
        return json_error(404, f"작업 {job_id} 을(를) 찾을 수 없습니다")

    broker = request.app[keys.BROKER]
    sub = broker.subscribe(job_id)

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx 등 프록시가 응답을 모아두면 실시간이 아니게 된다.
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)

    try:
        sent = after
        async with connect(request.app[keys.DB_PATH]) as db:
            while True:
                chunk = await repository.get_script_logs(db, job_id, after_id=sent)
                if not chunk:
                    break
                for row in chunk:
                    await _sse_send(response, {"type": "line", **row})
                sent = chunk[-1]["id"]

        if job["status"] in repository.ACTIVE_STATUSES:
            await _sse_pump(response, sub, sent)

        await _sse_send(response, {"type": "end", "status": await _status(request, job_id)})
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        broker.unsubscribe(sub)
    return response


async def _status(request: web.Request, job_id: int) -> str | None:
    async with connect(request.app[keys.DB_PATH]) as db:
        async with db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)) as cur:
            row = await cur.fetchone()
    return row["status"] if row else None


async def _sse_pump(response: web.StreamResponse, sub, sent: int) -> None:
    while True:
        try:
            event = await asyncio.wait_for(sub.get(), SSE_HEARTBEAT_SECONDS)
        except TimeoutError:
            # 주석 하트비트. 중간 프록시가 유휴 연결을 끊는 것을 막는다.
            await response.write(b": ping\n\n")
            continue
        if event is None:
            return
        if event.get("type") == "line":
            if event["id"] <= sent:
                continue  # 따라잡기 구간과 겹친 줄
            sent = event["id"]
        await _sse_send(response, event)


async def _sse_send(response: web.StreamResponse, event: dict) -> None:
    payload = _dumps(event)
    prefix = f"id: {event['id']}\n" if "id" in event else ""
    await response.write(f"{prefix}data: {payload}\n\n".encode())


def _opt(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    # 버전을 여기 싣는다. 로그인 화면은 세션이 없어 /api/me 를 못 부르는데,
    # 화면에 버전을 띄우려면 인증 없이 받을 곳이 하나는 있어야 한다.
    return json_response({"ok": True, "version": __version__})


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
