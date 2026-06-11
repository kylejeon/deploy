from __future__ import annotations

import pytest

from autodeploy.models import Job, JobStatus, Step
from autodeploy.slack_notifier import SlackNotifier
from autodeploy.ssh import StreamLine


class MockSlackClient:
    """slack-sdk AsyncWebClient 대용. 모든 chat.*/files_upload_v2 호출을 기록."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.uploaded: list[dict] = []
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

    async def files_upload_v2(self, **kwargs) -> dict:
        self.uploaded.append(kwargs)
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
async def test_step_log_stdout_does_not_post():
    """stdout은 스레드에 게시 안 됨 (전체 로그는 종료 시 파일로)."""
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job()

    await n.job_started(job)
    await n.step_started(job, Step.INFRA_INSTALL)
    posted_before = len(client.posted)

    for i in range(50):
        await n.step_log(job, Step.INFRA_INSTALL, StreamLine("stdout", f"line {i}"))

    assert len(client.posted) == posted_before  # stdout은 게시 안 됨
    assert client.updated == []  # 미리보기 갱신도 없음


@pytest.mark.asyncio
async def test_step_log_stderr_buffered_for_failure_tail():
    """stderr는 누적 — 실패 시 요약에 tail로 노출."""
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job(status=JobStatus.RUNNING)

    await n.job_started(job)
    await n.step_started(job, Step.APP_INSTALL)
    await n.step_log(job, Step.APP_INSTALL, StreamLine("stderr", "container died"))
    await n.step_log(job, Step.APP_INSTALL, StreamLine("stdout", "ignored"))

    assert n._stderr_buf[(42, Step.APP_INSTALL)] == ["container died"]


@pytest.mark.asyncio
async def test_step_finished_posts_summary_message():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job()

    await n.job_started(job)
    await n.step_started(job, Step.INFRA_INSTALL)
    await n.step_log(job, Step.INFRA_INSTALL, StreamLine("stdout", "in progress"))

    posted_before = len(client.posted)
    await n.step_finished(job, Step.INFRA_INSTALL, success=True, duration_s=98)

    # 5초 미리보기 제거 후엔 step_finished 메시지 한 건만 추가
    new_msgs = client.posted[posted_before:]
    assert len(new_msgs) == 1
    assert "완료" in new_msgs[0]["text"]


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
    await n.step_log(job, Step.GIT_PULL, StreamLine("stderr", "warn"))

    job.status = JobStatus.SUCCEEDED
    await n.job_finished(job, error=None)

    assert job.id not in n._parent_ts
    assert job.id not in n._thread_ts
    assert not any(k[0] == job.id for k in n._stderr_buf)


# ---------- upload_log_file ----------

@pytest.mark.asyncio
async def test_upload_log_file_uses_job_thread_and_status_filename():
    """종료 후 _cleanup으로 내부 dict가 비어도 job.slack_thread_ts로 첨부 가능해야 함."""
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job(status=JobStatus.SUCCEEDED, slack_thread_ts="1700000000.999999")

    await n.upload_log_file(job, "full log body")

    assert len(client.uploaded) == 1
    up = client.uploaded[0]
    assert up["channel"] == "C99"
    assert up["thread_ts"] == "1700000000.999999"
    assert up["content"] == "full log body"
    assert "install-42" in up["filename"]
    assert "HOSP01" in up["filename"]
    assert "succeeded" in up["filename"]


@pytest.mark.asyncio
async def test_upload_log_file_failed_status_in_filename():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job(status=JobStatus.FAILED, slack_thread_ts="ts")
    await n.upload_log_file(job, "errors here")
    assert "failed" in client.uploaded[0]["filename"]


@pytest.mark.asyncio
async def test_upload_log_file_noop_without_thread_or_text():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job_no_thread = _job(status=JobStatus.SUCCEEDED, slack_thread_ts=None)
    await n.upload_log_file(job_no_thread, "body")
    job_empty = _job(status=JobStatus.SUCCEEDED, slack_thread_ts="ts")
    await n.upload_log_file(job_empty, "")
    assert client.uploaded == []


@pytest.mark.asyncio
async def test_upload_log_file_swallows_exceptions(capsys):
    """업로드 실패해도 작업 결과를 망치지 않음. stderr에 진단 한 줄."""

    class BrokenClient(MockSlackClient):
        async def files_upload_v2(self, **kwargs):
            raise RuntimeError("invalid_auth")

    n = SlackNotifier(BrokenClient(), "C99")
    job = _job(status=JobStatus.FAILED, slack_thread_ts="ts")
    await n.upload_log_file(job, "log")  # 예외 안 던짐
    captured = capsys.readouterr()
    assert "upload_log_file failed" in captured.err
    assert "invalid_auth" in captured.err


# ---------- retry: 기존 스레드 재사용 ----------

@pytest.mark.asyncio
async def test_retry_job_posts_sub_header_inside_existing_thread():
    """slack_thread_ts가 사전 설정된 Job은 새 부모 대신 그 스레드에 sub-header를 게시한다."""
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")

    original_thread = "1700000000.000099"
    retry_job = _job(
        id=43,
        status=JobStatus.RUNNING,
        slack_thread_ts=original_thread,
        retry_of=42,
    )
    await n.job_started(retry_job)

    # 2건 게시 (sub-header + ack), 둘 다 thread_ts=원본
    assert len(client.posted) == 2
    sub_header, ack = client.posted
    assert sub_header["thread_ts"] == original_thread  # 채널 본문이 아니라 스레드 안
    assert ack["thread_ts"] == original_thread

    # 헤더는 sub-header ts (chat.update 대상)
    assert n._parent_ts[43] == sub_header["ts"]
    # 모든 후속 메시지의 thread_ts는 원본 스레드
    assert n._thread_ts[43] == original_thread


@pytest.mark.asyncio
async def test_retry_step_messages_go_into_original_thread():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")

    original_thread = "1700000000.000077"
    retry_job = _job(id=44, slack_thread_ts=original_thread, retry_of=42)
    await n.job_started(retry_job)
    posted_before = len(client.posted)

    await n.step_started(retry_job, Step.INFRA_INSTALL)
    new_msg = client.posted[posted_before]
    assert new_msg["thread_ts"] == original_thread


@pytest.mark.asyncio
async def test_retry_job_finished_updates_sub_header_not_original_parent():
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")

    original_thread = "1700000000.000055"
    retry_job = _job(id=45, status=JobStatus.RUNNING, slack_thread_ts=original_thread, retry_of=42)
    await n.job_started(retry_job)
    sub_header_ts = client.posted[0]["ts"]
    assert sub_header_ts != original_thread  # 새 ts가 발급됨

    retry_job.status = JobStatus.SUCCEEDED
    retry_job.admin_web_url = "http://10.0.0.99/"
    await n.job_finished(retry_job, error=None)

    # chat.update는 sub-header ts를 갱신 (원본 부모는 건드리지 않음)
    [updated] = client.updated
    assert updated["ts"] == sub_header_ts
    assert "완료" in updated["text"]


@pytest.mark.asyncio
async def test_install_path_unchanged_no_thread_ts_preset():
    """기존 install 경로(slack_thread_ts 사전 설정 없음)는 동작이 같아야 한다."""
    client = MockSlackClient()
    n = SlackNotifier(client, "C99")
    job = _job(status=JobStatus.RUNNING)
    assert job.slack_thread_ts is None  # 사전 설정 없음

    await n.job_started(job)
    parent, ack = client.posted
    assert "thread_ts" not in parent  # 채널 본문
    assert ack["thread_ts"] == parent["ts"]
    assert n._parent_ts[42] == parent["ts"]
    assert n._thread_ts[42] == parent["ts"]
    assert job.slack_thread_ts == parent["ts"]
