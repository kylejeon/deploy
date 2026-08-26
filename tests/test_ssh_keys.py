"""SSH 키 등록 (dev-spec-web-console §F9)."""
from __future__ import annotations

import shlex

import pytest

from autodeploy.ssh import FakeSSHClient, StreamLine
from autodeploy.ssh_keys import (
    SSHKeyError,
    build_install_command,
    ensure_controller_key,
    install_public_key,
    register_key,
    verify_key_auth,
)

PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI0000000000000000000000000000 autodeploy@macmini"


# ── 컨트롤러 키 ───────────────────────────────────────
def test_generates_key_when_missing(tmp_path):
    key = tmp_path / "id_ed25519"
    pub = ensure_controller_key(key)
    assert pub.startswith("ssh-ed25519 ")
    assert key.exists() and (tmp_path / "id_ed25519.pub").exists()


def test_existing_key_is_never_overwritten(tmp_path):
    key = tmp_path / "id_ed25519"
    first = ensure_controller_key(key)
    before = key.read_bytes()

    second = ensure_controller_key(key)

    assert first == second
    assert key.read_bytes() == before, "기존 개인키를 덮어쓰면 안 된다"


def test_private_key_without_public_raises(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    with pytest.raises(SSHKeyError, match="공개키가 없습니다"):
        ensure_controller_key(key)


# ── 설치 명령 ─────────────────────────────────────────
def test_command_is_idempotent_and_sets_permissions():
    cmd = build_install_command(PUB)
    assert "mkdir -p ~/.ssh" in cmd
    assert "chmod 700 ~/.ssh" in cmd
    assert "chmod 600 ~/.ssh/authorized_keys" in cmd
    assert "grep -qxF" in cmd, "중복 등록을 막는 검사가 있어야 한다"
    assert "ssh-keygen" not in cmd, "타겟에서는 키를 만들지 않는다"


def test_command_quotes_injection_attempt():
    """셸이 실제로 토큰을 어떻게 쪼개는지로 검증한다 (문자열 포함 여부는 근거가 약함)."""
    evil = "ssh-ed25519 AAAA'; rm -rf / #"
    tokens = shlex.split(build_install_command(evil))
    assert evil in tokens, "공개키 전체가 인자 하나로 유지돼야 한다"
    assert "rm" not in tokens, "주입된 명령이 독립 토큰으로 떨어지면 안 된다"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-key", "AAAAB3Nza"])
def test_rejects_non_public_key(bad):
    with pytest.raises(SSHKeyError, match="공개키 형식"):
        build_install_command(bad)


# ── 원격 설치 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_install_runs_single_command():
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [StreamLine("stdout", "")], 0)
    async with ssh:
        await install_public_key(ssh, PUB)
    assert len(ssh.executed) == 1
    assert "authorized_keys" in ssh.executed[0]


@pytest.mark.asyncio
async def test_install_failure_raises():
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [StreamLine("stderr", "Permission denied")], 1)
    async with ssh:
        with pytest.raises(SSHKeyError, match="exit 1"):
            await install_public_key(ssh, PUB)


# ── 키 인증 검증 ──────────────────────────────────────
class _FakeConn:
    def __init__(self, exit_status=0):
        self._exit = exit_status
        self.closed = False
        self.ran: list[str] = []

    async def run(self, cmd, check=False):
        self.ran.append(cmd)
        return type("R", (), {"exit_status": self._exit})()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_verify_uses_publickey_only_and_closes():
    captured = {}
    conn = _FakeConn()

    async def fake_connect(host, **kw):
        captured.update(kw, host=host)
        return conn

    ok = await verify_key_auth(
        "10.0.0.9", username="connecteve", key_path="/tmp/k", connect_fn=fake_connect
    )
    assert ok is True
    assert captured["preferred_auth"] == ("publickey",), "비밀번호로 통과하면 검증이 무의미"
    assert captured["known_hosts"] is None
    assert "password" not in captured
    assert conn.ran == ["true"]
    assert conn.closed


@pytest.mark.asyncio
async def test_verify_false_on_connect_error():
    async def boom(host, **kw):
        raise OSError("Connection refused")

    assert await verify_key_auth("10.0.0.9", username="u", connect_fn=boom) is False


@pytest.mark.asyncio
async def test_verify_false_on_nonzero_exit():
    async def fake_connect(host, **kw):
        return _FakeConn(exit_status=255)

    assert await verify_key_auth("10.0.0.9", username="u", connect_fn=fake_connect) is False


# ── 전체 흐름 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_register_key_happy_path(tmp_path):
    key = tmp_path / "id_ed25519"
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)

    async def fake_connect(host, **kw):
        return _FakeConn()

    pub = await register_key(
        host="10.0.0.9",
        username="connecteve",
        password="secret",
        key_path=key,
        ssh_factory=lambda h, p: ssh,
        connect_fn=fake_connect,
    )
    assert pub.startswith("ssh-ed25519 ")
    assert "authorized_keys" in ssh.executed[0]


@pytest.mark.asyncio
async def test_register_key_fails_when_verification_fails(tmp_path):
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)

    async def refuse(host, **kw):
        raise OSError("still asking for password")

    with pytest.raises(SSHKeyError, match="키 인증 접속이 되지 않습니다"):
        await register_key(
            host="10.0.0.9",
            username="connecteve",
            password="secret",
            key_path=tmp_path / "id_ed25519",
            ssh_factory=lambda h, p: ssh,
            connect_fn=refuse,
        )


@pytest.mark.asyncio
async def test_password_never_appears_in_remote_command(tmp_path):
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)

    async def fake_connect(host, **kw):
        return _FakeConn()

    await register_key(
        host="10.0.0.9",
        username="connecteve",
        password="sup3r-s3cret-pw",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh,
        connect_fn=fake_connect,
    )
    assert all("sup3r-s3cret-pw" not in c for c in ssh.executed)
