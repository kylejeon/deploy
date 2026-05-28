"""SSH 추상 (Protocol) + asyncssh 기반 실 구현 + 테스트용 Fake.

dev-spec F2.1. workflow·git_sync·scripts·healthcheck는 SSHClient만 의존.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, runtime_checkable

import asyncssh

log = logging.getLogger(__name__)

_T = TypeVar("_T")


async def _retry_async(
    coro_fn: Callable[[], Awaitable[_T]],
    *,
    attempts: int,
    backoff: Sequence[float],
    retryable: tuple[type[BaseException], ...] = (OSError, asyncssh.Error),
    label: str = "operation",
) -> _T:
    """coro_fn을 최대 attempts번 시도. 실패 사이에 backoff[i] 초 대기.

    backoff가 attempts보다 짧으면 마지막 값을 반복 사용. retryable 예외만 재시도.
    모두 실패하면 마지막 예외를 그대로 raise.
    """
    last_exc: BaseException | None = None
    for i in range(attempts):
        if i > 0:
            wait = backoff[min(i - 1, len(backoff) - 1)]
            log.warning("%s attempt %d/%d failed; retrying in %.1fs", label, i, attempts, wait)
            await asyncio.sleep(wait)
        try:
            return await coro_fn()
        except retryable as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


@dataclass(frozen=True, slots=True)
class StreamLine:
    stream: str  # 'stdout' | 'stderr'
    line: str


LineCallback = Callable[[StreamLine], Awaitable[None] | None]


class SSHError(RuntimeError):
    pass


@runtime_checkable
class SSHClient(Protocol):
    async def __aenter__(self) -> "SSHClient": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def exec(self, command: str, on_line: LineCallback | None = None) -> int: ...


async def _invoke(cb: LineCallback | None, line: StreamLine) -> None:
    if cb is None:
        return
    r = cb(line)
    if inspect.isawaitable(r):
        await r


class AsyncSSHClient:
    """asyncssh 기반 SSHClient.

    known_hosts 기본값을 **명시적으로 `None`** 으로 둔다. asyncssh 규약상:
      - kwarg 미지정 → 기본 `~/.ssh/known_hosts` 파일 검증
      - `None` 명시 → 검증 완전 비활성 (asyncssh 공식 문서 확인됨)
      - 빈 튜플 `()` → 빈 키 리스트와 매칭 시도 → 항상 실패 (직관과 반대)

    사내·폐쇄망 운영 가정에서 host key 검증을 끄고 다닌다. 병원 서버 재설치·
    IP 변경 시에도 봇이 막히지 않도록.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 22,
        known_hosts: Any = None,
        connect_attempts: int = 3,
        connect_backoff: Sequence[float] = (1.0, 2.0),
    ) -> None:
        self._host = host
        self._user = username
        self._password = password
        self._port = port
        self._known_hosts = known_hosts
        self._connect_attempts = connect_attempts
        self._connect_backoff = connect_backoff
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self) -> "AsyncSSHClient":
        async def _attempt() -> asyncssh.SSHClientConnection:
            return await asyncssh.connect(
                host=self._host,
                port=self._port,
                username=self._user,
                password=self._password,
                known_hosts=self._known_hosts,
            )

        try:
            self._conn = await _retry_async(
                _attempt,
                attempts=self._connect_attempts,
                backoff=self._connect_backoff,
                label=f"SSH connect {self._host}",
            )
        except (OSError, asyncssh.Error) as exc:
            raise SSHError(
                f"SSH connect failed after {self._connect_attempts} attempts: {self._host}: {exc}"
            ) from exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def exec(self, command: str, on_line: LineCallback | None = None) -> int:
        if self._conn is None:
            raise SSHError("not connected")
        process = await self._conn.create_process(command)
        try:
            async def reader(name: str, stream: Any) -> None:
                async for raw in stream:
                    await _invoke(on_line, StreamLine(name, raw.rstrip("\r\n")))
            await asyncio.gather(
                reader("stdout", process.stdout),
                reader("stderr", process.stderr),
            )
            result = await process.wait()
            return result.exit_status if result.exit_status is not None else -1
        finally:
            process.close()


class FakeSSHClient:
    """테스트용. enqueue된 substring 패턴에 매칭되는 응답을 순차 반환."""

    def __init__(self, *, fail_connect: bool = False) -> None:
        self.connected = False
        self.executed: list[str] = []
        self._responses: list[tuple[str, list[StreamLine], int, bool]] = []
        self._fail_connect = fail_connect

    def enqueue(
        self,
        pattern: str,
        lines: list[StreamLine] | None = None,
        exit_code: int = 0,
        *,
        repeat: bool = False,
    ) -> None:
        """repeat=True면 응답을 pop하지 않고 계속 재사용 (폴링 시나리오에 유용)."""
        self._responses.append((pattern, list(lines or []), exit_code, repeat))

    async def __aenter__(self) -> "FakeSSHClient":
        if self._fail_connect:
            raise SSHError("simulated connect failure")
        self.connected = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.connected = False

    async def exec(self, command: str, on_line: LineCallback | None = None) -> int:
        self.executed.append(command)
        if not self.connected:
            raise SSHError("not connected")
        for i, (pattern, lines, code, repeat) in enumerate(self._responses):
            if pattern in command:
                if not repeat:
                    self._responses.pop(i)
                for line in lines:
                    await _invoke(on_line, line)
                return code
        raise AssertionError(f"FakeSSHClient: unexpected command: {command!r}")
