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
    StatusCommand,
    parse_command,
)
from autodeploy.config import DeploymentType
from autodeploy.db import connect
from autodeploy.models import Job
from autodeploy.settings import Settings
from autodeploy.workflow import Workflow

_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")


@dataclass(frozen=True, slots=True)
class CommandContext:
    user_id: str
    channel_id: str
    text: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    """봇이 사용자에게 줄 응답 + 백그라운드로 시작할 workflow job."""

    response: dict | None
    workflow_job: Job | None = None


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
                job = await repo.get_job(db, cmd.job_id)
            else:
                recent = await repo.list_recent_jobs(db, limit=1)
                job = recent[0] if recent else None
        return CommandResult(response=messages.status_response(job))

    if isinstance(cmd, ListCommand):
        async with connect(settings.db_path) as db:
            jobs = await repo.list_recent_jobs(db, limit=cmd.limit)
        return CommandResult(response=messages.list_response(jobs, limit=cmd.limit))

    if isinstance(cmd, CancelCommand):
        return CommandResult(
            response=messages.validation_error(
                "cancel은 v1.1에서 지원 예정입니다.",
                suggestion=f"`@autodeploy status {cmd.job_id}` 로 진행 상황 확인 가능",
            )
        )

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
        self._running_tasks: set[asyncio.Task] = set()
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
                task = asyncio.create_task(self._workflow.run(result.workflow_job))
                self._running_tasks.add(task)
                task.add_done_callback(self._running_tasks.discard)
        except Exception:
            logger.exception("dispatch failed")

    async def start(self) -> None:
        await self._socket_handler.start_async()

    async def close(self) -> None:
        await self._socket_handler.close_async()
