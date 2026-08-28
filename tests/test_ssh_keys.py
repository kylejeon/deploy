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
    KeyRegistration,
    build_mask_command,
    is_desktop_profile,
    parse_anydesk_id,
    prepare_desktop,
    mask_sleep_targets,
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

    result = await register_key(
        host="10.0.0.9",
        username="connecteve",
        password="secret",
        key_path=key,
        ssh_factory=lambda h, p: ssh,
        connect_fn=fake_connect,
    )
    assert result.pubkey.startswith("ssh-ed25519 ")
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


# ── 절전 끄기 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registering_a_key_also_masks_sleep(tmp_path):
    """설치 중 서버가 잠들면 SSH 가 끊겨 작업이 죽는다.

    웹 콘솔 경로에서 이걸 자동으로 걸어주는 자리는 여기뿐이다 — 콘솔은 hubctl 을
    돌릴 뿐이고 playbook 도 절전을 건드리지 않는다.
    """
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    ssh.enqueue("systemctl mask", [], 0)

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
    )
    assert result.sleep_masked is True
    masked = next(c for c in ssh.executed if "systemctl mask" in c)
    for target in ("sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target"):
        assert target in masked


@pytest.mark.asyncio
async def test_the_sudo_password_goes_to_stdin_not_the_command_line(tmp_path):
    """비밀번호를 `printf ... | sudo -S` 로 만들면 타겟의 `ps` 에 그대로 보인다.

    같은 서버에 로그인한 누구나 볼 수 있다는 뜻이다. sudo -S 는 어차피 표준
    입력에서 읽으므로 명령줄은 깨끗하게 두고 stdin 으로 보낸다.
    """
    ssh = FakeSSHClient()
    ssh.connected = True
    ssh.enqueue("systemctl mask", [], 0)

    await mask_sleep_targets(ssh, sudo_password="sup3r-s3cret-pw")

    assert "sup3r-s3cret-pw" not in ssh.executed[0], ssh.executed[0]
    assert ssh.stdins[0] == "sup3r-s3cret-pw\n"
    assert "sup3r-s3cret-pw" not in build_mask_command()


@pytest.mark.asyncio
async def test_a_failed_mask_does_not_fail_the_registration(tmp_path):
    """키 등록이 막히면 설치를 아예 못 한다. 부수적인 절전 설정으로 그걸 막지 않는다.

    대신 무엇이 안 됐는지는 돌려줘서 화면이 말할 수 있게 한다.
    """
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    ssh.enqueue("systemctl mask", [], 1)      # sudo 권한 없음 등

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
    )
    assert isinstance(result, KeyRegistration)
    assert result.pubkey.startswith("ssh-ed25519 "), "키 등록 자체는 성공해야 한다"
    assert result.sleep_masked is False
    assert "exit 1" in result.sleep_error


@pytest.mark.asyncio
async def test_a_mask_that_blows_up_does_not_fail_the_registration(tmp_path):
    """연결이 끊기는 등 예외가 나도 마찬가지다."""
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    # systemctl mask 응답을 안 넣어둔다 → FakeSSHClient 가 예외를 던진다

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
    )
    assert result.pubkey.startswith("ssh-ed25519 ")
    assert result.sleep_masked is False
    assert result.sleep_error


# ── hybrid 타겟 PC 준비 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("hybrid-with-ai", True),
        ("hybrid-without-ai", True),
        ("onprem", False),
    ],
)
def test_only_hybrid_targets_get_desktop_prep(profile, expected):
    """onprem 은 폐쇄망 서버다 — AnyDesk 저장소에 닿지도 않고 사람이 앉지도 않는다."""
    assert is_desktop_profile(profile) is expected


@pytest.mark.asyncio
async def test_the_prep_script_never_carries_a_password_on_the_command_line():
    """스크립트도 sudo 비밀번호도 명령줄에 실리면 타겟 `ps` 에 보인다.

    그래서 두 번에 나눈다 — 스크립트는 표준입력으로 임시 파일에 쓰고(mktemp 대신
    umask 077), sudo 비밀번호는 실행할 때 표준입력으로 준다.
    """
    ssh = FakeSSHClient()
    ssh.connected = True
    ssh.enqueue("cat >", [], 0)
    ssh.enqueue("sudo -S", [], 0)

    await prepare_desktop(
        ssh, sudo_password="sudo-pw", target_user="connecteve",
        anydesk_password="anydesk-pw",
    )

    write_cmd, run_cmd = ssh.executed
    for secret in ("sudo-pw", "anydesk-pw"):
        assert secret not in write_cmd, write_cmd
        assert secret not in run_cmd, run_cmd

    # 스크립트 본문은 표준입력으로, sudo 비밀번호도 표준입력으로.
    assert "anydesk-pw" in ssh.stdins[0], "스크립트에 값이 안 들어갔다"
    assert "TARGET_USER=connecteve" in ssh.stdins[0]
    assert ssh.stdins[1] == "sudo-pw\n"
    assert "umask 077" in write_cmd, "임시 파일이 남들에게 읽히면 안 된다"


@pytest.mark.asyncio
async def test_the_prep_script_is_removed_even_when_it_fails():
    """실패해도 비밀번호가 든 임시 파일을 타겟에 남기지 않는다."""
    ssh = FakeSSHClient()
    ssh.connected = True
    ssh.enqueue("cat >", [], 0)
    ssh.enqueue("sudo -S", [], 1)

    rc = await prepare_desktop(ssh, sudo_password="pw", anydesk_password="x")
    assert rc == 1
    assert "rm -f" in ssh.executed[1], ssh.executed[1]


def test_the_anydesk_id_is_read_from_a_fixed_marker_line():
    """사람이 읽는 출력과 따로 둔 기계용 줄에서만 읽는다."""
    assert parse_anydesk_id([
        "  이 장비의 AnyDesk ID:",
        "    123 456 789",
        "ANYDESK_ID=123456789",
    ]) == "123456789"
    assert parse_anydesk_id(["    (조회 실패 - 서비스 기동 후 재시도)"]) is None


@pytest.mark.asyncio
async def test_registering_a_hybrid_server_runs_the_prep(tmp_path):
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    ssh.enqueue("systemctl mask", [], 0)
    ssh.enqueue("cat >", [], 0)
    ssh.enqueue("sudo -S -p '' bash", [StreamLine("stdout", "ANYDESK_ID=987654321")], 0)

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
        prepare=True, anydesk_password="ad-pw",
    )
    assert result.prep_ran is True
    assert result.prep_error is None
    assert result.anydesk_id == "987654321"


@pytest.mark.asyncio
async def test_a_failed_prep_does_not_fail_the_registration(tmp_path):
    """준비가 실패해도 키는 심었다 — 설치는 할 수 있어야 한다."""
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    ssh.enqueue("systemctl mask", [], 0)
    ssh.enqueue("cat >", [], 0)
    ssh.enqueue("sudo -S -p '' bash", [StreamLine("stdout", "  !! 저장소 접근 실패")], 1)

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
        prepare=True,
    )
    assert result.pubkey.startswith("ssh-ed25519 ")
    assert result.prep_ran is True
    assert "exit 1" in result.prep_error
    assert any("저장소 접근 실패" in line for line in result.prep_log)


# ── 본체 시리얼 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_registering_a_key_also_reads_the_serial(tmp_path):
    """sudo 비밀번호를 쥐고 타겟에 붙어 있는 자리가 여기뿐이다.

    나중에 읽으려면 사람이 비밀번호를 다시 내놓거나, 데몬이 저장해둔 값을
    꺼내야 한다. 이미 손에 쥐고 있을 때 한 번 읽어두는 편이 싸다.
    """
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    ssh.enqueue("systemctl mask", [], 0)
    ssh.enqueue("dmidecode", [StreamLine("stdout", "PF3ABCDE")], 0)

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
    )

    assert result.serial == "PF3ABCDE"
    assert result.serial_error is None
    read = next(c for c in ssh.executed if "dmidecode" in c)
    assert "secret" not in read, "비밀번호가 명령줄에 실리면 타겟의 ps 에 보인다"


@pytest.mark.asyncio
async def test_a_failed_serial_read_does_not_fail_the_registration(tmp_path):
    """시리얼은 표시용이다. 그것 때문에 설치를 못 하게 되면 앞뒤가 바뀐다."""
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    ssh.enqueue("systemctl mask", [], 0)
    ssh.enqueue("dmidecode", [StreamLine("stderr", "dmidecode: command not found")], 127)

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
    )

    assert result.pubkey.startswith("ssh-ed25519 "), "키 등록 자체는 성공해야 한다"
    assert result.serial is None
    assert "dmidecode" in result.serial_error


@pytest.mark.asyncio
async def test_a_serial_read_that_blows_up_does_not_fail_the_registration(tmp_path):
    """연결이 끊기는 등 예외가 나도 마찬가지다."""
    ssh = FakeSSHClient()
    ssh.enqueue("authorized_keys", [], 0)
    ssh.enqueue("systemctl mask", [], 0)
    # dmidecode 응답을 안 넣어둔다 → FakeSSHClient 가 예외를 던진다

    async def fake_connect(host, **kw):
        return _FakeConn()

    result = await register_key(
        host="10.0.0.9", username="connecteve", password="secret",
        key_path=tmp_path / "id_ed25519",
        ssh_factory=lambda h, p: ssh, connect_fn=fake_connect,
    )

    assert result.pubkey.startswith("ssh-ed25519 ")
    assert result.serial is None
    assert result.serial_error
