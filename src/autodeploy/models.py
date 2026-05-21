"""도메인 모델 (Job, JobEvent, ScriptLogLine)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Step(StrEnum):
    SSH_CONNECT = "ssh_connect"
    GIT_PULL = "git_pull"
    INFRA_INSTALL = "infra_install"
    APP_INSTALL = "app_install"
    HEALTHCHECK = "healthcheck"
    DONE = "done"


STEPS_IN_ORDER: tuple[Step, ...] = (
    Step.SSH_CONNECT,
    Step.GIT_PULL,
    Step.INFRA_INSTALL,
    Step.APP_INSTALL,
    Step.HEALTHCHECK,
)


@dataclass(slots=True)
class Job:
    id: int | None
    target_ip: str
    deployment_type: str
    hospital_code: str
    started_by: str
    slack_channel: str
    hospital_name: str | None = None
    hospital_address: str | None = None
    status: JobStatus = JobStatus.QUEUED
    current_step: Step | None = None
    slack_thread_ts: str | None = None
    admin_web_url: str | None = None
    script_commit_sha: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # 런타임 캐시: app 스크립트가 출력한 URL 모음 ({"Frontend": "...", "Temporal Web": "...", ...}).
    # DB에는 admin_web_url(=Frontend)만 저장. 메시지 표시용.
    extra_urls: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class JobEvent:
    id: int | None
    job_id: int
    step: str
    level: str
    message: str
    created_at: datetime | None = None


@dataclass(slots=True)
class ScriptLogLine:
    id: int | None
    job_id: int
    step: str
    stream: str
    line: str
    created_at: datetime | None = None
