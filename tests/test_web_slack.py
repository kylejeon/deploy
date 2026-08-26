"""웹 작업의 Slack 게시 (§F7) + 정적 화면 서빙.

기존 SlackNotifier 는 건드리지 않는다 (D1) — 여기서 검증하는 것은 hubctl 작업 전용
게시자다.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from autodeploy.accounts import create_user
from autodeploy.db import connect
from autodeploy.repository import get_job_detail, mark_key_installed
from autodeploy.web import create_app
from autodeploy.web.auth import CSRF_HEADER
from autodeploy.web.slack import WebJobNotifier
from tests.test_web_jobs import FAKE_HUBCTL, SITES_YML, USERNAME, PASSWORD, wait_finished, write_repo


class FakeSlack:
    """chat_postMessage / chat_getPermalink 만 흉내낸다."""

    def __init__(self, *, fail: bool = False, permalink: str | None = "https://x.slack.com/archives/C1/p123") -> None:
        self.posts: list[dict] = []
        self.returned_ts: list[str] = []
        self._fail = fail
        self._permalink = permalink
        self._n = 0

    async def chat_postMessage(self, **kwargs):
        if self._fail:
            raise RuntimeError("slack down")
        self.posts.append(kwargs)
        self._n += 1
        ts = f"1700000000.{self._n:06d}"
        self.returned_ts.append(ts)
        return {"ts": ts}

    async def chat_getPermalink(self, **kwargs):
        if self._permalink is None:
            raise RuntimeError("no permalink")
        return {"permalink": self._permalink}


# ── 게시자 단위 ─────────────────────────────────────────────────────


async def test_started_posts_and_returns_thread_and_permalink():
    slack = FakeSlack()
    notifier = WebJobNotifier(slack, "C123")
    ts, permalink = await notifier.job_started(
        7, kind="install", hosts=("alpha", "beta"), env="stage", ref="v1.0.2",
        started_by="web:yonghyuk", command="./bin/hubctl install -e stage -l alpha,beta",
    )
    assert ts and permalink == "https://x.slack.com/archives/C1/p123"

    text = slack.posts[0]["text"]
    assert "작업 #7" in text
    assert "alpha, beta" in text
    assert "stage" in text and "v1.0.2" in text
    assert "web:yonghyuk" in text
    assert slack.posts[0]["channel"] == "C123"
    assert "thread_ts" not in slack.posts[0], "시작 메시지가 스레드 부모가 된다"


async def test_patch_create_shows_controller_as_target():
    slack = FakeSlack()
    await WebJobNotifier(slack, "C1").job_started(
        1, kind="patch", hosts=(), env=None, ref="v1", started_by="web:x", command="./bin/hubctl patch create",
    )
    assert "컨트롤러" in slack.posts[0]["text"]


async def test_finished_replies_in_the_thread():
    slack = FakeSlack()
    notifier = WebJobNotifier(slack, "C1")
    await notifier.job_finished(
        7, thread_ts="1700000000.000001", status="failed", exit_code=2,
        hosts=[
            {"host": "alpha", "status": "succeeded", "recap": {"ok": 5, "changed": 1, "failed": 0, "unreachable": 0}},
            {"host": "beta", "status": "failed", "recap": {"ok": 4, "changed": 1, "failed": 1, "unreachable": 0}},
        ],
        duration="22분 41초", console_url="http://mac:8080#job/7",
    )
    post = slack.posts[0]
    assert post["thread_ts"] == "1700000000.000001"
    assert "실패" in post["text"]
    assert "`alpha`" in post["text"] and "`beta`" in post["text"]
    assert "failed=1" in post["text"]
    assert "http://mac:8080#job/7" in post["text"]


async def test_finished_without_a_thread_posts_nothing():
    """시작 게시가 실패했으면 이어붙일 스레드가 없다."""
    slack = FakeSlack()
    await WebJobNotifier(slack, "C1").job_finished(
        1, thread_ts=None, status="succeeded", exit_code=0, hosts=[], duration="1초"
    )
    assert slack.posts == []


async def test_notifier_itself_does_not_filter_by_status():
    """게시자는 상태를 가리지 않는다. 승인 대기를 거르는 것은 JobService 쪽 책임이고,
    그 동작은 test_patch_posts_start_once_across_create_and_apply 가 검증한다."""
    slack = FakeSlack()
    await WebJobNotifier(slack, "C1").job_finished(
        1, thread_ts="t", status="awaiting", exit_code=0, hosts=[], duration="3초"
    )
    assert len(slack.posts) == 1


async def test_slack_failure_never_raises():
    """Slack 이 죽었다고 설치가 실패하면 안 된다."""
    slack = FakeSlack(fail=True)
    ts, permalink = await WebJobNotifier(slack, "C1").job_started(
        1, kind="verify", hosts=("a",), env=None, ref=None, started_by="web:x", command="x"
    )
    assert (ts, permalink) == (None, None)
    await WebJobNotifier(slack, "C1").job_finished(
        1, thread_ts="t", status="failed", exit_code=1, hosts=[], duration="1초"
    )


async def test_missing_permalink_still_creates_the_thread():
    slack = FakeSlack(permalink=None)
    ts, permalink = await WebJobNotifier(slack, "C1").job_started(
        1, kind="verify", hosts=("a",), env=None, ref=None, started_by="web:x", command="x"
    )
    assert ts and permalink is None


# ── JobService 통합 ─────────────────────────────────────────────────


@pytest.fixture
async def client(temp_db, tmp_path):
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
        await mark_key_installed(db, "alpha")
    inventory = tmp_path / "sites.yml"
    inventory.write_text(SITES_YML, encoding="utf-8")

    slack = FakeSlack()
    app = create_app(
        db_path=temp_db,
        hubctl_repo=write_repo(tmp_path, FAKE_HUBCTL),
        inventory_path=inventory,
        static_dir=tmp_path / "static",
        hubctl_shell=("bash", "-c"),
        log_dir=tmp_path / "joblogs",
        notifier=WebJobNotifier(slack, "C1"),
        console_url="http://mac:8080",
    )
    c = TestClient(TestServer(app))
    await c.start_server()
    resp = await c.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    c.csrf = (await resp.json())["csrf_token"]
    c.slack = slack
    try:
        yield c
    finally:
        await c.close()


async def create_job(client, payload):
    resp = await client.post("/api/jobs", json=payload, headers={CSRF_HEADER: client.csrf})
    return (await resp.json())["id"]


async def test_web_job_creates_a_thread_and_replies(client, temp_db):
    """AC-11: 웹에서 시작한 작업이 Slack 채널에도 스레드로 게시된다."""
    job_id = await create_job(client, {"kind": "verify", "hosts": ["alpha"]})
    await wait_finished(client, job_id)

    assert len(client.slack.posts) == 2
    start, finish = client.slack.posts
    assert "thread_ts" not in start, "시작 메시지가 스레드 부모다"
    # 종료 메시지는 시작 메시지가 돌려준 ts 아래에 붙어야 한다.
    assert finish["thread_ts"] == client.slack.returned_ts[0]
    assert "작업 #%d" % job_id in start["text"]
    assert "http://mac:8080#job/%d" % job_id in finish["text"]


async def test_thread_ts_and_permalink_are_stored(client, temp_db):
    job_id = await create_job(client, {"kind": "verify", "hosts": ["alpha"]})
    await wait_finished(client, job_id)

    async with connect(temp_db) as db:
        job = await get_job_detail(db, job_id)
    assert job["slack_thread_ts"]
    assert job["slack_permalink"] == "https://x.slack.com/archives/C1/p123"


async def test_permalink_is_exposed_to_the_console(client):
    job_id = await create_job(client, {"kind": "verify", "hosts": ["alpha"]})
    await wait_finished(client, job_id)
    body = await (await client.get(f"/api/jobs/{job_id}")).json()
    assert body["slack_permalink"].startswith("https://")


async def test_patch_posts_start_once_across_create_and_apply(client, temp_db):
    """create → 승인 → apply 는 한 작업이다. 스레드를 두 번 만들면 안 된다."""
    job_id = await create_job(client, {"kind": "patch", "ref": "v1.0.2"})
    job = await wait_finished(client, job_id)
    assert job["status"] == "awaiting"
    assert len(client.slack.posts) == 1, "승인 대기는 종료가 아니라 종료 메시지가 없어야 한다"

    async with connect(temp_db) as db:
        await db.execute(
            "INSERT INTO job_hosts (job_id, host, status) VALUES (?, 'alpha', 'queued')", (job_id,)
        )
        await db.commit()
    await client.post(f"/api/jobs/{job_id}/approve", headers={CSRF_HEADER: client.csrf})
    await wait_finished(client, job_id)

    starts = [p for p in client.slack.posts if "thread_ts" not in p]
    assert len(starts) == 1, "스레드 부모는 하나여야 한다"
    assert len(client.slack.posts) == 2


async def test_a_job_rejected_before_start_posts_nothing(client):
    """AC-16 에 걸려 만들어지지도 않은 작업은 Slack 에 안 뜬다."""
    resp = await client.post(
        "/api/jobs", json={"kind": "verify", "hosts": ["nokey"]}, headers={CSRF_HEADER: client.csrf}
    )
    assert resp.status == 400
    assert client.slack.posts == []


# ── 정적 화면 ───────────────────────────────────────────────────────


@pytest.fixture
async def ui_client(temp_db, tmp_path):
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
    repo = tmp_path / "hub-provisioning"
    repo.mkdir()
    inventory = tmp_path / "sites.yml"
    inventory.write_text(SITES_YML, encoding="utf-8")
    # 실제로 배포되는 static 디렉터리를 그대로 쓴다.
    static = Path(__file__).resolve().parents[1] / "src" / "autodeploy" / "web" / "static"
    app = create_app(
        db_path=temp_db, hubctl_repo=repo, inventory_path=inventory, static_dir=static
    )
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


async def test_login_page_is_served_without_a_session(ui_client):
    """AC-1: 미인증이면 로그인 화면이 실제로 떠야 한다."""
    resp = await ui_client.get("/login")
    assert resp.status == 200
    body = await resp.text()
    assert "AutoDeploy" in body
    assert "/static/console.css" in body


async def test_stylesheet_is_public(ui_client):
    """로그인 화면도 스타일시트를 받아야 한다."""
    resp = await ui_client.get("/static/console.css")
    assert resp.status == 200
    assert "--accent" in await resp.text()


async def test_console_requires_a_session(ui_client):
    resp = await ui_client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login"


async def test_console_is_served_after_login(ui_client):
    await ui_client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    resp = await ui_client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "/static/console.js" in body
    assert 'id="view-dash"' in body


async def test_console_script_is_served(ui_client):
    await ui_client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    resp = await ui_client.get("/static/console.js")
    assert resp.status == 200
    assert "/api/jobs" in await resp.text()


async def test_login_page_redirects_when_already_signed_in(ui_client):
    await ui_client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    resp = await ui_client.get("/login", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/"
