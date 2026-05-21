from __future__ import annotations

import pytest

from autodeploy.models import Job, JobStatus, Step
from autodeploy.slack_notifier import SlackNotifier
from autodeploy.ssh import StreamLine


class MockSlackClient:
    """slack-sdk AsyncWebClient 대용. 모든 chat.* 호출을 기록."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self._counter = 0

    def _next_ts(self) -> str:
        self._counter += 1
        return f"1700000000.{self._counter:06d}"

    async def chat_postMessage(self, **kwargs) -> dict:
        ts = self._next_ts()
        self.posted.append({"ts": ts, **kwargs})
        return {"ts": ts, "ok": True}

    async def chat_update(self, **kwargs) -> dict:
        self.updated.append(kwargs)
        return {"ok": True}


def _job(**kw) -> Job:
    base = dict(
        id=42,
        target_ip="192.168.1.50",
        deployment_type="hybrid-with-ai",
        hospital_code="HOSP01",
        hospital_name="서울대병원",
        started_by="U01",
        slack_channel="C01",
    )
    base.update(kw)
    return Job(**base)


@pytest.mark.asyncio
async def test_job_started_posts_parent_and_thread_ack():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")

    job = _job(status=JobStatus.RUNNING)
    await n.job_started(job)

    assert len(client.posted) == 2
    parent, ack = client.posted
    assert parent["channel"] == "C99"
    assert "thread_ts" not in parent
    assert ack["thread_ts"] == parent["ts"]
    assert "#42" in parent["text"]
    assert "#42" in ack["text"]
    # job 객체에도 thread_ts 반영
    assert job.slack_thread_ts == parent["ts"]


@pytest.mark.asyncio
async def test_step_started_posts_to_thread():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job()

    await n.job_started(job)
    posted_before = len(client.posted)
    await n.step_started(job, Step.INFRA_INSTALL)

    new_msgs = client.posted[posted_before:]
    assert len(new_msgs) == 1
    assert new_msgs[0]["thread_ts"] == client.posted[0]["ts"]
    assert "인프라 설치" in new_msgs[0]["text"]


@pytest.mark.asyncio
async def test_step_log_does_not_post_until_flush_interval():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99", stdout_flush_interval=60.0)  # 사실상 안 됨
    job = _job()

    await n.job_started(job)
    await n.step_started(job, Step.INFRA_INSTALL)
    posted_before = len(client.posted)

    for i in range(20):
        await n.step_log(job, Step.INFRA_INSTALL, StreamLine("stdout", f"line {i}"))

    # 60초 안 지났으니 preview 게시 안 됨
    assert len(client.posted) == posted_before
    # 라인은 내부 버퍼에 누적
    assert len(n._stdout_buf[(42, Step.INFRA_INSTALL)]) == 20


@pytest.mark.asyncio
async def test_step_log_flushes_when_interval_zero():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99", stdout_flush_interval=0.0)  # 매번 flush
    job = _job()

    await n.job_started(job)
    await n.step_started(job, Step.INFRA_INSTALL)

    await n.step_log(job, Step.INFRA_INSTALL, StreamLine("stdout", "first"))
    posted_after_first = len(client.posted)

    await n.step_log(job, Step.INFRA_INSTALL, StreamLine("stdout", "second"))

    # 첫 라인: chat_postMessage로 preview 생성
    # 두 번째: chat_update로 갱신
    assert posted_after_first == len(client.posted) - 0  # 추가 post 없음
    assert any("실시간 로그" in str(u.get("blocks", "")) for u in client.updated) or \
        any("실시간 로그" in p.get("text", "") for p in client.posted)


@pytest.mark.asyncio
async def test_step_finished_force_flushes_then_posts_summary():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99", stdout_flush_interval=60.0)
    job = _job()

    await n.job_started(job)
    await n.step_started(job, Step.INFRA_INSTALL)
    await n.step_log(job, Step.INFRA_INSTALL, StreamLine("stdout", "in progress"))

    posted_before = len(client.posted)
    await n.step_finished(job, Step.INFRA_INSTALL, success=True, duration_s=98)

    # 강제 flush + step_finished 메시지 = 최소 2건 추가
    new_msgs = client.posted[posted_before:]
    assert len(new_msgs) >= 2
    finish_msg = new_msgs[-1]
    assert "완료" in finish_msg["text"]


@pytest.mark.asyncio
async def test_job_finished_succeeded_updates_parent_header():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job(status=JobStatus.RUNNING)

    await n.job_started(job)
    parent_ts = client.posted[0]["ts"]

    job.status = JobStatus.SUCCEEDED
    job.admin_web_url = "http://192.168.1.50/"
    job.script_commit_sha = "abc123"
    await n.job_finished(job, error=None)

    # 요약 메시지 (스레드) + parent 갱신
    summary = client.posted[-1]
    assert summary["thread_ts"] == parent_ts
    assert "http://192.168.1.50/" in str(summary["blocks"])
    assert summary["attachments"] == [{"color": "good"}]

    # parent chat.update 호출
    [updated] = client.updated
    assert updated["ts"] == parent_ts
    assert "완료" in updated["text"]


@pytest.mark.asyncio
async def test_job_finished_failed_summary_uses_danger_color():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job(status=JobStatus.RUNNING)

    await n.job_started(job)
    await n.step_started(job, Step.APP_INSTALL)
    await n.step_log(job, Step.APP_INSTALL, StreamLine("stderr", "container died"))

    job.status = JobStatus.FAILED
    job.current_step = Step.APP_INSTALL
    job.error_message = "script exited with 1"
    await n.job_finished(job, error=RuntimeError("boom"))

    failure = client.posted[-1]
    assert failure["attachments"] == [{"color": "danger"}]
    assert "container died" in str(failure["blocks"])
    # parent 헤더 갱신: 실패
    assert "❌" in client.updated[-1]["text"] or "실패" in client.updated[-1]["text"]


@pytest.mark.asyncio
async def test_state_cleaned_up_after_job_finished():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job(status=JobStatus.RUNNING)

    await n.job_started(job)
    await n.step_started(job, Step.GIT_PULL)
    await n.step_log(job, Step.GIT_PULL, StreamLine("stdout", "x"))

    job.status = JobStatus.SUCCEEDED
    await n.job_finished(job, error=None)

    assert job.id not in n._parent_ts
    assert not any(k[0] == job.id for k in n._stdout_buf)
