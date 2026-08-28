"""본체 시리얼 읽기 (`dmidecode -s system-serial-number`)."""
from __future__ import annotations

import pytest

from autodeploy.node_info import (
    SERIAL_COMMAND,
    SerialRead,
    fetch_serial,
    parse_serial,
    read_serial,
)
from autodeploy.ssh import FakeSSHClient, StreamLine

# asyncio_mode = "auto" 라 async 테스트에는 마크가 필요 없다.


def out(*lines: str) -> list[StreamLine]:
    return [StreamLine("stdout", line) for line in lines]


def err(*lines: str) -> list[StreamLine]:
    return [StreamLine("stderr", line) for line in lines]


# ── 명령 모양 ─────────────────────────────────────────
def test_the_password_is_not_on_the_command_line():
    """`printf <비번> | sudo -S` 로 만들면 그 줄이 타겟의 ps 에 그대로 보인다."""
    assert "sudo -S" in SERIAL_COMMAND, "비밀번호는 표준입력으로 받아야 한다"
    assert "dmidecode -s system-serial-number" in SERIAL_COMMAND
    assert "printf" not in SERIAL_COMMAND and "echo" not in SERIAL_COMMAND
    assert "-p ''" in SERIAL_COMMAND, "프롬프트가 출력에 섞이면 값으로 오해한다"


async def test_the_password_travels_on_stdin():
    ssh = FakeSSHClient()
    ssh.enqueue("dmidecode", out("PF3ABCDE"), 0)
    async with ssh:
        await read_serial(ssh, sudo_password="sup3r-s3cret-pw")

    assert ssh.stdins == ["sup3r-s3cret-pw\n"]
    assert "sup3r-s3cret-pw" not in ssh.executed[0]


# ── 값 고르기 ─────────────────────────────────────────
def test_a_plain_value_is_the_serial():
    assert parse_serial(["PF3ABCDE"]) == "PF3ABCDE"
    assert parse_serial(["  PF3ABCDE  "]) == "PF3ABCDE"


def test_the_banner_line_is_skipped():
    """판본에 따라 `# dmidecode 3.3` 이 앞에 붙는다. 값은 늘 마지막 줄이다."""
    assert parse_serial(["# dmidecode 3.3", "PF3ABCDE"]) == "PF3ABCDE"


@pytest.mark.parametrize(
    "value",
    ["Default string", "To Be Filled By O.E.M.", "System Serial Number",
     "Not Specified", "None", "0123456789", "<OUT OF SPEC>", "default STRING", ""],
)
def test_placeholders_are_not_serials(value):
    """제조사가 번호를 안 넣은 기계는 서로 같은 문자열을 내놓는다.

    그대로 저장하면 **서버마다 같은 값**이 뜨는데, 그건 비어 있는 것보다 나쁘다 —
    다른 기계를 구분한다고 착각하게 만든다.
    """
    assert parse_serial([value]) is None


def test_nothing_at_all_is_not_a_serial():
    assert parse_serial([]) is None
    assert parse_serial(["", "   "]) is None


# ── 읽기 ──────────────────────────────────────────────
async def test_reading_a_serial():
    ssh = FakeSSHClient()
    ssh.enqueue("dmidecode", out("PF3ABCDE"), 0)
    async with ssh:
        read = await read_serial(ssh, sudo_password="pw")

    assert read == SerialRead(
        serial="PF3ABCDE", exit_code=0, stdout=("PF3ABCDE",), stderr=(), error=None
    )
    assert read.ok


async def test_stderr_is_never_mistaken_for_the_value():
    """경고를 값으로 읽으면 서버 목록에 오류 문구가 시리얼로 박힌다.

    `#` 로 시작하지 않는 줄을 고른 이유가 있다 — 배너만 걸러도 통과하는 검사면
    표준오류를 통째로 섞어 읽어도 안 걸린다.
    """
    ssh = FakeSSHClient()
    ssh.enqueue("dmidecode", err("sudo: unable to resolve host node1"), 0)
    async with ssh:
        read = await read_serial(ssh, sudo_password="pw")

    assert read.serial is None
    assert read.error


async def test_a_missing_dmidecode_says_so():
    ssh = FakeSSHClient()
    ssh.enqueue("dmidecode", err("bash: dmidecode: command not found"), 127)
    async with ssh:
        read = await read_serial(ssh, sudo_password="pw")

    assert read.serial is None
    assert "dmidecode" in read.error and "설치" in read.error


async def test_a_rejected_sudo_says_so():
    ssh = FakeSSHClient()
    ssh.enqueue("dmidecode", err("sudo: 1 incorrect password attempt"), 1)
    async with ssh:
        read = await read_serial(ssh, sudo_password="wrong")

    assert read.serial is None
    assert "sudo" in read.error


async def test_a_machine_without_a_serial_says_which_value_came_back():
    """'왜 안 뜨냐'는 물음에 답이 되려면 기계가 뭐라고 했는지가 있어야 한다."""
    ssh = FakeSSHClient()
    ssh.enqueue("dmidecode", out("Default string"), 0)
    async with ssh:
        read = await read_serial(ssh, sudo_password="pw")

    assert read.serial is None
    assert "Default string" in read.error


async def test_the_connection_is_closed_when_fetching():
    ssh = FakeSSHClient()
    ssh.enqueue("dmidecode", out("PF3ABCDE"), 0)

    read = await fetch_serial(
        host="192.0.2.10", username="connecteve", key_path="~/.ssh/id_ed25519",
        sudo_password="pw", ssh_factory=lambda: ssh,
    )

    assert read.serial == "PF3ABCDE"
    assert ssh.connected is False, "세션을 열어둔 채 끝내면 타겟에 연결이 쌓인다"
