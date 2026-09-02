"""hubctl 명령 조립 + 서브프로세스 실행 + 취소 (dev-spec-web-console §F3).

## 왜 `zsh -lc` 로 감싸는가

launchd 로 뜨는 데몬은 `~/.zshrc` 를 읽지 않는다. hubctl 은 `VAULT_ADDR`·
`VAULT_TOKEN`·`HUB_DEPLOY_GIT_TOKEN`·AWS 자격을 환경에서 읽으므로, 로그인 셸을
거쳐야 사람이 터미널에서 실행할 때와 같은 환경이 된다. 대신 명령 전체가 셸
문자열이 되므로 **모든 인자에 `shlex.quote`** 를 건다.

## 왜 clean 만 hubctl 을 우회하는가

`bin/hubctl` 의 `cmd_clean()` 은 호스트명을 `read -r` 로 직접 받고, 못 읽으면
exit 2 로 죽는다. `-y` 도 명시적으로 거부한다(파괴적 작업이라 일부러 그렇게 만들었다).
`clean.yml` 헤더 주석이 아래 직접 호출 형태를 정식 사용법으로 문서화하고 있으므로
그것을 그대로 쓴다. 웹이 이미 호스트명 타이핑 확인을 받고, 그 값을 `confirm=` 으로
넘긴다 — 확인 절차가 사라지는 게 아니라 터미널에서 웹으로 옮겨올 뿐이다.

## 왜 patch 는 원샷(`hubctl patch`)을 쓰는가

`patch create` 와 원샷은 **같은 일을 하지 않는다.** `patch-create.yml` 헤더가
그렇게 적고 있다.

| | 원샷 `hubctl patch` | `hubctl patch create` |
|---|---|---|
| 어디서 | 타겟에서 (become 필요) | 컨트롤러 로컬 |
| base | 지금 깔려 있는 live release-config | `-e patch_base=<ref>` 로 준 git ref |
| 용도 | 온라인 | 폐쇄망 반입용 |

우리 병원들은 온라인이므로 원샷이 맞다. `patch create` 는 `-e
patch_create_hosts=localhost` 를 **run_playbook 인자로 하드코딩**하는데
(`bin/hubctl` cmd_patch), 그것이 passthrough 뒤에 붙어 나중 `-e` 가 이기므로
밖에서 덮을 수 없다 — 원샷 말고는 온라인 create 로 가는 길이 없다.

create/apply 2단계 조립도 남겨둔다. 폐쇄망 반입은 여전히 그 길이고,
`approve()` 가 `phase="apply"` 로 쓴다. `patch apply` 에는 `-e hub_deploy_ref`
를 넘기지 않는다 — `roles/patch_apply/tasks/main.yml:180` 이 번들 메타를 SoT 로
보고 불일치하면 죽인다.

## 원샷 중간의 `[y/N]`

원샷은 create 를 끝내고 `read -r` 로 적용 여부를 묻는다. 콘솔은 시작 전에 사람이
확인한 뒤 실행하므로 여기에 `y` 를 자동으로 넣는다 (`PATCH_CONFIRM_MARKER`).

프롬프트 줄(`... [y/N] `) 자체는 **개행이 없어** 줄 단위 펌프에 잡히지 않는다.
개행으로 끝나는 바로 앞 줄을 신호로 쓴다.

## become 비밀번호

`-K`(대화형)를 못 쓰므로 `ANSIBLE_BECOME_PASSWORD_FILE` 로 넘긴다.
ansible 소스(`cli/__init__.py::get_password_from_file`) 확인 결과:

- 파일에 **실행 권한이 있으면 스크립트로 실행**해 stdout 을 비밀번호로 쓴다
  → 0600(실행 비트 없음)은 기밀성뿐 아니라 정확성 문제다.
- 아니면 읽어서 `.strip()` → 끝의 개행은 알아서 제거된다.
- 빈 값이면 에러 → 비밀번호가 없으면 파일 자체를 만들지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from autodeploy.ansible_log import AnsibleLogParser, HostRecap, ParsedLine
from autodeploy.inventory import is_valid_host
from autodeploy.masking import SecretMasker
from autodeploy.models import JobKind

log = logging.getLogger(__name__)

ENVS: tuple[str, ...] = ("dev", "stage", "prod")
REF_TYPES: tuple[str, ...] = ("branch", "tag", "commit")

# clean_mode → (ansible level, keep_data). runbook §3-2 의 3가지 초기화 방식.
CLEAN_MODES: dict[str, tuple[str, str]] = {
    "reset": ("reset", "false"),        # 클러스터 + /data 삭제 (재설치용)
    "reset-keep": ("reset", "true"),    # 클러스터만 삭제, /data 보존
    "uninstall": ("uninstall", "false"),  # bootstrap 산출물까지 제거 (반납)
}

DEFAULT_INVENTORY = "inventory/sites.yml"
DEFAULT_REPO_PATH = "~/hub-provisioning"

# 한 줄이 이보다 길면 잘라서 흘린다. ansible 은 실패 태스크의 인자를 통째로
# 되뱉기 때문에 base64 페이로드 한 줄이 수 MB 가 될 수 있고, 그대로 두면
# 리더 버퍼가 무한히 자란다.
MAX_LINE_BYTES = 64 * 1024
_TRUNCATED = " …[잘림]"


class HubctlError(RuntimeError):
    """명령 조립 단계의 잘못된 입력."""


# ── 명령 조립 ────────────────────────────────────────────────────────

# 원샷 `hubctl patch` 가 적용 여부를 묻기 **직전에** 내는 줄 (bin/hubctl cmd_patch).
# 프롬프트 자체는 개행이 없어 못 잡으므로 이 줄을 신호로 쓴다.
# 설치 직전에 컨트롤러의 hub-provisioning 을 맞출 브랜치.
SYNC_BRANCH = "main"

PATCH_CONFIRM_MARKER = "이번에 적용될 내용입니다"
PATCH_CONFIRM_ANSWER = "y\n"


def _check_hosts(hosts: Sequence[str], *, required: bool = True) -> tuple[str, ...]:
    clean = tuple(h.strip() for h in hosts if h and h.strip())
    if required and not clean:
        raise HubctlError("대상 호스트가 비었습니다")
    if len(set(clean)) != len(clean):
        raise HubctlError(f"중복된 호스트: {clean}")
    for host in clean:
        if not is_valid_host(host):
            raise HubctlError(f"호스트명으로 쓸 수 없습니다: {host!r}")
    return clean


def _check_choice(value: str | None, allowed: Sequence[str], label: str) -> str:
    if value not in allowed:
        raise HubctlError(f"{label} 는 {'|'.join(allowed)} 중 하나여야 합니다 (받은 값: {value!r})")
    return value


def _passthrough(ref: str | None, ref_type: str | None) -> list[str]:
    """`--` 뒤로 ansible-playbook 에 그대로 넘어가는 추가 변수."""
    if not ref:
        if ref_type:
            raise HubctlError("ref 없이 ref_type 만 지정할 수 없습니다")
        return []
    if "\n" in ref or not ref.strip():
        raise HubctlError(f"잘못된 ref: {ref!r}")
    args = ["--", "-e", f"hub_deploy_ref={ref.strip()}"]
    if ref_type:
        _check_choice(ref_type, REF_TYPES, "ref_type")
        # branch 는 roles 기본값이라 생략해도 같지만, 태그를 branch 로 두면
        # patch_create 가 경고를 띄우므로 지정된 값은 그대로 넘긴다.
        args += ["-e", f"hub_deploy_ref_type={ref_type}"]
    return args


def build_sync_command(branch: str = SYNC_BRANCH) -> str:
    """컨트롤러의 hub-provisioning 을 `origin/<branch>` 에 맞추는 명령.

    `git pull` 한 방으로 하지 않는 이유가 셋 있다.

    - **다른 브랜치에 있으면** pull 이 조용히 그 브랜치를 당긴다. 설치는 main
      기준이라야 하므로 checkout 을 명시한다.
    - **로컬 커밋이 얹혀 있으면** pull 이 병합 커밋을 만들어 버린다. `--ff-only`
      면 대신 멈춘다 — 컨트롤러가 baseline 에서 벗어났다는 뜻이라 사람이 봐야 한다.
    - fetch·checkout·merge 중 **어디서 깨졌는지**가 로그에 남는다.

    마지막 줄은 어떤 버전으로 설치했는지를 기록으로 남기기 위한 것이다.
    `inventory/sites.yml` 은 저장소의 `.gitignore` 에 있어 이 명령이 건드리지 않는다.
    """
    b = shlex.quote(branch)
    fmt = shlex.quote(f"hub-provisioning {branch} @ %h  %ad  %s")
    return (
        f"git fetch origin {b}"
        f" && git checkout {b}"
        f" && git merge --ff-only origin/{b}"
        f" && git --no-pager log -1 --date=short --format={fmt}"
    )


def build_command(
    kind: JobKind | str,
    *,
    hosts: Sequence[str] = (),
    env: str | None = None,
    ref: str | None = None,
    ref_type: str | None = None,
    clean_mode: str | None = None,
    phase: str | None = None,
    inventory: str = DEFAULT_INVENTORY,
) -> str:
    """작업 하나를 실행할 셸 명령 문자열. `HUBCTL_REPO_PATH` 에서 실행되는 전제.

    `-l` 은 전체 선택이어도 항상 호스트를 전부 나열한다. 생략하면 "실행 시점의
    인벤토리 전체"가 대상이 되어, 작업 생성 후 누가 서버를 추가하면 의도치 않은
    서버까지 딸려 들어간다.
    """
    kind = JobKind(kind)
    argv: list[str]

    if kind in (JobKind.INSTALL, JobKind.CONFIGURE):
        targets = _check_hosts(hosts)
        _check_choice(env, ENVS, "env")
        argv = ["./bin/hubctl", kind.value, "-e", env, "-l", ",".join(targets)]
        argv += _passthrough(ref, ref_type)

    elif kind is JobKind.VERIFY:
        targets = _check_hosts(hosts)
        argv = ["./bin/hubctl", "verify", "-l", ",".join(targets)]

    elif kind is JobKind.ROLLBACK:
        targets = _check_hosts(hosts)
        # hubctl 이 `-e ENV` 를 명시적으로 거부한다 (cmd_rollback).
        argv = ["./bin/hubctl", "rollback", "-l", ",".join(targets)]

    elif kind is JobKind.PATCH:
        if phase is None:
            # 원샷 — 타겟에서 번들을 만들고(base=live release-config) 그대로 적용한다.
            targets = _check_hosts(hosts)
            if not ref:
                raise HubctlError("patch 에는 ref 가 필요합니다")
            argv = ["./bin/hubctl", "patch", "-l", ",".join(targets)]
            argv += _passthrough(ref, ref_type)
        elif phase == "create":
            # hubctl 이 `patch create` 에 -l 을 거부한다 — 컨트롤러 로컬 실행이다.
            if _check_hosts(hosts, required=False):
                raise HubctlError("patch create 는 -l 을 받지 않습니다 (컨트롤러 로컬 실행)")
            if not ref:
                raise HubctlError("patch create 에는 ref 가 필요합니다")
            argv = ["./bin/hubctl", "patch", "create"] + _passthrough(ref, ref_type)
        elif phase == "apply":
            targets = _check_hosts(hosts)
            # ref 를 넘기지 않는다 — 번들 메타가 SoT 라 불일치 시 playbook 이 죽는다.
            argv = ["./bin/hubctl", "patch", "apply", "-l", ",".join(targets)]
        else:
            raise HubctlError(
                f"patch 의 phase 는 None(원샷)·'create'·'apply' 중 하나여야 합니다 (받은 값: {phase!r})"
            )

    elif kind is JobKind.CLEAN:
        targets = _check_hosts(hosts)
        if len(targets) != 1:
            # confirm= 이 호스트명 하나와 대조되므로 여러 대를 한 번에 못 지운다.
            # 이 제약은 사고 방지 장치라 우회하지 않는다.
            raise HubctlError(f"clean 은 한 번에 한 대만 가능합니다 (받은 값: {list(targets)})")
        host = targets[0]
        level, keep_data = CLEAN_MODES[_check_choice(clean_mode, tuple(CLEAN_MODES), "clean_mode")]
        argv = [
            "ansible-playbook", "clean.yml",
            "-i", inventory,
            "-l", host,
            "-e", f"confirm={host}",
            "-e", f"level={level}",
            "-e", f"keep_data={keep_data}",
        ]

    else:  # JobKind.SSH_KEY 는 asyncssh 로 처리한다 (ssh_keys.py).
        raise HubctlError(f"hubctl 로 실행하지 않는 작업입니다: {kind.value}")

    return shlex.join(argv)


def build_preflight_command() -> str:
    """대시보드 자격 점검 스트립용. 작업(jobs)으로 기록하지 않는 읽기 전용 호출."""
    return shlex.join(["./bin/hubctl", "preflight"])


# ── 실행 ────────────────────────────────────────────────────────────

@dataclass(slots=True)
class RunResult:
    exit_code: int
    cancelled: bool = False
    recaps: dict[str, HostRecap] = field(default_factory=dict)
    steps: tuple[str, ...] = ()
    line_count: int = 0

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.cancelled


LineHandler = Callable[[str, ParsedLine], object]


class HubctlRunner:
    """명령 하나를 실행하고 줄을 흘려보낸다. 인스턴스는 실행 1회당 1개.

    `on_line(stream, parsed)` 로 (stdout|stderr, 파싱 결과)를 넘긴다.
    코루틴을 돌려주면 await 한다.
    """

    def __init__(
        self,
        repo_path: str | Path = DEFAULT_REPO_PATH,
        *,
        become_password: str | None = None,
        masker: SecretMasker | None = None,
        env_overrides: Mapping[str, str] | None = None,
        login_shell: Sequence[str] = ("zsh", "-lc"),
        kill_grace: float = 10.0,
    ) -> None:
        self._repo_path = Path(repo_path).expanduser()
        self._become_password = become_password or None
        # become 비밀번호는 호출자가 잊더라도 반드시 마스킹 대상이다. ansible 은
        # 실패한 태스크의 인자를 통째로 되뱉기 때문에 로그로 샐 경로가 실재한다.
        secrets = list(masker.values) if masker is not None else []
        if become_password:
            secrets.append(become_password)
        self._masker = SecretMasker(secrets)
        self._env_overrides = dict(env_overrides or {})
        self._login_shell = tuple(login_shell)
        self._kill_grace = kill_grace
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self._escalation: asyncio.Task[None] | None = None
        self.parser = AnsibleLogParser()

    # -- 상태 조회 --

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    # -- 실행 --

    async def run(
        self,
        command: str,
        *,
        on_line: LineHandler | None = None,
        auto_reply: tuple[str, str] | None = None,
        on_reply=None,
    ) -> RunResult:
        """`auto_reply=(마커, 응답)` 이면 마커가 든 줄을 처음 볼 때 응답을 stdin 에 쓴다.

        원샷 `hubctl patch` 가 중간에 `read -r` 로 묻는 것을 답하기 위한 것이다.
        **한 번만** 쓴다 — 같은 문구가 또 나와도 다시 답하지 않는다.
        `on_reply()` 는 실제로 쓴 뒤에 불린다 (기록용).
        """
        if self._proc is not None:
            raise HubctlError("HubctlRunner 는 재사용할 수 없습니다 (실행 1회당 1개)")
        if not self._repo_path.is_dir():
            raise HubctlError(f"hubctl 저장소 경로가 없습니다: {self._repo_path}")

        pw_file = self._write_become_file()
        steps: list[str] = []
        count = 0
        try:
            if self._cancelled:
                # 기동 직전에 취소가 들어왔다. 프로세스를 띄우지 않는다.
                return RunResult(exit_code=-1, cancelled=True)

            proc = await asyncio.create_subprocess_exec(
                *self._login_shell,
                command,
                cwd=str(self._repo_path),
                # stdin 을 열어둔다. 원샷 patch 는 여기로 답을 받는다. 파이프를
                # 안 열면 자식이 부모의 stdin(launchd 에선 /dev/null)을 물려받아
                # `read` 가 즉시 EOF 를 받고 "사용자 취소" 로 끝난다.
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(pw_file),
                # 프로세스 그룹을 따로 만든다. 취소할 때 zsh 만 죽이면 ansible 이
                # 고아로 남아 계속 서버를 건드리므로, 그룹 통째로 신호를 보낸다.
                start_new_session=True,
            )
            self._proc = proc
            if auto_reply is None:
                # 답할 것이 없으면 곧바로 닫는다. 열어둔 채 두면 stdin 을 읽는
                # 명령(`hubctl clean` 의 호스트명 확인 등)이 영영 기다린다 —
                # 예전처럼 EOF 를 받고 제 안내문과 함께 죽는 편이 낫다.
                self._close_stdin()
            if self._cancelled:
                # `cancel()` 이 기동과 겹쳐 들어왔다면 그때는 self._proc 이 None 이라
                # 신호를 못 보냈다. 여기서 다시 확인해 놓친 취소를 집행한다.
                await self.cancel()

            pending_reply = auto_reply

            async def pump(stream: asyncio.StreamReader, name: str) -> None:
                nonlocal count, pending_reply
                async for raw in _iter_lines(stream):
                    text = self._masker(raw.decode("utf-8", errors="replace"))
                    parsed = self.parser.feed(text)
                    count += 1
                    if pending_reply is not None and pending_reply[0] in text:
                        answer = pending_reply[1]
                        pending_reply = None   # 한 번만 답한다
                        await self._reply(answer)
                        if on_reply is not None:
                            result = on_reply(answer)
                            if asyncio.iscoroutine(result):
                                await result
                    if parsed.step_started and parsed.step and parsed.step not in steps:
                        steps.append(parsed.step)
                    if on_line is not None:
                        result = on_line(name, parsed)
                        if asyncio.iscoroutine(result):
                            await result

            assert proc.stdout is not None and proc.stderr is not None
            await asyncio.gather(
                pump(proc.stdout, "stdout"),
                pump(proc.stderr, "stderr"),
            )
            # 자식이 stdout·stderr 를 닫았다 = 끝나는 중이다. 여기서 stdin 도
            # 닫는다 — 아직 무언가를 기다리고 있다면 EOF 를 보고 빠져나가라는 뜻.
            self._close_stdin()
            exit_code = await proc.wait()
        finally:
            if self._escalation is not None:
                self._escalation.cancel()
                with suppress(asyncio.CancelledError):
                    await self._escalation
            self._close_stdin()
            _shred(pw_file)

        return RunResult(
            exit_code=exit_code,
            cancelled=self._cancelled,
            recaps=dict(self.parser.recaps),
            steps=tuple(steps),
            line_count=count,
        )

    async def cancel(self) -> bool:
        """프로세스 그룹에 SIGTERM. 유예 후에도 살아있으면 SIGKILL.

        다중 호스트 작업은 프로세스가 하나이므로 전부 중단된다 (개별 취소 불가).
        """
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return False
        if not _signal_group(pgid, signal.SIGTERM):
            return False
        log.warning("작업 취소 요청 — SIGTERM (pgid=%d)", pgid)
        if self._escalation is None:
            self._escalation = asyncio.create_task(self._escalate(proc, pgid))
        return True

    async def _escalate(self, proc: asyncio.subprocess.Process, pgid: int) -> None:
        try:
            await asyncio.wait_for(proc.wait(), self._kill_grace)
        except TimeoutError:
            log.warning("SIGTERM 후 %.0f초 미종료 → SIGKILL (pgid=%d)", self._kill_grace, pgid)
            _signal_group(pgid, signal.SIGKILL)

    # -- 환경 --

    async def _reply(self, answer: str) -> None:
        """자식의 stdin 에 한 줄 쓴다. 이미 닫혔으면 조용히 넘어간다."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return
        try:
            proc.stdin.write(answer.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            # 답하려는 순간 자식이 죽었다. 로그로 남기지 않는다 — 종료 코드가 말한다.
            log.debug("stdin 응답 실패 (자식이 이미 종료)")
        # 한 번 답하면 더 받을 것이 없다. 닫아서 그 뒤의 read 는 EOF 를 보게 한다.
        self._close_stdin()

    def _close_stdin(self) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return
        with suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
            proc.stdin.close()

    def _child_env(self, pw_file: Path | None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._env_overrides)
        if pw_file is not None:
            env["ANSIBLE_BECOME_PASSWORD_FILE"] = str(pw_file)
        # 색을 받지 않는다. 평문을 접두사로 분류하고 색은 웹에서 입힌다.
        env["NO_COLOR"] = "1"
        env.pop("ANSIBLE_FORCE_COLOR", None)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _write_become_file(self) -> Path | None:
        if not self._become_password:
            return None
        fd, name = tempfile.mkstemp(prefix="autodeploy-become-", suffix=".txt")
        try:
            # 실행 비트가 있으면 ansible 이 스크립트로 실행해버린다. 0600 필수.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._become_password + "\n")
        except BaseException:
            with suppress(OSError):
                os.unlink(name)
            raise
        return Path(name)


def _shred(path: Path | None) -> None:
    """become 비밀번호 파일 제거. 실패해도 작업 결과를 가리지 않는다."""
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("become 비밀번호 파일 삭제 실패: %s", path)


def _signal_group(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        log.error("프로세스 그룹에 신호를 보낼 수 없습니다 (pgid=%d, sig=%s)", pgid, sig.name)
        return False
    return True


async def _iter_lines(
    stream: asyncio.StreamReader, *, max_bytes: int = MAX_LINE_BYTES
) -> AsyncIterator[bytes]:
    """줄 단위로 흘린다.

    `StreamReader.readline()` 을 쓰지 않는 이유: 기본 64KB 리미트를 넘는 줄에서
    `LimitOverrunError` 를 던져 스트림이 통째로 죽는다. ansible 은 실패한 태스크의
    인자를 통째로 되뱉기 때문에 그런 줄이 실제로 나온다. 여기서는 넘치면 잘라서
    계속 흘린다 — 로그 한 줄 때문에 작업 추적을 잃는 것이 더 나쁘다.
    """
    buf = bytearray()
    truncating = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            if not truncating:
                yield bytes(buf[:idx])
            truncating = False
            del buf[: idx + 1]
        if len(buf) > max_bytes:
            if not truncating:
                yield bytes(buf[:max_bytes]) + _TRUNCATED.encode()
                truncating = True
            buf.clear()
    if buf and not truncating:
        yield bytes(buf)
