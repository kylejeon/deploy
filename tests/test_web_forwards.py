"""사내망 타겟으로 가는 임시 TCP 중계.

중계는 콘솔이 병원망으로 내는 길이다. 여기서 검증하는 것의 절반은 "동작한다"가
아니라 **"임의의 주소·포트로는 못 연다"** 쪽이다.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from autodeploy.accounts import create_user
from autodeploy.db import connect
from autodeploy.web import api, create_app, forwards
from autodeploy.web.auth import CSRF_HEADER

USERNAME = "yonghyuk"
PASSWORD = "correct-horse-battery"

SITES_YML = """\
sites:
  hosts:
    testpc:
      ansible_host: 127.0.0.1
      ansible_user: connecteve
      site_name: testpc
      profile: onprem
    faraway:
      ansible_host: 192.0.2.10
      ansible_user: connecteve
      site_name: faraway
      profile: onprem
    hyb:
      ansible_host: 127.0.0.1
      ansible_user: connecteve
      site_name: hyb
      profile: hybrid-with-ai
    tailnet:
      ansible_host: 100.101.102.103
      ansible_user: connecteve
      site_name: tailnet
      profile: onprem
"""


@pytest.fixture
async def echo():
    """타겟 흉내 — 받은 바이트를 그대로 돌려준다."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(1024):
                writer.write(b"echo:" + data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def client(temp_db, tmp_path, echo, monkeypatch):
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
    inventory = tmp_path / "sites.yml"
    inventory.write_text(SITES_YML, encoding="utf-8")
    repo = tmp_path / "hub-provisioning"
    repo.mkdir()

    # 허용 포트는 traefik hostPort 로 고정돼 있다. 테스트가 그 포트를 실제로
    # 점유하면 개발기와 충돌하므로, 임시 echo 포트를 한 건만 허용에 얹는다.
    monkeypatch.setitem(forwards.ALLOWED_PORTS, echo, "테스트")

    app = create_app(
        db_path=temp_db,
        hubctl_repo=repo,
        inventory_path=inventory,
        static_dir=tmp_path / "static",
        hubctl_shell=("bash", "-c"),
    )
    c = TestClient(TestServer(app))
    await c.start_server()
    resp = await c.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    c.csrf = (await resp.json())["csrf_token"]
    c.echo_port = echo
    try:
        yield c
    finally:
        await c.close()


async def open_forward(client, host: str, port: int):
    return await client.post(
        "/api/forwards", json={"host": host, "port": port},
        headers={CSRF_HEADER: client.csrf},
    )


async def roundtrip(port: int, payload: bytes = b"hello") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        return await asyncio.wait_for(reader.read(1024), 5)
    finally:
        writer.close()


# ── 동작 ────────────────────────────────────────────────────────────


async def test_traffic_reaches_the_target_through_the_relay(client):
    resp = await open_forward(client, "testpc", client.echo_port)
    assert resp.status == 201
    body = await resp.json()

    assert body["host"] == "testpc"
    assert body["address"] == "127.0.0.1"
    assert body["listen_port"] != client.echo_port, "중계는 별도 포트를 연다"
    assert await roundtrip(body["listen_port"]) == b"echo:hello"


async def test_reopening_the_same_pair_reuses_the_listener(client):
    first = await (await open_forward(client, "testpc", client.echo_port)).json()
    second = await (await open_forward(client, "testpc", client.echo_port)).json()
    # 버튼을 두 번 눌러도 포트가 늘어나면 안 된다.
    assert first["listen_port"] == second["listen_port"]
    assert len((await (await client.get("/api/forwards")).json())["forwards"]) == 1


async def test_closing_stops_the_listener(client):
    body = await (await open_forward(client, "testpc", client.echo_port)).json()
    port = body["listen_port"]
    assert await roundtrip(port) == b"echo:hello"

    resp = await client.delete(
        f"/api/forwards/{body['key']}", headers={CSRF_HEADER: client.csrf}
    )
    assert resp.status == 200
    with pytest.raises((ConnectionRefusedError, OSError)):
        await roundtrip(port)


async def test_closing_something_that_is_not_open_is_404(client):
    resp = await client.delete(
        "/api/forwards/testpc:8000", headers={CSRF_HEADER: client.csrf}
    )
    assert resp.status == 404


async def test_listing_reports_open_forwards(client):
    await open_forward(client, "testpc", client.echo_port)
    body = await (await client.get("/api/forwards")).json()
    assert body["forwards"][0]["host"] == "testpc"


# ── onprem / hybrid 별 포트 계획 ────────────────────────────────────


async def entries(client, host: str, env: str | None = None) -> dict[int, dict]:
    q = f"/api/forwards?host={host}" + (f"&env={env}" if env else "")
    body = await (await client.get(q)).json()
    return {e["port"]: e for e in body["entries"]}


async def test_onprem_relays_every_port(client):
    plan = await entries(client, "testpc", "dev")
    for port in (8000, 8001, 8002, 8003):
        assert plan[port]["mode"] == "relay", port
        assert plan[port]["url"] is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("dev", {
            8000: "https://dev-gateway.connecteve.com",
            8001: "https://dev-temporal-web.connecteve.com",
            8003: "https://dev-grafana.connecteve.com",
        }),
        ("stage", {
            8000: "https://stage-gateway.connecteve.com",
            8001: "https://stage-temporal-web.connecteve.com",
            8003: "https://stage-grafana.connecteve.com",
        }),
        ("prod", {
            8000: "https://hub.connecteve.com",
            8001: "https://temporal-web.connecteve.com",
            8003: "https://grafana.connecteve.com",
        }),
    ],
)
async def test_hybrid_sends_central_ports_to_the_cloud(client, env, expected):
    plan = await entries(client, "hyb", env)
    for port, url in expected.items():
        assert plan[port]["mode"] == "cloud", port
        assert plan[port]["url"] == url


async def test_hybrid_still_relays_webpacs(client):
    """영상은 병원 안에 남는다 — 8002 는 프로파일과 무관하게 사이트다."""
    for env in ("dev", "stage", "prod"):
        plan = await entries(client, "hyb", env)
        assert plan[8002]["mode"] == "relay"
        assert plan[8002]["url"] is None


async def test_hybrid_without_an_env_cannot_choose_an_address(client):
    """verify/clean 처럼 `-e ENV` 가 없는 작업. 중계를 내주면 404 만 보게 된다."""
    plan = await entries(client, "hyb", None)
    assert plan[8000]["mode"] == "unknown"
    assert plan[8000]["url"] is None
    assert plan[8002]["mode"] == "relay", "8002 는 환경과 무관하다"


async def test_plan_needs_a_known_host(client):
    assert (await client.get("/api/forwards?host=nope")).status == 404


# ── 화면만 숨기는 것으로는 부족하다 ────────────────────────────────


async def test_opening_a_cloud_port_on_hybrid_is_refused(client):
    resp = await client.post(
        "/api/forwards", json={"host": "hyb", "port": 8000, "env": "dev"},
        headers={CSRF_HEADER: client.csrf},
    )
    assert resp.status == 400
    assert "dev-gateway.connecteve.com" in (await resp.json())["error"]


async def test_opening_a_cloud_port_without_an_env_is_refused(client):
    resp = await client.post(
        "/api/forwards", json={"host": "hyb", "port": 8003},
        headers={CSRF_HEADER: client.csrf},
    )
    assert resp.status == 400
    assert "환경" in (await resp.json())["error"]


async def test_onprem_cloud_ports_still_open(client, monkeypatch):
    """규칙은 hybrid 에만 걸린다. onprem 은 8000 도 중계 대상이다."""
    monkeypatch.setitem(forwards.ALLOWED_PORTS, 8000, "프론트")
    resp = await client.post(
        "/api/forwards", json={"host": "testpc", "port": 8000, "env": "dev"},
        headers={CSRF_HEADER: client.csrf},
    )
    assert resp.status == 201


# ── 터미널 (22) ─────────────────────────────────────────────────────


async def test_the_terminal_is_relayed_on_every_profile(client):
    """터미널은 프로파일과 무관하게 사이트다 — 중앙에는 그 PC 가 없다."""
    for host, env in [("testpc", "dev"), ("hyb", "dev"), ("hyb", "prod"), ("hyb", None)]:
        plan = await entries(client, host, env)
        assert plan[22]["mode"] == "relay", (host, env)
        assert plan[22]["url"] is None


async def test_the_terminal_is_marked_for_ssh_not_a_browser_tab(client):
    """화면이 `ssh://` 로 넘길지 새 탭으로 열지 구분하는 표시."""
    plan = await entries(client, "testpc", "dev")
    assert plan[22]["via"] == "ssh"
    for port in (8000, 8001, 8002, 8003):
        assert plan[port]["via"] == "http", port


async def test_the_terminal_comes_last(client):
    """설치 끝나고 웹을 먼저 보고, 필요할 때 터미널로 내려간다."""
    body = await (await client.get("/api/forwards?host=testpc&env=dev")).json()
    assert [e["port"] for e in body["entries"]] == [8000, 8001, 8002, 8003, 22]


async def test_the_plan_carries_the_login_name(client):
    """`ssh://<사용자>@...` 를 만들려면 인벤토리의 ansible_user 가 필요하다."""
    body = await (await client.get("/api/forwards?host=testpc")).json()
    assert body["user"] == "connecteve"


async def test_opening_the_terminal_relays_to_sshd(client):
    """22 도 다른 포트와 같은 중계다 — 특별한 경로가 아니다."""
    resp = await open_forward(client, "testpc", 22)
    assert resp.status == 201
    body = await resp.json()
    assert body["label"] == "터미널"
    assert body["port"] == 22


# ── tailnet 너머의 타겟 ─────────────────────────────────────────────


async def test_a_tailnet_target_is_relayed_like_any_other(client):
    """타겟이 사내 LAN 이 아니라 tailnet 에 있어도 중계는 똑같다.

    중계는 맥미니에서 타겟으로 TCP 를 잇는 것뿐이라, 그 길이 LAN 이든
    Tailscale 이든 상관하지 않는다. 인벤토리에 등록된 주소로 연결할 뿐이다.
    """
    plan = await entries(client, "tailnet", "dev")
    for port in (8000, 8001, 8002, 8003, 22):
        assert plan[port]["mode"] == "relay", port

    body = await (await open_forward(client, "tailnet", 8000)).json()
    assert body["address"] == "100.101.102.103", "인벤토리 주소로 이어야 한다"
    assert body["listen_port"] != 8000


# ── 열어주면 안 되는 것 ─────────────────────────────────────────────


async def test_an_arbitrary_port_is_refused(client):
    """허용 목록 밖이면 거절. 아니면 콘솔이 사내망 포트 스캐너가 된다.

    예시로 k0s API 서버(6443)를 쓴다 — 뚫리면 클러스터가 통째로 넘어가는 포트다.
    (전에는 22 를 예시로 썼는데 터미널 때문에 허용 목록에 들어갔다. 22 가 열린
    것과 임의 포트가 열리는 것은 다르다 — 아래 터미널 절 참고.)
    """
    resp = await open_forward(client, "testpc", 6443)
    assert resp.status == 400
    assert "열 수 없는 포트" in (await resp.json())["error"]


async def test_a_host_outside_the_inventory_is_refused(client):
    """등록되지 않은 주소로는 못 연다 — 임의 host 를 받으면 프록시가 된다."""
    resp = await open_forward(client, "192.168.100.209", client.echo_port)
    assert resp.status == 404
    resp = await open_forward(client, "", client.echo_port)
    assert resp.status == 404


async def test_opening_requires_a_session(temp_db, tmp_path, echo):
    inventory = tmp_path / "sites.yml"
    inventory.write_text(SITES_YML, encoding="utf-8")
    repo = tmp_path / "hub-provisioning"
    repo.mkdir()
    app = create_app(
        db_path=temp_db, hubctl_repo=repo, inventory_path=inventory,
        static_dir=tmp_path / "static",
    )
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        assert (await c.post("/api/forwards", json={"host": "testpc", "port": echo})).status == 401
        assert (await c.get("/api/forwards")).status == 401
    finally:
        await c.close()


# ── 정리 ────────────────────────────────────────────────────────────


async def test_shutting_the_app_down_closes_every_forward(client):
    body = await (await open_forward(client, "testpc", client.echo_port)).json()
    port = body["listen_port"]
    assert await roundtrip(port) == b"echo:hello"

    await client.close()   # 앱 cleanup
    with pytest.raises((ConnectionRefusedError, OSError)):
        await roundtrip(port)


async def test_an_unreachable_target_does_not_kill_the_relay(client):
    """타겟이 아직 안 떴어도 중계는 살아 있어야 한다 — 설치 직후 재시도할 수 있게."""
    import autodeploy.web.forwards as fw

    monkey = fw.ALLOWED_PORTS
    monkey[9] = "테스트-불통"
    try:
        body = await (await open_forward(client, "faraway", 9)).json()
        port = body["listen_port"]
        # 접속은 되지만 곧 끊긴다 (타겟 도달 실패)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            assert await asyncio.wait_for(reader.read(16), 10) == b""
        finally:
            writer.close()
        # 중계 자체는 목록에 남아 있다
        listed = (await (await client.get("/api/forwards")).json())["forwards"]
        assert any(f["key"] == body["key"] for f in listed)
    finally:
        monkey.pop(9, None)


# ── 유휴 정리 ───────────────────────────────────────────────────────


async def test_idle_forwards_are_reaped(client, monkeypatch):
    """열어둔 것을 잊어도 영원히 남지 않는다."""
    monkeypatch.setattr(forwards, "IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(forwards, "REAP_INTERVAL", 0.02)
    manager = forwards.ForwardManager(bind_host="127.0.0.1")
    manager.start()
    try:
        f = await manager.open(host="testpc", address="127.0.0.1", port=client.echo_port)
        assert manager.get(f.key) is not None
        await asyncio.sleep(0.3)
        assert manager.get(f.key) is None, "무사용 중계가 정리되지 않았다"
    finally:
        await manager.stop()
