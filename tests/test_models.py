from __future__ import annotations

from autodeploy.models import STEPS_IN_ORDER, Job, JobStatus, Step


def test_steps_in_order_has_five_phases():
    assert STEPS_IN_ORDER == (
        Step.SSH_CONNECT,
        Step.GIT_PULL,
        Step.INFRA_INSTALL,
        Step.APP_INSTALL,
        Step.HEALTHCHECK,
    )


def test_step_done_not_in_pipeline():
    # 'done'은 종료 마커이지 실행 단계가 아님
    assert Step.DONE not in STEPS_IN_ORDER


def test_job_status_string_values():
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.RUNNING.value == "running"


def test_job_defaults_queued_no_step():
    job = Job(
        id=None,
        target_ip="192.168.1.50",
        deployment_type="hybrid-with-ai",
        hospital_code="HOSP01",
        started_by="U01",
        slack_channel="C01",
    )
    assert job.status == JobStatus.QUEUED
    assert job.current_step is None
    assert job.hospital_name is None
