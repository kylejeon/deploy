"""Phase D — 작업 생성·실행·취소·승인 + SSE + 서버 변경 API.

작업 실행은 **진짜 서브프로세스**를 띄운다. `bin/hubctl` 자리에 ansible 출력을
흉내내는 스크립트를 놓고 bash 로 감싸므로, 명령 조립 → 실행 → 줄 파싱 → DB 적재
→ RECAP 반영 → 상태 확정까지 실제 경로를 그대로 탄다.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from autodeploy.accounts import create_user
from autodeploy.ansible_log import LineKind, ParsedLine
from autodeploy.db import connect
from autodeploy.queue import JobQueue
from autodeploy.repository import mark_key_installed
from autodeploy.web import create_app, keys
from autodeploy.web import jobs as jobs_module
from autodeploy.web.auth import CSRF_HEADER
from autodeploy.web.jobs import JobService, _LogSink
from autodeploy.web.sse import SseBroker

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
    beta:
      ansible_host: 192.0.2.11
      ansible_user: connecteve
      site_name: beta
      profile: onprem
    nokey:
      ansible_host: 192.0.2.12
      ansible_user: connecteve
      site_name: nokey
      profile: onprem
"""

# alpha 성공 / beta 실패. hubctl 이 받은 인자를 첫 줄에 그대로 뱉어 검증에 쓴다.
FAKE_HUBCTL = r"""#!/bin/bash
echo "ARGS: $*"
echo ''
echo 'PLAY [hubctl verify] ****'
echo ''
echo 'TASK [Gathering Facts] ****'
echo 'ok: [alpha]'
echo 'fatal: [beta]: FAILED! =>'
echo '    msg: 실패했습니다'
echo '    rc: 1'
echo ''
echo 'PLAY RECAP ****'
echo 'alpha  : ok=2 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0'
echo 'beta   : ok=1 changed=0 unreachable=0 failed=1 skipped=0 rescued=0 ignored=0'
if [ -n "$FAKE_EXIT" ]; then exit "$FAKE_EXIT"; fi
exit 0
"""

# beta 만 죽고 나머지는 계속 돈다. 도는 도중의 호스트별 상태를 보기 위한 것.
PARTIAL_FAIL_HUBCTL = r"""#!/bin/bash
echo 'PLAY [Bootstrap (host -> empty k0s)] ****'
echo 'TASK [Gathering Facts] ****'
echo 'ok: [alpha]'
echo 'fatal: [beta]: FAILED! =>'
echo '    msg: 죽었습니다'
touch "$FAKE_STARTED"
sleep 60
"""

# 위와 같은데 ansible 이 그 실패를 무시한 경우 (ignore_errors). `...ignoring` 은
# **호스트 이름이 없는 줄**로 나온다 — 실제 ansible 출력 그대로다.
IGNORED_FAIL_HUBCTL = r"""#!/bin/bash
echo 'PLAY [Bootstrap (host -> empty k0s)] ****'
echo 'TASK [Gathering Facts] ****'
echo 'ok: [alpha]'
echo 'fatal: [beta]: FAILED! =>'
echo '    msg: 무시되는 실패'
echo '...ignoring'
touch "$FAKE_STARTED"
sleep 60
"""

# 원샷 `hubctl patch` — create 를 끝내고 stdin 으로 [y/N] 을 묻는다.
# 프롬프트 줄에 **개행이 없는 것**까지 진짜와 똑같이 맞춘다 (printf).
CONFIRM_HUBCTL = r"""#!/bin/bash
echo "ARGS: $*"
echo 'PLAY [패치 번들 생성 (patch_create)] ****'
echo 'TASK [patch_create | 산출 요약] ****'
echo 'ok: [alpha]'
echo '  위 산출 요약의 변경 앱 목록이 이번에 적용될 내용입니다.'
printf '이대로 적용(apply)을 진행할까요? [y/N] '
read -r reply || reply=""
echo
echo "REPLY:[${reply}]"
case "$reply" in
  y|Y|yes|YES) ;;
  *) echo 'CANCELLED'; exit 2 ;;
esac
echo 'PLAY [패치 번들 적용 (patch_apply)] ****'
echo 'PLAY RECAP ****'
echo 'alpha  : ok=2 changed=1 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0'
exit 0
"""

# 롤을 차례로 흘린 뒤 멈춰 선다. 도는 도중의 진행 단계를 보기 위한 것.
STEPPING_HUBCTL = r"""#!/bin/bash
echo 'PLAY [Bootstrap (host -> empty k0s)] ****'
echo 'TASK [os_base : apt] ****'
echo 'ok: [alpha]'
echo 'TASK [k0s : install] ****'
echo 'ok: [alpha]'
echo 'PLAY [Configuration (empty k0s -> platform)] ****'
echo 'TASK [gitops_publish : push] ****'
echo 'ok: [alpha]'
touch "$FAKE_STARTED"
sleep 60
"""

SLOW_HUBCTL = r"""#!/bin/bash
echo "ARGS: $*"
echo 'PLAY [hubctl verify] ****'
touch "$FAKE_STARTED"
sleep 60
"""


def write_repo(tmp_path: Path, script: str = FAKE_HUBCTL) -> Path:
    """가짜 hub-provisioning. **진짜 git 저장소**로 만든다.

    작업은 시작 직전에 `git fetch/checkout/merge` 로 저장소를 main 최신으로
    맞춘다. 여기가 git 저장소가 아니면 그 단계에서 전부 막혀 실제 실행 경로를
    검증할 수 없다. origin 을 자기 자신으로 걸어 네트워크 없이 성립시킨다.
    """
    repo = tmp_path / "hub-provisioning"
    (repo / "bin").mkdir(parents=True)
    hubctl = repo / "bin" / "hubctl"
    hubctl.write_text(script, encoding="utf-8")
    hubctl.chmod(0o755)
    git_init(repo)
    return repo


def break_origin(repo: Path) -> None:
    """origin 을 없는 경로로 돌려 `git fetch` 가 실패하게 만든다."""
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "/nonexistent/x.git"],
        check=True, capture_output=True,
    )


def git_init(repo: Path) -> None:
    """origin 이 자기 자신인 main 저장소. 최신화 명령이 그대로 성립한다."""
    who = ["-c", "user.email=t@example.com", "-c", "user.name=t",
           "-c", "commit.gpgsign=false"]
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), *a], check=True, capture_output=True
    )
    run("-c", "init.defaultBranch=main", "init", "-q")
    run(*who, "add", "-A")
    run(*who, "commit", "-qm", "init")
    # main 아닌 브랜치도 하나 둔다 — 콘솔이 dev 로 맞출 수 있어야 한다.
    run("branch", "dev")
    run("remote", "add", "origin", str(repo))


async def make_client(temp_db, tmp_path, *, script: str = FAKE_HUBCTL, env=None, keys_for=("alpha", "beta")):
    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
        for host in keys_for:
            await mark_key_installed(db, host)

    inventory = tmp_path / "sites.yml"
    inventory.write_text(SITES_YML, encoding="utf-8")

    app = create_app(
        db_path=temp_db,
        hubctl_repo=write_repo(tmp_path, script),
        inventory_path=inventory,
        static_dir=tmp_path / "static",
        hubctl_shell=("bash", "-c"),
        hubctl_env=env or {},
        log_dir=tmp_path / "joblogs",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.post(
        "/api/login", json={"username": USERNAME, "password": PASSWORD}
    )
    client.csrf = (await resp.json())["csrf_token"]
    return client


@pytest.fixture
async def client(temp_db, tmp_path):
    c = await make_client(temp_db, tmp_path)
    try:
        yield c
    finally:
        await c.close()


async def post(client, path, payload=None):
    return await client.post(
        path, json=payload or {}, headers={CSRF_HEADER: client.csrf}
    )


async def wait_finished(client, job_id, *, timeout=20.0):
    """작업이 종료 상태에 이를 때까지."""
    async def poll():
        while True:
            body = await (await client.get(f"/api/jobs/{job_id}")).json()
            if body["status"] in ("succeeded", "failed", "cancelled", "awaiting"):
                return body
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(poll(), timeout)


# ── 작업 생성 ───────────────────────────────────────────────────────


async def test_create_verify_job_runs_to_completion(client):
    resp = await post(client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]})
    assert resp.status == 201
    job_id = (await resp.json())["id"]

    job = await wait_finished(client, job_id)
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["started_by"] == f"web:{USERNAME}"


async def test_host_results_come_from_the_play_recap(client):
    """AC-7: 한 대만 실패하면 호스트별로 갈려야 한다."""
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    job = await wait_finished(client, job_id)

    hosts = {h["host"]: h for h in job["hosts"]}
    assert hosts["alpha"]["status"] == "succeeded"
    assert hosts["beta"]["status"] == "failed"
    assert hosts["beta"]["recap"] == {"ok": 1, "changed": 0, "failed": 1, "unreachable": 0}


async def test_command_uses_a_single_limit_with_all_hosts(client):
    """AC-4: 선택한 서버만, -l a,b 하나로."""
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
    assert "ARGS: verify -l alpha,beta" in text


async def test_logs_are_stored_with_host_and_kind(client, temp_db):
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    async with connect(temp_db) as db:
        from autodeploy.repository import get_script_logs

        rows = await get_script_logs(db, job_id)

    by_line = {r["line"]: r for r in rows}
    assert by_line["ok: [alpha]"]["host"] == "alpha"
    assert by_line["ok: [alpha]"]["kind"] == "ok"
    # fatal 의 YAML 본문도 같은 호스트로 묶여야 호스트 필터에서 안 사라진다 (AC-6).
    assert by_line["    rc: 1"]["host"] == "beta"
    assert by_line["    rc: 1"]["kind"] == "err"


async def test_failed_exit_code_marks_the_job_failed(temp_db, tmp_path):
    c = await make_client(temp_db, tmp_path, env={"FAKE_EXIT": "2"})
    try:
        job_id = (await (await post(
            c, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
        )).json())["id"]
        job = await wait_finished(c, job_id)
        assert job["status"] == "failed"
        assert job["exit_code"] == 2
        assert "종료 코드 2" in job["error_message"]
    finally:
        await c.close()


# ── 생성 검증 ───────────────────────────────────────────────────────


async def test_unknown_kind_rejected(client):
    resp = await post(client, "/api/jobs", {"kind": "destroy", "hosts": ["alpha"]})
    assert resp.status == 400
    assert "알 수 없는 작업 종류" in (await resp.json())["error"]


async def test_unknown_host_rejected(client):
    resp = await post(client, "/api/jobs", {"kind": "verify", "hosts": ["ghost"]})
    assert resp.status == 400
    assert "인벤토리에 없는 서버" in (await resp.json())["error"]


async def test_host_without_ssh_key_blocks_the_job(client):
    """AC-16: 30분 돌리고 SSH 로 죽는 것보다 시작 전에 거른다."""
    resp = await post(client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "nokey"]})
    assert resp.status == 400
    body = await resp.json()
    assert "nokey" in body["error"]
    assert "alpha" not in body["error"], "문제가 있는 서버만 지목해야 한다"


async def test_empty_hosts_rejected(client):
    resp = await post(client, "/api/jobs", {"kind": "verify", "hosts": []})
    assert resp.status == 400


async def test_install_requires_env(client):
    resp = await post(client, "/api/jobs", {"kind": "install", "hosts": ["alpha"]})
    assert resp.status == 400
    assert "env" in (await resp.json())["error"]


async def test_invalid_job_is_not_recorded(client):
    """조립 단계에서 걸러 DB 에 흔적을 남기지 않는다."""
    await post(client, "/api/jobs", {"kind": "install", "hosts": ["alpha"]})
    assert (await (await client.get("/api/jobs")).json())["jobs"] == []


async def test_job_creation_needs_csrf(client):
    resp = await client.post("/api/jobs", json={"kind": "verify", "hosts": ["alpha"]})
    assert resp.status == 403


# ── clean 재검증 ────────────────────────────────────────────────────


async def test_clean_requires_the_exact_hostname(client):
    """AC-9 / §7: 화면 검증만 믿지 않고 서버에서 다시 대조한다."""
    resp = await post(client, "/api/jobs", {
        "kind": "clean", "hosts": ["alpha"], "clean_mode": "reset", "confirm": "alph"
    })
    assert resp.status == 400
    assert "호스트명을 정확히" in (await resp.json())["error"]


async def test_clean_without_confirm_is_rejected(client):
    resp = await post(client, "/api/jobs", {
        "kind": "clean", "hosts": ["alpha"], "clean_mode": "reset"
    })
    assert resp.status == 400


async def test_clean_refuses_multiple_hosts(client):
    resp = await post(client, "/api/jobs", {
        "kind": "clean", "hosts": ["alpha", "beta"], "clean_mode": "reset", "confirm": "alpha"
    })
    assert resp.status == 400
    assert "한 대" in (await resp.json())["error"]


async def test_clean_keep_data_reaches_the_playbook(temp_db, tmp_path):
    """AC-9: --keep-data 선택 시 keep_data=true 로 전달된다."""
    repo = write_repo(tmp_path)
    # clean 은 hubctl 이 아니라 ansible-playbook 을 직접 부른다. 그 자리를 가로챈다.
    fake = repo / "ansible-playbook"
    fake.write_text('#!/bin/bash\necho "ARGS: $*"\nexit 0\n', encoding="utf-8")
    fake.chmod(0o755)

    async with connect(temp_db) as db:
        await create_user(db, USERNAME, PASSWORD)
        await mark_key_installed(db, "alpha")
    inventory = tmp_path / "sites.yml"
    inventory.write_text(SITES_YML, encoding="utf-8")

    app = create_app(
        db_path=temp_db,
        hubctl_repo=repo,
        inventory_path=inventory,
        static_dir=tmp_path / "static",
        # PATH 앞에 저장소를 끼워 가짜 ansible-playbook 이 잡히게 한다.
        hubctl_shell=("bash", "-c"),
        hubctl_env={"PATH": f"{repo}:/usr/bin:/bin"},
        log_dir=tmp_path / "joblogs",
    )
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        resp = await c.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        c.csrf = (await resp.json())["csrf_token"]
        job_id = (await (await post(c, "/api/jobs", {
            "kind": "clean", "hosts": ["alpha"],
            "clean_mode": "reset-keep", "confirm": "alpha",
        })).json())["id"]
        await wait_finished(c, job_id)

        text = await (await c.get(f"/api/jobs/{job_id}/log")).text()
        assert "-e confirm=alpha" in text
        assert "-e level=reset" in text
        assert "-e keep_data=true" in text
    finally:
        await c.close()


# ── 취소 ────────────────────────────────────────────────────────────


async def test_cancel_running_job_kills_the_process(temp_db, tmp_path):
    """AC-8: 실제 프로세스가 죽고 상태가 cancelled 가 된다."""
    started = tmp_path / "started"
    c = await make_client(
        temp_db, tmp_path, script=SLOW_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        job_id = (await (await post(
            c, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
        )).json())["id"]

        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "가짜 hubctl 이 실제로 떠야 의미 있는 테스트다"

        resp = await post(c, f"/api/jobs/{job_id}/cancel")
        assert resp.status == 200
        assert (await resp.json())["outcome"] == "requested"

        job = await wait_finished(c, job_id)
        assert job["status"] == "cancelled"
        assert job["cancel_by"] == USERNAME
        assert job["hosts"][0]["status"] == "cancelled"
    finally:
        await c.close()


async def test_cancel_queued_job_never_starts_it(temp_db, tmp_path):
    """동시 실행 1개라 두 번째는 대기한다. 대기 중 취소는 프로세스를 띄우지 않는다."""
    started = tmp_path / "started"
    c = await make_client(
        temp_db, tmp_path, script=SLOW_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        first = (await (await post(c, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]})).json())["id"]
        second = (await (await post(c, "/api/jobs", {"kind": "verify", "hosts": ["beta"]})).json())["id"]

        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)

        body = await (await c.get(f"/api/jobs/{second}")).json()
        assert body["status"] == "queued"
        assert body["queue_position"] == 1, "앞선 작업 하나가 남아있다"

        resp = await post(c, f"/api/jobs/{second}/cancel")
        assert (await resp.json())["outcome"] == "dequeued"
        assert (await (await c.get(f"/api/jobs/{second}")).json())["status"] == "cancelled"

        await post(c, f"/api/jobs/{first}/cancel")
        await wait_finished(c, first)
    finally:
        await c.close()


async def test_cancel_finished_job_is_409(client):
    job_id = (await (await post(client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]})).json())["id"]
    await wait_finished(client, job_id)
    resp = await post(client, f"/api/jobs/{job_id}/cancel")
    assert resp.status == 409


async def test_cancel_unknown_job_is_404(client):
    assert (await post(client, "/api/jobs/9999/cancel")).status == 404


# ── patch 승인 ──────────────────────────────────────────────────────


async def seed_awaiting_patch(temp_db, host="alpha"):
    """`승인 대기` 상태의 patch 를 DB 에 직접 만든다.

    콘솔은 이제 원샷만 만들므로 이 상태로 가는 길이 화면에는 없다. 그래도
    폐쇄망 반입(create → apply)과 승인/거부/방치 정리는 살아 있는 기능이라
    상태 기계 자체는 계속 검증한다.
    """
    async with connect(temp_db) as db:
        cur = await db.execute(
            "INSERT INTO jobs (kind, status, ref, started_by)"
            " VALUES ('patch','awaiting','v1.0.2',?)",
            (f"web:{USERNAME}",),
        )
        job_id = int(cur.lastrowid)
        await db.execute(
            "INSERT INTO job_hosts (job_id, host, status) VALUES (?, ?, 'queued')",
            (job_id, host),
        )
        await db.commit()
    return job_id


async def test_patch_runs_create_and_apply_in_one_shot(temp_db, tmp_path):
    """콘솔의 patch 는 원샷이다 — 승인 대기 없이 한 작업이 끝까지 간다.

    `patch create` 는 컨트롤러 로컬(base = 준 git ref)이라 폐쇄망 반입용이고,
    온라인 사이트는 타겟에서 도는 원샷(base = 지금 깔려 있는 release-config)이
    맞다 (patch-create.yml 헤더).
    """
    client = await make_client(temp_db, tmp_path, script=CONFIRM_HUBCTL)
    try:
        job_id = (await (await post(
            client, "/api/jobs",
            {"kind": "patch", "ref": "v1.0.2", "ref_type": "tag", "hosts": ["alpha"]},
        )).json())["id"]
        job = await wait_finished(client, job_id)
        assert job["status"] == "succeeded"

        text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
        assert "ARGS: patch -l alpha -- -e hub_deploy_ref=v1.0.2 -e hub_deploy_ref_type=tag" in text
        assert "patch create" not in text and "patch apply" not in text
    finally:
        await client.close()


async def test_patch_answers_the_confirmation_prompt(temp_db, tmp_path):
    """원샷이 중간에 묻는 `[y/N]` 에 y 를 넣는다.

    프롬프트 줄은 개행이 없어 줄 단위 펌프에 안 잡힌다 — 개행으로 끝나는 바로
    앞 줄(PATCH_CONFIRM_MARKER)을 신호로 쓴다. 이게 깨지면 자식이 EOF 를 받아
    "사용자 취소"(rc=2)로 끝나므로, 아래 succeeded 가 곧 그 검증이다.
    """
    client = await make_client(temp_db, tmp_path, script=CONFIRM_HUBCTL)
    try:
        job_id = (await (await post(
            client, "/api/jobs", {"kind": "patch", "ref": "v1", "hosts": ["alpha"]}
        )).json())["id"]
        job = await wait_finished(client, job_id)

        text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
        assert "REPLY:[y]" in text
        assert "CANCELLED" not in text
        assert job["status"] == "succeeded"
        # 사람이 [y/N] 을 눌렀을 자리다. 대신 답했다는 사실이 기록에 남아야 한다.
        assert any("y 를 보냈습니다" in e["message"] for e in job["events"])
    finally:
        await client.close()


async def test_only_patch_gets_an_answer_on_stdin(temp_db, tmp_path):
    """다른 종류는 stdin 을 곧바로 닫는다 — 안 그러면 읽는 명령이 영영 기다린다."""
    client = await make_client(temp_db, tmp_path, script=CONFIRM_HUBCTL)
    try:
        job_id = (await (await post(
            client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
        )).json())["id"]
        job = await wait_finished(client, job_id)

        text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
        assert "REPLY:[]" in text, "EOF 를 받아 빈 값이어야 한다"
        assert job["status"] == "failed"
    finally:
        await client.close()


async def test_patch_targets_run_and_end_together(client):
    """대상은 미리 골라 job_hosts 에 남고, 원샷이라 그대로 결말까지 간다."""
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "patch", "ref": "v1", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    job = await wait_finished(client, job_id)

    assert [h["host"] for h in job["hosts"]] == ["alpha", "beta"]
    assert {h["status"] for h in job["hosts"]} == {"succeeded", "failed"}


async def test_patch_checks_ssh_keys_before_it_starts(client):
    """AC-16 이 patch 에도 걸린다 — 타겟에서 도는 명령이라 키가 없으면 죽는다."""
    resp = await post(
        client, "/api/jobs", {"kind": "patch", "ref": "v1", "hosts": ["alpha", "nokey"]}
    )
    assert resp.status == 400
    assert "nokey" in (await resp.json())["error"]


async def test_patch_requires_targets_and_ref(client):
    assert (await post(client, "/api/jobs", {"kind": "patch", "ref": "v1"})).status == 400
    assert (await post(client, "/api/jobs", {"kind": "patch", "hosts": ["alpha"]})).status == 400


async def test_patch_approve_runs_apply(client, temp_db):
    """폐쇄망 반입 경로. 승인은 기록해 둔 대상에 `patch apply` 를 돌린다."""
    job_id = await seed_awaiting_patch(temp_db)

    resp = await post(client, f"/api/jobs/{job_id}/approve")
    assert resp.status == 200
    job = await wait_finished(client, job_id)
    assert job["status"] == "succeeded"

    text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
    assert "ARGS: patch apply -l alpha" in text
    # 번들 메타가 SoT 라 apply 에는 ref 를 넘기지 않는다.
    assert "patch apply -l alpha -- -e hub_deploy_ref" not in text


async def test_patch_reject_leaves_the_server_untouched(client, temp_db):
    job_id = await seed_awaiting_patch(temp_db)

    resp = await post(client, f"/api/jobs/{job_id}/reject")
    assert resp.status == 200
    job = await wait_finished(client, job_id)
    assert job["status"] == "cancelled"

    text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
    assert "patch apply" not in text, "거부했는데 적용이 돌면 안 된다"
    assert any("번들은 유지" in e["message"] for e in job["events"])


async def test_approve_requires_awaiting_state(client):
    job_id = (await (await post(client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]})).json())["id"]
    await wait_finished(client, job_id)
    resp = await post(client, f"/api/jobs/{job_id}/approve")
    assert resp.status == 409


# ── SSE ─────────────────────────────────────────────────────────────


async def read_sse(client, job_id: str, *, after: int = 0, limit: int = 400):
    """스트림을 끝(type=end)까지 읽어 이벤트 목록으로."""
    events = []
    resp = await client.get(f"/api/jobs/{job_id}/stream?after={after}")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    async for raw in resp.content:
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        events.append(event)
        if event.get("type") == "end" or len(events) >= limit:
            break
    resp.close()
    return events


async def test_stream_replays_a_finished_job(client):
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    events = await read_sse(client, job_id)
    lines = [e["line"] for e in events if e["type"] == "line"]
    assert "ok: [alpha]" in lines
    assert events[-1]["type"] == "end"
    assert events[-1]["status"] == "succeeded"


async def test_stream_after_cursor_skips_replayed_lines(client):
    """AC-5 재연결: 마지막 id 이후만 받아야 중복이 안 생긴다."""
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    everything = await read_sse(client, job_id)
    line_events = [e for e in everything if e["type"] == "line"]
    midpoint = line_events[len(line_events) // 2]["id"]

    rest = await read_sse(client, job_id, after=midpoint)
    ids = [e["id"] for e in rest if e["type"] == "line"]
    assert ids, "이후 줄이 있어야 한다"
    assert min(ids) > midpoint


async def test_stream_carries_host_and_kind(client):
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    events = await read_sse(client, job_id)
    by_line = {e["line"]: e for e in events if e["type"] == "line"}
    assert by_line["ok: [alpha]"]["host"] == "alpha"
    assert by_line["fatal: [beta]: FAILED! =>"]["kind"] == "err"


async def test_stream_requires_login(client):
    await post(client, "/api/logout")
    resp = await client.get("/api/jobs/1/stream")
    assert resp.status == 401


async def test_stream_unknown_job_is_404(client):
    assert (await client.get("/api/jobs/9999/stream")).status == 404


async def test_stream_delivers_lines_while_the_job_runs(temp_db, tmp_path):
    """살아있는 작업에 붙으면 새 줄이 밀려와야 한다 (AC-5)."""
    started = tmp_path / "started"
    c = await make_client(
        temp_db, tmp_path, script=SLOW_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        job_id = (await (await post(
            c, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
        )).json())["id"]
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)

        resp = await c.get(f"/api/jobs/{job_id}/stream")
        seen = []

        async def collect():
            async for raw in resp.content:
                line = raw.decode().strip()
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))
                    if len(seen) >= 2:
                        return

        await asyncio.wait_for(collect(), 15)
        resp.close()
        assert any("PLAY [hubctl verify]" in e.get("line", "") for e in seen)

        await post(c, f"/api/jobs/{job_id}/cancel")
        await wait_finished(c, job_id)
    finally:
        await c.close()


# ── 방치된 승인 대기 정리 (§9) ──────────────────────────────────────


async def test_awaiting_expires_after_the_ttl(client, temp_db):
    """승인 없이 남은 patch 는 24시간 뒤 취소된다. 번들은 컨트롤러에 남는다."""
    job_id = await seed_awaiting_patch(temp_db)

    async with connect(temp_db) as db:
        await db.execute(
            "UPDATE jobs SET created_at = datetime('now', '-25 hours') WHERE id=?", (job_id,)
        )
        await db.commit()

    expired = await client.app[keys.JOB_SERVICE].expire_awaiting(older_than_hours=24)
    assert expired == [job_id]

    body = await (await client.get(f"/api/jobs/{job_id}")).json()
    assert body["status"] == "cancelled"
    assert any("번들은 유지" in e["message"] for e in body["events"])


async def test_recent_awaiting_is_left_alone(client, temp_db):
    job_id = await seed_awaiting_patch(temp_db)

    assert await client.app[keys.JOB_SERVICE].expire_awaiting(older_than_hours=24) == []
    assert (await (await client.get(f"/api/jobs/{job_id}")).json())["status"] == "awaiting"


# ── 작업 기록 삭제 ──────────────────────────────────────────────────


async def delete_jobs(client, payload):
    return await client.delete(
        "/api/jobs", json=payload, headers={CSRF_HEADER: client.csrf}
    )


async def count_rows(temp_db, table: str, job_id: int) -> int:
    async with connect(temp_db) as db:
        async with db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE job_id = ?", (job_id,)
        ) as cur:
            return int((await cur.fetchone())["n"])


async def test_deleting_a_job_takes_its_logs_with_it(client, temp_db):
    """기록만 지우고 로그가 남으면 DB 는 계속 불어난다 (ON DELETE CASCADE 확인).

    이건 스키마를 믿는 것이 아니라 실제로 확인해야 한다 — 외래키 CASCADE 는
    연결마다 `PRAGMA foreign_keys = ON` 이 켜져 있을 때만 동작한다.
    """
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    # 지우기 전에 실제로 쌓였는지 본다. 안 쌓였으면 아래 검사가 무의미하다.
    assert await count_rows(temp_db, "script_logs", job_id) > 0
    assert await count_rows(temp_db, "job_hosts", job_id) > 0

    resp = await delete_jobs(client, {"ids": [job_id]})
    assert resp.status == 200
    assert (await resp.json())["deleted"] == [job_id]

    assert (await client.get(f"/api/jobs/{job_id}")).status == 404
    assert await count_rows(temp_db, "script_logs", job_id) == 0
    assert await count_rows(temp_db, "job_hosts", job_id) == 0
    assert await count_rows(temp_db, "job_events", job_id) == 0


async def test_deleting_everything_leaves_the_running_job_alone(temp_db, tmp_path):
    """`전체 삭제` 는 끝난 것만 치운다.

    돌고 있는 작업의 행을 지우면 러너의 다음 로그 INSERT 가 외래키에서 죽어
    실행이 통째로 넘어간다. 그래서 건너뛰고, 몇 건을 남겼는지 돌려준다.
    """
    started = tmp_path / "started"
    c = await make_client(
        temp_db, tmp_path, script=SLOW_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        done = (await (await post(c, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]})).json())["id"]
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "가짜 hubctl 이 실제로 떠야 의미 있는 테스트다"

        running = done  # 첫 작업이 SLOW 라 아직 돌고 있다
        assert (await (await c.get(f"/api/jobs/{running}")).json())["status"] == "running"

        resp = await delete_jobs(c, {"all": True})
        assert resp.status == 200
        body = await resp.json()
        assert body["deleted"] == []
        assert body["skipped"] == [running]
        assert (await c.get(f"/api/jobs/{running}")).status == 200
    finally:
        await c.close()


async def test_a_running_job_in_the_selection_is_skipped_not_refused(temp_db, tmp_path):
    """섞여 있으면 끝난 것만 지우고 나머지는 남긴다 — 요청 전체를 거절하지 않는다."""
    started = tmp_path / "started"
    c = await make_client(
        temp_db, tmp_path, script=SLOW_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        running = (await (await post(c, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]})).json())["id"]
        queued = (await (await post(c, "/api/jobs", {"kind": "verify", "hosts": ["beta"]})).json())["id"]
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)

        body = await (await delete_jobs(c, {"ids": [running, queued]})).json()
        assert body["deleted"] == []
        assert body["skipped"] == [running, queued], "queued 도 아직 실행 전이라 남겨야 한다"
    finally:
        await c.close()


async def test_deleting_all_clears_finished_jobs(client):
    first = (await (await post(client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]})).json())["id"]
    await wait_finished(client, first)
    second = (await (await post(client, "/api/jobs", {"kind": "verify", "hosts": ["beta"]})).json())["id"]
    await wait_finished(client, second)

    body = await (await delete_jobs(client, {"all": True})).json()
    assert sorted(body["deleted"]) == sorted([first, second])
    assert (await (await client.get("/api/jobs")).json())["jobs"] == []


async def test_deleting_an_id_that_is_gone_is_not_an_error(client):
    """두 번 눌러도 같은 결과여야 한다 — 5초마다 새로 그리는 화면이라 겹칠 수 있다."""
    resp = await delete_jobs(client, {"ids": [9999]})
    assert resp.status == 200
    assert (await resp.json())["deleted"] == []


async def test_deleting_without_a_target_is_refused(client):
    for payload in ({}, {"ids": []}, {"ids": "3"}, {"all": False}):
        resp = await delete_jobs(client, payload)
        assert resp.status == 400, payload


async def test_deleting_requires_a_session(temp_db, tmp_path):
    app = create_app(
        db_path=temp_db,
        hubctl_repo=write_repo(tmp_path),
        inventory_path=tmp_path / "sites.yml",
        static_dir=tmp_path / "static",
    )
    (tmp_path / "sites.yml").write_text(SITES_YML, encoding="utf-8")
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        assert (await c.delete("/api/jobs", json={"all": True})).status == 401
    finally:
        await c.close()


# ── 도는 도중의 호스트별 상태 ───────────────────────────────────────


async def host_states(client, job_id) -> dict[str, str]:
    body = await (await client.get(f"/api/jobs/{job_id}")).json()
    return {h["host"]: h["status"] for h in body["hosts"]}


async def wait_until(check, *, timeout=10.0):
    async def poll():
        while True:
            got = await check()
            if got is not None:
                return got
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(poll(), timeout)


async def test_a_host_that_dies_shows_failed_before_the_job_ends(temp_db, tmp_path):
    """한 대가 죽으면 **그 자리에서** 실패로 보여야 한다.

    PLAY RECAP 은 맨 끝에만 온다. 그것만 보면 40분짜리 설치에서 한 대가 5분 만에
    죽었는데도 목록이 끝까지 '실행 중' 이다. 여러 대를 한 작업으로 돌릴 때
    서버마다 줄을 나눠 보여주는 의미가 여기에 있다.
    """
    started = tmp_path / "started"
    c = await make_client(
        temp_db, tmp_path, script=PARTIAL_FAIL_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        job_id = (await (await post(
            c, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
        )).json())["id"]

        async def beta_failed():
            states = await host_states(c, job_id)
            return states if states.get("beta") == "failed" else None

        states = await wait_until(beta_failed)
        assert states["alpha"] == "running", "죽지 않은 서버까지 끌고 내려가면 안 된다"

        # 작업 자체는 아직 돌고 있다 — 그게 이 테스트의 요점이다.
        job = await (await c.get(f"/api/jobs/{job_id}")).json()
        assert job["status"] == "running"
    finally:
        await c.close()


async def test_an_ignored_failure_does_not_mark_the_host(temp_db, tmp_path):
    """`ignore_errors` 로 무시한 실패까지 빨갛게 칠하면 안 된다.

    ansible 은 fatal 줄 바로 뒤에 `...ignoring` 을 낸다. 그 줄에는 **호스트
    이름이 없어서** 로그 파서만으로는 어느 서버 얘기인지 알 수 없다.
    직전에 실패로 찍은 호스트를 되돌리는 것으로 처리한다.
    """
    started = tmp_path / "started"
    c = await make_client(
        temp_db, tmp_path, script=IGNORED_FAIL_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        job_id = (await (await post(
            c, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
        )).json())["id"]
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)   # flush 가 몇 번 돌 시간

        states = await host_states(c, job_id)
        assert states["beta"] == "running", f"무시된 실패로 실패 처리됐다: {states}"
        assert states["alpha"] == "running"
    finally:
        await c.close()


async def test_the_recap_still_has_the_last_word(client):
    """도는 동안의 표시는 임시다 — 최종 판정은 여전히 PLAY RECAP 이 한다."""
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    states = await host_states(client, job_id)
    assert states == {"alpha": "succeeded", "beta": "failed"}


# ── 프로파일 스냅샷 ─────────────────────────────────────────────────


async def test_the_profile_is_recorded_when_the_job_is_created(client):
    """목록에 띄우려면 작업이 프로파일을 알아야 한다."""
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha", "beta"]}
    )).json())["id"]
    body = await (await client.get("/api/jobs")).json()
    job = next(j for j in body["jobs"] if j["id"] == job_id)
    assert {h["host"]: h["profile"] for h in job["hosts"]} == {
        "alpha": "onprem", "beta": "onprem",
    }


async def test_changing_a_servers_profile_does_not_rewrite_history(client):
    """지난 작업이 **무엇으로 설치됐는지**는 바뀌면 안 된다.

    인벤토리를 참조해서 그리면, 나중에 서버를 onprem 에서 hybrid 로 바꾸는 순간
    지난 작업 기록까지 전부 hybrid 로 보인다. 그래서 실행 시점 값을 job_hosts 에
    박아둔다.
    """
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
    )).json())["id"]
    await wait_finished(client, job_id)

    # 서버를 hybrid 로 바꾼다 (작업이 끝나야 인벤토리를 고칠 수 있다)
    mtime = (await (await client.get("/api/servers")).json())["mtime_ns"]
    resp = await client.put("/api/servers/alpha", json={
        "ansible_host": "192.0.2.10", "ansible_user": "connecteve",
        "site_name": "alpha", "profile": "hybrid-with-ai", "mtime_ns": mtime,
    }, headers={CSRF_HEADER: client.csrf})
    assert resp.status == 200, await resp.text()

    detail = await (await client.get(f"/api/jobs/{job_id}")).json()
    assert detail["hosts"][0]["profile"] == "onprem", "지난 작업 기록이 따라 바뀌었다"

    # 새 작업은 바뀐 값으로 기록된다
    new_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
    )).json())["id"]
    fresh = await (await client.get(f"/api/jobs/{new_id}")).json()
    assert fresh["hosts"][0]["profile"] == "hybrid-with-ai"


# ── 로그 유실 (종료와 flush 가 겹칠 때) ─────────────────────────────


async def test_closing_the_sink_mid_flush_keeps_every_line(temp_db, tmp_path, mocker):
    """작업이 끝나는 순간 주기 flush 가 돌고 있어도 줄이 사라지면 안 된다.

    flush 는 버퍼를 통째로 꺼낸 **뒤에** DB 를 기다린다. 그 사이에 close() 가
    루프를 취소하면 꺼내둔 배치가 함께 없어진다 — 하필 그 시점이 작업이 끝나는
    순간이라, 마지막 0.15초(대개 PLAY RECAP 과 오류 줄)가 조용히 유실된다.
    """
    service = JobService(
        db_path=temp_db,
        hubctl_repo=tmp_path,
        inventory_path=tmp_path / "sites.yml",
        queue=JobQueue(),
        broker=SseBroker(),
        log_dir=tmp_path / "joblogs",
    )
    async with connect(temp_db) as db:
        cur = await db.execute(
            "INSERT INTO jobs (kind, status, started_by) VALUES ('verify','running','web:t')"
        )
        job_id = int(cur.lastrowid)
        await db.commit()

    sink = _LogSink(service, job_id, "verify")
    await sink.start()

    # 주기 flush 를 DB 문 앞에 세운다 — 배치는 이미 버퍼에서 빠져나온 뒤다.
    entered, release = asyncio.Event(), asyncio.Event()
    real_connect = jobs_module.connect

    @asynccontextmanager
    async def blocked_connect(path):
        entered.set()
        await release.wait()
        async with real_connect(path) as db:
            yield db

    mocker.patch.object(jobs_module, "connect", blocked_connect)
    for text in ("PLAY RECAP ****", "alpha : ok=2 failed=0", "beta : ok=1 failed=1"):
        sink.feed("stdout", ParsedLine(text=text, kind=LineKind.RECAP, host=None, step="verify"))

    await asyncio.wait_for(entered.wait(), 5.0)
    closing = asyncio.create_task(sink.close())
    await asyncio.sleep(0.05)   # close() 가 루프를 세울 틈을 준다
    release.set()
    await asyncio.wait_for(closing, 5.0)

    async with real_connect(temp_db) as db:
        async with db.execute(
            "SELECT line FROM script_logs WHERE job_id=? ORDER BY id", (job_id,)
        ) as cur:
            saved = [r["line"] for r in await cur.fetchall()]
    assert saved == ["PLAY RECAP ****", "alpha : ok=2 failed=0", "beta : ok=1 failed=1"]


# ── 진행 단계 (도는 동안 갱신) ──────────────────────────────────────


async def test_current_step_follows_the_log_while_the_job_runs(temp_db, tmp_path):
    """단계가 로그를 따라 움직여야 한다.

    예전에는 시작할 때 한 번 `install` 로 적고 끝이었다. `install` 은 단계 키가
    아니라 화면이 단계 목록에서 못 찾았고, 50분짜리 설치가 끝날 때까지 진행
    표시가 한 칸도 안 움직였다.
    """
    started = tmp_path / "started"
    client = await make_client(
        temp_db, tmp_path, script=STEPPING_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        job_id = (await (await post(
            client, "/api/jobs", {"kind": "install", "hosts": ["alpha"], "env": "dev"}
        )).json())["id"]

        async def wait_step(want):
            while True:
                body = await (await client.get(f"/api/jobs/{job_id}")).json()
                if body["current_step"] == want:
                    return body
                await asyncio.sleep(0.05)

        body = await asyncio.wait_for(wait_step("gitops"), 15.0)
        assert body["status"] == "running", "아직 도는 중이어야 의미가 있다"
    finally:
        await post(client, f"/api/jobs/{job_id}/cancel")
        await wait_finished(client, job_id)
        await client.close()


async def test_a_job_kind_is_never_written_as_a_step(temp_db, tmp_path):
    """시작 직후에는 단계가 비어 있어야 한다 — `install` 을 적으면 안 된다."""
    started = tmp_path / "started"
    client = await make_client(
        temp_db, tmp_path, script=SLOW_HUBCTL, env={"FAKE_STARTED": str(started)}
    )
    try:
        job_id = (await (await post(
            client, "/api/jobs", {"kind": "install", "hosts": ["alpha"], "env": "dev"}
        )).json())["id"]
        while not started.exists():
            await asyncio.sleep(0.05)
        body = await (await client.get(f"/api/jobs/{job_id}")).json()
        assert body["current_step"] != "install"
    finally:
        await post(client, f"/api/jobs/{job_id}/cancel")
        await wait_finished(client, job_id)
        await client.close()


# ── 시작 전 hub-provisioning 최신화 ─────────────────────────────────


async def test_install_updates_hub_provisioning_first(client):
    """설치는 컨트롤러 저장소를 main 최신으로 맞춘 뒤에 시작한다.

    마지막 줄에 어떤 커밋으로 깔았는지가 남아야 한다 — 나중에 "그때 뭐가
    깔렸나" 를 로그만 보고 답할 수 있어야 한다.
    """
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "install", "hosts": ["alpha"], "env": "dev"}
    )).json())["id"]
    await wait_finished(client, job_id)

    text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
    assert "hub-provisioning 을 main 최신으로 맞추는 중" in text
    assert "hub-provisioning main @" in text, "설치에 쓰인 커밋이 로그에 남아야 한다"
    # 최신화가 먼저다 — 그러고 나서 hubctl 이 돈다.
    assert text.index("hub-provisioning main @") < text.index("ARGS:")


async def test_install_stops_when_the_repo_cannot_be_updated(temp_db, tmp_path):
    """최신화에 실패하면 **타겟을 손대기 전에** 멈춘다.

    조용히 옛 버전으로 깔면 "최신으로 유지" 라는 목적 자체가 없어진다.
    반쯤 된 상태가 안 남으므로 원인을 고치고 다시 실행하면 된다.
    """
    client = await make_client(temp_db, tmp_path)
    try:
        break_origin(tmp_path / "hub-provisioning")
        job_id = (await (await post(
            client, "/api/jobs", {"kind": "install", "hosts": ["alpha"], "env": "dev"}
        )).json())["id"]
        job = await wait_finished(client, job_id)

        assert job["status"] == "failed"
        assert "최신화" in (job["error_message"] or "")
        text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
        assert "ARGS:" not in text, "hubctl 이 돌면 안 된다 — 타겟을 건드리기 전에 멈춰야 한다"
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["verify", "rollback"])
async def test_recovery_kinds_run_even_when_the_repo_is_stuck(temp_db, tmp_path, kind):
    """점검·복구는 Bitbucket 이 죽었을 때 **오히려 더 필요하다.**

    최신화 실패로 이것까지 막으면 정작 손쓸 방법이 없어진다.
    """
    client = await make_client(temp_db, tmp_path)
    try:
        break_origin(tmp_path / "hub-provisioning")
        job_id = (await (await post(
            client, "/api/jobs", {"kind": kind, "hosts": ["alpha"]}
        )).json())["id"]
        job = await wait_finished(client, job_id)

        assert job["status"] == "succeeded"
        text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
        assert "최신으로 맞추는 중" not in text
    finally:
        await client.close()


async def test_patch_apply_does_not_update_the_repo(temp_db, tmp_path):
    """번들은 생성 시점의 playbook 으로 만들어졌다.

    적용 직전에 저장소를 바꾸면 번들과 playbook 의 앞뒤가 어긋난다.
    """
    client = await make_client(temp_db, tmp_path)
    try:
        break_origin(tmp_path / "hub-provisioning")
        job_id = await seed_awaiting_patch(temp_db)
        assert (await post(client, f"/api/jobs/{job_id}/approve")).status == 200
        job = await wait_finished(client, job_id)

        assert job["status"] == "succeeded"
        text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
        assert "ARGS: patch apply -l alpha" in text
        assert "최신으로 맞추는 중" not in text
    finally:
        await client.close()


# ── 1.1.1.0 전환 (configure --only) ─────────────────────────────────
async def test_configure_with_only_reaches_hubctl(client):
    """패치 화면이 configure 로 보낼 때. 세 단계만 돌아야 한다."""
    resp = await post(client, "/api/jobs", {
        "kind": "configure", "hosts": ["alpha"], "env": "prod",
        "ref": "release/1.1.1.0",
        "only": "gitops_publish,secrets_fetch_env_file,flux_wire",
    })
    assert resp.status == 201, await resp.text()
    job_id = (await resp.json())["id"]
    await wait_finished(client, job_id)

    text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
    assert "--only gitops_publish,secrets_fetch_env_file,flux_wire" in text
    assert "-e prod" in text and "hub_deploy_ref=release/1.1.1.0" in text


async def test_the_only_tags_are_kept_on_the_job(client):
    """재시도가 같은 명령을 다시 만들려면 기록이 남아 있어야 한다."""
    job_id = (await (await post(client, "/api/jobs", {
        "kind": "configure", "hosts": ["alpha"], "env": "prod",
        "ref": "release/1.1.1.0", "only": "flux_wire",
    })).json())["id"]

    job = await (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["only_tags"] == "flux_wire"


async def test_an_unknown_only_tag_never_starts(client):
    """허용 목록 밖의 단계는 작업이 만들어지기 전에 막힌다."""
    resp = await post(client, "/api/jobs", {
        "kind": "configure", "hosts": ["alpha"], "env": "prod", "only": "k0s",
    })
    assert resp.status == 400
    assert "허용되지 않은" in (await resp.json())["error"]


async def test_patch_does_not_take_only(client):
    resp = await post(client, "/api/jobs", {
        "kind": "patch", "hosts": ["alpha"], "ref": "v1", "only": "flux_wire",
    })
    assert resp.status == 400


# ── hub-provisioning 브랜치 ─────────────────────────────────────────
async def test_the_sync_branch_defaults_to_main(client):
    job_id = (await (await post(
        client, "/api/jobs", {"kind": "verify", "hosts": ["alpha"]}
    )).json())["id"]
    job = await (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["sync_branch"] == "main"


async def test_a_job_can_follow_another_branch(client):
    """아직 main 에 없는 기능을 시험할 때 (configure --only 는 dev 에만 있다)."""
    job_id = (await (await post(client, "/api/jobs", {
        "kind": "install", "hosts": ["alpha"], "env": "dev", "sync_branch": "dev",
    })).json())["id"]
    await wait_finished(client, job_id)

    job = await (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["sync_branch"] == "dev"

    text = await (await client.get(f"/api/jobs/{job_id}/log")).text()
    assert "dev 최신으로 맞추는 중" in text
    assert "git checkout dev" in text or "Switched to branch 'dev'" in text or "'dev'" in text


async def test_a_bogus_sync_branch_never_starts(client):
    """`-` 로 시작하면 git 이 옵션으로 읽는다. 작업이 만들어지기 전에 막는다."""
    resp = await post(client, "/api/jobs", {
        "kind": "verify", "hosts": ["alpha"], "sync_branch": "-rf",
    })
    assert resp.status == 400
    assert "브랜치" in (await resp.json())["error"]
