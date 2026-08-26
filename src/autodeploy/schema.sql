-- AutoDeploy SQLite schema v2
-- v1: dev-spec §5 (Slack 봇 / SSH 스크립트 워크플로)
-- v2: dev-spec-web-console-20260826 §6 (웹 콘솔 / hubctl)
-- 모두 CREATE IF NOT EXISTS — 부트스트랩 시 멱등. 기존 DB의 스키마 변경은 db.py::_migrate 담당.

CREATE TABLE IF NOT EXISTS jobs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  -- v2: 작업 종류. ssh_key 는 F9(키 등록) 전용.
  kind              TEXT NOT NULL DEFAULT 'install'
                      CHECK(kind IN ('install','configure','patch','rollback','verify','clean','ssh_key')),
  status            TEXT NOT NULL
                      CHECK(status IN ('queued','running','awaiting','succeeded','failed','cancelled')),
  -- v2: hubctl 인자
  env               TEXT,          -- dev|stage|prod (patch/rollback/verify/clean 은 NULL)
  ref               TEXT,          -- hub_deploy_ref
  ref_type          TEXT,          -- branch|tag|commit
  clean_mode        TEXT,          -- reset|reset-keep|uninstall
  exit_code         INTEGER,
  cancel_by         TEXT,
  current_step      TEXT,
  started_by        TEXT NOT NULL, -- 'web:<username>' | 'slack:@<user>'
  slack_channel     TEXT,
  slack_thread_ts   TEXT,
  admin_web_url     TEXT,
  script_commit_sha TEXT,
  error_message     TEXT,
  -- v1 legacy: 구 SSH 워크플로 이력 보존용. 신규(hubctl) 작업은 NULL.
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

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS job_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step        TEXT NOT NULL,
  level       TEXT NOT NULL CHECK(level IN ('info','warn','error')),
  message     TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, created_at);

CREATE TABLE IF NOT EXISTS script_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step        TEXT NOT NULL,
  stream      TEXT NOT NULL CHECK(stream IN ('stdout','stderr')),
  line        TEXT NOT NULL,
  host        TEXT,        -- v2: 줄에서 추출한 ansible 호스트 (공통 줄은 NULL)
  -- v2: 줄 분류 (ansible_log.LineKind). stream(stdout/stderr)과는 별개 —
  -- ansible 은 [ERROR] 까지 전부 stdout 으로 쓰므로 stream 만으로는 에러를 못 가린다.
  kind        TEXT,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_script_logs_job ON script_logs(job_id, step, id);

-- ── v2: 웹 콘솔 ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  pw_hash       BLOB NOT NULL,
  pw_salt       BLOB NOT NULL,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at TIMESTAMP,
  disabled_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  TIMESTAMP NOT NULL,
  client_ip   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS job_hosts (
  job_id            INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  host              TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
  recap_ok          INTEGER,
  recap_changed     INTEGER,
  recap_failed      INTEGER,
  recap_unreachable INTEGER,
  PRIMARY KEY (job_id, host)
);

CREATE TABLE IF NOT EXISTS server_meta (
  host             TEXT PRIMARY KEY,
  memo             TEXT,        -- 병원명 등. sites.yml 스키마에 없는 웹 전용 값
  key_installed_at TIMESTAMP,   -- F9: 공개키 설치 + 키 인증 검증 성공 시각
  updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
