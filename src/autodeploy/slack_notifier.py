"""Notifier Protocol의 Slack 실 구현.

스레드 관리 + 단계 시작/종료 메시지 + 부모 메시지 헤더 갱신 + 종료 시 전체 로그
파일 첨부. 진행 중 stdout 미리보기는 따로 게시하지 않는다 (운영자가 보고 싶을 때
첨부 파일로 본다).
slack-sdk AsyncWebClient를 외부에서 주입받음 (테스트 용이성).
"""
from __future__ import annotations

import sys
import time
from typing import Any

from autodeploy import messages
from autodeploy.models import Job, JobStatus, Step
from autodeploy.ssh import StreamLine


class SlackNotifier:
    """slack-sdk AsyncWebClient를 사용해 메시지를 게시한다.

    Notifier Protocol 호환 — workflow가 직접 의존.
    """

    def __init__(self, web_client: Any, channel_id: str) -> None:
        self._client = web_client
        self._channel = channel_id

        # job별 상태
        # _parent_ts: chat.update 대상 (헤더). install이면 부모 메시지 ts, retry면 스레드 안 sub-header ts
        # _thread_ts: 모든 후속 메시지의 thread_ts 인자. install이면 _parent_ts와 동일, retry면 원본 부모 ts
        self._parent_ts: dict[int, str] = {}
        self._thread_ts: dict[int, str] = {}
        self._start_times: dict[int, float] = {}  # monotonic
        # (job_id, step)별 stderr 누적 — 실패 요약에 tail로 노출.
        self._stderr_buf: dict[tuple[int, Step], list[str]] = {}

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
        self._stderr_buf[(job.id, step)] = []
        msg = messages.step_started(step)
        await self._client.chat_postMessage(
            channel=self._channel,
            thread_ts=thread_ts,
            text=msg["text"],
            blocks=msg["blocks"],
        )

    async def step_log(self, job: Job, step: Step, line: StreamLine) -> None:
        # stdout은 슬랙에 게시하지 않는다 (작업 종료 시 첨부 파일에 통째로 들어감).
        # stderr만 누적해두고 실패 요약에 tail로 사용.
        if self._thread_ts.get(job.id) is None:
            return
        if line.stream == "stderr":
            self._stderr_buf.setdefault((job.id, step), []).append(line.line)

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

    async def upload_log_file(self, job: Job, log_text: str) -> None:
        """전체 스크립트 로그를 텍스트 파일로 같은 스레드에 첨부.

        thread_ts는 job.slack_thread_ts에서 직접 읽는다 — job_finished 이후 _cleanup이
        내부 dict를 비우기 때문. 업로드 실패는 작업 결과를 망치지 않도록 stderr만 찍고
        삼킨다 (네트워크 일시 장애·권한 누락 등).
        """
        if not job.slack_thread_ts or not log_text:
            return
        filename = f"install-{job.id}-{job.hospital_code}-{job.status.value}.log"
        try:
            await self._client.files_upload_v2(
                channel=self._channel,
                thread_ts=job.slack_thread_ts,
                content=log_text,
                filename=filename,
                title=filename,
                initial_comment="📄 전체 스크립트 로그",
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[SlackNotifier] upload_log_file failed: {exc}\n")

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

    def _cleanup(self, job_id: int) -> None:
        self._parent_ts.pop(job_id, None)
        self._thread_ts.pop(job_id, None)
        self._start_times.pop(job_id, None)
        for key in list(self._stderr_buf):
            if key[0] == job_id:
                self._stderr_buf.pop(key, None)


def _color_attachments(summary: dict) -> list[dict] | None:
    color = summary.get("attachment_color")
    return [{"color": color}] if color else None
