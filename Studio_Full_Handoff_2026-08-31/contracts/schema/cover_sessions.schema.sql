CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    license_key TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_ping REAL NOT NULL
                , user_id TEXT NOT NULL DEFAULT '');
CREATE INDEX idx_sessions_license
                ON sessions(license_key)
            ;
CREATE TABLE users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    user_id       TEXT NOT NULL,
                    user_name     TEXT NOT NULL,
                    created_at    REAL NOT NULL
                , pw_reset_pending INTEGER NOT NULL DEFAULT 0);
