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
        G._set_provider_refusal(answer)
        return None

    def count(self, v6_model):
        return sum(1 for model, _n in self.calls if model == v6_model)


def run(answers, *, scene=1, model="NARWHAL_LITE", out_dir=None, fresh_run=True):
    """Один кадр через настоящий generate_image: (результат, провайдер, лог, журнал, папка)."""
    if fresh_run:
        G._SAFETY_FALLBACK_USED.clear()
        G._FRAME_GIVEUP.clear()
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

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
