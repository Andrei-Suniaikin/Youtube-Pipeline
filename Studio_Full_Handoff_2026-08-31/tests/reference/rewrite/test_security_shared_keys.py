import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import textwrap
import urllib.error
from pathlib import Path

import pytest
from fastapi import HTTPException

import api_auth
import rewrite_core
from auth import LicenseManager
from migrations.shared_keys_fair_queue_001 import migrate_shared_keys_fair_queue


ROOT = Path(__file__).resolve().parents[1]


def _create_api_keys_table(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE api_keys (
                key_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                license_key TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_used_at REAL,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )


def test_disable_revokes_session_and_api_key(tmp_path):
    sessions_db = tmp_path / "sessions.db"
    manager = LicenseManager(str(sessions_db))
    assert manager.setup_admin("admin", "secret1", "127.0.0.1")[0]
    assert manager.create_account("partner", "secret2", "Partner")[0]
    ok, session_id, _ = manager.login_password("partner", "secret2", "127.0.0.1")
    assert ok
    user_id = manager.get_user_id(session_id)

    _create_api_keys_table(sessions_db)
    created = api_auth.create_api_key(
        str(sessions_db), user_id, "", "integration"
    )
    api_auth.configure(str(sessions_db), license_mgr=manager)

    assert manager.delete_account("partner")
    assert manager.validate(session_id) is None
    assert manager.login_password("partner", "secret2", "127.0.0.1")[0] is False
    with pytest.raises(HTTPException) as exc:
        api_auth.get_api_user(
            authorization=f"Bearer {created['plain_key']}",
            x_api_key=None,
            x_session_id=None,
        )
    assert exc.value.status_code == 401
    with sqlite3.connect(sessions_db) as conn:
        assert conn.execute(
            "SELECT status FROM users WHERE username='partner'"
        ).fetchone()[0] == "disabled"
        assert conn.execute(
            "SELECT status FROM api_keys WHERE key_id=?",
            (created["key_id"],),
        ).fetchone()[0] == "revoked"


def test_progress_is_isolated_by_user_and_job(tmp_path):
    manager = rewrite_core.ProgressManager(str(tmp_path / "progress.json"))
    a1 = manager.get_or_create_task(
        "script.txt", "English", "English", 1, user_id="A", job_id="job-1"
    )
    a2 = manager.get_or_create_task(
        "script.txt", "English", "English", 2, user_id="A", job_id="job-2"
    )
    b1 = manager.get_or_create_task(
        "script.txt", "English", "English", 3, user_id="B", job_id="job-1"
    )

    assert a1 is not a2
    assert a1 is not b1
    assert len(manager.tasks) == 3
    assert manager.delete_task(
        "script.txt", "English", user_id="A", job_id="job-1"
    )
    assert manager._task_key(
        "script.txt", "English", user_id="A", job_id="job-2"
    ) in manager.tasks
    assert manager._task_key(
        "script.txt", "English", user_id="B", job_id="job-1"
    ) in manager.tasks


def test_legacy_progress_is_not_reused(tmp_path):
    manager = rewrite_core.ProgressManager(str(tmp_path / "progress.json"))
    legacy = rewrite_core.TaskProgress(
        file_path="script.txt",
        language="English",
        lang_display="English",
        status="completed",
        chunks=[],
    )
    manager.tasks["A::script.txt::English"] = legacy

    current = manager.get_or_create_task(
        "script.txt", "English", "English", 1, user_id="A", job_id="new-job"
    )

    assert current is not legacy
    assert current.status == "pending"


def test_migration_scrubs_jobs_but_keeps_tenant_byok_keys(tmp_path):
    """BYOK-2026-07-30: свои ключи юзеров миграция НЕ удаляет (они рабочие),
    но задачи по-прежнему не носят секретов."""
    sessions_db = tmp_path / "sessions.db"
    jobs_db = tmp_path / "jobs.db"
    marker = "legacy-provider-secret"
    with sqlite3.connect(sessions_db) as conn:
        conn.execute(
            "CREATE TABLE users (username TEXT, user_id TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT, user_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE user_keys (user_id TEXT, openai_api_key TEXT)"
        )
        conn.execute(
            "INSERT INTO user_keys VALUES ('u1', ?)", (marker,)
        )
    with sqlite3.connect(jobs_db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT, data TEXT)")
        conn.execute(
            "INSERT INTO jobs VALUES ('j1', ?)",
            (json.dumps({"api_key": marker, "oauth_token": marker}),),
        )

    migrate_shared_keys_fair_queue(
        str(sessions_db), str(jobs_db), enabled=True
    )

    with sqlite3.connect(sessions_db) as conn:
        assert conn.execute(
            "SELECT openai_api_key FROM user_keys"
        ).fetchone()[0] == marker
    with sqlite3.connect(jobs_db) as conn:
        assert marker not in conn.execute("SELECT data FROM jobs").fetchone()[0]


def test_openai_call_uses_server_key_and_verified_context(monkeypatch):
    server_key = "server-key-marker"
    client_marker = "client-key-marker"
    monkeypatch.setenv("OPENAI_API_KEY", server_key)
    monkeypatch.setattr(
        rewrite_core,
        "get_keys_config",
        lambda *_args, **_kwargs: {
            "gpt_service_tier": "default",
            "gpt_use_cache": False,
            "debug_mode": False,
        },
    )
    monkeypatch.setattr(rewrite_core.RATE_LIMITER, "get_delay", lambda: 0)
    monkeypatch.setattr(rewrite_core.RATE_LIMITER, "on_success", lambda: None)

    tls_context = object()
    import ssl

    monkeypatch.setattr(ssl, "create_default_context", lambda: tls_context)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            ).encode()

    def fake_urlopen(req, timeout, context):
        captured["authorization"] = dict(req.header_items())["Authorization"]
        captured["context"] = context
        return Response()

    monkeypatch.setattr(rewrite_core.urllib.request, "urlopen", fake_urlopen)
    ok, result = rewrite_core._call_openai_api_impl(
        [{"role": "user", "content": "hello"}],
        "gpt-4.1-nano",
        client_marker,
        queue.Queue(),
    )

    assert ok and result == "ok"
    assert captured["authorization"] == f"Bearer {server_key}"
    assert client_marker not in captured["authorization"]
    assert captured["context"] is tls_context


def test_openai_429_retry_is_bounded(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "server-key")
    monkeypatch.setenv("REWRITE_OPENAI_MAX_429_RETRIES", "2")
    monkeypatch.setattr(
        rewrite_core,
        "get_keys_config",
        lambda *_args, **_kwargs: {
            "gpt_service_tier": "default",
            "gpt_use_cache": False,
            "debug_mode": False,
        },
    )
    monkeypatch.setattr(rewrite_core.RATE_LIMITER, "get_delay", lambda: 0)
    monkeypatch.setattr(rewrite_core.RATE_LIMITER, "on_rate_limit", lambda: 0)
    monkeypatch.setattr(rewrite_core.time, "sleep", lambda _seconds: None)
    calls = {"count": 0}

    def rate_limited(*_args, **_kwargs):
        calls["count"] += 1
        raise urllib.error.HTTPError(
            "https://api.openai.com",
            429,
            "rate",
            {"Retry-After": "0"},
            io.BytesIO(b'{"error":{"message":"rate"}}'),
        )

    monkeypatch.setattr(rewrite_core.urllib.request, "urlopen", rate_limited)
    ok, error = rewrite_core._call_openai_api_impl(
        [{"role": "user", "content": "hello"}],
        "gpt-4.1-nano",
        "ignored",
        queue.Queue(),
    )

    assert not ok
    assert "Rate limit" in error
    assert calls["count"] == 3


def test_allowed_partner_job_uses_no_client_provider_key(tmp_path):
    marker = "client-provider-secret-marker"
    script = textwrap.dedent(
        f"""
        import json
        import sqlite3
        from types import SimpleNamespace
        import server
        from fastapi import HTTPException

        server.license_mgr.is_user_active = lambda _user_id: True

        request = SimpleNamespace(state=SimpleNamespace(user_id="partner"))
        req = server.RewriteJobRequest(
            files=["script.txt"],
            languages=[{{"code": "English", "display": "English"}}],
            model="gpt-4.1-nano",
            max_threads=1,
            openai_api_key={marker!r},
        )
        response = server.create_rewrite_job(req, request)
        job = server.queue.get_job(response["job_id"])
        assert job.api_key == ""
        assert job.oauth_token == ""
        assert job.oauth_tokens == "[]"
        with sqlite3.connect(server.DB_PATH) as conn:
            raw = conn.execute("SELECT data FROM jobs WHERE id=?", (job.id,)).fetchone()[0]
        assert {marker!r} not in raw
        try:
            server._validate_model_admission("unapproved-expensive-model", "partner")
        except HTTPException as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError("unapproved model accepted")
        """
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "HOME": str(tmp_path),
            "REWRITE_SERVER_DIR": str(tmp_path),
            "REWRITE_DB_PATH": str(tmp_path / "jobs.db"),
            "SESSION_DB": str(tmp_path / "sessions.db"),
            "OPENAI_API_KEY": "server-provider-key",
            "REWRITE_ALLOWED_OPENAI_MODELS": "gpt-4.1-nano",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_frontend_offers_own_key_input_without_revealing_values():
    """BYOK-2026-07-30 (требование C): вписать свой ключ может ЛЮБОЙ юзер, но
    значение ключа в браузер не возвращается — только признак «задан»."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    client = (ROOT / "server_client.py").read_text(encoding="utf-8")

    # Поля ввода своего ключа есть и они НЕ спрятаны за data-admin-only.
    assert 'id="set-own-openai-key"' in html
    assert 'id="set-own-claude-token"' in html
    for marker in ('id="set-own-openai-key"', 'id="set-own-claude-token"'):
        card_start = html.rfind('<div class="card', 0, html.index(marker))
        assert "data-admin-only" not in html[card_start:html.index(marker)]

    # Значение ключа сервер не отдаёт: UI читает только *_key_set / *_key_source.
    assert "own_openai_key_set" in html
    assert "own_claude_token_set" in html
    # Никакого echo секрета в input: единственное присваивание — очистка.
    assert "input.value = ''" in html
    assert "cfg.openai_api_key" not in html
    assert "cfg.oauth_tokens" not in html

    # Легаси-копии секретов в браузере по-прежнему вычищаются.
    assert "localStorage.setItem('openai_key'" not in html
    assert "localStorage.removeItem('openai_key')" in html
    assert '"openai_api_key"' not in client
