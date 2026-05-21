from __future__ import annotations

import pytest

from autodeploy.models import Job, JobStatus, Step
from autodeploy.notifier import NullNotifier, RecordingNotifier
from autodeploy.ssh import StreamLine


def _job(**kw) -> Job:
    base = dict(
        id=1,
        target_ip="1.2.3.4",
        deployment_type="on-premise",
        hospital_code="HOSP01",
        started_by="U01",
        slack_channel="C01",
    )
    base.update(kw)
    return Job(**base)


@pytest.mark.asyncio
async def test_null_notifier_swallows_all_calls():
    n = NullNotifier()
    job = _job()
    # 어떤 호출도 예외 없이 통과
    await n.job_started(job)
    await n.step_started(job, Step.GIT_PULL)
    await n.step_log(job, Step.GIT_PULL, StreamLine("stdout", "x"))
    await n.step_finished(job, Step.GIT_PULL, success=True, duration_s=1.2)
    await n.job_finished(job, error=None)


@pytest.mark.asyncio
async def test_recording_notifier_captures_events_in_order():
    n = RecordingNotifier()
    job = _job()

    await n.job_started(job)
    await n.step_started(job, Step.INFRA_INSTALL)
    await n.step_log(job, Step.INFRA_INSTALL, StreamLine("stdout", "installing"))
    await n.step_finished(job, Step.INFRA_INSTALL, success=True, duration_s=10.5)
    job.status = JobStatus.SUCCEEDED
    await n.job_finished(job, error=None)

    names = [e[0] for e in n.events]
    assert names == [
        "job_started",
        "step_started",
        "step_log",
        "step_finished",
        "job_finished",
    ]
    assert n.events[2][1]["line"] == "installing"
    assert n.events[3][1]["success"] is True
    assert n.events[4][1]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_recording_notifier_captures_error_string():
    n = RecordingNotifier()
    job = _job()
    job.status = JobStatus.FAILED
    await n.job_finished(job, error=RuntimeError("script failed"))
    assert n.events[-1][1]["error"] == "script failed"
