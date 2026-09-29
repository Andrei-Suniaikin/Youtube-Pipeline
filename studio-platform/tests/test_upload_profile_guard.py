#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Исключительное владение профилем AdsPower: чужой профиль — задание ждёт (29.09.2026).

Что проверяется и почему это опасно.

Распределитель заданий заливки считал занятыми только те профили, которые занял он сам.
На минской машине рядом живут старый конвейер gen, конвейер манхвы и человек, который
открывает профиль руками в AdsPower. Два браузера в одном профиле рвут куки и ломают
идущую заливку, а заливщик, увидев уже открытый профиль, ещё и «присоединяется без
рестарта» — то есть садится прямо в чужое окно. Поэтому перед выдачей спрашиваем у
AdsPower этой машины, кто открыт прямо сейчас, и чужой профиль пропускаем.

Три вещи здесь важнее остальных:

  1. «AdsPower не ответил» — это НЕ «профиль занят». Перепутав их, мы одним обрывом
     сети остановили бы заливку сразу на всех каналах. Молчание обязано оставлять
     выдачу работать и обязано быть сказано вслух в журнале выпуска.
  2. Профиль, открытый НАШЕЙ же идущей заливкой, посторонним не считается — иначе мы
     принимали бы себя за чужого и поднимали тревогу на ровном месте.
  3. Ответ AdsPower кэшируется: нода опрашивает кабинет несколько раз в минуту, а предел
     машины — около запроса в секунду и общий с идущими заливками. Без кэша ворота
     отнимали бы этот предел у настоящей работы.

Плюс тревога §13 (задание стоит из-за чужого профиля дольше получаса, порог правится с
экрана «Админ») и экран: «не знаем» на карточке профилей обязано отличаться от
«свободен» — человек, увидевший «свободен», откроет профиль руками.

Сеть не трогается: транспорт `requests.get` внутри `pub_adspower` подменён целиком.

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_upload_profile_guard.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="upload-guard-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

import requests                                                    # noqa: E402

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT);
""")
from pub_calls import ensure_calls_schema                          # noqa: E402

ensure_calls_schema(_c)
_c.commit()
_c.close()

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

import pub_adspower as A                                           # noqa: E402
import pub_alerts as AL                                            # noqa: E402
import app as W                                                    # noqa: E402
from pub_upload_api import register_upload_routes                  # noqa: E402

FAILS = []
TENANT = "t1"
TOKEN = "upl_test_token"
ADS_URL = "http://127.0.0.1:50325"
ADS2_URL = "http://10.0.0.9:50325"
# Время берём настоящее: карточка профилей судит о свежести сведений по стенным часам,
# и выдуманная эпоха выглядела бы для неё «сведениями недельной давности».
CLOCK = {"t": time.time()}


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, cond):
    check(label, bool(cond), True)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def sql(q, args=()):
    c = conn()
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def run(q, args=()):
    c = conn()
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def job(release_id):
    return sql("SELECT * FROM pub_upload_jobs WHERE release_id=?", (release_id,))[0]


def journal(release_id):
    return " | ".join(r["message"] for r in sql(
        "SELECT message FROM pub_release_log WHERE release_id=? ORDER BY id", (release_id,)))


# ─────────────────────────── фейковый AdsPower ───────────────────────────

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeAdsPower:
    """Транспорт под запись: кто открыт НА КАЖДОЙ машине, сколько раз спросили, молчит ли.

    Ответ хранится по адресу машины, а не одним множеством на всех: машин в системе
    несколько, и спросить не ту — это ровно та ошибка, ради которой всё и затевалось.
    """

    def __init__(self):
        self.calls = 0
        self.open_on = {}
        self.silent = False

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        tail = "/api/v1/browser/local-active"
        if not url.endswith(tail):
            raise AssertionError(f"ворота спросили не то: {url}")
        if self.silent:
            raise requests.RequestException("нет связи с машиной")
        who = sorted(self.open_on.get(url[:-len(tail)], set()))
        return FakeResponse({"code": 0, "msg": "success",
                             "data": {"list": [{"user_id": u} for u in who]}})


ADS = FakeAdsPower()


def open_now(uids, url=None):
    """Кто открыт прямо сейчас: либо словарём «машина → профили», либо на минской."""
    ADS.open_on = dict(uids) if url is None and isinstance(uids, dict) else {
        (url or ADS_URL): set(uids)}
A.requests = type("R", (), {"get": staticmethod(ADS.get),
                            "RequestException": requests.RequestException})


def forget_answers():
    """Забыть кэш и выдержку пауз: следующий вопрос уйдёт к машине сразу."""
    A._reset_active_cache_for_tests()
    A._reset_pace_state_for_tests()


# ─────────────────────────── стенд ───────────────────────────

def db():
    return conn()


app = FastAPI()
register_upload_routes(app, {"db": db, "_now": lambda: CLOCK["t"],
                             "upload_token": lambda: TOKEN})
client = TestClient(app, raise_server_exceptions=False)


def claim(worker="1", host="minsk"):
    r = client.post("/api/upload/claim", json={"host": host, "worker": worker},
                    headers={"X-Upload-Token": TOKEN})
    return r.json().get("task")


def seed():
    c = conn()
    now = CLOCK["t"]
    c.execute("INSERT INTO pub_hosts(id,tenant_id,code,title,adspower_url,status,created) "
              "VALUES(1,?,'minsk','Минская машина',?,'active',?)", (TENANT, ADS_URL, now))
    for serial, uid in (("46", "u46"), ("12", "u12")):
        c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,status,"
                  "created,adspower_user_id,active_now,last_seen) "
                  "VALUES(?,?,?,1,'active',?,?,0,?)",
                  (TENANT, serial, f"Профиль {serial}", now, uid, now))
    c.commit()          # иначе следующий запрос из другого соединения их не увидит
    # Вторая машина со своим AdsPower: спросить не ту — ровно та ошибка, ради которой
    # ворота и делались, и поймать её можно только на двух машинах сразу.
    c.execute("INSERT INTO pub_hosts(id,tenant_id,code,title,adspower_url,status,created) "
              "VALUES(2,?,'gdansk','Гданьская машина',?,'active',?)",
              (TENANT, ADS2_URL, now))
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,status,"
              "created,adspower_user_id,active_now,last_seen) "
              "VALUES(?,'77','Профиль 77',2,'active',?,'u77',0,?)", (TENANT, now, now))
    prof = {r[1]: r[0] for r in c.execute("SELECT id, serial FROM pub_profiles")}
    for n, (rid, uc, serial) in enumerate((("rel-a", "UC" + "a" * 22, "46"),
                                            ("rel-b", "UC" + "b" * 22, "46"),
                                            ("rel-c", "UC" + "c" * 22, "77")), start=1):
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
                  "plan_item_version,channel_id,uc,slot_at,status,phase,created,updated) "
                  "VALUES(?,?,?,1,?,?,?,'pending','created',?,?)",
                  (rid, TENANT, n, n, uc, now + 86400, now, now))
        c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,"
                  "profile_id,host_id,channel_url,title,description,schedule_iso,"
                  "visibility,video_path,slot_at,status,created,updated) "
                  "VALUES(?,?,?,?,?,1,?,?,'','','public',?,?,'queued',?,?)",
                  (rid, TENANT, n, uc, prof[serial],
                   f"https://studio.youtube.com/channel/{uc}/", f"ролик {rid}",
                   os.path.join(TMP, "v.mp4"), now + 86400, now, now))
    # Задание второй машины до своего сценария в очереди не стоит: иначе оно мешалось
    # бы в каждой проверке выше, где речь о единственном занятом профиле.
    c.execute("UPDATE pub_upload_jobs SET status='done' WHERE release_id='rel-c'")
    c.commit()
    c.close()
    return prof


# ─────────────────────────── проверки ───────────────────────────

def revive(release_id="rel-a", keep_mark=False):
    """Вернуть задание в очередь, не пересобирая стенд: seed() одноразовый.

    Нужно проверкам, которые идут после того, как задание уже ушло в работу: свой
    отдельный стенд для каждой из них означал бы три копии одних и тех же вставок,
    которые разойдутся при первой же правке схемы.
    """
    # Остальные задания уводим из очереди: если на том же профиле висит НАША идущая
    # заливка, ворота справедливо сочтут профиль своим, и сцена «держит посторонний»
    # просто не наступит — проверка будет зелёной, ничего не проверив.
    run("UPDATE pub_upload_jobs SET status='done' WHERE release_id<>?", (release_id,))
    keep = "" if keep_mark else ", blocked_since=NULL, blocked_why=NULL"
    run(f"UPDATE pub_upload_jobs SET status='queued', host=NULL, worker=NULL, "
        f"lease_until=NULL, claim_token=NULL{keep} WHERE release_id=?", (release_id,))
    run("UPDATE pub_releases SET status='pending' WHERE release_id=?", (release_id,))


def main() -> int:
    prof = seed()

    print("Профиль открыт посторонним — задание остаётся в очереди")
    open_now({"u46"})
    forget_answers()
    check("задание не выдано", claim(), None)
    a = job("rel-a")
    check("оно всё ещё в очереди", a["status"], "queued")
    check_true("помечено, с какого времени ждёт", a["blocked_since"] is not None)
    check_true("причина сказана словами", "не нами" in (a["blocked_why"] or ""))
    check_true("и записана в журнал выпуска", "открыт на машине «Минская машина»"
               in journal("rel-a"))
    check_true("токен заливки не выдан", a["claim_token"] is None)

    print("Второе задание на тот же занятый профиль — тоже ждёт")
    b = job("rel-b")
    check_true("оба задания помечены", b["blocked_since"] is not None)

    print("Машину не долбим: ответ живёт в кэше")
    was = ADS.calls
    check("первый опрос спросил машину один раз", was, 1)
    for _ in range(5):
        CLOCK["t"] += 60          # часы идут: иначе «переписали заново» не отличить
        claim()
    check("пять опросов подряд — ни одного лишнего вопроса", ADS.calls, was)
    check("метка времени ожидания не переписана заново",
          job("rel-a")["blocked_since"], a["blocked_since"])

    print("Кэш всё-таки протухает — иначе занятость замёрзнет навсегда")
    # Часы выдуманные, и сон их двигает — как настоящий. Сон-пустышка при
    # остановленных часах заставил бы выдержку пауз крутиться вечно, и тест не упал
    # бы, а завис: зависание — это не «красный», его никто не читает как поломку.
    fake = {"t": 1000.0}
    ticking = dict(sleep=lambda s: fake.__setitem__("t", fake["t"] + s),
                   clock=lambda: fake["t"])
    A._reset_active_cache_for_tests()
    A._reset_pace_state_for_tests()
    before = ADS.calls
    A.active_profiles(ADS_URL, **ticking)
    A.active_profiles(ADS_URL, **ticking)
    check("пока свежо — один вопрос", ADS.calls - before, 1)
    fake["t"] += A.ACTIVE_TTL_OK + 1
    A.active_profiles(ADS_URL, **ticking)
    check("протухло — спросили заново", ADS.calls - before, 2)

    print("Профиль освободили — задание уходит в работу")
    open_now(set())
    forget_answers()
    task = claim()
    check("выдано именно ждавшее задание", (task or {}).get("release_id"), "rel-a")
    a = job("rel-a")
    check("пометка ожидания снята", (a["blocked_since"], a["blocked_why"]), (None, None))
    check("задание взято в работу", a["status"], "running")
    check_true("в журнале сказано, что профиль освободился",
               "профиль освободился" in journal("rel-a"))

    print("Наша собственная заливка посторонним не считается")
    # rel-a сейчас льётся на профиле 46, и AdsPower честно показывает его открытым —
    # это МЫ. Второе задание на тот же профиль ждёт очереди, а не чужака.
    open_now({"u46"})
    forget_answers()
    was_said = journal("rel-b")
    check("второе задание не выдано", claim(worker="2"), None)
    b = job("rel-b")
    check("и НЕ помечено как ждущее чужого", (b["blocked_since"], b["blocked_why"]),
          (None, None))
    check("в журнал не добавлено обвинение постороннего", journal("rel-b"), was_said)

    print("AdsPower молчит — заливка НЕ встаёт")
    run("UPDATE pub_upload_jobs SET status='done', finished=? WHERE release_id='rel-a'",
        (CLOCK["t"],))
    ADS.silent = True
    forget_answers()
    task = claim(worker="2")
    check("задание всё равно выдано", (task or {}).get("release_id"), "rel-b")
    b = job("rel-b")
    check("ожиданием это не считается", b["blocked_since"], None)
    check_true("но в журнале сказано прямо",
               "занятость профиля проверить не удалось" in journal("rel-b"))

    print("Неудачу помним дольше удачи — молчащую машину не переспрашиваем зря")
    was = ADS.calls
    run("UPDATE pub_upload_jobs SET status='queued', claim_token=NULL "
        "WHERE release_id='rel-b'")
    claim(worker="3")
    check("второй опрос молчащую машину не трогал", ADS.calls, was)
    ADS.silent = False

    print("Тревога §13: задание стоит из-за чужого профиля дольше получаса")
    # Возвращаем задание в очередь: выданное ничего не ждёт, и §13 про него молчит.
    run("UPDATE pub_upload_jobs SET status='queued', claim_token=NULL, host=NULL, "
        "worker=NULL WHERE release_id='rel-b'")
    open_now({"u46"})
    forget_answers()
    claim(worker="9")
    stuck_at = job("rel-b")["blocked_since"]
    check_true("задание снова ждёт чужого профиля", stuck_at is not None)
    c = conn()
    early = AL.detect(c, now=stuck_at + 29 * 60)
    check("через 29 минут молчим", "upload_profile_busy" in early, False)
    late = AL.detect(c, now=stuck_at + 31 * 60)
    check("через 31 минуту говорим", "upload_profile_busy" in late, True)
    text = late["upload_profile_busy"].text
    check_true("названо, какое задание стоит", "rel-b" in text)
    check_true("сказано, что делать", "закрыть профиль в AdsPower" in text)
    check_true("сказано про деньги", "не стоит" in late["upload_profile_busy"].money)
    c.close()

    print("Порог правится с экрана «Админ»")
    c = conn()
    AL.set_limit(c, "PROFILE_BUSY_SEC", 600, now=CLOCK["t"])
    c.commit()
    check("новый порог применён", AL.PROFILE_BUSY_SEC, 600.0)
    check("теперь хватает одиннадцати минут",
          "upload_profile_busy" in AL.detect(c, now=stuck_at + 11 * 60), True)
    shown = {x["key"]: x for x in AL.limits_state(c)["limits"]}["PROFILE_BUSY_SEC"]
    check("на экране он свой", (shown["own"], shown["value"]), (True, 600.0))
    check("а по умолчанию — полчаса", shown["default"], 1800.0)
    AL.set_limit(c, "PROFILE_BUSY_SEC", None, now=CLOCK["t"])
    c.commit()
    check("возврат к умолчанию", AL.PROFILE_BUSY_SEC, 1800.0)
    c.close()

    print("Тревога звучит один раз и снимается, когда профиль освободили")
    sent = []
    c = conn()
    AL.tick(c, lambda text: (sent.append(text), True)[1], now=stuck_at + 31 * 60)
    mine = [t for t in sent if "чужой профиль AdsPower" in t]
    check("сказано ровно один раз", len(mine), 1)
    check_true("это тревога, а не «починилось»", mine[0].startswith("ТРЕВОГА"))
    run("UPDATE pub_upload_jobs SET blocked_since=NULL, blocked_why=NULL "
        "WHERE release_id='rel-b'")
    sent.clear()
    AL.tick(c, lambda text: (sent.append(text), True)[1], now=stuck_at + 40 * 60)
    fixed = [t for t in sent if "чужой профиль AdsPower" in t]
    check("после освобождения сказано «починилось»", len(fixed), 1)
    check_true("и это именно «починилось»", fixed[0].startswith("ПОЧИНИЛОСЬ"))
    c.close()

    print("Экран: занят / свободен / не знаем — три разных состояния")
    open_now({"u46"})
    forget_answers()
    claim(worker="4")            # ворота спросили машину и записали, кто открыт
    shown = {p["serial"]: p for p in W.ui_profiles()["profiles"]}
    check("открытый профиль — занят", shown["46"]["busy"], "busy")
    check("закрытый — свободен", shown["12"]["busy"], "free")
    check("у свободного нет оговорок", shown["12"]["busy_why"], "")
    run("UPDATE pub_profiles SET active_at=? WHERE serial='12'",
        (time.time() - A.ACTIVE_FRESH_SEC - 60,))
    shown = {p["serial"]: p for p in W.ui_profiles()["profiles"]}
    check("устаревшие сведения — не знаем", shown["12"]["busy"], "unknown")
    check_true("и сказано почему", "не «сейчас»" in shown["12"]["busy_why"])
    run("UPDATE pub_profiles SET active_at=NULL WHERE serial='12'")
    shown = {p["serial"]: p for p in W.ui_profiles()["profiles"]}
    check("никогда не спрашивали — тоже не знаем", shown["12"]["busy"], "unknown")
    check_true("«не знаем» не равно «свободен»", shown["12"]["busy"] != "free")

    print("Молчание машины не стирает того, что мы знали")
    run("UPDATE pub_profiles SET active_now=1, active_at=? WHERE serial='46'",
        (time.time(),))
    ADS.silent = True
    forget_answers()
    claim(worker="5")
    check("занятость не обнулена молчанием",
          {p["serial"]: p["busy"] for p in W.ui_profiles()["profiles"]}["46"], "busy")
    ADS.silent = False

    print("Спрашиваем ту машину, где живёт профиль, а не первую попавшуюся")
    # Профиль 77 живёт на гданьской машине. Если спросить минскую (первую в списке),
    # ответ будет про совсем другие профили — и мы либо придержим свободное задание,
    # либо, что хуже, полезем в занятый профиль.
    run("UPDATE pub_upload_jobs SET status='queued', claim_token=NULL, host=NULL, "
        "worker=NULL WHERE release_id IN ('rel-b','rel-c')")
    open_now({ADS_URL: {"u77"}, ADS2_URL: set()})
    forget_answers()
    claim(worker="6")
    check("профиль свободен на СВОЕЙ машине — задание не придержано",
          job("rel-c")["blocked_since"], None)
    run("UPDATE pub_upload_jobs SET status='queued', claim_token=NULL, host=NULL, "
        "worker=NULL WHERE release_id IN ('rel-b','rel-c')")
    open_now({ADS_URL: set(), ADS2_URL: {"u77"}})
    forget_answers()
    claim(worker="7")
    held = job("rel-c")
    check_true("занят на своей машине — задание придержано", held["blocked_since"])
    check_true("и названа именно та машина",
               "Гданьская машина" in (held["blocked_why"] or ""))

    print("База без новой колонки не роняет весь проход тревог")
    # Процесс тревог ходит в базу своим таймером и схему не создаёт: он может прийти
    # раньше, чем служба кабинета применит миграцию. Один отсутствующий столбец не
    # должен выключить ВСЕ поводы разом.
    old_db = sqlite3.connect(":memory:")
    old_db.executescript(
        "CREATE TABLE pub_upload_jobs(id INTEGER PRIMARY KEY, tenant_id TEXT, "
        "status TEXT, created REAL, finished REAL);")
    check("на старой схеме повод молчит и не падает",
          AL.check_upload_profile_busy(old_db, time.time()), [])
    old_db.close()

    print("Экран нарисован тремя состояниями, а не галочкой")
    page = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    check_true("состояния заведены отдельной таблицей", "const PROFILE_BUSY" in page)
    for word in ("занят", "свободен", "не знаем"):
        check_true(f"на карточке есть «{word}»", f"title: '{word}'" in page)

    print("Молчание AdsPower не выдаётся за освобождение")
    # Разбор 30.09.2026. Раньше в учёт приходила ОДНА строка «занят», и любое незнание —
    # упавший AdsPower, неопрошенная машина, несверённый профиль — выглядело как
    # освобождение: пометка снималась, в журнал шло «профиль освободился», а тревога
    # уходила владельцу как «ПОЧИНИЛОСЬ». То есть обрыв связи объявлял сам себя
    # починкой ровно в ту минуту, когда посторонний держал профиль.
    revive("rel-a")
    forget_answers()
    open_now({"u46"})
    claim()
    check_true("посторонний держит — пометка стоит",
               job("rel-a")["blocked_since"] is not None)
    said_before = journal("rel-a")

    ADS.silent = True
    forget_answers()
    try:
        claim()
    finally:
        ADS.silent = False
    tail = journal("rel-a")[len(said_before):]
    check("«освободился» при молчании в журнал не писали", "освободил" in tail, False)
    check_true("зато сказано, что спросить не удалось",
               "проверить не удалось" in tail)
    given = job("rel-a")
    check_true("и задание всё-таки выдано: незнание заливку не останавливает",
               given["status"] == "running")
    # Пометка означает «стоит из-за чужого профиля». Выданное задание не стоит, и
    # оставленная пометка врала бы на экране, а вернувшись в очередь — и тревогой.
    check("на выданном задании пометки ожидания нет", given["blocked_since"], None)

    print("А честный ответ «свободен» пометку снимает и говорит об этом")
    revive("rel-a")
    forget_answers()
    open_now({"u46"})
    claim()                                   # снова придержали
    said_before = journal("rel-a")
    forget_answers()
    open_now(set())                           # посторонний закрыл профиль
    claim()
    check("пометка снята", job("rel-a")["blocked_since"], None)
    check("и это сказано в журнале",
          "освободил" in journal("rel-a")[len(said_before):], True)

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
