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
CREATE TABLE api_keys (
                key_id       TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                license_key  TEXT NOT NULL,
                key_hash     TEXT NOT NULL UNIQUE,
                prefix       TEXT NOT NULL,
                name         TEXT NOT NULL,
                created_at   REAL NOT NULL,
                last_used_at REAL,
                status       TEXT NOT NULL DEFAULT 'active'
            );
CREATE INDEX idx_api_keys_user ON api_keys(user_id);
CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX idx_api_keys_status ON api_keys(status);
CREATE TABLE user_keys (
                user_id                 TEXT PRIMARY KEY,
                anthropic_api_key       TEXT NOT NULL DEFAULT '',
                openai_api_key          TEXT NOT NULL DEFAULT '',
                oauth_tokens            TEXT NOT NULL DEFAULT '[]',
                gpt_service_tier        TEXT NOT NULL DEFAULT 'default',
                gpt_use_cache           INTEGER NOT NULL DEFAULT 1,
                postprocess_check_model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                custom_models           TEXT NOT NULL DEFAULT '[]',
                debug_mode              INTEGER NOT NULL DEFAULT 0,
                updated_at              REAL NOT NULL
            , rew_presets TEXT NOT NULL DEFAULT '{}', gen_settings_presets TEXT NOT NULL DEFAULT '{}', codex_auth TEXT NOT NULL DEFAULT '', credential_mode TEXT NOT NULL DEFAULT 'server_fallback');
CREATE TABLE users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    user_id       TEXT NOT NULL,
                    user_name     TEXT NOT NULL,
                    created_at    REAL NOT NULL
                , status TEXT NOT NULL DEFAULT 'active', disabled_at REAL);
CREATE TABLE user_keys_bak_20260805(
  user_id TEXT,
  anthropic_api_key TEXT,
  openai_api_key TEXT,
  oauth_tokens TEXT,
  gpt_service_tier TEXT,
  gpt_use_cache INT,
  postprocess_check_model TEXT,
  custom_models TEXT,
  debug_mode INT,
  updated_at REAL,
  rew_presets TEXT,
  gen_settings_presets TEXT,
  codex_auth TEXT
);
