"""5단계 워크플로 오케스트레이터. dev-spec F2 + 상태 머신.

ssh_connect → git_pull → infra_install → app_install → healthcheck → done

각 단계 결과를 DB(repository)에 기록하고 Notifier로 이벤트 발행. 실패 시 즉시 종료.
"""
from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

from autodeploy import repository as repo
from autodeploy.config import DeploymentType
from autodeploy.db import connect
from autodeploy.git_sync import GitSyncError, sync_repo
from autodeploy.healthcheck import wait_for_cluster_ready
from autodeploy.models import Job, JobStatus, Step
from autodeploy.notifier import Notifier
from autodeploy.scripts import run_script
from autodeploy.ssh import LineCallback, SSHClient, SSHError, StreamLine


SSHFactory = Callable[[str], AbstractAsyncContextManager[SSHClient]]


# URL embedded credential pattern: scheme://user:TOKEN@host → scheme://user:***@host
# git clone 명령 + stderr가 토큰 포함 URL을 echo할 때 DB/Slack 평문 노출 방지 (QA D-3).
_URL_SECRET_PATTERN = re.compile(r"(https?://[^/\s:@]+:)[^@\s]+(@)")


def mask_url_secrets(text: str) -> str:
    return _URL_SECRET_PATTERN.sub(r"\1***\2", text)


# app 스크립트 stdout의 URL 라인 추출 (QA O-6).
# 예: "[INFO] Frontend URL: http://172.17.0.1:31489"
# 예: "[INFO] Web-PACS URL (Traefik): http://172.17.0.1:30080/"
_APP_URL_PATTERN = re.compile(
    r"\[INFO\]\s+(?P<label>.+?)\s+URL"
    r"(?:\s*\([^)]+\))?"
    r"\s*:\s*"
    r"(?P<scheme>https?)://[^/\s:]+:(?P<port>\d+)(?P<path>/\S*)?",
    re.IGNORECASE,
)


def extract_urls_from_lines(target_ip: str, lines) -> dict[str, str]:
    """app 스크립트 stdout 라인들에서 URL 추출. host는 target_ip로 재구성."""
    out: dict[str, str] = {}
    for line in lines:
        m = _APP_URL_PATTERN.search(line)
        if m:
            label = m.group("label").strip()
            scheme = m.group("scheme").lower()
            port = m.group("port")
            path = m.group("path") or ""
            out[label] = f"{scheme}://{target_ip}:{port}{path}"
    return out


# 타겟에 사전 설치되어 있어야 하는 명령. 누락 시 git_pull 단계에서 명확한 메시지로 거부.
# 봇은 비대화형 SSH라 자격증명 프롬프트나 도구 누락 에러(127)를 사람이 못 보기 때문에,
# 사람이 읽기 쉬운 한 줄 메시지로 빨리 떨어뜨리는 게 중요하다.
_REQUIRED_TARGET_TOOLS: tuple[str, ...] = ("git",)


async def check_target_tools(ssh: SSHClient) -> list[str]:
    """타겟 서버에 _REQUIRED_TARGET_TOOLS가 모두 설치돼 있는지 확인. 누락 도구 리스트 반환."""
    missing: list[str] = []
    for tool in _REQUIRED_TARGET_TOOLS:
        rc = await ssh.exec(f"command -v {shlex.quote(tool)} > /dev/null 2>&1")
        if rc != 0:
            missing.append(tool)
    return missing


async def install_missing_tools(
    ssh: SSHClient,
    missing: list[str],
    on_line=None,
) -> int:
    """누락 도구를 sudo apt로 자동 설치 시도. 마지막 exec exit code 반환.

    `apt update && apt install -y <tools>`를 한 chain으로 실행. DEBIAN_FRONTEND=noninteractive로
    대화형 프롬프트(서비스 재시작 확인 등) 차단. connecteve 계정의 NOPASSWD sudo 권한 가정 —
    이미 setup-*.sh가 같은 권한으로 동작 중이라 추가 권한 요구 없음.
    """
    if not missing:
        return 0
    pkgs = " ".join(shlex.quote(t) for t in missing)
    cmd = (
        "sudo apt-get update && "
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs}"
    )
    return await ssh.exec(cmd, on_line=on_line)


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    bitbucket_user: str
    bitbucket_app_password: str
    repo_host_path: str
    repo_branch: str
    work_dir: str
    admin_web_url_template: str = "http://{ip}/"
    healthcheck_poll_interval: float = 10.0
    healthcheck_timeout: float = 600.0


class WorkflowError(RuntimeError):
    def __init__(self, step: Step, message: str) -> None:
        super().__init__(f"[{step.value}] {message}")
        self.step = step
        self.message = message


class Workflow:
    def __init__(
        self,
        *,
        ssh_factory: SSHFactory,
        db_path: str | Path,
        deployment_types: dict[str, DeploymentType],
        notifier: Notifier,
        cfg: WorkflowConfig,
    ) -> None:
        self.ssh_factory = ssh_factory
        self.db_path = db_path
        self.deployment_types = deployment_types
        self.notifier = notifier
        self.cfg = cfg

    async def run(self, job: Job) -> Job:
        async with connect(self.db_path) as db:
            await self._prepare(db, job)
            try:
                await self._execute(db, job)
                await self._mark_success(db, job)
            except WorkflowError as err:
                await self._mark_failure(db, job, err)
            return job

    async def _prepare(self, db, job: Job) -> None:
        if job.id is None:
            job.id = await repo.create_job(db, job)
        await repo.mark_running(db, job.id)
        job.status = JobStatus.RUNNING
        await self.notifier.job_started(job)
        # SlackNotifier가 install 경로에서 부모 메시지 ts를 job.slack_thread_ts에 채워준다.
        # 그 값이 DB에도 들어가야 다음 retry 명령이 같은 스레드에서 동작 (find_jobs_by_thread_ts).
        # retry 경로에선 create_job 시점에 이미 들어있어 이 update는 같은 값으로 no-op.
        if job.slack_thread_ts is not None:
            await repo.update_thread_ts(db, job.id, job.slack_thread_ts)

    async def _execute(self, db, job: Job) -> None:
        deployment = self.deployment_types.get(job.deployment_type)
        if deployment is None:
            raise WorkflowError(
                Step.SSH_CONNECT,
                f"unknown deployment type: {job.deployment_type}",
            )

        try:
            async with self.ssh_factory(job.target_ip) as ssh:
                await self._step_done(db, job, Step.SSH_CONNECT, success=True, duration=0.0)

                sha = await self._step_git_pull(db, job, ssh)
                await repo.update_commit_sha(db, job.id, sha)

                await self._step_script(db, job, ssh, Step.INFRA_INSTALL, deployment.infra)
                await self._step_script(db, job, ssh, Step.APP_INSTALL, deployment.app)
                await self._step_healthcheck(db, job, ssh)
        except SSHError as exc:
            raise WorkflowError(Step.SSH_CONNECT, str(exc)) from exc

    async def _step_git_pull(self, db, job: Job, ssh: SSHClient) -> str:
        await self._step_start(db, job, Step.GIT_PULL)
        start = time.monotonic()

        # 사전 도구 검증 — 누락 시 자동 설치 시도 후 재검증. 봇이 이미 setup-*.sh에 sudo를
        # 쓰고 있어 단일 패키지 설치도 같은 권한 모델에서 일관성 있게 동작.
        missing = await check_target_tools(ssh)
        if missing:
            log_collector = self._make_log_collector(db, job, Step.GIT_PULL)
            rc = await install_missing_tools(ssh, missing, on_line=log_collector)
            if rc != 0:
                duration = time.monotonic() - start
                await self._step_done(db, job, Step.GIT_PULL, success=False, duration=duration)
                raise WorkflowError(
                    Step.GIT_PULL,
                    f"누락 도구({', '.join(missing)}) 자동 설치 실패 (exit {rc}). "
                    f"`ssh connecteve@{job.target_ip}` 접속 후 "
                    f"`sudo apt-get install -y {' '.join(missing)}` 수동 실행 필요.",
                )
            still_missing = await check_target_tools(ssh)
            if still_missing:
                duration = time.monotonic() - start
                await self._step_done(db, job, Step.GIT_PULL, success=False, duration=duration)
                raise WorkflowError(
                    Step.GIT_PULL,
                    f"자동 설치 후에도 도구 누락: {', '.join(still_missing)}. PATH 확인 필요.",
                )

        try:
            sha = await sync_repo(
                ssh,
                user=self.cfg.bitbucket_user,
                app_password=self.cfg.bitbucket_app_password,
                repo_host_path=self.cfg.repo_host_path,
                branch=self.cfg.repo_branch,
                target_dir=self.cfg.work_dir,
                on_line=self._make_log_collector(db, job, Step.GIT_PULL),
            )
        except GitSyncError as exc:
            duration = time.monotonic() - start
            await self._step_done(db, job, Step.GIT_PULL, success=False, duration=duration)
            raise WorkflowError(Step.GIT_PULL, str(exc)) from exc
        duration = time.monotonic() - start
        await self._step_done(db, job, Step.GIT_PULL, success=True, duration=duration)
        return sha

    async def _step_script(self, db, job: Job, ssh: SSHClient, step: Step, spec) -> None:
        await self._step_start(db, job, step)
        start = time.monotonic()
        # app_install 단계에서만 stdout을 별도로 캡처해 URL 추출에 사용 (QA O-6)
        capture: list[str] | None = [] if step == Step.APP_INSTALL else None
        rc = await run_script(
            ssh,
            workdir=self.cfg.work_dir,
            spec=spec,
            code=job.hospital_code,
            on_line=self._make_log_collector(db, job, step, capture=capture),
        )
        duration = time.monotonic() - start
        if rc != 0:
            await self._step_done(db, job, step, success=False, duration=duration)
            raise WorkflowError(step, f"script exited with {rc}")
        if capture is not None:
            job.extra_urls = extract_urls_from_lines(job.target_ip, capture)
        await self._step_done(db, job, step, success=True, duration=duration)

    async def _step_healthcheck(self, db, job: Job, ssh: SSHClient) -> None:
        await self._step_start(db, job, Step.HEALTHCHECK)
        start = time.monotonic()
        result = await wait_for_cluster_ready(
            ssh,
            poll_interval=self.cfg.healthcheck_poll_interval,
            timeout=self.cfg.healthcheck_timeout,
        )
        duration = time.monotonic() - start
        if not result.ready:
            await self._step_done(db, job, Step.HEALTHCHECK, success=False, duration=duration)
            raise WorkflowError(
                Step.HEALTHCHECK,
                f"cluster not ready after {duration:.0f}s; last:\n{result.last_output}",
            )
        await self._step_done(db, job, Step.HEALTHCHECK, success=True, duration=duration)

    async def _step_start(self, db, job: Job, step: Step) -> None:
        await repo.update_current_step(db, job.id, step)
        job.current_step = step
        await repo.add_event(db, job.id, step.value, "info", "started")
        await self.notifier.step_started(job, step)

    async def _step_done(
        self,
        db,
        job: Job,
        step: Step,
        *,
        success: bool,
        duration: float,
    ) -> None:
        level = "info" if success else "error"
        msg = f"finished ({duration:.1f}s)" if success else f"failed ({duration:.1f}s)"
        await repo.add_event(db, job.id, step.value, level, msg)
        await self.notifier.step_finished(job, step, success=success, duration_s=duration)

    def _make_log_collector(
        self,
        db,
        job: Job,
        step: Step,
        *,
        capture: list[str] | None = None,
    ) -> LineCallback:
        async def collect(line: StreamLine) -> None:
            masked = StreamLine(line.stream, mask_url_secrets(line.line))
            if capture is not None and masked.stream == "stdout":
                capture.append(masked.line)
            await repo.add_script_log(db, job.id, step.value, masked.stream, masked.line)
            await self.notifier.step_log(job, step, masked)
        return collect

    async def _mark_success(self, db, job: Job) -> None:
        # Frontend URL이 잡혔으면 admin_web_url로, 아니면 template fallback (QA O-6)
        admin_url = job.extra_urls.get("Frontend") or self.cfg.admin_web_url_template.format(
            ip=job.target_ip
        )
        await repo.finish_job(db, job.id, JobStatus.SUCCEEDED, admin_web_url=admin_url)
        job.status = JobStatus.SUCCEEDED
        job.admin_web_url = admin_url
        job.current_step = Step.DONE
        await self.notifier.job_finished(job, error=None)

    async def _mark_failure(self, db, job: Job, err: WorkflowError) -> None:
        await repo.finish_job(db, job.id, JobStatus.FAILED, error_message=err.message)
        job.status = JobStatus.FAILED
        job.error_message = err.message
        await self.notifier.job_finished(job, error=err)
