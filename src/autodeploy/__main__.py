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


async def _start_web(settings, web_client, notifier_enabled: bool):
    """웹 콘솔을 데몬과 같은 루프에 올린다.

    별도 프로세스로 띄우면 같은 SQLite 파일을 두 프로세스가 쓰게 되어 잠금이
    엉킨다. 웹 기동에 실패해도 Slack 봇은 계속 돌아야 하므로 예외를 삼킨다.
    """
    import os

    try:
        # 임포트도 try 안에 둔다. aiohttp 가 빠진 환경에서 `autodeploy.web` 임포트가
        # try 밖에서 터지면 Slack 봇까지 같이 죽어 KeepAlive 크래시 루프가 된다 —
        # 바로 위 docstring 이 약속한 것과 정반대다.
        from autodeploy.masking import SecretMasker
        from autodeploy.settings import HUBCTL_SECRET_ENV
        from autodeploy.web import create_app, run_web
        from autodeploy.web.slack import WebJobNotifier

        app = create_app(
            db_path=settings.db_path,
            hubctl_repo=settings.hubctl_repo_path,
            masker=SecretMasker(
                [os.environ.get(name, "") for name in HUBCTL_SECRET_ENV]
                + [settings.become_password, settings.ssh_password]
            ),
            become_password=settings.become_password,
            notifier=WebJobNotifier(web_client, settings.slack_channel_id)
            if notifier_enabled else None,
            console_url=settings.web.public_url or None,
            session_ttl_days=settings.web.session_ttl_days,
            secure_cookie=settings.web.secure_cookie,
            trust_forwarded=settings.web.trust_forwarded,
            forward_bind=settings.web.host,
            node_prep=settings.node_prep,
        )
        runner, _ = await run_web(app, host=settings.web.host, port=settings.web.port)
    except Exception:
        log.exception("웹 콘솔 기동 실패 — Slack 봇만 계속 실행합니다")
        return None
    if settings.web.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "웹 콘솔이 %s 에 열려 있습니다. 외부 노출 시 HTTPS 리버스 프록시 필수 (§11)",
            settings.web.host,
        )
    return runner


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
    if settings.slack_enabled:
        log.info("Slack channel: %s", settings.slack_channel_id)
        log.info("Allowed users: %d명", len(settings.allowed_users))
    else:
        log.info("Slack 봇 비활성 (SLACK_ENABLED=false)")

    await init_db(settings.db_path)

    # 데몬이 죽으면 그 작업을 감독하던 러너도 사라진다. 남은 running/awaiting 을
    # 정리하지 않으면 영원히 '진행 중'으로 보이고 큐가 막힌 것처럼 읽힌다 (§9).
    from autodeploy.db import connect as db_connect
    from autodeploy.repository import reap_stale_jobs

    async with db_connect(settings.db_path) as db:
        reaped = await reap_stale_jobs(db, reason="데몬 재시작으로 중단됨")
    if reaped:
        log.warning("재시작으로 중단된 작업 %d건 정리: %s", len(reaped), reaped)

    bot = None
    web_client = None
    if settings.slack_enabled:
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

    web_runner = None
    if settings.web.enabled:
        # 웹에서 시작한 작업도 Slack 스레드에 게시한다 (F7).
        # Slack 을 끄면 게시할 곳이 없으므로 게시자도 붙이지 않는다.
        web_runner = await _start_web(
            settings, web_client, notifier_enabled=settings.slack_enabled
        )
    else:
        log.info("웹 콘솔 비활성 (WEB_ENABLED=false)")

    if bot is None and web_runner is None:
        # 둘 다 없으면 이 프로세스가 할 일이 없다. 조용히 떠 있으면 KeepAlive 가
        # 살려두는 바람에 "돌고 있는데 아무 반응이 없는" 상태로 오래 남는다.
        if settings.web.enabled:
            # 켜라고 했는데 못 떴다 = 설정이 아니라 기동이 실패한 것이다.
            # WEB_HOST 를 특정 IP(예: tailnet 주소)로 묶어두면 그 인터페이스가
            # 아직 안 올라온 부팅 직후에 여기로 온다. 종료 코드로 알리면
            # launchd KeepAlive 가 ThrottleInterval 뒤에 다시 띄운다.
            log.error(
                "웹 콘솔(%s:%d)이 뜨지 못했습니다 — 위의 기동 실패 로그를 보세요."
                " WEB_HOST 를 특정 IP 로 묶었다면 그 인터페이스가 아직 없을 수 있습니다"
                " (재시도합니다).",
                settings.web.host, settings.web.port,
            )
        else:
            log.error(
                "Slack 봇도 웹 콘솔도 켜져 있지 않습니다. "
                "SLACK_ENABLED 나 WEB_ENABLED 중 하나는 true 여야 합니다."
            )
        return 2

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(sig_name: str) -> None:
        log.info("종료 신호 받음: %s", sig_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig.name)

    bot_task = None
    if bot is not None:
        log.info("Socket Mode 연결 시작")
        bot_task = asyncio.create_task(bot.start(), name="autodeploy-bot")

    waits = {asyncio.create_task(stop_event.wait(), name="stop-wait")}
    if bot_task is not None:
        # bot_task가 먼저 끝나면 (예: 연결 실패) 그것도 종료 신호
        waits.add(bot_task)

    try:
        done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        if bot_task is not None and bot_task in done and not stop_event.is_set():
            exc = bot_task.exception()
            if exc is not None:
                log.error("봇 비정상 종료: %s", exc)
                return 4
    finally:
        log.info("종료 진행...")
        if web_runner is not None:
            try:
                await web_runner.cleanup()
            except Exception:
                log.exception("웹 콘솔 종료 중 오류")
        if bot is not None:
            try:
                await bot.close()
            except Exception:
                log.exception("bot.close 중 오류")
        if bot_task is not None and not bot_task.done():
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
