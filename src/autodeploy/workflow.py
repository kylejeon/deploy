"""5단계 워크플로 오케스트레이터. dev-spec F2 + 상태 머신.

ssh_connect → git_pull → infra_install → app_install → healthcheck → done

각 단계 결과를 DB(repository)에 기록하고 Notifier로 이벤트 발행. 실패 시 즉시 종료.
"""
from __future__ import annotations

import asyncio
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
from autodeploy import site_registration
from autodeploy.site_registration import INSTALLATION_TYPE_MAP, SiteAPIError
from autodeploy.ssh import LineCallback, SSHClient, SSHError, StreamLine
from autodeploy.jira_client import JiraAPIError, JiraClient
from autodeploy.product_registration import ProductRegistrationClient


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


async def ensure_universe_enabled(
    ssh: SSHClient,
    *,
    sudo_password: str = "",
    on_line=None,
) -> int:
    """타겟의 apt universe 저장소를 활성화. 멱등 (이미 켜져있으면 no-op).

    Ubuntu의 awscli·일부 K8s 도구는 universe에만 존재. setup-*.sh가 내부에서
    `apt install <pkg>`를 호출했을 때 'no installation candidate'로 실패하는 것을
    선제 차단. add-apt-repository가 없으면 software-properties-common을 먼저 깐다.
    """
    inner = (
        # add-apt-repository가 없으면 software-properties-common 설치
        "if ! command -v add-apt-repository > /dev/null 2>&1; then "
        "apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common; "
        "fi && "
        # universe 활성화 (이미 활성이면 no-op) + 인덱스 갱신
        "add-apt-repository -y universe && "
        "apt-get update"
    )
    sudo_prefix = "sudo"
    if sudo_password:
        pw_q = shlex.quote(sudo_password)
        sudo_prefix = f"printf '%s\\n' {pw_q} | sudo -S -p ''"
    cmd = f"{sudo_prefix} bash -c {shlex.quote(inner)}"
    return await ssh.exec(cmd, on_line=on_line)


async def install_missing_tools(
    ssh: SSHClient,
    missing: list[str],
    *,
    sudo_password: str = "",
    on_line=None,
) -> int:
    """누락 도구를 sudo apt로 자동 설치 시도. exit code 반환.

    `apt-get update && apt-get install -y <tools>`를 한 sudo 세션 안에서 실행 (bash -c).
    DEBIAN_FRONTEND=noninteractive로 대화형 프롬프트(서비스 재시작 확인 등) 차단.
    sudo_password가 주어지면 stdin으로 자동 주입(`sudo -S`) — connecteve 계정이
    NOPASSWD가 아니어도 동작. 비어있으면 NOPASSWD 가정.
    """
    if not missing:
        return 0
    pkgs = " ".join(shlex.quote(t) for t in missing)
    inner = (
        f"apt-get update && "
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkgs}"
    )
    sudo_prefix = "sudo"
    if sudo_password:
        pw_q = shlex.quote(sudo_password)
        # printf은 bash 빌트인이라 별도 프로세스 안 생김 → ps에 비밀번호 노출 X
        sudo_prefix = f"printf '%s\\n' {pw_q} | sudo -S -p ''"
    cmd = f"{sudo_prefix} bash -c {shlex.quote(inner)}"
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
    # sudo 비밀번호 (보통 settings.ssh_password와 동일). 비어있으면 NOPASSWD 가정.
    # apt-get 자동 설치 + setup-*.sh sudo 호출에 stdin으로 주입.
    sudo_password: str = ""
    # 설치 직후 자동 병원 등록용 마스터 계정. 비어있으면 site_register 단계 skip.
    site_admin_email: str = ""
    site_admin_password: str = ""
    # on-premise일 때 강제할 Host 헤더. Traefik이 Host로 라우팅하므로 target IP로
    # 직접 쳐도 'localhost'로 보내야 매칭되는 환경. hybrid는 None으로 두면
    # aiohttp가 URL의 dev-gateway.connecteve.com을 그대로 Host로 씀.
    site_host_header_onpremise: str = "localhost"
    # hybrid 케이스의 base URL. 운영 환경 분리 시 env로 오버라이드.
    site_cloud_base_url: str = "https://dev-gateway.connecteve.com"
    # site API 요청에 박는 x-api-env 헤더 (Postman 캡쳐 기준 'dev').
    site_api_env: str = "dev"
    # Jira (생산관리 프로젝트) — product_register 단계용.
    # 비워두면 product_register 단계를 skip.
    jira_base_url: str = "https://connecteve.atlassian.net"
    jira_email: str = ""
    jira_api_token: str = ""
    jira_key: str = "PMFM"
    # product_register 전체 타임아웃 (초). D8-7.
    product_register_timeout: float = 300.0
    # 표준 NodePort (gateway-infra-next에서 고정). install 스크립트의 [INFO] X URL
    # 출력 유무와 무관하게 항상 이 포트로 URL 구성됨. 환경별 차이 생기면 .env로 override.
    port_frontend: int = 8000
    port_temporal: int = 8001
    port_webpacs: int = 8002


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
        # site_register 단계에서 받은 토큰 캐시. product_register에서 재사용 (D8-1).
        self._site_token: str | None = None

    async def run(self, job: Job) -> Job:
        async with connect(self.db_path) as db:
            await self._prepare(db, job)
            try:
                await self._execute(db, job)
                await self._mark_success(db, job)
            except WorkflowError as err:
                await self._mark_failure(db, job, err)
            except asyncio.CancelledError:
                # 외부에서 task.cancel() 호출됨 (Slack의 @autodeploy cancel <id>).
                # DB·Slack 모두 정리한 뒤 그대로 re-raise해서 task가 정상 cancel 상태로 끝나게.
                await self._mark_cancelled(db, job)
                raise
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
                # runtime Job 객체에도 반영해야 success_summary가 "-" 대신 실제 SHA를 표시.
                # DB만 업데이트하면 notifier가 받는 Job 인스턴스의 script_commit_sha는 None.
                job.script_commit_sha = sha

                await self._step_script(db, job, ssh, Step.INFRA_INSTALL, deployment.infra)
                await self._step_script(db, job, ssh, Step.APP_INSTALL, deployment.app)
                await self._step_healthcheck(db, job, ssh)
                await self._step_site_register(db, job)
                await self._step_dicom_gateway_restart(db, job, ssh)
        except SSHError as exc:
            raise WorkflowError(Step.SSH_CONNECT, str(exc)) from exc

        await self._step_product_register(db, job)

    async def _step_git_pull(self, db, job: Job, ssh: SSHClient) -> str:
        await self._step_start(db, job, Step.GIT_PULL)
        start = time.monotonic()

        # universe 저장소를 먼저 활성화. setup-*.sh가 내부에서 awscli 같은 universe-only
        # 패키지를 설치하려다 'no installation candidate'로 실패하는 것을 선제 차단.
        log_collector = self._make_log_collector(db, job, Step.GIT_PULL)
        rc = await ensure_universe_enabled(
            ssh, sudo_password=self.cfg.sudo_password, on_line=log_collector,
        )
        if rc != 0:
            duration = time.monotonic() - start
            await self._step_done(db, job, Step.GIT_PULL, success=False, duration=duration)
            raise WorkflowError(
                Step.GIT_PULL,
                f"apt universe 저장소 활성화 실패 (exit {rc}). "
                f"`ssh connecteve@{job.target_ip}` 접속 후 "
                f"`sudo add-apt-repository -y universe && sudo apt-get update` 수동 실행 필요.",
            )

        # 사전 도구 검증 — 누락 시 자동 설치 시도 후 재검증. 봇이 이미 setup-*.sh에 sudo를
        # 쓰고 있어 단일 패키지 설치도 같은 권한 모델에서 일관성 있게 동작.
        missing = await check_target_tools(ssh)
        if missing:
            rc = await install_missing_tools(
                ssh, missing,
                sudo_password=self.cfg.sudo_password,
                on_line=log_collector,
            )
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
        # infra/app 단계 모두에서 URL 캡처 — on-premise는 인프라 스크립트가 URL을 출력하기도 함.
        # stdout+stderr 양쪽 모두 수집 ([INFO] 줄이 stderr로 가는 스크립트도 있음).
        capture: list[str] | None = (
            [] if step in (Step.INFRA_INSTALL, Step.APP_INSTALL) else None
        )
        rc = await run_script(
            ssh,
            workdir=self.cfg.work_dir,
            spec=spec,
            code=job.hospital_code,
            sudo_password=self.cfg.sudo_password,
            on_line=self._make_log_collector(db, job, step, capture=capture),
        )
        duration = time.monotonic() - start
        if rc != 0:
            await self._step_done(db, job, step, success=False, duration=duration)
            raise WorkflowError(step, f"script exited with {rc}")
        if capture is not None:
            new_urls = extract_urls_from_lines(job.target_ip, capture)
            if new_urls:
                # 단계 별로 병합. 같은 라벨이면 나중 단계(app)가 인프라 단계 값을 덮음.
                job.extra_urls = {**(job.extra_urls or {}), **new_urls}
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

    async def _step_site_register(self, db, job: Job) -> None:
        """설치 직후 frontend(site-management) API로 자동 병원 등록.

        on-premise와 hybrid(with-ai/without-ai) 모두 자동 등록. 차이는 base URL과
        Host 헤더 처리뿐.

        skip 조건 (조용히 단계 자체를 건너뜀, 부분 실행 흔적 없음):
        - site_admin_email/password가 비어있음 — 운영자 옵션-인 (.env 안 채움)

        실패 조건 (WorkflowError로 작업 전체 실패):
        - on-premise인데 Frontend URL 미캡쳐로 base URL 결정 불가
        - sign-in 실패 (계정 오류, 네트워크 등)
        - sites 등록 실패 (멱등 케이스인 409/duplicate는 성공으로 간주)
        """
        if not self.cfg.site_admin_email or not self.cfg.site_admin_password:
            return

        await self._step_start(db, job, Step.SITE_REGISTER)
        start = time.monotonic()

        try:
            base_url, host_header = self._resolve_site_api_endpoint(job)
        except WorkflowError:
            duration = time.monotonic() - start
            await self._step_done(db, job, Step.SITE_REGISTER, success=False, duration=duration)
            raise

        installation_type = INSTALLATION_TYPE_MAP.get(
            job.deployment_type, job.deployment_type
        )
        display_name = job.hospital_name or job.hospital_code

        try:
            result, token = await site_registration.register_hospital(
                base_url,
                email=self.cfg.site_admin_email,
                password=self.cfg.site_admin_password,
                code=job.hospital_code,
                display_name=display_name,
                address=job.hospital_address or "",
                installation_type=installation_type,
                host_header=host_header,
                api_env=self.cfg.site_api_env,
            )
            # D8-1: 로그인 토큰을 인스턴스 변수에 캐싱 → product_register가 재사용
            self._site_token = token
        except SiteAPIError as exc:
            duration = time.monotonic() - start
            await self._step_done(db, job, Step.SITE_REGISTER, success=False, duration=duration)
            raise WorkflowError(Step.SITE_REGISTER, str(exc)) from exc

        duration = time.monotonic() - start
        msg = "이미 등록됨 (멱등)" if result == "already_exists" else "신규 등록"
        await repo.add_event(db, job.id, Step.SITE_REGISTER.value, "info", msg)
        await self._step_done(db, job, Step.SITE_REGISTER, success=True, duration=duration)

    async def _step_dicom_gateway_restart(self, db, job: Job, ssh: SSHClient) -> None:
        """dicom-gateway pod 강제 재시작. 실패해도 작업 전체는 SUCCEEDED.

        설치 직후 dicom-gateway가 Consul KV/Vault 시크릿 최신값을 못 잡고 있는
        케이스가 흔해서 한 번 강제 재기동. delete pod 방식 — deployment 컨트롤러가
        새 pod를 즉시 생성. 실패는 운영자가 수동으로 처리할 수 있으므로 raise하지
        않고 경고만 남김 (작업 자체는 SUCCEEDED 유지).
        """
        await self._step_start(db, job, Step.DICOM_GATEWAY_RESTART)
        start = time.monotonic()
        rc = await ssh.exec(
            "kubectl -n hub delete pod -l app=dicom-gateway",
            on_line=self._make_log_collector(db, job, Step.DICOM_GATEWAY_RESTART),
        )
        duration = time.monotonic() - start
        success = rc == 0
        await self._step_done(db, job, Step.DICOM_GATEWAY_RESTART, success=success, duration=duration)
        if not success:
            await repo.add_event(
                db, job.id, Step.DICOM_GATEWAY_RESTART.value, "warn",
                f"kubectl delete pod 실패 (exit {rc}). 수동 재시작: "
                f"`ssh connecteve@{job.target_ip} 'kubectl -n hub delete pod -l app=dicom-gateway'`",
            )

    async def _step_product_register(self, db, job: Job) -> None:
        """Jira 이슈 검색 → Lookup ID 조회 → Product POST. 부분 실패 허용.

        skip 조건 (조용히 단계 자체를 건너뜀, Slack 알림 없음):
        - JIRA_EMAIL 또는 JIRA_API_TOKEN 비어있음
        - site_admin 자격증명 비어있음 (토큰 없어서 Lookup API 호출 불가)

        실패해도 job 전체는 SUCCEEDED 유지 (dicom_gateway_restart 패턴).
        """
        if not self.cfg.jira_email or not self.cfg.jira_api_token:
            return
        if not self.cfg.site_admin_email or not self.cfg.site_admin_password:
            return

        await self._step_start(db, job, Step.PRODUCT_REGISTER)
        start = time.monotonic()

        async def _event(level: str, message: str) -> None:
            await repo.add_event(db, job.id, Step.PRODUCT_REGISTER.value, level, message)

        hospital_display_name = job.hospital_name or job.hospital_code
        jira = JiraClient(
            base_url=self.cfg.jira_base_url,
            email=self.cfg.jira_email,
            api_token=self.cfg.jira_api_token,
            project_key=self.cfg.jira_key,
        )

        try:
            inner_result = await asyncio.wait_for(
                self._run_product_register_inner(
                    job, jira, hospital_display_name, _event
                ),
                timeout=self.cfg.product_register_timeout,
            )
        except asyncio.TimeoutError:
            duration = time.monotonic() - start
            await _event(
                "error",
                f"product_register 단계 타임아웃 ({self.cfg.product_register_timeout:.0f}s 초과)",
            )
            await self._step_done(
                db, job, Step.PRODUCT_REGISTER, success=False, duration=duration
            )
            return

        duration = time.monotonic() - start
        if inner_result is None:
            # Jira 검색 실패 / 이슈 0건 / 토큰 없음 등 → step failure
            await self._step_done(
                db, job, Step.PRODUCT_REGISTER, success=False, duration=duration
            )
            return

        success_count, fail_count = inner_result
        success = success_count > 0 or fail_count == 0
        await self._step_done(
            db, job, Step.PRODUCT_REGISTER, success=success, duration=duration
        )
        if fail_count > 0 and success_count == 0:
            await _event(
                "warn",
                f"제품 등록 전부 실패 ({success_count + fail_count}건 중 {fail_count}건 실패)",
            )

    async def _run_product_register_inner(
        self,
        job: Job,
        jira: JiraClient,
        hospital_display_name: str,
        _event,
    ) -> tuple[int, int] | None:
        """product_register 실제 로직. asyncio.wait_for로 감싸기 위해 분리.

        step failure 원인을 _event로 기록하고 None 반환.
        성공(부분 포함) 시 (success_count, fail_count) 반환.
        _step_done은 호출하지 않음 — caller가 처리.
        """
        try:
            issues = await jira.search_issues(hospital_display_name)
        except JiraAPIError as exc:
            await _event("error", f"Jira 검색 실패: {exc}")
            return None

        if not issues:
            await _event(
                "warn",
                f"Jira 이슈를 찾지 못함 (병원: {hospital_display_name})",
            )
            return None

        token = self._site_token
        if not token:
            await _event("error", "site_token 없음 — site_register 단계 토큰 캐싱 실패")
            return None

        base_url, host_header = self._resolve_site_api_endpoint(job)
        client = ProductRegistrationClient(
            base_url=base_url,
            token=token,
            api_env=self.cfg.site_api_env,
            host_header=host_header,
        )

        success_count, fail_count = await client.register_products(
            job, issues, on_event=_event
        )
        return success_count, fail_count

    def _resolve_site_api_endpoint(self, job: Job) -> tuple[str, str | None]:
        """(base_url, host_header_override) — deployment_type별 분기.

        on-premise: target_ip + 고정 frontend 포트(cfg.port_frontend, 기본 8000).
                    Traefik Host 라우팅용으로 'localhost' 헤더 강제.
        hybrid:    클라우드 base URL 그대로. Host 헤더는 URL에서 자동.
        """
        if job.deployment_type == "on-premise":
            base_url = f"http://{job.target_ip}:{self.cfg.port_frontend}"
            return base_url, self.cfg.site_host_header_onpremise
        return self.cfg.site_cloud_base_url, None

    async def register_existing_job(self, job: Job) -> str:
        """site_register 단계만 단독 실행 — 기존 작업의 데이터 재사용.

        반환: 'created' | 'already_exists'
        실패 시: WorkflowError 또는 SiteAPIError가 그대로 raise.

        전체 워크플로와 달리:
        - job.status는 건드리지 않음 (DB의 jobs.status는 그대로 유지)
        - notifier로 step 이벤트 발행하지 않음 (호출자가 결과 메시지 직접 전송)
        - DB에는 'manual site_register' 이벤트만 한 줄 남김
        """
        if not self.cfg.site_admin_email or not self.cfg.site_admin_password:
            raise SiteAPIError(
                "site_admin 자격증명이 비어있음 — .env의 SITE_ADMIN_EMAIL/PASSWORD 확인"
            )
        base_url, host_header = self._resolve_site_api_endpoint(job)
        installation_type = INSTALLATION_TYPE_MAP.get(
            job.deployment_type, job.deployment_type
        )
        display_name = job.hospital_name or job.hospital_code

        result, _token = await site_registration.register_hospital(
            base_url,
            email=self.cfg.site_admin_email,
            password=self.cfg.site_admin_password,
            code=job.hospital_code,
            display_name=display_name,
            address=job.hospital_address or "",
            installation_type=installation_type,
            host_header=host_header,
            api_env=self.cfg.site_api_env,
        )
        msg = "수동 재시도: 이미 등록됨 (멱등)" if result == "already_exists" else "수동 재시도: 신규 등록"
        async with connect(self.db_path) as db:
            await repo.add_event(db, job.id, Step.SITE_REGISTER.value, "info", msg)
        return result

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
            # capture는 URL 추출용. 일부 스크립트는 [INFO] 정보 로그를 stderr로 출력하므로
            # 양쪽 stream 모두 받음. 토큰 마스킹은 이미 위에서 적용됨.
            if capture is not None:
                capture.append(masked.line)
            await repo.add_script_log(db, job.id, step.value, masked.stream, masked.line)
            await self.notifier.step_log(job, step, masked)
        return collect

    async def _mark_success(self, db, job: Job) -> None:
        # 표준 3개 URL은 항상 채움 — install 스크립트가 [INFO] X URL을 안 찍어도
        # 고정 NodePort 기준 결정적. 이미 캡쳐된 값이 있으면 그대로 둠 (운영자가
        # 일부러 다른 라벨/포트를 추가 출력한 경우 존중).
        defaults = {
            "Frontend": f"http://{job.target_ip}:{self.cfg.port_frontend}/",
            "Temporal Web": f"http://{job.target_ip}:{self.cfg.port_temporal}/",
            "Web-PACS": f"http://{job.target_ip}:{self.cfg.port_webpacs}/",
        }
        merged = {**defaults, **(job.extra_urls or {})}
        job.extra_urls = merged
        admin_url = merged["Frontend"]
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

    async def _mark_cancelled(self, db, job: Job) -> None:
        """사용자 요청으로 작업이 취소됐을 때 정리. DB·이벤트·Slack 알림."""
        step_label = job.current_step.value if job.current_step else "?"
        await repo.add_event(db, job.id, step_label, "warn", "cancelled by user")
        await repo.finish_job(db, job.id, JobStatus.CANCELLED)
        job.status = JobStatus.CANCELLED
        await self.notifier.job_finished(job, error=None)
