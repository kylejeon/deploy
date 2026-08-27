"""서버 인벤토리 변경 + SSH 키 등록 API (§F2 / §F9)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from autodeploy.accounts import create_user
from autodeploy.db import connect
from autodeploy.inventory import load_inventory
from autodeploy.ssh_keys import KeyRegistration, SSHKeyError
from autodeploy.web import api, create_app
from autodeploy.web.auth import CSRF_HEADER

USERNAME = "yonghyuk"
PASSWORD = "correct-horse-battery"

SITES_YML = """\
sites:
  hosts:
    alpha:
      ansible_host: 192.0.2.10
      ansible_user: connecteve
      site_name: alpha
      profile: onprem
"""

NEW_SERVER = {
    "host": "qa-209",
    "ansible_host": "192.168.100.209",
    "ansible_user": "connecteve",
    "site_name": "qa209",
    "profile": "onprem",
}


@pytest.fixture
def inventory_path(tmp_path: Path) -> Path:
    path = tmp_path / "sites.yml"
    path.write_text(SITES_YML, encoding="utf-8")
    return path


@pytest.fixture
async def client(temp_db, tmp_path, inventory_path):
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)

    repo = tmp_path / "hub-provisioning"
    repo.mkdir()
    app = create_app(
        db_path=temp_db,
        hubctl_repo=repo,
        inventory_path=inventory_path,
        static_dir=tmp_path / "static",
        hubctl_shell=("bash", "-c"),
    )
    c = TestClient(TestServer(app))
    await c.start_server()
    resp = await c.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    c.csrf = (await resp.json())["csrf_token"]
    try:
        yield c
    finally:
        await c.close()


async def send(client, method, path, payload=None):
    return await client.request(
        method, path, json=payload or {}, headers={CSRF_HEADER: client.csrf}
    )


async def current_mtime(client):
    return (await (await client.get("/api/servers")).json())["mtime_ns"]


def like_a_browser(payload: str) -> dict:
    """브라우저의 `JSON.parse` 를 흉내낸다 — 숫자 리터럴은 double 로 읽힌다.

    JS 의 Number 는 배정밀도 실수라 정수를 2^53 까지만 정확히 담는다.
    `parse_int` 를 float 경유로 두면 파이썬에서도 같은 손실이 재현된다.
    """
    return json.loads(payload, parse_int=lambda raw: int(float(raw)))


# ── 추가 ────────────────────────────────────────────────────────────


async def test_add_server_writes_sites_yml(client, inventory_path):
    """AC-3: 추가하면 sites.yml 이 갱신되고 백업이 남는다."""
    resp = await send(client, "POST", "/api/servers",
                      {**NEW_SERVER, "mtime_ns": await current_mtime(client)})
    assert resp.status == 201

    hosts = {s.host: s for s in load_inventory(inventory_path).servers}
    assert set(hosts) == {"alpha", "qa-209"}
    assert hosts["qa-209"].ansible_host == "192.168.100.209"
    assert hosts["qa-209"].profile == "onprem"

    backups = list(inventory_path.parent.glob("sites.yml.bak-*"))
    assert len(backups) == 1


async def test_add_returns_the_new_mtime_for_the_next_edit(client):
    before = await current_mtime(client)
    body = await (await send(client, "POST", "/api/servers",
                             {**NEW_SERVER, "mtime_ns": before})).json()
    assert body["mtime_ns"] != before
    assert body["mtime_ns"] == await current_mtime(client)


async def test_duplicate_host_rejected(client):
    payload = {**NEW_SERVER, "host": "alpha", "mtime_ns": await current_mtime(client)}
    resp = await send(client, "POST", "/api/servers", payload)
    assert resp.status == 409
    assert "이미 등록된" in (await resp.json())["error"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "bad host"),
        ("host", ""),
        ("ansible_host", ""),
        ("ansible_user", ""),
        ("profile", "k8s"),
        ("profile", ""),
    ],
)
async def test_invalid_server_rejected(client, field, value):
    payload = {**NEW_SERVER, field: value, "mtime_ns": await current_mtime(client)}
    resp = await send(client, "POST", "/api/servers", payload)
    assert resp.status == 400


async def test_stale_mtime_is_a_conflict(client, inventory_path):
    """§F2: 다른 곳에서 고친 걸 조용히 덮어쓰면 안 된다."""
    stale = await current_mtime(client)
    inventory_path.write_text(SITES_YML + "      # 다른 사람이 손댐\n", encoding="utf-8")

    resp = await send(client, "POST", "/api/servers", {**NEW_SERVER, "mtime_ns": stale})
    assert resp.status == 409
    assert "다른 곳에서" in (await resp.json())["error"]


async def test_editing_survives_the_browsers_json_parse(client, inventory_path):
    """§F2 의 잠금 키가 브라우저를 왕복해도 그대로여야 한다.

    실제로 한 번 났다. `st_mtime_ns` 는 나노초라 61비트인데 JSON 숫자로 내보내면
    브라우저의 JSON.parse 가 double 로 읽어 반올림한다(이 크기에서 간격 256ns).
    화면이 돌려준 값이 파일 시각과 달라지므로 서버는 "다른 곳에서 수정됐다" 고
    판단했고, **서버 편집이 100% 409 로 실패했다.** 실제 mtime 20개를 재봤더니
    20개 전부 바뀌었다. 파이썬 테스트만으로는 절대 안 잡힌다 — 파이썬 int 는
    임의 정밀도라 서버끼리는 아무 문제가 없었다.
    """
    raw = await (await client.get("/api/servers")).text()
    seen = like_a_browser(raw)

    resp = await send(client, "PUT", "/api/servers/alpha", {
        "ansible_host": "192.0.2.99",
        "ansible_user": "connecteve",
        "site_name": "alpha",
        "profile": "onprem",
        "mtime_ns": seen["mtime_ns"],
    })
    assert resp.status == 200, await resp.text()
    assert load_inventory(inventory_path).get("alpha").ansible_host == "192.0.2.99"


async def test_the_lock_key_is_not_a_json_number(client, inventory_path):
    """위 사고의 원인을 직접 못박는다 — 값이 아니라 **타입**이 문제였다."""
    body = await (await client.get("/api/servers")).json()
    assert isinstance(body["mtime_ns"], str), "숫자로 내보내면 브라우저에서 깎인다"
    assert int(body["mtime_ns"]) == inventory_path.stat().st_mtime_ns


async def test_every_write_returns_a_usable_lock_key(client):
    """추가·수정·삭제 응답의 키로 곧바로 다음 편집을 할 수 있어야 한다.

    새로고침 없이 연속으로 고칠 때 쓰는 값이라, 한 곳만 숫자로 남아도 그 다음
    저장이 409 로 막힌다.
    """
    added = like_a_browser(await (await send(
        client, "POST", "/api/servers", {**NEW_SERVER, "mtime_ns": await current_mtime(client)}
    )).text())
    assert isinstance(added["mtime_ns"], str)

    edited = like_a_browser(await (await send(client, "PUT", "/api/servers/qa-209", {
        **NEW_SERVER, "site_name": "qa209b", "mtime_ns": added["mtime_ns"],
    })).text())
    assert isinstance(edited["mtime_ns"], str)

    resp = await send(client, "DELETE", "/api/servers/qa-209",
                      {"mtime_ns": edited["mtime_ns"]})
    assert resp.status == 200, await resp.text()


async def test_add_requires_csrf(client):
    resp = await client.post("/api/servers", json=NEW_SERVER)
    assert resp.status == 403


# ── 수정 ────────────────────────────────────────────────────────────


async def test_edit_server(client, inventory_path):
    resp = await send(client, "PUT", "/api/servers/alpha", {
        "ansible_host": "192.0.2.99",
        "ansible_user": "connecteve",
        "site_name": "alpha",
        "profile": "hybrid-with-ai",
        "mtime_ns": await current_mtime(client),
    })
    assert resp.status == 200
    server = load_inventory(inventory_path).get("alpha")
    assert server.ansible_host == "192.0.2.99"
    assert server.profile == "hybrid-with-ai"


async def test_edit_saves_memo_to_the_database(client, temp_db):
    """메모는 sites.yml 스키마에 없는 값이라 DB 에 둔다 (§F2)."""
    await send(client, "PUT", "/api/servers/alpha", {
        "ansible_host": "192.0.2.10", "ansible_user": "connecteve",
        "site_name": "alpha", "profile": "onprem", "memo": "연세와병원",
        "mtime_ns": await current_mtime(client),
    })
    body = await (await client.get("/api/servers")).json()
    assert body["servers"][0]["memo"] == "연세와병원"


async def test_renaming_a_host_is_refused(client):
    """이름이 바뀌면 job_hosts·server_meta 참조가 끊긴다."""
    resp = await send(client, "PUT", "/api/servers/alpha", {
        **NEW_SERVER, "mtime_ns": await current_mtime(client),
    })
    assert resp.status == 400
    assert "호스트명은 바꿀 수 없습니다" in (await resp.json())["error"]


async def test_edit_unknown_host_is_404(client):
    resp = await send(client, "PUT", "/api/servers/ghost", NEW_SERVER)
    assert resp.status == 404


# ── 삭제 ────────────────────────────────────────────────────────────


async def test_delete_server(client, inventory_path, temp_db):
    async with connect(temp_db) as db:
        from autodeploy.repository import mark_key_installed

        await mark_key_installed(db, "alpha")

    resp = await send(client, "DELETE", "/api/servers/alpha",
                      {"mtime_ns": await current_mtime(client)})
    assert resp.status == 200
    assert load_inventory(inventory_path).servers == ()

    # 부가정보도 같이 지운다 — 같은 이름으로 다시 등록했을 때 남의 키 상태를
    # 물려받으면 AC-16 게이트가 잘못 열린다.
    async with connect(temp_db) as db:
        from autodeploy.repository import get_server_meta

        assert await get_server_meta(db) == {}


async def test_delete_unknown_host_is_404(client):
    resp = await send(client, "DELETE", "/api/servers/ghost")
    assert resp.status == 404


# ── 실행 중 편집 금지 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/api/servers", NEW_SERVER),
        ("PUT", "/api/servers/alpha", NEW_SERVER),
        ("DELETE", "/api/servers/alpha", {}),
    ],
)
async def test_inventory_is_frozen_while_a_job_is_active(client, temp_db, method, path, payload):
    """§F2: 인벤토리가 흔들리면 화면에서 본 목록과 실제 대상이 어긋난다."""
    async with connect(temp_db) as db:
        await db.execute(
            "INSERT INTO jobs (kind, status, started_by) VALUES ('install','running','web:x')"
        )
        await db.commit()

    resp = await send(client, method, path, payload)
    assert resp.status == 409
    error = (await resp.json())["error"]
    assert "진행 중인 작업" in error
    # 막고 있는 작업을 번호로 짚어줘야 손을 쓸 수 있다. "1건" 만으로는
    # 어디를 봐야 할지 알 수 없다 — 재시작을 넘긴 좀비 작업일 때 특히.
    assert "#1" in error, error


async def test_finished_jobs_do_not_block_edits(client, temp_db):
    async with connect(temp_db) as db:
        await db.execute(
            "INSERT INTO jobs (kind, status, started_by) VALUES ('install','succeeded','web:x')"
        )
        await db.commit()
    resp = await send(client, "POST", "/api/servers",
                      {**NEW_SERVER, "mtime_ns": await current_mtime(client)})
    assert resp.status == 201


# ── SSH 키 등록 (F9) ────────────────────────────────────────────────


async def test_ssh_key_registration_marks_the_server(client, monkeypatch, temp_db):
    calls = []

    async def fake_register(*, host, username, password, **kw):
        calls.append((host, username, password))
        return KeyRegistration(pubkey="ssh-ed25519 AAAA... autodeploy@macmini",
                               sleep_masked=True)

    monkeypatch.setattr(api, "register_key", fake_register)

    resp = await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": "target-pw"})
    assert resp.status == 200
    assert (await resp.json())["key_installed_at"] is not None

    # 인벤토리의 ansible_host/ansible_user 로 붙어야 한다 (호스트 키 이름이 아니라).
    assert calls == [("192.0.2.10", "connecteve", "target-pw")]


async def test_the_response_says_whether_sleep_was_masked(client, monkeypatch):
    """키는 됐는데 절전만 실패한 경우를 화면이 구분해 말할 수 있어야 한다.

    뭉뚱그리면 긴 설치가 한밤중에 SSH 끊김으로 죽고, 원인을 로그에서 찾을 수 없다.
    """
    async def fake_register(**kw):
        return KeyRegistration(pubkey="ssh-ed25519 AAAA...", sleep_masked=False,
                               sleep_error="systemctl mask 가 exit 1 로 끝났습니다")

    monkeypatch.setattr(api, "register_key", fake_register)
    resp = await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": "pw"})

    assert resp.status == 200, "절전 실패로 키 등록까지 막으면 설치를 못 한다"
    body = await resp.json()
    assert body["key_installed_at"] is not None
    assert body["sleep_masked"] is False
    assert "exit 1" in body["sleep_error"]


async def test_only_hybrid_servers_get_the_desktop_prep(client, monkeypatch, inventory_path):
    """onprem 은 폐쇄망 서버라 준비 스크립트를 돌리지 않는다."""
    seen = {}

    async def fake_register(**kw):
        seen[kw["host"]] = kw["prepare"]
        return KeyRegistration(pubkey="k", sleep_masked=True, prep_ran=kw["prepare"])

    monkeypatch.setattr(api, "register_key", fake_register)
    await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": "pw"})
    assert seen == {"192.0.2.10": False}, "onprem 인데 준비를 돌렸다"

    # 같은 서버를 hybrid 로 바꾸면 돈다
    inventory_path.write_text(
        SITES_YML.replace("profile: onprem", "profile: hybrid-with-ai"), encoding="utf-8"
    )
    seen.clear()
    await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": "pw"})
    assert seen == {"192.0.2.10": True}


async def test_the_anydesk_id_lands_in_the_server_list(client, monkeypatch, inventory_path):
    """접속 ID 는 사람이 읽어야 하는 값이라 목록에 남는다 — 메모는 건드리지 않는다."""
    inventory_path.write_text(
        SITES_YML.replace("profile: onprem", "profile: hybrid-with-ai"), encoding="utf-8"
    )
    await send(client, "PUT", "/api/servers/alpha", {
        "ansible_host": "192.0.2.10", "ansible_user": "connecteve", "site_name": "alpha",
        "profile": "hybrid-with-ai", "memo": "연세와병원",
        "mtime_ns": await current_mtime(client),
    })

    async def fake_register(**kw):
        return KeyRegistration(pubkey="k", sleep_masked=True, prep_ran=True,
                               anydesk_id="123456789")

    monkeypatch.setattr(api, "register_key", fake_register)
    body = await (await send(client, "POST", "/api/servers/alpha/ssh-key",
                             {"password": "pw"})).json()
    assert body["anydesk_id"] == "123456789"

    servers = (await (await client.get("/api/servers")).json())["servers"]
    row = next(s for s in servers if s["host"] == "alpha")
    assert row["anydesk_id"] == "123456789"
    assert row["memo"] == "연세와병원", "사람이 쓴 메모를 덮어썼다"


async def test_the_prep_log_is_masked_before_it_leaves_the_server(temp_db, tmp_path, inventory_path, monkeypatch):
    """스크립트 출력에 AnyDesk 비밀번호가 섞여도 화면으로 나가면 안 된다."""
    from autodeploy.masking import SecretMasker

    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
    repo = tmp_path / "hub-provisioning"
    repo.mkdir()
    app = create_app(
        db_path=temp_db, hubctl_repo=repo, inventory_path=inventory_path,
        static_dir=tmp_path / "static", masker=SecretMasker(["ad-secret"]),
    )
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        resp = await c.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        c.csrf = (await resp.json())["csrf_token"]

        async def fake_register(**kw):
            return KeyRegistration(pubkey="k", sleep_masked=True, prep_ran=True,
                                   prep_log=("설정값: ad-secret",))

        monkeypatch.setattr(api, "register_key", fake_register)
        body = await (await send(c, "POST", "/api/servers/alpha/ssh-key",
                                 {"password": "pw"})).json()
        assert "ad-secret" not in str(body), body
    finally:
        await c.close()


async def test_ssh_key_registration_opens_the_install_gate(client, monkeypatch):
    async def fake_register(**kw):
        return KeyRegistration(pubkey="ssh-ed25519 AAAA...", sleep_masked=True)

    monkeypatch.setattr(api, "register_key", fake_register)
    before = await (await client.get("/api/servers")).json()
    assert before["servers"][0]["key_installed_at"] is None

    await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": "pw"})
    after = await (await client.get("/api/servers")).json()
    assert after["servers"][0]["key_installed_at"] is not None


async def test_ssh_key_failure_does_not_mark_the_server(client, monkeypatch):
    async def boom(**kw):
        raise SSHKeyError("비밀번호가 올바르지 않습니다")

    monkeypatch.setattr(api, "register_key", boom)
    resp = await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": "wrong"})
    assert resp.status == 400
    assert "비밀번호" in (await resp.json())["error"]

    body = await (await client.get("/api/servers")).json()
    assert body["servers"][0]["key_installed_at"] is None


async def test_ssh_key_repeated_failures_are_throttled(client, monkeypatch):
    """§9: 비밀번호를 계속 넣어보는 것도 무차별 대입이다 (3회/60초)."""
    async def boom(**kw):
        raise SSHKeyError("접속 실패")

    monkeypatch.setattr(api, "register_key", boom)
    for _ in range(3):
        assert (await send(client, "POST", "/api/servers/alpha/ssh-key",
                           {"password": "x"})).status == 400

    resp = await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": "x"})
    assert resp.status == 429


async def test_ssh_key_requires_a_password(client):
    resp = await send(client, "POST", "/api/servers/alpha/ssh-key", {})
    assert resp.status == 400


async def test_ssh_key_unknown_host_is_404(client):
    resp = await send(client, "POST", "/api/servers/ghost/ssh-key", {"password": "x"})
    assert resp.status == 404


async def test_target_password_is_never_echoed_back(client, monkeypatch):
    """비밀번호는 DB·로그·응답 어디에도 남기지 않는다 (§F9)."""
    secret = "s3cret-target-password"

    async def boom(**kw):
        raise SSHKeyError(f"접속 실패 ({kw['host']})")

    monkeypatch.setattr(api, "register_key", boom)
    resp = await send(client, "POST", "/api/servers/alpha/ssh-key", {"password": secret})
    assert secret not in await resp.text()
