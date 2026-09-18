CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL
                , user_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'flow', cancel_requested INTEGER NOT NULL DEFAULT 0, run_token TEXT NOT NULL DEFAULT '');
CREATE TABLE api_keys (
                    key_id       TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    name         TEXT NOT NULL,
                    key_hash     TEXT NOT NULL UNIQUE,
                    prefix       TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    last_used_at REAL,
                    status       TEXT NOT NULL DEFAULT 'active'
                );
CREATE TABLE usage (
                    user_id    TEXT NOT NULL,
                    day        TEXT NOT NULL,
                    key_source TEXT NOT NULL,
                    covers     INTEGER NOT NULL DEFAULT 0,
                    credits    INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day, key_source)
                );
CREATE INDEX idx_api_keys_user ON api_keys(user_id);
CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX idx_usage_user_day ON usage(user_id, day);
CREATE INDEX idx_jobs_sched ON jobs(status, user_id, created_at, id);
CREATE INDEX idx_jobs_provider_sched ON jobs(status, cancel_requested, provider, user_id, created_at, id);
CREATE TABLE job_requests (user_id TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, job_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(user_id,request_key));
CREATE TABLE cover_remote (job_id TEXT,item TEXT,ordinal INTEGER,family TEXT,credential_hash TEXT,remote_id TEXT DEFAULT "",state TEXT,cache TEXT DEFAULT "",error TEXT DEFAULT "",updated_at REAL, request_hash TEXT NOT NULL DEFAULT "", request_seed TEXT NOT NULL DEFAULT "", request_json TEXT NOT NULL DEFAULT "", source_hash TEXT NOT NULL DEFAULT "",PRIMARY KEY(job_id,item,ordinal));
CREATE TABLE cover_stages(job_id TEXT,item TEXT,stage TEXT,source_hash TEXT,value_json TEXT,PRIMARY KEY(job_id,item,stage));
CREATE TABLE cover_artifacts (job_id TEXT,path TEXT,item TEXT,sha256 TEXT,fallback INTEGER,PRIMARY KEY(job_id,path));
