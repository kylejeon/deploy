"""Notifier Protocol + 기본 구현 (Null, Recording).

Slack 통합은 Phase 3에서 SlackNotifier로 별도 추가. workflow는 Notifier만 의존.
"""
from __future__ import annotations

from typing import Any, Protocol

from autodeploy.models import Job, Step
from autodeploy.ssh import StreamLine


class Notifier(Protocol):
    async def job_started(self, job: Job) -> None: ...
    async def step_started(self, job: Job, step: Step) -> None: ...
    async def step_log(self, job: Job, step: Step, line: StreamLine) -> None: ...
    async def step_finished(
        self,
        job: Job,
        step: Step,
        *,
        success: bool,
        duration_s: float,
    ) -> None: ...
    async def job_finished(self, job: Job, *, error: BaseException | None) -> None: ...
    async def upload_log_file(self, job: Job, log_text: str) -> None: ...


class NullNotifier:
    """No-op. 봇 미통합 환경 또는 테스트 기본값."""

    async def job_started(self, job: Job) -> None: pass
    async def step_started(self, job: Job, step: Step) -> None: pass
    async def step_log(self, job: Job, step: Step, line: StreamLine) -> None: pass
    async def step_finished(self, job: Job, step: Step, *, success: bool, duration_s: float) -> None: pass
    async def job_finished(self, job: Job, *, error: BaseException | None) -> None: pass
    async def upload_log_file(self, job: Job, log_text: str) -> None: pass


class RecordingNotifier:
    """테스트용. 모든 호출을 순서대로 self.events에 누적."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def job_started(self, job: Job) -> None:
        self.events.append(("job_started", {"job_id": job.id, "ip": job.target_ip, "type": job.deployment_type}))

    async def step_started(self, job: Job, step: Step) -> None:
        self.events.append(("step_started", {"job_id": job.id, "step": step.value}))

    async def step_log(self, job: Job, step: Step, line: StreamLine) -> None:
        self.events.append(("step_log", {"job_id": job.id, "step": step.value, "stream": line.stream, "line": line.line}))

    async def step_finished(self, job: Job, step: Step, *, success: bool, duration_s: float) -> None:
        self.events.append(("step_finished", {"job_id": job.id, "step": step.value, "success": success}))

    async def job_finished(self, job: Job, *, error: BaseException | None) -> None:
        self.events.append((
            "job_finished",
            {"job_id": job.id, "status": job.status.value, "error": str(error) if error else None},
        ))

    async def upload_log_file(self, job: Job, log_text: str) -> None:
        self.events.append((
            "upload_log_file",
            {"job_id": job.id, "status": job.status.value, "log_len": len(log_text)},
        ))
