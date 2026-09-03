"""BYOK-2026-07-30 (этап 2): свой credential КАЖДОГО вида приоритетнее серверного.

Требование владельца: «каждый юзер мог писать свой любой ключ, включая для
каждого провайдера Claude Token, Codex, вообще всё, что возможно».

Покрываем:
  A. все виды credential — OpenAI API key, Anthropic API key, Claude OAuth
     token(ы), своя подписка Codex; приоритет всегда свой → серверный;
     нет ни своего, ни серверного → внятная ошибка admission (не 500);
  B. серверная подписка владельца (его codex-home / его Anthropic-ключ) не
     достаётся другому тенанту, «своё» и «серверное» разделены явно;
  C. значения не возвращаются наружу (ни в JSON, ни в HTML), только «задан»;
  D. миграция не удаляет пользовательские ключи (в т.ч. codex_auth) ни при
     флаге OFF, ни при ON; мёртвый серверный ключ не роняет старт сервиса;
  E. claude-кап не обнуляется при пустом серверном пуле (задачи не висят).
"""

import json
import os
import queue as std_queue
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import rewrite_core
from migrations.shared_keys_fair_queue_001 import migrate_shared_keys_fair_queue
from migrations.user_keys_001 import migrate_user_keys

ROOT = Path(__file__).resolve().parents[1]
ADMIN = rewrite_core.ADMIN_USER_ID

SERVER_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEYS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKENS",
    "REWRITE_CLAUDE_OAUTH_TOKENS",
    "REWRITE_CODEX_HOME",
    "REWRITE_CODEX_HOMES",
    "REWRITE_SERVER_SUBSCRIPTION_USER_IDS",
)


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    """Чистое окружение: серверных кредов нет, ANTHROPIC_API_KEY не унаследован."""
    for name in SERVER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(rewrite_core, "ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setenv(
        "REWRITE_CODEX_USER_HOMES_DIR", str(tmp_path / "codex-user-homes")
    )
    with rewrite_core._SERVER_CREDENTIAL_POOLS_LOCK:
        rewrite_core._SERVER_CREDENTIAL_POOLS.clear()
    yield
    with rewrite_core._SERVER_CREDENTIAL_POOLS_LOCK:
        rewrite_core._SERVER_CREDENTIAL_POOLS.clear()
    rewrite_core.clear_current_user_id()


@pytest.fixture
def tenant_store(tmp_path, monkeypatch):
    """sessions.db со схемой user_keys из настоящей миграции (с codex_auth)."""
    sessions_db = tmp_path / "sessions.db"
    migrate_user_keys(str(sessions_db), str(tmp_path / "no-legacy-config.json"))
    monkeypatch.setattr(rewrite_core, "SESSIONS_DB_PATH", str(sessions_db))
    return sessions_db


# ---------------------------------------------------------------- A: Anthropic


def test_own_anthropic_key_beats_server_and_server_stays_with_owner(
    tenant_store, monkeypatch
):
    """A+B: свой Anthropic-ключ выигрывает; серверный — только владельцу."""
    own = "tenant-own-anthropic-key-marker"
    server_key = "server-owner-anthropic-key-marker"
    monkeypatch.setenv("ANTHROPIC_API_KEY", server_key)

    # Партнёр без своего ключа: серверный владельческий ему НЕ достаётся.
    assert rewrite_core.get_effective_anthropic_keys("partner") == []
    assert rewrite_core.credential_source("anthropic", "partner") == "none"
    # Владелец (админ-тенант) серверный ключ видит.
    assert rewrite_core.get_effective_anthropic_keys(ADMIN) == [server_key]
    assert rewrite_core.credential_source("anthropic", ADMIN) == "server"

    # Свой ключ → работает и приоритетнее любого серверного.
    rewrite_core.save_keys_config(anthropic_api_key=own, user_id="partner")
    assert rewrite_core.get_user_anthropic_keys("partner") == [own]
    assert rewrite_core.get_effective_anthropic_keys("partner") == [own]
    assert rewrite_core.credential_source("anthropic", "partner") == "user"

    rewrite_core.save_keys_config(anthropic_api_key=own, user_id=ADMIN)
    assert rewrite_core.get_effective_anthropic_keys(ADMIN) == [own]

    # Пустая строка = удалить свой → снова прежний источник.
    rewrite_core.save_keys_config(anthropic_api_key="", user_id="partner")
    assert rewrite_core.credential_source("anthropic", "partner") == "none"


def test_claude_api_path_uses_own_anthropic_key_not_server(
    tenant_store, monkeypatch
):
    """A: прямой Anthropic-путь Claude берёт СВОЙ ключ тенанта (не серверный)."""
    own = "tenant-own-anthropic-key-for-call"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "server-owner-anthropic-key")
    rewrite_core.save_keys_config(anthropic_api_key=own, user_id="partner")

    seen: dict = {}

    def fake_once(messages, api_key, model, log_queue, task_info, use_caching):
        seen["api_key"] = api_key
        return True, "ok"

    monkeypatch.setattr(rewrite_core, "_call_claude_api_once", fake_once)
    monkeypatch.setattr(rewrite_core.RATE_LIMITER, "on_success", lambda: None)
    rewrite_core.set_current_user_id("partner")
    try:
        ok, result = rewrite_core.call_api_universal(
            [{"role": "user", "content": "hi"}],
            api_key="",
            model="claude-sonnet-4-6",
            log_queue=None,
            provider="claude",
        )
    finally:
        rewrite_core.clear_current_user_id()

    assert ok and result == "ok"
    assert seen["api_key"] == own

    # Своего ключа нет и серверный владельческий недоступен → внятная ошибка на
    # границе провайдера, а не пустой запрос и загадочная 401 от вендора.
    seen.clear()
    rewrite_core.save_keys_config(anthropic_api_key="", user_id="partner")
    rewrite_core.set_current_user_id("partner")
    try:
        ok, error = rewrite_core.call_api_universal(
            [{"role": "user", "content": "hi"}],
            api_key="",
            model="claude-sonnet-4-6",
            log_queue=None,
            provider="claude",
        )
    finally:
        rewrite_core.clear_current_user_id()
    assert ok is False
    assert "not configured" in error
    assert "server-owner-anthropic-key" not in error
    assert seen == {}


# -------------------------------------------------------------------- A: Codex


def _codex_auth_payload(token: str) -> str:
    return json.dumps({"tokens": {"access_token": token}})


def test_own_codex_subscription_is_materialized_privately(
    tenant_store, monkeypatch
):
    """A: своя подписка Codex тенанта становится приватным CODEX_HOME."""
    payload = _codex_auth_payload("tenant-own-codex-access-token")
    rewrite_core.save_keys_config(codex_auth=payload, user_id="partner")

    assert rewrite_core.get_user_codex_auth("partner") == payload
    assert rewrite_core.credential_source("codex", "partner") == "user"

    homes = rewrite_core.get_user_codex_homes("partner")
    assert len(homes) == 1
    home = Path(homes[0])
    auth_file = home / "auth.json"
    assert json.loads(auth_file.read_text(encoding="utf-8")) == json.loads(payload)
    assert auth_file.stat().st_mode & 0o777 == 0o600
    assert home.stat().st_mode & 0o777 == 0o700
    # Каталог назван хешем user_id — пользовательские данные в путь не попадают.
    assert "partner" not in str(home)

    # Codex сам обновил токен в файле → повторная материализация его НЕ затирает.
    refreshed = _codex_auth_payload("rotated-by-codex-cli")
    auth_file.write_text(refreshed, encoding="utf-8")
    assert rewrite_core.get_user_codex_homes("partner") == [str(home)]
    assert auth_file.read_text(encoding="utf-8") == refreshed

    # Тенант вписал другое значение → файл перезаписывается.
    changed = _codex_auth_payload("tenant-pasted-new-auth")
    rewrite_core.save_keys_config(codex_auth=changed, user_id="partner")
    rewrite_core.get_user_codex_homes("partner")
    assert json.loads(auth_file.read_text(encoding="utf-8")) == json.loads(changed)


def test_own_codex_pool_is_leased_and_isolated_per_tenant(
    tenant_store, monkeypatch
):
    """A+B: каждый тенант арендует СВОЙ codex-home, чужой не виден."""
    rewrite_core.save_keys_config(
        codex_auth=_codex_auth_payload("codex-a"), user_id="tenant-a"
    )
    rewrite_core.save_keys_config(
        codex_auth=_codex_auth_payload("codex-b"), user_id="tenant-b"
    )
    home_a = rewrite_core.get_user_codex_homes("tenant-a")[0]
    home_b = rewrite_core.get_user_codex_homes("tenant-b")[0]
    assert home_a != home_b

    lease_a = rewrite_core.get_effective_credential_pool(
        "codex", "tenant-a"
    ).acquire(timeout=0)
    lease_b = rewrite_core.get_effective_credential_pool(
        "codex", "tenant-b"
    ).acquire(timeout=0)
    try:
        assert lease_a.value == home_a
        assert lease_b.value == home_b
    finally:
        lease_a.release()
        lease_b.release()


def test_server_codex_subscription_never_reaches_another_tenant(
    tenant_store, monkeypatch
):
    """B: серверная подписка владельца чужому тенанту недоступна (fail closed)."""
    server_home = "/srv/owner-codex-home"
    monkeypatch.setenv("REWRITE_CODEX_HOME", server_home)

    # Владелец: серверная подписка доступна.
    assert rewrite_core.server_credential_owner(ADMIN) is True
    assert rewrite_core.credential_source("codex", ADMIN) == "server"
    owner_lease = rewrite_core.get_effective_credential_pool(
        "codex", ADMIN
    ).acquire(timeout=0)
    try:
        assert owner_lease.value == server_home
    finally:
        owner_lease.release()

    # Партнёр без своей подписки: ни аренды, ни тихой работы на кредах владельца.
    assert rewrite_core.server_credential_owner("partner") is False
    assert rewrite_core.credential_source("codex", "partner") == "none"
    with pytest.raises(rewrite_core.CredentialPoolExhausted) as excinfo:
        rewrite_core.get_effective_credential_pool("codex", "partner")
    assert "your own Codex auth.json" in str(excinfo.value)
    assert server_home not in str(excinfo.value)

    # Тот же отказ на уровне вызова провайдера — без запуска CLI владельца.
    rewrite_core.set_current_user_id("partner")
    try:
        ok, error = rewrite_core.call_codex_subscription(
            [{"role": "user", "content": "hi"}], "codex:gpt-5.6-terra"
        )
    finally:
        rewrite_core.clear_current_user_id()
    assert ok is False
    assert "your own Codex auth.json" in error
    assert server_home not in error

    # Явный allowlist владельца открывает серверную подписку названному аккаунту.
    monkeypatch.setenv("REWRITE_SERVER_SUBSCRIPTION_USER_IDS", "partner")
    assert rewrite_core.credential_source("codex", "partner") == "server"


def test_own_codex_auth_wins_over_server_subscription_for_owner(
    tenant_store, monkeypatch
):
    """A: даже у владельца свой вписанный auth.json приоритетнее серверного."""
    monkeypatch.setenv("REWRITE_CODEX_HOME", "/srv/owner-codex-home")
    rewrite_core.save_keys_config(
        codex_auth=_codex_auth_payload("owner-own-pasted"), user_id=ADMIN
    )
    own_home = rewrite_core.get_user_codex_homes(ADMIN)[0]
    assert rewrite_core.credential_source("codex", ADMIN) == "user"
    lease = rewrite_core.get_effective_credential_pool("codex", ADMIN).acquire(
        timeout=0
    )
    try:
        assert lease.value == own_home
    finally:
        lease.release()


# ------------------------------------------------------- A: admission / errors


def _run_server_script(script: str, tmp_path, extra_env: dict) -> None:
    sessions_db = tmp_path / "sessions.db"
    migrate_user_keys(str(sessions_db), str(tmp_path / "no-legacy.json"))
    env = os.environ.copy()
    for name in SERVER_ENV_NAMES:
        env.pop(name, None)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "HOME": str(tmp_path),
            "REWRITE_SERVER_DIR": str(tmp_path),
            "REWRITE_STATIC_DIR": str(ROOT / "static"),
            "REWRITE_DB_PATH": str(tmp_path / "jobs.db"),
            "SESSION_DB": str(sessions_db),
            "REWRITE_CODEX_USER_HOMES_DIR": str(tmp_path / "codex-homes"),
            "REWRITE_ALLOWED_OPENAI_MODELS": "gpt-4.1-nano",
            "REWRITE_ALLOWED_CLAUDE_MODELS": "claude-sonnet-4-6",
            "REWRITE_ALLOWED_CODEX_MODELS": "codex:gpt-5.6-terra",
        }
    )
    env.update(extra_env)
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def test_admission_covers_every_credential_kind(tmp_path):
    """A: для каждого вида — своё работает, ничего нет → внятная 503, не 500."""
    _run_server_script(
        """
        import json
        import rewrite_core
        import server
        from fastapi import HTTPException

        def expect(status, model, uid, needle):
            try:
                server._validate_model_admission(model, uid)
            except HTTPException as exc:
                assert exc.status_code == status, (model, exc.status_code)
                assert needle in exc.detail, exc.detail
            else:
                raise AssertionError(f"{model} admitted without credential")

        # 1) Claude: нет ни OAuth, ни Anthropic-ключа, ни серверной подписки.
        expect(503, "claude-sonnet-4-6", "partner", "Anthropic API key")
        # 2) Codex: серверная подписка — владельца, своей нет.
        expect(503, "codex:gpt-5.6-terra", "partner", "your own Codex")
        # 3) OpenAI: ни своего, ни серверного.
        expect(503, "gpt-4.1-nano", "partner", "your own key")

        # Свой Anthropic-ключ открывает Claude-модель по API-пути (без подписки).
        rewrite_core.save_keys_config(
            anthropic_api_key="own-anthropic", user_id="partner"
        )
        is_claude, is_codex, tokens = server._validate_model_admission(
            "claude-sonnet-4-6", "partner"
        )
        assert (is_claude, is_codex, tokens) == (True, False, [])

        # Свой OAuth-токен переводит ту же модель на путь подписки.
        rewrite_core.save_keys_config(
            oauth_tokens=["own-claude-oauth"], user_id="partner"
        )
        assert server._validate_model_admission(
            "claude-sonnet-4-6", "partner"
        ) == (True, False, ["own-claude-oauth"])

        # Своя подписка Codex открывает codex-модель.
        rewrite_core.save_keys_config(
            codex_auth=json.dumps({"tokens": {"access_token": "own-codex"}}),
            user_id="partner",
        )
        assert server._validate_model_admission(
            "codex:gpt-5.6-terra", "partner"
        )[:2] == (False, True)

        # Свой OpenAI-ключ открывает gpt-модель.
        rewrite_core.save_keys_config(openai_api_key="own-openai", user_id="partner")
        assert server._validate_model_admission("gpt-4.1-nano", "partner") == (
            False, False, []
        )
        """,
        tmp_path,
        {},
    )


def test_claude_job_without_oauth_runs_on_own_anthropic_key(tmp_path):
    """A: задача Claude без OAuth не уходит в подписку и берёт свой API-ключ."""
    _run_server_script(
        """
        import rewrite_core
        import server
        from types import SimpleNamespace

        server.license_mgr.is_user_active = lambda _uid: True
        rewrite_core.save_keys_config(
            anthropic_api_key="own-anthropic-key", user_id="partner"
        )
        request = SimpleNamespace(state=SimpleNamespace(user_id="partner"))
        req = server.RewriteJobRequest(
            files=["script.txt"],
            languages=[{"code": "English", "display": "English"}],
            model="claude-sonnet-4-6",
            max_threads=1,
        )
        job_id = server.create_rewrite_job(req, request)["job_id"]
        job = server.queue.get_job(job_id)
        # Подписки нет → use_subscription False, значит провайдер = прямой API.
        assert job.use_subscription is False, job.use_subscription
        assert job.api_key == "" and job.oauth_tokens == "[]"
        assert server._get_effective_api_key(job) == "own-anthropic-key"

        # А с OAuth-токеном та же модель идёт подпиской и ключа не требует.
        rewrite_core.save_keys_config(oauth_tokens=["own-oauth"], user_id="partner")
        job_id2 = server.create_rewrite_job(req, request)["job_id"]
        job2 = server.queue.get_job(job_id2)
        assert job2.use_subscription is True
        assert server._get_effective_api_key(job2) == ""
        """,
        tmp_path,
        {},
    )


def test_api_v1_admission_matches_ui_admission(tmp_path):
    """A+B: у API v1 та же политика кредов, что у UI (сайты не расходятся)."""
    _run_server_script(
        """
        import json
        import rewrite_core
        import server
        from api import ApiJobCreate
        from fastapi import HTTPException

        # Достаём реальную функцию создания задачи API v1 из роутера.
        route = next(
            r for r in server.api_router.routes
            if getattr(r, "path", "") == "/api/v1/jobs"
            and "POST" in getattr(r, "methods", set())
        )
        create = route.endpoint

        # Файл тенанта должен существовать — иначе 400 сработает раньше кредов.
        upload_dir = server.UPLOAD_DIR / "partner"
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "fileid_script.txt").write_text("text", encoding="utf-8")

        def admit(model, uid):
            body = ApiJobCreate(
                files=["fileid"],
                languages=[{"code": "English"}],
                model=model,
            )
            return create(body, {"user_id": uid})

        # Codex без своей подписки → 503, а не тихая работа на кредах владельца.
        try:
            admit("codex:gpt-5.6-terra", "partner")
        except HTTPException as exc:
            assert exc.status_code == 503, exc.status_code
            assert "your own Codex" in exc.detail, exc.detail
        else:
            raise AssertionError("API v1 leaked the owner Codex subscription")

        # Claude без OAuth, но со своим Anthropic-ключом → 503 больше не летит.
        try:
            admit("claude-sonnet-4-6", "partner")
        except HTTPException as exc:
            assert exc.status_code == 503, exc.status_code
            assert "Anthropic API key" in exc.detail, exc.detail
        else:
            raise AssertionError("API v1 admitted Claude without any credential")
        """,
        tmp_path,
        {},
    )


# ------------------------------------------------------------------- C: no leak


def test_settings_keys_never_returns_any_credential_value(tmp_path):
    """C: только признаки «задан»; ни свои, ни серверные значения не уходят."""
    _run_server_script(
        """
        import json
        import rewrite_core
        import server
        from types import SimpleNamespace

        own_openai = "own-openai-secret-value"
        own_anthropic = "own-anthropic-secret-value"
        own_oauth = "own-claude-oauth-secret-value"
        own_codex = json.dumps({"tokens": {"access_token": "own-codex-secret"}})
        server_openai = "server-openai-secret-value"
        server_anthropic = "server-anthropic-secret-value"
        server_oauth = "server-claude-oauth-secret-value"

        rewrite_core.save_keys_config(
            openai_api_key=own_openai,
            anthropic_api_key=own_anthropic,
            oauth_tokens=[own_oauth],
            codex_auth=own_codex,
            user_id="partner",
        )
        request = SimpleNamespace(state=SimpleNamespace(user_id="partner"))
        resp = server.get_settings_keys(request)
        blob = json.dumps(resp, ensure_ascii=False)

        assert resp["own_openai_key_set"] is True
        assert resp["own_anthropic_key_set"] is True
        assert resp["own_claude_token_set"] is True
        assert resp["own_codex_auth_set"] is True
        assert resp["openai_key_source"] == "user"
        assert resp["anthropic_key_source"] == "user"
        assert resp["claude_key_source"] == "user"
        assert resp["codex_key_source"] == "user"
        for secret in (own_openai, own_anthropic, own_oauth, "own-codex-secret",
                       server_openai, server_anthropic, server_oauth):
            assert secret not in blob, secret

        # Серверная подписка владельца партнёру даже не анонсируется.
        assert resp["codex_configured"] is False
        assert resp["anthropic_configured"] is False
        assert resp["server_credential_owner"] is False

        # HTML не содержит ни одного значения — только поля ввода.
        html = (
            server.STATIC_DIR / "index.html"
        ).read_text(encoding="utf-8")
        for secret in (own_openai, own_anthropic, own_oauth, "own-codex-secret"):
            assert secret not in html
        assert "set-own-anthropic-key" in html
        assert "set-own-codex-auth" in html

        # Значение своей подписки Codex вычищается из логов задач тем же
        # редактором, что серверные креды. FIX-2026-07-30 (P3 №5): регистрация
        # секретов перенесена на путь РЕАЛЬНОГО использования (материализация
        # CODEX_HOME перед вызовом CLI) — read-only просмотр настроек больше не
        # засоряет глобальный реестр. Гарантия для логов задач та же.
        rewrite_core.get_user_codex_homes("partner")
        assert "own-codex-secret" not in rewrite_core.redact_server_credentials(
            f"leak {own_codex}"
        )
        assert "own-anthropic-secret-value" not in (
            rewrite_core.redact_server_credentials(f"leak {own_anthropic}")
        )
        """,
        tmp_path,
        {
            "OPENAI_API_KEY": "server-openai-secret-value",
            "ANTHROPIC_API_KEY": "server-anthropic-secret-value",
            "REWRITE_CLAUDE_OAUTH_TOKENS": json.dumps(
                ["server-claude-oauth-secret-value"]
            ),
            "REWRITE_CODEX_HOME": "/srv/owner-codex-home",
        },
    )


def test_codex_auth_endpoint_rejects_garbage_and_accepts_json(tmp_path):
    """C: ручка принимает только JSON-объект auth.json, значение не эхом."""
    _run_server_script(
        """
        import asyncio
        import json
        import rewrite_core
        import server
        from fastapi import HTTPException
        from types import SimpleNamespace

        class Req:
            def __init__(self, payload):
                self._payload = payload
                self.state = SimpleNamespace(user_id="partner")

            async def json(self):
                return self._payload

        def post(payload):
            return asyncio.run(server.save_settings_keys(Req(payload)))

        for bad in ("not json at all", "[1,2,3]", "\\"just-a-string\\"", 42):
            try:
                post({"codex_auth": bad})
            except HTTPException as exc:
                assert exc.status_code == 422, (bad, exc.status_code)
            else:
                raise AssertionError(f"codex_auth accepted garbage: {bad!r}")

        good = json.dumps({"tokens": {"access_token": "abc"}})
        assert post({"codex_auth": good}) == {"status": "ok"}
        assert rewrite_core.get_user_codex_auth("partner") == good
        # "" = удалить свою подписку.
        assert post({"codex_auth": ""}) == {"status": "ok"}
        assert rewrite_core.get_user_codex_auth("partner") == ""
        """,
        tmp_path,
        {},
    )


# ----------------------------------------------------- D: migration / startup


def test_migration_keeps_codex_auth_with_flag_off_and_on(tmp_path):
    """D: своя подписка Codex выживает миграцию и при OFF, и при ON."""
    sessions_db = tmp_path / "sessions.db"
    jobs_db = tmp_path / "jobs.db"
    codex_auth = json.dumps({"tokens": {"access_token": "codex-must-survive"}})
    own_key = "openai-must-survive"
    own_anthropic = "anthropic-must-survive"
    job_secret = "job-carried-secret-must-die"

    migrate_user_keys(str(sessions_db), str(tmp_path / "no-legacy.json"))
    with sqlite3.connect(sessions_db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (username TEXT, user_id TEXT, status TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT, user_id TEXT)")
        conn.execute(
            "INSERT INTO user_keys (user_id, openai_api_key, anthropic_api_key, "
            "codex_auth, updated_at) VALUES ('partner', ?, ?, ?, 0)",
            (own_key, own_anthropic, codex_auth),
        )
    with sqlite3.connect(jobs_db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute(
            "INSERT INTO jobs VALUES ('j1', ?)",
            (json.dumps({"api_key": job_secret, "model": "gpt-4.1-nano"}),),
        )

    def stored():
        with sqlite3.connect(sessions_db) as conn:
            return conn.execute(
                "SELECT openai_api_key, anthropic_api_key, codex_auth "
                "FROM user_keys WHERE user_id='partner'"
            ).fetchone()

    # Флаг OFF — истинный no-op.
    assert migrate_shared_keys_fair_queue(
        str(sessions_db), str(jobs_db), enabled=False
    )[:2] == (0, 0)
    assert stored() == (own_key, own_anthropic, codex_auth)

    # Флаг ON — job-секрет вычищен, пользовательские креды целы.
    user_rows, job_rows, _msg = migrate_shared_keys_fair_queue(
        str(sessions_db), str(jobs_db), enabled=True
    )
    assert user_rows == 0
    assert job_rows == 1
    assert stored() == (own_key, own_anthropic, codex_auth)
    with sqlite3.connect(jobs_db) as conn:
        assert job_secret not in conn.execute(
            "SELECT data FROM jobs"
        ).fetchone()[0]

    # Идемпотентность: живой codex_auth не считается residue.
    again = migrate_shared_keys_fair_queue(
        str(sessions_db), str(jobs_db), enabled=True
    )
    assert again[:2] == (0, 0)
    assert "already applied" in again[2]
    assert stored() == (own_key, own_anthropic, codex_auth)


def test_dead_server_key_does_not_break_startup_or_claude_cap(tmp_path):
    """D+E: мёртвый/пустой серверный пул не роняет старт и не обнуляет кап."""
    _run_server_script(
        """
        import rewrite_core
        import server

        # Серверных кредов нет вовсе — сервис всё равно поднялся (импорт прошёл).
        assert rewrite_core.get_server_openai_keys() == []
        assert rewrite_core.get_server_claude_oauth_tokens() == []
        # Preflight по-прежнему честно сообщает о пустом пуле, но это лишь
        # предупреждение на старте, а не остановка сервиса.
        try:
            rewrite_core.preflight_server_credential_pools()
        except RuntimeError:
            pass
        else:
            raise AssertionError("preflight silently accepted an empty pool")

        # Находка ревью: кап claude не должен быть 0 при пустом серверном пуле,
        # иначе claude-задачи на СВОИХ токенах висят pending вечно.
        assert server._provider_job_caps["claude"] >= 1, server._provider_job_caps
        assert server._provider_job_caps["openai"] >= 1
        assert server._provider_job_caps["codex"] >= 1
        """,
        tmp_path,
        {"REWRITE_SHARED_KEYS_ENABLED": "1"},
    )


def test_own_openai_key_survives_missing_codex_column(tmp_path, monkeypatch):
    """D: БД без колонки codex_auth не должна ломать чтение остальных ключей."""
    sessions_db = tmp_path / "legacy-sessions.db"
    with sqlite3.connect(sessions_db) as conn:
        conn.execute(
            """
            CREATE TABLE user_keys (
                user_id TEXT PRIMARY KEY,
                anthropic_api_key TEXT NOT NULL DEFAULT '',
                openai_api_key TEXT NOT NULL DEFAULT '',
                oauth_tokens TEXT NOT NULL DEFAULT '[]',
                gpt_service_tier TEXT NOT NULL DEFAULT 'default',
                gpt_use_cache INTEGER NOT NULL DEFAULT 1,
                postprocess_check_model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                custom_models TEXT NOT NULL DEFAULT '[]',
                debug_mode INTEGER NOT NULL DEFAULT 0,
                rew_presets TEXT NOT NULL DEFAULT '{}',
                gen_settings_presets TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )
    monkeypatch.setattr(rewrite_core, "SESSIONS_DB_PATH", str(sessions_db))
    rewrite_core.save_keys_config(openai_api_key="legacy-own-key", user_id="partner")
    assert rewrite_core.get_user_openai_keys("partner") == ["legacy-own-key"]
    assert rewrite_core.get_user_codex_auth("partner") == ""

    # Сохранение своей подписки Codex само добавляет колонку (self-healing).
    payload = json.dumps({"tokens": {"access_token": "late-codex"}})
    rewrite_core.save_keys_config(codex_auth=payload, user_id="partner")
    assert rewrite_core.get_user_codex_auth("partner") == payload
    assert rewrite_core.get_user_openai_keys("partner") == ["legacy-own-key"]
