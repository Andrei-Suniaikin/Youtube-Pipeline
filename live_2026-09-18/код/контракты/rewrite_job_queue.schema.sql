CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    priority INTEGER DEFAULT 0
                , user_id TEXT DEFAULT '', log TEXT DEFAULT '', provider TEXT DEFAULT '', run_token TEXT DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0, resume_count INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0);
CREATE INDEX idx_jobs_user_id ON jobs(user_id);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE TABLE qa_findings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    created_at REAL NOT NULL,
    source     TEXT NOT NULL,   -- judge | cleaner | detcheck
    ftype      TEXT NOT NULL,   -- score | gate_fail | gate_unknown | planned_delete |
                                -- escalate | edit_applied | flag | warn | fabrication_risk |
                                -- truncated | length_dev
    severity   TEXT NOT NULL,   -- info | warn | block
    quote      TEXT,            -- улика (обрезана до 300 симв); находка без улики не пишется,
                                -- кроме агрегатных типов (score/edit_applied/length_dev)
    pos_pct    REAL,
    data       TEXT             -- JSON-довесок
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE INDEX idx_qaf_job   ON qa_findings(job_id);
CREATE INDEX idx_qaf_ftype ON qa_findings(ftype);
CREATE TABLE fair_queue_users (
                        provider TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        first_seen REAL NOT NULL,
                        PRIMARY KEY(provider, user_id)
                    );
CREATE TABLE fair_queue_state (
                        provider TEXT PRIMARY KEY,
                        last_user_id TEXT NOT NULL DEFAULT ''
                    );
CREATE INDEX idx_jobs_provider_fair ON jobs(status, provider, user_id, created_at, id);
CREATE INDEX idx_jobs_run_token ON jobs(id, status, run_token);
CREATE TABLE create_intents (user_id TEXT NOT NULL, scope TEXT NOT NULL, key_hash TEXT NOT NULL, payload_hash TEXT NOT NULL, job_id TEXT NOT NULL, PRIMARY KEY(user_id,scope,key_hash));
