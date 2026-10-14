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

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_env import load_env, mask                                # noqa: E402

load_env()

import pub_alerts as A                                            # noqa: E402
import pub_digest as D                                            # noqa: E402
import pub_earn as E                                              # noqa: E402
from pub_alerts_tg import Telegram                                # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT") or None

_FG_USAGE_URL = "https://googler.fast-gen.ai/api/v6/usage"


def refresh_fastgen_usage(c, now) -> None:
    """Свежий снимок расхода ключа FastGen перед каждым тиком (пункт 8 ТЗ, батч
    16.09.2026) — без него поводы §9/§10 (`pub_alerts.check_key_expiring`,
    `check_usage_near_limit`) никогда бы не увидели актуальные цифры, если в кабинет
    никто не заходит. Сбой сети не роняет проход: снимок просто не обновится, а
    правило «устаревший снимок не повод» не даст соврать по старым цифрам.

    Здесь же — единственное место, где слышно ОТКАЗ провайдера по ключу: §11
    (`pub_alerts.check_key_dead`) сам в сеть не ходит и читает то, что запишет
    `note_key_refused`. Запрос бесплатный (чтение статистики, 0 кредитов): проверять
    ключ генерацией нельзя — один кадр FastGen стоит денег.

    Ключ читается через `main.keys()` + `main._first_credential(..., "googler")` —
    ТОЧНО тот же разбор, что и в кабинете (скаляр, список, `googler_pool`; путь —
    `main.KEYS_PATH`, тот же `VS_SERVER_KEYS_PATH`). До review issue 3 здесь был свой
    разбор файла, который не понимал `googler_pool`: если в файле ключей записан
    только пул, снимок молча не обновлялся, и поводы §9/§10 никогда не срабатывали.

    `save_fastgen_usage` живёт в `server/main.py` — та же функция, что кладёт снимок
    из кабинета, чтобы форма записи была одна на двоих, а не две похожие. Импорт
    кабинета лениво: он тянет `googler_worker`, и тем, кто не смотрит на модели
    картинок (--whoami, --test, --state), этот вес не нужен.
    """
    if os.path.join(BASE, "workers") not in sys.path:
        sys.path.insert(0, os.path.join(BASE, "workers"))
    import main as vsgen                                          # noqa: E402
    try:
        key = vsgen._first_credential(vsgen.keys(), "googler")
    except Exception:
        key = ""
    if not key:
        return
    try:
        r = requests.get(_FG_USAGE_URL, headers={"X-API-Key": key}, timeout=15)
        r.raise_for_status()
        usage = r.json()
    except Exception as exc:
        # Ответ провайдера и отсутствие ответа — разные события, а до 21.09.2026 они
        # обрабатывались одинаково: и «401, ключ не принят», и «нет интернета» просто
        # не обновляли снимок, и мёртвый ключ выглядел как обрыв связи. Отличает их
        # ровно один признак: у HTTPError есть `response` с кодом, у обрыва — нет.
        code = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
        if code in A.KEY_REFUSED_CODES:
            # Ещё не тревога: §11 ждёт второго отказа подряд. Кладём маску, чтобы в
            # сообщении было видно, о каком ключе речь; значение ключа не сохраняется.
            A.note_key_refused(c, A.FASTGEN_FAMILY, code, now, mask(key))
        return
    # Ключ принят — снимаем заявку на отказ, если она была. Принятый запрос и есть
    # единственное доказательство того, что ключ заменили: по нему §11 гаснет и уходит
    # «ПОЧИНИЛОСЬ», а по таймеру «само рассосётся» мы бы обещали то, чего не знаем.
    A.note_key_accepted(c, A.FASTGEN_FAMILY, now)
    try:
        vsgen.save_fastgen_usage(c, usage, now)
    except Exception:
        pass


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
        print("\nвсё спокойно: ни один из тринадцати поводов не выполнен")
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
        # 21.09.2026: цикл переживал свой стенд и оставался сиротой. `trap "kill 0"` в
        # dev/run.sh спасает только при Ctrl+C: если стенд гасят по имени процесса
        # (`pkill -f "port 8084"`), умирает один uvicorn, а цикл продолжает слать
        # тревоги в телеграм каждую минуту. За день накопилось ДВАДЦАТЬ ТРИ таких
        # сироты — владелец получил их все сразу и попросил «поубивать студии».
        #
        # Поэтому цикл сам следит за родителем: остался без него (getppid() == 1) —
        # выходит. Это работает при любом способе убийства стенда, в том числе при
        # `kill -9`, где никакой trap не сработал бы вовсе.
        parent = os.getppid()
        while True:
            if args.loop and (os.getppid() != parent or os.getppid() == 1):
                print("стенд закрылся — останавливаюсь, чтобы не слать тревоги в пустоту")
                return 0
            # Перед каждым тиком — свежий снимок FastGen, иначе поводы §9/§10 видят
            # цифры только тогда, когда кто-то параллельно открывал кабинет.
            refresh_fastgen_usage(c, time.time())
            sent = A.tick(c, tg.send, tenant=TENANT)
            # Ежедневная сводка живёт на том же таймере, что и тревоги: своего процесса
            # ей не заводим — это была бы вторая сирота вдобавок к той, которую уже
            # пришлось учить следить за родителем. По умолчанию она ВЫКЛЮЧЕНА, и тогда
            # этот вызов ничего не делает и в сеть не ходит.
            digest = D.daily_tick(c, tg.send, TENANT, now=time.time())
            stamp = time.strftime("%H:%M:%S")
            # 14.10.2026: ежедневный сбор часов монетизации — на том же таймере и по той
            # же причине. Его сбой не должен глушить тревоги: тревоги уже ушли выше, а
            # здесь только печатаем, что не вышло, — следующий проход попробует снова.
            try:
                earn = E.daily_tick(c, TENANT, now=time.time())
                if earn.get("queued"):
                    print(f"{stamp}  ежедневный сбор часов: поставлено профилей "
                          f"{len(earn['queued'])}")
            except Exception as exc:                          # noqa: BLE001
                c.rollback()
                print(f"{stamp}  ежедневный сбор часов не поставлен: {exc}")
            if sent:
                for key, kind, _text in sent:
                    print(f"{stamp}  {kind:<6} {key}")
            if digest["sent"]:
                print(f"{stamp}  сводка отправлена")
            if not sent and not digest["sent"]:
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
