#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строки опроса v6 называют сцену так же, как остальной журнал (11.10.2026).

Что случилось. «Ultima Scrisoare de pe Front», задача 2193: журнал писал
«🔎 [95] Журнал: заказ gen_0c02… уже создан», а через две минуты —
«❌ [94] v6: провайдер не знает заказ gen_0c02…». Сцена одна, номера разные: опрос
и заказ v6 печатали индекс с нуля, весь остальной журнал — сцену с единицы. При разборе
отказ фильтра сцены 95 читался как отказ сцены 94, а «[21]», «[27]», «[93]» — как сцены,
которых среди пропущенных вовсе нет.

    .venv/bin/python tests/test_v6_log_scene_number.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))
os.environ.setdefault("VS_TEST_NO_NET", "1")

import googler_worker as G                                        # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def poll(answer, index):
    lines = []
    saved = (G.googler_request_v6, G.print_user)
    G.googler_request_v6 = lambda endpoint, method="POST", data=None: dict(answer)
    G.print_user = lambda msg, *a, **k: lines.append(str(msg))
    try:
        out = G.poll_v6_generation("gen_x", index=index, timeout=60)
    finally:
        G.googler_request_v6, G.print_user = saved
    return out, lines


def main() -> int:
    print("Отказ фильтра на опросе — номер сцены, а не индекс")
    out, lines = poll({"status": "failed", "error": "Request blocked by provider safety filters"},
                      index=94)
    check("кадра нет", out, None)
    check("строка про сцену 95", any(l.startswith("❌ [95] v6 generation failed") for l in lines), True)
    check("сцены 94 в журнале нет", any("[94]" in l for l in lines), False)

    print("\nГотовый кадр — тоже сценой")
    out, lines = poll({"status": "succeeded",
                       "results": [{"data": "data:image/png;base64,QUJD"}]}, index=0)
    check("кадр есть", out, "QUJD")
    check("«[1] v6 готово»", any(l.startswith("✅ [1] v6 готово") for l in lines), True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
