"""SQLite 부트스트랩 + 연결 헬퍼 + 스키마 마이그레이션."""
from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

# 쓰기 잠금 대기 한도. ansible 로그 적재가 몰릴 때 웹 요청이 튕기지 않을 만큼.
BUSY_TIMEOUT_MS = 5000

# v2에서 jobs에 추가된 컬럼 (기존 DB는 ALTER로 붙인다)
_JOBS_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("kind", "TEXT NOT NULL DEFAULT 'install'"),
    ("env", "TEXT"),
    ("ref", "TEXT"),
    ("ref_type", "TEXT"),
    ("clean_mode", "TEXT"),
    ("exit_code", "INTEGER"),
    ("cancel_by", "TEXT"),
    ("slack_permalink", "TEXT"),
    ("target_port", "INTEGER DEFAULT 22"),
    ("only_tags", "TEXT"),
    ("sync_branch", "TEXT"),
)

# jobs 테이블 재생성 시 옮길 컬럼 (신·구 공통. ALTER를 먼저 돌린 뒤라 전부 존재한다)
_JOBS_CARRY_COLUMNS: tuple[str, ...] = (
    "id", "kind", "status", "env", "ref", "ref_type", "clean_mode", "only_tags", "sync_branch",
    "exit_code", "cancel_by", "current_step", "started_by", "slack_channel",
    "slack_thread_ts", "slack_permalink", "admin_web_url", "script_commit_sha", "error_message",
    "target_ip", "target_port", "deployment_type", "hospital_code",
    "hospital_name", "hospital_address", "created_at", "started_at", "finished_at",
)


async def init_db(path: str | Path) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        # WAL: 읽는 쪽이 쓰는 쪽을 막지 않는다. 웹 요청·백그라운드 로그 적재·
        # Slack 워크플로가 한 파일을 동시에 쓰므로 기본 롤백 저널로는 서로
        # "database is locked" 를 만든다. DB 파일에 기록되는 설정이라 한 번만 켜면 된다.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(_SCHEMA)
        await db.commit()
        if await _jobs_needs_rebuild(db):
            _backup_db_file(path)
        await _migrate(db)
        await db.commit()


def _backup_db_file(path: Path) -> None:
    """파괴적 마이그레이션(테이블 재생성) 직전 원본 파일 복사."""
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dest)
    log.warning("스키마 재생성 전 DB 백업: %s", dest)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _table_sql(db: aiosqlite.Connection, table: str) -> str:
    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    row = await cur.fetchone()
    return (row[0] or "") if row else ""


async def _jobs_needs_rebuild(db: aiosqlite.Connection) -> bool:
    """구 jobs 정의는 status CHECK에 'awaiting'이 없고 legacy 컬럼이 NOT NULL이다.

    SQLite는 CHECK 제약과 NOT NULL을 ALTER로 못 고치므로 테이블을 재생성해야 한다.
    """
    sql = await _table_sql(db, "jobs")
    return bool(sql) and "'awaiting'" not in sql


async def _migrate(db: aiosqlite.Connection) -> None:
    """기존 DB에 없는 컬럼/제약을 맞춘다 (멱등).

    schema.sql의 CREATE IF NOT EXISTS는 이미 있는 테이블엔 손대지 않으므로
    컬럼 추가와 제약 변경은 여기서 처리한다.
    """
    cols = await _columns(db, "jobs")
    for name, decl in _JOBS_NEW_COLUMNS:
        if name not in cols:
            await db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")

    log_cols = await _columns(db, "script_logs")
    for name in ("host", "kind"):
        if name not in log_cols:
            await db.execute(f"ALTER TABLE script_logs ADD COLUMN {name} TEXT")

    # 지난 작업 행은 NULL 로 남는다. 그때의 프로파일은 알 수 없으므로 지어내지
    # 않고 비워둔다 — 화면이 "기록 없음" 으로 구분해 보여준다.
    if "profile" not in await _columns(db, "job_hosts"):
        await db.execute("ALTER TABLE job_hosts ADD COLUMN profile TEXT")

    meta_cols = await _columns(db, "server_meta")
    if "anydesk_id" not in meta_cols:
        await db.execute("ALTER TABLE server_meta ADD COLUMN anydesk_id TEXT")
    # 이미 등록된 서버는 NULL 로 남는다 — 서버 화면의 '조회' 로 채운다.
    if "serial" not in meta_cols:
        await db.execute("ALTER TABLE server_meta ADD COLUMN serial TEXT")

    if await _jobs_needs_rebuild(db):
        await _rebuild_jobs(db)


async def _rebuild_jobs(db: aiosqlite.Connection) -> None:
    """jobs 재생성: status CHECK에 'awaiting' 추가 + legacy 컬럼 nullable 완화.

    자식 테이블(job_events/script_logs/job_hosts)은 id를 그대로 옮기므로 참조가 유지된다.
    외래키를 끈 채 DROP→RENAME 하고, 끝나고 무결성을 검사한다.
    """
    log.warning("jobs 테이블 재생성 시작 (status CHECK + legacy nullable)")
    carried = ", ".join(_JOBS_CARRY_COLUMNS)
    await db.execute("PRAGMA foreign_keys=OFF")
    await db.executescript(
        f"""
        CREATE TABLE jobs_v2 (
          id                INTEGER PRIMARY KEY AUTOINCREMENT,
          kind              TEXT NOT NULL DEFAULT 'install'
                              CHECK(kind IN ('install','configure','patch','rollback','verify','clean','ssh_key')),
          status            TEXT NOT NULL
                              CHECK(status IN ('queued','running','awaiting','succeeded','failed','cancelled')),
          env               TEXT,
          ref               TEXT,
          ref_type          TEXT,
          clean_mode        TEXT,
          only_tags         TEXT,
          sync_branch       TEXT,
          exit_code         INTEGER,
          cancel_by         TEXT,
          current_step      TEXT,
          started_by        TEXT NOT NULL,
          slack_channel     TEXT,
          slack_thread_ts   TEXT,
          slack_permalink   TEXT,
          admin_web_url     TEXT,
          script_commit_sha TEXT,
          error_message     TEXT,
          target_ip         TEXT,
          target_port       INTEGER DEFAULT 22,
          deployment_type   TEXT,
          hospital_code     TEXT,
          hospital_name     TEXT,
          hospital_address  TEXT,
          created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          started_at        TIMESTAMP,
          finished_at       TIMESTAMP
        );
        INSERT INTO jobs_v2 ({carried}) SELECT {carried} FROM jobs;
        DROP TABLE jobs;
        ALTER TABLE jobs_v2 RENAME TO jobs;
        CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
        """
    )
    cur = await db.execute("PRAGMA foreign_key_check")
    broken = await cur.fetchall()
    await db.execute("PRAGMA foreign_keys=ON")
    if broken:
        raise RuntimeError(f"jobs 재생성 후 외래키 무결성 위반: {broken!r}")
    log.warning("jobs 테이블 재생성 완료")


@asynccontextmanager
async def connect(path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    path = Path(path).expanduser()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        # 쓰기가 겹치면 즉시 실패하지 말고 잠깐 기다린다 (기본값은 0 = 즉시 오류).
        await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        yield db
