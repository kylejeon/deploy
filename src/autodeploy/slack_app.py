"""Slack Bolt Socket Mode 봇.

handle_command는 pure (DB I/O만 부수효과) — bolt 의존 없이 단위 테스트 가능.
AutoDeployBot는 bolt를 감싸 멘션 이벤트를 dispatch.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from autodeploy import messages
from autodeploy import repository as repo
from autodeploy.commands import (
    CancelCommand,
    HelpCommand,
    InstallCommand,
    ListCommand,
    ParseError,
    RetryCommand,
    StatusCommand,
    parse_command,
)
from autodeploy.config import DeploymentType
from autodeploy.db import connect
from autodeploy.models import Job, JobStatus
from autodeploy.settings import Settings
from autodeploy.workflow import Workflow

_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")


@dataclass(frozen=True, slots=True)
class CommandContext:
    user_id: str
    channel_id: str
    text: str
    # 멘션이 스레드 댓글로 왔을 때만 채워짐. retry 명령이 스레드 컨텍스트로 원본을 찾는 데 사용.
    thread_ts: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """봇이 사용자에게 줄 응답 + 부수효과 신호.

    response: ephemeral로 게시할 메시지
    workflow_job: 백그라운드에서 시작할 workflow job
    cancel_job_id: 봇이 cancel해야 할 task의 job_id (검증은 이미 handle_command에서 완료)
    """

    response: dict | None
    workflow_job: Job | None = None
    cancel_job_id: int | None = None


async def handle_command(
    ctx: CommandContext,
    *,
    settings: Settings,
    deployment_types: dict[str, DeploymentType],
) -> CommandResult:
    """모든 명령을 받아 응답 dict + (선택) workflow_job 반환.

    부수효과는 DB 접근(Job 조회·생성)만. workflow 실행은 호출자(봇)가 트리거.
    """
    if settings.allowed_users and ctx.user_id not in settings.allowed_users:
        return CommandResult(response=messages.permission_denied())

    if settings.slack_channel_id and ctx.channel_id != settings.slack_channel_id:
        return CommandResult(
            response=messages.validation_error("이 채널에서는 사용할 수 없습니다.")
        )

    text = _MENTION_RE.sub("", ctx.text).strip()
    valid_types = frozenset(deployment_types)
    cmd = parse_command(text, valid_types)

    if isinstance(cmd, HelpCommand):
        return CommandResult(response=messages.help_response(sorted(valid_types)))

    if isinstance(cmd, ParseError):
        return CommandResult(
            response=messages.validation_error(cmd.message, suggestion=cmd.suggestion)
        )

    if isinstance(cmd, StatusCommand):
        async with connect(settings.db_path) as db:
            if cmd.job_id is not None:
                # 명시 id면 종료된 작업도 그대로 표시 (사용자가 콕 짚어 물어본 거)
                job = await repo.get_job(db, cmd.job_id)
            else:
                # 무인자면 진행 중(queued/running)만. 종료된 작업은 list로 보면 됨.
                active = await repo.find_active_jobs(db, limit=1)
                job = active[0] if active else None
        return CommandResult(response=messages.status_response(job))

    if isinstance(cmd, ListCommand):
        async with connect(settings.db_path) as db:
            jobs = await repo.list_recent_jobs(db, limit=cmd.limit)
        return CommandResult(response=messages.list_response(jobs, limit=cmd.limit))

    if isinstance(cmd, CancelCommand):
        async with connect(settings.db_path) as db:
            target = await repo.get_job(db, cmd.job_id)
        if target is None:
            return CommandResult(
                response=messages.validation_error(f"작업 #{cmd.job_id}을(를) 찾을 수 없습니다.")
            )
        if target.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return CommandResult(
                response=messages.validation_error(
                    f"작업 #{cmd.job_id}은 이미 종료됨 (`{target.status.value}`).",
                    suggestion=f"`@autodeploy status {cmd.job_id}` 로 자세히 확인",
                )
            )
        return CommandResult(
            response=messages.cancel_ack(cmd.job_id),
            cancel_job_id=cmd.job_id,
        )

    if isinstance(cmd, RetryCommand):
        return await _handle_retry(cmd, ctx, settings=settings)

    if isinstance(cmd, InstallCommand):
        async with connect(settings.db_path) as db:
            active = await repo.find_active_by_ip(db, cmd.target_ip)
            if active:
                return CommandResult(
                    response=messages.validation_error(
                        f"`{cmd.target_ip}`에 진행 중인 작업이 있습니다: #{active[0].id}",
                        suggestion=f"`@autodeploy status {active[0].id}`",
                    )
                )
            job = Job(
                id=None,
                target_ip=cmd.target_ip,
                deployment_type=cmd.deployment_type,
                hospital_code=cmd.hospital_code,
                hospital_name=cmd.hospital_name,
                hospital_address=cmd.hospital_address,
                started_by=ctx.user_id,
                slack_channel=ctx.channel_id,
            )
            job.id = await repo.create_job(db, job)
        return CommandResult(response=None, workflow_job=job)

    return CommandResult(response=messages.validation_error("internal error"))


async def _handle_retry(
    cmd: RetryCommand,
    ctx: CommandContext,
    *,
    settings: Settings,
) -> CommandResult:
    """retry: 원본 작업을 찾아 같은 파라미터로 새 Job 생성. 같은 슬랙 스레드에 묶음."""
    async with connect(settings.db_path) as db:
        original: Job | None = None
        if cmd.job_id is not None:
            original = await repo.get_job(db, cmd.job_id)
            if original is None:
                return CommandResult(
                    response=messages.validation_error(f"작업 #{cmd.job_id}을(를) 찾을 수 없습니다.")
                )
        elif ctx.thread_ts:
            jobs_in_thread = await repo.find_jobs_by_thread_ts(db, ctx.thread_ts)
            if not jobs_in_thread:
                return CommandResult(
                    response=messages.validation_error(
                        "이 스레드는 작업과 연결돼 있지 않아요.",
                        suggestion="`@autodeploy retry <job-id>` 형태로 명시할 수 있습니다.",
                    )
                )
            original = jobs_in_thread[0]  # 가장 최근 시도
        else:
            return CommandResult(
                response=messages.validation_error(
                    "retry는 작업 스레드 안에서 실행하거나, `retry <job-id>`로 명시해주세요.",
                )
            )

        # 같은 IP로 진행 중인 작업이 있으면 거부 (install과 동일)
        active = await repo.find_active_by_ip(db, original.target_ip)
        if active:
            return CommandResult(
                response=messages.validation_error(
                    f"`{original.target_ip}`에 진행 중인 작업이 있습니다: #{active[0].id}",
                    suggestion=f"`@autodeploy status {active[0].id}`",
                )
            )

        # 원본의 슬랙 스레드를 그대로 이어쓴다. SlackNotifier는 slack_thread_ts가 사전 설정된 걸 보고
        # 새 부모 메시지를 만들지 않고 기존 스레드에 sub-header를 게시한다.
        new_job = Job(
            id=None,
            target_ip=original.target_ip,
            deployment_type=original.deployment_type,
            hospital_code=original.hospital_code,
            hospital_name=original.hospital_name,
            hospital_address=original.hospital_address,
            started_by=ctx.user_id,
            slack_channel=ctx.channel_id,
            slack_thread_ts=original.slack_thread_ts,
            retry_of=original.id,
        )
        new_job.id = await repo.create_job(db, new_job)
    return CommandResult(response=None, workflow_job=new_job)


class AutoDeployBot:
    """slack-bolt Socket Mode 봇 래퍼.

    - app_mention 이벤트 → handle_command
    - response가 있으면 ephemeral로 게시
    - workflow_job이 있으면 workflow.run을 백그라운드로 시작
    """

    def __init__(
        self,
        *,
        settings: Settings,
        deployment_types: dict[str, DeploymentType],
        workflow: Workflow,
    ) -> None:
        # bolt는 import 시점에 비용이 좀 있어 지연 import
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.app.async_app import AsyncApp

        self._settings = settings
        self._deployment_types = deployment_types
        self._workflow = workflow
        self._app = AsyncApp(token=settings.slack_bot_token)
        self._socket_handler = AsyncSocketModeHandler(self._app, settings.slack_app_token)
        # job_id -> task. cancel 명령이 task.cancel()을 호출할 수 있도록.
        self._tasks_by_job: dict[int, asyncio.Task] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self._app.event("app_mention")
        async def _on_mention(event: dict, client: Any, logger: Any) -> None:
            await self._dispatch(event, client, logger)

    async def _dispatch(self, event: dict, client: Any, logger: Any) -> None:
        try:
            ctx = CommandContext(
                user_id=event.get("user", ""),
                channel_id=event.get("channel", ""),
                text=event.get("text", ""),
                # Slack은 스레드 댓글 이벤트에서 thread_ts(=부모 ts)를 같이 보냄. 채널 본문이면 None.
                thread_ts=event.get("thread_ts"),
            )
            result = await handle_command(
                ctx,
                settings=self._settings,
                deployment_types=self._deployment_types,
            )
            if result.response is not None:
                await client.chat_postEphemeral(
                    channel=ctx.channel_id,
                    user=ctx.user_id,
                    text=result.response["text"],
                    blocks=result.response.get("blocks"),
                )
            if result.workflow_job is not None:
                job_id = result.workflow_job.id
                task = asyncio.create_task(self._workflow.run(result.workflow_job))
                self._tasks_by_job[job_id] = task
                task.add_done_callback(lambda _t, jid=job_id: self._tasks_by_job.pop(jid, None))
            if result.cancel_job_id is not None:
                await self._cancel_task(result.cancel_job_id)
        except Exception:
            logger.exception("dispatch failed")

    async def _cancel_task(self, job_id: int) -> None:
        """task.cancel()로 워크플로 정상 종료 경로 발동. 정리(DB/Slack)는 workflow가 담당.

        task가 메모리에 없으면 (예: 데몬 재시작 후의 orphan) DB만이라도 CANCELLED로 정정.
        이 경로의 Slack 부모 메시지는 갱신 못함 — notifier의 _parent_ts가 사라졌기 때문.
        """
        task = self._tasks_by_job.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            return
        async with connect(self._settings.db_path) as db:
            target = await repo.get_job(db, job_id)
            if target is not None and target.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                await repo.finish_job(db, job_id, JobStatus.CANCELLED)

    async def start(self) -> None:
        await self._socket_handler.start_async()

    async def close(self) -> None:
        await self._socket_handler.close_async()
