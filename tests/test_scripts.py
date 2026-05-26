from __future__ import annotations

import pytest

from autodeploy.config import ScriptSpec
from autodeploy.scripts import run_script
from autodeploy.ssh import FakeSSHClient, StreamLine


@pytest.mark.asyncio
async def test_runs_with_sudo_when_specified():
    fake = FakeSSHClient()
    fake.enqueue("setup-onpremise.sh", [])
    spec = ScriptSpec(script="setup-onpremise.sh", sudo=True, args=("{code}",))

    async with fake as ssh:
        rc = await run_script(ssh, workdir="~/gateway-infra-next", spec=spec, code="HOSP01")

    assert rc == 0
    [cmd] = fake.executed
    assert "sudo" in cmd
    assert "./setup-onpremise.sh" in cmd
    assert "DEBIAN_FRONTEND=noninteractive" in cmd  # apt debconf 비대화형 방어
    assert "HOSP01" in cmd
    assert "cd " in cmd
    assert "gateway-infra-next" in cmd


@pytest.mark.asyncio
async def test_runs_without_sudo_when_not_specified():
    fake = FakeSSHClient()
    fake.enqueue("deploy-applications.sh", [])
    spec = ScriptSpec(script="deploy-applications.sh", sudo=False, args=("w-ai", "{code}"))

    async with fake as ssh:
        await run_script(ssh, workdir="~/gateway-infra-next", spec=spec, code="HOSP42")

    [cmd] = fake.executed
    assert "sudo " not in cmd
    assert "./deploy-applications.sh" in cmd
    assert "DEBIAN_FRONTEND=noninteractive" in cmd  # non-sudo도 apt 호출 가능성 있어 동일 적용
    assert "w-ai" in cmd
    assert "HOSP42" in cmd


@pytest.mark.asyncio
async def test_args_are_quoted_to_prevent_injection():
    fake = FakeSSHClient()
    fake.enqueue("setup-site.sh", [])
    spec = ScriptSpec(script="setup-site.sh", sudo=True, args=("{code}",))

    async with fake as ssh:
        await run_script(ssh, workdir="~/foo", spec=spec, code="HOSP; rm -rf /")

    [cmd] = fake.executed
    # 위험 문자를 포함한 code가 들어와도 shlex.quote가 단일 인용으로 무력화.
    # rm -rf /가 단일 인용 안에 갇혀 있어 별도 명령으로 실행되지 않음.
    assert "'HOSP; rm -rf /'" in cmd


@pytest.mark.asyncio
async def test_propagates_exit_code():
    fake = FakeSSHClient()
    fake.enqueue("x.sh", [], exit_code=42)

    async with fake as ssh:
        rc = await run_script(
            ssh,
            workdir="/tmp",
            spec=ScriptSpec(script="x.sh", sudo=False, args=()),
            code="X",
        )

    assert rc == 42


@pytest.mark.asyncio
async def test_streams_lines_to_callback():
    fake = FakeSSHClient()
    fake.enqueue(
        "setup-site.sh",
        [StreamLine("stdout", "step 1"), StreamLine("stdout", "step 2")],
    )
    captured: list[str] = []
    spec = ScriptSpec(script="setup-site.sh", sudo=True, args=("{code}",))

    async with fake as ssh:
        await run_script(
            ssh,
            workdir="~/g",
            spec=spec,
            code="HOSP01",
            on_line=lambda l: captured.append(l.line),
        )

    assert captured == ["step 1", "step 2"]


@pytest.mark.asyncio
async def test_no_args_produces_minimal_command():
    fake = FakeSSHClient()
    fake.enqueue("simple.sh", [])
    spec = ScriptSpec(script="simple.sh", sudo=False, args=())

    async with fake as ssh:
        await run_script(ssh, workdir="/opt/x", spec=spec, code="UNUSED")

    [cmd] = fake.executed
    assert "./simple.sh" in cmd
    assert "UNUSED" not in cmd  # code not used when no args


@pytest.mark.asyncio
async def test_sudo_with_password_uses_stdin_injection():
    """sudo_password가 주어지면 printf | sudo -S 형태로 비밀번호를 stdin으로 흘린다."""
    fake = FakeSSHClient()
    fake.enqueue("setup-site.sh", [])
    spec = ScriptSpec(script="setup-site.sh", sudo=True, args=("{code}",))

    async with fake as ssh:
        await run_script(
            ssh, workdir="~/g", spec=spec, code="HOSP01",
            sudo_password="myPass#$",
        )

    [cmd] = fake.executed
    assert "printf '%s\\n'" in cmd
    assert "sudo -S -p ''" in cmd
    # 특수문자 비밀번호도 shlex.quote로 안전하게 (단일 인용 안에)
    assert "'myPass#$'" in cmd
    # 실제 스크립트도 호출됨
    assert "./setup-site.sh" in cmd
    assert "HOSP01" in cmd


@pytest.mark.asyncio
async def test_sudo_without_password_uses_plain_sudo():
    """sudo_password가 비어있으면 NOPASSWD 가정 plain sudo (기존 동작)."""
    fake = FakeSSHClient()
    fake.enqueue("setup-site.sh", [])
    spec = ScriptSpec(script="setup-site.sh", sudo=True, args=("{code}",))

    async with fake as ssh:
        await run_script(ssh, workdir="~/g", spec=spec, code="HOSP01")
        # sudo_password 기본값 ""

    [cmd] = fake.executed
    assert "sudo" in cmd  # plain sudo (NOPASSWD)
    assert "./setup-site.sh" in cmd
    assert "sudo -S" not in cmd
    assert "printf" not in cmd


@pytest.mark.asyncio
async def test_password_not_used_when_sudo_false():
    """spec.sudo=False면 sudo 자체를 안 쓰니 비밀번호도 명령에 안 들어감."""
    fake = FakeSSHClient()
    fake.enqueue("deploy-applications.sh", [])
    spec = ScriptSpec(script="deploy-applications.sh", sudo=False, args=("w-ai", "{code}"))

    async with fake as ssh:
        await run_script(
            ssh, workdir="~/g", spec=spec, code="H1",
            sudo_password="secretPassword",
        )

    [cmd] = fake.executed
    assert "sudo" not in cmd
    assert "secretPassword" not in cmd
    assert "printf" not in cmd
