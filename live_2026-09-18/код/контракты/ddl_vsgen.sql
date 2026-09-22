CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, token TEXT UNIQUE, role TEXT, created TEXT, daily_quota INTEGER, keys TEXT, email TEXT, password_hash TEXT, tenant_id TEXT, key_opts TEXT, disabled INTEGER DEFAULT 0);
CREATE TABLE jobs(id INTEGER PRIMARY KEY, user_id INTEGER, provider TEXT, path TEXT, method TEXT, status INTEGER, bytes INTEGER, ts TEXT);
CREATE TABLE gen_jobs(id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, prompt TEXT, model TEXT, status TEXT, result_path TEXT, error TEXT, ts TEXT, title TEXT, source_text TEXT, settings TEXT, result_files TEXT);
CREATE TABLE tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT, is_active INTEGER DEFAULT 1, daily_quota INTEGER);
CREATE TABLE sessions(id TEXT PRIMARY KEY, user_id INTEGER, created REAL, expires REAL);
CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, dir_path TEXT, status TEXT, created REAL, updated REAL, settings TEXT);
CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT, project_id TEXT, kind TEXT, status TEXT, created REAL, started REAL, finished REAL, params TEXT, uid INTEGER, progress REAL, output_rel TEXT, error TEXT, claimed REAL, stage TEXT, claim_token TEXT, provider TEXT, lane TEXT, prompt_failover TEXT, transient_attempts INTEGER DEFAULT 0, retry_after REAL, capacity_units INTEGER, memory_mb INTEGER, credential_retries INTEGER DEFAULT 0, remote_retries INTEGER DEFAULT 0, render_sha256 TEXT, render_size INTEGER, render_error_sha256 TEXT, transcript_sha256 TEXT, transcript_rel TEXT, parent_job_id INTEGER, cancel_checks INTEGER DEFAULT 0, cancel_next_check REAL);
CREATE TABLE job_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, ts REAL, line TEXT);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE job_files(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, rel_path TEXT, bytes INTEGER);
CREATE TABLE presets(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, name TEXT, settings TEXT, created REAL);
CREATE TABLE voice_lib(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, name TEXT, voice_id TEXT, model_id TEXT, created REAL);
CREATE INDEX idx_job_logs_jid ON job_logs(job_id, id);
CREATE TABLE provider_rr_cursor(provider TEXT PRIMARY KEY, last_uid INTEGER NOT NULL, updated REAL NOT NULL);
CREATE INDEX idx_queue_sched ON queue_tasks(status, provider, kind, uid, created, job_id);
CREATE INDEX idx_queue_sched_provider_v2 ON queue_tasks(status, provider, kind, uid, created, job_id);
CREATE TABLE admin_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, admin_id INTEGER, admin_name TEXT, action TEXT, target TEXT, detail TEXT, result TEXT);
CREATE INDEX idx_admin_audit_ts ON admin_audit(id DESC);
CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
CREATE TABLE presets_bak_20260805(
  id INT,
  tenant_id TEXT,
  name TEXT,
  settings TEXT,
  created REAL
);
CREATE TABLE presets_bak_20260806(
  id INT,
  tenant_id TEXT,
  name TEXT,
  settings TEXT,
  created REAL
);
CREATE INDEX idx_queue_sched_retry_v1 ON queue_tasks(status, retry_after, provider, kind, uid, created, job_id);
CREATE TABLE card_jobs(job_id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, uid INTEGER NOT NULL, narration_sha256 TEXT NOT NULL, contract_version TEXT NOT NULL, request_sha256 TEXT NOT NULL, context_json TEXT NOT NULL, provider TEXT NOT NULL, prompt_failover TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, transient_requeues INTEGER NOT NULL DEFAULT 0, next_attempt REAL, output_rel TEXT, output_sha256 TEXT, output_json TEXT, raw_rel TEXT, raw_sha256 TEXT, raw_json TEXT, provenance TEXT, usage TEXT, error_code TEXT, last_error TEXT, created REAL NOT NULL, started REAL, finished REAL, UNIQUE(tenant_id,project_id,narration_sha256,contract_version));
CREATE INDEX idx_card_jobs_project_current ON card_jobs(tenant_id,project_id,contract_version,narration_sha256);
CREATE TABLE pub_hosts(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, code TEXT NOT NULL, title TEXT, adspower_url TEXT, status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, UNIQUE(tenant_id,code));
CREATE TABLE pub_profiles(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, serial TEXT NOT NULL, title TEXT, default_host_id INTEGER, status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, UNIQUE(tenant_id,serial));
CREATE TABLE pub_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, uc TEXT NOT NULL UNIQUE, name TEXT NOT NULL, handle TEXT, lang TEXT, niche TEXT, profile_id INTEGER, host_id INTEGER, tz TEXT NOT NULL DEFAULT 'Europe/Warsaw', schedule TEXT NOT NULL DEFAULT '{}', horizon_days INTEGER NOT NULL DEFAULT 3, preset_rewrite TEXT, preset_vsgen_id INTEGER, preset_cover TEXT, collaborator TEXT, video_mode TEXT NOT NULL DEFAULT 'slideshow', t2v_first_n INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, updated REAL NOT NULL, lead_days INTEGER NOT NULL DEFAULT 2, deferred INTEGER NOT NULL DEFAULT 1, deferred_limit INTEGER NOT NULL DEFAULT 5);
CREATE INDEX idx_pub_channels_list ON pub_channels(tenant_id,status,name);
CREATE INDEX idx_pub_channels_profile ON pub_channels(profile_id);
CREATE INDEX idx_pub_channels_lang ON pub_channels(lang,niche);
CREATE TABLE pub_tags(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT NOT NULL, created REAL NOT NULL, UNIQUE(tenant_id,name));
CREATE TABLE pub_channel_tags(channel_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY(channel_id,tag_id));
CREATE INDEX idx_pub_channel_tags_tag ON pub_channel_tags(tag_id,channel_id);
CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1, donor_url TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, slot_at REAL, slot_mode TEXT NOT NULL DEFAULT 'auto', overrides TEXT NOT NULL DEFAULT '{}', note TEXT, status TEXT NOT NULL DEFAULT 'planned', frozen INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, donor_title TEXT);
CREATE INDEX idx_pub_plan_queue ON pub_plan_items(channel_id,status,position,id);
CREATE INDEX idx_pub_plan_window ON pub_plan_items(tenant_id,slot_at,status);
CREATE TABLE pub_releases(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, plan_item_id INTEGER NOT NULL, plan_item_version INTEGER NOT NULL, channel_id INTEGER NOT NULL, uc TEXT NOT NULL, slot_at REAL NOT NULL, project_id TEXT, status TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL DEFAULT 'created', video_id TEXT, blocked_reason TEXT, evidence TEXT, heartbeat REAL, deadline REAL, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(plan_item_id,plan_item_version));
CREATE INDEX idx_pub_releases_live ON pub_releases(tenant_id,status,slot_at);
CREATE INDEX idx_pub_releases_channel ON pub_releases(channel_id,slot_at);
CREATE TABLE pub_release_events(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, seq INTEGER NOT NULL, phase TEXT NOT NULL, video_id TEXT, payload TEXT, created REAL NOT NULL, UNIQUE(release_id,seq));
CREATE INDEX idx_pub_release_events_rid ON pub_release_events(release_id,seq);
CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, service TEXT NOT NULL, kind TEXT NOT NULL, remote_job_id TEXT, status TEXT NOT NULL DEFAULT 'queued', payload TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(release_id,service,kind));
CREATE INDEX idx_pub_release_jobs_rid ON pub_release_jobs(release_id);
CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, ts REAL NOT NULL, level TEXT NOT NULL DEFAULT 'info', step TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX idx_pub_release_log_rid ON pub_release_log(release_id, id);
CREATE TABLE pub_upload_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL, uc TEXT NOT NULL, profile_id INTEGER, host_id INTEGER, channel_url TEXT NOT NULL, title TEXT NOT NULL, description TEXT, schedule_iso TEXT, visibility TEXT NOT NULL DEFAULT 'public', collaborator TEXT, video_path TEXT NOT NULL, video_sha256 TEXT, cover_path TEXT, slot_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued', phase TEXT, claim_token TEXT, host TEXT, worker TEXT, claimed REAL, lease_until REAL, heartbeat REAL, verdict TEXT, video_id TEXT, evidence TEXT, error TEXT, finished REAL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX idx_pub_upload_queue ON pub_upload_jobs(status, slot_at, id);
CREATE INDEX idx_pub_upload_profile ON pub_upload_jobs(status, profile_id);
CREATE UNIQUE INDEX idx_pub_plan_slot_busy_v2 ON pub_plan_items(channel_id,slot_at) WHERE slot_at IS NOT NULL AND status IN ('planned','frozen','running','done','error');
CREATE TABLE subs_cache(video_id TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE complaints(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, uid INTEGER, project_id TEXT, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', created REAL, started REAL, finished REAL, user_message TEXT, report TEXT);
CREATE INDEX idx_complaints_project ON complaints(tenant_id, project_id, id);
CREATE TABLE render_followups(job_id INTEGER PRIMARY KEY,status TEXT NOT NULL,next_attempt REAL NOT NULL);
CREATE UNIQUE INDEX idx_render_child ON queue_tasks(parent_job_id,kind) WHERE parent_job_id IS NOT NULL;
