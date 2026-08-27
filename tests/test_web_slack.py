"""웹 작업의 Slack 게시 (§F7) + 정적 화면 서빙.

기존 SlackNotifier 는 건드리지 않는다 (D1) — 여기서 검증하는 것은 hubctl 작업 전용
게시자다.
"""
from __future__ import annotations

import asyncio
import re
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


async def wait_posts(client, count, *, timeout=5.0):
    """Slack 게시가 `count` 건에 이를 때까지.

    작업 종료만 기다리면 안 된다. `_finalize` 는 DB 를 먼저 확정하고
    (`jobs` UPDATE + commit) Slack 은 그 다음에 올린다 — 느린 Slack 호출이
    작업 종료를 붙잡으면 안 되기 때문이다. 그래서 `wait_finished` 가 돌아온
    시점에 종료 메시지는 아직 안 올라가 있을 수 있다.
    """
    async def poll():
        while len(client.slack.posts) < count:
            await asyncio.sleep(0.01)
        return client.slack.posts

    return await asyncio.wait_for(poll(), timeout)


async def test_web_job_creates_a_thread_and_replies(client, temp_db):
    """AC-11: 웹에서 시작한 작업이 Slack 채널에도 스레드로 게시된다."""
    job_id = await create_job(client, {"kind": "verify", "hosts": ["alpha"]})
    await wait_finished(client, job_id)

    start, finish = await wait_posts(client, 2)
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
    """승인 대기 → apply 는 한 작업이다. 스레드를 두 번 만들면 안 된다.

    콘솔의 patch 는 이제 원샷이라 이 상태로 가는 길이 화면에는 없다. 폐쇄망
    반입(create → 승인 → apply)에서는 여전히 한 작업이므로 여기서 검증한다.
    """
    async with connect(temp_db) as db:
        cur = await db.execute(
            "INSERT INTO jobs (kind, status, ref, started_by)"
            " VALUES ('patch','awaiting','v1.0.2','web:yonghyuk')"
        )
        job_id = int(cur.lastrowid)
        await db.execute(
            "INSERT INTO job_hosts (job_id, host, status) VALUES (?, 'alpha', 'queued')", (job_id,)
        )
        await db.commit()
    assert client.slack.posts == []

    await client.post(f"/api/jobs/{job_id}/approve", headers={CSRF_HEADER: client.csrf})
    await wait_finished(client, job_id)
    await wait_posts(client, 2)

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


FAVICONS = ("favicon-32.png", "favicon-16.png", "apple-touch-icon.png")


async def test_favicons_are_served_without_a_session(ui_client):
    """탭 아이콘은 로그인 전에도 받아야 한다 — 브라우저가 세션 없이 요청한다.

    링크와 실제 파일이 어긋나면 탭이 기본 지구본으로 돌아간다.
    `pyproject.toml` 의 package-data(`web/static/*`)에서 빠져도 여기서 잡힌다.
    """
    for name in FAVICONS:
        resp = await ui_client.get(f"/static/{name}")
        assert resp.status == 200, name
        assert (await resp.read()).startswith(b"\x89PNG"), name

    for page in ("/login", "/"):
        # `/` 는 세션이 없으면 /login 으로 보낸다. 링크는 두 화면 모두에 있어야 한다.
        body = await (await ui_client.get(page)).text()
        for name in FAVICONS:
            assert f"/static/{name}" in body, f"{page} / {name}"


async def test_stylesheet_is_public(ui_client):
    """로그인 화면도 스타일시트를 받아야 한다."""
    resp = await ui_client.get("/static/console.css")
    assert resp.status == 200
    body = await resp.text()
    assert "--accent" in body
    # 태그로 시작하면 브라우저 CSS 파서가 첫 규칙을 통째로 삼킨다 (아래 참조).
    assert not body.lstrip().startswith("<")


def test_static_assets_are_not_wrapped_in_html_tags():
    """프로토타입에서 옮길 때 <style>/<script> 태그가 딸려오는 사고를 막는다.

    실제로 한 번 났다. console.css 가 `<style>` 로 시작했는데, 파일은 200 에
    Content-Type: text/css 로 멀쩡히 나가서 서버 쪽에서는 아무 티가 안 났다.
    브라우저만 `<style> :root` 를 하나의 셀렉터로 오인해 그 뒤 `{...}` 블록을
    통째로 삼켰고, 디자인 토큰이 전부 사라져 화면이 무스타일로 떴다.
    "--accent 가 본문에 있다" 만으로는 이걸 못 잡는다 — 문자열은 그대로 있으니까.
    """
    static = Path(__file__).resolve().parents[1] / "src" / "autodeploy" / "web" / "static"
    assets = sorted(static.glob("*.css")) + sorted(static.glob("*.js"))
    assert assets, "static 자산을 못 찾았다"
    for path in assets:
        text = path.read_text(encoding="utf-8")
        assert not text.lstrip().startswith("<"), f"{path.name} 이 태그로 시작한다"
        for tag in ("<style", "</style", "<script", "</script"):
            assert tag not in text, f"{path.name} 에 {tag} 가 남아 있다"


def test_every_control_in_the_console_is_wired_to_the_script():
    """화면의 버튼·폼·입력이 console.js 에서 실제로 쓰이는지 본다.

    실제로 한 번 났다. `＋ 서버 추가`(id=addSrv) 버튼이 console.html 에만 있고
    console.js 어디에서도 바인딩되지 않아, 눌러도 아무 일이 안 일어났다.
    serverModal() 함수는 멀쩡히 있었지만 행마다 붙는 `편집` 버튼에서만 불렸다.
    이런 누락은 서버 테스트로는 절대 안 잡힌다 — 파일은 200 으로 잘 나가니까.

    id 가 console.js 에 등장하는지만 보는 얕은 검사다. 잘못 묶인 것까지는
    못 잡지만, 아예 안 묶인 것은 잡는다.
    """
    static = Path(__file__).resolve().parents[1] / "src" / "autodeploy" / "web" / "static"
    html = (static / "console.html").read_text(encoding="utf-8")
    js = (static / "console.js").read_text(encoding="utf-8")

    controls = re.findall(r'<(?:button|form|input|select)\b[^>]*\bid="([^"]+)"', html)
    assert controls, "console.html 에서 컨트롤을 못 찾았다"
    unwired = [i for i in controls if i not in js]
    assert not unwired, f"console.js 가 쓰지 않는 컨트롤: {unwired}"


def test_every_hubctl_env_has_a_display_name():
    """`-e` 로 넘길 수 있는 환경은 작업 상세에도 이름이 있어야 한다.

    콘솔은 실행자 밑에 `Dev (-e dev)` 처럼 읽기 쉬운 이름과 실제 플래그 값을
    함께 보여준다. hubctl 의 ENVS 에 환경이 하나 늘면 그 환경만 이름 없이
    원문으로 떨어져(envLabel 의 폴백) 표기가 갈린다. 깨지지는 않지만
    한 화면에 두 가지 표기가 섞이는 것을 여기서 잡는다.
    """
    from autodeploy.hubctl import ENVS

    js = (
        Path(__file__).resolve().parents[1]
        / "src" / "autodeploy" / "web" / "static" / "console.js"
    ).read_text(encoding="utf-8")
    block = re.search(r"const ENV_LABEL = \{([^}]*)\}", js)
    assert block, "console.js 에서 ENV_LABEL 을 못 찾았다"
    named = set(re.findall(r"(\w+)\s*:", block.group(1)))
    missing = sorted(set(ENVS) - named)
    assert not missing, f"화면 이름이 없는 환경: {missing}"


def test_the_login_screen_does_not_stay_in_history():
    """로그인 뒤 이동은 replace 여야 한다.

    href 로 넘기면 로그인 화면이 히스토리에 남는다. 그러면 대시보드나 서버
    화면에서 뒤로가기를 누를 때 **이미 로그인했는데도 로그인 화면으로 돌아간다**
    (브라우저가 그 항목을 캐시에서 꺼내므로 서버의 302 도 안 탄다).
    """
    static = Path(__file__).resolve().parents[1] / "src" / "autodeploy" / "web" / "static"
    html = (static / "login.html").read_text(encoding="utf-8")
    assert 'location.replace("/")' in html
    assert 'location.href = "/"' not in html


def test_switching_views_counts_as_a_visit():
    """화면 전환이 히스토리 항목을 남겨야 뒤로가기가 앱 안에서 움직인다.

    전부 replaceState 면 콘솔 전체가 항목 하나라, 서버 화면에서 뒤로가기를
    누르는 순간 앱 밖으로 나가버린다.
    """
    js = (
        Path(__file__).resolve().parents[1]
        / "src" / "autodeploy" / "web" / "static" / "console.js"
    ).read_text(encoding="utf-8")
    assert "history.pushState" in js, "화면 전환이 방문으로 남지 않는다"
    # 뒤로가기로 들어온 경우까지 밀어넣으면 항목이 두 겹으로 쌓인다.
    assert "push: false" in js, "routeFromHash 가 덮어쓰기로 들어오지 않는다"


def test_stylesheet_starts_with_the_token_rule():
    """파일 맨 앞(주석 제외)이 곧바로 :root 규칙이어야 한다.

    위 사고에서 실제로 죽은 것이 이 규칙이다. `--accent` 가 파일 어딘가에
    문자열로 있는 것과, :root 가 최상위 규칙으로 파싱되는 것은 다르다.
    앞에 뭐라도 끼면 CSS 파서는 그것을 셀렉터로 보고 뒤 블록을 삼킨다.
    """
    css = (
        Path(__file__).resolve().parents[1]
        / "src" / "autodeploy" / "web" / "static" / "console.css"
    ).read_text(encoding="utf-8")
    head = re.sub(r"^\s*(/\*.*?\*/\s*)*", "", css, flags=re.S)
    assert head.startswith(":root{"), f"토큰 규칙 앞에 뭔가 있다: {head[:40]!r}"


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
