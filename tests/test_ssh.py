from __future__ import annotations

import time

import pytest

from autodeploy.ssh import FakeSSHClient, SSHError, StreamLine, _retry_async


@pytest.mark.asyncio
async def test_fake_records_executed():
    fake = FakeSSHClient()
    fake.enqueue("ls", [StreamLine("stdout", "file1.txt")])
    async with fake as ssh:
        rc = await ssh.exec("ls -la")
    assert rc == 0
    assert fake.executed == ["ls -la"]


@pytest.mark.asyncio
async def test_fake_dispatches_lines_to_async_callback():
    fake = FakeSSHClient()
    fake.enqueue(
        "echo",
        [StreamLine("stdout", "hello"), StreamLine("stderr", "warn")],
    )
    captured: list[StreamLine] = []

    async def collect(line):
        captured.append(line)

    async with fake as ssh:
        await ssh.exec("echo hi", on_line=collect)

    assert len(captured) == 2
    assert captured[0].stream == "stdout"
    assert captured[1].stream == "stderr"


@pytest.mark.asyncio
async def test_fake_supports_sync_callback():
    fake = FakeSSHClient()
    fake.enqueue("ls", [StreamLine("stdout", "x")])
    captured: list[str] = []
    async with fake as ssh:
        await ssh.exec("ls", on_line=lambda l: captured.append(l.line))
    assert captured == ["x"]


@pytest.mark.asyncio
async def test_fake_returns_exit_code():
    fake = FakeSSHClient()
    fake.enqueue("false", [], exit_code=1)
    async with fake as ssh:
        rc = await ssh.exec("false")
    assert rc == 1


@pytest.mark.asyncio
async def test_fake_raises_when_not_connected():
    fake = FakeSSHClient()
    fake.enqueue("anything", [])
    with pytest.raises(SSHError):
        await fake.exec("anything")


@pytest.mark.asyncio
async def test_fake_raises_on_unexpected_command():
    fake = FakeSSHClient()
    fake.enqueue("ls", [])
    async with fake as ssh:
        await ssh.exec("ls -la")
        with pytest.raises(AssertionError, match="unexpected"):
            await ssh.exec("rm -rf /")


def test_asyncssh_client_imports_without_error():
    # 실 SSH 서버 없이 import + 객체 생성만 검증 (connect는 별도 통합 테스트)
    from autodeploy.ssh import AsyncSSHClient
    client = AsyncSSHClient("1.2.3.4", "user", "pw")
    assert client._host == "1.2.3.4"
    assert client._connect_attempts == 3
    assert client._connect_backoff == (1.0, 2.0)


def test_asyncssh_client_skips_host_key_verification_by_default():
    """기본값이 빈 튜플 () — asyncssh에서 host key 검증 비활성 의미.
    `None`이면 ~/.ssh/known_hosts를 사용해서 검증이 켜지므로, () 이어야 한다.
    """
    from autodeploy.ssh import AsyncSSHClient
    client = AsyncSSHClient("1.2.3.4", "user", "pw")
    assert client._known_hosts == ()


def test_asyncssh_client_known_hosts_override_respected():
    """필요 시 검증을 켜고 싶다면 known_hosts에 명시적으로 값을 넣을 수 있다."""
    from autodeploy.ssh import AsyncSSHClient
    client = AsyncSSHClient(
        "1.2.3.4", "user", "pw",
        known_hosts="/etc/ssh/known_hosts",
    )
    assert client._known_hosts == "/etc/ssh/known_hosts"


# ---------- _retry_async (QA D-1 핫픽스) ----------

@pytest.mark.asyncio
async def test_retry_returns_on_first_success():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        return "ok"

    result = await _retry_async(fn, attempts=3, backoff=(0.01, 0.01))
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_succeeds_on_third_attempt():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("transient")
        return "ok"

    result = await _retry_async(fn, attempts=3, backoff=(0.01, 0.01))
    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_raises_after_max_attempts():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise OSError(f"perma{calls}")

    with pytest.raises(OSError, match="perma3"):
        await _retry_async(fn, attempts=3, backoff=(0.01, 0.01))
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_no_sleep_on_first_attempt():
    async def fn():
        return "ok"

    start = time.monotonic()
    result = await _retry_async(fn, attempts=3, backoff=(10.0, 20.0))
    elapsed = time.monotonic() - start

    assert result == "ok"
    assert elapsed < 0.1  # 첫 시도 성공이면 backoff 안 적용


@pytest.mark.asyncio
async def test_retry_non_retryable_propagates():
    async def fn():
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        await _retry_async(
            fn,
            attempts=3,
            backoff=(0.01,),
            retryable=(OSError,),  # ValueError는 명시 안 했으니 즉시 raise
        )
