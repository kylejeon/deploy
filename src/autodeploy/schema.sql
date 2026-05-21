-- AutoDeploy SQLite schema v1
-- dev-spec §5 기준. CREATE IF NOT EXISTS로 부트스트랩 시 멱등.

CREATE TABLE IF NOT EXISTS jobs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  target_ip         TEXT NOT NULL,
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
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_script_logs_job ON script_logs(job_id, step, id);
