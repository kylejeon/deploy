"""SSH 키 등록 (dev-spec-web-console §F9).

runbook §1-1 이 사람에게 시키던 작업을 봇이 대신한다:
  1) 컨트롤러(맥미니) 키 확보  2) 타겟 authorized_keys 에 공개키 설치
  3) 비밀번호를 끄고 키 인증만으로 재접속해 검증

타겟에서 ssh-keygen 은 실행하지 않는다 — 필요한 건 ~/.ssh 디렉터리이고,
쓰지도 않을 개인키를 납품 서버에 남기지 않기 위함.
"""
from __future__ import annotations

import logging
import shlex
import socket
import subprocess
from collections.abc import Callable
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
) -> str:
    """F9 전체 흐름. 성공 시 설치된 공개키 텍스트를 반환, 실패하면 SSHKeyError."""
    pubkey = ensure_controller_key(key_path)

    if ssh_factory is None:
        from autodeploy.ssh import AsyncSSHClient

        def ssh_factory(h: str, p: int) -> SSHClient:  # type: ignore[misc]
            return AsyncSSHClient(h, username=username, password=password, port=p)

    async with ssh_factory(host, port) as ssh:
        await install_public_key(ssh, pubkey, on_line=on_line)

    ok = await verify_key_auth(
        host, port=port, username=username, key_path=key_path, connect_fn=connect_fn
    )
    if not ok:
        raise SSHKeyError(
            "공개키는 넣었지만 키 인증 접속이 되지 않습니다 — "
            "타겟의 sshd 설정(PubkeyAuthentication)이나 홈 디렉터리 권한을 확인하세요."
        )
    return pubkey
