#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Слияние личности: карта, перенос, откат и три запрета.

Работает на копиях в временном каталоге — настоящие базы не трогаются. Проверяется
не только «переехало», но и то, что перенос **обратим** и что он останавливается там,
где мог бы затереть чужое.

19.09.2026, ребейз рерайта на боевой 18.09. Вместе с боем в `job_queue.db` приезжает
таблица `create_intents(user_id, scope, key_hash, payload_hash, job_id)` — ключи
повтора создания задачи. Перенос личности её не знал и переписывал `user_id` только
в `jobs`/`fair_queue_*`. Тихая двойная оплата: у задач владелец новый, у ключей —
старый, `lookup_create` под новым id не находит ничего, и тот же `Idempotency-Key`
создаёт ВТОРУЮ ОПЛАЧЕННУЮ задачу. Снаружи это выглядит как обычный повтор, никто
ничего не замечает. Проверяется и перенос, и откат: ключ обязан ходить вместе
с задачей, на которую он указывает, в обе стороны.

    .venv/bin/python tests/test_identity.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import identity as I                                              # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def make_world(root, cabinet=(("dev", 1, "1"),),
               rewrite=(("dev", "aaaaaaaaaaaa"),),
               cover=(("dev", "bbbbbbbbbbbb"),)):
    """Три сервиса в миниатюре: те же таблицы, те же каталоги, те же имена колонок."""
    cab = os.path.join(root, "dev", "vsgen.db")
    os.makedirs(os.path.dirname(cab), exist_ok=True)
    c = sqlite3.connect(cab)
    c.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, "
                    "tenant_id TEXT, disabled INTEGER DEFAULT 0);")
    for name, uid, tid in cabinet:
        c.execute("INSERT INTO users VALUES(?,?,?,0)", (uid, name, tid))
    c.commit(); c.close()

    def build(svc_dir, sessions_db, queue_db, users, extra_sessions, queue_cols):
        os.makedirs(os.path.join(root, svc_dir), exist_ok=True)
        s = sqlite3.connect(os.path.join(root, svc_dir, sessions_db))
        s.executescript(
            "CREATE TABLE users(username TEXT PRIMARY KEY, user_id TEXT);"
            "CREATE TABLE sessions(session_id TEXT PRIMARY KEY, user_id TEXT);"
            + extra_sessions)
        for login, uid in users:
            s.execute("INSERT INTO users VALUES(?,?)", (login, uid))
            for i in range(3):
                s.execute("INSERT INTO sessions VALUES(?,?)", (f"{uid}-s{i}", uid))
        s.commit(); s.close()
        q = sqlite3.connect(os.path.join(root, svc_dir, queue_db))
        q.executescript(queue_cols)
        for login, uid in users:
            q.execute("INSERT INTO jobs(id,user_id,data) VALUES(?,?,?)",
                      (f"{uid}-job", uid,
                       json.dumps({"output_dir": f"/srv/{svc_dir}/outputs/{uid}/x"})))
            # Ключи повтора есть только у рерайта: таблица приехала с ребейзом 18.09.
            # Строку кладём ровно там, где сервис её объявил, — так фикстура остаётся
            # снимком настоящей схемы, а не общим знаменателем двух соседей.
            if "create_intents" in queue_cols:
                q.execute("INSERT INTO create_intents(user_id,scope,key_hash,"
                          "payload_hash,job_id) VALUES(?,?,?,?,?)",
                          (uid, "api-rewrite", f"key-{uid}", f"payload-{uid}",
                           f"{uid}-job"))
        q.commit(); q.close()
        for login, uid in users:
            for d in ("presets", "outputs", "uploads"):
                p = os.path.join(root, svc_dir, d, uid)
                os.makedirs(p, exist_ok=True)
                open(os.path.join(p, "файл.json"), "w").write(login)

    build("rewrite", "sessions.db", "job_queue.db", rewrite,
          "CREATE TABLE api_keys(key_id TEXT PRIMARY KEY, user_id TEXT);"
          "CREATE TABLE user_keys(user_id TEXT PRIMARY KEY);",
          "CREATE TABLE jobs(id TEXT PRIMARY KEY, user_id TEXT, data TEXT);"
          "CREATE TABLE fair_queue_users(provider TEXT, user_id TEXT);"
          "CREATE TABLE fair_queue_state(provider TEXT, last_user_id TEXT);"
          # Схема слово в слово из боевого job_queue.py:201 (ребейз 18.09).
          "CREATE TABLE create_intents(user_id TEXT NOT NULL, scope TEXT NOT NULL, "
          "key_hash TEXT NOT NULL, payload_hash TEXT NOT NULL, job_id TEXT NOT NULL, "
          "PRIMARY KEY(user_id,scope,key_hash));")
    build("cover", "cover_sessions.db", "cover_job_queue.db", cover, "",
          "CREATE TABLE jobs(id TEXT PRIMARY KEY, user_id TEXT, data TEXT);"
          "CREATE TABLE api_keys(key_id TEXT PRIMARY KEY, user_id TEXT);")
    return cab


def q(path, sql, *args):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, args).fetchone()[0]
    finally:
        c.close()


def main() -> int:
    root = tempfile.mkdtemp(prefix="identity-test-")
    try:
        print("Карта: кто есть где")
        cab = make_world(root)
        p = I.plan(cab, root)
        row = p["rows"][0]
        check("логин найден во всех трёх", row["in_cabinet"], True)
        check("целевой id — tenant кабинета", row["target"], "1")
        check("rewrite к переносу", row["services"]["rewrite"]["state"], I.OK)
        check("cover к переносу", row["services"]["cover"]["state"], I.OK)
        check("проблем нет", p["problems"], [])

        print("\nПодсчёт до переноса")
        rw = [s for s in I.services(root) if s.name == "rewrite"][0]
        c = I.count(rw, "aaaaaaaaaaaa")
        # users + 3 сессии + задача + ключ повтора
        check("строки посчитаны", sum(c["rows"].values()), 6)
        check("ключ повтора попал в подсчёт",
              c["rows"].get("job_queue.db:create_intents.user_id"), 1)
        check("каталоги найдены", len(c["dirs"]), 3)
        check("путь в задаче замечен", c["json"], 1)

        print("\nПеренос")
        for svc in I.services(root):
            old = {"rewrite": "aaaaaaaaaaaa", "cover": "bbbbbbbbbbbb"}[svc.name]
            I.move_one(svc, old, "1", log=lambda *_a: None)
        check("пользователь под общим id",
              q(os.path.join(root, "rewrite", "sessions.db"),
                "SELECT user_id FROM users WHERE username='dev'"), "1")
        check("сессии переехали",
              q(os.path.join(root, "rewrite", "sessions.db"),
                "SELECT COUNT(*) FROM sessions WHERE user_id='1'"), 3)
        check("задача переехала",
              q(os.path.join(root, "cover", "cover_job_queue.db"),
                "SELECT COUNT(*) FROM jobs WHERE user_id='1'"), 1)
        # Без этой строки перенос молча раздваивает оплату: задача под новым id,
        # ключ повтора — под старым, и следующий тот же Idempotency-Key оплачивается
        # заново. Проверяем и то, что под старым id не осталось НИЧЕГО: половинчатый
        # перенос выглядел бы снаружи точно так же, как удачный.
        check("ключ повтора переехал за задачей",
              q(os.path.join(root, "rewrite", "job_queue.db"),
                "SELECT COUNT(*) FROM create_intents WHERE user_id='1'"), 1)
        check("под старым id ключей не осталось",
              q(os.path.join(root, "rewrite", "job_queue.db"),
                "SELECT COUNT(*) FROM create_intents WHERE user_id='aaaaaaaaaaaa'"), 0)
        check("ключ по-прежнему указывает на ту же задачу",
              q(os.path.join(root, "rewrite", "job_queue.db"),
                "SELECT job_id FROM create_intents WHERE user_id='1'"),
              "aaaaaaaaaaaa-job")
        check("каталог переименован",
              os.path.isdir(os.path.join(root, "rewrite", "presets", "1")), True)
        check("старого каталога нет",
              os.path.exists(os.path.join(root, "rewrite", "presets", "aaaaaaaaaaaa")),
              False)
        check("файл внутри цел",
              open(os.path.join(root, "rewrite", "presets", "1", "файл.json")).read(),
              "dev")
        data = q(os.path.join(root, "cover", "cover_job_queue.db"),
                 "SELECT data FROM jobs LIMIT 1")
        check("путь внутри задачи поправлен", "/outputs/1/x" in data, True)
        check("старого пути в задаче не осталось", "bbbbbbbbbbbb" in data, False)

        print("\nПовтор переноса ничего не ломает")
        p2 = I.plan(cab, root)
        check("переносить больше нечего", p2["movers"], [])
        check("состояние — «уже общий»",
              p2["rows"][0]["services"]["rewrite"]["state"], I.DONE)

        print("\nОткат возвращает как было")
        I.write_journal(root, {"at": 0, "stamp": "t", "backup": "",
                               "moved": [{"service": "rewrite", "login": "dev",
                                          "old": "aaaaaaaaaaaa", "new": "1"},
                                         {"service": "cover", "login": "dev",
                                          "old": "bbbbbbbbbbbb", "new": "1"}]})
        I.rollback(root, log=lambda *_a: None)
        check("id вернулся",
              q(os.path.join(root, "rewrite", "sessions.db"),
                "SELECT user_id FROM users WHERE username='dev'"), "aaaaaaaaaaaa")
        check("каталог вернулся",
              os.path.isdir(os.path.join(root, "rewrite", "presets", "aaaaaaaaaaaa")),
              True)
        check("путь в задаче вернулся",
              "bbbbbbbbbbbb" in q(os.path.join(root, "cover", "cover_job_queue.db"),
                                  "SELECT data FROM jobs LIMIT 1"), True)
        # Откат обязан быть полным по тем же причинам: ключ, оставшийся под новым id
        # после возврата задач, — это та же двойная оплата, только с другой стороны.
        check("ключ повтора вернулся",
              q(os.path.join(root, "rewrite", "job_queue.db"),
                "SELECT COUNT(*) FROM create_intents WHERE user_id='aaaaaaaaaaaa'"), 1)
        check("журнал опустел",
              json.load(open(I.journal_path(root), encoding="utf-8")), [])
        shutil.rmtree(root)

        print("\nЗапрет: у соседа есть, в кабинете нет")
        root = tempfile.mkdtemp(prefix="identity-test-")
        cab = make_world(root, cabinet=(("dev", 1, "1"),),
                         rewrite=(("dev", "aaaaaaaaaaaa"), ("чужой", "cccccccccccc")))
        p = I.plan(cab, root)
        stranger = next(r for r in p["rows"] if r["login"] == "чужой")
        check("помечен как «нет учётки»",
              stranger["services"]["rewrite"]["state"], I.NO_ACCOUNT)
        check("проблема названа", any("переносить некуда" in x for x in p["problems"]),
              True)
        check("свой при этом переносится",
              any(r["login"] == "dev" for r in p["movers"]), True)
        shutil.rmtree(root)

        print("\nЗапрет: целевой id уже занят другим человеком")
        # Крест-накрест: в rewrite логин «dev» сидит под id «2», а «2» — это целевой
        # id для «второго». Теоретически цепочку можно распутать переносом по порядку,
        # но мы этого НЕ делаем: остановиться и позвать человека дешевле, чем ошибиться
        # в порядке и затереть чужие данные. Со случайными 12-hex у соседей и мелкими
        # числами у кабинета такое столкновение и не встретится.
        root = tempfile.mkdtemp(prefix="identity-test-")
        cab = make_world(root, cabinet=(("dev", 1, "1"), ("второй", 2, "2")),
                         rewrite=(("dev", "2"), ("второй", "dddddddddddd")))
        p = I.plan(cab, root)
        second = next(r for r in p["rows"] if r["login"] == "второй")
        check("столкновение поймано",
              second["services"]["rewrite"]["state"], I.COLLISION)
        check("сказано, чей id занят",
              any("уже занят логином «dev»" in x for x in p["problems"]), True)
        check("и это не мешает увидеть остальных",
              next(r for r in p["rows"] if r["login"] == "dev")
              ["services"]["rewrite"]["state"], I.OK)

        print("\nЗапрет: каталог назначения уже существует")
        root2 = tempfile.mkdtemp(prefix="identity-test-")
        make_world(root2)
        os.makedirs(os.path.join(root2, "rewrite", "presets", "1"))
        rw = [s for s in I.services(root2) if s.name == "rewrite"][0]
        try:
            I.move_one(rw, "aaaaaaaaaaaa", "1", log=lambda *_a: None)
            check("перенос остановлен", "не остановился", "FileExistsError")
        except FileExistsError:
            check("перенос остановлен", "FileExistsError", "FileExistsError")
        shutil.rmtree(root2)
    finally:
        shutil.rmtree(root, ignore_errors=True)

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
