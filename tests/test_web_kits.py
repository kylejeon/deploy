"""설치 킷 작업 — API·작업 서비스 (러너는 가짜, test_kit_runner 가 따로 본다)."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from autodeploy.accounts import create_user
from autodeploy.ansible_log import LineKind
from autodeploy.db import connect
from autodeploy.kit_runner import KitResult
from autodeploy.kits import Kit
from autodeploy.repository import get_server_meta, mark_key_installed
from autodeploy.settings import KitConfig
from autodeploy.web import create_app
from autodeploy.web.auth import CSRF_HEADER

from tests.test_web_jobs import PASSWORD, SITES_YML, USERNAME, wait_finished

KITS = [
    Kit("connevo-gateway-1.2.0.2.run", "1.2.0.2", "full", 16 << 30, 4000, "aa"),
    Kit("connevo-gateway-patch-1.2.0.2.run", "1.2.0.2", "patch", 800 << 20, 4000, "bb", ("1.2.0.1",)),
]


class FakeKitRunner:
    def __init__(self, target, req, emit, outcome):
        self.target, self.req, self.emit, self.outcome = target, req, emit, outcome
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        return True

    async def run(self):
        self.emit("kit_check", f"probe {self.target.address}", LineKind.OUT)
        self.emit("kit_3", "::step:: 3/8 이미지 적재", LineKind.TASK)
        await asyncio.sleep(0)
        if self.outcome == "fail":
            return KitResult(status="failed", exit_code=6, error="패치 기준이 다릅니다 kit-secret",
                             kit_name="connevo-gateway-patch-1.2.0.2.run", mode="patch")
        return KitResult(status="succeeded", exit_code=0, kit_name="connevo-gateway-1.2.0.2.run",
                         mode="install", installed_after="1.2.0.2", addon=self.req.addon)


async def make_kit_client(temp_db, tmp_path, *, outcome="ok", password="kit-secret"):
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
        for host in ("alpha", "beta"):
            await mark_key_installed(db, host)
    inventory = tmp_path / "sites.yml"
    inventory.write_text(SITES_YML, encoding="utf-8")
    (tmp_path / "repo").mkdir()
    runners: list[FakeKitRunner] = []

    def factory(target, req, emit):
        r = FakeKitRunner(target, req, emit, outcome)
        runners.append(r)
        return r

    async def lister():
        return KITS

    from autodeploy.masking import SecretMasker
    app = create_app(
        db_path=temp_db, hubctl_repo=tmp_path / "repo", inventory_path=inventory,
        static_dir=tmp_path / "static", log_dir=tmp_path / "joblogs",
        masker=SecretMasker([password]),
        kit=KitConfig(password=password, cache_dir=tmp_path / "kits"),
        kit_runner_factory=factory, kit_lister=lister,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    client.csrf = (await resp.json())["csrf_token"]
    client.runners = runners
    return client


@pytest.fixture
async def kclient(temp_db, tmp_path):
    c = await make_kit_client(temp_db, tmp_path)
    try:
        yield c
    finally:
        await c.close()


async def post(client, path, payload):
    return await client.post(path, json=payload, headers={CSRF_HEADER: client.csrf})


INSTALL = {"kind": "kit", "kit_action": "install", "kit_version": "1.2.0.2", "kit_addon": "orthanc"}


async def test_two_servers_make_two_jobs_that_finish(kclient, temp_db):
    resp = await post(kclient, "/api/jobs", {**INSTALL, "hosts": ["alpha", "beta"]})
    assert resp.status == 201
    body = await resp.json()
    assert len(body["ids"]) == 2 and body["id"] == body["ids"][0]

    for job_id, host in zip(body["ids"], ["alpha", "beta"]):
        job = await wait_finished(kclient, job_id)
        assert job["status"] == "succeeded"
        assert job["kind"] == "kit" and job["kit_action"] == "install"
        assert job["kit_version"] == "1.2.0.2" and job["kit_addon"] == "orthanc"
        assert job["kit_name"] == "connevo-gateway-1.2.0.2.run" and job["kit_mode"] == "install"
        assert job["sync_branch"] is None
        assert [h["host"] for h in job["hosts"]] == [host]
        assert job["hosts"][0]["status"] == "succeeded"

    assert [r.target.address for r in kclient.runners] == ["192.0.2.10", "192.0.2.11"]
    async with connect(temp_db) as db:
        meta = await get_server_meta(db)
    assert meta["alpha"]["kit_version"] == "1.2.0.2" and meta["alpha"]["kit_addon"] == "orthanc"
    servers = (await (await kclient.get("/api/servers")).json())["servers"]
    assert {s["host"]: s["kit_version"] for s in servers}["beta"] == "1.2.0.2"


async def test_job_log_carries_the_kit_steps(kclient):
    job_id = (await (await post(kclient, "/api/jobs", {**INSTALL, "hosts": ["alpha"]})).json())["id"]
    await wait_finished(kclient, job_id)
    log = await (await kclient.get(f"/api/jobs/{job_id}/log")).text()
    assert "probe 192.0.2.10" in log and "이미지 적재" in log


async def test_a_failed_kit_job_keeps_the_reason_masked(temp_db, tmp_path):
    client = await make_kit_client(temp_db, tmp_path, outcome="fail")
    try:
        job_id = (await (await post(client, "/api/jobs", {**INSTALL, "hosts": ["alpha"]})).json())["id"]
        job = await wait_finished(client, job_id)
        assert job["status"] == "failed" and job["exit_code"] == 6
        assert "패치 기준" in job["error_message"] and "kit-secret" not in job["error_message"]
        assert job["hosts"][0]["status"] == "failed"
    finally:
        await client.close()


@pytest.mark.parametrize("payload, message", [
    ({**INSTALL, "kit_version": "9.9.9"}, "9.9.9 킷이 없습니다"),
    ({**INSTALL, "kit_version": "main"}, "버전이 올바르지 않습니다"),
    ({**INSTALL, "kit_addon": "pacs"}, "패키지는"),
    ({"kind": "kit", "kit_action": "format"}, "알 수 없는 킷 작업"),
    ({"kind": "kit", "kit_action": "uninstall-all"}, "'삭제'"),
])
async def test_bad_requests_make_no_job(kclient, payload, message):
    resp = await post(kclient, "/api/jobs", {**payload, "hosts": ["alpha"]})
    assert resp.status == 400
    assert message in (await resp.json())["error"]
    jobs = (await (await kclient.get("/api/jobs")).json())["jobs"]
    assert jobs == []


async def test_one_bad_server_stops_all_before_any_job(kclient):
    resp = await post(kclient, "/api/jobs", {**INSTALL, "hosts": ["alpha", "nokey"]})
    assert resp.status == 400
    assert "SSH 키" in (await resp.json())["error"]
    assert (await (await kclient.get("/api/jobs")).json())["jobs"] == []


async def test_uninstall_all_with_confirmation(kclient):
    resp = await post(kclient, "/api/jobs", {"kind": "kit", "kit_action": "uninstall-all",
                                            "confirm": "삭제", "hosts": ["alpha"]})
    assert resp.status == 201
    job = await wait_finished(kclient, (await resp.json())["id"])
    assert job["kit_action"] == "uninstall-all"


async def test_install_without_a_kit_password_is_refused(temp_db, tmp_path):
    client = await make_kit_client(temp_db, tmp_path, password="")
    try:
        resp = await post(client, "/api/jobs", {**INSTALL, "hosts": ["alpha"]})
        assert resp.status == 400 and "KIT_PASSWORD" in (await resp.json())["error"]
    finally:
        await client.close()


async def test_kit_list(kclient, tmp_path):
    (tmp_path / "kits").mkdir()
    (tmp_path / "kits" / "connevo-gateway-1.2.0.2.run.ok").write_text("x")
    body = await (await kclient.get("/api/kits")).json()
    assert body["password_set"] is True
    assert [(k["name"], k["cached"]) for k in body["kits"]] == [
        ("connevo-gateway-1.2.0.2.run", True), ("connevo-gateway-patch-1.2.0.2.run", False)]
    assert body["kits"][1]["bases"] == ["1.2.0.1"]
    assert "password" not in str(body).replace("password_set", "")


def test_every_kit_step_has_a_name_on_the_screen():
    """키는 서버(kits.KIT_STEPS)가 정하고 이름은 화면(PHASES.kit_*)이 쥔다.

    화면에 없는 단계 키가 current_step 에 오면 진행 표시가 그 자리에서 멈춘다.
    """
    import re
    from pathlib import Path

    from autodeploy.kits import KIT_STEPS

    js = (Path(__file__).resolve().parents[1] / "src" / "autodeploy" / "web" / "static"
          / "console.js").read_text(encoding="utf-8")
    keys: set[str] = set()
    for name in ("kit_install", "kit_uninstall"):
        block = re.search(name + r": \[(.*?)\n  \],", js, re.S)
        assert block, f"console.js 에서 PHASES.{name} 을 못 찾았다"
        keys |= set(re.findall(r'\["(kit_\w+)"', block.group(1)))
    assert keys == set(KIT_STEPS)
