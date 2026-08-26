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


async def test_listing_reports_open_forwards_and_allowed_ports(client):
    await open_forward(client, "testpc", client.echo_port)
    body = await (await client.get("/api/forwards")).json()
    assert body["forwards"][0]["host"] == "testpc"
    # 화면이 포트 목록을 하드코딩하지 않도록 서버가 알려준다.
    assert "8000" in body["ports"] or 8000 in body["ports"]


# ── 열어주면 안 되는 것 ─────────────────────────────────────────────


async def test_an_arbitrary_port_is_refused(client):
    """허용 목록 밖이면 거절. 아니면 콘솔이 사내망 포트 스캐너가 된다."""
    resp = await open_forward(client, "testpc", 22)
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
