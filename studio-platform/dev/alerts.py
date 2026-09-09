#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тревоги: посмотреть, проверить связь, гонять по кругу.

Утром порядок такой:

    1. @BotFather → /newbot → токен, положить в .env: VS_TG_TOKEN=...
    2. открыть боту дорогу — смотря куда слать:
         личный чат — написать боту любое сообщение;
         группа     — добавить бота и отправить туда /start;
         канал      — сделать бота АДМИНИСТРАТОРОМ с правом публикации.
       Найти своего бота проще всего через @BotFather → /mybots: там ссылка t.me.
    3. .venv/bin/python dev/alerts.py --whoami     ← покажет адрес
    4. дописать в .env:        VS_TG_CHAT=...
    5. .venv/bin/python dev/alerts.py --test       ← придёт сообщение

Для публичного канала числовой id не нужен: годится VS_TG_CHAT=@имя_канала.

Дальше:

    dev/alerts.py            что сейчас горит, ничего не отправляя
    dev/alerts.py --once     один проход с отправкой
    dev/alerts.py --loop     проход каждую минуту
    dev/alerts.py --state    что помнит система: когда сказали, о чём молчим
    dev/alerts.py --reset    забыть память тревог (следующая авария прозвучит заново)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_env import load_env, mask                                # noqa: E402

load_env()

import pub_alerts as A                                            # noqa: E402
from pub_alerts_tg import Telegram                                # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT") or None


def db():
    if not os.path.exists(DB):
        raise SystemExit(f"нет базы {DB} — сначала: python3 dev/make_db.py")
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def show(c, now):
    found = A.detect(c, now, TENANT)
    state = A.load_state(c)
    print(f"порогов: очередь {A.STALL_SEC // 60} мин · пауза провайдера "
          f"{A.RESTING_SEC // 60} мин · заливка {A.UPLOAD_SEC // 3600} ч · "
          f"ожидание {A.OLDEST_SEC // 3600} ч · диск {A.DISK_MIN_GB:.0f} ГБ · "
          f"повтор раз в {A.REPEAT_SEC // 60} мин")
    if not found:
        print("\nвсё спокойно: ни один из восьми поводов не выполнен")
    for key, a in sorted(found.items()):
        st = state.get(key) or {}
        said = ("сказано " + time.strftime("%H:%M", time.localtime(st["sent"]))
                if st.get("sent") else "ещё не сказано")
        print(f"\n● {a.title}   [{key}]   {said}")
        for line in a.text.splitlines():
            print(f"    {line}")
        if a.money:
            print(f"    деньги: {a.money}")
    silent = [k for k in state if k not in found]
    if silent:
        print(f"\nв памяти висят снятые поводы (скажем «починилось»): {', '.join(silent)}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--whoami", action="store_true", help="кто бот и какие есть chat_id")
    p.add_argument("--test", action="store_true", help="отправить пробное сообщение")
    p.add_argument("--once", action="store_true", help="один проход с отправкой")
    p.add_argument("--loop", action="store_true", help="проход каждые --every секунд")
    p.add_argument("--every", type=int, default=60)
    p.add_argument("--state", action="store_true", help="показать память тревог")
    p.add_argument("--reset", action="store_true", help="очистить память тревог")
    args = p.parse_args()

    tg = Telegram()
    if args.whoami:
        info = tg.whoami()
        if info["error"]:
            print(f"не вышло: {info['error']}")
            return 1
        me = info["me"] or {}
        print(f"бот: @{me.get('username') or '?'}  ({me.get('first_name') or ''})")
        if not info["chats"]:
            print("\nПока ничего не видно. Что сделать — смотря куда слать:")
            print("  личный чат — написать боту любое сообщение;")
            print("  группа     — добавить бота и отправить туда /start"
                  " (обычный текст он может не увидеть);")
            print("  канал      — сделать бота АДМИНИСТРАТОРОМ с правом публикации.")
            print("\nПотом повторить эту команду.")
            print("\nЕсли всё сделано, а список пуст: у бота задан webhook — тогда "
                  "getUpdates всегда пустой.")
            print("Снять: https://api.telegram.org/bot<ТОКЕН>/deleteWebhook")
            print("Ещё: Telegram хранит апдейты около суток — если писали давно, "
                  "напишите заново.")
            return 1
        print("\nдоступные адреса:")
        for ch in info["chats"]:
            kind = {"private": "личный чат", "group": "группа",
                    "supergroup": "группа", "channel": "канал"}.get(ch["type"],
                                                                    ch["type"])
            print(f"  VS_TG_CHAT={ch['id']:<16} {kind:<12} {ch['title']}")
            if ch.get("as_name"):
                print(f"  VS_TG_CHAT={ch['as_name']:<16} {'то же самое, по имени':<12} "
                      f"(устойчивее числа и читается)")
        return 0

    if args.test:
        print(f"токен {mask(tg.token)} · чат {tg.chat or '—'}")
        if not tg.enabled:
            print("не настроено: нужны VS_TG_TOKEN и VS_TG_CHAT в .env")
            return 1
        ok = tg.send("Проверка связи · Studio-платформа\n\n"
                     "Это сообщение отправлено вручную: dev/alerts.py --test\n"
                     "Тревоги приходят сюда же.")
        print("отправлено" if ok else "не отправлено — см. сообщение выше")
        return 0 if ok else 1

    c = db()
    try:
        if args.reset:
            A.save_state(c, {})
            print("память тревог очищена")
            return 0
        if args.state:
            state = A.load_state(c)
            if not state:
                print("память пуста: ни одной активной тревоги")
            for k, st in sorted(state.items()):
                since = time.strftime("%d.%m %H:%M", time.localtime(st.get("since") or 0))
                sent = (time.strftime("%d.%m %H:%M", time.localtime(st["sent"]))
                        if st.get("sent") else "—")
                print(f"  {k:<28} с {since}   сказано {sent}   раз: {st.get('n') or 0}")
            return 0

        if not (args.once or args.loop):
            show(c, time.time())
            print("\n(ничего не отправлено — это просмотр; отправка: --once или --loop)")
            return 0

        if not tg.enabled:
            print("телеграм не настроен: тревоги будут определяться, но не отправляться")
        while True:
            sent = A.tick(c, tg.send, tenant=TENANT)
            stamp = time.strftime("%H:%M:%S")
            if sent:
                for key, kind, _text in sent:
                    print(f"{stamp}  {kind:<6} {key}")
            else:
                print(f"{stamp}  тихо")
            if not args.loop:
                return 0
            time.sleep(max(5, args.every))
    finally:
        c.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
