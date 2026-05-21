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
async def test_status_without_id_returns_latest(temp_db):
    s = _settings(temp_db)
    async with connect(temp_db) as db:
        from autodeploy.models import Job
        await repo.create_job(db, Job(
            id=None, target_ip="2.2.2.2", deployment_type="hybrid-with-ai",
            hospital_code="H1", started_by="U01", slack_channel="C01",
        ))

    ctx = CommandContext(user_id="U01", channel_id="C01", text="status")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "2.2.2.2" in str(result.response["blocks"])


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
    assert job.deployment_type == "hybrid-with-ai"
    assert job.hospital_code == "HOSP01"
    assert job.started_by == "U01"

    # DB에 기록
    async with connect(temp_db) as db:
        loaded = await repo.get_job(db, job.id)
    assert loaded is not None
    assert loaded.status == JobStatus.QUEUED


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


# ---------- cancel / unknown ----------

@pytest.mark.asyncio
async def test_cancel_returns_not_yet_supported(temp_db):
    s = _settings(temp_db)
    ctx = CommandContext(user_id="U01", channel_id="C01", text="cancel 5")
    result = await handle_command(ctx, settings=s, deployment_types=TYPES)
    assert "v1.1" in result.response["text"]


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
