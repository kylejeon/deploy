from __future__ import annotations

import pytest

from autodeploy.healthcheck import wait_for_cluster_ready
from autodeploy.ssh import FakeSSHClient, StreamLine


@pytest.mark.asyncio
async def test_ready_on_first_poll_when_no_bad_pods():
    fake = FakeSSHClient()
    fake.enqueue("kubectl get pods", [])

    async with fake as ssh:
        result = await wait_for_cluster_ready(ssh, poll_interval=0.01, timeout=1)

    assert result.ready is True
    assert result.polls == 1
    assert result.last_output == ""


@pytest.mark.asyncio
async def test_polls_until_ready():
    fake = FakeSSHClient()
    fake.enqueue("kubectl get pods", [StreamLine("stdout", "default api Pending")])
    fake.enqueue("kubectl get pods", [StreamLine("stdout", "default api Pending")])
    fake.enqueue("kubectl get pods", [])

    async with fake as ssh:
        result = await wait_for_cluster_ready(ssh, poll_interval=0.01, timeout=5)

    assert result.ready is True
    assert result.polls == 3


@pytest.mark.asyncio
async def test_timeout_when_pods_never_ready():
    fake = FakeSSHClient()
    for _ in range(30):
        fake.enqueue("kubectl get pods", [StreamLine("stdout", "default api Pending")])

    async with fake as ssh:
        result = await wait_for_cluster_ready(ssh, poll_interval=0.01, timeout=0.05)

    assert result.ready is False
    assert "Pending" in result.last_output


@pytest.mark.asyncio
async def test_uses_kubectl_no_headers_pattern():
    fake = FakeSSHClient()
    fake.enqueue("kubectl get pods", [])

    async with fake as ssh:
        await wait_for_cluster_ready(ssh, poll_interval=0.01, timeout=1)

    [cmd] = fake.executed
    assert "--no-headers" in cmd
    assert "grep -vE 'Running|Completed'" in cmd
    assert "|| true" in cmd  # 0-match exit code 무력화


@pytest.mark.asyncio
async def test_empty_whitespace_lines_count_as_ready():
    fake = FakeSSHClient()
    fake.enqueue("kubectl get pods", [StreamLine("stdout", "   "), StreamLine("stdout", "")])

    async with fake as ssh:
        result = await wait_for_cluster_ready(ssh, poll_interval=0.01, timeout=1)

    assert result.ready is True
