#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поздняя проверка: вышли ли ролики, у которых расписание уже подтверждено.

Ничего не отменяет и не перезаливает — только повышает запись `scheduled` → `published`.

    dev/verify_public.py            что бы проверилось и что известно сейчас
    dev/verify_public.py --run      спросить YouTube и записать результат
    dev/verify_public.py --key ...  разовый ключ, не сохраняя его в базу
    dev/verify_public.py --save-key ...   положить ключ в admin_state

Ключ — YouTube Data API v3 (console.cloud.google.com → APIs → YouTube Data API v3 →
Credentials → API key). Квота бесплатная: `videos.list` стоит 1 единицу из 10 000
в сутки, то есть на 47 роликов в сутки её хватает с тысячекратным запасом.

Этим же `--run` живёт таймер `deploy/studio-verify.timer`. Слово другое, чем у
соседей (`dev/alerts.py --once`, `dev/stats.py --once`), и менять его не за что:
`--run` стоит здесь с первого дня, а второе имя для одного действия — лишний способ
ошибиться. Что важно для таймера, так это код возврата, и он значит вот что:

    0 — проход состоялся (в том числе «спрашивать было некого»);
    1 — проход НЕ состоялся: ждали ролики, а ключа не было.

Отсюда правило, которое легко сломать обратно: отсутствие ключа само по себе провалом
не считается. В сутки, когда ни один слот не наступил, спрашивать всё равно нечего, и
красный юнит в `systemctl --failed` означал бы поломку там, где её нет. Красный юнит
здесь читается однозначно: работа была, а сделать её было нечем.
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

import pub_verify as V                                            # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT") or None


def _сказать_что_вышли(c, out) -> None:
    """Отправить в телеграм, что ролики вышли. Единственная хорошая новость студии.

    Владелец 02.10.2026: «да, я хочу знать, что видосы готовы». До этого выход не
    сообщался никак: проход поднимал запись в базе и молчал, а знать из базы — это не
    знать. Отправляем ровно по факту смены состояния, а не по расписанию, поэтому
    сообщение приходит один раз на ролик и никогда не повторяется.

    Молчим в трёх случаях, и каждый намеренно: выходить было некому; стенд запущен с
    VS_ALERTS=0 (его копия базы не должна писать владельцу — правило с 21.09);
    телеграм не настроен (тогда `send` сам вернёт False, и мы честно это напечатаем).

    Неудачу отправки НЕ превращаем в провал прохода: проверка свою работу сделала и
    записала, а молчащий транспорт — отдельная беда, про которую скажет экран тревог.
    """
    if not out.get("вышли"):
        return
    текст = V.хорошая_новость(c, out["вышли"])
    if os.environ.get("VS_ALERTS", "").strip() == "0":
        print("\nтелеграм выключен (VS_ALERTS=0) — сообщение не отправляю:")
        print(текст)
        return
    try:
        import pub_alerts_tg
        отправлено = pub_alerts_tg.sender(log=print)(текст)
    except Exception as exc:                                       # noqa: BLE001
        print(f"\nсообщение про выход не отправилось: {exc}")
        return
    print("\nв телеграм отправлено: да" if отправлено else
          "\nв телеграм НЕ отправлено: транспорт не настроен (VS_TG_TOKEN/VS_TG_CHAT)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", action="store_true", help="спросить YouTube и записать")
    p.add_argument("--key", help="ключ на один запуск")
    p.add_argument("--save-key", help="сохранить ключ в admin_state")
    p.add_argument("--grace", type=int, default=None,
                   help=f"через сколько секунд после слота спрашивать (по умолчанию {V.GRACE_SEC})")
    args = p.parse_args()

    if not os.path.exists(DB):
        raise SystemExit(f"нет базы {DB} — сначала: python3 dev/make_db.py")
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        if args.save_key:
            c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                      (V.KEY_STATE, args.save_key.strip(), time.time()))
            c.commit()
            print(f"ключ сохранён: {mask(args.save_key)}")
            return 0

        key = args.key or V.api_key(c)
        print(f"ключ: {mask(key) if key else 'нет — проверка невозможна'}")
        now = time.time()
        rows = V.due(c, TENANT, now, args.grace)
        print(f"пора спросить: {len(rows)} (слот прошёл больше "
              f"{(V.GRACE_SEC if args.grace is None else args.grace) // 60} мин назад)")
        for r in rows:
            waited = (now - float(r["slot_at"] or now)) / 3600
            print(f"  {r['release_id']:<24} {r['video_id']:<14} слот прошёл {waited:.1f} ч назад")
        if not args.run:
            if rows:
                print("\nэто только показ. Чтобы спросить YouTube: --run")
            return 0
        if not key:
            print("\nбез ключа спрашивать нечем: вставить на экране «Ключи», строка "
                  "«YouTube Data API», или задать VS_YT_API_KEY на сервере")
            # Провал — только когда ключа не хватило для настоящей работы. Пустая
            # очередь ключа и не требовала: объявлять такой проход сломанным значит
            # учить оператора не верить красному юниту.
            return 1 if rows else 0
        print("\nспрашиваю YouTube:")
        out = V.run(c, TENANT, now, key=key, log=print)
        print(f"\nпроверено {out['checked']}: вышло {out['published']}, "
              f"только по ссылке {out['unlisted']}, ждём {out['waiting']}, "
              f"не подтверждено {out['stale']}, без ответа {out['unknown']}, "
              f"пропущено как недавно спрошенное {out['skipped']}")
        # Отложенные и причина — отдельной строкой, а не молчанием. Без неё проход,
        # упёршийся в квоту, выглядит в журнале как «проверено 0» без объяснения: ровно
        # тот симптом ночи 18.09, когда 403 не был виден никому.
        if out["quota_wait"]:
            print(f"отложено до сброса квоты: {out['quota_wait']}")
        # Предел вопросов за проход — не потеря и не поломка: очередь, которую доберёт
        # следующий проход. Без этой строки «проверено 50, а слотов 300» выглядит
        # обрывом, и оператор идёт искать несуществующую ошибку.
        if out["deferred"]:
            print(f"отложено на следующий проход (предел {V.ASK_LIMIT} за раз): "
                  f"{out['deferred']}")
        if out["why"]:
            print(f"причина остановки: {out['why']}")
        q = out["quota"]
        print(f"квота за сутки {q['day']}: потрачено {q['used']} из {q['budget']}")
        _сказать_что_вышли(c, out)
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
