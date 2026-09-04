"""타겟 PC 의 하드웨어 시리얼 읽기.

runbook 에서 사람이 치던 `sudo dmidecode -s system-serial-number` 를 콘솔이 대신
한다. 납품한 기계를 나중에 특정하는 번호라 서버 목록에 남긴다.

**root 가 필요하다.** dmidecode 는 `/dev/mem` 을 읽고, 대안인
`/sys/class/dmi/id/product_serial` 도 0400 이라 일반 계정으로는 어느 쪽도 안 된다.
그래서 sudo 를 쓰되 비밀번호는 **명령줄이 아니라 표준입력**으로 준다 —
`printf '%s\\n' <비번> | sudo -S ...` 로 만들면 그 줄이 타겟의 `ps` 에 그대로
보인다 (`ssh_keys.build_mask_command` 와 같은 이유). `-p ''` 는 프롬프트를 지워
출력에 섞이지 않게 한다.

읽기만 한다. 타겟의 상태를 바꾸지 않으므로 작업(jobs)으로 기록하지 않는다.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from autodeploy.ssh import LineCallback, SSHClient, StreamLine

SERIAL_COMMAND = "sudo -S -p '' dmidecode -s system-serial-number"

# 메인보드에 번호를 안 넣고 출고한 기계가 내놓는 값들. 저장하면 **서버마다 같은
# 문자열**이 뜨는데, 그건 비어 있는 것보다 나쁘다 — 서로 다른 기계를 구분한다고
# 착각하게 만든다. 비교는 소문자로 한다 (제조사마다 대소문자가 제각각이다).
PLACEHOLDER_SERIALS: frozenset[str] = frozenset({
    "none", "n/a", "na", "null", "unknown", "not specified", "not available",
    "not applicable", "default string", "system serial number",
    "chassis serial number", "base board serial number", "to be filled by o.e.m.",
    "to be filled by o.e.m", "0", "0123456789", "123456789",
    "<out of spec>", "<bad index>",
})


def parse_serial(lines: Sequence[str]) -> str | None:
    """dmidecode 의 표준출력에서 시리얼을 뽑는다. 못 믿을 값이면 None.

    `-s` 는 값 한 줄만 내놓지만 판본에 따라 `#` 로 시작하는 안내가 앞에 붙는다.
    값은 항상 마지막 줄이므로 뒤에서부터 본다.
    """
    for raw in reversed(list(lines)):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        return None if value.lower() in PLACEHOLDER_SERIALS else value
    return None


@dataclass(frozen=True, slots=True)
class SerialRead:
    """읽기 결과. `serial` 이 None 이면 `error` 에 사람이 읽을 이유가 들어 있다."""

    serial: str | None = None
    exit_code: int = 0
    stdout: tuple[str, ...] = ()
    stderr: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.serial is not None


def _failure_reason(exit_code: int, stdout: Sequence[str], stderr: Sequence[str]) -> str:
    """왜 못 읽었는지 한 줄로. 문구를 여기 모아둬야 화면마다 달라지지 않는다."""
    joined = " ".join(stderr).lower()
    if exit_code != 0:
        if "command not found" in joined or "dmidecode: not found" in joined:
            return (
                "타겟에 dmidecode 가 없습니다 — `sudo apt install dmidecode` 로 설치한 뒤"
                " 다시 조회하세요"
            )
        if "password" in joined or "sorry, try again" in joined or "sudo:" in joined:
            return (
                "타겟에서 sudo 를 쓰지 못했습니다 (비밀번호 확인)"
                f" — dmidecode 가 exit {exit_code} 로 끝났습니다"
            )
        tail = next((s.strip() for s in reversed(stderr) if s.strip()), "")
        return f"시리얼을 읽지 못했습니다 (exit {exit_code})" + (f": {tail}" if tail else "")

    raw = next((s.strip() for s in reversed(stdout) if s.strip() and not s.startswith("#")), "")
    if raw:
        return (
            f"이 기계에는 시리얼이 기록돼 있지 않습니다 — dmidecode 가 {raw!r} 를 돌려줍니다."
            " 제조사가 메인보드에 번호를 넣지 않은 것이라 콘솔에서 할 수 있는 일이 없습니다"
        )
    return "dmidecode 가 아무 값도 내놓지 않았습니다"


async def read_serial(
    ssh: SSHClient, *, sudo_password: str = "", on_line: LineCallback | None = None
) -> SerialRead:
    """이미 연결된 세션에서 시리얼을 읽는다. 예외를 만들지 않고 결과로 돌려준다.

    표준출력만 파싱한다. sudo 나 dmidecode 의 경고는 표준오류로 나오는데, 그걸
    값으로 착각하면 서버 목록에 오류 문구가 시리얼로 박힌다.
    """
    out: list[str] = []
    err: list[str] = []

    def collect(line: StreamLine) -> None:
        (out if line.stream == "stdout" else err).append(line.line)
        if on_line is not None:
            on_line(line)

    rc = await ssh.exec(
        SERIAL_COMMAND,
        on_line=collect,
        stdin=f"{sudo_password}\n" if sudo_password else None,
    )
    serial = parse_serial(out) if rc == 0 else None
    return SerialRead(
        serial=serial,
        exit_code=rc,
        stdout=tuple(out),
        stderr=tuple(err),
        error=None if serial else _failure_reason(rc, out, err),
    )


async def fetch_serial(
    *,
    host: str,
    username: str,
    key_path: str | Path,
    sudo_password: str = "",
    port: int = 22,
    ssh_factory: Callable[[], SSHClient] | None = None,
) -> SerialRead:
    """**키 인증**으로 붙어서 읽는다.

    비밀번호로 붙지 않는 이유: 콘솔은 이미 키를 심어뒀고(F9), 타겟의 로그인
    비밀번호가 `.env` 의 값과 같다는 보장이 없다. sudo 에만 비밀번호를 쓴다.
    """
    if ssh_factory is None:
        from autodeploy.ssh import AsyncSSHClient

        def ssh_factory() -> SSHClient:  # type: ignore[misc]
            return AsyncSSHClient(host, username=username, port=port, key_path=key_path)

    async with ssh_factory() as ssh:
        return await read_serial(ssh, sudo_password=sudo_password)


# ── 라이브 배포 ref ────────────────────────────────────────────
#
# 그 서버가 **지금 보고 있는** git ref. `configure --only` 는 이 값을 그대로
# `-e hub_deploy_ref` 로 넘겨야 한다 — 다른 값을 주면 flux_wire 의 partial_guard
# 가 "부분 실행은 배포 ref 를 바꾸지 않습니다" 로 시작 전에 멈춘다
# (RUNBOOK-hubctl §3-8).
#
# sudo 가 필요 없다. 사용자 kubeconfig 로 읽는다 (RUNBOOK-OPS §0 의 전제 그대로 —
# aqua 도구는 비로그인 셸의 PATH 에 없어서 앞에 직접 깔아준다).
LIVE_REF_COMMAND = (
    "export AQUA_GLOBAL_CONFIG=/etc/hub/aqua/aqua.yaml AQUA_ROOT_DIR=/opt/hub/aqua"
    " PATH=/opt/hub/aqua/bin:$PATH KUBECONFIG=$HOME/.kube/config;"
    " kubectl get gitrepository -n flux-system hub-deploy"
    " -o jsonpath='{.spec.ref}'"
)


@dataclass(frozen=True, slots=True)
class LiveRefRead:
    """`{"branch": "release/1.1.1.0"}` 를 (ref_type, ref) 로 풀어놓은 것."""

    ref: str | None = None
    ref_type: str | None = None
    exit_code: int = 0
    stdout: tuple[str, ...] = ()
    stderr: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.ref and self.ref_type)


def parse_live_ref(lines: Sequence[str]) -> tuple[str | None, str | None]:
    """jsonpath 출력에서 (ref_type, ref). 못 읽으면 (None, None).

    gitrepository.yaml.j2 가 안 쓰는 타입 키를 null 로 지워 **키가 하나만** 남는다
    (partial_guard.yml 주석). 그래서 키 이름이 곧 ref_type 이다.
    """
    for raw in reversed(list(lines)):
        text = raw.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        pairs = [(k, v) for k, v in data.items() if v]
        if len(pairs) == 1:
            return pairs[0][0], str(pairs[0][1])
        return None, None
    return None, None


async def read_live_ref(
    ssh: SSHClient, *, on_line: LineCallback | None = None
) -> LiveRefRead:
    """이미 연결된 세션에서 라이브 ref 를 읽는다. 예외 대신 결과로 돌려준다."""
    out: list[str] = []
    err: list[str] = []

    def collect(line: StreamLine) -> None:
        (out if line.stream == "stdout" else err).append(line.line)
        if on_line is not None:
            on_line(line)

    rc = await ssh.exec(LIVE_REF_COMMAND, on_line=collect)
    ref_type, ref = (None, None)
    if rc == 0:
        ref_type, ref = parse_live_ref(out)

    error = None
    if not (ref and ref_type):
        joined = " ".join(err).lower()
        if "not found" in joined or "notfound" in joined:
            error = (
                "이 서버에는 hub-deploy GitRepository 가 없습니다 — 아직 설치되지 않은"
                " 서버이거나 kubeconfig 를 못 읽었습니다"
            )
        elif rc != 0:
            tail = next((s.strip() for s in reversed(err) if s.strip()), "")
            error = f"라이브 ref 를 읽지 못했습니다 (exit {rc})" + (f": {tail}" if tail else "")
        else:
            error = "라이브 ref 가 비어 있습니다 (GitRepository 에 spec.ref 가 없습니다)"

    return LiveRefRead(
        ref=ref, ref_type=ref_type, exit_code=rc,
        stdout=tuple(out), stderr=tuple(err), error=error,
    )


async def fetch_live_ref(
    *,
    host: str,
    username: str,
    key_path: str | Path,
    port: int = 22,
    ssh_factory: Callable[[], SSHClient] | None = None,
) -> LiveRefRead:
    """키 인증으로 붙어서 읽는다. **sudo 를 쓰지 않는다.**"""
    if ssh_factory is None:
        from autodeploy.ssh import AsyncSSHClient

        def ssh_factory() -> SSHClient:  # type: ignore[misc]
            return AsyncSSHClient(host, username=username, port=port, key_path=key_path)

    async with ssh_factory() as ssh:
        return await read_live_ref(ssh)
