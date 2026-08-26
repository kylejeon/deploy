"""작업 실행 서비스 — 큐 + hubctl 러너 + 로그 적재 + SSE 를 잇는다.

dev-spec-web-console §F3~§F6. API 핸들러는 여기만 호출하고, 큐·서브프로세스·DB 를
직접 다루지 않는다.

## 로그를 모아서 넣는 이유

ansible 은 초당 수십 줄을 낸다. 줄마다 commit 하면 SQLite 가 매번 fsync 를 돌아
설치가 눈에 띄게 느려진다. 그렇다고 트랜잭션을 길게 열어두면 **다른 커넥션이
그 줄을 못 본다** — SSE 재연결(`?after=`)이 DB 를 다시 읽는 구조라 치명적이다.

그래서 짧은 주기(0.15초)로 모아서 한 트랜잭션에 넣고 커밋한 **뒤에** 방송한다.
방송되는 줄은 이미 DB 에 있으므로, 구독이 끊겨도 클라이언트가 마지막 id 로
재연결해 정확히 이어붙일 수 있다.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from autodeploy import repository
from autodeploy.ansible_log import ParsedLine, host_status
from autodeploy.db import connect
from autodeploy.hubctl import CLEAN_MODES, ENVS, HubctlError, HubctlRunner, build_command
from autodeploy.inventory import load_inventory
from autodeploy.masking import SecretMasker
from autodeploy.models import JobKind, JobStatus
from autodeploy.queue import CancelOutcome, JobQueue, JobTicket
from autodeploy.web.sse import SseBroker

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 0.15
FLUSH_MAX_ROWS = 200

# 작업당 DB 로그 상한 (§9). 넘으면 이후 줄은 파일에만 쌓는다 — 한 번 폭주한
# 작업이 DB 를 통째로 부풀려 다른 작업 조회까지 느리게 만드는 것을 막는다.
MAX_DB_LINES = 200_000

# hubctl 을 쓰지 않는 종류. 여기서 실행하지 않는다.
_NON_HUBCTL = frozenset({JobKind.SSH_KEY})

# 대상 호스트가 필요한 종류. patch create 만 예외 (컨트롤러 로컬 실행).
_HOSTLESS_PHASES = {("patch", "create")}


class JobError(RuntimeError):
    """요청이 잘못됐다 — 400."""


class JobConflict(RuntimeError):
    """지금 상태에서 할 수 없다 — 409."""


@dataclass(frozen=True, slots=True)
class JobRequest:
    kind: JobKind
    started_by: str
    hosts: tuple[str, ...] = ()
    env: str | None = None
    ref: str | None = None
    ref_type: str | None = None
    clean_mode: str | None = None
    confirm: str | None = None


@dataclass(slots=True)
class _Buffered:
    stream: str
    parsed: ParsedLine


class JobService:
    def __init__(
        self,
        *,
        db_path: str | Path,
        hubctl_repo: str | Path,
        inventory_path: str | Path,
        queue: JobQueue,
        broker: SseBroker,
        become_password: str = "",
        masker: SecretMasker | None = None,
        hubctl_env: dict[str, str] | None = None,
        hubctl_shell: Sequence[str] = ("zsh", "-lc"),
        log_dir: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.hubctl_repo = Path(hubctl_repo).expanduser()
        self.inventory_path = Path(inventory_path).expanduser()
        self.queue = queue
        self.broker = broker
        self._become_password = become_password
        self._masker = masker or SecretMasker()
        self._hubctl_env = dict(hubctl_env or {})
        self._hubctl_shell = tuple(hubctl_shell)
        self.log_dir = (
            Path(log_dir).expanduser() if log_dir is not None
            else self.db_path.parent / "joblogs"
        )
        self._runners: dict[int, HubctlRunner] = {}

    # ── 생성 ────────────────────────────────────────────────────────

    async def create(self, req: JobRequest) -> int:
        kind = JobKind(req.kind)
        if kind in _NON_HUBCTL:
            raise JobError(f"작업으로 실행하지 않는 종류입니다: {kind.value}")

        phase = "create" if kind is JobKind.PATCH else None
        hosts = tuple(dict.fromkeys(h.strip() for h in req.hosts if h and h.strip()))

        if (kind.value, phase) not in _HOSTLESS_PHASES:
            await self._check_hosts(kind, hosts, confirm=req.confirm)
        elif hosts:
            raise JobError("patch 는 번들 생성 후 승인 단계에서 대상을 지정합니다")

        # 명령을 먼저 조립한다 — env/ref/clean_mode 오류를 DB 에 흔적을 남기기 전에 잡는다.
        try:
            build_command(
                kind,
                hosts=hosts,
                env=req.env,
                ref=req.ref,
                ref_type=req.ref_type,
                clean_mode=req.clean_mode,
                phase=phase,
            )
        except HubctlError as exc:
            raise JobError(str(exc)) from exc

        async with connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO jobs (kind, status, env, ref, ref_type, clean_mode, started_by)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (kind.value, req.env, req.ref, req.ref_type, req.clean_mode, req.started_by),
            )
            job_id = int(cur.lastrowid)
            if hosts:
                await db.executemany(
                    "INSERT INTO job_hosts (job_id, host, status) VALUES (?, ?, 'queued')",
                    [(job_id, h) for h in hosts],
                )
            await db.commit()

        await self.queue.submit(job_id, self._make_runner(job_id, hosts, req, phase))
        log.info("작업 %d 생성 (%s, hosts=%s, by=%s)", job_id, kind.value, hosts, req.started_by)
        return job_id

    async def _check_hosts(
        self, kind: JobKind, hosts: tuple[str, ...], *, confirm: str | None
    ) -> None:
        if not hosts:
            raise JobError("대상 서버를 하나 이상 선택하세요")

        inventory = load_inventory(self.inventory_path)
        known = {s.host for s in inventory.servers}
        unknown = [h for h in hosts if h not in known]
        if unknown:
            raise JobError(f"인벤토리에 없는 서버입니다: {', '.join(unknown)}")

        # AC-16: 키가 없으면 SSH 로 죽는다. 설치를 30분 돌리고 나서 알게 되는 것보다
        # 시작 전에 거르는 편이 낫다.
        async with connect(self.db_path) as db:
            meta = await repository.get_server_meta(db)
        missing = [h for h in hosts if not (meta.get(h) or {}).get("key_installed_at")]
        if missing:
            raise JobError(
                f"SSH 키가 등록되지 않은 서버가 있습니다: {', '.join(missing)}"
                " — 서버 화면에서 키 등록을 먼저 실행하세요"
            )

        if kind is JobKind.CLEAN:
            if len(hosts) != 1:
                raise JobError("초기화는 한 번에 한 대만 가능합니다")
            # 화면 검증만 믿지 않는다 (§7). 파괴적 작업이라 서버에서 다시 대조한다.
            if (confirm or "").strip() != hosts[0]:
                raise JobError("확인을 위해 대상 호스트명을 정확히 입력하세요")

    # ── 취소 / 승인 ─────────────────────────────────────────────────

    async def cancel(self, job_id: int, *, by: str) -> str:
        outcome = await self.queue.cancel(job_id)
        if outcome is CancelOutcome.DEQUEUED:
            # 프로세스가 뜬 적 없다. 워커가 잡지 않으므로 여기서 확정한다.
            await self._finalize_cancelled(job_id, by=by, reason="시작 전 취소됨")
            return outcome.value
        if outcome is CancelOutcome.REQUESTED:
            async with connect(self.db_path) as db:
                await db.execute("UPDATE jobs SET cancel_by=? WHERE id=?", (by, job_id))
                await db.commit()
            return outcome.value

        # 큐에 없다 = 이미 끝났거나, patch 승인 대기 중이다.
        status = await self._status(job_id)
        if status is None:
            raise JobError(f"작업 {job_id} 을(를) 찾을 수 없습니다")
        if status == JobStatus.AWAITING:
            await self.reject(job_id, by=by)
            return CancelOutcome.DEQUEUED.value
        raise JobConflict(f"이미 종료된 작업입니다 (상태: {status})")

    async def approve(self, job_id: int, *, by: str) -> None:
        """patch 번들 적용 승인 → `patch apply` 실행 (AC-10)."""
        job = await self._require_awaiting(job_id)
        hosts = tuple(h["host"] for h in job["hosts"])
        if not hosts:
            raise JobConflict("적용할 대상 서버가 기록돼 있지 않습니다")

        req = JobRequest(
            kind=JobKind.PATCH,
            started_by=job["started_by"],
            hosts=hosts,
            ref=job["ref"],
            ref_type=job["ref_type"],
        )
        async with connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET status='queued', finished_at=NULL WHERE id=?", (job_id,)
            )
            await repository.add_event(db, job_id, "apply", "info", f"{by} 님이 적용을 승인했습니다")
        await self.queue.submit(job_id, self._make_runner(job_id, hosts, req, "apply"))

    async def reject(self, job_id: int, *, by: str) -> None:
        """적용 거부. 번들은 컨트롤러에 남는다 — 나중에 다시 승인할 수 있다."""
        await self._require_awaiting(job_id)
        await self._finalize_cancelled(job_id, by=by, reason="적용이 거부되었습니다 (번들은 유지)")

    async def expire_awaiting(self, *, older_than_hours: int = 24) -> list[int]:
        """방치된 승인 대기를 정리한다 (§9). 번들은 남는다."""
        async with connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM jobs WHERE status='awaiting'"
                " AND created_at <= datetime('now', ?)",
                (f"-{int(older_than_hours)} hours",),
            ) as cur:
                ids = [int(r["id"]) for r in await cur.fetchall()]
        for job_id in ids:
            await self._finalize_cancelled(
                job_id, by="system", reason=f"{older_than_hours}시간 내 승인이 없어 자동 취소 (번들은 유지)"
            )
        return ids

    # ── 실행 ────────────────────────────────────────────────────────

    def _make_runner(self, job_id: int, hosts, req: JobRequest, phase: str | None):
        async def run(ticket: JobTicket) -> None:
            await self._execute(job_id, ticket, hosts=hosts, req=req, phase=phase)

        return run

    async def _execute(
        self, job_id: int, ticket: JobTicket, *, hosts, req: JobRequest, phase: str | None
    ) -> None:
        command = build_command(
            req.kind,
            hosts=hosts,
            env=req.env,
            ref=req.ref,
            ref_type=req.ref_type,
            clean_mode=req.clean_mode,
            phase=phase,
        )
        step = phase or req.kind.value

        async with connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET status='running', started_at=COALESCE(started_at,"
                " CURRENT_TIMESTAMP), current_step=? WHERE id=?",
                (step, job_id),
            )
            if hosts:
                await db.execute(
                    "UPDATE job_hosts SET status='running' WHERE job_id=? AND status='queued'",
                    (job_id,),
                )
            await repository.add_event(db, job_id, step, "info", f"실행: {command}")

        self.broker.publish(job_id, {"type": "status", "status": "running", "step": step})

        runner = HubctlRunner(
            self.hubctl_repo,
            become_password=self._become_password,
            masker=self._masker,
            env_overrides=self._hubctl_env,
            login_shell=self._hubctl_shell,
        )
        self._runners[job_id] = runner
        ticket.bind_cancel(runner.cancel)

        if ticket.cancel_requested:
            # 큐에서 꺼낸 뒤 프로세스가 뜨기 전에 취소가 들어왔다.
            self._runners.pop(job_id, None)
            await self._finalize_cancelled(job_id, by=None, reason="시작 전 취소됨")
            return

        sink = _LogSink(self, job_id, step)
        await sink.start()
        try:
            result = await runner.run(command, on_line=sink.feed)
        except Exception as exc:
            log.exception("작업 %d 실행 실패", job_id)
            await sink.close()
            await self._finalize(job_id, hosts, status=JobStatus.FAILED, exit_code=None,
                                 error=f"실행할 수 없습니다: {exc}", recaps={})
            return
        finally:
            self._runners.pop(job_id, None)
            await sink.close()

        cancelled = result.cancelled or ticket.cancel_requested
        if cancelled:
            status = JobStatus.CANCELLED
        elif result.exit_code != 0:
            status = JobStatus.FAILED
        elif req.kind is JobKind.PATCH and phase == "create":
            # 번들만 만들었다. 사람이 승인해야 타겟에 적용한다 (AC-10).
            status = JobStatus.AWAITING
        else:
            status = JobStatus.SUCCEEDED

        await self._finalize(
            job_id, hosts, status=status, exit_code=result.exit_code,
            error=None if status is not JobStatus.FAILED else f"종료 코드 {result.exit_code}",
            recaps=result.recaps,
        )

    async def _finalize(
        self, job_id: int, hosts, *, status: JobStatus, exit_code: int | None,
        error: str | None, recaps: dict,
    ) -> None:
        async with connect(self.db_path) as db:
            if status is JobStatus.AWAITING:
                await db.execute(
                    "UPDATE jobs SET status=?, exit_code=? WHERE id=?",
                    (status.value, exit_code, job_id),
                )
            else:
                await db.execute(
                    "UPDATE jobs SET status=?, exit_code=?, finished_at=CURRENT_TIMESTAMP,"
                    " error_message=COALESCE(?, error_message) WHERE id=?",
                    (status.value, exit_code, error, job_id),
                )
            for host in hosts:
                if status is JobStatus.CANCELLED:
                    host_state, recap = "cancelled", None
                else:
                    recap = recaps.get(host)
                    host_state = host_status(recaps, host)
                await db.execute(
                    "UPDATE job_hosts SET status=?, recap_ok=?, recap_changed=?,"
                    " recap_failed=?, recap_unreachable=? WHERE job_id=? AND host=?",
                    (
                        host_state,
                        recap.ok if recap else None,
                        recap.changed if recap else None,
                        recap.failed if recap else None,
                        recap.unreachable if recap else None,
                        job_id,
                        host,
                    ),
                )
            await db.commit()

        self.broker.publish(
            job_id, {"type": "status", "status": status.value, "exit_code": exit_code}
        )
        if status is not JobStatus.AWAITING:
            self.broker.close_job(job_id)
        log.info("작업 %d 종료: %s (exit=%s)", job_id, status.value, exit_code)

    async def _finalize_cancelled(self, job_id: int, *, by: str | None, reason: str) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET status='cancelled', finished_at=CURRENT_TIMESTAMP,"
                " cancel_by=COALESCE(?, cancel_by), error_message=COALESCE(error_message, ?)"
                " WHERE id=?",
                (by, reason, job_id),
            )
            await db.execute(
                "UPDATE job_hosts SET status='cancelled' WHERE job_id=?"
                " AND status IN ('queued','running')",
                (job_id,),
            )
            await repository.add_event(db, job_id, "cancel", "warn", reason)
        self.broker.publish(job_id, {"type": "status", "status": "cancelled"})
        self.broker.close_job(job_id)

    # ── 조회 헬퍼 ───────────────────────────────────────────────────

    async def _status(self, job_id: int) -> str | None:
        async with connect(self.db_path) as db:
            async with db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)) as cur:
                row = await cur.fetchone()
        return row["status"] if row else None

    async def _require_awaiting(self, job_id: int) -> dict:
        async with connect(self.db_path) as db:
            job = await repository.get_job_detail(db, job_id)
        if job is None:
            raise JobError(f"작업 {job_id} 을(를) 찾을 수 없습니다")
        if job["status"] != JobStatus.AWAITING.value:
            raise JobConflict(f"승인 대기 중인 작업이 아닙니다 (상태: {job['status']})")
        return job


class _LogSink:
    """줄을 모아 DB 에 적재하고, 커밋 뒤에 방송한다."""

    __slots__ = ("_service", "_job_id", "_step", "_buffer", "_task", "_closed",
                 "_db_lines", "_overflow", "_lock")

    def __init__(self, service: JobService, job_id: int, step: str) -> None:
        self._service = service
        self._job_id = job_id
        self._step = step
        self._buffer: list[_Buffered] = []
        self._task: asyncio.Task | None = None
        self._closed = False
        self._db_lines = 0
        self._overflow = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with connect(self._service.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) AS n FROM script_logs WHERE job_id=?", (self._job_id,)
            ) as cur:
                self._db_lines = int((await cur.fetchone())["n"])
        self._task = asyncio.create_task(self._loop(), name=f"logsink-{self._job_id}")

    def feed(self, stream: str, parsed: ParsedLine) -> None:
        self._buffer.append(_Buffered(stream, parsed))
        self._step = parsed.step or self._step

    async def _loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(FLUSH_INTERVAL)
                await self.flush()
        except asyncio.CancelledError:
            raise

    async def flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer, []

            events: list[dict] = []
            async with connect(self._service.db_path) as db:
                for item in batch:
                    p = item.parsed
                    if self._db_lines >= MAX_DB_LINES:
                        self._write_overflow(p.text)
                        continue
                    cur = await db.execute(
                        "INSERT INTO script_logs (job_id, step, stream, line, host, kind)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (self._job_id, p.step or self._step, item.stream, p.text,
                         p.host, p.kind.value),
                    )
                    self._db_lines += 1
                    events.append({
                        "type": "line",
                        "id": int(cur.lastrowid),
                        "step": p.step or self._step,
                        "stream": item.stream,
                        "line": p.text,
                        "host": p.host,
                        "kind": p.kind.value,
                    })
                    if self._db_lines == MAX_DB_LINES:
                        await repository.add_event(
                            db, self._job_id, self._step, "warn",
                            f"로그가 {MAX_DB_LINES}줄을 넘어 이후는 파일에만 기록합니다:"
                            f" {self._overflow_path()}",
                        )
                await db.commit()

            # 커밋한 뒤에 방송한다. 끊긴 구독자가 after= 로 재연결했을 때
            # 방송된 줄이 DB 에 이미 있어야 정확히 이어붙는다.
            self._service.broker.publish_many(self._job_id, events)

    def _overflow_path(self) -> Path:
        return self._service.log_dir / f"job-{self._job_id}.log"

    def _write_overflow(self, line: str) -> None:
        if self._overflow is None:
            path = self._overflow_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._overflow = path.open("a", encoding="utf-8")
        self._overflow.write(line + "\n")

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()
        if self._overflow is not None:
            self._overflow.close()
            self._overflow = None
