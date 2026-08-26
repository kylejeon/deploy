"""`python -m autodeploy` 진입점. 데몬 본체.

.env 로딩 → settings → DB init → deployment types → workflow + Slack notifier + bot
→ Socket Mode 연결. SIGINT/SIGTERM에서 정상 종료.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from autodeploy import __version__
from autodeploy.config import ConfigError, load_deployment_types
from autodeploy.db import init_db
from autodeploy.settings import SettingsError, load_settings
from autodeploy.slack_app import AutoDeployBot
from autodeploy.slack_notifier import SlackNotifier
from autodeploy.ssh import AsyncSSHClient
from autodeploy.workflow import Workflow, WorkflowConfig

log = logging.getLogger("autodeploy")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def _run() -> int:
    # .env가 있으면 로드 (없으면 환경변수로 처리)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"[설정 오류] {exc}", file=sys.stderr)
        print("Hint: .env를 채웠는지 확인 (.env.example 참고)", file=sys.stderr)
        return 2

    _setup_logging(settings.log_level)
    log.info("AutoDeploy v%s 시작", __version__)
    log.info("DB: %s", settings.db_path)
    log.info("Slack channel: %s", settings.slack_channel_id)
    log.info("Allowed users: %d명", len(settings.allowed_users))

    await init_db(settings.db_path)

    try:
        deployment_types = load_deployment_types(settings.config_path)
    except ConfigError as exc:
        log.error("deployment_types config 오류: %s", exc)
        return 3
    log.info("Deployment types: %s", ", ".join(sorted(deployment_types)))

    # slack-sdk import는 여기서만 — settings 검증 전 import 실패하지 않도록
    from slack_sdk.web.async_client import AsyncWebClient

    web_client = AsyncWebClient(token=settings.slack_bot_token)
    notifier = SlackNotifier(web_client, settings.slack_channel_id)

    def ssh_factory(host: str, port: int):
        return AsyncSSHClient(
            host,
            username=settings.ssh_user,
            password=settings.ssh_password,
            port=port,
        )

    workflow = Workflow(
        ssh_factory=ssh_factory,
        db_path=settings.db_path,
        deployment_types=deployment_types,
        notifier=notifier,
        cfg=WorkflowConfig(
            bitbucket_user=settings.bitbucket_user,
            bitbucket_app_password=settings.bitbucket_app_password,
            repo_host_path=settings.repo_host_path,
            repo_branch=settings.repo_branch,
            work_dir=settings.work_dir,
            # sudo 비밀번호 = ssh 비밀번호 (connecteve 계정 공통). NOPASSWD가 아니어도 동작.
            sudo_password=settings.ssh_password,
            site_admin_email=settings.site_admin_email,
            site_admin_password=settings.site_admin_password,
            site_cloud_base_url=settings.site_cloud_base_url,
            site_api_env=settings.site_api_env,
            jira_base_url=settings.jira_base_url,
            jira_email=settings.jira_email,
            jira_api_token=settings.jira_api_token,
            jira_key=settings.jira_key,
            port_frontend=settings.port_frontend,
            port_temporal=settings.port_temporal,
            port_webpacs=settings.port_webpacs,
        ),
    )

    bot = AutoDeployBot(
        settings=settings,
        deployment_types=deployment_types,
        workflow=workflow,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(sig_name: str) -> None:
        log.info("종료 신호 받음: %s", sig_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig.name)

    log.info("Socket Mode 연결 시작")
    bot_task = asyncio.create_task(bot.start(), name="autodeploy-bot")

    try:
        # bot_task가 먼저 끝나면 (예: 연결 실패) 그것도 종료 신호
        done, _ = await asyncio.wait(
            {bot_task, asyncio.create_task(stop_event.wait(), name="stop-wait")},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if bot_task in done and not stop_event.is_set():
            exc = bot_task.exception()
            if exc is not None:
                log.error("봇 비정상 종료: %s", exc)
                return 4
    finally:
        log.info("종료 진행...")
        try:
            await bot.close()
        except Exception:
            log.exception("bot.close 중 오류")
        if not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except (asyncio.CancelledError, Exception):
                pass

    log.info("종료 완료")
    return 0


def main() -> int:
    # 서브커맨드가 있으면 CLI(계정 관리), 없으면 데몬.
    from autodeploy import cli

    argv = sys.argv[1:]
    if argv:
        return cli.main(argv)

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
