from __future__ import annotations

from pathlib import Path

import pytest

from autodeploy import repository as repo
from autodeploy.config import load_deployment_types
from autodeploy.db import connect
from autodeploy.models import JobStatus
from autodeploy.settings import Settings
from autodeploy.slack_app import CommandContext, handle_command


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "deployment_types.yaml"
TYPES = load_deployment_types(CONFIG_PATH)


def _settings(db_path: Path, *, allowed: set[str] | None = None, channel: str = "C01") -> Settings:
    return Settings(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        slack_channel_id=channel,
        allowed_users=frozenset(allowed) if allowed is not None else frozenset(),
        ssh_user="connecteve",
        ssh_password="pw",
        bitbucket_user="u",
        bitbucket_app_password="atbb",
        db_path=db_path,
        config_path=Path("config/deployment_types.yaml"),
        repo_host_path="x",
        repo_branch="dev",
        work_dir="~/x",
        log_level="INFO",
        site_admin_email="",
        site_admin_password="",
        site_cloud_base_url="https://dev-gateway.connecteve.com",
        site_api_env="dev",
        jira_base_url="https://connecteve.atlassian.net",
        jira_email="",
        jira_api_token="",
        jira_key="PMFM",
        port_frontend=8000,
        port_temporal=8001,
        port_webpacs=8002,
    )


# ---------- 권한·채널 ----------

@pytest.mark.asyncio
async def test_permission_denied_when_user_not_allowed(temp_db):
    s = _settings(temp_db, allowed={"U01"})
    ctx = CommandContext(user_id="UNKNOWN", channel_id="C01", text="help")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is None
    assert "권한이 없습니다" in result.response["text"]


@pytest.mark.asyncio
async def test_no_allowed_users_means_open_access(temp_db):
    # 빈 allowed_users 셋 → 권한 체크 비활성 (개발 모드)
    s = _settings(temp_db, allowed=set())
    ctx = CommandContext(user_id="ANY", channel_id="C01", text="help")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "AutoDeploy 명령어" in result.response["text"]


@pytest.mark.asyncio
async def test_wrong_channel_rejected(temp_db):
    s = _settings(temp_db, allowed={"U01"}, channel="C_BOT")
    ctx = CommandContext(user_id="U01", channel_id="C_OTHER", text="help")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "이 채널에서는" in result.response["text"]


# ---------- help / list / status ----------

@pytest.mark.asyncio
async def test_help_returns_help_message(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="help")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    body = str(result.response["blocks"])
    assert "install" in body
    assert "hybrid-with-ai" in body


@pytest.mark.asyncio
async def test_list_returns_recent_jobs(temp_db):
    s = _settings(temp_db)
    # seed
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        await repo.create_job(db, Job(
            id=None, target_ip="1.1.1.1", deployment_type="on-premise",
            hospital_code="H1", started_by="U01", slack_channel="C01",
        ))

    ctx = CommandContext(user_id="U01", channel_id="C01", text="list 5")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "1.1.1.1" in str(result.response["blocks"])


@pytest.mark.asyncio
async def test_status_without_id_returns_only_active(temp_db):
    """status 무인자는 진행 중(running/queued) 작업만 표시. 종료된 작업은 무시."""
    s = _settings(temp_db)
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        # 종료된 작업 (가장 최근이지만 status는 cancelled)
        finished_id = await repo.create_job(db, Job(
            id=None, target_ip="2.2.2.2", deployment_type="hybrid-with-ai",
            hospital_code="H1", started_by="U01", slack_channel="C01",
        ))
        await repo.finish_job(db, finished_id, JobStatus.CANCELLED)
        # 진행 중 작업 (더 오래된 id지만 running 상태)
        active_id = await repo.create_job(db, Job(
            id=None, target_ip="3.3.3.3", deployment_type="on-premise",
            hospital_code="H2", started_by="U01", slack_channel="C01",
        ))
        await repo.mark_running(db, active_id)

    ctx = CommandContext(user_id="U01", channel_id="C01", text="status")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    body = str(result.response["blocks"])
    # active 한 작업이 잡혀야 함
    assert "3.3.3.3" in body
    # 종료된 cancelled 작업은 status에 안 나타남
    assert "2.2.2.2" not in body


@pytest.mark.asyncio
async def test_status_without_id_when_only_finished_jobs_exist(temp_db):
    """진행 중 작업이 0건이면 (모두 종료됐어도) '진행 중 없음' 메시지."""
    s = _settings(temp_db)
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        job_id = await repo.create_job(db, Job(
            id=None, target_ip="9.9.9.9", deployment_type="on-premise",
            hospital_code="H", started_by="U01", slack_channel="C01",
        ))
        await repo.finish_job(db, job_id, JobStatus.SUCCEEDED)

    ctx = CommandContext(user_id="U01", channel_id="C01", text="status")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "진행 중인 작업이 없습니다" in result.response["text"]


@pytest.mark.asyncio
async def test_status_with_explicit_id_shows_finished_jobs_too(temp_db):
    """`status <id>`는 종료된 작업도 그대로 표시 (사용자가 콕 짚어서 물어본 것)."""
    s = _settings(temp_db)
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        job_id = await repo.create_job(db, Job(
            id=None, target_ip="4.4.4.4", deployment_type="on-premise",
            hospital_code="H", started_by="U01", slack_channel="C01",
        ))
        await repo.finish_job(db, job_id, JobStatus.FAILED, error_message="boom")

    ctx = CommandContext(user_id="U01", channel_id="C01", text=f"status {job_id}")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    body = str(result.response["blocks"])
    assert "4.4.4.4" in body  # 종료된 작업이지만 표시됨
    assert "failed" in body


@pytest.mark.asyncio
async def test_status_when_no_job_exists(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="status")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "진행 중인 작업이 없습니다" in result.response["text"]


# ---------- install ----------

@pytest.mark.asyncio
async def test_install_creates_job_and_returns_workflow_job(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(
        user_id="U01",
        channel_id="C01",
        text="install 192.168.1.50 --type=hybrid-with-ai --code=HOSP01",
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)

    assert result.response is None  # install 자체엔 ephemeral 응답 없음 (workflow가 parent로 메시지)
    assert result.workflow_job is not None
    job = result.workflow_job
    assert job.id is not None
    assert job.target_ip == "192.168.1.50"
    assert job.target_port == 22  # 기본 포트
    assert job.deployment_type == "hybrid-with-ai"
    assert job.hospital_code == "HOSP01"
    assert job.started_by == "U01"

    # DB에 기록 — target_port도 라운드트립
    async with connect(temp_db) as db:
        loaded = await repo.get_job(db, job.id)
    assert loaded is not None
    assert loaded.status == JobStatus.QUEUED
    assert loaded.target_port == 22


@pytest.mark.asyncio
async def test_install_with_host_port_persists_port(temp_db):
    """IP:PORT 형식으로 install하면 target_port가 DB까지 라운드트립."""
    s = _settings(temp_db)
    ctx = CommandContext(
        user_id="U01",
        channel_id="C01",
        text="install 110.15.83.84:22022 --type=on-premise --code=H",
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is not None
    job = result.workflow_job
    assert job.target_ip == "110.15.83.84"
    assert job.target_port == 22022

    async with connect(temp_db) as db:
        loaded = await repo.get_job(db, job.id)
    assert loaded.target_port == 22022


@pytest.mark.asyncio
async def test_install_rejects_duplicate_active_ip(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(
        user_id="U01", channel_id="C01",
        text="install 10.0.0.5 --type=on-premise --code=HOSP01",
    )
    first = await handle_command(ctx, settings=s, deployment_types=TYPES)
    # 진행 중으로 표시
    async with connect(temp_db) as db:
        await repo.mark_running(db, first.workflow_job.id)

    # 같은 IP 재시도
    second = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert second.workflow_job is None
    assert "진행 중" in second.response["text"]
    assert str(first.workflow_job.id) in second.response["text"]


@pytest.mark.asyncio
async def test_install_allows_same_ip_after_previous_finished(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(
        user_id="U01", channel_id="C01",
        text="install 10.0.0.6 --type=on-premise --code=HOSP01",
    )
    first = await handle_command(ctx, settings=s, deployment_types=TYPES)
    async with connect(temp_db) as db:
        await repo.finish_job(db, first.workflow_job.id, JobStatus.SUCCEEDED)

    second = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert second.workflow_job is not None
    assert second.workflow_job.id != first.workflow_job.id


@pytest.mark.asyncio
async def test_install_with_quoted_hospital_name(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(
        user_id="U01", channel_id="C01",
        text='install 10.0.0.7 --type=on-premise --code=HOSP01 --name="서울대병원"',
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job.hospital_name == "서울대병원"


@pytest.mark.asyncio
async def test_install_missing_type_returns_validation_error(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(
        user_id="U01", channel_id="C01",
        text="install 10.0.0.8 --code=HOSP01",
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is None
    assert "--type" in result.response["text"]


# ---------- cancel ----------

@pytest.mark.asyncio
async def test_cancel_running_job_returns_cancel_job_id(temp_db):
    """진행 중 작업에 cancel → CommandResult.cancel_job_id 세팅 + ephemeral ack."""
    s = _settings(temp_db)
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        job_id = await repo.create_job(db, Job(
            id=None, target_ip="10.0.0.10", deployment_type="on-premise",
            hospital_code="H1", started_by="U01", slack_channel="C01",
        ))
        await repo.mark_running(db, job_id)

    ctx = CommandContext(user_id="U01", channel_id="C01", text=f"cancel {job_id}")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)

    assert result.cancel_job_id == job_id
    assert result.workflow_job is None
    assert str(job_id) in result.response["text"]
    assert "취소" in result.response["text"]


@pytest.mark.asyncio
async def test_cancel_unknown_job_returns_error(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="cancel 99999")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)

    assert result.cancel_job_id is None
    assert "99999" in result.response["text"]


@pytest.mark.asyncio
async def test_cancel_already_finished_job_returns_error(temp_db):
    s = _settings(temp_db)
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        job_id = await repo.create_job(db, Job(
            id=None, target_ip="10.0.0.11", deployment_type="on-premise",
            hospital_code="H1", started_by="U01", slack_channel="C01",
        ))
        await repo.finish_job(db, job_id, JobStatus.SUCCEEDED)

    ctx = CommandContext(user_id="U01", channel_id="C01", text=f"cancel {job_id}")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)

    assert result.cancel_job_id is None
    assert "이미 종료" in result.response["text"]
    assert "succeeded" in result.response["text"]


# ---------- register ----------

@pytest.mark.asyncio
async def test_register_loads_job_and_sets_register_job(temp_db):
    """register N → DB에서 job 로드 후 register_job 필드에 담아 ack."""
    s = _settings(temp_db)
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        job_id = await repo.create_job(db, Job(
            id=None, target_ip="10.0.0.5", deployment_type="hybrid-with-ai",
            hospital_code="hch-bp", hospital_name="부평힘찬병원",
            started_by="U01", slack_channel="C01",
        ))
        await repo.finish_job(db, job_id, JobStatus.SUCCEEDED)

    ctx = CommandContext(user_id="U01", channel_id="C01", text=f"register {job_id}")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)

    assert result.register_job is not None
    assert result.register_job.id == job_id
    assert result.register_job.hospital_code == "hch-bp"
    assert str(job_id) in result.response["text"]
    assert result.workflow_job is None
    assert result.cancel_job_id is None


@pytest.mark.asyncio
async def test_register_unknown_job_returns_error(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="register 99999")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)

    assert result.register_job is None
    assert "99999" in result.response["text"]


# ---------- unknown ----------


@pytest.mark.asyncio
async def test_unknown_command_returns_parse_error(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="destroy 1.1.1.1")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "destroy" in result.response["text"]


# ---------- 멘션 토큰 제거 ----------

@pytest.mark.asyncio
async def test_strips_bot_mention_token_from_text(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="<@UBOTXYZ123> help")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "AutoDeploy 명령어" in result.response["text"]


# ---------- retry ----------

async def _seed_failed_job(db_path, *, thread_ts="1700000000.000001") -> int:
    """실패 상태의 시드 작업 생성 (재시도 원본용)."""
    from autodeploy.models import Job
    async with connect(db_path) as db:
        job = Job(
            id=None,
            target_ip="10.0.0.99",
            deployment_type="hybrid-with-ai",
            hospital_code="HOSP01",
            hospital_name="테스트병원",
            hospital_address="서울시",
            started_by="U01",
            slack_channel="C01",
            slack_thread_ts=thread_ts,
        )
        job_id = await repo.create_job(db, job)
        await repo.finish_job(db, job_id, JobStatus.FAILED, error_message="boom")
    return job_id


@pytest.mark.asyncio
async def test_retry_by_thread_context_creates_new_job_with_same_params(temp_db):
    s = _settings(temp_db)
    original_id = await _seed_failed_job(temp_db, thread_ts="1700000000.111111")

    ctx = CommandContext(
        user_id="U01", channel_id="C01", text="retry",
        thread_ts="1700000000.111111",
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)

    assert result.response is None
    job = result.workflow_job
    assert job is not None
    assert job.id != original_id
    assert job.target_ip == "10.0.0.99"
    assert job.deployment_type == "hybrid-with-ai"
    assert job.hospital_code == "HOSP01"
    assert job.hospital_name == "테스트병원"
    assert job.slack_thread_ts == "1700000000.111111"  # 같은 스레드 재사용
    assert job.retry_of == original_id


@pytest.mark.asyncio
async def test_retry_by_explicit_id(temp_db):
    s = _settings(temp_db)
    original_id = await _seed_failed_job(temp_db, thread_ts="1700000000.222222")

    ctx = CommandContext(
        user_id="U02", channel_id="C01",
        text=f"retry {original_id}",
        # 스레드 컨텍스트 없이도 명시 id로 동작
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is not None
    assert result.workflow_job.retry_of == original_id
    assert result.workflow_job.slack_thread_ts == "1700000000.222222"
    assert result.workflow_job.started_by == "U02"  # 새 요청자 기록


@pytest.mark.asyncio
async def test_retry_without_thread_and_without_id_returns_error(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="retry")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is None
    assert "스레드 안에서" in result.response["text"] or "retry" in result.response["text"]


@pytest.mark.asyncio
async def test_retry_with_unknown_thread_ts(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(
        user_id="U01", channel_id="C01", text="retry",
        thread_ts="9999999999.999999",
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is None
    assert "연결돼 있지 않" in result.response["text"]


@pytest.mark.asyncio
async def test_retry_with_unknown_job_id(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="retry 99999")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is None
    assert "99999" in result.response["text"]


@pytest.mark.asyncio
async def test_retry_refused_when_target_ip_already_active(temp_db):
    s = _settings(temp_db)
    original_id = await _seed_failed_job(temp_db, thread_ts="1700000000.333333")
    # 같은 IP로 새 작업이 진행 중인 상황 시뮬레이션
    from autodeploy.models import Job
    async with connect(temp_db) as db:
        active = Job(
            id=None,
            target_ip="10.0.0.99",
            deployment_type="on-premise",
            hospital_code="HOSP_OTHER",
            started_by="U02",
            slack_channel="C01",
        )
        active_id = await repo.create_job(db, active)
        await repo.mark_running(db, active_id)

    ctx = CommandContext(
        user_id="U01", channel_id="C01", text="retry",
        thread_ts="1700000000.333333",
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job is None
    assert "진행 중" in result.response["text"]
    assert str(active_id) in result.response["text"]


@pytest.mark.asyncio
async def test_retry_picks_latest_in_thread_chain(temp_db):
    """같은 스레드에 여러 시도가 있으면 가장 최근 작업의 파라미터를 사용."""
    s = _settings(temp_db)
    thread = "1700000000.444444"
    first_id = await _seed_failed_job(temp_db, thread_ts=thread)
    # 두 번째 시도 — 다른 병원코드로 가정 (실제 retry 시 동일하겠지만, 추출 로직 검증용)
    from autodeploy.models import Job
    async with connect(temp_db) as db:
        second = Job(
            id=None,
            target_ip="10.0.0.99",
            deployment_type="hybrid-with-ai",
            hospital_code="HOSP_NEW",
            started_by="U01",
            slack_channel="C01",
            slack_thread_ts=thread,
        )
        second_id = await repo.create_job(db, second)
        await repo.finish_job(db, second_id, JobStatus.FAILED, error_message="boom2")

    ctx = CommandContext(
        user_id="U01", channel_id="C01", text="retry", thread_ts=thread,
    )
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert result.workflow_job.retry_of == second_id  # 가장 최근
    assert result.workflow_job.hospital_code == "HOSP_NEW"
    assert first_id != second_id  # 사용 변수 안 쓴 경고 회피
