#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отказ провайдера доезжает словами провайдера, а не нашей обёрткой.

Какой дефект ловится. Картинка через v6 отказывала так:

    result = {"success": False, "error": f"{_lbl}: v6 генерация не удалась"}

и эта строка печаталась и в «Попытка N неудачна», и в «Все N попыток исчерпаны», то есть
была ВСЕМ, что человек видел в логе задачи. Ни HTTP-кода, ни текста провайдера в ней нет.
Вторая половина того же дефекта — в журнале вызовов: `_journal_close_frame` закрывал
строку по правилу `error or _last_gen_error() or "кадр не получен"`, а при отказе на
СОЗДАНИИ заказа (единственное место, где провайдер вообще называет причину)
`_last_gen_error` пуст — в колонку `error` уезжало «кадр не получен».

Отсюда боевой итог: причину восьми отказов установить нельзя вообще.

Третий кусок — транспорт. `googler_request_v6` на 400/402/403/422 возвращал тело ответа
БЕЗ кода, а 401 вообще не был в списке «без ретрая»: мёртвый ключ шесть раз получал один
и тот же отказ, после чего наверх уходило «HTTPError: 401 Client Error» — без тела, то
есть без единственного места, где провайдер объясняет, что не так.

Проверяется: код в тексте, отказ ключа без шести повторов, причина в журнале и в логе,
и то, что причина прошлой попытки не выдаётся за причину нынешней.

Провайдера нет — его ответы подменены.

    .venv/bin/python tests/test_provider_refusal_reason.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

import googler_worker as G                                        # noqa: E402

FAILS = []

SECRET_URL = "https://cdn.fastgen.example/x?sig=AAAABBBBCCCCDDDD"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class Resp:
    """Ответ провайдера: ровно то, что читает googler_request_v6."""

    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("не json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise G.requests.exceptions.HTTPError(
                f"{self.status_code} Client Error: for url: x")


class FakeHTTP:
    """Транспорт под запись: сколько раз спросили и что ответили."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts += 1
        return self.responses[min(self.posts - 1, len(self.responses) - 1)]

    def get(self, url, headers=None, timeout=None):
        self.posts += 1
        return self.responses[min(self.posts - 1, len(self.responses) - 1)]


class FakeJournal:
    """Журнал вызовов на бумажке: интересует только текст отказа."""

    enabled = True

    def __init__(self):
        self.failed_with = None

    def delivered(self, call_id, result_path, cost=None):
        return True

    def failed(self, call_id, error, ordered=False):
        self.failed_with = str(error)
        return True


def with_http(fake, fn):
    """Подменить транспорт воркера на время одного вызова."""
    saved_post, saved_get = G.requests.post, G.requests.get
    G.requests.post, G.requests.get = fake.post, fake.get
    try:
        return fn()
    finally:
        G.requests.post, G.requests.get = saved_post, saved_get


def test_http_code_travels_with_the_body():
    print("\nКод ответа едет вместе с телом, а мёртвый ключ не повторяют шесть раз")
    fake = FakeHTTP(Resp(401, {"error": {"code": "invalid_api_key",
                                         "message": "API key expired"}}))
    r = with_http(fake, lambda: G.googler_request_v6("generations", data={"prompt": "кот"}))
    err = str(r.get("_error") or "")
    check_true("код в тексте отказа", err.startswith("HTTP 401:"))
    check_true("и суть от провайдера тоже", "API key expired" in err)
    check("401 не ретраится: ключ мёртв, повтор вернёт то же самое", fake.posts, 1)

    fake = FakeHTTP(Resp(402, None, text="no credits remaining"))
    r = with_http(fake, lambda: G.googler_request_v6("generations", data={"prompt": "x"}))
    check("не-json тело тоже с кодом", str(r.get("_error")), "HTTP 402: no credits remaining")


def test_refusal_is_remembered_on_order_creation():
    print("\nОтказ на создании заказа запоминается как настоящая причина")
    G._set_provider_refusal("")
    G._set_last_gen_error("")
    fake = FakeHTTP(Resp(401, {"error": "invalid api key"}))
    out = with_http(fake, lambda: G.googler_v6_generate("narwhal", "кот на крыше", index=1))
    check("кадр не получен", out, None)
    check_true("причина запомнена", "HTTP 401" in G._provider_refusal())
    check_true("вместе со словами провайдера", "invalid api key" in G._provider_refusal())

    print("  и ответ без id операции — тоже причина, а не пустота")
    G._set_provider_refusal("")
    saved = G.googler_request_v6
    G.googler_request_v6 = lambda *a, **kw: {"status": "queued"}
    try:
        G.googler_v6_generate("narwhal", "кот", index=1)
    finally:
        G.googler_request_v6 = saved
    check_true("сказано, чего не хватило", "без id операции" in G._provider_refusal())


def test_wrapper_no_longer_wins():
    print("\nОбёртка уступает место правде провайдера")
    G._set_provider_refusal("")
    G._set_last_gen_error("")
    check("правды нет — остаётся обёртка", G._gen_failure_error("NARWHAL"),
          "NARWHAL: v6 генерация не удалась")

    G._set_provider_refusal('HTTP 401: {"error":"api key revoked"}')
    text = G._gen_failure_error("NARWHAL")
    check_true("модель названа", text.startswith("NARWHAL: "))
    check_true("код в строке лога", "HTTP 401" in text)
    check("обёртки больше нет", "генерация не удалась" in text, False)

    print("  причина прошлой попытки не выдаётся за причину нынешней")
    G._set_provider_refusal("")
    check("после сброса — снова обёртка", G._gen_failure_error("NARWHAL"),
          "NARWHAL: v6 генерация не удалась")


def test_journal_gets_the_real_reason():
    print("\nВ колонку error журнала вызовов уходит причина, а не «кадр не получен»")
    G._set_provider_refusal("")
    G._set_last_gen_error("")
    journal = FakeJournal()
    G._JOURNAL_FRAME.state = {"journal": journal, "call_id": 5, "order_id": "",
                              "ordered": False, "closed": False, "path": ""}
    G._set_provider_refusal('HTTP 401: {"error":"api key expired"}')
    G._journal_close_frame(None)
    check_true("причина в журнале", "HTTP 401" in (journal.failed_with or ""))
    check("безликого «кадр не получен» нет",
          "кадр не получен" in (journal.failed_with or ""), False)

    print("  наша обёртка пропускает правду вперёд")
    journal = FakeJournal()
    G._JOURNAL_FRAME.state = {"journal": journal, "call_id": 6, "order_id": "",
                              "ordered": False, "closed": False, "path": ""}
    G._set_provider_refusal("HTTP 402: no credits remaining")
    G._journal_close_frame(None, error="NARWHAL: v6 генерация не удалась")
    check_true("в журнале причина", "no credits remaining" in (journal.failed_with or ""))

    print("  а текст исключения причину называет сам и остаётся первым")
    journal = FakeJournal()
    G._JOURNAL_FRAME.state = {"journal": journal, "call_id": 7, "order_id": "",
                              "ordered": False, "closed": False, "path": ""}
    G._set_provider_refusal("HTTP 402: no credits remaining")
    G._journal_close_frame(None, error="OpenAIKeyError: ключ отсутствует/пуст")
    check_true("исключение не затёрто", "OpenAIKeyError" in (journal.failed_with or ""))

    print("  ничего не известно — прежний запасной текст на месте")
    G._set_provider_refusal("")
    G._set_last_gen_error("")
    journal = FakeJournal()
    G._JOURNAL_FRAME.state = {"journal": journal, "call_id": 8, "order_id": "",
                              "ordered": False, "closed": False, "path": ""}
    G._journal_close_frame(None)
    check("запасной текст", journal.failed_with, "кадр не получен")


def _silent(module):
    """Подменить печать воркера на список строк. Возвращает (строки, вернуть_как_было)."""
    lines = []
    saved_user, saved_log = module.print_user, module.log
    module.print_user = lambda msg, level="INFO", **kw: lines.append(str(msg))
    module.log = lambda msg, *a, **kw: lines.append(str(msg))

    def restore():
        module.print_user, module.log = saved_user, saved_log

    return lines, restore


def test_image_call_site_really_says_it():
    """Не хелпер в изоляции, а ТА САМАЯ строка, которую человек читает в логе задачи.

    Проверяется место подключения: generate_image уходит в ветку v6-отказа, и в
    «Попытка N неудачна» и «Все N попыток исчерпаны» обязан стоять ответ провайдера.
    Вернуть на строке 11090 литерал `f"{_lbl}: v6 генерация не удалась"` — тест падает.
    """
    print("\nКартинки: причина доезжает до строк «Попытка N» и «Все N исчерпаны»")
    saved_model, saved_delay = G.GOOGLER_MODEL, G.RETRY_DELAY
    saved_gen = G.googler_v6_generate
    G.GOOGLER_MODEL = "NARWHAL_LITE"      # любая модель ветки v6
    G.RETRY_DELAY = 0                     # без сна: проверяем текст, а не паузы

    def refuse(*_a, **_kw):
        G._set_provider_refusal('HTTP 402: {"error":"no credits remaining"}')
        return None

    G.googler_v6_generate = refuse
    lines, restore = _silent(G)
    try:
        out = G.generate_image({"visual_prompt": "кот на крыше"}, "cinematic", index=0)
    finally:
        restore()
        G.googler_v6_generate = saved_gen
        G.GOOGLER_MODEL, G.RETRY_DELAY = saved_model, saved_delay
    check("кадр не получен", out[1], None)
    attempt = [l for l in lines if "Попытка 1 неудачна" in l]
    final = [l for l in lines if "попыток исчерпаны" in l]
    check_true("строка попытки есть", bool(attempt))
    check_true("и в ней код провайдера", bool(attempt) and "HTTP 402" in attempt[0])
    check("а обёртки в ней нет",
          bool(attempt) and "v6 генерация не удалась" in attempt[0], False)
    check_true("итоговая строка есть", bool(final))
    check_true("и в ней тоже причина", bool(final) and "no credits remaining" in final[0])


def test_video_call_sites_really_say_it():
    """Клип — самый дорогой вызов, и до правки его отказ читался как «v6 video не удалось».

    Половина правки (сброс причины) в видео-ветках была, а читателя у неё не было:
    ни i2v, ни t2v не печатали ни кода, ни слов провайдера.
    """
    print("\nВидео: тот же отказ виден и на клипах — i2v и t2v")
    saved = (G.VIDEO_METHOD, G.RETRY_DELAY, G.VEO_RETRY_DELAY,
             G.googler_v6_generate, G.image_to_base64)
    G.VIDEO_METHOD = "flower"             # v6-ветка видео, не veo
    G.RETRY_DELAY = G.VEO_RETRY_DELAY = 0
    G.image_to_base64 = lambda _p: "data:image/jpeg;base64,AAAA"

    def refuse(*_a, **_kw):
        G._set_provider_refusal('HTTP 401: {"error":"api key revoked"}')
        return None

    G.googler_v6_generate = refuse
    lines, restore = _silent(G)
    try:
        i2v = G.generate_video("кот", "/tmp/frame_001.jpg", index=0)
        i2v_lines = list(lines)
        lines.clear()
        t2v = G._t2v_generate_with_retry("кот", "landscape", 0)
        t2v_lines = list(lines)
    finally:
        restore()
        (G.VIDEO_METHOD, G.RETRY_DELAY, G.VEO_RETRY_DELAY,
         G.googler_v6_generate, G.image_to_base64) = saved
    check("клипа нет", (i2v[1], t2v[1]), (None, None))
    attempt = [l for l in i2v_lines if "Ошибка" in l]
    check_true("отказ попытки назван словами провайдера",
               bool(attempt) and "HTTP 401" in attempt[0])
    check("безликого «v6 video не удалось» больше нет",
          any("v6 video не удалось" in l for l in i2v_lines), False)
    last = [l for l in i2v_lines if "Не удалось запустить генерацию видео" in l]
    check_true("итоговая строка клипа объясняет, почему",
               bool(last) and "api key revoked" in last[0])
    t2v_last = [l for l in t2v_lines if "клип не получен после" in l]
    check_true("и у t2v появилась такая же итоговая строка",
               bool(t2v_last) and "HTTP 401" in t2v_last[0])


def test_frame_that_never_called_has_no_reason():
    """Ранний выход кадра не имеет права закрыться причиной ЧУЖОГО кадра.

    `_v6_generic_terminal_frame_blocked` и `_account_limit_hit` возвращают None ДО
    цикла попыток, то есть до сброса причины, а пул переиспользует потоки: в журнале
    у кадра, к провайдеру не ходившего, появлялось убедительное «HTTP 401 expired».
    Сброс стоит на входе в заявку (_journal_frame_call) — убрать его оттуда, и тест
    падает.
    """
    print("\nКадр, который к провайдеру не ходил, закрывается без чужой причины")
    G._set_provider_refusal('HTTP 401: {"error":"api key expired"}')
    G._set_last_gen_error("прошлый кадр: таймаут")
    journal = FakeJournal()

    @G._journal_frame_call
    def _blocked_frame():
        # ровно то, что делают ранние выходы: ни одного обращения к провайдеру
        G._JOURNAL_FRAME.state = {"journal": journal, "call_id": 42, "order_id": "",
                                  "ordered": False, "closed": False, "path": ""}
        return (0, None)

    _blocked_frame()
    check("в журнале запасной текст, а не чужая причина",
          journal.failed_with, "кадр не получен")


def test_poll_failure_is_a_reason_too():
    print("\nОтказ на опросе уже созданного заказа — тоже причина")
    G._set_provider_refusal("")
    G._set_last_gen_error("")
    fake = FakeHTTP(Resp(200, {"status": "failed",
                               "error": "SAFETY_FILTER: prompt rejected"}))
    out = with_http(fake, lambda: G.poll_v6_generation("gen-1", index=1, timeout=5))
    check("кадра нет", out, None)
    check_true("причина от провайдера сохранена",
               "SAFETY_FILTER" in G._provider_refusal())
    check_true("и ретрай-логика видит её по-прежнему",
               "SAFETY_FILTER" in G._last_gen_error())


def test_signed_url_in_the_body_is_not_our_problem_to_leak():
    print("\nПодписанный URL из тела отказа уезжает в кабинет — и там режется")
    G._set_provider_refusal("")
    fake = FakeHTTP(Resp(403, None, text="forbidden for " + SECRET_URL))
    r = with_http(fake, lambda: G.googler_request_v6("generations", data={"p": 1}))
    check_true("воркер тело не прячет: иначе причина пропадёт совсем",
               SECRET_URL in str(r.get("_error")))
    # Резать — работа кабинета, и она проверена в tests/test_key_refusal_reason.py:
    # у воркера списка секретов кабинета нет и быть не должно.


def main() -> int:
    saved_refusal, saved_last = G._provider_refusal(), G._last_gen_error()
    saved_state = getattr(G._JOURNAL_FRAME, "state", None)
    try:
        test_http_code_travels_with_the_body()
        test_refusal_is_remembered_on_order_creation()
        test_wrapper_no_longer_wins()
        test_journal_gets_the_real_reason()
        test_image_call_site_really_says_it()
        test_video_call_sites_really_say_it()
        test_frame_that_never_called_has_no_reason()
        test_poll_failure_is_a_reason_too()
        test_signed_url_in_the_body_is_not_our_problem_to_leak()
    finally:
        G._set_provider_refusal(saved_refusal)
        G._set_last_gen_error(saved_last)
        G._JOURNAL_FRAME.state = saved_state
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
