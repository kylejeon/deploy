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
    assert "sudo ./setup-onpremise.sh" in cmd
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
