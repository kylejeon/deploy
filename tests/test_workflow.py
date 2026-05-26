"""end-to-end (mocked) 워크플로 시나리오."""
from __future__ import annotations

from pathlib import Path

import pytest

from autodeploy import repository as repo
from autodeploy.config import load_deployment_types
from autodeploy.db import connect
from autodeploy.models import Job, JobStatus, Step
from autodeploy.notifier import RecordingNotifier
from autodeploy.ssh import FakeSSHClient, StreamLine
from autodeploy.workflow import (
    Workflow,
    WorkflowConfig,
    extract_urls_from_lines,
    mask_url_secrets,
)


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "deployment_types.yaml"


def _cfg() -> WorkflowConfig:
    return WorkflowConfig(
        bitbucket_user="youngwoochon",
        bitbucket_app_password="TOKEN",
        repo_host_path="bitbucket.org/connecteve-workspace/gateway-infra-next.git",
        repo_branch="dev",
        work_dir="~/gateway-infra-next",
        admin_web_url_template="http://{ip}/",
        healthcheck_poll_interval=0.001,
        healthcheck_timeout=0.5,
    )


def _job(deployment_type: str = "hybrid-with-ai", ip: str = "192.168.1.50") -> Job:
    return Job(
        id=None,
        target_ip=ip,
        deployment_type=deployment_type,
        hospital_code="HOSP01",
        started_by="U01",
        slack_channel="C01",
    )


def _make_workflow(fake: FakeSSHClient, temp_db, notifier=None) -> Workflow:
    return Workflow(
        ssh_factory=lambda host: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=notifier or RecordingNotifier(),
        cfg=_cfg(),
    )


def _enqueue_preflight_ok(fake: FakeSSHClient) -> None:
    """preflight: universe 활성화 OK + `command -v git`가 0을 반환하는 상태."""
    fake.enqueue("add-apt-repository", [])  # universe enable
    fake.enqueue("command -v git", [], exit_code=0)


def _enqueue_happy_path_hybrid(fake: FakeSSHClient) -> None:
    """hybrid-with-ai 성공 시나리오 응답 등록 (디렉토리 미존재 → clone)."""
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=1)  # missing → clone path
    fake.enqueue("git clone", [])
    fake.enqueue("git fetch --all && git checkout", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "abc123")])
    fake.enqueue("setup-site.sh", [StreamLine("stdout", "infra ok")])
    fake.enqueue("deploy-applications.sh", [StreamLine("stdout", "app ok")])
    fake.enqueue("kubectl get pods", [])


@pytest.mark.asyncio
async def test_happy_path_hybrid_with_ai(temp_db):
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    notifier = RecordingNotifier()
    wf = _make_workflow(fake, temp_db, notifier)

    result = await wf.run(_job())

    assert result.status == JobStatus.SUCCEEDED
    assert result.admin_web_url == "http://192.168.1.50/"
    assert result.current_step == Step.DONE
    assert result.script_commit_sha is None  # 객체에 직접 반영은 안 됨 (DB만)

    # DB 검증
    async with connect(temp_db) as db:
        loaded = await repo.get_job(db, result.id)
    assert loaded.status == JobStatus.SUCCEEDED
    assert loaded.script_commit_sha == "abc123"

    # 명령 순서 검증 — 인자/스크립트
    assert any("setup-site.sh" in c and "HOSP01" in c for c in fake.executed)
    app_cmd = next(c for c in fake.executed if "deploy-applications.sh" in c)
    assert "w-ai" in app_cmd  # hybrid-with-ai의 분기 인자

    # Notifier 이벤트
    names = [e[0] for e in notifier.events]
    assert names[0] == "job_started"
    assert names[-1] == "job_finished"
    assert notifier.events[-1][1]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_on_premise_uses_correct_scripts(temp_db):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)  # exists → update path
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-onpremise.sh", [])
    fake.enqueue("deploy-applications-onpremise.sh", [])
    fake.enqueue("kubectl get pods", [])

    wf = _make_workflow(fake, temp_db)
    result = await wf.run(_job(deployment_type="on-premise"))

    assert result.status == JobStatus.SUCCEEDED
    # on-premise 스크립트 호출
    assert any("setup-onpremise.sh" in c for c in fake.executed)
    assert any("deploy-applications-onpremise.sh" in c for c in fake.executed)
    # hybrid 스크립트는 호출 안 됨
    assert not any("setup-site.sh" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_hybrid_without_ai_passes_wo_ai_arg(temp_db):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])

    wf = _make_workflow(fake, temp_db)
    await wf.run(_job(deployment_type="hybrid-without-ai"))

    app_cmd = next(c for c in fake.executed if "deploy-applications.sh" in c)
    assert "wo-ai" in app_cmd


@pytest.mark.asyncio
async def test_unknown_deployment_type_fails_immediately(temp_db):
    fake = FakeSSHClient()  # 어떤 명령도 호출되면 안 됨
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(deployment_type="invalid-type"))

    assert result.status == JobStatus.FAILED
    assert "invalid-type" in result.error_message
    assert fake.executed == []  # SSH 시도조차 안 함


@pytest.mark.asyncio
async def test_ssh_connect_failure(temp_db):
    fake = FakeSSHClient(fail_connect=True)
    notifier = RecordingNotifier()
    wf = _make_workflow(fake, temp_db, notifier)

    result = await wf.run(_job())

    assert result.status == JobStatus.FAILED
    assert "simulated connect failure" in result.error_message
    # SSH connect 실패 시점에 종료 — git/script 명령은 등록 안 됐어도 실행 시도 없음
    last = notifier.events[-1]
    assert last[0] == "job_finished"
    assert last[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_git_pull_failure_stops_pipeline(temp_db):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [], exit_code=128)  # 실패
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())

    assert result.status == JobStatus.FAILED
    assert result.error_message.startswith("git update failed") or "update" in result.error_message
    # 후속 스크립트는 실행 안 됨
    assert not any("setup-site.sh" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_install_path_persists_slack_thread_ts_to_db(temp_db):
    """SlackNotifier가 부모 ts를 job.slack_thread_ts에 채우면 DB에도 저장돼야 한다.
    그래야 다음 retry가 같은 스레드 컨텍스트로 동작 (find_jobs_by_thread_ts).
    """

    class _ThreadStampingNotifier:
        async def job_started(self, job):
            job.slack_thread_ts = "1700000000.SETBYNOTIFIER"

        async def step_started(self, job, step): pass
        async def step_log(self, job, step, line): pass
        async def step_finished(self, job, step, *, success, duration_s): pass
        async def job_finished(self, job, *, error): pass

    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    wf = _make_workflow(fake, temp_db, _ThreadStampingNotifier())

    # _handle_install이 미리 DB에 만들고 workflow.run으로 넘기는 흐름 모사
    async with connect(temp_db) as db:
        job = Job(
            id=None, target_ip="1.1.1.1", deployment_type="hybrid-with-ai",
            hospital_code="HOSP01", started_by="U01", slack_channel="C01",
        )
        job.id = await repo.create_job(db, job)

    await wf.run(job)

    async with connect(temp_db) as db:
        loaded = await repo.get_job(db, job.id)
    assert loaded.slack_thread_ts == "1700000000.SETBYNOTIFIER"


@pytest.mark.asyncio
async def test_preflight_auto_installs_missing_git_and_proceeds(temp_db):
    """git 누락 → sudo apt install 자동 시도 → 재검증 통과 → workflow 계속."""
    fake = FakeSSHClient()
    fake.enqueue("add-apt-repository", [])  # universe enable
    fake.enqueue("command -v git", [], exit_code=1)  # 첫 검증: 누락
    fake.enqueue("apt-get install", [StreamLine("stdout", "Setting up git")])  # 자동 설치 성공
    fake.enqueue("command -v git", [], exit_code=0)  # 재검증: 통과
    fake.enqueue("test -d", [], exit_code=1)  # clone path
    fake.enqueue("git clone", [])
    fake.enqueue("git fetch --all && git checkout", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "abc")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())
    assert result.status == JobStatus.SUCCEEDED
    # apt install 명령이 실제로 호출됐는지
    assert any("apt-get install" in c and "git" in c for c in fake.executed)
    assert any("DEBIAN_FRONTEND=noninteractive" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_preflight_auto_install_failure_aborts_with_manual_hint(temp_db):
    """apt install 자체가 실패하면 분명한 에러로 종료. 수동 설치 안내 포함."""
    fake = FakeSSHClient()
    fake.enqueue("add-apt-repository", [])  # universe enable
    fake.enqueue("command -v git", [], exit_code=1)
    fake.enqueue("apt-get install", [StreamLine("stderr", "E: package not found")], exit_code=100)
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())
    assert result.status == JobStatus.FAILED
    assert "자동 설치 실패" in result.error_message
    assert "git" in result.error_message
    assert "sudo apt-get install" in result.error_message  # 수동 설치 힌트
    # clone은 시도하지 않음
    assert not any("git clone" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_universe_repo_enabled_before_preflight(temp_db):
    """git_pull 진입 시 universe 활성화가 가장 먼저 실행돼야 함 (apt 도구 확인보다 앞)."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    wf = _make_workflow(fake, temp_db)

    await wf.run(_job())

    # add-apt-repository universe가 command -v git보다 먼저
    universe_idx = next(i for i, c in enumerate(fake.executed) if "add-apt-repository" in c)
    git_check_idx = next(i for i, c in enumerate(fake.executed) if "command -v git" in c)
    assert universe_idx < git_check_idx

    # universe 명령에 sudo + apt-get update 포함
    universe_cmd = fake.executed[universe_idx]
    assert "add-apt-repository -y universe" in universe_cmd
    assert "apt-get update" in universe_cmd


@pytest.mark.asyncio
async def test_universe_failure_aborts_with_manual_hint(temp_db):
    """universe 활성화 자체가 실패하면 명확한 메시지로 종료."""
    fake = FakeSSHClient()
    fake.enqueue("add-apt-repository", [StreamLine("stderr", "E: cannot add repo")], exit_code=1)
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())
    assert result.status == JobStatus.FAILED
    assert "universe" in result.error_message
    assert "add-apt-repository" in result.error_message  # 수동 안내 포함
    # preflight 이후 단계로 가지 않음
    assert not any("command -v git" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_preflight_skips_install_when_tools_already_present(temp_db):
    """첫 검증부터 통과면 install_missing_tools는 호출 안 됨 (universe 활성화는 별개)."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    wf = _make_workflow(fake, temp_db)

    await wf.run(_job())
    # 도구가 이미 있으면 `command -v git`는 한 번만 (재검증 없음)
    git_check_count = sum(1 for c in fake.executed if "command -v git" in c)
    assert git_check_count == 1


@pytest.mark.asyncio
async def test_auto_install_injects_sudo_password_when_set(temp_db):
    """WorkflowConfig.sudo_password가 채워져 있으면 apt-get은 printf | sudo -S로 호출."""
    fake = FakeSSHClient()
    fake.enqueue("add-apt-repository", [])  # universe enable
    fake.enqueue("command -v git", [], exit_code=1)
    fake.enqueue("apt-get install", [])
    fake.enqueue("command -v git", [], exit_code=0)
    fake.enqueue("test -d", [], exit_code=1)
    fake.enqueue("git clone", [])
    fake.enqueue("git fetch --all && git checkout", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])

    cfg = WorkflowConfig(
        bitbucket_user="u", bitbucket_app_password="x",
        repo_host_path="bitbucket.org/x.git", repo_branch="dev",
        work_dir="~/x",
        healthcheck_poll_interval=0.001, healthcheck_timeout=0.5,
        sudo_password="myPass#$",
    )
    wf = Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=RecordingNotifier(),
        cfg=cfg,
    )
    result = await wf.run(_job())
    assert result.status == JobStatus.SUCCEEDED

    apt_cmd = next(c for c in fake.executed if "apt-get install" in c)
    assert "printf '%s\\n'" in apt_cmd
    assert "sudo -S -p ''" in apt_cmd
    assert "'myPass#$'" in apt_cmd

    # 인프라 스크립트도 같은 패턴으로 sudo -S 사용
    infra_cmd = next(c for c in fake.executed if "setup-site.sh" in c)
    assert "sudo -S -p ''" in infra_cmd
    assert "'myPass#$'" in infra_cmd


@pytest.mark.asyncio
async def test_auto_install_uses_plain_sudo_when_no_password(temp_db):
    """sudo_password가 비어있으면(NOPASSWD 가정) 기존 plain sudo 형태."""
    fake = FakeSSHClient()
    fake.enqueue("add-apt-repository", [])  # universe enable
    fake.enqueue("command -v git", [], exit_code=1)
    fake.enqueue("apt-get install", [])
    fake.enqueue("command -v git", [], exit_code=0)
    fake.enqueue("test -d", [], exit_code=1)
    fake.enqueue("git clone", [])
    fake.enqueue("git fetch --all && git checkout", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])

    wf = _make_workflow(fake, temp_db)  # default cfg: sudo_password=""
    await wf.run(_job())

    apt_cmd = next(c for c in fake.executed if "apt-get install" in c)
    assert "sudo bash" in apt_cmd or "sudo apt-get" in apt_cmd
    assert "sudo -S" not in apt_cmd
    assert "printf" not in apt_cmd


@pytest.mark.asyncio
async def test_infra_script_failure_stops_pipeline(temp_db):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [StreamLine("stderr", "boom")], exit_code=1)
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())

    assert result.status == JobStatus.FAILED
    assert "script exited with 1" in result.error_message
    # app/healthcheck 실행 안 됨
    assert not any("deploy-applications.sh" in c for c in fake.executed)
    assert not any("kubectl get pods" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_healthcheck_timeout_marks_failed(temp_db):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue(
        "kubectl get pods",
        [StreamLine("stdout", "default api Pending")],
        repeat=True,  # 폴링 동안 무한 반복
    )
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())

    assert result.status == JobStatus.FAILED
    assert "cluster not ready" in result.error_message
    assert "Pending" in result.error_message


@pytest.mark.asyncio
async def test_script_logs_persisted(temp_db):
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())

    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT step, stream, line FROM script_logs WHERE job_id=? ORDER BY id",
            (result.id,),
        ) as cur:
            rows = await cur.fetchall()

    assert len(rows) >= 2  # infra + app stdout
    steps = {r["step"] for r in rows}
    assert "infra_install" in steps
    assert "app_install" in steps


# ---------- O-6 admin web URL 추출 ----------

def test_extract_urls_frontend_temporal_pacs():
    lines = [
        "[INFO] Frontend URL: http://172.17.0.1:31489",
        "[INFO] Temporal Web URL: http://172.17.0.1:31917",
        "[INFO] Web-PACS URL (Traefik): http://172.17.0.1:30080/",
        "[INFO] Other random log line",
    ]
    urls = extract_urls_from_lines("192.168.1.50", lines)
    assert urls == {
        "Frontend": "http://192.168.1.50:31489",
        "Temporal Web": "http://192.168.1.50:31917",
        "Web-PACS": "http://192.168.1.50:30080/",
    }


def test_extract_urls_replaces_docker_bridge_with_target_ip():
    urls = extract_urls_from_lines(
        "10.0.0.1",
        ["[INFO] Frontend URL: http://172.17.0.1:31489"],
    )
    assert urls["Frontend"].startswith("http://10.0.0.1:")
    assert "172.17.0.1" not in urls["Frontend"]


def test_extract_urls_empty_when_no_match():
    urls = extract_urls_from_lines("1.2.3.4", ["nothing here", "[INFO] foo bar"])
    assert urls == {}


@pytest.mark.asyncio
async def test_workflow_captures_app_urls_and_uses_frontend_as_admin(temp_db):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue(
        "deploy-applications.sh",
        [
            StreamLine("stdout", "[INFO] Frontend URL: http://172.17.0.1:31489"),
            StreamLine("stdout", "[INFO] Temporal Web URL: http://172.17.0.1:31917"),
            StreamLine("stdout", "[INFO] Web-PACS URL (Traefik): http://172.17.0.1:30080/"),
        ],
    )
    fake.enqueue("kubectl get pods", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(ip="192.168.1.50"))

    assert result.status == JobStatus.SUCCEEDED
    assert result.admin_web_url == "http://192.168.1.50:31489"
    assert result.extra_urls == {
        "Frontend": "http://192.168.1.50:31489",
        "Temporal Web": "http://192.168.1.50:31917",
        "Web-PACS": "http://192.168.1.50:30080/",
    }


@pytest.mark.asyncio
async def test_workflow_admin_url_falls_back_to_template_when_no_frontend(temp_db):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue(
        "deploy-applications.sh",
        [StreamLine("stdout", "nothing useful")],
    )
    fake.enqueue("kubectl get pods", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(ip="10.0.0.1"))
    assert result.admin_web_url == "http://10.0.0.1/"  # template fallback


# ---------- D-3 토큰 마스킹 ----------

def test_mask_url_secrets_replaces_password():
    assert (
        mask_url_secrets("git clone https://user:ATBBzv12345@bitbucket.org/x.git")
        == "git clone https://user:***@bitbucket.org/x.git"
    )


def test_mask_url_secrets_handles_http_scheme():
    assert mask_url_secrets("http://u:secret@x") == "http://u:***@x"


def test_mask_url_secrets_passes_through_clean_urls():
    line = "remote: ok  https://bitbucket.org/foo/bar.git"
    assert mask_url_secrets(line) == line


def test_mask_url_secrets_passes_through_plain_text():
    assert mask_url_secrets("nothing to mask here") == "nothing to mask here"


@pytest.mark.asyncio
async def test_workflow_masks_token_in_script_logs(temp_db):
    """git clone stderr가 토큰 포함 URL을 echo해도 DB에는 마스킹된 형태로 저장."""
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=1)  # clone path
    # clone 명령의 stderr에 토큰 URL이 echo되는 상황 시뮬레이션
    fake.enqueue(
        "git clone",
        [StreamLine("stderr", "fatal: cloning https://youngwoochon:ATBBleak@bitbucket.org/x.git failed")],
    )
    fake.enqueue("git fetch --all && git checkout", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())
    assert result.status == JobStatus.SUCCEEDED

    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT line FROM script_logs WHERE job_id=? AND step='git_pull'",
            (result.id,),
        ) as cur:
            rows = await cur.fetchall()
    lines = [r["line"] for r in rows]
    joined = "\n".join(lines)
    assert "ATBBleak" not in joined  # 토큰 평문 없음
    assert "***@bitbucket.org" in joined  # 마스킹 형태로


@pytest.mark.asyncio
async def test_notifier_emits_step_events_for_each_phase(temp_db):
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    notifier = RecordingNotifier()
    wf = _make_workflow(fake, temp_db, notifier)

    await wf.run(_job())

    step_starts = [e[1]["step"] for e in notifier.events if e[0] == "step_started"]
    step_finishes = [e[1]["step"] for e in notifier.events if e[0] == "step_finished"]
    # ssh_connect는 step_done만 emit (start 안 함; 즉시 완료)
    assert step_starts == ["git_pull", "infra_install", "app_install", "healthcheck"]
    assert "ssh_connect" in step_finishes
    assert "healthcheck" in step_finishes
    assert all(e[1]["success"] for e in notifier.events if e[0] == "step_finished")
