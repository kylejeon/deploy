"""설치 킷 작업 하나를 끝까지 돌린다 — 서버 한 대, 작업 하나.

    kit_check   사전 점검  : 접속·sudo·OS·공간, 설치된 버전 → 쓸 킷을 고른다
    kit_fetch   킷 준비    : 빌드 PC → 맥미니 캐시 (LAN, 이어받기, 페이로드 sha256 확인)
    kit_send    전송       : 맥미니 → 타겟 /var/tmp/connevo-kits (rsync --partial, 이어받기)
                             + 타겟에서 `.run --check` (파일이 온전한지)
    kit_0~8     킷 실행    : 킷이 내는 `::step:: N/8 이름` 그대로
    kit_remove  삭제       : uninstall.sh (삭제 작업일 때만)
    kit_verify  검증       : VERSION 파일·웹 응답, 성공하면 타겟의 .run 을 지운다

## 왜 타겟에서 systemd-run 으로 띄우나

설치는 15~25분, 병원 PC 는 Tailscale 중계(DERP)를 탈 때가 많다. SSH 세션 안에서
그대로 돌리면 연결이 한 번 끊길 때 SIGHUP 으로 **설치가 중간에 죽는다** — 이미지
적재나 DB 마이그레이션 한가운데서. 그래서 설치는 타겟의 systemd 유닛으로 떼어
내고, 콘솔은 그 로그 파일을 따라 읽는다. 끊기면 다시 붙어 **읽은 줄 다음부터**
이어 읽는다. 설치 자체는 콘솔과 상관없이 끝까지 간다.

같은 이유로 **킷 실행이 시작된 뒤에는 취소하지 않는다.** 반쯤 깔린 게이트웨이가
가장 나쁜 상태다. 받기·보내기 중에는 취소된다 (서버는 바뀌지 않는다).

## 비밀값

- sudo 비밀번호: `sudo -S -p ''` 의 표준입력으로만 (명령줄·ps 에 안 보인다).
- 키트 비밀번호: 타겟에 0600 파일(`job-<id>.pw`)로 두고 킷이 끝나면 지운다.
  실패해도 finally 에서 한 번 더 지운다. 킷은 그 값을 출력하지 않는다.
- 초기 관리자 비밀번호는 킷이 화면·로그에 내지 않는다 (install-info.txt 에만).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from autodeploy.ansible_log import LineKind
from autodeploy.kits import Kit, KitCache, KitError, choose_kit, version_key
from autodeploy.ssh import SSHClient, StreamLine

log = logging.getLogger(__name__)

REMOTE_DIR = "/var/tmp/connevo-kits"
INSTALL_DIRS = ("/connevo-gateway", "/opt/connevo-gateway")
GIB = 1 << 30

# 킷의 종료 코드 (newhub deploy/kit/run-header.sh 의 무인 모드)
EXIT_MEANING: dict[int, str] = {
    0: "완료",
    1: "실패",
    2: "옵션 오류",
    3: "키트 비밀번호가 맞지 않습니다",
    4: "킷 파일이 손상되었습니다",
    5: "설치되어 있지 않습니다",
    6: "패치의 기준 버전이 설치된 버전과 다릅니다 — 전체 킷으로 업그레이드하세요",
    7: "다른 프로그램이 쓰는 포트가 있습니다",
    8: "공간이 부족합니다",
    9: "설치는 끝났지만 점검에 실패한 항목이 있습니다",
}

_STEP_RE = re.compile(r"^::step:: (?P<n>\d+)/\d+\s*(?P<name>.*)$")
_RESULT_RE = re.compile(r"^::result:: (?P<level>ok|warn|fail) (?P<code>-?\d+) ?(?P<msg>.*)$")
_EXIT_RE = re.compile(r"^::exit:: (?P<rc>-?\d+|lost)$")
_PERCENT_RE = re.compile(r"(\d{1,3})%")
# 1.2.0.2 이하 킷의 install.sh 는 설치 정보 끝에 초기 비밀번호를 그대로 찍는다.
# 새 킷은 찍지 않지만, 옛 킷 로그가 콘솔 DB·내려받은 로그에 남지 않게 여기서도 가린다.
_SECRET_LINES = (
    (re.compile(r"^(\s*관리자 초기 비밀번호[^:]*):.+$"), r"\1: (가림 — 서버의 install-info.txt)"),
    (re.compile(r"^(\s*Orthanc 웹: \S+)\s+\(.*\)\s*$"), r"\1  (계정: 서버의 install-info.txt)"),
)


def redact(text: str) -> str:
    if "sudo cat" in text:   # 새 킷이 이미 가린 줄
        return text
    for pattern, repl in _SECRET_LINES:
        text = pattern.sub(repl, text)
    return text

Emit = Callable[[str, str, LineKind], None]   # step, text, kind


@dataclass(frozen=True, slots=True)
class KitTarget:
    host: str           # 인벤토리 이름
    address: str        # ansible_host
    user: str           # ansible_user
    port: int = 22


@dataclass(frozen=True, slots=True)
class KitRequest:
    job_id: int
    action: str                 # install | uninstall-keep | uninstall-all
    version: str | None = None  # install 때 고른 버전
    addon: str | None = None    # 새 설치일 때만 쓰인다


@dataclass(slots=True)
class Probe:
    os: str = ""
    arch: str = ""
    mem_gb: int = 0
    tmp_free_kb: int = 0
    install_dir: str | None = None
    installed: str | None = None
    addon: str | None = None
    rsync: bool = False
    systemd_run: bool = False


@dataclass(slots=True)
class KitResult:
    status: str                         # succeeded | failed | cancelled
    exit_code: int | None = None
    error: str | None = None
    warn: str | None = None             # 성공이지만 확인할 것 (종료 코드 9)
    kit_name: str | None = None
    mode: str | None = None             # install | upgrade | patch | uninstall-keep | uninstall-all
    installed_before: str | None = None
    installed_after: str | None = None
    addon: str | None = None


class KitJobError(RuntimeError):
    """작업을 멈추는 이유. 사람에게 그대로 보여줄 문장."""

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def probe_command() -> str:
    dirs = " ".join(INSTALL_DIRS)
    return (
        ". /etc/os-release 2>/dev/null; echo \"OS=${ID:-?} ${VERSION_ID:-?}\"; "
        "echo \"ARCH=$(uname -m)\"; "
        "echo \"MEM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 ))\"; "
        f"mkdir -p -m 700 {REMOTE_DIR}; "
        f"echo \"TMP_FREE_KB=$(df -Pk {REMOTE_DIR} | awk 'NR==2{{print $4}}')\"; "
        f"for d in {dirs}; do if [ -f \"$d/docker-compose.yml\" ]; then "
        "echo \"INSTALL_DIR=$d\"; echo \"INSTALLED=$(cat \"$d/VERSION\" 2>/dev/null)\"; break; fi; done; "
        "command -v rsync >/dev/null && echo RSYNC=1; "
        "command -v systemd-run >/dev/null && echo SYSTEMD_RUN=1; true"
    )


def addon_command() -> str:
    """설치된 패키지. .env 가 root 전용(0600)이라 sudo 가 든다. sudo 확인도 겸한다."""
    dirs = " ".join(INSTALL_DIRS)
    script = (
        f"for d in {dirs}; do if [ -f \"$d/.env\" ]; then "
        "sed -n 's/^ADDON=//p' \"$d/.env\" | head -n1; break; fi; done; true"
    )
    return f"sudo -S -p '' sh -c {shlex.quote(script)}"


def parse_probe(lines: Sequence[str]) -> Probe:
    p = Probe()
    for raw in lines:
        key, _, value = raw.strip().partition("=")
        value = value.strip()
        if key == "OS":
            p.os = value
        elif key == "ARCH":
            p.arch = value
        elif key == "MEM_GB" and value.isdigit():
            p.mem_gb = int(value)
        elif key == "TMP_FREE_KB" and value.isdigit():
            p.tmp_free_kb = int(value)
        elif key == "INSTALL_DIR":
            p.install_dir = value or None
        elif key == "INSTALLED":
            p.installed = value or None
        elif key == "RSYNC":
            p.rsync = value == "1"
        elif key == "SYSTEMD_RUN":
            p.systemd_run = value == "1"
    return p


def classify(text: str) -> LineKind:
    s = text.strip()
    if s.startswith("::step::") or s.startswith("== ["):
        return LineKind.TASK
    if s.startswith("::result:: fail") or s.startswith("설치 실패") or s.startswith("실패:"):
        return LineKind.ERROR
    if s.startswith("::result:: warn") or s.startswith("! ") or text.startswith("  ! "):
        return LineKind.WARN
    if s.startswith("::result:: ok") or s.startswith("준비됨:") or s in ("설치 완료", "삭제 완료", "OK"):
        return LineKind.OK
    return LineKind.OUT


def job_paths(job_id: int) -> dict[str, str]:
    base = f"{REMOTE_DIR}/job-{job_id}"
    return {"pw": base + ".pw", "log": base + ".log", "rc": base + ".rc",
            "unit": f"connevo-kit-job{job_id}"}


def kit_inner_command(req: KitRequest, kit: Kit | None, mode: str, install_dir: str | None) -> str:
    """systemd 유닛이 돌릴 bash 스크립트. 출력은 job-<id>.log, 종료 코드는 .rc."""
    p = job_paths(req.job_id)
    if mode.startswith("uninstall"):
        flag = "--yes --keep-data" if mode == "uninstall-keep" else "--yes"
        d = shlex.quote(install_dir or INSTALL_DIRS[0])
        body = (
            "echo '::step:: 1/1 삭제'; "
            f"bash {d}/uninstall.sh {flag}; rc=$?; "
            "if [ $rc = 0 ]; then echo '::result:: ok 0 삭제했습니다'; "
            "else echo \"::result:: fail $rc 삭제 중 오류가 났습니다\"; fi; "
            f"echo $rc > {p['rc']}"
        )
        return f"{{ {body}; }} > {p['log']} 2>&1"
    assert kit is not None
    run = f"{REMOTE_DIR}/{kit.name}"
    if mode == "install":
        envs = f"KIT_PASSWORD_FILE={p['pw']} ADDON={shlex.quote(req.addon or 'suite')}"
        action = "--install"
    else:  # upgrade | patch — 설치된 패키지를 그대로 둔다 (ADDON 을 넘기지 않는다)
        envs = f"KIT_PASSWORD_FILE={p['pw']}"
        # 옛 킷(1.2.0.2 이하)에는 --upgrade 가 없다. 모르는 인자는 메뉴로 빠져 아무것도
        # 안 하고 끝나므로, 그 킷에는 설치·업그레이드·패치를 다 하는 --install 을 준다.
        action = "--upgrade" if kit.unattended else "--install"
    return (
        f"{envs} bash {run} {action} > {p['log']} 2>&1; echo $? > {p['rc']}; rm -f {p['pw']}"
    )


def launch_command(job_id: int, inner: str) -> str:
    unit = job_paths(job_id)["unit"]
    return (
        "sudo -S -p '' systemd-run --quiet --collect"
        f" --unit={unit} --setenv=HOME=/root"
        f" /bin/bash -c {shlex.quote(inner)}"
    )


def follow_command(job_id: int, from_line: int) -> str:
    """로그를 from_line 줄부터 따라 읽다가 끝나면 `::exit:: <rc>` 를 낸다."""
    p = job_paths(job_id)
    log_, rc, unit = p["log"], p["rc"], p["unit"]
    return (
        f"L={log_}; R={rc}; U={unit}; i=0; "
        "while [ ! -f \"$L\" ] && [ ! -s \"$R\" ] && [ $i -lt 60 ]; do sleep 1; i=$((i+1)); done; "
        f"tail -n +{int(from_line)} -F \"$L\" 2>/dev/null & t=$!; "
        "while [ ! -s \"$R\" ]; do "
        "if ! systemctl is-active -q \"$U\"; then sleep 3; [ -s \"$R\" ] || break; fi; "
        "sleep 2; done; "
        "sleep 2; kill $t 2>/dev/null; wait $t 2>/dev/null; "
        "if [ -s \"$R\" ]; then echo \"::exit:: $(cat \"$R\")\"; else echo '::exit:: lost'; fi"
    )


def verify_command(install_dir: str | None) -> str:
    d = shlex.quote(install_dir or INSTALL_DIRS[0])
    return (
        f"echo \"VERSION=$(cat {d}/VERSION 2>/dev/null)\"; "
        "echo \"HEALTH=$(curl -sk -o /dev/null -m 10 -w '%{http_code}' https://127.0.0.1:8000/api/health)\"; true"
    )


# ── 로컬 프로세스 (rsync) ───────────────────────────────────────


class ProgressThrottle:
    """rsync 의 \\r 진행 표시를 몇 % 마다 한 줄로 줄인다. 안 그러면 수만 줄이 된다."""

    def __init__(self, *, step: int = 5, seconds: float = 30.0) -> None:
        self._step, self._seconds = step, seconds
        self._last_pct = -100
        self._last_at = 0.0

    def want(self, text: str) -> bool:
        found = _PERCENT_RE.findall(text)
        if not found:
            return bool(text.strip())
        pct, now = int(found[-1]), time.monotonic()
        if pct == self._last_pct:
            return False
        if pct >= self._last_pct + self._step or pct == 100 or now - self._last_at >= self._seconds:
            self._last_pct, self._last_at = pct, now
            return True
        return False


class LocalProcess:
    """취소할 수 있는 로컬 명령. 출력은 \\r·\\n 어느 쪽으로 끊겨도 줄로 낸다."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    async def run(self, argv: Sequence[str], on_text: Callable[[str], None]) -> int:
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        buf = b""
        assert self._proc.stdout is not None
        while chunk := await self._proc.stdout.read(4096):
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                on_text(part.decode("utf-8", "replace"))
        if buf:
            on_text(buf.decode("utf-8", "replace"))
        rc = await self._proc.wait()
        self._proc = None
        return rc

    def kill(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True


def ssh_option_string(key_path: str | Path, *, check_host: bool) -> str:
    """rsync -e 에 넘길 ssh. 타겟은 포맷·재설치로 호스트 키가 바뀌므로 검사하지
    않는다 (asyncssh 쪽 known_hosts=None 과 같은 정책). 빌드 PC 는 고정 장비라 검사한다."""
    opts = [
        "ssh", "-i", str(Path(key_path).expanduser()), "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=8",
        "-o", "LogLevel=ERROR",
    ]
    if check_host:
        opts += ["-o", "StrictHostKeyChecking=accept-new"]
    else:
        opts += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    return " ".join(opts)


# ── 러너 ────────────────────────────────────────────────────────


SshFactory = Callable[[], SSHClient]
ListKits = Callable[[], Awaitable[list[Kit]]]
RsyncFn = Callable[[Sequence[str], Callable[[str], None]], Awaitable[int]]


@dataclass(slots=True)
class KitRunner:
    target: KitTarget
    request: KitRequest
    list_kits: ListKits
    cache: KitCache
    ssh_factory: SshFactory
    source: str                     # 빌드 PC  user@host:dir
    key_path: str | Path
    sudo_password: str
    kit_password: str
    emit: Emit
    rsync: RsyncFn | None = None    # 테스트가 바꿔 끼운다. 기본은 LocalProcess
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    reconnect_window: float = 1800.0
    reconnect_wait: float = 15.0
    send_attempts: int = 6
    _cancel: bool = False
    _committed: bool = False
    _pw_written: bool = False
    _proc: LocalProcess = field(default_factory=LocalProcess)

    # 취소: 킷 실행 전에만 듣는다 ─────────────────────────────
    def cancel(self) -> bool:
        self._cancel = True
        if self._committed:
            self.emit("kit_check", "킷 실행이 이미 시작되어 취소하지 않습니다 — 끝까지 진행합니다",
                      LineKind.WARN)
            return False
        self._proc.kill()
        return True

    def _check_cancel(self) -> None:
        if self._cancel and not self._committed:
            raise asyncio.CancelledError

    async def _rsync(self, argv: Sequence[str], on_text: Callable[[str], None]) -> int:
        if self.rsync is not None:
            return await self.rsync(argv, on_text)
        return await self._proc.run(argv, on_text)

    async def run(self) -> KitResult:
        result = KitResult(status="failed")
        try:
            await self._run(result)
        except asyncio.CancelledError:
            if self._committed:
                raise
            result.status, result.error = "cancelled", "킷 실행 전에 취소했습니다 (서버는 바뀌지 않았습니다)"
        except KitJobError as exc:
            result.status, result.error, result.exit_code = "failed", str(exc), exc.exit_code
        finally:
            if self._pw_written:
                await self._shred_password()
        return result

    async def _exec(self, ssh: SSHClient, command: str, step: str, *, stdin: str | None = None,
                    echo: bool = False) -> tuple[int, list[str], list[str]]:
        out: list[str] = []
        err: list[str] = []

        def collect(line: StreamLine) -> None:
            (out if line.stream == "stdout" else err).append(line.line)
            if echo or line.stream == "stderr":
                self.emit(step, line.line, LineKind.WARN if line.stream == "stderr" else classify(line.line))

        rc = await ssh.exec(command, collect, stdin=stdin)
        return rc, out, err

    async def _run(self, result: KitResult) -> None:
        t, req = self.target, self.request
        self.emit("kit_check", f"── {t.user}@{t.address} 에 접속", LineKind.TASK)
        try:
            ssh_cm = self.ssh_factory()
            ssh = await ssh_cm.__aenter__()
        except Exception as exc:
            raise KitJobError(f"타겟에 접속할 수 없습니다: {exc}") from exc
        try:
            probe = await self._probe(ssh)
            result.installed_before = probe.installed
            result.addon = probe.addon
            kit, mode = await self._plan(probe, result)
            result.mode, result.kit_name = mode, kit.name if kit else None
            self._check_cancel()
            if kit is not None:
                local = await self._fetch(kit)
                self._check_cancel()
                await self._send(ssh, kit, local, probe)
                self._check_cancel()
                self._pw_written = True
                await self._exec(ssh, f"umask 077; cat > {job_paths(req.job_id)['pw']}",
                                 "kit_send", stdin=self.kit_password + "\n")
            self._check_cancel()
            await self._launch(ssh, req, kit, mode, probe)
        finally:
            try:
                await ssh_cm.__aexit__(None, None, None)
            except Exception:
                pass

        rc, message = await self._follow()
        result.exit_code = rc
        if rc not in (0, 9):
            meaning = EXIT_MEANING.get(rc, f"종료 코드 {rc}")
            raise KitJobError(f"{message or meaning} (종료 코드 {rc})", exit_code=rc)
        if rc == 9:
            result.warn = message or EXIT_MEANING[9]

        await self._verify(result, kit, mode, probe)
        result.status = "succeeded"

    # 1. 사전 점검 ─────────────────────────────────────────────
    async def _probe(self, ssh: SSHClient) -> Probe:
        step = "kit_check"
        rc, out, err = await self._exec(ssh, probe_command(), step)
        if rc != 0:
            raise KitJobError(f"타겟 점검 명령이 실패했습니다 (exit {rc}): {' '.join(err)[-300:]}")
        probe = parse_probe(out)
        rc, out, err = await self._exec(ssh, addon_command(), step, stdin=self.sudo_password + "\n")
        if rc != 0:
            raise KitJobError("타겟에서 sudo 를 쓰지 못했습니다 — .env 의 SSH_PASSWORD(BECOME_PASSWORD)를 확인하세요")
        probe.addon = next((s.strip() for s in out if s.strip()), None)
        self.emit(step, f"OS {probe.os} · {probe.arch} · 메모리 {probe.mem_gb}GB · "
                        f"/var/tmp 여유 {probe.tmp_free_kb // 1024 // 1024}GB", LineKind.OUT)
        self.emit(step, f"설치된 버전: {probe.installed or '없음'}"
                        + (f" · 패키지 {probe.addon}" if probe.installed and probe.addon else "")
                        + (f" ({probe.install_dir})" if probe.install_dir else ""), LineKind.OUT)
        if not probe.os.startswith("ubuntu 24.04"):
            self.emit(step, f"! 검증된 OS는 Ubuntu 24.04 입니다 (지금: {probe.os})", LineKind.WARN)
        if probe.arch and probe.arch != "x86_64":
            raise KitJobError(f"x86_64 PC만 지원합니다 (지금: {probe.arch})")
        if not probe.systemd_run:
            raise KitJobError("타겟에 systemd-run 이 없습니다")
        return probe

    async def _plan(self, probe: Probe, result: KitResult) -> tuple[Kit | None, str]:
        req, step = self.request, "kit_check"
        if req.action.startswith("uninstall"):
            if not probe.installed and not probe.install_dir:
                raise KitJobError("이 서버에는 설치되어 있지 않습니다 — 지울 것이 없습니다", exit_code=5)
            self.emit(step, f"작업: {'삭제 (데이터·비밀값은 남김)' if req.action == 'uninstall-keep' else '전체 삭제 (데이터 포함)'}",
                      LineKind.OK)
            return None, req.action
        try:
            kits = await self.list_kits()
        except Exception as exc:
            raise KitJobError(f"빌드 PC 의 킷 목록을 읽지 못했습니다: {exc}") from exc
        try:
            kit = choose_kit(kits, req.version or "", probe.installed)
        except KitError as exc:
            raise KitJobError(str(exc)) from exc
        if not probe.installed:
            mode = "install"
            what = f"새 설치 (패키지 {req.addon or 'suite'})"
        elif kit.is_patch:
            mode = "patch"
            what = f"패치 {probe.installed} → {kit.version} (설치된 패키지 유지)"
        else:
            mode = "upgrade"
            what = (f"전체 킷으로 {'다시 설치' if version_key(probe.installed) == version_key(kit.version) else '업그레이드'}"
                    f" {probe.installed} → {kit.version} (설치된 패키지 유지)")
        self.emit(step, f"작업: {what} — {kit.name} ({kit.size / GIB:.1f}GB)", LineKind.OK)
        return kit, mode

    # 2. 킷 준비 (빌드 PC → 캐시) ──────────────────────────────
    async def _fetch(self, kit: Kit) -> Path:
        step = "kit_fetch"
        local = self.cache.path(kit)
        if self.cache.is_ready(kit):
            self.emit(step, f"맥미니 캐시에 있습니다: {local}", LineKind.OK)
            return local
        self.cache.dir.mkdir(parents=True, exist_ok=True)
        host, _, directory = self.source.partition(":")
        self.emit(step, f"── 빌드 PC 에서 받는 중: {self.source}/{kit.name} ({kit.size / GIB:.1f}GB)", LineKind.TASK)
        throttle = ProgressThrottle()
        argv = ["rsync", "-t", "--partial", "--progress",
                "-e", ssh_option_string(self.key_path, check_host=True),
                f"{host}:{directory.rstrip('/')}/{kit.name}", str(local)]
        rc = await self._rsync(argv, lambda s: throttle.want(s) and self.emit(step, s.strip(), LineKind.OUT))
        self._check_cancel()
        if rc != 0:
            raise KitJobError(f"빌드 PC 에서 킷을 받지 못했습니다 (rsync exit {rc})")
        self.emit(step, "받은 파일 확인 중 (sha256, 1~2분)", LineKind.OUT)
        if not await self.cache.verify(kit):
            local.unlink(missing_ok=True)
            raise KitJobError("받은 킷의 sha256 이 헤더와 다릅니다 — 지우고 다시 받으세요", exit_code=4)
        removed = self.cache.prune(kit)
        if removed:
            self.emit(step, f"캐시 정리 (최근 {self.cache.keep_versions}개 버전만 남김): {', '.join(removed)}", LineKind.OUT)
        self.emit(step, "킷 준비 완료", LineKind.OK)
        return local

    # 3. 전송 (캐시 → 타겟) ────────────────────────────────────
    async def _send(self, ssh: SSHClient, kit: Kit, local: Path, probe: Probe) -> None:
        step = "kit_send"
        t = self.target
        remote = f"{REMOTE_DIR}/{kit.name}"
        rc, out, _ = await self._exec(ssh, f"stat -c %s {remote} 2>/dev/null || echo 0", step)
        have = int(out[-1]) if out and out[-1].strip().isdigit() else 0
        # .run 이 차지할 자리 + 킷이 풀 자리(페이로드 GB + 5GB, run-header 의 조건)
        need_kb = (max(0, kit.size - have) + ((kit.size + GIB - 1) // GIB + 5) * GIB) // 1024
        if probe.tmp_free_kb and probe.tmp_free_kb < need_kb:
            raise KitJobError(
                f"타겟 {REMOTE_DIR} 여유 공간이 {probe.tmp_free_kb // 1024 // 1024}GB 입니다."
                f" {need_kb // 1024 // 1024 + 1}GB 이상 필요합니다 (킷 + 풀 자리)", exit_code=8)
        if not probe.rsync:
            raise KitJobError("타겟에 rsync 가 없습니다 — `sudo apt install rsync` 후 다시 실행하세요")
        if have == kit.size:
            self.emit(step, "타겟에 같은 크기의 파일이 있습니다 — 내용만 확인합니다", LineKind.OUT)
        else:
            self.emit(step, f"── 타겟으로 보내는 중 ({kit.size / GIB:.1f}GB"
                            + (f", {have / GIB:.1f}GB 부터 이어서" if have else "") + ")", LineKind.TASK)
        argv = ["rsync", "-t", "--partial", "--progress",
                "-e", ssh_option_string(self.key_path, check_host=False),
                str(local), f"{t.user}@{t.address}:{remote}"]
        for attempt in range(1, self.send_attempts + 1):
            throttle = ProgressThrottle()
            started = time.monotonic()
            rc = await self._rsync(argv, lambda s: throttle.want(s) and self.emit(step, s.strip(), LineKind.OUT))
            self._check_cancel()
            if rc == 0:
                break
            if attempt == self.send_attempts:
                raise KitJobError(f"타겟으로 보내지 못했습니다 (rsync exit {rc}, {attempt}회 시도)")
            wait = min(120.0, 10.0 * attempt)
            self.emit(step, f"! 전송이 끊겼습니다 (rsync exit {rc}, {time.monotonic() - started:.0f}초)."
                            f" {wait:.0f}초 뒤 이어서 보냅니다 ({attempt + 1}/{self.send_attempts})", LineKind.WARN)
            await self.sleep(wait)
            self._check_cancel()
        self.emit(step, "타겟에서 파일 확인 중 (.run --check, 수 분)", LineKind.OUT)
        throttle = ProgressThrottle(step=20)
        def check_line(line: StreamLine) -> None:
            if throttle.want(line.line):
                self.emit(step, line.line.strip(), classify(line.line))
        rc = await ssh.exec(f"bash {remote} --check", check_line)
        if rc != 0:
            await ssh.exec(f"rm -f {remote}")
            raise KitJobError("타겟에 보낸 킷이 손상되었습니다 — 지웠으니 다시 실행하면 새로 보냅니다", exit_code=4)
        self.emit(step, "전송 완료", LineKind.OK)

    # 4. 킷 실행 ───────────────────────────────────────────────
    async def _launch(self, ssh: SSHClient, req: KitRequest, kit: Kit | None, mode: str, probe: Probe) -> None:
        step = "kit_remove" if mode.startswith("uninstall") else "kit_0"
        inner = kit_inner_command(req, kit, mode, probe.install_dir)
        p = job_paths(req.job_id)
        await ssh.exec(f"rm -f {p['log']} {p['rc']}")
        self._committed = True
        what = {"install": "--install", "upgrade": "--upgrade", "patch": "--upgrade"}.get(mode, "uninstall.sh")
        if kit is not None and not kit.unattended:
            what = "--install"
            self.emit(step, "! 무인 모드 이전 킷입니다 — 진행 단계가 표시되지 않고 로그만 나옵니다", LineKind.WARN)
        self.emit(step, f"── 타겟에서 시작: {kit.name if kit else ''} {what} (systemd 유닛 {p['unit']})".replace("  ", " "),
                  LineKind.TASK)
        rc, _, err = await self._exec(ssh, launch_command(req.job_id, inner), step, stdin=self.sudo_password + "\n")
        if rc != 0:
            self._committed = False
            raise KitJobError(f"타겟에서 킷을 시작하지 못했습니다 (systemd-run exit {rc}): {' '.join(err)[-300:]}")

    async def _follow(self) -> tuple[int, str | None]:
        """로그를 끝까지 따라 읽는다. 끊기면 다시 붙어 이어 읽는다."""
        req = self.request
        received = 0
        exit_rc: int | None = None
        lost = False
        message: str | None = None
        uninstall = req.action.startswith("uninstall")
        step = "kit_remove" if uninstall else "kit_0"
        down_since: float | None = None

        def on_line(line: StreamLine) -> None:
            nonlocal received, exit_rc, lost, message, step, down_since
            if line.stream != "stdout":
                return
            text = line.line
            m = _EXIT_RE.match(text.strip())
            if m:
                if m["rc"] == "lost":
                    lost = True
                else:
                    exit_rc = int(m["rc"])
                return
            received += 1
            down_since = None
            m = _STEP_RE.match(text.strip())
            if m and not uninstall:
                n = int(m["n"])
                if 0 <= n <= 8:
                    step = f"kit_{n}"
                self.emit(step, f"━━ {m['n']}/8 {m['name']}", LineKind.TASK)
                return
            if m and uninstall:
                self.emit(step, f"━━ {m['name']}", LineKind.TASK)
                return
            m = _RESULT_RE.match(text.strip())
            if m:
                message = m["msg"] or None
                kind = {"ok": LineKind.OK, "warn": LineKind.WARN, "fail": LineKind.ERROR}[m["level"]]
                self.emit(step, f"결과: {m['msg']}", kind)
                return
            self.emit(step, redact(text), classify(text))

        while exit_rc is None and not lost:
            try:
                async with self.ssh_factory() as ssh:
                    await ssh.exec(follow_command(req.job_id, received + 1), on_line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 끊김: 다시 붙는다
                log.info("킷 로그 따라 읽기 끊김 (job %d): %s", req.job_id, type(exc).__name__)
            if exit_rc is not None or lost:
                break
            now = time.monotonic()
            down_since = down_since or now
            if now - down_since > self.reconnect_window:
                raise KitJobError(
                    f"타겟과 연결이 {int(self.reconnect_window // 60)}분 넘게 끊겼습니다 — 설치는 타겟에서"
                    f" 계속 돌고 있을 수 있습니다. 타겟의 {job_paths(req.job_id)['log']} 를 확인하세요")
            self.emit(step, f"! 타겟 연결이 끊겼습니다. {self.reconnect_wait:.0f}초 뒤 다시 붙습니다"
                            " (설치는 타겟에서 계속됩니다)", LineKind.WARN)
            await self.sleep(self.reconnect_wait)
        if lost:
            raise KitJobError("타겟의 킷 실행이 결과 없이 끝났습니다 (재부팅되었거나 강제로 멈춤)."
                              f" 타겟의 {job_paths(req.job_id)['log']} 를 확인하세요")
        assert exit_rc is not None
        return exit_rc, message

    # 5. 검증 ──────────────────────────────────────────────────
    async def _verify(self, result: KitResult, kit: Kit | None, mode: str, probe: Probe) -> None:
        step = "kit_verify"
        self.emit(step, "── 설치 결과 확인", LineKind.TASK)
        p = job_paths(self.request.job_id)
        async with self.ssh_factory() as ssh:
            if mode.startswith("uninstall"):
                rc, out, _ = await self._exec(ssh, probe_command(), step)
                after = parse_probe(out)
                if after.install_dir:
                    raise KitJobError(f"삭제 뒤에도 {after.install_dir}/docker-compose.yml 이 남아 있습니다")
                result.installed_after, result.addon = None, None
                self.emit(step, "게이트웨이가 지워진 것을 확인했습니다", LineKind.OK)
                await ssh.exec(f"rm -f {p['log']} {p['rc']}")
                return
            assert kit is not None
            rc, out, _ = await self._exec(ssh, probe_command(), step)
            after = parse_probe(out)
            rc, out, _ = await self._exec(ssh, verify_command(after.install_dir), step)
            values = dict(s.strip().partition("=")[::2] for s in out if "=" in s)
            version, health = values.get("VERSION", ""), values.get("HEALTH", "")
            if version != kit.version:
                raise KitJobError(f"설치된 버전이 {version or '없음'} 입니다 ({kit.version} 이어야 합니다)")
            if health != "200":
                raise KitJobError(f"웹(8000) 이 응답하지 않습니다 (HTTP {health or '-'})")
            rc, out, _ = await self._exec(ssh, addon_command(), step, stdin=self.sudo_password + "\n")
            result.addon = next((s.strip() for s in out if s.strip()), None) if rc == 0 else result.addon
            result.installed_after = version
            self.emit(step, f"버전 {version} · 웹 응답 정상 · 패키지 {result.addon or '?'}", LineKind.OK)
            await ssh.exec(f"rm -f {REMOTE_DIR}/{kit.name} {p['log']} {p['rc']}")
            self.emit(step, f"타겟의 킷 파일을 지웠습니다 ({REMOTE_DIR}/{kit.name})", LineKind.OUT)
            self.emit(step, "초기 관리자 비밀번호: 타겟의 install-info.txt (sudo cat …/install-info.txt)", LineKind.OUT)

    async def _shred_password(self) -> None:
        p = job_paths(self.request.job_id)
        try:
            async with self.ssh_factory() as ssh:
                await ssh.exec(f"rm -f {p['pw']}")
        except Exception:
            log.warning("job %d: 타겟의 키트 비밀번호 파일을 지우지 못했습니다 (%s)", self.request.job_id, p["pw"])


async def read_kit_version(
    *, host: str, username: str, key_path: str | Path, sudo_password: str = "",
    port: int = 22, ssh_factory: SshFactory | None = None,
) -> tuple[str | None, str | None]:
    """설치된 (버전, 패키지). 설치되어 있지 않으면 (None, None). 서버 화면의 ↻ 가 쓴다."""
    if ssh_factory is None:
        from autodeploy.ssh import AsyncSSHClient

        def ssh_factory() -> SSHClient:  # type: ignore[misc]
            return AsyncSSHClient(host, username=username, port=port, key_path=key_path)

    out: list[str] = []
    async with ssh_factory() as ssh:
        await ssh.exec(probe_command(), lambda line: line.stream == "stdout" and out.append(line.line))
        probe = parse_probe(out)
        if not probe.install_dir:
            return None, None
        addon_out: list[str] = []
        rc = await ssh.exec(addon_command(),
                            lambda line: line.stream == "stdout" and addon_out.append(line.line),
                            stdin=f"{sudo_password}\n" if sudo_password else None)
    addon = next((s.strip() for s in addon_out if s.strip()), None) if rc == 0 else None
    return probe.installed or "?", addon
