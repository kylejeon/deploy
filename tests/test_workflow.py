"""end-to-end (mocked) 워크플로 시나리오."""
from __future__ import annotations

import asyncio
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
    """preflight: sleep mask + universe 활성화 OK + `command -v git`가 0을 반환."""
    fake.enqueue("systemctl mask", [])  # 절전 타겟 mask (첫 .sh 전)
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
    fake.enqueue("kubectl -n hub delete pod", [])


@pytest.mark.asyncio
async def test_happy_path_hybrid_with_ai(temp_db):
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    notifier = RecordingNotifier()
    wf = _make_workflow(fake, temp_db, notifier)

    result = await wf.run(_job())

    assert result.status == JobStatus.SUCCEEDED
    # 표준 NodePort(8000) 기준 admin_web_url 자동 구성
    assert result.admin_web_url == "http://192.168.1.50:8000/"
    assert result.current_step == Step.DONE
    assert result.script_commit_sha == "abc123"  # success_summary 표시용으로 객체에도 반영

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
    fake.enqueue("kubectl -n hub delete pod", [])

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
    fake.enqueue("kubectl -n hub delete pod", [])

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
    fake.enqueue("systemctl mask", [])  # 절전 mask
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
    fake.enqueue("kubectl -n hub delete pod", [])
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
    fake.enqueue("systemctl mask", [])  # 절전 mask
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
    """git_pull 진입 시 universe 활성화가 apt 도구 확인보다 앞에 실행돼야 함."""
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
async def test_sleep_mask_runs_before_universe(temp_db):
    """절전 타겟 mask가 universe 활성화보다 먼저 실행돼야 함 (첫 .sh 전 보장)."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    wf = _make_workflow(fake, temp_db)

    await wf.run(_job())

    sleep_idx = next(i for i, c in enumerate(fake.executed) if "systemctl mask" in c)
    universe_idx = next(i for i, c in enumerate(fake.executed) if "add-apt-repository" in c)
    assert sleep_idx < universe_idx

    sleep_cmd = fake.executed[sleep_idx]
    assert "sleep.target" in sleep_cmd
    assert "suspend.target" in sleep_cmd
    assert "hibernate.target" in sleep_cmd
    assert "hybrid-sleep.target" in sleep_cmd


@pytest.mark.asyncio
async def test_sleep_mask_failure_aborts_with_manual_hint(temp_db):
    """절전 타겟 mask 자체가 실패하면 GIT_PULL 단계 실패로 종료. universe까지 안 감."""
    fake = FakeSSHClient()
    fake.enqueue("systemctl mask", [StreamLine("stderr", "Failed to mask")], exit_code=1)
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job())
    assert result.status == JobStatus.FAILED
    assert result.current_step == Step.GIT_PULL
    assert "절전" in result.error_message or "sleep" in result.error_message.lower()
    assert "systemctl mask" in result.error_message  # 수동 안내 포함
    # universe 활성화 단계로 가지 않음
    assert not any("add-apt-repository" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_universe_failure_aborts_with_manual_hint(temp_db):
    """universe 활성화 자체가 실패하면 명확한 메시지로 종료."""
    fake = FakeSSHClient()
    fake.enqueue("systemctl mask", [])  # 절전 mask는 통과
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
    fake.enqueue("systemctl mask", [])  # 절전 mask
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
    fake.enqueue("kubectl -n hub delete pod", [])

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
    fake.enqueue("systemctl mask", [])  # 절전 mask
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
    fake.enqueue("kubectl -n hub delete pod", [])

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
    assert "exited with 1" in result.error_message
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
    fake.enqueue("kubectl -n hub delete pod", [])
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
async def test_workflow_captures_urls_from_infra_step_too(temp_db):
    """on-premise처럼 infra 스크립트가 URL을 출력하는 케이스도 잡아야 함."""
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue(
        "setup-site.sh",
        [
            StreamLine("stdout", "[INFO] Temporal Web URL: http://192.168.100.213:30851"),
            StreamLine("stdout", "[INFO] Web-PACS URL (Traefik): http://192.168.100.213:30080/"),
        ],
    )
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])
    fake.enqueue("kubectl -n hub delete pod", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(ip="192.168.100.213"))

    assert result.status == JobStatus.SUCCEEDED
    assert "Temporal Web" in result.extra_urls
    assert result.extra_urls["Temporal Web"] == "http://192.168.100.213:30851"
    assert "Web-PACS" in result.extra_urls
    assert result.extra_urls["Web-PACS"] == "http://192.168.100.213:30080/"


@pytest.mark.asyncio
async def test_workflow_captures_urls_from_stderr(temp_db):
    """일부 스크립트는 [INFO] 로그를 stderr로 출력 — 그래도 잡아야 함."""
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue(
        "deploy-applications.sh",
        [
            StreamLine("stderr", "[INFO] Frontend URL: http://192.168.100.213:31489"),
        ],
    )
    fake.enqueue("kubectl get pods", [])
    fake.enqueue("kubectl -n hub delete pod", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(ip="192.168.100.213"))
    assert result.admin_web_url == "http://192.168.100.213:31489"


@pytest.mark.asyncio
async def test_workflow_merges_urls_across_infra_and_app(temp_db):
    """infra·app 양쪽이 URL을 출력하면 모두 보관. 동일 라벨은 나중(app) 값이 우선."""
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue(
        "setup-site.sh",
        [
            StreamLine("stdout", "[INFO] Temporal Web URL: http://10.0.0.1:30851"),
            StreamLine("stdout", "[INFO] Web-PACS URL: http://10.0.0.1:30080/"),
        ],
    )
    fake.enqueue(
        "deploy-applications.sh",
        [
            # 같은 라벨이지만 다른 포트 — 새 값으로 덮어쓰기
            StreamLine("stdout", "[INFO] Web-PACS URL: http://10.0.0.1:30090/"),
            # 신규 라벨 추가
            StreamLine("stdout", "[INFO] Frontend URL: http://10.0.0.1:31489"),
        ],
    )
    fake.enqueue("kubectl get pods", [])
    fake.enqueue("kubectl -n hub delete pod", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(ip="10.0.0.1"))
    assert result.extra_urls["Temporal Web"] == "http://10.0.0.1:30851"  # infra 단계 유지
    assert result.extra_urls["Web-PACS"] == "http://10.0.0.1:30090/"      # app가 덮어씀
    assert result.extra_urls["Frontend"] == "http://10.0.0.1:31489"       # app에서 신규
    assert result.admin_web_url == "http://10.0.0.1:31489"                 # Frontend가 admin


@pytest.mark.asyncio
async def test_workflow_admin_url_falls_back_to_fixed_port_when_no_frontend(temp_db):
    """install 스크립트가 [INFO] Frontend URL을 출력 안 해도 표준 NodePort(8000)로 구성."""
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
    fake.enqueue("kubectl -n hub delete pod", [])
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(ip="10.0.0.1"))
    assert result.admin_web_url == "http://10.0.0.1:8000/"
    # 표준 3개 URL이 모두 채워져 있어야 함
    assert result.extra_urls["Frontend"] == "http://10.0.0.1:8000/"
    assert result.extra_urls["Temporal Web"] == "http://10.0.0.1:8001/"
    assert result.extra_urls["Web-PACS"] == "http://10.0.0.1:8002/"


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
    fake.enqueue("kubectl -n hub delete pod", [])
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
async def test_workflow_cancellation_marks_db_and_notifies(temp_db):
    """워크플로 도중 task.cancel() 호출 시 DB는 CANCELLED, notifier에 job_finished(cancelled)."""

    class _HangingSSHClient(FakeSSHClient):
        """특정 명령 substring을 만나면 이벤트가 set될 때까지 대기 → cancel 가능."""
        def __init__(self, hang_on: str) -> None:
            super().__init__()
            self._hang_on = hang_on
            self.entered_hang = asyncio.Event()
            self._release = asyncio.Event()

        async def exec(self, command, on_line=None):
            if self._hang_on in command:
                self.entered_hang.set()
                await self._release.wait()  # never released → cancel로만 빠져나옴
                return 0
            return await super().exec(command, on_line)

    fake = _HangingSSHClient(hang_on="setup-site.sh")
    fake.enqueue("systemctl mask", [])
    fake.enqueue("add-apt-repository", [])
    fake.enqueue("command -v git", [], exit_code=0)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])  # 도달 시 hang

    notifier = RecordingNotifier()
    wf = _make_workflow(fake, temp_db, notifier)

    task = asyncio.create_task(wf.run(_job()))
    # setup-site.sh 진입까지 대기
    await asyncio.wait_for(fake.entered_hang.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # DB가 CANCELLED로 정리됐는지
    async with connect(temp_db) as db:
        async with db.execute("SELECT id, status FROM jobs ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    assert row["status"] == "cancelled"

    # notifier에 job_finished(cancelled) 이벤트
    finish_events = [e for e in notifier.events if e[0] == "job_finished"]
    assert len(finish_events) == 1
    assert finish_events[0][1]["status"] == "cancelled"


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
    # hybrid 케이스에선 site_register 단계가 skip되므로 4단계
    # hybrid에선 site_register는 자격증명 없으면 skip. dicom_gateway_restart는 항상 실행.
    assert step_starts == [
        "git_pull", "infra_install", "app_install", "healthcheck",
        "dicom_gateway_restart",
    ]
    assert "ssh_connect" in step_finishes
    assert "healthcheck" in step_finishes
    assert "site_register" not in step_finishes  # skipped (no creds in default cfg)
    assert all(e[1]["success"] for e in notifier.events if e[0] == "step_finished")


# ---------- site_register 단계 ----------

def _cfg_with_site_creds(**overrides) -> WorkflowConfig:
    base = dict(
        bitbucket_user="u", bitbucket_app_password="x",
        repo_host_path="bitbucket.org/x.git", repo_branch="dev",
        work_dir="~/x",
        healthcheck_poll_interval=0.001, healthcheck_timeout=0.5,
        site_admin_email="admin@x.com",
        site_admin_password="pw",
    )
    base.update(overrides)
    return WorkflowConfig(**base)


def _make_wf_with_cfg(fake: FakeSSHClient, temp_db, cfg: WorkflowConfig) -> Workflow:
    return Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=RecordingNotifier(),
        cfg=cfg,
    )


def _enqueue_onpremise_happy_path(fake: FakeSSHClient, frontend_port: str = "31435") -> None:
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue(
        "setup-onpremise.sh",
        [
            StreamLine("stdout", f"[INFO] Frontend URL: http://172.17.0.1:{frontend_port}"),
        ],
    )
    fake.enqueue("deploy-applications-onpremise.sh", [])
    fake.enqueue("kubectl get pods", [])
    fake.enqueue("kubectl -n hub delete pod", [])


@pytest.mark.asyncio
async def test_site_register_runs_for_hybrid_with_cloud_base_url(temp_db, mocker):
    """hybrid는 클라우드 base URL로 자동 등록 — Frontend URL 캡쳐 불필요."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)  # Frontend URL 안 찍힘
    cfg = _cfg_with_site_creds()
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED
    spy.assert_called_once()
    args, kwargs = spy.call_args
    assert args[0] == "https://dev-gateway.connecteve.com"  # default cloud URL
    assert kwargs["host_header"] is None  # hybrid는 URL에서 자동
    assert kwargs["installation_type"] == "HYBRID_ONPREM_AI"
    assert kwargs["api_env"] == "dev"


@pytest.mark.asyncio
async def test_site_register_uses_hybrid_without_ai_installation_type(temp_db, mocker):
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])
    fake.enqueue("kubectl -n hub delete pod", [])
    cfg = _cfg_with_site_creds()
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    await wf.run(_job(deployment_type="hybrid-without-ai"))
    _, kwargs = spy.call_args
    assert kwargs["installation_type"] == "HYBRID_CLOUD_AI"


@pytest.mark.asyncio
async def test_site_register_respects_custom_cloud_base_url(temp_db, mocker):
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_site_creds(
        site_cloud_base_url="https://prod-gateway.connecteve.com",
        site_api_env="prod",
    )
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    await wf.run(_job(deployment_type="hybrid-with-ai"))
    args, kwargs = spy.call_args
    assert args[0] == "https://prod-gateway.connecteve.com"
    assert kwargs["api_env"] == "prod"


@pytest.mark.asyncio
async def test_site_register_skipped_when_credentials_empty(temp_db, mocker):
    fake = FakeSSHClient()
    _enqueue_onpremise_happy_path(fake)
    cfg = _cfg_with_site_creds(site_admin_email="", site_admin_password="")
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    spy = mocker.patch("autodeploy.workflow.site_registration.register_hospital")

    result = await wf.run(_job(deployment_type="on-premise"))
    assert result.status == JobStatus.SUCCEEDED
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_site_register_runs_for_onpremise_with_creds(temp_db, mocker):
    fake = FakeSSHClient()
    _enqueue_onpremise_happy_path(fake, frontend_port="31435")
    cfg = _cfg_with_site_creds()
    notifier = RecordingNotifier()
    wf = Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=notifier,
        cfg=cfg,
    )
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    job = _job(deployment_type="on-premise", ip="192.168.1.50")
    job.hospital_name = "은평성모병원"
    job.hospital_address = "서울"
    result = await wf.run(job)

    assert result.status == JobStatus.SUCCEEDED
    spy.assert_called_once()
    args, kwargs = spy.call_args
    # base_url은 이제 고정 port_frontend(=8000)로 구성. install 로그의 [INFO] Frontend URL과 무관.
    assert args[0] == "http://192.168.1.50:8000"
    assert kwargs["email"] == "admin@x.com"
    assert kwargs["password"] == "pw"
    assert kwargs["code"] == "HOSP01"
    assert kwargs["display_name"] == "은평성모병원"
    assert kwargs["address"] == "서울"
    assert kwargs["installation_type"] == "ON_PREMISE"
    assert kwargs["host_header"] == "localhost"

    # notifier에 site_register step 이벤트 발행됨
    step_starts = [e[1]["step"] for e in notifier.events if e[0] == "step_started"]
    assert "site_register" in step_starts


@pytest.mark.asyncio
async def test_site_register_uses_hospital_code_when_name_missing(temp_db, mocker):
    fake = FakeSSHClient()
    _enqueue_onpremise_happy_path(fake)
    cfg = _cfg_with_site_creds()
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    job = _job(deployment_type="on-premise")  # hospital_name None
    await wf.run(job)

    _, kwargs = spy.call_args
    assert kwargs["display_name"] == "HOSP01"  # code로 fallback
    assert kwargs["address"] == ""  # None → ""


@pytest.mark.asyncio
async def test_site_register_proceeds_even_without_frontend_log_line(temp_db, mocker):
    """포트가 고정(8000)이므로 install 스크립트가 [INFO] Frontend URL을 안 찍어도 단계 정상 진행."""
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha1")])
    fake.enqueue("setup-onpremise.sh", [])  # Frontend URL 안 찍힘
    fake.enqueue("deploy-applications-onpremise.sh", [])
    fake.enqueue("kubectl get pods", [])
    fake.enqueue("kubectl -n hub delete pod", [])
    cfg = _cfg_with_site_creds()
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    result = await wf.run(_job(deployment_type="on-premise", ip="10.0.0.50"))
    assert result.status == JobStatus.SUCCEEDED
    spy.assert_called_once()
    args, _ = spy.call_args
    assert args[0] == "http://10.0.0.50:8000"


@pytest.mark.asyncio
async def test_site_register_api_failure_marks_workflow_failed(temp_db, mocker):
    from autodeploy.site_registration import SiteAPIError as _SiteErr

    fake = FakeSSHClient()
    _enqueue_onpremise_happy_path(fake)
    cfg = _cfg_with_site_creds()
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        side_effect=_SiteErr("sign-in 실패 (HTTP 401): bad credentials"),
    )

    result = await wf.run(_job(deployment_type="on-premise"))
    assert result.status == JobStatus.FAILED
    assert "sign-in 실패" in result.error_message
    assert result.current_step == Step.SITE_REGISTER


@pytest.mark.asyncio
async def test_site_register_already_exists_still_succeeds(temp_db, mocker):
    """API가 already_exists를 반환해도 작업은 성공으로 마무리."""
    fake = FakeSSHClient()
    _enqueue_onpremise_happy_path(fake)
    cfg = _cfg_with_site_creds()
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("already_exists", "FAKE_TOKEN"),
    )

    result = await wf.run(_job(deployment_type="on-premise"))
    assert result.status == JobStatus.SUCCEEDED


# ---------- register_existing_job (단독 재시도) ----------

@pytest.mark.asyncio
async def test_register_existing_job_hybrid_uses_cloud_url(temp_db, mocker):
    cfg = _cfg_with_site_creds()
    wf = Workflow(
        ssh_factory=lambda h: FakeSSHClient(),
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=RecordingNotifier(),
        cfg=cfg,
    )
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    async with connect(temp_db) as db:
        job = _job(deployment_type="hybrid-with-ai")
        job.hospital_name = "부평힘찬병원"
        job.id = await repo.create_job(db, job)

    result = await wf.register_existing_job(job)
    assert result == "created"
    args, kwargs = spy.call_args
    assert args[0] == "https://dev-gateway.connecteve.com"
    assert kwargs["host_header"] is None
    assert kwargs["installation_type"] == "HYBRID_ONPREM_AI"
    assert kwargs["code"] == "HOSP01"
    assert kwargs["display_name"] == "부평힘찬병원"


@pytest.mark.asyncio
async def test_register_existing_job_onpremise_uses_fixed_port(temp_db, mocker):
    """포트가 고정(8000)이므로 DB에서 다시 로드한 Job도 추가 정보 없이 base URL 결정됨."""
    cfg = _cfg_with_site_creds()
    wf = Workflow(
        ssh_factory=lambda h: FakeSSHClient(),
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=RecordingNotifier(),
        cfg=cfg,
    )
    spy = mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    async with connect(temp_db) as db:
        job = _job(deployment_type="on-premise", ip="192.168.1.50")
        job.id = await repo.create_job(db, job)
        await repo.finish_job(db, job.id, JobStatus.SUCCEEDED)

    result = await wf.register_existing_job(job)
    assert result == "created"
    args, kwargs = spy.call_args
    assert args[0] == "http://192.168.1.50:8000"
    assert kwargs["host_header"] == "localhost"


@pytest.mark.asyncio
async def test_register_existing_job_raises_when_creds_missing(temp_db):
    from autodeploy.site_registration import SiteAPIError as _SiteErr

    cfg = _cfg_with_site_creds(site_admin_email="", site_admin_password="")
    wf = Workflow(
        ssh_factory=lambda h: FakeSSHClient(),
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=RecordingNotifier(),
        cfg=cfg,
    )
    job = _job(deployment_type="hybrid-with-ai")
    job.id = 42

    with pytest.raises(_SiteErr):
        await wf.register_existing_job(job)


@pytest.mark.asyncio
async def test_register_existing_job_records_db_event(temp_db, mocker):
    cfg = _cfg_with_site_creds()
    wf = Workflow(
        ssh_factory=lambda h: FakeSSHClient(),
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=RecordingNotifier(),
        cfg=cfg,
    )
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("already_exists", "FAKE_TOKEN"),
    )

    async with connect(temp_db) as db:
        job = _job(deployment_type="hybrid-with-ai")
        job.id = await repo.create_job(db, job)
        await repo.finish_job(db, job.id, JobStatus.SUCCEEDED)

    await wf.register_existing_job(job)

    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT step, level, message FROM job_events WHERE job_id=? AND step='site_register'",
            (job.id,),
        ) as cur:
            rows = await cur.fetchall()
    assert any("수동 재시도" in r["message"] and "이미 등록됨" in r["message"] for r in rows)


@pytest.mark.asyncio
async def test_dicom_gateway_restart_runs_for_all_deployment_types(temp_db):
    """on-premise / hybrid-with-ai / hybrid-without-ai 모두 마지막 단계로 실행."""
    for dtype in ("on-premise", "hybrid-with-ai", "hybrid-without-ai"):
        fake = FakeSSHClient()
        _enqueue_preflight_ok(fake)
        fake.enqueue("test -d", [], exit_code=0)
        fake.enqueue("git remote set-url origin", [])
        fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha")])
        if dtype == "on-premise":
            fake.enqueue("setup-onpremise.sh", [])
            fake.enqueue("deploy-applications-onpremise.sh", [])
        else:
            fake.enqueue("setup-site.sh", [])
            fake.enqueue("deploy-applications.sh", [])
        fake.enqueue("kubectl get pods", [])
        fake.enqueue("kubectl -n hub delete pod", [])
        wf = _make_workflow(fake, temp_db)

        result = await wf.run(_job(deployment_type=dtype, ip=f"10.0.0.{hash(dtype) % 100}"))
        assert result.status == JobStatus.SUCCEEDED, f"{dtype} failed"
        # 마지막 명령이 kubectl delete pod (dicom-gateway 재시작)
        delete_cmds = [c for c in fake.executed if "kubectl -n hub delete pod" in c]
        assert len(delete_cmds) == 1
        assert "app=dicom-gateway" in delete_cmds[0]


@pytest.mark.asyncio
async def test_dicom_gateway_restart_failure_does_not_fail_workflow(temp_db):
    """delete pod가 non-zero exit해도 작업 전체는 SUCCEEDED — 운영자가 수동 처리."""
    fake = FakeSSHClient()
    _enqueue_preflight_ok(fake)
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "sha")])
    fake.enqueue("setup-site.sh", [])
    fake.enqueue("deploy-applications.sh", [])
    fake.enqueue("kubectl get pods", [])
    fake.enqueue(
        "kubectl -n hub delete pod",
        [StreamLine("stderr", "Error from server (Forbidden)")],
        exit_code=1,
    )
    wf = _make_workflow(fake, temp_db)

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED  # 단계 실패해도 작업은 성공
    # DB 이벤트에 경고 흔적
    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT level, message FROM job_events "
            "WHERE job_id=? AND step='dicom_gateway_restart' AND level='warn'",
            (result.id,),
        ) as cur:
            warns = await cur.fetchall()
    assert len(warns) == 1
    assert "kubectl delete pod 실패" in warns[0]["message"]
    assert "수동 재시작" in warns[0]["message"]


@pytest.mark.asyncio
async def test_dicom_gateway_restart_runs_after_site_register(temp_db, mocker):
    """순서: healthcheck → site_register → dicom_gateway_restart."""
    fake = FakeSSHClient()
    _enqueue_onpremise_happy_path(fake)
    fake.enqueue("kubectl -n hub delete pod", [])  # _enqueue_onpremise_happy_path엔 없음
    cfg = _cfg_with_site_creds()
    notifier = RecordingNotifier()
    wf = Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=notifier,
        cfg=cfg,
    )
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    await wf.run(_job(deployment_type="on-premise"))

    step_order = [e[1]["step"] for e in notifier.events if e[0] == "step_started"]
    # site_register가 dicom_gateway_restart보다 먼저
    assert step_order.index("site_register") < step_order.index("dicom_gateway_restart")


# ---------- 단계 8: product_register ----------

def _cfg_with_jira(**overrides) -> WorkflowConfig:
    base = dict(
        bitbucket_user="u", bitbucket_app_password="x",
        repo_host_path="bitbucket.org/x.git", repo_branch="dev",
        work_dir="~/x",
        healthcheck_poll_interval=0.001, healthcheck_timeout=0.5,
        site_admin_email="admin@x.com",
        site_admin_password="pw",
        jira_email="jira@x.com",
        jira_api_token="JIRA_TOKEN",
        jira_key="PMFM",
    )
    base.update(overrides)
    return WorkflowConfig(**base)


def _enqueue_full_happy_path(fake: FakeSSHClient) -> None:
    """8단계 전체 성공 시나리오 (hybrid-with-ai, site_register + product_register 포함)."""
    _enqueue_happy_path_hybrid(fake)


@pytest.mark.asyncio
async def test_product_register_skipped_when_jira_email_missing(temp_db, mocker):
    """T-WF-8-1: JIRA_EMAIL 없음 → product_register step_started 이벤트 없음, job SUCCEEDED."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_jira(jira_email="")
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED

    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT * FROM job_events WHERE job_id=? AND step='product_register'",
            (result.id,),
        ) as cur:
            rows = await cur.fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_product_register_skipped_when_site_admin_creds_missing(temp_db, mocker):
    """T-WF-8-2: site_admin 자격증명 없음 → skip, job SUCCEEDED."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_jira(site_admin_email="", site_admin_password="")
    wf = _make_wf_with_cfg(fake, temp_db, cfg)

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED

    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT * FROM job_events WHERE job_id=? AND step='product_register'",
            (result.id,),
        ) as cur:
            rows = await cur.fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_product_register_success_two_issues(temp_db, mocker):
    """T-WF-8-3: 2개 이슈 → 2개 등록 성공 → step success."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_jira()
    notifier = RecordingNotifier()
    wf = Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=notifier,
        cfg=cfg,
    )
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )

    # Jira + ProductRegistrationClient mock
    mock_issues = [
        {"fields": {"summary": "[병원] CONNEVO KOA 1.0", "description": None}},
        {"fields": {"summary": "[병원] CONNEVO Spin 2.0", "description": None}},
    ]
    mocker.patch(
        "autodeploy.workflow.JiraClient.search_issues",
        return_value=mock_issues,
    )
    mocker.patch(
        "autodeploy.workflow.ProductRegistrationClient.register_products",
        return_value=(2, 0),
    )

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED

    step_events = [e[1]["step"] for e in notifier.events if e[0] == "step_finished"]
    assert "product_register" in step_events
    finished = next(
        e for e in notifier.events
        if e[0] == "step_finished" and e[1]["step"] == "product_register"
    )
    assert finished[1]["success"] is True


@pytest.mark.asyncio
async def test_product_register_partial_failure_step_success(temp_db, mocker):
    """T-WF-8-4: 이슈 2개 중 1개 POST 실패 → step success (1개는 성공했으므로)."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_jira()
    wf = _make_wf_with_cfg(fake, temp_db, cfg)
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )
    mocker.patch(
        "autodeploy.workflow.JiraClient.search_issues",
        return_value=[{"fields": {"summary": "[병원] CONNEVO KOA 1.0", "description": None}}],
    )
    mocker.patch(
        "autodeploy.workflow.ProductRegistrationClient.register_products",
        return_value=(1, 1),
    )

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_product_register_all_fail_step_failure_job_succeeded(temp_db, mocker):
    """T-WF-8-5: 이슈 2개 전부 실패 → step failure, job SUCCEEDED."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_jira()
    notifier = RecordingNotifier()
    wf = Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=notifier,
        cfg=cfg,
    )
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )
    mocker.patch(
        "autodeploy.workflow.JiraClient.search_issues",
        return_value=[{"fields": {"summary": "[병원] CONNEVO KOA 1.0", "description": None}}],
    )
    mocker.patch(
        "autodeploy.workflow.ProductRegistrationClient.register_products",
        return_value=(0, 2),
    )

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED

    finished = next(
        e for e in notifier.events
        if e[0] == "step_finished" and e[1]["step"] == "product_register"
    )
    assert finished[1]["success"] is False


@pytest.mark.asyncio
async def test_product_register_jira_zero_issues_step_failure_job_succeeded(temp_db, mocker):
    """T-WF-8-6: Jira 검색 0건 → step failure, job SUCCEEDED."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_jira()
    notifier = RecordingNotifier()
    wf = Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=notifier,
        cfg=cfg,
    )
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )
    mocker.patch(
        "autodeploy.workflow.JiraClient.search_issues",
        return_value=[],
    )

    result = await wf.run(_job(deployment_type="hybrid-with-ai"))
    assert result.status == JobStatus.SUCCEEDED

    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT level, message FROM job_events "
            "WHERE job_id=? AND step='product_register'",
            (result.id,),
        ) as cur:
            rows = await cur.fetchall()
    assert any("찾지 못함" in r["message"] or "0건" in r["message"] for r in rows)


@pytest.mark.asyncio
async def test_product_register_runs_after_dicom_gateway_restart(temp_db, mocker):
    """T-WF-8-7: dicom_gateway_restart 다음에 product_register가 실행됨을 순서로 검증."""
    fake = FakeSSHClient()
    _enqueue_happy_path_hybrid(fake)
    cfg = _cfg_with_jira()
    notifier = RecordingNotifier()
    wf = Workflow(
        ssh_factory=lambda h: fake,
        db_path=temp_db,
        deployment_types=load_deployment_types(CONFIG_PATH),
        notifier=notifier,
        cfg=cfg,
    )
    mocker.patch(
        "autodeploy.workflow.site_registration.register_hospital",
        return_value=("created", "FAKE_TOKEN"),
    )
    mocker.patch(
        "autodeploy.workflow.JiraClient.search_issues",
        return_value=[],  # 0건 → step failure (순서 검증만 목적)
    )

    await wf.run(_job(deployment_type="hybrid-with-ai"))

    step_starts = [e[1]["step"] for e in notifier.events if e[0] == "step_started"]
    assert "dicom_gateway_restart" in step_starts
    assert "product_register" in step_starts
    dicom_idx = step_starts.index("dicom_gateway_restart")
    product_idx = step_starts.index("product_register")
    assert dicom_idx < product_idx
