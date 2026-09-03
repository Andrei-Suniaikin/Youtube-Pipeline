"""FIX-2026-07-30: находки ревью BYOK (P0 №1 … P2 №11).

Каждый тест падает на коде ДО соответствующей правки:

  №1  рантайм не переармирует use_subscription (был безусловный claude→подписка);
  №2  свой Anthropic-ключ выигрывает у СЕРВЕРНОГО OAuth-пула (был обратный порядок);
  №3  обновлённый Codex'ом auth.json возвращается в БД (жил только в /tmp);
  №4  корень per-user CODEX_HOME приватный, симлинк отвергается;
  №5  read-only пути не засоряют глобальный реестр секретов;
  №6  несохранённый codex_auth честно возвращает False (был тихий ok);
  №7  rate limiter на владельца credential (был один глобальный на всех);
  №8  «не смог прочитать» ≠ «ключа нет» (был тихий откат на серверный ключ);
  №9  у пользовательского пула своя параллельность (была ёмкость 1);
  №10 серверная подписка Codex — только владельцу (env-allowlist не единственный гейт);
  №11 глобальный кап на claude-CLI реально берётся (семафор был мёртвым).
"""

import json
import os
import queue as std_queue
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import rewrite_core
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
    "REWRITE_USER_OPENAI_PER_KEY_CONCURRENCY",
)


def _reset_process_registries() -> None:
    """Сбросить процесс-глобальные реестры между тестами.

    getattr-guard'ы нужны, чтобы файл можно было прогнать и на коде ДО правок
    (там части реестров ещё нет) — тест обязан падать на самой проверке, а не
    разваливаться в setup.
    """
    for registry, lock in (
        ("_SERVER_CREDENTIAL_POOLS", "_SERVER_CREDENTIAL_POOLS_LOCK"),
        ("_RATE_LIMITERS", "_RATE_LIMITERS_LOCK"),
        ("_BYOK_CONFIGURED", "_BYOK_CONFIGURED_LOCK"),
        ("_CODEX_HOME_OWNERS", "_CODEX_HOME_OWNERS_LOCK"),
    ):
        store = getattr(rewrite_core, registry, None)
        guard = getattr(rewrite_core, lock, None)
        if store is None or guard is None:
            continue
        with guard:
            store.clear()


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    for name in SERVER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(rewrite_core, "ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setenv(
        "REWRITE_CODEX_USER_HOMES_DIR", str(tmp_path / "codex-user-homes")
    )
    _reset_process_registries()
    yield
    _reset_process_registries()
    rewrite_core.clear_current_user_id()


@pytest.fixture
def tenant_store(tmp_path, monkeypatch):
    sessions_db = tmp_path / "sessions.db"
    migrate_user_keys(str(sessions_db), str(tmp_path / "no-legacy-config.json"))
    monkeypatch.setattr(rewrite_core, "SESSIONS_DB_PATH", str(sessions_db))
    return sessions_db


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
        timeout=180,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


# ------------------------------------------------- №1 + №2: runtime не перетирает


def test_runtime_does_not_rearm_subscription_over_own_anthropic_key(tmp_path):
    """№1+№2: свой Anthropic-ключ не подменяется серверной подпиской владельца.

    До правки: (а) admission брал get_effective_claude_oauth_tokens → серверный
    пул поверх своего ключа; (б) _execute_job_common безусловно ставил
    use_subscription=True для claude → задача уезжала на подписку владельца.
    """
    _run_server_script(
        """
        import rewrite_core
        import server
        from types import SimpleNamespace

        server.license_mgr.is_user_active = lambda _uid: True
        rewrite_core.save_keys_config(
            anthropic_api_key="own-anthropic-key", user_id="partner"
        )

        # Серверный OAuth-пул существует и НЕ должен выигрывать у своего ключа.
        assert rewrite_core.get_server_claude_oauth_tokens() == [
            "server-oauth-token"
        ]
        assert rewrite_core.credential_source("claude", "partner") == "user"

        request = SimpleNamespace(state=SimpleNamespace(user_id="partner"))
        req = server.RewriteJobRequest(
            files=["script.txt"],
            languages=[{"code": "English", "display": "English"}],
            model="claude-sonnet-4-6",
            max_threads=1,
        )
        job_id = server.create_rewrite_job(req, request)["job_id"]
        assert server.queue.get_job(
            job_id
        ).use_subscription is False, "admission armed the owner subscription"
        # Задача должна быть RUNNING с валидным run_token — иначе provider_guard
        # честно отменит её ещё до job_fn.
        job = server.queue.claim_next("claude")
        assert job is not None and job.id == job_id

        seen = {}

        def fake_job_fn(j, log_queue, stop_event):
            seen["use_sub"] = j.use_subscription
            seen["api_key"] = server._get_effective_api_key(j)

        server._execute_job_common(job, fake_job_fn)
        assert not job.error, job.error
        assert seen["use_sub"] is False, "runtime re-armed the owner subscription"
        assert seen["api_key"] == "own-anthropic-key", seen["api_key"]

        # Свой OAuth-токен по-прежнему включает путь подписки.
        rewrite_core.save_keys_config(oauth_tokens=["own-oauth"], user_id="partner")
        job_id2 = server.create_rewrite_job(req, request)["job_id"]
        job2 = server.queue.claim_next("claude")
        assert job2 is not None and job2.id == job_id2
        assert job2.use_subscription is True
        seen.clear()
        server._execute_job_common(job2, fake_job_fn)
        assert not job2.error, job2.error
        assert seen["use_sub"] is True
        """,
        tmp_path,
        {"REWRITE_CLAUDE_OAUTH_TOKENS": json.dumps(["server-oauth-token"])},
    )


def test_own_anthropic_key_beats_server_oauth_pool(tenant_store, monkeypatch):
    """№2: порядок свой OAuth → свой Anthropic → серверный OAuth."""
    monkeypatch.setenv(
        "REWRITE_CLAUDE_OAUTH_TOKENS", json.dumps(["server-oauth-token"])
    )
    rewrite_core.save_keys_config(anthropic_api_key="own-anthropic", user_id="partner")

    # Свой API-ключ выигрывает: OAuth-путь не выбирается ([] = прямой API).
    assert rewrite_core.get_claude_admission_credential("partner") == []
    assert rewrite_core.credential_source("claude", "partner") == "user"

    # Свой OAuth важнее своего API-ключа.
    rewrite_core.save_keys_config(oauth_tokens=["own-oauth"], user_id="partner")
    assert rewrite_core.get_claude_admission_credential("partner") == ["own-oauth"]

    # Ничего своего нет → серверный пул (прежнее поведение сохранено).
    rewrite_core.save_keys_config(
        oauth_tokens=[], anthropic_api_key="", user_id="partner"
    )
    assert rewrite_core.get_claude_admission_credential("partner") == [
        "server-oauth-token"
    ]

    # Нет вообще ничего → None, admission обязан отказать.
    monkeypatch.delenv("REWRITE_CLAUDE_OAUTH_TOKENS")
    assert rewrite_core.get_claude_admission_credential("partner") is None


# ------------------------------------------------------- №3: refresh обратно в БД


def _codex_payload(token: str) -> str:
    return json.dumps({"tokens": {"access_token": token}, "last_refresh": "2026-07-30"})


def test_refreshed_codex_auth_is_persisted_back_to_db(tenant_store, tmp_path):
    """№3: обновлённый CLI токен переживает чистку каталога homes."""
    rewrite_core.save_keys_config(
        codex_auth=_codex_payload("original-token"), user_id="partner"
    )
    home = rewrite_core.get_user_codex_homes("partner")[0]

    # Codex обновил auth.json в своей изолированной копии.
    isolated = tmp_path / "isolated-home"
    isolated.mkdir()
    refreshed = _codex_payload("rotated-token")
    (isolated / "auth.json").write_text(refreshed, encoding="utf-8")

    rewrite_core._sync_codex_auth_back(str(isolated), home)

    # Значение вернулось в хранилище тенанта, а не осталось только в файле.
    assert json.loads(rewrite_core.get_user_codex_auth("partner")) == json.loads(
        refreshed
    )

    # Каталог homes стёрли (эмуляция чистки /tmp) — токен не потерян.
    import shutil

    shutil.rmtree(rewrite_core._codex_user_homes_root())
    new_home = rewrite_core.get_user_codex_homes("partner")[0]
    assert json.loads(
        (Path(new_home) / "auth.json").read_text(encoding="utf-8")
    ) == json.loads(refreshed)


def test_codex_homes_root_is_not_in_shared_tempdir(tenant_store, monkeypatch):
    """№3/№4: дефолтный корень — приватный каталог приложения, не /tmp."""
    import tempfile

    monkeypatch.delenv("REWRITE_CODEX_USER_HOMES_DIR", raising=False)
    root = rewrite_core._codex_user_homes_root()
    assert not root.startswith(tempfile.gettempdir() + os.sep), root
    assert root.startswith(os.path.dirname(os.path.abspath(str(tenant_store))))


# ------------------------------------------------------------- №4: симлинк-атака


def test_codex_home_rejects_symlinked_directory(tenant_store, tmp_path, monkeypatch):
    """№4: подменённый симлинком каталог тенанта не принимается."""
    homes_root = tmp_path / "homes-root"
    homes_root.mkdir(mode=0o700)
    monkeypatch.setenv("REWRITE_CODEX_USER_HOMES_DIR", str(homes_root))
    rewrite_core.save_keys_config(
        codex_auth=_codex_payload("token-value"), user_id="partner"
    )
    import hashlib

    digest = hashlib.sha256(b"partner").hexdigest()[:16]
    attacker_target = tmp_path / "attacker"
    attacker_target.mkdir()
    os.symlink(str(attacker_target), str(homes_root / digest))

    with pytest.raises(RuntimeError):
        rewrite_core.get_user_codex_homes("partner")
    # Секрет в подставной каталог не записан.
    assert not (attacker_target / "auth.json").exists()


# ------------------------------------- №5: read-only не засоряет реестр секретов


def test_readonly_paths_do_not_register_secret_tree(tenant_store):
    """№5: регистрируются только секретные поля и только на пути использования."""
    payload = json.dumps(
        {
            "tokens": {"access_token": "codex-access-token-value"},
            "account_email": "partner-person@example.com",
            "last_refresh": "2026-07-30T10:00:00Z",
        }
    )
    rewrite_core.save_keys_config(codex_auth=payload, user_id="partner")
    with rewrite_core._REGISTERED_SERVER_SECRETS_LOCK:
        rewrite_core._REGISTERED_SERVER_SECRETS.clear()

    # Read-only пути (кабинет, credential_source) реестр не трогают.
    rewrite_core.get_user_codex_auth("partner")
    rewrite_core.credential_source("codex", "partner")
    with rewrite_core._REGISTERED_SERVER_SECRETS_LOCK:
        assert rewrite_core._REGISTERED_SERVER_SECRETS == set()

    # Материализация (реальное использование) регистрирует именно секреты.
    rewrite_core.get_user_codex_homes("partner")
    with rewrite_core._REGISTERED_SERVER_SECRETS_LOCK:
        registered = set(rewrite_core._REGISTERED_SERVER_SECRETS)
    assert "codex-access-token-value" in registered
    assert "partner-person@example.com" not in registered
    assert "2026-07-30T10:00:00Z" not in registered


# --------------------------------------------------- №6: честный статус сохранения


def test_save_keys_config_reports_unsaved_codex_auth(tenant_store, monkeypatch):
    """№6: провал ALTER больше не выдаётся за успешное сохранение."""
    assert rewrite_core.save_keys_config(
        codex_auth=_codex_payload("ok"), user_id="partner"
    ) is True

    monkeypatch.setattr(
        rewrite_core, "_ensure_user_keys_codex_column", lambda _conn: False
    )
    assert rewrite_core.save_keys_config(
        codex_auth=_codex_payload("not-saved"), user_id="partner"
    ) is False


# ----------------------------------------------------- №7: лимитер по владельцу


def test_rate_limiter_is_per_credential_owner(tenant_store, monkeypatch):
    """№7: 429 по чужому BYOK-ключу не поднимает задержку остальным."""
    monkeypatch.setenv("OPENAI_API_KEY", "server-openai-key")
    rewrite_core.save_keys_config(openai_api_key="dead-partner-key", user_id="partner")
    rewrite_core.save_keys_config(openai_api_key="good-key", user_id="other")

    partner = rewrite_core.rate_limiter_for("openai", "partner")
    other = rewrite_core.rate_limiter_for("openai", "other")
    server_pool = rewrite_core.rate_limiter_for("openai", "no-own-key-tenant")

    assert partner is not other and partner is not server_pool
    assert rewrite_core.rate_limiter_for("openai", "partner") is partner

    for _ in range(5):
        partner.on_rate_limit()
    assert partner.get_delay() > 5.0
    assert other.get_delay() < 2.0
    assert server_pool.get_delay() < 2.0


# ------------------------------------------ №8: сбой чтения ≠ «своего ключа нет»


def test_storage_failure_does_not_fall_back_to_server_key(tenant_store, monkeypatch):
    """№8: при сбое чтения BYOK-тенант не уезжает на серверный ключ владельца."""
    monkeypatch.setenv("OPENAI_API_KEY", "server-openai-key")
    rewrite_core.save_keys_config(openai_api_key="own-openai-key", user_id="partner")
    assert rewrite_core.get_effective_openai_keys("partner") == ["own-openai-key"]

    # Хранилище становится нечитаемым (эмуляция блокировки/сбоя sqlite).
    monkeypatch.setattr(
        rewrite_core, "SESSIONS_DB_PATH", "/nonexistent-dir/sessions.db"
    )
    with pytest.raises(rewrite_core.KeysConfigUnavailable):
        rewrite_core.get_user_openai_keys("partner")
    with pytest.raises(rewrite_core.KeysConfigUnavailable):
        rewrite_core.get_effective_openai_keys("partner")

    # Тенант без BYOK продолжает работать на серверном ключе (регресса нет).
    assert rewrite_core.get_effective_openai_keys("never-had-own-key") == [
        "server-openai-key"
    ]


# ---------------------------------------------- №9: параллельность своего пула


def test_user_pool_has_its_own_concurrency_limit(tenant_store):
    """№9: один свой ключ больше не означает ёмкость пула = 1."""
    assert rewrite_core._credential_limit("openai") == 1
    assert rewrite_core._credential_limit("openai", "partner") > 1

    rewrite_core.save_keys_config(openai_api_key="own-openai-key", user_id="partner")
    pool = rewrite_core.get_effective_credential_pool("openai", "partner")
    assert pool.stats()["capacity"] > 1

    first = pool.acquire(timeout=0)
    try:
        second = pool.acquire(timeout=0)  # до правки: CredentialPoolExhausted
        second.release()
    finally:
        first.release()


# ------------------------------------ №10: серверная подписка Codex — владельцу


def test_codex_server_subscription_is_owner_only_without_env_allowlist(
    tenant_store, monkeypatch
):
    """№10: пустой env-allowlist больше не открывает подписку владельца всем."""
    monkeypatch.setenv("REWRITE_CODEX_HOME", "/srv/owner-codex-home")
    monkeypatch.delenv("REWRITE_CODEX_ALLOWED_USER_IDS", raising=False)

    # Необязательный env-гейт по-прежнему «разрешает» — но он не единственный.
    assert rewrite_core.provider_user_allowed("codex", "partner") is True
    assert rewrite_core.credential_source("codex", "partner") == "none"
    with pytest.raises(rewrite_core.CredentialPoolExhausted):
        rewrite_core.get_effective_credential_pool("codex", "partner")

    # Владельцу серверная подписка доступна.
    assert rewrite_core.credential_source("codex", ADMIN) == "server"


# ----------------------------------------- №11: глобальный кап на claude-CLI


def test_claude_cli_calls_are_capped_by_global_semaphore(monkeypatch):
    """№11: семафор реально берётся (раньше объявлен и не использовался)."""
    calls = []

    def fake_impl(*args, **kwargs):
        calls.append(1)
        return True, "ok"

    monkeypatch.setattr(rewrite_core, "_call_claude_subscription_impl", fake_impl)
    monkeypatch.setattr(rewrite_core, "_claude_sema", threading.Semaphore(1))
    monkeypatch.setattr(rewrite_core, "SUBSCRIPTION_CALL_TIMEOUT_SEC", 0)

    # Слот свободен — вызов проходит.
    ok, result = rewrite_core.call_claude_subscription(
        [{"role": "user", "content": "hi"}], std_queue.Queue(), oauth_token="t"
    )
    assert (ok, result) == (True, "ok")
    assert len(calls) == 1

    # Слот занят другим вызовом — CLI не запускается, ошибка внятная.
    rewrite_core._claude_sema.acquire()
    try:
        ok, result = rewrite_core.call_claude_subscription(
            [{"role": "user", "content": "hi"}], std_queue.Queue(), oauth_token="t"
        )
    finally:
        rewrite_core._claude_sema.release()
    assert ok is False
    assert rewrite_core.SUBSCRIPTION_TIMEOUT_PREFIX in result
    assert len(calls) == 1, "CLI слот не ограничивает параллельность"
