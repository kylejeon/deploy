"""v1 스키마 DB → v2 마이그레이션 검증.

운영 맥미니의 DB가 v1이라 이 경로가 실제로 돌아간다. 데이터 보존이 핵심.
"""
from __future__ import annotations

import aiosqlite
import pytest

from autodeploy.db import connect, init_db

V1_SCHEMA = """
CREATE TABLE jobs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  target_ip         TEXT NOT NULL,
  target_port       INTEGER NOT NULL DEFAULT 22,
  deployment_type   TEXT NOT NULL,
  hospital_code     TEXT NOT NULL,
  hospital_name     TEXT,
  hospital_address  TEXT,
  status            TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
  current_step      TEXT,
  started_by        TEXT NOT NULL,
  slack_channel     TEXT NOT NULL,
  slack_thread_ts   TEXT,
  admin_web_url     TEXT,
  script_commit_sha TEXT,
  error_message     TEXT,
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at        TIMESTAMP,
  finished_at       TIMESTAMP
);
CREATE TABLE job_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step        TEXT NOT NULL,
  level       TEXT NOT NULL CHECK(level IN ('info','warn','error')),
  message     TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE script_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step        TEXT NOT NULL,
  stream      TEXT NOT NULL CHECK(stream IN ('stdout','stderr')),
  line        TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


async def _make_v1_db(path):
    async with aiosqlite.connect(path) as db:
        await db.executescript(V1_SCHEMA)
        cur = await db.execute(
            """INSERT INTO jobs
               (target_ip, deployment_type, hospital_code, status, started_by,
                slack_channel, slack_thread_ts, script_commit_sha)
               VALUES ('10.0.0.5','hybrid-with-ai','hch-bp','succeeded',
                       'slack:@yonghyuk','C0CHAN','1712.0001','a91c4f2')"""
        )
        job_id = cur.lastrowid
        await db.execute(
            "INSERT INTO job_events (job_id, step, level, message) VALUES (?,?,?,?)",
            (job_id, "app_install", "info", "started"),
        )
        await db.execute(
            "INSERT INTO script_logs (job_id, step, stream, line) VALUES (?,?,?,?)",
            (job_id, "app_install", "stdout", "[INFO] Frontend URL: http://172.17.0.1:8000"),
        )
        await db.commit()
        return job_id


@pytest.mark.asyncio
async def test_v1_data_survives_migration(tmp_path):
    path = tmp_path / "state.db"
    job_id = await _make_v1_db(path)

    await init_db(path)

    async with connect(path) as db:
        async with db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)) as cur:
            job = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) AS n FROM job_events WHERE job_id=?", (job_id,)) as cur:
            n_events = (await cur.fetchone())["n"]
        async with db.execute("SELECT COUNT(*) AS n FROM script_logs WHERE job_id=?", (job_id,)) as cur:
            n_logs = (await cur.fetchone())["n"]

    assert job["target_ip"] == "10.0.0.5"
    assert job["hospital_code"] == "hch-bp"
    assert job["status"] == "succeeded"
    assert job["slack_thread_ts"] == "1712.0001"
    assert job["script_commit_sha"] == "a91c4f2"
    assert job["kind"] == "install"        # 기본값이 채워짐
    assert n_events == 1
    assert n_logs == 1


@pytest.mark.asyncio
async def test_migration_backs_up_db_file(tmp_path):
    path = tmp_path / "state.db"
    await _make_v1_db(path)
    await init_db(path)
    backups = list(tmp_path.glob("state.db.bak-*"))
    assert len(backups) == 1, "재생성 전 백업 파일이 있어야 한다"
    assert backups[0].stat().st_size > 0


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_makes_no_second_backup(tmp_path):
    path = tmp_path / "state.db"
    await _make_v1_db(path)
    await init_db(path)
    await init_db(path)
    await init_db(path)
    assert len(list(tmp_path.glob("state.db.bak-*"))) == 1, "이미 v2면 백업/재생성 안 함"


@pytest.mark.asyncio
async def test_awaiting_status_accepted_after_migration(tmp_path):
    path = tmp_path / "state.db"
    await _make_v1_db(path)
    await init_db(path)
    async with connect(path) as db:
        await db.execute(
            "INSERT INTO jobs (kind, status, started_by) VALUES ('patch','awaiting','web:yonghyuk')"
        )
        await db.commit()
        async with db.execute("SELECT status FROM jobs WHERE kind='patch'") as cur:
            assert (await cur.fetchone())["status"] == "awaiting"


@pytest.mark.asyncio
async def test_legacy_columns_nullable_after_migration(tmp_path):
    """hubctl 작업은 target_ip/deployment_type/hospital_code 가 없다."""
    path = tmp_path / "state.db"
    await _make_v1_db(path)
    await init_db(path)
    async with connect(path) as db:
        await db.execute(
            """INSERT INTO jobs (kind, status, env, ref, started_by)
               VALUES ('install','queued','dev','main','web:yonghyuk')"""
        )
        await db.commit()
        async with db.execute("SELECT target_ip, deployment_type FROM jobs WHERE env='dev'") as cur:
            row = await cur.fetchone()
    assert row["target_ip"] is None
    assert row["deployment_type"] is None


@pytest.mark.asyncio
async def test_bogus_status_still_rejected_after_migration(tmp_path):
    path = tmp_path / "state.db"
    await _make_v1_db(path)
    await init_db(path)
    async with connect(path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO jobs (kind, status, started_by) VALUES ('install','bogus','web:x')"
            )
            await db.commit()


@pytest.mark.asyncio
async def test_script_logs_host_column_added(tmp_path):
    path = tmp_path / "state.db"
    job_id = await _make_v1_db(path)
    await init_db(path)
    async with connect(path) as db:
        await db.execute(
            "INSERT INTO script_logs (job_id, step, stream, line, host) VALUES (?,?,?,?,?)",
            (job_id, "configure", "stdout", "ok: [yonseiwa]", "yonseiwa"),
        )
        await db.commit()
        async with db.execute("SELECT host FROM script_logs WHERE host IS NOT NULL") as cur:
            assert (await cur.fetchone())["host"] == "yonseiwa"


@pytest.mark.asyncio
async def test_job_hosts_profile_column_added(tmp_path):
    """실행 시점 프로파일 칸. 지난 작업 행은 NULL 로 남는다.

    그때 무엇이었는지는 알 수 없으므로 지어내지 않는다 — 화면이 "기록 없음"
    으로 구분해서 보여준다.
    """
    path = tmp_path / "state.db"
    job_id = await _make_v1_db(path)
    await init_db(path)
    async with connect(path) as db:
        await db.execute(
            "INSERT INTO job_hosts (job_id, host, status) VALUES (?,?, 'queued')",
            (job_id, "old"),
        )
        await db.execute(
            "INSERT INTO job_hosts (job_id, host, status, profile) VALUES (?,?, 'queued', ?)",
            (job_id, "new", "hybrid-with-ai"),
        )
        await db.commit()
        async with db.execute(
            "SELECT host, profile FROM job_hosts ORDER BY host"
        ) as cur:
            rows = {r["host"]: r["profile"] for r in await cur.fetchall()}
    assert rows == {"new": "hybrid-with-ai", "old": None}


@pytest.mark.asyncio
async def test_cascade_still_works_after_rebuild(tmp_path):
    """재생성 후에도 job 삭제 시 자식 행이 함께 지워져야 한다."""
    path = tmp_path / "state.db"
    job_id = await _make_v1_db(path)
    await init_db(path)
    async with connect(path) as db:
        await db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        await db.commit()
        async with db.execute("SELECT COUNT(*) AS n FROM job_events") as cur:
            assert (await cur.fetchone())["n"] == 0
        async with db.execute("SELECT COUNT(*) AS n FROM script_logs") as cur:
            assert (await cur.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_new_tables_exist(temp_db):
    async with connect(temp_db) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            names = {r["name"] for r in await cur.fetchall()}
    assert {"users", "sessions", "job_hosts", "server_meta"} <= names


@pytest.mark.asyncio
async def test_server_meta_serial_column_added(tmp_path):
    """이미 등록된 서버는 NULL 로 남는다 — 서버 화면의 '조회' 로 채운다."""
    path = tmp_path / "state.db"
    await _make_v1_db(path)
    await init_db(path)
    async with connect(path) as db:
        await db.execute("INSERT INTO server_meta (host) VALUES ('old')")
        await db.execute("INSERT INTO server_meta (host, serial) VALUES ('new', 'PF3ABCDE')")
        await db.commit()
        async with db.execute("SELECT host, serial FROM server_meta ORDER BY host") as cur:
            rows = {r["host"]: r["serial"] for r in await cur.fetchall()}
    assert rows == {"new": "PF3ABCDE", "old": None}
