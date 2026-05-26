"""Notifier Protocol의 Slack 실 구현.

스레드 관리, stdout chat.update 배치(시간 기반), 부모 메시지 헤더 갱신.
slack-sdk AsyncWebClient를 외부에서 주입받음 (테스트 용이성).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from autodeploy import messages
from autodeploy.models import Job, JobStatus, Step
from autodeploy.ssh import StreamLine

_KST = ZoneInfo("Asia/Seoul")


def _now_kst_str() -> str:
    return datetime.now(_KST).strftime("%H:%M:%S")


class SlackNotifier:
    """slack-sdk AsyncWebClient를 사용해 메시지를 게시한다.

    Notifier Protocol 호환 — workflow가 직접 의존.
    """

    def __init__(
        self,
        web_client: Any,
        channel_id: str,
        *,
        stdout_flush_interval: float = 5.0,
    ) -> None:
        self._client = web_client
        self._channel = channel_id
        self._flush_interval = stdout_flush_interval

        # job별 상태
        # _parent_ts: chat.update 대상 (헤더). install이면 부모 메시지 ts, retry면 스레드 안 sub-header ts
        # _thread_ts: 모든 후속 메시지의 thread_ts 인자. install이면 _parent_ts와 동일, retry면 원본 부모 ts
        self._parent_ts: dict[int, str] = {}
        self._thread_ts: dict[int, str] = {}
        self._start_times: dict[int, float] = {}  # monotonic
        # (job_id, step)별 상태
        self._stdout_buf: dict[tuple[int, Step], list[str]] = {}
        self._stderr_buf: dict[tuple[int, Step], list[str]] = {}
        self._preview_ts: dict[tuple[int, Step], str] = {}
        self._last_flush: dict[tuple[int, Step], float] = {}

    # ---------- Notifier protocol ----------

    async def job_started(self, job: Job) -> None:
        is_retry = job.slack_thread_ts is not None
        parent = messages.parent_message(job)

        if is_retry:
            # 재시도: 기존 스레드 안에 sub-header를 게시한다.
            # 이 sub-header가 새 작업의 헤더 역할 (chat.update 대상).
            thread_ts = job.slack_thread_ts
            resp = await self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=thread_ts,
                text=parent["text"],
                blocks=parent["blocks"],
            )
            self._parent_ts[job.id] = resp["ts"]
            self._thread_ts[job.id] = thread_ts  # 후속 메시지는 원본 스레드에
        else:
            # 신규 install: 채널에 부모 메시지 게시. 그 ts가 헤더이자 스레드 루트.
            resp = await self._client.chat_postMessage(
                channel=self._channel,
                text=parent["text"],
                blocks=parent["blocks"],
            )
            ts = resp["ts"]
            self._parent_ts[job.id] = ts
            self._thread_ts[job.id] = ts
            job.slack_thread_ts = ts

        self._start_times[job.id] = time.monotonic()

        ack = messages.ack_message(job.id)
        await self._client.chat_postMessage(
            channel=self._channel,
            thread_ts=self._thread_ts[job.id],
            text=ack["text"],
            blocks=ack["blocks"],
        )

    async def step_started(self, job: Job, step: Step) -> None:
        thread_ts = self._thread_ts.get(job.id)
        if thread_ts is None:
            return
        key = (job.id, step)
        self._stdout_buf[key] = []
        self._stderr_buf[key] = []
        self._last_flush[key] = time.monotonic()  # interval 기준점 reset
        msg = messages.step_started(step)
        await self._client.chat_postMessage(
            channel=self._channel,
            thread_ts=thread_ts,
            text=msg["text"],
            blocks=msg["blocks"],
        )

    async def step_log(self, job: Job, step: Step, line: StreamLine) -> None:
        if self._thread_ts.get(job.id) is None:
            return
        key = (job.id, step)
        buf = self._stderr_buf if line.stream == "stderr" else self._stdout_buf
        buf.setdefault(key, []).append(line.line)

        now = time.monotonic()
        last = self._last_flush.get(key, 0.0)
        if now - last >= self._flush_interval:
            await self._flush_preview(job, step)
            self._last_flush[key] = now

    async def step_finished(
        self,
        job: Job,
        step: Step,
        *,
        success: bool,
        duration_s: float,
    ) -> None:
        thread_ts = self._thread_ts.get(job.id)
        if thread_ts is None:
            return
        # stdout이 한 줄이라도 있었으면 마지막 상태 강제 flush (없으면 미리보기 메시지 자체 생략)
        if self._stdout_buf.get((job.id, step)):
            await self._flush_preview(job, step)

        msg = messages.step_finished(step, success=success, duration_s=duration_s)
        await self._client.chat_postMessage(
            channel=self._channel,
            thread_ts=thread_ts,
            text=msg["text"],
            blocks=msg["blocks"],
        )

    async def job_finished(self, job: Job, *, error: BaseException | None) -> None:
        header_ts = self._parent_ts.get(job.id)
        thread_ts = self._thread_ts.get(job.id)
        if header_ts is None or thread_ts is None:
            return
        total = time.monotonic() - self._start_times.get(job.id, time.monotonic())

        summary = self._build_summary(job, total)
        if summary is not None:
            await self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=thread_ts,
                text=summary["text"],
                blocks=summary["blocks"],
                attachments=_color_attachments(summary),
            )

        # 헤더 갱신 (install이면 부모, retry면 sub-header)
        parent = messages.parent_message(job, total_duration_s=total)
        await self._client.chat_update(
            channel=self._channel,
            ts=header_ts,
            text=parent["text"],
            blocks=parent["blocks"],
        )

        self._cleanup(job.id)

    # ---------- helpers ----------

    def _build_summary(self, job: Job, total: float) -> dict | None:
        if job.status == JobStatus.SUCCEEDED:
            return messages.success_summary(job, total_duration_s=total)
        if job.status == JobStatus.FAILED:
            stderr_tail: list[str] = []
            if job.current_step is not None:
                stderr_tail = list(self._stderr_buf.get((job.id, job.current_step), []))
            return messages.failure_summary(
                job,
                step=job.current_step,
                stderr_tail=stderr_tail,
                duration_s=total,
            )
        if job.status == JobStatus.CANCELLED:
            return messages.cancel_summary(
                job,
                step_at_cancel=job.current_step,
                duration_s=total,
            )
        return None

    async def _flush_preview(self, job: Job, step: Step) -> None:
        thread_ts = self._thread_ts.get(job.id)
        if thread_ts is None:
            return
        key = (job.id, step)
        lines = self._stdout_buf.get(key, [])
        msg = messages.stdout_preview(step, lines, last_update_kst=_now_kst_str())
        preview_ts = self._preview_ts.get(key)
        if preview_ts is None:
            resp = await self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=thread_ts,
                text=msg["text"],
                blocks=msg["blocks"],
            )
            self._preview_ts[key] = resp["ts"]
        else:
            await self._client.chat_update(
                channel=self._channel,
                ts=preview_ts,
                text=msg["text"],
                blocks=msg["blocks"],
            )

    def _cleanup(self, job_id: int) -> None:
        self._parent_ts.pop(job_id, None)
        self._thread_ts.pop(job_id, None)
        self._start_times.pop(job_id, None)
        for key in list(self._stdout_buf):
            if key[0] == job_id:
                self._stdout_buf.pop(key, None)
                self._stderr_buf.pop(key, None)
                self._preview_ts.pop(key, None)
                self._last_flush.pop(key, None)


def _color_attachments(summary: dict) -> list[dict] | None:
    color = summary.get("attachment_color")
    return [{"color": color}] if color else None
