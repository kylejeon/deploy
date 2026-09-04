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
from contextlib import suppress
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from autodeploy import repository
from autodeploy.ansible_log import LineKind, ParsedLine, host_status, is_step
from autodeploy.db import connect
from autodeploy.hubctl import (
    CLEAN_MODES,
    ENVS,
    PATCH_CONFIRM_ANSWER,
    PATCH_CONFIRM_MARKER,
    SYNC_BRANCH,
    HubctlError,
    HubctlRunner,
    build_command,
    build_sync_command,
)
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



# 서버에 무언가를 새로 얹는 종류. 이들만 시작 직전에 hub-provisioning 을
# 최신으로 맞추고, 맞추지 못하면 시작하지 않는다.
#
# verify(읽기 전용)·clean·rollback 은 뺐다. 복구·점검은 Bitbucket 이 죽었을 때
# **오히려 더 필요한** 작업이라, 최신화 실패로 막아버리면 안 된다.
_NEEDS_FRESH_REPO = frozenset({JobKind.INSTALL, JobKind.CONFIGURE, JobKind.PATCH})


def _needs_fresh_repo(kind: JobKind, phase: str | None) -> bool:
    # patch 의 apply 는 이미 만들어 둔 번들을 넣는 단계다. 그 번들은 생성 시점의
    # playbook 으로 만들어졌으므로 여기서 저장소를 바꾸면 앞뒤가 어긋난다.
    return kind in _NEEDS_FRESH_REPO and phase != "apply"


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
    # configure --only (1.1.1.0 신규 이미지 전환). 다른 종류에는 쓰지 않는다.
    only: str | None = None
    # 실행 직전 hub-provisioning 을 맞출 브랜치. 비우면 main.
    sync_branch: str | None = None
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
        notifier=None,
        console_url: str | None = None,
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
        # Slack 게시. 없으면 조용히 건너뛴다 (F7 은 선택 기능이다).
        self._notifier = notifier
        self._console_url = console_url

    # ── 생성 ────────────────────────────────────────────────────────

    async def create(self, req: JobRequest) -> int:
        kind = JobKind(req.kind)
        if kind in _NON_HUBCTL:
            raise JobError(f"작업으로 실행하지 않는 종류입니다: {kind.value}")

        # patch 는 원샷이다 (phase=None) — 타겟에서 번들을 만들고 그대로 적용한다.
        # 2단계 create/apply 는 폐쇄망 반입용으로 남아 있고 `approve()` 만 쓴다.
        phase = None
        hosts = tuple(dict.fromkeys(h.strip() for h in req.hosts if h and h.strip()))
        profiles = await self._check_hosts(kind, hosts, confirm=req.confirm)

        # 명령을 먼저 조립한다 — env/ref/clean_mode 오류를 DB 에 흔적을 남기기 전에 잡는다.
        try:
            build_command(
                kind,
                hosts=hosts,
                env=req.env,
                ref=req.ref,
                ref_type=req.ref_type,
                only=req.only,
                clean_mode=req.clean_mode,
                phase=phase,
            )
            build_sync_command(req.sync_branch or SYNC_BRANCH)
        except HubctlError as exc:
            raise JobError(str(exc)) from exc

        async with connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO jobs"
                " (kind, status, env, ref, ref_type, clean_mode, only_tags,"
                "  sync_branch, started_by)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                (kind.value, req.env, req.ref, req.ref_type, req.clean_mode,
                 req.only, req.sync_branch or SYNC_BRANCH, req.started_by),
            )
            job_id = int(cur.lastrowid)
            if hosts:
                await db.executemany(
                    "INSERT INTO job_hosts (job_id, host, status, profile)"
                    " VALUES (?, ?, 'queued', ?)",
                    [(job_id, h, profiles.get(h)) for h in hosts],
                )
            await db.commit()

        await self.queue.submit(job_id, self._make_runner(job_id, hosts, req, phase))
        log.info("작업 %d 생성 (%s, hosts=%s, by=%s)", job_id, kind.value, hosts, req.started_by)
        return job_id

    async def _check_hosts(
        self, kind: JobKind, hosts: tuple[str, ...], *, confirm: str | None
    ) -> dict[str, str]:
        """대상 검증. 부수적으로 **실행 시점의 프로파일**을 함께 돌려준다.

        인벤토리를 어차피 여기서 읽으므로 한 번 더 읽지 않는다. 이 값은
        job_hosts 에 박아둔다 — 나중에 서버를 onprem 에서 hybrid 로 바꿔도
        지난 작업이 무엇으로 설치됐는지가 바뀌면 안 된다.
        """
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

        return {s.host: s.profile for s in inventory.servers if s.host in set(hosts)}

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
            # 이 화면이 생기기 전에 만들어진 patch 는 대상이 비어 있다.
            raise JobConflict(
                "적용할 대상 서버가 기록돼 있지 않습니다"
                " — 패치 화면에서 대상을 골라 다시 만드세요 (번들은 컨트롤러에 남아 있습니다)"
            )

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
        # 생성 때 조립한 것은 검증용이고, 실제로 도는 것은 여기서 만든다.
        # 인자를 한쪽에만 넣으면 화면이 시킨 것과 다른 명령이 돈다.
        command = build_command(
            req.kind,
            hosts=hosts,
            env=req.env,
            ref=req.ref,
            ref_type=req.ref_type,
            only=req.only,
            clean_mode=req.clean_mode,
            phase=phase,
        )
        step = phase or req.kind.value
        # `install` 같은 종류 이름은 단계 키가 아니다. 그대로 적으면 화면이
        # 단계 목록에서 못 찾아 끝날 때까지 진행 표시가 멈춰 있다.
        # 실제 단계는 로그가 알려주므로 그때까지는 비워둔다 (화면: "시작 중").
        first_step = step if is_step(step) else None

        async with connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET status='running', started_at=COALESCE(started_at,"
                " CURRENT_TIMESTAMP), current_step=? WHERE id=?",
                (first_step, job_id),
            )
            if hosts:
                await db.execute(
                    "UPDATE job_hosts SET status='running' WHERE job_id=? AND status='queued'",
                    (job_id,),
                )
            await repository.add_event(db, job_id, step, "info", f"실행: {command}")

        self.broker.publish(job_id, {"type": "status", "status": "running", "step": step})
        await self._notify_started(job_id, req, hosts, command)

        sink = _LogSink(self, job_id, step, current=first_step)
        await sink.start()

        # hub-provisioning 을 고른 브랜치(기본 main)로 맞춘 뒤에 시작한다. 작업은
        # 한 번에 하나만 돌므로, 여기서 당겨도 도는 중인 다른 작업의 playbook 이
        # 발밑에서 바뀌는 일은 없다 (작업 생성 시점에 당기면 그럴 수 있다).
        branch = req.sync_branch or SYNC_BRANCH
        if _needs_fresh_repo(req.kind, phase):
            sync_rc = await self._sync_repo(ticket, sink, branch)
            if sync_rc != 0:
                await sink.close()
                if ticket.cancel_requested:
                    await self._finalize_cancelled(job_id, by=None, reason="최신화 중 취소됨")
                    return
                await self._finalize(
                    job_id, hosts, status=JobStatus.FAILED, exit_code=sync_rc,
                    error=f"hub-provisioning({branch}) 최신화에 실패해 시작하지"
                          " 않았습니다 — 타겟 서버는 그대로입니다. 위 로그에서 이유를"
                          " 확인하고 다시 실행하세요.",
                    recaps={},
                )
                return

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
            await sink.close()
            await self._finalize_cancelled(job_id, by=None, reason="시작 전 취소됨")
            return
        # 원샷 patch 는 번들을 만든 뒤 `[y/N]` 로 적용 여부를 묻는다. 화면에서
        # 시작 전에 이미 확인을 받았으므로 여기서 y 를 넣는다.
        auto_reply = (
            (PATCH_CONFIRM_MARKER, PATCH_CONFIRM_ANSWER)
            if req.kind is JobKind.PATCH and phase is None
            else None
        )
        try:
            result = await runner.run(
                command,
                on_line=sink.feed,
                auto_reply=auto_reply,
                on_reply=lambda _answer: self._note_auto_apply(job_id, req.started_by),
            )
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

    async def _sync_repo(
        self, ticket: JobTicket, sink: _LogSink, branch: str = SYNC_BRANCH
    ) -> int:
        """hub-provisioning 을 최신으로 맞춘다. 0 이면 성공.

        출력은 작업 로그에 그대로 남는다 — 마지막 줄에 어떤 커밋으로 설치했는지가
        찍히므로, 나중에 "그때 뭐가 깔렸나" 를 로그만 보고 답할 수 있다.
        """
        runner = HubctlRunner(
            self.hubctl_repo,
            masker=self._masker,
            env_overrides=self._hubctl_env,
            login_shell=self._hubctl_shell,
        )
        ticket.bind_cancel(runner.cancel)
        if ticket.cancel_requested:
            return -1

        sink.feed("stdout", ParsedLine(
            f"── hub-provisioning 을 {branch} 최신으로 맞추는 중",
            LineKind.TASK, None, None,
        ))
        try:
            result = await runner.run(build_sync_command(branch), on_line=sink.feed)
        except Exception as exc:
            log.exception("hub-provisioning 최신화 실행 실패")
            sink.feed("stderr", ParsedLine(
                f"최신화를 실행할 수 없습니다: {exc}", LineKind.ERROR, None, None,
            ))
            return -1
        if result.cancelled:
            return -1
        return result.exit_code

    async def _note_auto_apply(self, job_id: int, started_by: str) -> None:
        """적용 확인에 자동으로 답했다는 사실을 기록에 남긴다.

        터미널이면 사람이 변경 앱 목록을 보고 `y` 를 눌렀을 자리다. 콘솔은 시작
        전에 확인을 받고 여기서 대신 답하는데, 그게 어디에도 안 남으면 나중에
        기록만 보고는 누가 적용을 승인했는지 알 수 없다.
        """
        async with connect(self.db_path) as db:
            await repository.add_event(
                db, job_id, "apply", "info",
                f"변경 앱 요약 뒤의 적용 확인에 y 를 보냈습니다"
                f" (시작할 때 {started_by} 님이 확인)",
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
            if status in (JobStatus.FAILED, JobStatus.CANCELLED):
                # 대상 없이 돈 단계(patch create)가 죽으면 위 루프가 job_hosts 를
                # 건드리지 않는다. 작업은 실패인데 서버는 '대기' 로 남아 목록이
                # 어긋나므로 남은 줄을 여기서 함께 닫는다.
                await db.execute(
                    "UPDATE job_hosts SET status=? WHERE job_id=?"
                    " AND status IN ('queued','running')",
                    ("failed" if status is JobStatus.FAILED else "cancelled", job_id),
                )
            await db.commit()

        self.broker.publish(
            job_id, {"type": "status", "status": status.value, "exit_code": exit_code}
        )
        if status is not JobStatus.AWAITING:
            self.broker.close_job(job_id)
        log.info("작업 %d 종료: %s (exit=%s)", job_id, status.value, exit_code)
        await self._notify_finished(job_id, status, exit_code)

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

    # ── Slack (F7) ──────────────────────────────────────────────────

    async def _notify_started(self, job_id: int, req, hosts, command: str) -> None:
        """작업당 한 번만 스레드를 만든다. patch 의 apply 는 같은 스레드에 이어 붙는다."""
        if self._notifier is None:
            return
        async with connect(self.db_path) as db:
            async with db.execute(
                "SELECT slack_thread_ts FROM jobs WHERE id=?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
        if row is not None and row["slack_thread_ts"]:
            return

        thread_ts, permalink = await self._notifier.job_started(
            job_id,
            kind=req.kind.value,
            hosts=tuple(hosts),
            env=req.env,
            ref=req.ref,
            started_by=req.started_by,
            command=command,
        )
        if not thread_ts:
            return
        async with connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET slack_thread_ts=?, slack_permalink=? WHERE id=?",
                (thread_ts, permalink, job_id),
            )
            await db.commit()

    async def _notify_finished(
        self, job_id: int, status: JobStatus, exit_code: int | None
    ) -> None:
        if self._notifier is None or status is JobStatus.AWAITING:
            return
        async with connect(self.db_path) as db:
            job = await repository.get_job_detail(db, job_id)
        if job is None or not job.get("slack_thread_ts"):
            return
        await self._notifier.job_finished(
            job_id,
            thread_ts=job["slack_thread_ts"],
            status=status.value,
            exit_code=exit_code,
            hosts=job["hosts"],
            duration=_duration(job),
            console_url=f"{self._console_url}#job/{job_id}" if self._console_url else None,
        )

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


def _duration(job: dict) -> str:
    """SQLite 의 UTC 문자열 두 개로 소요시간을 만든다."""
    start, end = job.get("started_at"), job.get("finished_at")
    if not start or not end:
        return "–"
    try:
        began = datetime.fromisoformat(start)
        ended = datetime.fromisoformat(end)
    except ValueError:
        return "–"
    total = int((ended - began).total_seconds())
    minutes, seconds = divmod(max(0, total), 60)
    return f"{minutes}분 {seconds:02d}초" if minutes else f"{seconds}초"


class _LogSink:
    """줄을 모아 DB 에 적재하고, 커밋 뒤에 방송한다."""

    __slots__ = ("_service", "_job_id", "_step", "_buffer", "_task", "_closed",
                 "_db_lines", "_overflow", "_lock", "_newly_failed", "_last_error_host",
                 "_current")

    def __init__(self, service: JobService, job_id: int, step: str,
                 *, current: str | None = None) -> None:
        self._service = service
        self._job_id = job_id
        self._step = step
        # DB 의 jobs.current_step 에 마지막으로 적어둔 값. 바뀔 때만 쓴다.
        self._current = current
        self._buffer: list[_Buffered] = []
        self._task: asyncio.Task | None = None
        self._closed = False
        self._db_lines = 0
        self._overflow = None
        self._lock = asyncio.Lock()
        # 실패한 것이 확인된 호스트 중 아직 DB 에 못 적은 것.
        self._newly_failed: set[str] = set()
        self._last_error_host: str | None = None

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
        # 호스트별 실패를 **그 자리에서** 잡는다. PLAY RECAP 은 맨 끝에만 오므로
        # 그것만 보면 한 대가 이미 죽었는데도 목록에서 40분 내내 '실행 중' 이다.
        # 여기서는 표시만 바꾼다 — 최종 판정은 여전히 RECAP 이 한다(_finalize).
        if parsed.host:
            if parsed.kind is LineKind.ERROR:
                self._newly_failed.add(parsed.host)
                self._last_error_host = parsed.host
            elif parsed.kind is LineKind.WARN:
                self._newly_failed.discard(parsed.host)   # `ignoring: [host]` 변종
        elif self._last_error_host and parsed.text.lstrip().startswith("...ignoring"):
            # ignore_errors 가 걸린 태스크에서 ansible 이 내는 줄이다. **호스트
            # 이름이 없고** 바로 앞 fatal 줄에 붙어 나오므로, 직전에 실패로 찍은
            # 호스트의 표시를 되돌린다. 이걸 안 하면 무시하기로 한 실패 때문에
            # 목록에서 멀쩡한 서버가 빨갛게 보인다.
            self._newly_failed.discard(self._last_error_host)
            self._last_error_host = None

    async def _loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._buffer and not self._newly_failed:
                return
            batch, self._buffer = self._buffer, []
            failed, self._newly_failed = self._newly_failed, set()

            events: list[dict] = []
            async with connect(self._service.db_path) as db:
                for item in batch:
                    p = item.parsed
                    if self._db_lines >= MAX_DB_LINES:
                        self._write_overflow(p.text)
                        continue
                    # created_at 을 직접 넣는다. CURRENT_TIMESTAMP 로 두면 그 값을
                    # 알려고 다시 읽어야 하고, 안 보내면 실시간 줄만 시각이 비어
                    # 재생 경로와 모양이 달라진다.
                    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
                    cur = await db.execute(
                        "INSERT INTO script_logs (job_id, step, stream, line, host, kind, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (self._job_id, p.step or self._step, item.stream, p.text,
                         p.host, p.kind.value, stamp),
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
                        "created_at": stamp,
                    })
                    if self._db_lines == MAX_DB_LINES:
                        await repository.add_event(
                            db, self._job_id, self._step, "warn",
                            f"로그가 {MAX_DB_LINES}줄을 넘어 이후는 파일에만 기록합니다:"
                            f" {self._overflow_path()}",
                        )
                if failed:
                    await self._mark_failed(db, failed)
                advanced = await self._advance_step(db)
                await db.commit()

            # 커밋한 뒤에 방송한다. 끊긴 구독자가 after= 로 재연결했을 때
            # 방송된 줄이 DB 에 이미 있어야 정확히 이어붙는다.
            self._service.broker.publish_many(self._job_id, events)
            if advanced:
                # 화면이 단계 표시를 다시 그리게 한다. 이걸 안 보내면 다음
                # 폴링(4초)까지 이전 단계에 머문다.
                self._service.broker.publish(
                    self._job_id, {"type": "status", "status": "running", "step": advanced}
                )

    async def _advance_step(self, db) -> str | None:
        """진행 단계가 넘어갔으면 jobs.current_step 을 갱신한다.

        예전에는 시작할 때 한 번만 적고 끝이라, 50분짜리 설치가 끝날 때까지
        화면의 단계 표시가 한 칸도 안 움직였다. 파서가 이미 줄마다 단계를
        붙이고 있으므로 여기서 바뀐 것만 반영한다.
        """
        step = self._step
        if not is_step(step) or step == self._current:
            return None
        await db.execute("UPDATE jobs SET current_step=? WHERE id=?", (step, self._job_id))
        self._current = step
        return step

    async def _mark_failed(self, db, hosts: set[str]) -> None:
        """도는 동안 죽은 호스트를 목록에서 바로 실패로 보이게 한다.

        `status='running'` 인 것만 건드린다 — 이미 결말이 난 호스트를 덮어쓰지
        않기 위해서다. 최종 판정은 여전히 `_finalize` 가 PLAY RECAP 으로 다시
        쓴다. 여기서 하는 것은 40분짜리 작업에서 한 대가 5분 만에 죽었는데도
        목록이 끝까지 '실행 중' 으로 보이는 것을 막는 일이다.
        """
        holes = ",".join("?" * len(hosts))
        await db.execute(
            f"UPDATE job_hosts SET status='failed'"
            f" WHERE job_id=? AND status='running' AND host IN ({holes})",
            (self._job_id, *sorted(hosts)),
        )

    def _overflow_path(self) -> Path:
        return self._service.log_dir / f"job-{self._job_id}.log"

    def _write_overflow(self, line: str) -> None:
        if self._overflow is None:
            path = self._overflow_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._overflow = path.open("a", encoding="utf-8")
        self._overflow.write(line + "\n")

    async def close(self) -> None:
        """루프를 **취소하지 않고** 플래그로 세운다.

        flush 는 배치를 버퍼에서 통째로 꺼낸 **뒤에** DB 를 기다린다. 그 사이에
        cancel() 이 들어오면 꺼내둔 배치가 함께 사라진다 — 그리고 close() 가
        불리는 시점이 정확히 작업이 끝나는 순간이라, 마지막 0.15초가 통으로
        날아간다. 거기 있는 것이 하필 PLAY RECAP 과 오류 줄이다.

        플래그로 세우면 루프가 하던 flush 를 끝내고 제 발로 나간다. 대신 최대
        FLUSH_INTERVAL 만큼 늦게 끝난다 (0.15초).
        """
        self._closed = True
        if self._task is not None:
            try:
                # DB 가 걸려 close() 가 영영 안 끝나는 일은 막는다. 취소는
                # 여기서만, 그것도 마지막 수단으로 쓴다.
                await asyncio.wait_for(self._task, FLUSH_INTERVAL * 20)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
        await self.flush()
        if self._overflow is not None:
            self._overflow.close()
            self._overflow = None
