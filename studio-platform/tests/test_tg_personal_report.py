#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Личный отчёт в телеграм: привязка кодом, только беды, отбор, раз в сутки (14.10.2026).

Владелец: «в настройках подключать свой тг, чтобы тебе конкретно по твоим каналам
приходил отчёт: 1. у этих каналов проебан AdsPower, 2. этим каналам не хватает донора».

Что здесь опасно и потому проверяется.

1. **Привязка чужого чата к чужому человеку.** Код одноразовый и живёт 15 минут:
   просроченный не привязывает, использованный второй раз — тоже.
2. **Отчёт обязан находить обе беды из того, что студия уже знает**: профиль не
   привязан, профиль пропал из AdsPower, задание ждёт чужой открытый профиль, выпуск в
   разборе по входу/прокси — это «AdsPower»; запас ниже порога и выпуск в разборе из-за
   донора — «Донор». Канал без расписания — «не знаем», а не «нет донора».
3. **Тишина без бед** и **не чаще раза в сутки**: владелец 28.09.2026 жаловался на поток
   сообщений. Непринятое телеграмом не помечается отправленным.
4. **Чужая привязка недостижима**: API берёт человека из сессии; чужой тег/канал в
   отборе — отказ.

Сети нет: транспорт подставной.

    VS_ALERTS=0 .venv/bin/python tests/test_tg_personal_report.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="tg-personal-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_DIGEST_TZ"] = "UTC"
os.environ.pop("VS_TG_TOKEN", None)
os.environ.pop("VS_TG_CHAT", None)
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                          # noqa: E402
import pub_alerts as A                                            # noqa: E402
import pub_tg_personal as P                                       # noqa: E402

FAILS = []
# Среда 14.10.2026, 07:00 UTC — до часа рассылки (9:00).
NOW = dt.datetime(2026, 10, 14, 7, 0, tzinfo=dt.timezone.utc).timestamp()
HOUR = 3600
DAY = 86400


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeTg:
    """Подставной бот: копит отправленное, отдаёт заготовленные апдейты."""
    def __init__(self, ok=True):
        self.token = "123:fake"
        self.sent = []
        self.ok = ok
        self.queue = []
        self.offsets = []
        self.fail_updates = None

    def send_to(self, chat, text):
        self.sent.append((str(chat), text))
        return self.ok

    def updates(self, offset=0):
        self.offsets.append(offset)
        if self.fail_updates:
            return self.fail_updates
        return {"ok": True, "result": [u for u in self.queue if u["update_id"] >= offset],
                "error": ""}

    def me(self):
        return {"username": "studio_test_bot"}

    def start(self, uid, chat_id, code, title="Иван"):
        self.queue.append({"update_id": uid, "message": {
            "text": f"/start {code}", "chat": {"id": chat_id, "type": "private",
                                               "first_name": title}}})


def setup_db(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
        role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
      CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
        kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, tenant_id TEXT,
        settings TEXT);
    """)
    c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(1,'аня','user','t1')")
    c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(2,'боря','user','t1')")
    c.commit()
    return c


DAILY = {"*": ["10:00"]}


def channel(c, name, schedule=DAILY, profile=True, tags=(), tid="t1", stock=20,
            missing=False):
    pid = None
    if profile:
        pid = c.execute("INSERT INTO pub_profiles(tenant_id,serial,status,created,missing_since) "
                        "VALUES(?,?,'active',?,?)",
                        (tid, f"s{name}", NOW, NOW - 2 * DAY if missing else None)).lastrowid
    uc = "UC" + str(abs(hash(name)) % 10 ** 22).rjust(22, "0")
    cid = c.execute("INSERT INTO pub_channels(tenant_id,uc,name,tz,schedule,profile_id,"
                    "created,updated) VALUES(?,?,?,'UTC',?,?,?,?)",
                    (tid, uc, name, json.dumps(schedule), pid, NOW, NOW)).lastrowid
    for tag in tags:
        row = c.execute("SELECT id FROM pub_tags WHERE tenant_id=? AND name=?",
                        (tid, tag)).fetchone()
        tag_id = row[0] if row else c.execute(
            "INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)",
            (tid, tag, NOW)).lastrowid
        c.execute("INSERT INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)", (cid, tag_id))
    # Запас: по ссылке на каждый ближайший выход (10:00 UTC ежедневно).
    first = dt.datetime(2026, 10, 14, 10, 0, tzinfo=dt.timezone.utc).timestamp()
    for i in range(stock):
        c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,status,"
                  "created,updated) VALUES(?,?,?,?,'planned',?,?)",
                  (tid, cid, f"https://youtu.be/{cid}x{i}", first + i * DAY, NOW, NOW))
    c.commit()
    return cid


_n = [0]


def blocked(c, cid, reason, tid="t1"):
    _n[0] += 1
    item = c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,"
                     "status,created,updated) VALUES(?,?,?,?,'error',?,?)",
                     (tid, cid, f"https://youtu.be/b{_n[0]}", NOW - DAY - _n[0], NOW, NOW)
                     ).lastrowid
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,phase,status,blocked_reason,created,updated) "
              "VALUES(?,?,?,1,?,'UC',?,'media_ready','blocked',?,?,?)",
              (f"rel{_n[0]}", tid, item, cid, NOW - DAY, reason, NOW, NOW))
    c.commit()


def scheduled(c, cid, days=5, tid="t1"):
    """Ролики, уже стоящие в YouTube на 10:00 ближайших дней: канал «выходит»."""
    first = dt.datetime(2026, 10, 14, 10, 0, tzinfo=dt.timezone.utc).timestamp()
    for i in range(days):
        _n[0] += 1
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
                  "plan_item_version,channel_id,uc,slot_at,phase,status,created,updated) "
                  "VALUES(?,?,?,1,?,'UC',?,'scheduled','done',?,?)",
                  (f"ok{_n[0]}", tid, 10_000 + _n[0], cid, first + i * DAY, NOW, NOW))
    c.commit()


def in_work(c, cid, phase, tid="t1"):
    _n[0] += 1
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,phase,status,created,updated) "
              "VALUES(?,?,?,1,?,'UC',?,?,'running',?,?)",
              (f"w{_n[0]}", tid, 20_000 + _n[0], cid, NOW + 3 * HOUR, phase, NOW, NOW))
    c.commit()


def fixture():
    """Каналы на все случаи: два здоровых и по одному на каждую беду."""
    c = setup_db(os.path.join(TMP, f"f{len(os.listdir(TMP))}.db"))
    ids = {}
    ids["ok1"] = channel(c, "Здоровый", tags=("ww2",))
    ids["ok2"] = channel(c, "Тоже здоровый")
    scheduled(c, ids["ok1"])
    scheduled(c, ids["ok2"])
    ids["noprof"] = channel(c, "Без профиля", profile=False, tags=("ww2",))
    ids["gone"] = channel(c, "Пропал профиль", missing=True)
    ids["proxy"] = channel(c, "Прокси", tags=("манхва",))
    blocked(c, ids["proxy"], "upload failed: AdsPower: Check Proxy Fail (profile s1)")
    blocked(c, ids["proxy"], "upload failed: AdsPower: Check Proxy Fail (profile s1)")
    ids["busy"] = channel(c, "Занятый профиль")
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
              "title,video_path,slot_at,status,blocked_since,blocked_why,created,updated) "
              "VALUES('rbusy','t1',?,'UC','u','t','v',?, 'queued',?,?,?,?)",
              (ids["busy"], NOW, NOW - 2 * HOUR, "профиль открыт посторонним", NOW, NOW))
    ids["subs"] = channel(c, "Без субтитров", tags=("ww2",))
    blocked(c, ids["subs"], "у ролика нет субтитров (TranscriptsDisabled)")
    ids["empty"] = channel(c, "Пустой", stock=0, tags=("манхва",))
    ids["nosched"] = channel(c, "Без расписания", schedule={}, stock=0)
    ids["other"] = channel(c, "Чужой", profile=False, tid="t2", stock=0)
    c.commit()
    return c, ids


# ─────────────────────────── 1. привязка кодом ───────────────────────────

def part_link():
    print("Привязка по коду")
    c = setup_db(os.path.join(TMP, "link.db"))
    tg = FakeTg()
    check("без живых кодов getUpdates не трогаем",
          (P.poll(c, tg, NOW)["polled"], tg.offsets), (False, []))
    code = P.start_link(c, 1, "t1", now=NOW)["code"]
    check("код — 12 знаков из алфавита start", len(code) == 12 and
          all(ch.isalnum() or ch in "-_" for ch in code), True)
    check("до привязки — не подключён, код виден", (P.settings(c, 1, NOW)["linked"],
                                                    P.settings(c, 1, NOW)["code"]), (False, code))
    tg.start(100, 555, code)
    out = P.poll(c, tg, NOW + 60)
    check("действующий код привязал чат", out["bound"], [1])
    s = P.settings(c, 1, NOW + 60)
    check("подключён, код погашен", (s["linked"], s["chat_title"], s["code"]),
          (True, "Иван", ""))
    check("рассылка после привязки всё ещё выключена", s["enabled"], False)
    check("в чат ушёл ответ «готово» с именем",
          tg.sent and tg.sent[-1][0] == "555" and "аня" in tg.sent[-1][1], True)
    check("offset сохранён — апдейт не придёт второй раз",
          A._state_row(c, P.OFFSET_KEY), 101)

    # Повтор того же кода — из другого чата.
    code2 = P.start_link(c, 2, "t1", now=NOW)["code"]       # чтобы опрос вообще шёл
    tg.queue.append({"update_id": 101, "message": {"text": f"/start {code}",
                                                   "chat": {"id": 777, "type": "private"}}})
    out = P.poll(c, tg, NOW + 120)
    check("использованный код никого не привязал", out["bound"], [])
    check("чат первого человека не перехвачен", P.settings(c, 1, NOW)["linked"]
          and c.execute("SELECT chat_id FROM pub_user_tg WHERE user_id=1").fetchone()[0],
          "555")
    check("в чужой чат ушло «код не узнан»", "не узнан" in tg.sent[-1][1], True)

    # Просроченный код.
    tg.start(102, 888, code2)
    out = P.poll(c, tg, NOW + P.CODE_TTL + 1, force=True)
    check("просроченный код не привязывает", out["bound"], [])
    check("человек 2 не подключён", P.settings(c, 2, NOW)["linked"], False)
    check("ответ в чат — «код устарел»", "устарел" in tg.sent[-1][1], True)

    # Webhook: getUpdates отвечает 409 — не падаем, говорим словами.
    P.start_link(c, 2, "t1", now=NOW + 1000)
    tg.fail_updates = {"ok": False, "result": [], "code": 409,
                       "error": "HTTP 409: Conflict: can't use getUpdates method while "
                                "webhook is active"}
    out = P.poll(c, tg, NOW + 1001)
    check("webhook — ошибка словами, без падения", "webhook" in out["error"], True)
    check("ошибка опроса запомнена для профиля", "webhook" in P.poll_error(c), True)

    # Отключение.
    P.set_settings(c, 1, "t1", enabled=True, now=NOW)
    s = P.unlink(c, 1, now=NOW)
    check("отключить: чата нет, рассылка выключена", (s["linked"], s["enabled"]), (False, False))
    try:
        P.set_settings(c, 1, "t1", enabled=True, now=NOW)
        check("включить без чата нельзя", "прошло", "отказ")
    except ValueError:
        check("включить без чата нельзя", "отказ", "отказ")


# ─────────────────────────── 2. содержание отчёта ───────────────────────────

def part_report():
    print("\nОтчёт: обе беды, отбор, длина")
    c, ids = fixture()
    data = P.collect(c, "t1", None, NOW)
    ads = {x["name"]: x["reasons"] for x in data["adspower"]}
    don = {x["name"]: x["reasons"] for x in data["donor"]}
    check("AdsPower: без профиля", ads.get("Без профиля"),
          ["профиль AdsPower не привязан к каналу"])
    check("AdsPower: пропавший профиль с датой", ads.get("Пропал профиль"),
          ["профиль sПропал профиль пропал из AdsPower (с 12.10)"])
    check("AdsPower: прокси — причина из block_reasons и счёт выпусков",
          ads.get("Прокси"), ["прокси профиля не работает (2 выпуска)"])
    check("AdsPower: задание ждёт чужой профиль", ads.get("Занятый профиль"),
          ["заливка ждёт профиль, открытый посторонним (1 выпуск)"])
    check("Донор: выпуск без субтитров", don.get("Без субтитров"),
          ["у донора отключены субтитры (1 выпуск)"])
    check("Донор: запас кончился", don.get("Пустой"), ["материала нет — вставьте ссылки"])
    check("здоровые каналы не упомянуты",
          any(n in ads or n in don for n in ("Здоровый", "Тоже здоровый")), False)
    check("канал без расписания — «не знаем», а не «нет донора»",
          ("Без расписания" in don, data["supply_unknown"]), (False, ["Без расписания"]))
    check("чужой арендатор не попал никуда",
          "Чужой" in ads or "Чужой" in don, False)

    text = P.render(data, "14.10.2026", "все каналы студии")
    print("\n----- пример сообщения -----\n" + text + "\n----------------------------\n")
    check("текст: обе группы с заголовками",
          ("AdsPower" in text and "Донор" in text), True)
    check("текст под пределом Telegram", len(text) <= 4096, True)

    # Отбор по тегу: только ww2 (Здоровый, Без профиля, Без субтитров).
    tag_ww2 = c.execute("SELECT id FROM pub_tags WHERE name='ww2'").fetchone()[0]
    flt = {"mode": "pick", "tags": [tag_ww2], "channels": []}
    d2 = P.collect(c, "t1", flt, NOW)
    check("отбор по тегу: AdsPower", [x["name"] for x in d2["adspower"]], ["Без профиля"])
    check("отбор по тегу: Донор", [x["name"] for x in d2["donor"]], ["Без субтитров"])
    d3 = P.collect(c, "t1", {"mode": "pick", "tags": [tag_ww2], "channels": [ids["empty"]]},
                   NOW)
    check("тег + отдельный канал складываются",
          sorted(x["name"] for x in d3["donor"]), ["Без субтитров", "Пустой"])
    check("пустой выбор «по выбору» — отказ, не вечное молчание",
          _raises(lambda: P.clean_filter({"mode": "pick"})), True)

    # Без бед — пустой текст.
    d4 = P.collect(c, "t1", {"mode": "pick", "tags": [], "channels": [ids["ok1"], ids["ok2"]]},
                   NOW)
    check("без бед текст пуст", P.render(d4, "14.10.2026"), "")
    # Канал без расписания сам повода писать не даёт.
    d5 = P.collect(c, "t1", {"mode": "pick", "tags": [], "channels": [ids["nosched"]]}, NOW)
    check("только «запас неизвестен» — тоже молчим", P.render(d5, "14.10.2026"), "")

    # «Завтра без видео»: каналы по главной причине, группами.
    c2, _ = fixture()
    a = channel(c2, "Права А"); b = channel(c2, "Права Б")
    for cid in (a, b):
        blocked(c2, cid, "account recovery needs the owner: signed in but exact channel "
                         "not confirmed")
    w = channel(c2, "Делается")
    in_work(c2, w, "project_ready")
    late = channel(c2, "Выйдет послезавтра")
    scheduled(c2, late, days=0)
    c2.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
               "channel_id,uc,slot_at,phase,status,created,updated) VALUES('late','t1',"
               "30001,1,?,'UC',?,'scheduled','done',?,?)", (late, NOW + 2 * DAY, NOW, NOW))
    c2.commit()
    sil = {g["title"]: g for g in P.collect(c2, "t1", None, NOW)["silent"]}
    check("права менеджера — одной группой на два канала",
          sil.get("в аккаунте нет этого канала — нужны права менеджера", {}).get("channels"),
          ["Права А", "Права Б"])
    check("   сторона и кто чинит",
          sil["в аккаунте нет этого канала — нужны права менеджера"]["side"], "Вход и профиль")
    check("ролик ещё делается — наша сторона",
          (sil.get("ролик ещё делается: картинки и сборка", {}).get("channels"),
           sil.get("ролик ещё делается: картинки и сборка", {}).get("side")),
          (["Делается"], "Студия"))
    check("ролик только послезавтра — тоже «завтра без видео»",
          any("Выйдет послезавтра" in g["channels"] for g in sil.values()), True)
    check("здоровые и без расписания не молчат",
          any(n in g["channels"] for g in sil.values()
              for n in ("Здоровый", "Тоже здоровый", "Без расписания", "Чужой")), False)
    text = P.render(P.collect(c2, "t1", None, NOW), "15.10.2026")
    print("\n----- «завтра без видео» -----\n" + text + "\n----------------------------\n")
    check("в тексте раздел и группа",
          ("Завтра без видео" in text and "Права А, Права Б" in text), True)

    # Длина: сотня бед сворачивается в «и ещё N».
    many = {"adspower": [{"name": f"Канал номер {i:03d}", "reasons": ["x" * 60], "stuck": 1}
                         for i in range(120)], "donor": [], "supply_unknown": [],
            "channels": 120, "min_days": 3.0}
    long = P.render(many, "14.10.2026")
    check("длинный отчёт под 4096", len(long) <= 4096, True)
    check("длинный отчёт кончается «и ещё N»", "и ещё" in long.splitlines()[-1], True)
    return c, ids


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


# ─────────────────────────── 3. суточная рассылка ───────────────────────────

def part_daily(c, ids):
    print("\nРаз в сутки и молчание без бед")
    tg = FakeTg()
    code = P.start_link(c, 1, "t1", now=NOW)["code"]
    tg.start(1, 501, code)
    code2 = P.start_link(c, 2, "t1", now=NOW)["code"]
    tg.start(2, 502, code2)
    P.poll(c, tg, NOW)
    tg.sent.clear()
    # Человек 1 — все каналы; человек 2 — только здоровые.
    P.set_settings(c, 1, "t1", enabled=True, hour=9, now=NOW)
    P.set_settings(c, 2, "t1", enabled=True, hour=9,
                   flt={"mode": "pick", "channels": [ids["ok1"], ids["ok2"]]}, now=NOW)
    check("до часа рассылки — ничего", (P.daily_tick(c, tg, NOW), tg.sent), ([], []))
    nine = NOW + 2 * HOUR
    out = P.daily_tick(c, tg, nine)
    check("в 9:00 ушёл один отчёт — человеку с бедами",
          [chat for chat, _ in tg.sent], ["501"])
    check("человеку без бед — тишина, но сутки отмечены",
          {r["user_id"]: r["sent"] for r in out}, {1: True, 2: False})
    tg.sent.clear()
    check("второй раз за сутки — ничего", (P.daily_tick(c, tg, nine + HOUR), tg.sent), ([], []))
    # Завтра телеграм не принял — не отмечаем, пробуем снова.
    tg.ok = False
    out = P.daily_tick(c, tg, nine + DAY)
    check("непринятое не отмечено", [r["sent"] for r in out if r["user_id"] == 1], [False])
    tg.ok = True
    tg.sent.clear()
    P.daily_tick(c, tg, nine + DAY + 60)
    check("следующий проход доотправил", [chat for chat, _ in tg.sent], ["501"])
    # Выключенная рассылка — ничего.
    P.set_settings(c, 1, "t1", enabled=False, now=NOW)
    tg.sent.clear()
    P.daily_tick(c, tg, nine + 2 * DAY)
    check("выключено — не шлём", tg.sent, [])
    # «Прислать сейчас» без бед — строка «бед нет», суточную отметку не трогает.
    r = P.send_now(c, tg, 2, nine + 2 * DAY)
    check("проверка без бед всё равно присылает строку",
          (r["sent"], r["empty"], "бед нет" in r["text"]), (True, True, True))


# ─────────────────────────── 4. API: только своё ───────────────────────────

def part_api():
    print("\nAPI: человек видит и меняет только своё")
    c = setup_db(DB)
    ids = {"mine": channel(c, "Мой канал", profile=False, tags=("наш",)),
           "foreign": channel(c, "Канал чужого арендатора", tid="t2")}
    foreign_tag = c.execute("INSERT INTO pub_tags(tenant_id,name,created) VALUES('t2','их',?)",
                            (NOW,)).lastrowid
    for sid, uid in (("s-anya", 1), ("s-borya", 2)):
        c.execute("INSERT INTO sessions(id,user_id,expires) VALUES(?,?,?)",
                  (sid, uid, NOW + 10 * 365 * DAY))
    c.commit()

    import app as W                                                # noqa: E402
    from fastapi.testclient import TestClient                      # noqa: E402
    tg = FakeTg()
    W._tg = lambda: tg
    client = TestClient(W.app, raise_server_exceptions=False)

    def as_(sid):
        client.cookies.clear()
        client.cookies.set(W.auth.COOKIE, sid)

    as_("s-anya")
    r = client.post("/ui/tg-report/link")
    check("ссылка t.me с кодом", r.status_code == 200 and
          r.json()["link"].startswith("https://t.me/studio_test_bot?start="), True)
    code = r.json()["code"]
    tg.start(10, 9001, code, title="Аня")
    r = client.post("/ui/tg-report/check")
    check("«Проверить» привязал чат", (r.json()["linked"], r.json()["chat_title"]),
          (True, "Аня"))
    names = [x["name"] for x in r.json()["channels"]]
    check("в справочнике только каналы своего арендатора", names, ["Мой канал"])

    as_("s-borya")
    r = client.get("/ui/tg-report")
    check("другой человек не видит чужой привязки", r.json()["linked"], False)
    client.post("/ui/tg-report/unlink")
    r = client.post("/ui/tg-report/send-now")
    check("«прислать сейчас» без своего чата — отказ", r.status_code, 400)
    as_("s-anya")
    check("чужое «отключить» не задело привязку", client.get("/ui/tg-report").json()["linked"],
          True)

    r = client.post("/ui/tg-report/settings", json={"filter": {"mode": "pick",
                                                               "channels": [ids["foreign"]]}})
    check("чужой канал в отборе — 400", r.status_code, 400)
    r = client.post("/ui/tg-report/settings", json={"filter": {"mode": "pick",
                                                               "tags": [foreign_tag]}})
    check("чужой тег в отборе — 400", r.status_code, 400)
    r = client.post("/ui/tg-report/settings", json={"enabled": True, "hour": 8,
                                                    "filter": {"mode": "pick",
                                                               "channels": [ids["mine"]]}})
    check("свои настройки сохраняются",
          (r.status_code, r.json()["enabled"], r.json()["hour"], r.json()["filter"]["channels"]),
          (200, True, 8, [ids["mine"]]))
    r = client.post("/ui/tg-report/settings", json={"hour": 30})
    check("час вне 0–23 — 400", r.status_code, 400)
    tg.sent.clear()
    r = client.post("/ui/tg-report/send-now")
    check("«прислать сейчас» ушло в свой чат",
          (r.status_code, [chat for chat, _ in tg.sent]), (200, ["9001"]))
    check("в отчёте — канал без профиля", "Мой канал" in tg.sent[-1][1], True)
    client.cookies.clear()
    check("без входа — 401", client.get("/ui/tg-report").status_code, 401)

    src = open(os.path.join(BASE, "dev", "alerts.py"), encoding="utf-8").read()
    check("проход тревог зовёт личные отчёты", "P.tick(c, tg" in src, True)


def main() -> int:
    part_link()
    c, ids = part_report()
    part_daily(c, ids)
    part_api()
    print()
    if FAILS:
        print(f"провалов: {len(FAILS)}")
        for f in FAILS:
            print(f"  · {f}")
        return 1
    print("провалов: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
