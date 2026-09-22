#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож живости выходов субтитров (19.09.2026).

Дефект. Ротация выходов знала про выход ровно одно: ответил он на рабочий запрос или
нет. Полумёртвый туннель — порт слушает, запросы не проходят — от живого этим не
отличается, он отличается только тем, что каждый заказ сначала ждёт его таймаут.
Именно так у владельца стоит `127.0.0.1:1080`, и узнать это можно было только руками.

Проверяем сквозную проверку выхода: стук в НЕЙТРАЛЬНЫЙ адрес (не в YouTube — иначе
проверка жгла бы ту же квоту и готовила бан тем выходам, живость которых проверяет),
запись «жив/мёртв, когда отвечал, чем ответил», вывод мёртвого из ротации и его
самостоятельное возвращение. И три предохранителя: проверка не задерживает заказ,
не ходит чаще, чем нужно, и НЕ останавливает работу, если «мёртвыми» вышли все.

    .venv/bin/python tests/test_subs_exit_watch.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_remote as PR                                            # noqa: E402

FAILS = []
TMP = tempfile.mkdtemp(prefix="subs-watch-")
DB = os.path.join(TMP, "vsgen.db")

ALIVE = "socks5h://127.0.0.1:1081"
DEAD = "socks5h://127.0.0.1:1080"          # тот самый мёртвый туннель владельца
SECRET = "socks5h://ads:s3cret@10.0.0.7:1080"      # прокси профиля AdsPower с паролем


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Resp:
    def __init__(self, status=200, text="fl=1\nip=203.0.113.9\nts=1\n"):
        self.status_code = status
        self.text = text


class Net:
    """Сеть понарошку: кто из выходов отвечает, решает `dead`."""

    def __init__(self):
        self.dead = set()
        self.seen = []
        self.status = 200
        self.slow = 0.0

    def install(self):
        self.saved = requests.Session.request
        net = self

        def spy(session, method, url, **kw):
            proxy = (session.proxies or {}).get("https", "")
            net.seen.append({"url": url, "proxy": proxy, "timeout": kw.get("timeout"),
                             "thread": threading.current_thread().name,
                             "cookies": len(session.cookies)})
            if net.slow:
                time.sleep(net.slow)
            if proxy in net.dead:
                raise requests.exceptions.ConnectTimeout("туннель не отвечает")
            return Resp(net.status)

        requests.Session.request = spy

    def remove(self):
        requests.Session.request = self.saved


NET = Net()          # одна подменённая сеть на весь прогон: спрятанных вторых нет


def reset(exits, dead=()):
    """Чистый стенд: свой список выходов, пустая память и пустая база сторожа.

    Фоновый обход мог ещё идти — дожидаемся его, иначе он допишет свои стуки в
    следующую проверку, и та начнёт мерить не то, что думает.
    """
    wait_probe()
    PR.SUBS_PROXIES = list(exits)
    PR._health.clear()
    PR._health_loaded = False
    PR._subs_rest_until.clear()
    NET.dead = set(dead)
    NET.seen.clear()
    NET.slow = 0.0
    if os.path.exists(DB):
        os.remove(DB)


def wait_probe(limit=5.0) -> bool:
    end = time.time() + limit
    while time.time() < end:
        if not PR._probe_running:
            return True
        time.sleep(0.01)
    return False


class Track:
    """Автодорожка донора — минимум, который читает `_pick_track`."""

    language_code = "en"
    is_generated = True

    def fetch(self):
        return [{"text": "текст донора "} for _ in range(40)]


class FakeApi:
    def __init__(self, http_client):
        self.http = http_client

    def list(self, vid):
        return [Track()]


COOKIES = os.path.join(TMP, "cookies.txt")


def make_cookies() -> None:
    """Настоящий файл cookies владельца: без него проверка «cookies не уехали»
    проходила бы сама собой — брать было бы нечего."""
    with open(COOKIES, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(".youtube.com\tTRUE\t/\tFALSE\t2147483647\tSID\tсессия-владельца\n")
    PR.SUBS_COOKIES = COOKIES


def main() -> int:
    os.environ["VS_DB"] = DB                 # стенд работает на своей копии базы
    make_cookies()

    print("Проверка идёт в нейтральный адрес, а не в YouTube")
    check("адрес проверки не на YouTube",
          any(h in PR.PROBE_URL for h in ("youtube.com", "youtu.be", "googlevideo")), False)
    NET.install()
    net = NET
    try:
        reset([ALIVE, DEAD], dead=[DEAD])
        summary = PR.subs_probe_all(force=True)
        check("проверены оба выхода", summary["checked"], 2)
        check("живой найден", summary["alive"], 1)
        check("мёртвых пока нет — один промах не приговор", summary["dead"], 0)
        check("стучались только в адрес проверки",
              {s["url"] for s in net.seen}, {PR.PROBE_URL})
        check("у проверки свой короткий таймаут",
              {s["timeout"] for s in net.seen}, {PR.PROBE_TIMEOUT})
        check("рабочая сессия cookies владельца берёт",
              len(PR._subs_session(ALIVE).cookies), 1)
        check("а проверочная — нет", len(PR._probe_session(ALIVE).cookies), 0)
        check("cookies владельца в чужой адрес не уехали",
              {s["cookies"] for s in net.seen}, {0})

        print("\nВыход помечается мёртвым не с первого промаха")
        check("после одного промаха выход ещё в ротации", DEAD in PR._subs_live_exits(), True)
        PR.subs_probe_all(force=True)
        check("после второго — мёртв", sorted(PR._subs_dead_exits()), [DEAD])
        check("мёртвый выведен из ротации", PR._subs_live_exits(), [ALIVE])

        print("\nТаблица помнит, жив ли выход, когда отвечал и чем ответил")
        conn = sqlite3.connect(DB)
        rows = {r[0]: r for r in conn.execute(
            "SELECT exit_url,alive,fails,checked,ok_at,ms,ip,error FROM subs_exit_health")}
        conn.close()
        check("строка на каждый выход", sorted(rows), sorted([ALIVE, DEAD]))
        check("живой записан живым", rows[ALIVE][1], 1)
        check("у живого известно время последнего ответа", rows[ALIVE][4] > 0, True)
        check("у живого записан внешний адрес", rows[ALIVE][6], "203.0.113.9")
        check("мёртвый записан мёртвым", rows[DEAD][1], 0)
        check("у мёртвого сохранена последняя ошибка",
              "ConnectTimeout" in rows[DEAD][7], True)
        check("у мёртвого нет времени ответа", rows[DEAD][4], 0.0)

        print("\nОживший выход возвращается сам")
        net.dead.clear()
        PR.subs_probe_all(force=True)
        check("мёртвых не осталось", sorted(PR._subs_dead_exits()), [])
        check("выход снова в ротации", sorted(PR._subs_live_exits()), sorted([ALIVE, DEAD]))

        print("\nЕсли «мёртвыми» вышли все — проверке не верим, работа идёт")
        net.dead.update([ALIVE, DEAD])
        PR.subs_probe_all(force=True)
        PR.subs_probe_all(force=True)
        check("сторож считает мёртвыми обоих", len(PR._subs_dead_exits()), 2)
        check("ротация всё равно не пуста",
              sorted(PR._subs_live_exits()), sorted([ALIVE, DEAD]))
        state = PR.subs_exits_state()
        check("плитка честно говорит, что проверке верить нельзя", state["trusted"], False)
        check("и красным", state["kind"], "err")

        print("\nВыходы с одним внешним адресом видно")
        # У владельца в списке и туннели, и прокси профилей: два выхода с общим адресом
        # делят квоту и бан, то есть вторым выходом только притворяются.
        reset([ALIVE, DEAD])
        PR.subs_probe_all(force=True)     # оба отвечают, и оба — с одного адреса
        state = PR.subs_exits_state()
        check("общий внешний адрес назван", sorted(state["shared_ip"]), ["203.0.113.9"])
        check("и указаны оба выхода", sorted(state["shared_ip"].get("203.0.113.9", [])),
              sorted([ALIVE, DEAD]))
        check("каждый выход помечен", [e["ip_shared"] for e in state["exits"]], [True, True])

        print("\nСведения без свежести на ротацию не влияют")
        far = time.time() + PR.PROBE_STALE + 10
        check("протухшее «мёртв» забыто", sorted(PR._subs_dead_exits(now=far)), [])
        reset([ALIVE, DEAD], dead=[ALIVE, DEAD])   # мертвы оба, но ни одной проверки
        check("невиданный выход мёртвым не считается", sorted(PR._subs_dead_exits()), [])
        check("и остаётся в ротации", sorted(PR._subs_live_exits()), sorted([ALIVE, DEAD]))

        print("\nПроверка не ходит чаще, чем нужно")
        check("фоновый обход прошлой проверки закончился", wait_probe(), True)
        net.seen.clear()
        net.dead.clear()
        PR.subs_probe_all(force=True)
        было = len(net.seen)
        second = PR.subs_probe_all()
        check("первый обход состоялся", было, 2)
        check("сразу следом никто не опрашивается", second["checked"], 0)
        check("и в сеть не ходили", len(net.seen), было)
        check("и будить сторожа незачем", PR.subs_probe_due(), False)

        print("\nУдачная выдача субтитров засчитывается как проверка")
        # Единственный выход признан мёртвым: заказ всё равно идёт (верить проверке,
        # когда мертвы все, нельзя) — и его успех сам снимает приговор.
        reset([ALIVE], dead=[ALIVE])
        PR.subs_probe_all(force=True)
        PR.subs_probe_all(force=True)
        check("выход считается мёртвым", sorted(PR._subs_dead_exits()), [ALIVE])
        saved_api = PR._transcript_api
        PR._transcript_api = lambda proxy: FakeApi(PR._subs_session(proxy))
        try:
            text = PR.donor_text("https://youtu.be/abcdefghijk")
        finally:
            PR._transcript_api = saved_api
        check("субтитры получены", len(text) > 200, True)
        check("после настоящей выдачи выход снова живой", sorted(PR._subs_dead_exits()), [])

        print("\nОтдых и смерть — разные вещи")
        reset([ALIVE, DEAD])
        PR.subs_probe_all(force=True)
        PR._subs_rest(ALIVE)                 # исчерпана квота адреса, но выход жив
        check("отдыхающий выход не мёртв", ALIVE in PR._subs_dead_exits(), False)
        check("но из ротации ушёл", PR._subs_live_exits(), [DEAD])
        tile = {e["name"]: e for e in PR.subs_exits_state()["exits"]}
        check("плитка называет это отдыхом", tile[ALIVE]["state"], "отдыхает")
        check("и показывает, сколько ждать", tile[ALIVE]["rest_min"] > 0, True)
        PR._subs_rest_until.clear()

        print("\nЗаказ не ждёт проверку")
        net.seen.clear()
        PR._health.clear()                   # сведений нет — сторожу есть что делать
        check("сторож считает, что пора", PR.subs_probe_due(), True)
        net.slow = 0.3
        started = time.time()
        got = PR._subs_live_exits()
        spent = time.time() - started
        check("список выходов отдан сразу", spent < 0.2, True)
        check("и он полный, пока проверка не закончилась",
              sorted(got), sorted([ALIVE, DEAD]))
        check("обход запущен", PR._probe_running, True)
        check("сторож дошёл до конца", wait_probe(), True)
        check("проверка шла не в потоке заказа",
              {s["thread"] for s in net.seen}, {"subs-probe"})
        net.slow = 0.0

        print("\nПароль прокси не утекает ни в базу, ни на экран")
        reset([SECRET], dead=[SECRET])
        PR.subs_probe_all(force=True)
        PR.subs_probe_all(force=True)
        conn = sqlite3.connect(DB)
        saved = conn.execute("SELECT error FROM subs_exit_health").fetchall()
        conn.close()
        # Без этой проверки предыдущая ничего не значила бы: пустая ошибка пароля
        # тоже «не содержит».
        check("ошибка выхода записана", [("ConnectTimeout" in (r[0] or "")) for r in saved],
              [True])
        check("в тексте ошибки пароля нет", any("s3cret" in (r[0] or "") for r in saved), False)
        shown = PR.subs_exits_state()
        check("на плитке пароля нет", "s3cret" in repr(shown), False)
        check("но выход узнаваем", "10.0.0.7:1080" in shown["exits"][0]["name"], True)

        print("\nПосле рестарта сторож помнит, что знал")
        PR._health.clear()
        PR._health_loaded = False            # процесс кабинета поднялся заново
        check("мёртвый выход опознан по базе, без новой проверки",
              sorted(PR._subs_dead_exits()), [SECRET])
        check("и новых стуков в сеть для этого не потребовалось", len(net.seen), 2)

        print("\nПульт читает состояние своим соединением")
        outside = sqlite3.connect(DB)
        PR._health.clear()                   # как будто пульт — отдельный процесс
        PR._health_loaded = True
        state = PR.subs_exits_state(c=outside)
        outside.close()
        check("состояние видно из чужого соединения", state["dead"], 1)
        check("и понятной строкой", state["text"], "выход не отвечает на проверку")

        print("\nСторож пишет в ту же базу, что и кабинет")
        # Стенд задаёт базу через VS_SERVER_DB (dev/run.sh), кабинет читает её же
        # (main.py:21). Промахнуться мимо неё — значит писать сторожа в один файл,
        # а показывать пульту другой.
        alt = os.path.join(TMP, "stand.db")
        was = os.environ.pop("VS_DB")
        os.environ["VS_SERVER_DB"] = alt
        try:
            try:
                conn = PR._cabinet_db()
                where = conn.execute("PRAGMA database_list").fetchall()[0][2]
                conn.close()
            except Exception as exc:        # промах мимо базы стенда — это провал
                where = f"база не открылась: {type(exc).__name__}"
            # realpath: на macOS /var — символическая ссылка на /private/var
            check("без VS_DB берётся база стенда", os.path.realpath(where),
                  os.path.realpath(alt))
        finally:
            os.environ.pop("VS_SERVER_DB", None)
            os.environ["VS_DB"] = was
        conn = PR._cabinet_db()
        where = conn.execute("PRAGMA database_list").fetchall()[0][2]
        conn.close()
        check("VS_DB остаётся сильнее", os.path.realpath(where), os.path.realpath(DB))

        print("\nБез таблицы плитка не падает")
        os.remove(DB)
        empty = sqlite3.connect(DB)
        state = PR.subs_exits_state(c=empty)
        empty.close()
        check("сторож ещё не работал", state["kind"], "none")
        check("и это сказано словами", state["text"], "проверки ещё не было")
    finally:
        net.remove()

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
