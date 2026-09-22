#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Названия доноров при добавлении ссылок и при открытии канала (пункт 1, фаза 3, 22.09.2026).

Три отдельных контракта, и путать их нельзя:

  1) `POST /ui/links` (`ui_links`) обязана спрашивать НАСТОЯЩЕЕ название донора —
     пулом потоков, ДО открытия базы, тем же приёмом, что и боевая
     `server/pub_ui.py:channel_links` — и класть его в `pub_plan_items.donor_title`
     (AC-7). Раньше сюда всегда передавался `titles={u: "" for u in urls}`, и строка
     плана вставала без названия навсегда — дозагрузки ждать было неоткуда.
  2) `POST /ui/links/preview` (`ui_links_preview`) — «раскладка по датам должна быть
     мгновенной», сети здесь нет и не будет: ноль обращений к `donor_title` (AC-8).
  3) `GET /ui/channels/{id}` (`ui_channel`) лениво дотягивает названия у СТАРЫХ строк
     плана, которые остались без них, — но не больше `DONOR_TITLE_BACKFILL_CAP=5` за
     один заход (AC-12), и не повторяет попытку для строки, которая только что не
     ответила, — кулдаун в памяти процесса (AC-13).

Плюс два замечания ревью-итерации 1 (22.09.2026):

  4) Issue 1 — ни `ui_channel`, ни `ui_links` не смеют выполнять синхронную работу с
     SQLite прямо в цикле событий единого процесса (кабинет+пульт+Rewrite+Cover):
     `_load_channel_calendar_sync`/`_finish_channel_response_sync`/`_write_donor_titles`
     и `_apply_links_sync` уходят в пул потоков (`run_in_threadpool`), а связь чтения не
     остаётся открытой на время ожидания сети.
  5) Issue 4 — просроченные отметки кулдауна дозагрузки выметаются на каждом открытии
     канала (`_prune_backfill_cooldown`), а не копятся в памяти до перезапуска процесса.

Плюс два замечания ревью-итерации 2 (22.09.2026):

  6) Issue 2 — отбор и чистка кулдауна (`_select_backfill_candidates`/
     `_prune_backfill_cooldown`) выполняются на цикле событий, а не в пуле потоков:
     иначе перебор `.items()` в потоке пула мог совпасть по времени с записью в тот же
     словарь из `_backfill_donor_titles_network` другого ПАРАЛЛЕЛЬНОГО открытия канала
     на цикле событий и словить `RuntimeError: dictionary changed size during iteration`.
  7) Issue 3 — `POST /ui/links` проверяет существование канала ДО похода в oembed
     (`_channel_exists_sync`, тот же порядок, что и в кабинетном `channel_links`), а не
     после: несуществующий/чужой `channel_id` не должен оплачивать ни одного похода в
     сеть перед тем, как получить 404.

Сети и стенда не требует: `donor_title` подменена целиком, `requests` не участвует.

    .venv/bin/python tests/test_donor_title_links.py
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


def main() -> int:
    work = tempfile.mkdtemp(prefix="donor-title-links-")
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
    import pub_ui as U                                                 # noqa: PLC0415

    def conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    def add_channel(schedule=None, created=None):
        c = conn()
        try:
            created = created if created is not None else time.time() - 30 * 86400
            cid = U.create_channel(c, "dev", "", f"Канал {cid_seq[0]}", now=created)
            cid_seq[0] += 1
            c.execute("UPDATE pub_channels SET schedule=?, tz=? WHERE id=?",
                      (json.dumps(schedule or {}), "UTC", cid))
            c.commit()
        finally:
            c.close()
        return cid

    cid_seq = [1]

    # ───────────────────────── AC-7: POST /ui/links пишет настоящие названия ─────────────────────────
    print("AC-7: POST /ui/links кладёт в базу названия из donor_title, не пустые строки")
    TITLES = {
        "https://youtu.be/aaa111": "Настоящее название A",
        "https://youtu.be/bbb222": "Настоящее название B",
    }
    calls_links = []

    def fake_donor_title_links(url):
        calls_links.append(url)
        return TITLES.get(url, "")

    pult.donor_title = fake_donor_title_links
    cid1 = add_channel()
    body = pult.LinksIn(channel_id=cid1, links="\n".join(TITLES.keys()), slot=None)
    res = asyncio.run(pult.ui_links(body))
    check("оба URL встали (ни один не пропущен)", sorted(res["skipped"]), [])

    c = conn()
    try:
        rows = {r["donor_url"]: r["donor_title"] for r in c.execute(
            "SELECT donor_url, donor_title FROM pub_plan_items WHERE channel_id=?", (cid1,))}
    finally:
        c.close()
    # Мутация-проверка: верни `titles={u: "" for u in urls}` в `ui_links` (было так до
    # фазы 3) — обе строки ниже начнут падать, донор снова встанет без названия.
    check("донор A — настоящее название, не пусто", rows.get("https://youtu.be/aaa111"),
          "Настоящее название A")
    check("донор B — настоящее название, не пусто", rows.get("https://youtu.be/bbb222"),
          "Настоящее название B")
    check("donor_title спрошен по разу на каждую ссылку", sorted(calls_links),
          sorted(TITLES.keys()))

    print("\n  Провал сети по ОДНОЙ ссылке не топит пачку (Edge Case #2)")
    calls_links.clear()
    cid1b = add_channel()

    def flaky_donor_title(url):
        calls_links.append(url)
        if url.endswith("dead"):
            return ""                       # сеть не ответила — donor_title() сама глотает ошибку
        return "Живой донор"

    pult.donor_title = flaky_donor_title
    body2 = pult.LinksIn(channel_id=cid1b,
                         links="https://youtu.be/deaddead\nhttps://youtu.be/aliveok", slot=None)
    res2 = asyncio.run(pult.ui_links(body2))
    check("обе ссылки встали — провал одного названия не роняет пачку",
          sorted(res2["skipped"]), [])
    c = conn()
    try:
        rows2 = {r["donor_url"]: r["donor_title"] for r in c.execute(
            "SELECT donor_url, donor_title FROM pub_plan_items WHERE channel_id=?", (cid1b,))}
    finally:
        c.close()
    check("мёртвая ссылка встала с пустым названием, не уронив запрос",
          rows2.get("https://youtu.be/deaddead"), "")
    check("живая ссылка рядом — с настоящим названием",
          rows2.get("https://youtu.be/aliveok"), "Живой донор")

    # ─────────────────────── AC-8: POST /ui/links/preview сети не касается ───────────────────────
    print("\nAC-8: POST /ui/links/preview — ноль обращений к donor_title (мгновенный предпросмотр)")
    calls_preview = []

    def counting_donor_title(url):
        calls_preview.append(url)
        return "не должно быть вызвано"

    pult.donor_title = counting_donor_title
    cid2 = add_channel()
    body3 = pult.LinksIn(channel_id=cid2, links="https://youtu.be/ccc333", slot=None)
    preview = pult.ui_links_preview(body3)
    # Мутация-проверка: если бы кто-то добавил в `ui_links_preview` тот же пул
    # потоков, что и в `ui_links` (что спека явно запрещает, FR-10/AC-8), эта
    # проверка поймала бы вызов donor_title там, где сети быть не должно.
    check("donor_title НЕ вызван ни разу", calls_preview, [])
    check("предпросмотр всё равно раскладывает ссылку по датам",
          preview["placed"] or preview["queued"] > 0 or preview["skipped"], True)

    # ────────────── AC-12/AC-13: дозагрузка при открытии канала — кап и кулдаун ──────────────
    print("\nAC-12/AC-13: открытие канала — не больше 5 запросов, кулдаун не долбит мёртвые ссылки")
    cid3 = add_channel(schedule={"*": ["09:00"]})
    now = time.time()
    urls7 = [f"https://youtu.be/backfill{i}" for i in range(7)]
    c = conn()
    try:
        for i, u in enumerate(urls7):
            c.execute(
                "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,"
                "created,updated) VALUES(?,?,?,?,?,?)",
                ("dev", cid3, u, now - (i + 1) * 3600, now, now))
        c.commit()
    finally:
        c.close()

    calls_backfill = []
    pult.donor_title = lambda url: (calls_backfill.append(url), "")[1]   # все «не ответили»
    pult._donor_title_backfill_cooldown.clear()

    d1 = asyncio.run(pult.ui_channel(cid3, history=1))
    first_batch = list(calls_backfill)
    # Мутация-проверка: подними `DONOR_TITLE_BACKFILL_CAP` до 7 (или убери срез по
    # кап-у в `_backfill_donor_titles`) — эта проверка поймает разницу первой: за
    # один заход было бы 7 запросов, не 5.
    check("за один заход — не больше 5 запросов oembed", len(first_batch), 5)
    check("ни один запрос не повторился внутри одного захода",
          len(set(first_batch)), len(first_batch))
    check("все 5 попавших под кап — из наших семи ссылок",
          set(first_batch) <= set(urls7), True)

    calls_backfill.clear()
    d2 = asyncio.run(pult.ui_channel(cid3, history=1))
    second_batch = list(calls_backfill)
    # Мутация-проверка: очисти (или не заводи) `_donor_title_backfill_cooldown` между
    # заходами — вторая проверка поймает разницу первой: без кулдауна второй заход
    # снова спросил бы 5 ссылок, в том числе те же самые пять, что уже не ответили.
    check("второй заход НЕ повторяет только что провалившиеся 5 ссылок",
          set(second_batch) & set(first_batch), set())
    check("второй заход дотягивает ровно оставшиеся (не более 5, и не 0)",
          len(second_batch), min(5, len(urls7) - len(first_batch)))

    calls_backfill.clear()
    d3 = asyncio.run(pult.ui_channel(cid3, history=1))
    check("третий заход — все семь уже либо получили название, либо в кулдауне: "
          "новых запросов нет", calls_backfill, [])

    for d in (d1, d2, d3):
        for row in d["calendar"]:
            check("наружу '_plan_item_id' не просачивается", "_plan_item_id" in row, False)

    # ────────────── Issue 1 ревью (22.09.2026): цикл событий не стоит на SQLite ──────────────
    print("\nIssue 1: ни `ui_channel`, ни `ui_links` не держат цикл событий на синхронной "
          "работе с SQLite")

    async def heartbeat(duration, interval=0.04):
        """Считает, сколько раз событийный цикл успел освободиться за `duration` секунд.

        Если где-то внутри проверяемого вызова синхронный код (SQLite, `time.sleep`)
        выполняется прямо в цикле событий, а не в пуле потоков, этот `await
        asyncio.sleep(...)` просто не получит своей очереди, пока тот код не закончит
        — тиков наберётся заметно меньше, чем `duration / interval`.
        """
        ticks = 0
        end = time.monotonic() + duration
        while time.monotonic() < end:
            await asyncio.sleep(interval)
            ticks += 1
        return ticks

    async def run_with_canary(coro, head_start=0.1, interval=0.02):
        """Строгая версия `heartbeat` (уточнение оркестратора к Issue 1, ревью-цикл 2):
        сама `heartbeat` выше уязвима к порядку запуска `asyncio.gather` — если
        проверяемый вызов блокирует поток СИНХРОННО ДО своего первого `await`, `gather`
        может так и не дать сопрограмме-«канарейке» выполнить даже первую строчку (её
        собственный отсчёт `end = time.monotonic() + duration` тогда стартует уже ПОСЛЕ
        блокировки, и окно замера целиком уезжает за её пределы — блокировка остаётся
        незамеченной, хотя произошла).

        Здесь канарейка запускается ОТДЕЛЬНОЙ задачей (`asyncio.ensure_future`) и получает
        `head_start` секунд, чтобы несколько раз тикнуть и поставить в цикл событий
        ОЧЕРЕДНОЙ таймер ДО того, как проверяемая корутина хотя бы начнёт выполняться.
        Дальше меряется не число тиков, а МАКСИМАЛЬНЫЙ разрыв между соседними тиками —
        если где-то внутри проверяемого вызова живёт синхронный SQLite/`time.sleep`
        прямо в цикле событий, уже ожидающий таймер канарейки не сработает, пока тот
        код не закончит, и разрыв станет заметно больше `interval`.
        """
        ticks_ts = []
        stop = asyncio.Event()

        async def canary():
            while not stop.is_set():
                await asyncio.sleep(interval)
                ticks_ts.append(time.monotonic())

        task = asyncio.ensure_future(canary())
        await asyncio.sleep(head_start)
        result = await coro
        stop.set()
        await task
        gaps = [b - a for a, b in zip(ticks_ts, ticks_ts[1:])]
        return result, (max(gaps) if gaps else 0.0)

    DELAY_IO = 0.3   # имитация «медленного диска»/блокировки — заведомо короче боевых 5 с

    print("\n  1a) ui_channel: SQLite в трёх шагах (Stage A / запись / Stage C)")
    real_db = pult.db

    def slow_db():
        time.sleep(DELAY_IO)                        # имитация ожидания блокировки sqlite3
        return real_db()

    pult.db = slow_db
    pult.donor_title = lambda url: ""               # эта часть теста не про сеть
    pult._donor_title_backfill_cooldown.clear()
    cid5 = add_channel(schedule={"*": ["09:00"]})

    async def run_ui_channel_with_heartbeat():
        return await asyncio.gather(pult.ui_channel(cid5, history=1),
                                    heartbeat(DELAY_IO * 2 + 0.2))

    _d5, ticks_channel = asyncio.run(run_ui_channel_with_heartbeat())
    pult.db = real_db
    # Мутация-проверка: верни `ui_channel` к одной связи `c = db()` на всю функцию,
    # открытой ДО дозагрузки и используемой после (старое поведение) — эта проверка
    # поймает разницу первой: «медленный» `db()` встанет прямо в цикле событий, тиков
    # наберётся в разы меньше.
    check_ge = lambda label, got, minimum: check(label, got >= minimum, True)
    check_ge("ui_channel: событийный цикл тикал, пока SQLite «ждал диск»",
             ticks_channel, 5)

    print("\n  1a-strict) ui_channel: та же проверка канарейкой с головным стартом — не "
          "зависит от порядка запуска asyncio.gather (см. docstring run_with_canary)")
    pult.db = slow_db
    pult._donor_title_backfill_cooldown.clear()
    cid5b = add_channel(schedule={"*": ["09:00"]})
    _d5b, gap_channel = asyncio.run(run_with_canary(pult.ui_channel(cid5b, history=1)))
    pult.db = real_db
    # Мутация-проверка: верни `ui_channel` к прямым синхронным вызовам
    # `_load_channel_calendar_sync(...)`/`_finish_channel_response_sync(...)` вместо
    # `await run_in_threadpool(...)` — широкая проверка выше (`ticks_channel`) её не
    # ловит (её собственное окно heartbeat стартует уже ПОСЛЕ синхронного участка,
    # который в мутации целиком проходит ДО первого `await` внутри `ui_channel`); эта
    # проверка ловит её первой: канарейка была запущена и тикала ДО старта `ui_channel`,
    # и разрыв между её тиками вырастет примерно до `DELAY_IO`.
    check_true("канарейка не застревает дольше половины DELAY_IO во время ui_channel "
               "(строгая проверка Issue 1)", gap_channel < DELAY_IO * 0.5,
               f"наибольший разрыв между тиками канарейки: {gap_channel:.3f} с "
               f">= {DELAY_IO * 0.5:.3f} с")

    print("\n  1b) ui_links: то же для add_links()+commit() (уточнение оркестратора "
          "к Issue 1)")
    calls_apply = []
    real_apply = pult._apply_links_sync

    def slow_apply(*a, **kw):
        time.sleep(DELAY_IO)
        calls_apply.append(True)
        return real_apply(*a, **kw)

    pult._apply_links_sync = slow_apply
    cid6 = add_channel()
    body6 = pult.LinksIn(channel_id=cid6, links="https://youtu.be/eventloop6", slot=None)

    async def run_ui_links_with_heartbeat():
        return await asyncio.gather(pult.ui_links(body6), heartbeat(DELAY_IO + 0.2))

    _res6, ticks_links = asyncio.run(run_ui_links_with_heartbeat())
    pult._apply_links_sync = real_apply
    check("ui_links: add_links()/commit() правда были вызваны", calls_apply, [True])
    # Мутация-проверка: в `ui_links` замени `await run_in_threadpool(_apply_links_sync, …)`
    # на прямой синхронный вызов `_apply_links_sync(...)` — эта проверка поймает разницу
    # первой: `time.sleep(DELAY_IO)` внутри встанет прямо в цикле событий, и `heartbeat`
    # не наберёт тиков, пока запись не закончится.
    check_ge("ui_links: событийный цикл тикал, пока add_links()/commit() «ждали диск»",
             ticks_links, 4)

    print("\n  1b-strict) ui_links: та же проверка канарейкой с головным стартом")
    pult._apply_links_sync = slow_apply
    calls_apply.clear()
    cid6b = add_channel()
    body6b = pult.LinksIn(channel_id=cid6b, links="https://youtu.be/eventloop6b", slot=None)
    _res6b, gap_links = asyncio.run(run_with_canary(pult.ui_links(body6b)))
    pult._apply_links_sync = real_apply
    check("ui_links (строгая проверка): add_links()/commit() правда были вызваны",
          calls_apply, [True])
    # Мутация-проверка: в `ui_links` замени `await run_in_threadpool(_apply_links_sync, …)`
    # на прямой синхронный вызов `_apply_links_sync(...)` — эта проверка ловит её первой
    # тем же приёмом, что и `1a-strict` выше.
    check_true("канарейка не застревает дольше половины DELAY_IO во время ui_links "
               "(строгая проверка Issue 1)", gap_links < DELAY_IO * 0.5,
               f"наибольший разрыв между тиками канарейки: {gap_links:.3f} с "
               f">= {DELAY_IO * 0.5:.3f} с")

    print("\n  1c) ui_channel: связь с базой не остаётся открытой, пока идёт ожидание сети")
    import threading

    open_conns = []
    lock = threading.Lock()

    class _TrackedConn:
        def __init__(self, real):
            self._real = real
            with lock:
                open_conns.append(self)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):
            with lock:
                if self in open_conns:
                    open_conns.remove(self)
            self._real.close()

    def tracked_db():
        return _TrackedConn(real_db())

    overlap = {"seen": False}

    def slow_network_donor_title(url):
        with lock:
            if open_conns:
                overlap["seen"] = True
        time.sleep(0.2)
        with lock:
            if open_conns:
                overlap["seen"] = True
        return "Название пришло из сети"

    pult.db = tracked_db
    pult.donor_title = slow_network_donor_title
    pult._donor_title_backfill_cooldown.clear()
    cid7 = add_channel(schedule={"*": ["09:00"]})
    c = conn()
    try:
        now7 = time.time()
        c.execute(
            "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,"
            "created,updated) VALUES(?,?,?,?,?,?)",
            ("dev", cid7, "https://youtu.be/eventloop7", now7 - 3600, now7, now7))
        c.commit()
    finally:
        c.close()
    asyncio.run(pult.ui_channel(cid7, history=1))
    pult.db = real_db
    # Мутация-проверка: верни старую `_backfill_donor_titles(c, cal)`, которой передавали
    # уже открытую связь чтения, и вызови её ДО закрытия `c` в `ui_channel` — эта
    # проверка поймает разницу первой: `open_conns` будет непусто в момент, когда
    # `slow_network_donor_title` «ждёт сеть».
    check("ни одна связь SQLite не остаётся открытой на время ожидания сети (Issue 1)",
          overlap["seen"], False)

    # ────────────────── Issue 4 ревью (22.09.2026): кулдаун не растёт бесконечно ──────────────────
    print("\nIssue 4: просроченные отметки кулдауна выметаются, а не копятся до рестарта")
    pult._donor_title_backfill_cooldown.clear()
    stale_pid, fresh_pid = 900001, 900002
    now8 = time.time()
    pult._donor_title_backfill_cooldown[stale_pid] = (
        now8 - pult.DONOR_TITLE_BACKFILL_COOLDOWN_SEC - 10)   # старше своего же окна
    pult._donor_title_backfill_cooldown[fresh_pid] = now8       # только что провалилась
    # Строка, у которой могла бы быть просроченная отметка, но её самой уже нет в `cal`
    # (утекла из видимой ленты/удалена) — именно тот случай, который раньше не давал
    # повода почистить словарь вовсе.
    pult._select_backfill_candidates([])
    # Мутация-проверка: убери вызов `_prune_backfill_cooldown` из
    # `_select_backfill_candidates` (`web/app.py`) — эта проверка поймает разницу первой:
    # просроченная отметка `stale_pid` останется в словаре.
    check("просроченная отметка выметена", stale_pid in pult._donor_title_backfill_cooldown,
          False)
    check("свежая отметка (внутри окна) осталась", fresh_pid in pult._donor_title_backfill_cooldown,
          True)

    # ─────── Issue 2 ревью-итерация 2 (22.09.2026): кулдаун не обходится из пула потоков ───────
    print("\nIssue 2 (итерация 2): отбор/чистка кулдауна выполняются на цикле событий, "
          "не в пуле потоков — иначе `_backfill_donor_titles_network` другого "
          "ПАРАЛЛЕЛЬНОГО открытия канала могла бы менять тот же словарь из другого потока "
          "прямо во время перебора `.items()` в `_prune_backfill_cooldown`")
    import threading                                                   # noqa: PLC0415
    main_thread = threading.current_thread()
    prune_threads = []
    real_prune = pult._prune_backfill_cooldown

    def wrapped_prune(now):
        prune_threads.append(threading.current_thread())
        return real_prune(now)

    pult._prune_backfill_cooldown = wrapped_prune
    pult.donor_title = lambda url: ""
    pult._donor_title_backfill_cooldown.clear()
    cid9 = add_channel(schedule={"*": ["09:00"]})
    asyncio.run(pult.ui_channel(cid9, history=1))
    pult._prune_backfill_cooldown = real_prune
    # Мутация-проверка: верни отбор кандидатов (`_select_backfill_candidates`) внутрь
    # `_load_channel_calendar_sync`, которая выполняется через `run_in_threadpool`
    # (старое поведение до итерации 2) — эта проверка поймает разницу первой:
    # `_prune_backfill_cooldown` окажется вызван из потока пула потоков, а не из
    # главного потока, где крутится `asyncio.run`.
    check("отбор кандидатов вызван хотя бы раз", len(prune_threads) >= 1, True)
    check("кулдаун-словарь чистится на том же потоке, что и цикл событий "
          "(не в пуле потоков)", all(t is main_thread for t in prune_threads), True)

    # ────── Issue 3 ревью-итерация 2 (22.09.2026): канал проверяется ДО похода в сеть ──────
    print("\nIssue 3 (итерация 2): POST /ui/links на несуществующий канал — 404 БЕЗ единого "
          "похода в donor_title")
    calls_issue3 = []

    def counting_donor_title_issue3(url):
        calls_issue3.append(url)
        return "не должно быть вызвано"

    pult.donor_title = counting_donor_title_issue3
    missing_cid = 999999                                     # заведомо не существует
    body_missing = pult.LinksIn(channel_id=missing_cid,
                                links="https://youtu.be/ghostchannel", slot=None)
    raised = None
    try:
        asyncio.run(pult.ui_links(body_missing))
    except pult.HTTPException as exc:
        raised = exc
    check_true("POST /ui/links на несуществующий канал бросает HTTPException(404)",
               raised is not None and raised.status_code == 404,
               f"{raised!r}")
    # Мутация-проверка: убери вызов `_channel_exists_sync` (или проверку `if not
    # await run_in_threadpool(...)`) из `ui_links`, вернув старый порядок «сеть →
    # потом проверка канала внутри `_apply_links_sync`» — эта проверка поймает разницу
    # первой: `donor_title` будет вызван хотя бы раз ДО того, как всплывёт 404.
    check("ни одного похода в donor_title для несуществующего канала",
          calls_issue3, [])

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
