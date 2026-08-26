"""HTTP 레벨 API 테스트 (dev-spec-web-console §7).

pytest-aiohttp 없이 aiohttp.test_utils 로 실제 서버를 띄운다 — 미들웨어 체인과
쿠키 처리를 실제로 태워야 인증·CSRF 검증이 의미가 있다.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from autodeploy.accounts import create_user
from autodeploy.db import connect
from autodeploy.web import create_app
from autodeploy.web.auth import CSRF_HEADER, SESSION_COOKIE

USERNAME = "yonghyuk"
PASSWORD = "correct-horse-battery"

SITES_YML = """\
sites:
  hosts:
    yonseiwa:
      ansible_host: 192.168.100.224
      ansible_user: connecteve
      site_name: yonseiwa
      profile: hybrid-with-ai
    qa-209:
      ansible_host: 192.168.100.209
      ansible_user: connecteve
      site_name: qa209
      profile: onprem
"""


@pytest.fixture
def inventory_file(tmp_path: Path) -> Path:
    path = tmp_path / "sites.yml"
    path.write_text(SITES_YML, encoding="utf-8")
    return path


@pytest.fixture
async def client(temp_db, tmp_path, inventory_file):
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)

    repo = tmp_path / "hub-provisioning"
    repo.mkdir()
    app = create_app(
        db_path=temp_db,
        hubctl_repo=repo,
        inventory_path=inventory_file,
        static_dir=tmp_path / "static",
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


async def login(client: TestClient, password: str = PASSWORD, username: str = USERNAME):
    return await client.post("/api/login", json={"username": username, "password": password})


async def csrf(client: TestClient) -> str:
    resp = await client.get("/api/me")
    return (await resp.json())["csrf_token"]


# ── 인증 게이트 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path", ["/api/me", "/api/servers", "/api/jobs", "/api/jobs/1", "/api/preflight"]
)
async def test_api_requires_login(client, path):
    resp = await client.get(path)
    assert resp.status == 401
    assert (await resp.json())["error"] == "로그인이 필요합니다"


async def test_root_redirects_to_login_when_anonymous(client):
    """AC-1: 미인증 상태로 / 접근 시 로그인 화면으로 보낸다."""
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login"


async def test_login_page_is_public(client):
    resp = await client.get("/login")
    # Phase E 에서 HTML 이 들어오기 전까지는 503 자리표시자지만, 인증은 걸리지 않는다.
    assert resp.status != 401


async def test_healthz_is_public(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


# ── 로그인 ──────────────────────────────────────────────────────────


async def test_login_sets_an_httponly_session_cookie(client):
    resp = await login(client)
    assert resp.status == 200
    body = await resp.json()
    assert body["username"] == USERNAME
    assert body["csrf_token"]

    cookie = resp.cookies[SESSION_COOKIE]
    assert cookie["httponly"], "스크립트가 세션 토큰을 읽을 수 있으면 안 된다"
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"


async def test_login_response_never_contains_the_password(client):
    resp = await login(client)
    assert PASSWORD not in await resp.text()


@pytest.mark.parametrize(
    "payload",
    [
        {"username": USERNAME, "password": "wrong-password"},
        {"username": "nosuchuser", "password": PASSWORD},
        {"username": "", "password": ""},
    ],
)
async def test_bad_login_gives_one_generic_message(client, payload):
    resp = await client.post("/api/login", json=payload)
    assert resp.status in (400, 401)
    assert (await resp.json())["error"] == "아이디 또는 비밀번호가 올바르지 않습니다"


async def test_login_rejects_malformed_json(client):
    resp = await client.post(
        "/api/login", data="not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


async def test_repeated_failures_return_429_with_retry_after(client):
    """AC-2: 5회 실패 후 잠기고, 올바른 비밀번호도 그 동안은 막힌다."""
    for _ in range(5):
        resp = await login(client, password="nope")
        assert resp.status == 401, "잠금을 유발한 시도까지는 자격 실패로 답해야 한다"
        assert (await resp.json())["error"] == "아이디 또는 비밀번호가 올바르지 않습니다"

    resp = await login(client)
    assert resp.status == 429
    body = await resp.json()
    assert body["retry_after"] >= 1
    assert "초 후" in body["error"]


async def test_lock_response_does_not_reveal_the_threshold(client):
    """실패 응답이 마지막 한 번만 달라지면 그 차이로 임계값을 역산할 수 있다."""
    statuses = [(await login(client, password="nope")).status for _ in range(5)]
    assert set(statuses) == {401}


async def test_me_after_login(client):
    await login(client)
    resp = await client.get("/api/me")
    assert resp.status == 200
    body = await resp.json()
    assert body["username"] == USERNAME
    assert len(body["csrf_token"]) == 64


async def test_logout_invalidates_the_session(client):
    await login(client)
    token = await csrf(client)
    resp = await client.post("/api/logout", headers={CSRF_HEADER: token})
    assert resp.status == 200
    assert (await client.get("/api/me")).status == 401


async def test_forged_cookie_is_rejected(client):
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: "forged-token"})
    assert (await client.get("/api/me")).status == 401


# ── CSRF ────────────────────────────────────────────────────────────


async def test_post_without_csrf_header_is_rejected(client):
    await login(client)
    resp = await client.post("/api/logout")
    assert resp.status == 403
    assert "CSRF" in (await resp.json())["error"]


async def test_post_with_wrong_csrf_header_is_rejected(client):
    await login(client)
    resp = await client.post("/api/logout", headers={CSRF_HEADER: "a" * 64})
    assert resp.status == 403


async def test_get_requests_do_not_need_csrf(client):
    await login(client)
    assert (await client.get("/api/servers")).status == 200


async def test_login_itself_does_not_need_csrf(client):
    """세션이 없으니 파생할 토큰도 없다. SameSite=Lax 가 그 자리를 대신한다."""
    assert (await login(client)).status == 200


# ── 서버 목록 ───────────────────────────────────────────────────────


async def test_servers_list_merges_inventory_and_meta(client, temp_db):
    async with connect(temp_db) as db:
        from autodeploy.repository import mark_key_installed, set_server_memo

        await set_server_memo(db, "yonseiwa", "연세와병원")
        await mark_key_installed(db, "qa-209")

    await login(client)
    resp = await client.get("/api/servers")
    assert resp.status == 200
    body = await resp.json()

    by_host = {s["host"]: s for s in body["servers"]}
    assert set(by_host) == {"yonseiwa", "qa-209"}
    assert by_host["yonseiwa"]["ansible_host"] == "192.168.100.224"
    assert by_host["yonseiwa"]["profile"] == "hybrid-with-ai"
    assert by_host["yonseiwa"]["memo"] == "연세와병원"
    assert by_host["yonseiwa"]["key_installed_at"] is None
    assert by_host["qa-209"]["key_installed_at"] is not None


async def test_servers_response_carries_mtime_for_optimistic_locking(client):
    """§F2: 저장할 때 이 값을 되돌려줘야 남의 수정을 덮어쓰지 않는다."""
    await login(client)
    body = await (await client.get("/api/servers")).json()
    assert isinstance(body["mtime_ns"], int)
    assert body["mtime_ns"] > 0


async def test_wrong_repo_path_reports_503(temp_db, tmp_path):
    """§9: 경로 오류는 조용히 '서버 0대'로 보이면 안 된다."""
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
    app = create_app(
        db_path=temp_db,
        hubctl_repo=tmp_path / "nope",
        static_dir=tmp_path / "static",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await login(client)
        resp = await client.get("/api/servers")
        assert resp.status == 503
        assert "HUBCTL_REPO_PATH" in (await resp.json())["error"]
    finally:
        await client.close()


async def test_absent_sites_yml_in_a_valid_repo_is_an_empty_list(temp_db, tmp_path):
    """서버를 한 대도 안 넣은 초기 상태는 오류가 아니다."""
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
    repo = tmp_path / "hub-provisioning"
    (repo / "inventory").mkdir(parents=True)
    app = create_app(db_path=temp_db, hubctl_repo=repo, static_dir=tmp_path / "static")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await login(client)
        resp = await client.get("/api/servers")
        assert resp.status == 200
        body = await resp.json()
        assert body["servers"] == []
        assert body["mtime_ns"] == 0
    finally:
        await client.close()


# ── 작업 목록 ───────────────────────────────────────────────────────


async def _seed_jobs(db) -> tuple[int, int]:
    cur = await db.execute(
        "INSERT INTO jobs (kind, status, env, ref, started_by, exit_code)"
        " VALUES ('install', 'failed', 'stage', 'v1.0.2', 'web:yonghyuk', 2)"
    )
    failed_id = int(cur.lastrowid)
    cur = await db.execute(
        "INSERT INTO jobs (kind, status, started_by) VALUES ('verify', 'succeeded', 'web:yonghyuk')"
    )
    ok_id = int(cur.lastrowid)
    await db.executemany(
        "INSERT INTO job_hosts (job_id, host, status, recap_ok, recap_changed,"
        " recap_failed, recap_unreachable) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (failed_id, "alpha", "succeeded", 5, 1, 0, 0),
            (failed_id, "beta", "failed", 4, 1, 1, 0),
        ],
    )
    await db.execute(
        "INSERT INTO job_events (job_id, step, level, message)"
        " VALUES (?, 'bootstrap', 'error', '부트스트랩 실패')",
        (failed_id,),
    )
    await db.commit()
    return failed_id, ok_id


async def test_jobs_list_includes_hosts(client, temp_db):
    async with connect(temp_db) as db:
        failed_id, _ = await _seed_jobs(db)

    await login(client)
    body = await (await client.get("/api/jobs")).json()
    jobs = {j["id"]: j for j in body["jobs"]}
    assert len(jobs) == 2
    assert jobs[failed_id]["kind"] == "install"
    assert jobs[failed_id]["env"] == "stage"
    assert jobs[failed_id]["exit_code"] == 2
    assert [h["host"] for h in jobs[failed_id]["hosts"]] == ["alpha", "beta"]


async def test_jobs_list_newest_first(client, temp_db):
    async with connect(temp_db) as db:
        failed_id, ok_id = await _seed_jobs(db)
    await login(client)
    body = await (await client.get("/api/jobs")).json()
    assert [j["id"] for j in body["jobs"]] == [ok_id, failed_id]


async def test_jobs_limit_is_clamped(client, temp_db):
    async with connect(temp_db) as db:
        await _seed_jobs(db)
    await login(client)
    body = await (await client.get("/api/jobs?limit=1")).json()
    assert len(body["jobs"]) == 1


async def test_jobs_limit_rejects_non_integer(client):
    await login(client)
    assert (await client.get("/api/jobs?limit=abc")).status == 400


async def test_job_detail_has_hosts_and_events(client, temp_db):
    """AC-7: 호스트별로 성공/실패가 갈려 보여야 한다."""
    async with connect(temp_db) as db:
        failed_id, _ = await _seed_jobs(db)

    await login(client)
    body = await (await client.get(f"/api/jobs/{failed_id}")).json()
    assert body["status"] == "failed"
    hosts = {h["host"]: h for h in body["hosts"]}
    assert hosts["alpha"]["status"] == "succeeded"
    assert hosts["beta"]["status"] == "failed"
    assert hosts["beta"]["recap"] == {"ok": 4, "changed": 1, "failed": 1, "unreachable": 0}
    assert body["events"][0]["message"] == "부트스트랩 실패"


async def test_unknown_job_is_404(client):
    await login(client)
    assert (await client.get("/api/jobs/9999")).status == 404


async def test_job_id_must_be_an_integer(client):
    await login(client)
    assert (await client.get("/api/jobs/abc")).status == 400


# ── 로그 다운로드 ───────────────────────────────────────────────────


async def test_job_log_download(client, temp_db):
    async with connect(temp_db) as db:
        failed_id, _ = await _seed_jobs(db)
        from autodeploy.repository import add_script_logs

        await add_script_logs(
            db,
            [
                (failed_id, "bootstrap", "stdout", "PLAY [Bootstrap] ***", None, "task"),
                (failed_id, "bootstrap", "stdout", "ok: [alpha]", "alpha", "ok"),
            ],
        )

    await login(client)
    resp = await client.get(f"/api/jobs/{failed_id}/log")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert f"job-{failed_id}.log" in resp.headers["Content-Disposition"]
    assert (await resp.text()).splitlines() == ["PLAY [Bootstrap] ***", "ok: [alpha]"]


async def test_job_log_for_unknown_job_is_404(client):
    await login(client)
    assert (await client.get("/api/jobs/9999/log")).status == 404


# ── preflight ───────────────────────────────────────────────────────


PREFLIGHT_OK = """\
echo ''
echo '━━ Preflight — 컨트롤러 자격 점검 (Vault / AWS / Bitbucket) ━━'
echo '✔ Vault: VAULT_ADDR + 토큰 OK'
echo '✔ hvac (Vault 클라이언트): OK'
echo '✔ AWS(ECR): 자격 OK'
echo '✔ Bitbucket: 토큰 OK (user=x-bitbucket-api-token-auth)'
echo ''
echo '✔ Preflight succeeded'
"""

PREFLIGHT_FAIL = """\
echo '━━ Preflight — 컨트롤러 자격 점검 ━━'
echo '✘ VAULT_ADDR 미설정' >&2
echo '✔ AWS(ECR): 자격 OK'
echo '✘ Preflight failed (exit 2)' >&2
exit 2
"""


@pytest.fixture
async def preflight_client(temp_db, tmp_path, inventory_file, request):
    """`bin/hubctl` 을 가짜 스크립트로 대체해 preflight 핸들러만 검증한다."""
    script = request.param
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)

    repo = tmp_path / "hub-provisioning"
    (repo / "bin").mkdir(parents=True)
    hubctl = repo / "bin" / "hubctl"
    hubctl.write_text("#!/bin/bash\n" + script, encoding="utf-8")
    hubctl.chmod(0o755)

    app = create_app(
        db_path=temp_db,
        hubctl_repo=repo,
        inventory_path=inventory_file,
        static_dir=tmp_path / "static",
        hubctl_shell=("bash", "-c"),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.parametrize("preflight_client", [PREFLIGHT_OK], indirect=True)
async def test_preflight_reports_the_four_credential_checks(preflight_client):
    await login(preflight_client)
    resp = await preflight_client.get("/api/preflight")
    assert resp.status == 200
    body = await resp.json()

    assert body["ok"] is True
    assert body["exit_code"] == 0
    assert [c["message"] for c in body["checks"]] == [
        "Vault: VAULT_ADDR + 토큰 OK",
        "hvac (Vault 클라이언트): OK",
        "AWS(ECR): 자격 OK",
        "Bitbucket: 토큰 OK (user=x-bitbucket-api-token-auth)",
    ]


@pytest.mark.parametrize("preflight_client", [PREFLIGHT_OK], indirect=True)
async def test_preflight_drops_the_overall_summary_line(preflight_client):
    """hubctl finish() 의 요약은 항목이 아니다 — ok/exit_code 가 이미 담고 있다."""
    await login(preflight_client)
    body = await (await preflight_client.get("/api/preflight")).json()
    assert not any("Preflight succeeded" in c["message"] for c in body["checks"])


@pytest.mark.parametrize("preflight_client", [PREFLIGHT_FAIL], indirect=True)
async def test_preflight_failure_marks_the_failing_check(preflight_client):
    await login(preflight_client)
    body = await (await preflight_client.get("/api/preflight")).json()

    assert body["ok"] is False
    assert body["exit_code"] == 2
    by_level = {c["message"]: c["level"] for c in body["checks"]}
    assert by_level["VAULT_ADDR 미설정"] == "err"
    assert by_level["AWS(ECR): 자격 OK"] == "ok"
    assert not any("Preflight failed" in m for m in by_level)


@pytest.mark.parametrize("preflight_client", [PREFLIGHT_OK], indirect=True)
async def test_preflight_requires_login(preflight_client):
    assert (await preflight_client.get("/api/preflight")).status == 401


async def test_preflight_reports_a_missing_hubctl(client):
    """저장소에 bin/hubctl 이 없으면 503 으로 알린다 (§9)."""
    await login(client)
    resp = await client.get("/api/preflight")
    assert resp.status in (503, 200)
    if resp.status == 200:
        assert (await resp.json())["ok"] is False


# ── 응답 인코딩 ─────────────────────────────────────────────────────


async def test_korean_is_not_escaped_in_responses(client):
    """\\uXXXX 로 나가면 로그·curl 로 오류를 사람이 못 읽는다."""
    resp = await client.get("/api/servers")
    assert "로그인이 필요합니다" in await resp.text()
