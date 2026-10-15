#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кадр под фильтром после смягчений — один заказ другой моделью того же ключа (05.10.2026).

Что случилось. Выпуск №101 «Istoria din Farfurie» четыре раза за ночь падал на картинках:
6 кадров из 175 (сцены про реального человека и марку) провайдер резал фильтром
безопасности и после трёх смягчений. Это больше 2% ролика, подмена предыдущим кадром не
положена, а «вернуть в работу» упиралось в тот же фильтр. У моделей одного ключа FastGen
фильтры разные — так уже спасаются обложки (cover_core.COVER_SAFETY_FALLBACK_PROVIDER).

Проверяется:
  * отказ фильтра после смягчений → ровно один заказ откатной моделью, кадр получен,
    строка «🛟 … фильтр модели X — заказ моделью Y» в журнале задачи;
  * откат тоже отклонён → больше заказов нет, кадр сдаётся как «фильтр» (повтор позже его
    не тронет, решает правило 2%);
  * тот же кадр в том же прогоне (добивка пропущенных) второй откат не получает;
  * обычный сбой (не фильтр) и откат на ту же модель отката не вызывают;
  * откат выключается настройкой;
  * openai-image дважды молча отказал («Generation failed») — тот же один заказ откатом
    (14.10.2026), а добивка основную модель больше не оплачивает;
  * openai-image N раз подряд ответил «No accounts available» — тот же откат (15.10.2026);
    откат тоже занят — кадр «позже», и добивка снова имеет право на откат;
  * журнал платных вызовов: строка основной модели закрыта отказом, откат заявлен своей
    строкой с моделью отката;
  * фактическая модель кадра — в image_model_fallbacks.json; кадр, позже нарисованный
    основной моделью, оттуда убирается; квитанции кадров не тронуты.

Провайдера нет — его ответы подменены.

    .venv/bin/python tests/test_image_safety_fallback.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))
os.environ.setdefault("VS_TEST_NO_NET", "1")

import googler_worker as G                                        # noqa: E402

# Порядок по умолчанию (11.10.2026) проверяется в конце; сценарии ниже написаны для
# одной модели отката — Flower.
DEFAULT_CHAIN = G.IMAGE_SAFETY_FALLBACK_MODEL
G.IMAGE_SAFETY_FALLBACK_MODEL = "FLOWER"

FAILS = []
TMP = tempfile.mkdtemp(prefix="image-safety-fallback-")
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-frame").decode("ascii")
FILTER = "Request blocked by provider safety filters"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Provider:
    """Подменённый v6: на каждую модель — свой ответ, все вызовы записаны."""

    def __init__(self, answers):
        self.answers = answers           # {v6-модель: "ok" | текст отказа}
        self.calls = []

    def __call__(self, v6_model, prompt, **kw):
        self.calls.append((v6_model, len(kw.get("inputs") or [])))
        answer = self.answers.get(v6_model, "ok")
        if answer == "ok":
            return PNG
        # Как опрос v6 на status=failed: причина — и в ретрай-логику, и в журнал.
        G._set_last_gen_error(G._typed_v6_terminal_error(answer))
        G._set_provider_refusal(answer)
        return None

    def count(self, v6_model):
        return sum(1 for model, _n in self.calls if model == v6_model)


def run(answers, *, scene=1, model="NARWHAL_LITE", out_dir=None, fresh_run=True):
    """Один кадр через настоящий generate_image: (результат, провайдер, лог, журнал, папка)."""
    if fresh_run:
        G._SAFETY_FALLBACK_USED.clear()
        G._SAFETY_FALLBACK_BUSY.clear()
        G._FRAME_GIVEUP.clear()
        G._V6_GENERIC_TERMINAL_FRAMES.clear()
    out_dir = out_dir or tempfile.mkdtemp(dir=TMP)
    provider = Provider(answers)
    lines, reserves, closes = [], [], []
    saved = (G.GOOGLER_MODEL, G.RETRY_DELAY, G.googler_v6_generate, G.print_user,
             G._journal_reserve_frame, G._journal_close_frame)
    real_reserve, real_close = G._journal_reserve_frame, G._journal_close_frame

    def reserve(*a, **kw):
        reserves.append(kw.get("model"))
        return real_reserve(*a, **kw)

    def close(image_data, error=""):
        closes.append((bool(image_data), error))
        return real_close(image_data, error)

    G.GOOGLER_MODEL, G.RETRY_DELAY = model, 0
    G.googler_v6_generate = provider
    G.print_user = lambda msg, *a, **k: lines.append(str(msg))
    G._journal_reserve_frame, G._journal_close_frame = reserve, close
    try:
        out = G.generate_image({"visual_prompt": "general Thanom Chakkapak at a sauce factory"},
                               "cinematic", index=scene - 1,
                               output_path=os.path.join(out_dir, f"{scene:03d}.jpg"))
    finally:
        (G.GOOGLER_MODEL, G.RETRY_DELAY, G.googler_v6_generate, G.print_user,
         G._journal_reserve_frame, G._journal_close_frame) = saved
    return out, provider, lines, (reserves, closes), out_dir


def sidecar(out_dir):
    path = os.path.join(out_dir, G.IMAGE_MODEL_FALLBACKS_FILE)
    if not os.path.exists(path):
        return None
    return json.load(open(path, encoding="utf-8"))


LITE, FLOWER = G.IMAGE_MODEL_V6["NARWHAL_LITE"], G.IMAGE_MODEL_V6["FLOWER"]

print("\nФильтр основной модели → один заказ моделью отката")
out, prov, lines, (reserves, closes), d1 = run({LITE: FILTER, FLOWER: "ok"})
check("кадр получен", out[1] is not None, True)
check("модели отката — ровно один заказ", prov.count(FLOWER), 1)
check("основная модель: исходный заказ и смягчения (не больше четырёх)",
      1 <= prov.count(LITE) <= 4, True)
check("откат — после всех заказов основной модели",
      [m for m, _n in prov.calls].index(FLOWER) == len(prov.calls) - 1, True)
check("строка в журнале задачи",
      any("🛟 [1] Сцена 1: фильтр модели" in l and "заказ моделью" in l for l in lines), True)
check("журнал вызовов: заявка основной модели, затем отката", reserves, ["NARWHAL_LITE", "FLOWER"])
check("строка основной модели закрыта отказом с указанием отката",
      any((not ok) and "заказан моделью FLOWER" in err for ok, err in closes), True)
check("строка отката закрыта доставкой", bool(closes) and closes[-1][0], True)
mark = sidecar(d1)
check("фактическая модель кадра записана рядом с квитанциями",
      [(r["scene"], r["model"], r["instead_of"]) for r in (mark or {}).get("frames", [])],
      [(1, "FLOWER", "NARWHAL_LITE")])
check("квитанции кадров не тронуты",
      os.path.exists(os.path.join(d1, G.IMAGE_RECEIPTS_FILE)), False)

print("\nОткат тоже отклонён фильтром")
out, prov, lines, _j, d2 = run({LITE: FILTER, FLOWER: FILTER})
check("кадра нет", out[1], None)
check("модели отката — всё равно ровно один заказ", prov.count(FLOWER), 1)
check("кадр сдался как «фильтр» (повтор позже его не тронет)",
      G._FRAME_GIVEUP.get(0, ("", ""))[0], G.GAP_FILTER)
check("в причине — что откат не помог",
      "откат на модель FLOWER" in G._FRAME_GIVEUP.get(0, ("", ""))[1], True)
check("пометки модели нет — кадра нет", sidecar(d2), None)

print("\nТот же кадр снова в этом прогоне (добивка пропущенных)")
out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: "ok"}, out_dir=d2, fresh_run=False)
check("второго отката нет", prov.count(FLOWER), 0)
check("кадр сдался как «фильтр»",
      (out[1], G._FRAME_GIVEUP.get(0, ("", ""))[0]), (None, G.GAP_FILTER))

print("\nОткат упал обычным сбоем, а не фильтром")
# Как в бою: кабинет ставит ключу 15 попыток (GOOGLER_MAX_RETRIES), и у отката остаются
# витки — без запрета повторов каждый из них был бы ещё одной оплатой.
os.environ["GOOGLER_MAX_RETRIES"] = "15"
try:
    out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: "HTTP 500: internal error"})
finally:
    os.environ.pop("GOOGLER_MAX_RETRIES", None)
check("повторов на откатной модели нет — каждый был бы оплатой",
      (out[1], prov.count(FLOWER)), (None, 1))

print("\nОткат не для всего подряд")
out, prov, lines, _j, _d = run({LITE: "HTTP 500: internal error"})
check("обычный сбой — без отката", prov.count(FLOWER), 0)
out, prov, lines, _j, _d = run({FLOWER: FILTER}, model="FLOWER")
check("основная модель и есть модель отката — отката нет",
      prov.count(FLOWER) >= 1 and all(m == FLOWER for m, _n in prov.calls)
      and not any("🛟" in l for l in lines), True)
check("safety_fallback_model(FLOWER) пуст", G.safety_fallback_model("FLOWER"), "")
check("safety_fallback_model(NARWHAL_LITE) = FLOWER",
      G.safety_fallback_model("NARWHAL_LITE"), "FLOWER")
saved_fb = G.IMAGE_SAFETY_FALLBACK_MODEL
G.IMAGE_SAFETY_FALLBACK_MODEL = "OFF"
try:
    out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: "ok"})
    check("выключено настройкой — отката нет", (prov.count(FLOWER), out[1]), (0, None))
    G.IMAGE_SAFETY_FALLBACK_MODEL = "NO_SUCH_MODEL"
    check("неизвестная модель отката — отката нет", G.safety_fallback_model("NARWHAL_LITE"), "")
finally:
    G.IMAGE_SAFETY_FALLBACK_MODEL = saved_fb

print("\nКадр позже нарисован основной моделью — пометка убирается")
check("до перерисовки пометка есть", bool(sidecar(d1)), True)
out, prov, lines, _j, _d = run({LITE: "ok"}, out_dir=d1)
check("кадр получен основной моделью", (out[1] is not None, prov.count(FLOWER)), (True, 0))
check("пометка убрана (файл удалён — других кадров в нём не было)", sidecar(d1), None)

print("\nПометка не трогает чужие кадры")
d3 = tempfile.mkdtemp(dir=TMP)
G.record_model_fallback(os.path.join(d3, "007.jpg"), 7,
                        {"model": "FLOWER", "instead_of": "NARWHAL_LITE", "reason": "x", "at": 1})
out, prov, lines, _j, _d = run({LITE: "ok"}, out_dir=d3)
check("кадр 1 перерисован, пометка кадра 7 осталась",
      [r["scene"] for r in (sidecar(d3) or {}).get("frames", [])], [7])

print("\nОткатный заказ прошлого прогона потерян провайдером (404) — заказывается заново "
      "(09.10.2026)")
# Живой случай «Istoria din Farfurie», задача 937: журнал помнил откатный заказ (verify),
# провайдер на опрос отвечал 404, и кадр пропускался в каждом прогоне без нового заказа.
from call_journal import Decision                                 # noqa: E402
retired, saved = [], (G._journal_reserve_frame, G.poll_v6_generation, G.googler_request_v6,
                      G._journal_retire_lost)
answers = iter([Decision("verify", call_id=7, remote_order_id="gen_lost"),
                Decision("retry", call_id=7)])


def reserve_fb(*a, **kw):
    if kw.get("model") == "FLOWER":
        return next(answers)
    return G._JOURNAL_OFF


G._journal_reserve_frame = reserve_fb
G.poll_v6_generation = lambda *a, **k: None
G.googler_request_v6 = lambda path, **k: {"_error": "HTTP 404: not found or expired"}
G._journal_retire_lost = lambda decision, final: retired.append((decision.remote_order_id, final))
try:
    out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: "ok"}, scene=4)
finally:
    (G._journal_reserve_frame, G.poll_v6_generation, G.googler_request_v6,
     G._journal_retire_lost) = saved
check("потерянный откатный заказ закрыт «результата не будет»",
      [(o, f.startswith("провайдер потерял заказ")) for o, f in retired], [("gen_lost", True)])
check("и откатной моделью заказан заново — ровно один раз", prov.count(FLOWER), 1)
check("кадр получен", out[1] is not None, True)
check("в журнале задачи сказано, что заказываем заново",
      any("заказываю откатный кадр заново" in l for l in lines), True)
check("«журнал не разрешил» больше не пишется",
      any("Журнал не разрешил откатный заказ" in l for l in lines), False)

print("\nОткатная модель временно занята — право на откат не сгорает (09.10.2026)")
# Задача 993: «No accounts available, please try again later» на Flower — кадр уходил
# соседнему, хотя добивка могла бы взять его откатом.
BUSY = "No accounts available, please try again later"
d5 = tempfile.mkdtemp(dir=TMP)
out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: BUSY}, scene=5, out_dir=d5)
check("первый прогон: кадра нет", out[1] is None, True)
check("сдался как «позже», а не «фильтр»", (G._FRAME_GIVEUP.get(4) or ("", ""))[0], G.GAP_LATER)
check("значит, поздний повтор его возьмёт", G.late_retry_indices([4]), [4])
check("в журнале задачи — «временно недоступен», а не «пропускаю»",
      (any("временно недоступен" in l for l in lines),
       any("Откат на" in l and "пропускаю картинку" in l for l in lines)), (True, False))
out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: "ok"}, scene=5, out_dir=d5, fresh_run=False)
check("добивка того же прогона: откат заказан снова и кадр получен",
      (prov.count(FLOWER), out[1] is not None), (1, True))

print("\nОтказ самой откатной модели — по-прежнему окончательно")
d6 = tempfile.mkdtemp(dir=TMP)
out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: FILTER}, scene=6, out_dir=d6)
check("сдался как «фильтр»", (G._FRAME_GIVEUP.get(5) or ("", ""))[0], G.GAP_FILTER)
out, prov, lines, _j, _d = run({LITE: FILTER, FLOWER: "ok"}, scene=6, out_dir=d6, fresh_run=False)
check("второго отката в том же прогоне нет", prov.count(FLOWER), 0)

print("\nopenai-image дважды отказал без причины — откат (14.10.2026)")
OPENAI_V6 = G.IMAGE_MODEL_V6["OPENAI"]
GENERIC = "Generation failed, please try again later"
out, prov, lines, (reserves, _c), _d = run({OPENAI_V6: GENERIC, FLOWER: "ok"},
                                           scene=11, model="OPENAI")
check("кадр получен откатом", out[1] is not None, True)
check("openai-image — два заказа, откат — один",
      (prov.count(OPENAI_V6), prov.count(FLOWER)), (2, 1))
check("в журнале задачи — что отказ без причины",
      any("🛟" in l and "дважды отказала без причины" in l for l in lines), True)
check("журнал вызовов: openai, затем откат", reserves[-1], "FLOWER")
d12 = tempfile.mkdtemp(dir=TMP)
out, prov, lines, _j, _d = run({OPENAI_V6: GENERIC, FLOWER: GENERIC}, scene=12,
                               model="OPENAI", out_dir=d12)
check("откат тоже отказал — кадра нет, заказов 2 + 1",
      (out[1], prov.count(OPENAI_V6), prov.count(FLOWER)), (None, 2, 1))
out, prov, lines, _j, _d = run({OPENAI_V6: "ok", FLOWER: "ok"}, scene=12, model="OPENAI",
                               out_dir=d12, fresh_run=False)
check("добивка того же прогона ничего не оплачивает", (out[1], len(prov.calls)), (None, 0))
out, prov, lines, _j, _d = run({OPENAI_V6: "Request blocked by provider safety filters",
                                FLOWER: "ok"}, scene=13, model="OPENAI")
check("одиночный generic не нужен: фильтр по-прежнему идёт своим путём",
      (out[1] is not None, prov.count(FLOWER)), (True, 1))
saved_fb = G.IMAGE_SAFETY_FALLBACK_MODEL
G.IMAGE_SAFETY_FALLBACK_MODEL = "OFF"
try:
    out, prov, lines, _j, _d = run({OPENAI_V6: GENERIC}, scene=14, model="OPENAI")
    check("откат выключен — как раньше: два заказа и стоп",
          (out[1], prov.count(OPENAI_V6),
           any("дважды вернул generic terminal" in l for l in lines)), (None, 2, True))
finally:
    G.IMAGE_SAFETY_FALLBACK_MODEL = saved_fb

print("\nopenai-image: «No accounts available» подряд — откат (15.10.2026)")
# С 00:00 UTC 15.10 пул аккаунтов openai-image у FastGen пуст; кадр крутил 15 попыток
# и падал дырой. Владелец: такие кадры — тем же откатом.
N = G.IMAGE_BUSY_FALLBACK_AFTER
check("порог по умолчанию — 3", N, 3)
out, prov, lines, (reserves, _c), _d = run({OPENAI_V6: BUSY, FLOWER: "ok"},
                                           scene=21, model="OPENAI")
check("кадр получен откатом после N «занято»",
      (out[1] is not None, prov.count(OPENAI_V6), prov.count(FLOWER)), (True, N, 1))
check("в журнале задачи — что нет свободных аккаунтов",
      any("🛟" in l and "нет свободных аккаунтов" in l for l in lines), True)
check("журнал вызовов: откат заявлен своей моделью", reserves[-1], "FLOWER")
check("модель кадра записана рядом",
      (sidecar(_d) or {}).get("frames", [{}])[0].get("instead_of"), "OPENAI")
d22 = tempfile.mkdtemp(dir=TMP)
out, prov, lines, _j, _d = run({OPENAI_V6: BUSY, FLOWER: BUSY}, scene=22,
                               model="OPENAI", out_dir=d22)
check("откат тоже занят — кадр отложен «позже», не «фильтр»",
      (out[1], (G._FRAME_GIVEUP.get(21) or ("", ""))[0]), (None, G.GAP_LATER))
out, prov, lines, _j, _d = run({OPENAI_V6: BUSY, FLOWER: "ok"}, scene=22, model="OPENAI",
                               out_dir=d22, fresh_run=False)
check("добивка того же прогона: право на откат вернулось, кадр получен",
      (out[1] is not None, prov.count(FLOWER)), (True, 1))
saved_fb = G.IMAGE_SAFETY_FALLBACK_MODEL
G.IMAGE_SAFETY_FALLBACK_MODEL = "OFF"
try:
    out, prov, lines, _j, _d = run({OPENAI_V6: BUSY}, scene=23, model="OPENAI")
    check("откат выключен — крутит все попытки, как раньше, без «фильтра»",
          (out[1], prov.count(OPENAI_V6) > N, (G._FRAME_GIVEUP.get(22) or ("", ""))[0] != G.GAP_FILTER),
          (None, True, True))
finally:
    G.IMAGE_SAFETY_FALLBACK_MODEL = saved_fb
out, prov, lines, _j, _d = run({LITE: BUSY, FLOWER: "ok"}, scene=24)
check("другая основная модель «занято» — без отката, как раньше",
      (out[1], prov.count(FLOWER)), (None, 0))

print("\nПорядок отката: Nano Banana 2 → Pro → Flower последним (11.10.2026)")
# Владелец: Flower по качеству сильно хуже — в последнюю очередь.
check("по умолчанию", DEFAULT_CHAIN, "NARWHAL,GEM_PIX_2,FLOWER")
G.IMAGE_SAFETY_FALLBACK_MODEL = DEFAULT_CHAIN
NB2, PRO = G.IMAGE_MODEL_V6["NARWHAL"], G.IMAGE_MODEL_V6["GEM_PIX_2"]
try:
    G._SAFETY_FALLBACK_BUSY.clear()
    check("основная Lite → откат Nano Banana 2", G.safety_fallback_model("NARWHAL_LITE"), "NARWHAL")
    check("основная Nano Banana 2 → откат Pro", G.safety_fallback_model("NARWHAL"), "GEM_PIX_2")
    out, prov, lines, _j, _d = run({LITE: FILTER, NB2: "ok"}, scene=7)
    check("кадр получен Nano Banana 2, Flower не звали",
          (out[1] is not None, prov.count(NB2), prov.count(FLOWER)), (True, 1, 0))
    d8 = tempfile.mkdtemp(dir=TMP)
    out, prov, lines, _j, _d = run({LITE: FILTER, NB2: BUSY}, scene=8, out_dir=d8)
    check("Nano Banana 2 занята — кадр отложен «позже»",
          (out[1], (G._FRAME_GIVEUP.get(7) or ("", ""))[0]), (None, G.GAP_LATER))
    out, prov, lines, _j, _d = run({LITE: FILTER, NB2: "ok", PRO: "ok"}, scene=8,
                                   out_dir=d8, fresh_run=False)
    check("добивка: откат уже следующей по списку — Pro",
          (out[1] is not None, prov.count(PRO), prov.count(NB2), prov.count(FLOWER)),
          (True, 1, 0, 0))
    G._SAFETY_FALLBACK_BUSY.update({"NARWHAL", "GEM_PIX_2"})
    check("заняты обе — Flower", G.safety_fallback_model("NARWHAL_LITE"), "FLOWER")
    G._SAFETY_FALLBACK_BUSY.add("FLOWER")
    check("заняты все — снова первая по списку", G.safety_fallback_model("NARWHAL_LITE"), "NARWHAL")
finally:
    G.IMAGE_SAFETY_FALLBACK_MODEL = "FLOWER"
    G._SAFETY_FALLBACK_BUSY.clear()

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
