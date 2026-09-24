#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дозагрузка названий доноров — параллельно, а не по очереди (пункт 1, фаза 3, 22.09.2026).

Уточнение оркестратора к Algorithm шаг 9 спеки: 5 запросов `donor_title()` за один
заход на открытие канала (`DONOR_TITLE_BACKFILL_CAP`) и до 5 запросов при добавлении
ссылок (`DONOR_TITLE_PARALLEL_LIMIT`) должны идти ОДНОВРЕМЕННО (`asyncio.gather`), а не
по одному в цикле. Если бы это было не так, худший случай добавки к открытию канала
был бы 5 × 6 с (таймаут одного `donor_title()`, `server/pub_tags.py:46`) ≈ 30 с — а не
приблизительно один таймаут, ≈ 6 с, как того требует владелец.

Тест сам не ждёт настоящих 6 секунд (это раздуло бы прогон почти ни за что) — вместо
этого `donor_title` подменена на функцию с УПРАВЛЯЕМОЙ задержкой (`DELAY` секунд на
каждый вызов) и меряется настенное время всего захода:

  - если вызовы идут параллельно — весь заход укладывается в ~DELAY секунд независимо
    от того, 2 там ссылки или 5;
  - если бы вызовы шли по очереди — время выросло бы пропорционально их числу
    (5 × DELAY), и тест это поймает по превышенному порогу.

Тем же приёмом (кратно меньший DELAY, тот же множитель числа кандидатов) можно
экстраполировать вывод и на настоящий 6-секундный таймаут: конструкция, которая
параллелит 5 коротких задержек, параллелит и 5 таймаутов — асинхронный код не знает
разницы между «медленно ответил» и «ответил после большой паузы».

Проверяются оба места, где спека требует параллелизм:

  1. `_fetch_titles`/`ui_links` (POST /ui/links) — при вставке пачки ссылок.
  2. `_backfill_donor_titles`/`ui_channel` (GET /ui/channels/{id}) — при открытии канала.

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_donor_title_parallel_timing.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, cond, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}" + (f"  ({detail})" if detail else ""))
        FAILS.append(label)


DELAY = 1.2   # секунд на один «медленный» вызов donor_title — заведомо меньше боевых 6 с,
              # но с достаточным запасом, чтобы отличить параллельно от по очереди.
N = 5         # столько кандидатов подаём — ровно потолок (CAP/PARALLEL_LIMIT)


def main() -> int:
    work = tempfile.mkdtemp(prefix="donor-title-timing-")
    db_path = os.path.join(work, "vsgen.db")
    os.environ["VS_DEV_DB"] = db_path
    os.environ.setdefault("VS_DEV_TENANT", "dev")

    from pub_schema import ensure_pub_schema                          # noqa: PLC0415
    c0 = sqlite3.connect(db_path)
    c0.row_factory = sqlite3.Row
    ensure_pub_schema(c0)
    c0.commit()
    c0.close()

    import web.app as pult                                            # noqa: PLC0415
    import pub_ui as U                                                # noqa: PLC0415

    def conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    def slow_donor_title(url):
        time.sleep(DELAY)
        return ""                              # «не ответил» — нам важна только задержка

    # ─────────────── 1) POST /ui/links — пачка ссылок тянется одновременно ───────────────
    print(f"1) POST /ui/links: {N} ссылок, каждая имитирует ответ за {DELAY} с")
    pult.donor_title = slow_donor_title
    c = conn()
    cid1 = U.create_channel(c, "dev", "", "Канал (тайминг ссылок)", now=time.time() - 30 * 86400)
    c.close()
    urls = [f"https://youtu.be/slow{i}" for i in range(N)]
    body = pult.LinksIn(channel_id=cid1, links="\n".join(urls), slot=None)

    t0 = time.monotonic()
    asyncio.run(pult.ui_links(body))
    elapsed_links = time.monotonic() - t0
    print(f"     заняло {elapsed_links:.2f} с (было бы ~{N * DELAY:.1f} с по очереди)")
    # Мутация-проверка: замени `asyncio.gather(*(fetch_one(u) for u in urls))` в
    # `_fetch_titles` на последовательный цикл `for u in urls: ... await run_in_threadpool(...)`
    # — эта проверка поймает разницу первой: время вырастет примерно в N раз.
    check_true(f"{N} ссылок обработаны за один-два таймаута, не за N",
               elapsed_links < 2.5 * DELAY,
               f"{elapsed_links:.2f} с >= {2.5 * DELAY:.2f} с — похоже на последовательный обход")
    check_true("и не быстрее одного таймаута (тест не соврал сам себе — задержка правда была)",
               elapsed_links >= DELAY * 0.9, f"{elapsed_links:.2f} с")

    # ────────── 2) GET /ui/channels/{id} — дозагрузка при открытии тоже параллельна ──────────
    print(f"\n2) GET /ui/channels/{{id}}: {N + 2} строк без названия, кап {N}, "
          f"каждый поход имитирует {DELAY} с")
    pult._donor_title_backfill_cooldown.clear()
    c = conn()
    cid2 = U.create_channel(c, "dev", "", "Канал (тайминг открытия)",
                            now=time.time() - 30 * 86400)
    c.execute("UPDATE pub_channels SET schedule=?, tz=? WHERE id=?",
              (json.dumps({"*": ["09:00"]}), "UTC", cid2))
    now = time.time()
    for i in range(N + 2):
        c.execute(
            "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,"
            "created,updated) VALUES(?,?,?,?,?,?)",
            ("dev", cid2, f"https://youtu.be/openslow{i}", now - (i + 1) * 3600, now, now))
    c.commit()
    c.close()

    t0 = time.monotonic()
    d = asyncio.run(pult.ui_channel(cid2, history=1))
    elapsed_open = time.monotonic() - t0
    print(f"     заняло {elapsed_open:.2f} с (было бы ~{N * DELAY:.1f} с по очереди на 5 "
          "штук под капом)")
    # Мутация-проверка: замени `asyncio.gather(...)` в `_backfill_donor_titles` на
    # последовательный `for row in candidates: ... await run_in_threadpool(...)` — эта
    # проверка поймает разницу первой: время откроет канал вырастет пропорционально
    # капу (5 × DELAY вместо ~DELAY), и открытие «зависнет» на секунды дольше, чем
    # обещано владельцу («прибавка не больше ~6 с», уточнение оркестратора).
    check_true("открытие канала (дозагрузка до 5 штук) — тоже за один-два таймаута, "
               "не за пять",
               elapsed_open < 2.5 * DELAY,
               f"{elapsed_open:.2f} с >= {2.5 * DELAY:.2f} с — похоже на последовательный обход")
    check("вернулась цельная лента (без ошибок 500)", isinstance(d.get("calendar"), list), True)

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
