"""Стартовая проверка VoiceGenCore (GET /client/me) повторяется при обрывах связи.

Инцидент 15.09.2026 02:06–02:07: сразу после сброса дневного лимита провайдер отвечал
дольше 15 с, воркер делал ОДИН запрос и валил задачу («Read timed out») — j15120 и j15103
тенанта 14 ушли в error без единого повтора, хотя днём обычный повтор проходит за 40 с.

Инварианты:
  1) таймаут / обрыв / 5xx / 429 → повтор с нарастающей паузой 10→60 с, не больше 5 попыток;
  2) прочие 4xx (ключ отвергнут) → сразу стоп, без пауз и без второго запроса;
  3) в лог задачи уходит строка «VoiceGenCore не отвечает …, повтор через N с (попытка k/5)»;
  4) main() после удачного повтора идёт дальше (задача НЕ помечается error), а на 401 — стоп;
  5) весь стартовый цикл укладывается в бюджет ~4 минуты, даже если каждый запрос висит 30 с.
"""

import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import voicegencore_worker as worker  # noqa: E402


class _Response:
    def __init__(self, status):
        self.status_code = status


def _http_error(status):
    return worker.requests.exceptions.HTTPError(f"HTTP {status}", response=_Response(status))


ME_OK = {"data": {"has_active_subscription": True, "max_threads": 8, "free_remaining": 0}}


@pytest.fixture()
def wiring(monkeypatch):
    """check_auth отдаёт заранее заданную последовательность; паузы и лог — в списки."""
    state = {"script": [], "calls": 0, "sleeps": [], "log": []}

    def fake_check_auth(token):
        assert token == "tok"
        state["calls"] += 1
        item = state["script"].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(worker, "check_auth", fake_check_auth)
    monkeypatch.setattr(worker, "_pause", lambda s: state["sleeps"].append(s))
    monkeypatch.setattr(worker, "print_user", lambda msg, kind="INFO": state["log"].append(msg))
    monkeypatch.setattr(worker, "print_error_user_friendly", lambda msg, *a, **k: state["log"].append(msg))
    return state


def test_read_timeout_then_ok_proceeds(wiring):
    wiring["script"] = [worker.requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)"), ME_OK]

    assert worker.check_auth_with_retry("tok") is ME_OK
    assert wiring["calls"] == 2
    assert wiring["sleeps"] == [10]
    assert any("VoiceGenCore не отвечает (таймаут), повтор через 10 с (попытка 1/5)" in m for m in wiring["log"])


def test_401_stops_immediately_without_retry(wiring):
    wiring["script"] = [_http_error(401), ME_OK]

    with pytest.raises(worker.requests.exceptions.HTTPError):
        worker.check_auth_with_retry("tok")
    assert wiring["calls"] == 1
    assert wiring["sleeps"] == []
    assert not any("повтор через" in m for m in wiring["log"])


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_other_4xx_not_retried(wiring, status):
    wiring["script"] = [_http_error(status), ME_OK]
    with pytest.raises(worker.requests.exceptions.HTTPError):
        worker.check_auth_with_retry("tok")
    assert wiring["calls"] == 1 and wiring["sleeps"] == []


def test_5xx_429_and_connection_errors_are_retried(wiring):
    wiring["script"] = [
        _http_error(503),
        _http_error(429),
        worker.requests.exceptions.ConnectionError("boom"),
        ME_OK,
    ]

    assert worker.check_auth_with_retry("tok") is ME_OK
    assert wiring["calls"] == 4
    assert wiring["sleeps"] == [10, 20, 40]
    assert any("(HTTP 503), повтор через 10 с (попытка 1/5)" in m for m in wiring["log"])
    assert any("(HTTP 429), повтор через 20 с (попытка 2/5)" in m for m in wiring["log"])
    assert any("(нет соединения), повтор через 40 с (попытка 3/5)" in m for m in wiring["log"])


def test_gives_up_after_five_attempts_with_growing_pauses(wiring):
    wiring["script"] = [worker.requests.exceptions.ReadTimeout("t/o")] * 5 + [ME_OK]

    with pytest.raises(worker.VGCStartupUnavailable) as err:
        worker.check_auth_with_retry("tok")
    assert wiring["calls"] == 5            # шестой удачный ответ уже не спрашивается
    assert wiring["sleeps"] == [10, 20, 40, 60]
    assert "после 5 попыток" in str(err.value) and "t/o" in str(err.value)
    assert isinstance(err.value.__cause__, worker.requests.exceptions.ReadTimeout)


def test_startup_cycle_fits_in_budget_even_when_every_request_hangs(wiring, monkeypatch):
    """Худший случай: connect+read по 15 с = 30 с на запрос. Бюджет 240 с не пробивается."""
    clock = {"now": 0.0}
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock["now"])

    def hanging_check_auth(token):
        wiring["calls"] += 1
        clock["now"] += 30
        raise worker.requests.exceptions.ReadTimeout("t/o")

    def fake_pause(s):
        wiring["sleeps"].append(s)
        clock["now"] += s

    monkeypatch.setattr(worker, "check_auth", hanging_check_auth)
    monkeypatch.setattr(worker, "_pause", fake_pause)

    with pytest.raises(worker.VGCStartupUnavailable) as err:
        worker.check_auth_with_retry("tok")
    assert clock["now"] <= worker.AUTH_RETRY_BUDGET_SECONDS
    assert wiring["sleeps"] == [10, 20, 40]          # 60-секундная пауза уже не влезает в бюджет
    assert "после 4 попыток" in str(err.value)
    assert any("лимит ожидания 240 с исчерпан после 4 попыток" in m for m in wiring["log"])
    # типичный случай (таймаут 15 с на запрос) укладывается целиком: 5×15 + 130 = 205 с < 240
    assert worker.AUTH_RETRY_ATTEMPTS * 15 + sum(worker.AUTH_RETRY_DELAYS) <= worker.AUTH_RETRY_BUDGET_SECONDS


def test_cancel_during_wait_propagates(wiring, monkeypatch):
    wiring["script"] = [worker.requests.exceptions.ReadTimeout("t/o"), ME_OK]

    def cancelling_pause(s):
        raise worker.VGCCancelled("пользователь запросил отмену")

    monkeypatch.setattr(worker, "_pause", cancelling_pause)
    with pytest.raises(worker.VGCCancelled):
        worker.check_auth_with_retry("tok")
    assert wiring["calls"] == 1


# ---------- сквозная проводка через main() ----------

def _run_main(monkeypatch, tmp_path, wiring):
    project = tmp_path / "proj"
    project.mkdir()
    src = project / "text.txt"
    src.write_text("Первый абзац.\nВторой абзац.\n", encoding="utf-8")
    journal_root = project / ".remote_jobs"
    journal_root.mkdir()

    monkeypatch.setenv("VGC_API_TOKEN", "tok")
    monkeypatch.setattr(worker, "validate_pin", lambda project_dir, token: journal_root)

    def fake_process_text(token, text, voice_id, voice_engine, stability, output_dir, filename, thread_count, idx=0):
        Path(output_dir, filename).write_bytes(b"ID3fake")
        return True

    monkeypatch.setattr(worker, "process_text", fake_process_text)
    monkeypatch.setattr(sys, "argv", [
        "voicegencore_worker.py", "--project_dir", str(project),
        "--source_file", str(src), "--voice_id", "a" * 32,
    ])
    return worker.main()


def test_main_proceeds_after_transient_timeout(monkeypatch, tmp_path, wiring):
    wiring["script"] = [worker.requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)"), ME_OK]

    assert _run_main(monkeypatch, tmp_path, wiring) == 0
    assert wiring["calls"] == 2
    assert wiring["sleeps"] == [10]
    assert (tmp_path / "proj" / worker.AUDIO_DIR_NAME / "2.mp3").exists()
    assert any("повтор через 10 с (попытка 1/5)" in m for m in wiring["log"])
    assert not any("не удалось подключиться" in m for m in wiring["log"])


def test_main_stops_on_401_without_retry(monkeypatch, tmp_path, wiring):
    wiring["script"] = [_http_error(401), ME_OK]

    assert _run_main(monkeypatch, tmp_path, wiring) == 1
    assert wiring["calls"] == 1
    assert wiring["sleeps"] == []
    assert any("ключ отвергнут (HTTP 401)" in m for m in wiring["log"])


def test_main_reports_exhausted_retries_as_connection_failure(monkeypatch, tmp_path, wiring):
    wiring["script"] = [worker.requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")] * 5

    assert _run_main(monkeypatch, tmp_path, wiring) == 1
    assert wiring["calls"] == 5
    assert wiring["sleeps"] == [10, 20, 40, 60]
    assert any("не удалось подключиться: после 5 попыток" in m and "Read timed out" in m for m in wiring["log"])
