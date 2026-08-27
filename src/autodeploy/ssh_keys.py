"""SSH 키 등록 (dev-spec-web-console §F9).

runbook §1-1 이 사람에게 시키던 작업을 봇이 대신한다:
  1) 컨트롤러(맥미니) 키 확보  2) 타겟 authorized_keys 에 공개키 설치
  3) 비밀번호를 끄고 키 인증만으로 재접속해 검증

타겟에서 ssh-keygen 은 실행하지 않는다 — 필요한 건 ~/.ssh 디렉터리이고,
쓰지도 않을 개인키를 납품 서버에 남기지 않기 위함.
"""
from __future__ import annotations

import logging
import re
import secrets
import shlex
import socket
import subprocess
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from autodeploy.ssh import LineCallback, SSHClient

log = logging.getLogger(__name__)

DEFAULT_KEY_PATH = Path("~/.ssh/id_ed25519")


class SSHKeyError(RuntimeError):
    pass


def ensure_controller_key(
    key_path: str | Path = DEFAULT_KEY_PATH, *, comment: str | None = None
) -> str:
    """컨트롤러 개인키/공개키를 확보하고 공개키 텍스트를 반환.

    이미 있으면 그대로 읽는다 (기존 키를 절대 덮어쓰지 않는다).
    """
    key = Path(key_path).expanduser()
    pub = key.with_suffix(key.suffix + ".pub") if key.suffix else Path(str(key) + ".pub")

    if pub.exists():
        return pub.read_text(encoding="utf-8").strip()

    if key.exists():
        raise SSHKeyError(
            f"개인키는 있는데 공개키가 없습니다: {pub}\n"
            f"ssh-keygen -y -f {key} > {pub} 로 복구한 뒤 다시 시도하세요."
        )

    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cmt = comment or f"autodeploy@{socket.gethostname()}"
    log.warning("컨트롤러 SSH 키 생성: %s", key)
    proc = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", cmt, "-f", str(key)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SSHKeyError(f"ssh-keygen 실패 (exit {proc.returncode}): {proc.stderr.strip()}")
    if not pub.exists():
        raise SSHKeyError(f"ssh-keygen 이 공개키를 만들지 않았습니다: {pub}")
    return pub.read_text(encoding="utf-8").strip()


def build_install_command(pubkey: str) -> str:
    """authorized_keys 에 공개키를 멱등하게 추가하는 원격 셸 명령.

    grep -qxF 로 이미 있는 줄이면 건너뛴다 — 여러 번 실행해도 줄이 늘지 않는다.
    """
    key = pubkey.strip()
    if not key or not key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise SSHKeyError(f"공개키 형식이 아닙니다: {key[:40]!r}")
    q = shlex.quote(key)
    return (
        "umask 077 && mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"(grep -qxF {q} ~/.ssh/authorized_keys || printf '%s\\n' {q} >> ~/.ssh/authorized_keys)"
    )


# 절전으로 들어가면 SSH 가 끊기고 돌던 설치가 timeout 으로 죽는다. 재부팅해도
# 유지되는 설정이라 서버당 한 번만 걸면 된다.
SLEEP_TARGETS: tuple[str, ...] = (
    "sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target",
)


def build_mask_command() -> str:
    """절전 타겟 mask 명령. **비밀번호는 여기 들어가지 않는다.**

    `printf '%s\n' <비번> | sudo -S ...` 로 만들면 그 줄이 타겟의 `ps` 에 그대로
    보인다. `sudo -S` 는 표준입력에서 읽으므로 비밀번호는 exec 의 stdin 으로 보낸다.
    `-p ''` 는 프롬프트를 지워 출력에 섞이지 않게 한다.
    """
    return f"sudo -S -p '' systemctl mask {' '.join(SLEEP_TARGETS)}"


async def mask_sleep_targets(
    ssh: SSHClient, *, sudo_password: str = "", on_line: LineCallback | None = None
) -> int:
    """systemd 절전 타겟을 mask. 멱등 (이미 mask 면 아무 일도 안 한다)."""
    return await ssh.exec(
        build_mask_command(), on_line=on_line,
        stdin=f"{sudo_password}\n" if sudo_password else None,
    )


async def install_public_key(
    ssh: SSHClient, pubkey: str, on_line: LineCallback | None = None
) -> None:
    """이미 연결된 SSH 세션(비밀번호 인증)에 공개키를 심는다."""
    rc = await ssh.exec(build_install_command(pubkey), on_line=on_line)
    if rc != 0:
        raise SSHKeyError(
            f"authorized_keys 설치 실패 (exit {rc}) — 홈 디렉터리 권한이나 디스크 상태를 확인하세요"
        )


async def verify_key_auth(
    host: str,
    *,
    port: int = 22,
    username: str,
    key_path: str | Path = DEFAULT_KEY_PATH,
    connect_fn: Callable[..., object] | None = None,
) -> bool:
    """비밀번호를 끄고 **키 인증만으로** 접속되는지 확인 (runbook 의 `ssh <host> true`)."""
    key = str(Path(key_path).expanduser())
    connect = connect_fn or asyncssh.connect
    try:
        conn = await connect(
            host,
            port=port,
            username=username,
            client_keys=[key],
            known_hosts=None,
            preferred_auth=("publickey",),
        )
    except Exception as exc:  # asyncssh.Error, OSError 등
        log.info("키 인증 검증 실패 (%s@%s:%s): %s", username, host, port, exc)
        return False
    try:
        result = await conn.run("true", check=False)
        return result.exit_status == 0
    finally:
        conn.close()
        await conn.wait_closed()


# hybrid 사이트의 타겟은 사람이 앞에 앉는 데스크톱이라 원격 지원 준비가 필요하다.
# 배포 내용과 무관한 기계 설정이므로 설치 때마다가 아니라 등록할 때 한 번 돈다.
NODE_PREP_SCRIPT = Path(__file__).with_name("node_prep.sh")

# 스크립트가 마지막에 뱉는 기계용 줄. 사람이 읽는 출력과 따로 둔다 —
# 줄 모양이 바뀌면 파싱이 조용히 깨지기 때문이다.
_ANYDESK_ID_RE = re.compile(r"^ANYDESK_ID=(\d+)\s*$")


def is_desktop_profile(profile: str) -> bool:
    """준비 스크립트를 돌릴 프로파일인가.

    hybrid 사이트만이다 (`hybrid-with-ai` · `hybrid-without-ai`). onprem 은
    폐쇄망 서버라 AnyDesk 저장소에 닿지도 않고, 사람이 앞에 앉지도 않는다.
    """
    return profile.startswith("hybrid")


async def prepare_desktop(
    ssh: SSHClient,
    *,
    sudo_password: str = "",
    target_user: str = "connecteve",
    anydesk_password: str = "",
    weekly_reboot: bool = False,
    weekly_reboot_cron: str = "0 4 * * 0",
    on_line: LineCallback | None = None,
) -> int:
    """준비 스크립트를 타겟에서 root 로 실행한다.

    **비밀번호를 명령줄에 싣지 않는다.** 두 번에 나눠 보내는 이유가 그것이다.
      1) 스크립트를 표준입력으로 흘려 임시 파일에 쓴다 (mktemp 는 0600 이다)
      2) sudo 로 그 파일을 실행하고, sudo 비밀번호는 표준입력으로 준다

    한 번에 `sudo -S bash -s` 로 하면 sudo 와 bash 가 같은 표준입력을 두고
    다투게 되고(비밀번호 한 줄만 먹고 나머지를 넘겨준다는 보장이 없다),
    `sudo env VAR=... ` 로 값을 넘기면 그 줄이 타겟 `ps` 에 보인다.

    AnyDesk 비밀번호는 스크립트 안에 들어간다. 파일은 0600 이고 끝나면 지운다 —
    어차피 그 기계에 원격 접속 비밀번호를 심는 작업이라 무게가 맞는다.
    """
    body = NODE_PREP_SCRIPT.read_text(encoding="utf-8")
    header = (
        f"TARGET_USER={shlex.quote(target_user)}\n"
        f"ANYDESK_PASSWORD={shlex.quote(anydesk_password)}\n"
        f"WEEKLY_REBOOT={shlex.quote('true' if weekly_reboot else 'false')}\n"
        f"WEEKLY_REBOOT_CRON={shlex.quote(weekly_reboot_cron)}\n"
        "export TARGET_USER ANYDESK_PASSWORD WEEKLY_REBOOT WEEKLY_REBOOT_CRON\n"
    )
    remote = f"/tmp/.autodeploy-prep-{secrets.token_hex(8)}.sh"
    q = shlex.quote(remote)

    rc = await ssh.exec(f"umask 077 && cat > {q}", stdin=header + body)
    if rc != 0:
        raise SSHKeyError(f"준비 스크립트를 타겟에 쓰지 못했습니다 (exit {rc})")

    try:
        # 스크립트가 실패해도 임시 파일은 반드시 지운다.
        return await ssh.exec(
            f"sudo -S -p '' bash {q}; rc=$?; rm -f {q}; exit $rc",
            on_line=on_line,
            stdin=f"{sudo_password}\n" if sudo_password else None,
        )
    except Exception:
        await _try_remove(ssh, remote)
        raise


async def _try_remove(ssh: SSHClient, remote: str) -> None:
    with suppress(Exception):
        await ssh.exec(f"rm -f {shlex.quote(remote)}")


def parse_anydesk_id(lines: Sequence[str]) -> str | None:
    for line in lines:
        m = _ANYDESK_ID_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


@dataclass(frozen=True, slots=True)
class KeyRegistration:
    """등록 결과.

    `sleep_masked` 가 False 여도 등록은 성공한 것이다 — 아래 참조.
    """

    pubkey: str
    sleep_masked: bool
    sleep_error: str | None = None
    # hybrid 프로파일일 때만. prep_ran=False 는 "안 돌렸다"이고,
    # prep_error 가 있으면 "돌렸는데 실패했다"다 — 둘은 다른 이야기다.
    prep_ran: bool = False
    prep_log: tuple[str, ...] = ()
    prep_error: str | None = None
    anydesk_id: str | None = None


async def register_key(
    *,
    host: str,
    port: int = 22,
    username: str,
    password: str,
    key_path: str | Path = DEFAULT_KEY_PATH,
    ssh_factory: Callable[..., SSHClient] | None = None,
    connect_fn: Callable[..., object] | None = None,
    on_line: LineCallback | None = None,
    prepare: bool = False,
    anydesk_password: str = "",
    weekly_reboot: bool = False,
    weekly_reboot_cron: str = "0 4 * * 0",
) -> KeyRegistration:
    """F9 전체 흐름. 실패하면 SSHKeyError.

    `prepare=True` 면 타겟 PC 준비 스크립트까지 돌린다 (hybrid 전용).

    키를 심는 김에 **절전 타겟도 mask 한다.** 설치 중 서버가 잠들면 SSH 가 끊겨
    작업이 죽는데, 그걸 막는 유일한 자동 경로가 여기다 — 웹 콘솔은 hubctl 을
    돌릴 뿐이고 playbook 도 절전을 건드리지 않는다. 이 단계에서는 이미 sudo 를
    쓸 수 있는 비밀번호를 손에 쥐고 타겟에 붙어 있으므로 자리가 맞다.

    **mask 가 실패해도 등록은 성공시킨다.** 키 등록이 막히면 설치를 아예 못 하는데,
    부수적인 절전 설정 때문에 그걸 막을 이유가 없다. 대신 결과를 돌려줘서
    화면이 "키는 됐고 절전은 안 됐다" 를 말할 수 있게 한다.
    """
    pubkey = ensure_controller_key(key_path)

    if ssh_factory is None:
        from autodeploy.ssh import AsyncSSHClient

        def ssh_factory(h: str, p: int) -> SSHClient:  # type: ignore[misc]
            return AsyncSSHClient(h, username=username, password=password, port=p)

    masked, mask_error = False, None
    prep_ran, prep_error, prep_log = False, None, []
    async with ssh_factory(host, port) as ssh:
        await install_public_key(ssh, pubkey, on_line=on_line)
        try:
            rc = await mask_sleep_targets(ssh, sudo_password=password, on_line=on_line)
            masked = rc == 0
            if not masked:
                mask_error = f"systemctl mask 가 exit {rc} 로 끝났습니다 (sudo 권한 확인)"
        except Exception as exc:   # 연결이 끊겨도 키 등록 결과는 지키다
            mask_error = f"{type(exc).__name__}: {exc}"
            log.info("절전 타겟 mask 실패 (%s): %s", host, mask_error)

        if prepare:
            def collect(line) -> None:
                prep_log.append(line.line)
                if on_line is not None:
                    on_line(line)

            prep_ran = True
            try:
                rc = await prepare_desktop(
                    ssh,
                    sudo_password=password,
                    target_user=username,
                    anydesk_password=anydesk_password,
                    weekly_reboot=weekly_reboot,
                    weekly_reboot_cron=weekly_reboot_cron,
                    on_line=collect,
                )
                if rc != 0:
                    prep_error = f"준비 스크립트가 exit {rc} 로 끝났습니다"
            except Exception as exc:
                prep_error = f"{type(exc).__name__}: {exc}"
                log.info("타겟 준비 실패 (%s): %s", host, prep_error)

    ok = await verify_key_auth(
        host, port=port, username=username, key_path=key_path, connect_fn=connect_fn
    )
    if not ok:
        raise SSHKeyError(
            "공개키는 넣었지만 키 인증 접속이 되지 않습니다 — "
            "타겟의 sshd 설정(PubkeyAuthentication)이나 홈 디렉터리 권한을 확인하세요."
        )
    return KeyRegistration(
        pubkey=pubkey, sleep_masked=masked, sleep_error=mask_error,
        prep_ran=prep_ran, prep_log=tuple(prep_log), prep_error=prep_error,
        anydesk_id=parse_anydesk_id(prep_log),
    )
