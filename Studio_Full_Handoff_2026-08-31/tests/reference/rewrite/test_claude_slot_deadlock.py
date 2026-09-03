"""FIX-2026-07-30 (P0): двойной захват глобального claude-семафора = дедлок.

Правка №11 добавила обёртки ``call_claude_subscription`` /
``call_claude_conversation``, которые берут ``_claude_sema`` через
``_claude_cli_slot()``. Но семафор УЖЕ брался внутри impl-функций
(``with _claude_sema`` — см. rewrite_core.py.bak.fix.20260730 строки 6813 и 7459),
то есть один и тот же поток брал НЕреентрантный ``threading.Semaphore`` дважды.
Второй acquire не возвращался никогда → перманентный дедлок воркера.

Тесты ниже гоняют РЕАЛЬНЫЕ impl-функции (сеть/CLI замоканы) в отдельном потоке
и требуют завершения по ``join(timeout)``. На дореформенном коде поток остаётся
живым (висит на втором acquire) → тест падает по таймауту.

Плюс два не блокирующих замечания того же ревью:
  * ``_BYOK_CONFIGURED`` — процессный кэш, после рестарта пуст (признак BYOK
    теперь берётся из БД разовым чтением);
  * ``return False`` при провале ALTER стоял после UPSERT внутри
    ``with sqlite3.connect(...)`` → «не сохранено» при реально сохранённых ключах.
"""

import queue as std_queue
import sqlite3
import threading

import pytest

import rewrite_core
from migrations.user_keys_001 import migrate_user_keys

JOIN_TIMEOUT_SEC = 20


def _reset_byok_registries() -> None:
    """Сброс процесс-глобальных реестров BYOK.

    getattr-guard'ы — чтобы файл прогонялся и на коде ДО правок (часть реестров
    там ещё нет): тест обязан падать на самой проверке, а не разваливаться в setup.
    """
    for registry, lock in (
        ("_BYOK_CONFIGURED", "_BYOK_CONFIGURED_LOCK"),
        ("_BYOK_PRIME_STATE", "_BYOK_PRIME_LOCK"),
    ):
        store = getattr(rewrite_core, registry, None)
        guard = getattr(rewrite_core, lock, None)
        if store is None or guard is None:
            continue
        with guard:
            store.clear()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_API_KEYS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKENS",
        "REWRITE_CLAUDE_OAUTH_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_byok_registries()
    yield
    _reset_byok_registries()
    rewrite_core.clear_current_user_id()


@pytest.fixture
def tenant_store(tmp_path, monkeypatch):
    sessions_db = tmp_path / "sessions.db"
    migrate_user_keys(str(sessions_db), str(tmp_path / "no-legacy-config.json"))
    monkeypatch.setattr(rewrite_core, "SESSIONS_DB_PATH", str(sessions_db))
    return sessions_db


def _run_with_timeout(fn, timeout=JOIN_TIMEOUT_SEC):
    """Выполнить fn() в отдельном потоке; вернуть (finished, result)."""
    box = {}

    def _target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — падение тоже «завершение»
            box["error"] = exc

    # daemon: на СЛОМАННОМ коде поток висит вечно, интерпретатор не должен его ждать.
    thread = threading.Thread(target=_target, daemon=True, name="claude-slot-probe")
    thread.start()
    thread.join(timeout)
    return (not thread.is_alive()), box


# ------------------------------------------------- one-shot (call_claude_subscription)


def test_subscription_call_does_not_deadlock_on_its_own_slot(monkeypatch):
    """Обёртка + impl не должны брать глобальный слот дважды из одного потока."""
    monkeypatch.setattr(rewrite_core, "CLAUDE_CODE_SDK_AVAILABLE", True, raising=False)
    monkeypatch.setattr(rewrite_core, "_ensure_cli_ready", lambda *a, **k: True)
    monkeypatch.setattr(
        rewrite_core, "_collect_hung_cli_forensics", lambda *a, **k: None
    )

    # Сеть/CLI не трогаем: первая же операция внутри _run() (уже ПОД семафором)
    # изображает stall — impl отработает свой timeout-путь и вернётся.
    def _boom(*args, **kwargs):
        raise TimeoutError("fake stream stall (test)")

    monkeypatch.setattr(rewrite_core, "_claude_isolated_options", _boom)
    # Ровно один слот: второй acquire из того же потока = вечное ожидание.
    monkeypatch.setattr(rewrite_core, "_claude_sema", threading.Semaphore(1))

    finished, box = _run_with_timeout(
        lambda: rewrite_core.call_claude_subscription(
            [{"role": "user", "content": "hi"}],
            std_queue.Queue(),
            oauth_token="test-token",
        )
    )

    assert finished, (
        "call_claude_subscription не вернулся за "
        f"{JOIN_TIMEOUT_SEC}s — двойной захват _claude_sema (дедлок)"
    )
    assert "error" not in box, box.get("error")
    ok, message = box["result"]
    assert ok is False
    assert rewrite_core.SUBSCRIPTION_TIMEOUT_PREFIX in message
    # Слот отпущен ровно один раз — счётчик семафора вернулся к исходному.
    assert rewrite_core._claude_sema.acquire(timeout=0.5) is True
    rewrite_core._claude_sema.release()


def test_conversation_call_does_not_deadlock_on_its_own_slot(monkeypatch):
    """Тот же дедлок на conversation-пути (лиза уже взята → обёртка берёт слот)."""
    monkeypatch.setattr(rewrite_core, "CLAUDE_CODE_SDK_AVAILABLE", True, raising=False)

    monkeypatch.setattr(
        rewrite_core,
        "_claude_isolated_options",
        lambda *a, **k: (object(), {}, "/tmp/does-not-matter"),
    )

    class _DeadClient:
        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            raise RuntimeError("fake connect failure (test)")

        async def disconnect(self):
            return None

    monkeypatch.setattr(
        rewrite_core, "_SanitizedClaudeSDKClient", _DeadClient, raising=False
    )
    monkeypatch.setattr(rewrite_core, "_claude_sema", threading.Semaphore(1))

    finished, box = _run_with_timeout(
        lambda: rewrite_core.call_claude_conversation(
            turn_prompts=["one"],
            system_prompt="sys",
            model="claude-sonnet-4-5-20250929",
            oauth_token="test-token",
            log_queue=std_queue.Queue(),
            _credential_leased=True,
        )
    )

    assert finished, (
        "call_claude_conversation не вернулся за "
        f"{JOIN_TIMEOUT_SEC}s — двойной захват _claude_sema (дедлок)"
    )
    assert "error" not in box, box.get("error")
    ok, responses, error = box["result"]
    assert ok is False and responses == []
    assert "connect failed" in (error or "")
    assert rewrite_core._claude_sema.acquire(timeout=0.5) is True
    rewrite_core._claude_sema.release()


def test_slot_is_taken_exactly_once_per_call(monkeypatch):
    """Кап сохранён: на вызов приходится РОВНО один acquire/release."""
    events = {"acquire": 0, "release": 0}
    real = threading.Semaphore(4)

    class _CountingSemaphore:
        def acquire(self, blocking=True, timeout=None):
            got = real.acquire(blocking, timeout)
            if got:
                events["acquire"] += 1
            return got

        def release(self):
            events["release"] += 1
            real.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    monkeypatch.setattr(rewrite_core, "_claude_sema", _CountingSemaphore())
    monkeypatch.setattr(rewrite_core, "CLAUDE_CODE_SDK_AVAILABLE", True, raising=False)
    monkeypatch.setattr(rewrite_core, "_ensure_cli_ready", lambda *a, **k: True)
    monkeypatch.setattr(
        rewrite_core, "_collect_hung_cli_forensics", lambda *a, **k: None
    )

    def _boom(*args, **kwargs):
        raise TimeoutError("fake stream stall (test)")

    monkeypatch.setattr(rewrite_core, "_claude_isolated_options", _boom)

    finished, box = _run_with_timeout(
        lambda: rewrite_core.call_claude_subscription(
            [{"role": "user", "content": "hi"}],
            std_queue.Queue(),
            oauth_token="test-token",
        )
    )
    assert finished, "дедлок: вызов не завершился"
    assert events == {"acquire": 1, "release": 1}, events
    # Слот больше никем не занят.
    assert real.acquire(timeout=0.5) is True
    real.release()


def test_nested_slot_is_noop_but_outer_still_caps(monkeypatch):
    """Вложенный вход в слот — no-op; на «чужой» поток кап продолжает действовать."""
    monkeypatch.setattr(rewrite_core, "_claude_sema", threading.Semaphore(1))
    monkeypatch.setattr(rewrite_core, "SUBSCRIPTION_CALL_TIMEOUT_SEC", 0)

    def _nested():
        with rewrite_core._claude_cli_slot():
            with rewrite_core._claude_cli_slot():  # до правки — вечное ожидание
                # Пока слот занят этим потоком, другой поток его не получает.
                other = _run_with_timeout(
                    lambda: rewrite_core._claude_sema.acquire(timeout=0.3), timeout=5
                )
                assert other[0] is True
                assert other[1]["result"] is False
        return "done"

    finished, box = _run_with_timeout(_nested, timeout=10)
    assert finished, "вложенный захват слота повесил поток"
    assert "error" not in box, box.get("error")
    assert box["result"] == "done"
    assert rewrite_core._claude_slot_held() is False


# ------------------------------------ признак BYOK переживает рестарт процесса


def test_byok_flag_is_restored_from_db_after_restart(tenant_store, monkeypatch):
    """№8 (добор): после рестарта сбой чтения не уводит BYOK-тенанта на серверный ключ."""
    monkeypatch.setenv("OPENAI_API_KEY", "server-openai-key")
    rewrite_core.save_keys_config(openai_api_key="own-openai-key", user_id="partner")

    # Рестарт uvicorn: процессные кэши пусты, credential тенанта в этом процессе
    # ещё ни разу не читали.
    _reset_byok_registries()

    # Любое штатное обращение к хранилищу на здоровой БД (например открытие
    # кабинета владельцем) — здесь и запоминается факт BYOK всех тенантов.
    rewrite_core.get_keys_config("admin")

    # Теперь БД становится нечитаемой.
    monkeypatch.setattr(
        rewrite_core, "SESSIONS_DB_PATH", "/nonexistent-dir/sessions.db"
    )
    with pytest.raises(rewrite_core.KeysConfigUnavailable):
        rewrite_core.get_user_openai_keys("partner")
    with pytest.raises(rewrite_core.KeysConfigUnavailable):
        rewrite_core.get_effective_openai_keys("partner")

    # Тенанта без своего ключа это не касается — работает на серверном.
    assert rewrite_core.get_effective_openai_keys("never-had-own-key") == [
        "server-openai-key"
    ]


def test_byok_prime_reads_all_providers(tenant_store):
    """Разовое чтение поднимает факт BYOK по всем провайдерам, не только по openai."""
    rewrite_core.save_keys_config(
        anthropic_api_key="own-anthropic",
        oauth_tokens=["sk-ant-oat01-own"],
        user_id="partner",
    )
    _reset_byok_registries()
    rewrite_core.get_keys_config("admin")

    assert rewrite_core._byok_was_configured("partner", "anthropic") is True
    assert rewrite_core._byok_was_configured("partner", "claude") is True
    assert rewrite_core._byok_was_configured("partner", "openai") is False


# ------------------------------- «не сохранено» больше не значит «частично сохранено»


def test_failed_codex_alter_does_not_commit_other_keys(tenant_store, monkeypatch):
    """Провал ALTER → False И ни одна колонка не записана (было: UPSERT коммитился)."""
    assert rewrite_core.save_keys_config(
        openai_api_key="first-value", user_id="partner"
    ) is True

    monkeypatch.setattr(
        rewrite_core, "_ensure_user_keys_codex_column", lambda _conn: False
    )
    assert rewrite_core.save_keys_config(
        openai_api_key="second-value",
        codex_auth='{"tokens": {"access_token": "x"}}',
        user_id="partner",
    ) is False

    with sqlite3.connect(str(tenant_store)) as conn:
        stored = conn.execute(
            "SELECT openai_api_key FROM user_keys WHERE user_id = ?", ("partner",)
        ).fetchone()
    assert stored[0] == "first-value", (
        "ответ был «не сохранено», а ключи всё-таки записались"
    )
