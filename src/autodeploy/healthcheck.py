"""kubectl 기반 cluster readiness 폴링. dev-spec F2.5."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from autodeploy.ssh import LineCallback, SSHClient

# --no-headers로 헤더 제거. grep -v로 Running/Completed 제외.
# `|| true`로 grep이 0 match 시 exit 1 반환하는 걸 무력화 (출력 비어있으면 정상).
_CHECK_CMD = "kubectl get pods -A --no-headers 2>&1 | grep -vE 'Running|Completed' || true"


@dataclass(frozen=True, slots=True)
class HealthResult:
    ready: bool
    last_output: str
    polls: int


async def wait_for_cluster_ready(
    ssh: SSHClient,
    *,
    poll_interval: float = 10.0,
    timeout: float = 600.0,
    on_line: LineCallback | None = None,
) -> HealthResult:
    """kubectl 출력이 비어있을 때까지 폴링. timeout 초과 시 ready=False.

    last_output에는 마지막 폴 시점의 비정상 pod 정보가 담겨 디버깅에 사용.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    polls = 0
    last_output_lines: list[str] = []

    while True:
        polls += 1
        last_output_lines.clear()

        async def collect(line):
            last_output_lines.append(line.line)
            if on_line is not None:
                r = on_line(line)
                if asyncio.iscoroutine(r):
                    await r

        await ssh.exec(_CHECK_CMD, on_line=collect)
        # `|| true` 덕분에 exit code는 항상 0

        if not any(l.strip() for l in last_output_lines):
            return HealthResult(ready=True, last_output="", polls=polls)

        if loop.time() >= deadline:
            return HealthResult(
                ready=False,
                last_output="\n".join(last_output_lines),
                polls=polls,
            )
        await asyncio.sleep(poll_interval)
