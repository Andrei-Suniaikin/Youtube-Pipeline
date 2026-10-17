#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Доведение черновика по номеру машиной (18.10.2026, третий шаг «скрипта удержанных окон»).

Ролик залит, номер известен, вердикта «в расписании» нет. Такой выпуск раньше ждал
человека. Теперь задание в `repair` с номером выдаётся машине как ЗАДАЧА ДОВЕДЕНИЯ.
Проверяется:
  • выдача: поля тела (`finalize`, `finalize_video_id`, пустой отпечаток файла), файл
    ролика не отдаётся (409), обложка отдаётся; не выдаётся без номера, подтверждённому
    руками, без обложки, заданию в `error`, при занятом профиле, чаще раза в 6 часов;
  • отказ возвращает задание в `repair` с номером и счётом попыток; после третьего —
    причина «доведение по номеру не удалось 3 раза»; четвёртой выдачи нет;
  • вердикт scheduled закрывает задание и поднимает выпуск из разбора;
  • выход прошёл — переносится на ближайший свободный, как у очереди; некуда — задание
    держится с одной строкой в журнале;
  • do_scheduled ждёт, пока задание доводится, и падает словами, когда доводить нечем;
  • молчание машины во время доведения возвращает задание в `repair`, не в `error`,
    а поздний успех после молчания всё равно закрывает выпуск.

    VS_ALERTS=0 .venv/bin/python tests/test_finalize_by_video_id.py
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="finalize-by-id-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    project_id TEXT, kind TEXT, status TEXT, created REAL, finished REAL);
""")
_c.commit(); _c.close()

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

import pub_adspower                                                # noqa: E402
import pub_engine as E                                             # noqa: E402
import pub_services as S                                           # noqa: E402
import pub_upload_api as U                                         # noqa: E402

TOKEN = "upl_test"
CLOCK = {"t": time.time()}
OPEN = {"uids": set()}            # кто открыт на машине посторонним
FAILS = []
UC = {k: "UC" + k * 22 for k in "abcdefghij"}
COVER = os.path.join(TMP, "cover.jpg")
with open(COVER, "wb") as _f:
    _f.write(b"\xff\xd8\xff" + b"0" * 64)

# AdsPower не трогаем: отвечаем «знаем, открыты вот эти».
pub_adspower.active_profiles = lambda url, **kw: pub_adspower.HostActivity(
    True, frozenset(OPEN["uids"]), "", CLOCK["t"])


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    return c


app = FastAPI()
U.register_upload_routes(app, {"db": conn, "_now": lambda: CLOCK["t"], "upload_token": lambda: TOKEN})
client = TestClient(app, raise_server_exceptions=False)
H = {"X-Upload-Token": TOKEN}

FAR_ISO = "2026-12-01T12:00:00+01:00"
FAR_AT = dt.datetime.fromisoformat(FAR_ISO).timestamp()

N = {"i": 0}


def seed_base():
    c = conn()
    c.execute("INSERT INTO pub_hosts(id,tenant_id,code,title,adspower_url,status,created) "
              "VALUES(1,'t1','minsk','Минск','http://127.0.0.1:50325','active',?)", (CLOCK["t"],))
    c.execute("INSERT INTO pub_profiles(id,tenant_id,serial,title,default_host_id,status,created,"
              "adspower_user_id) VALUES(7,'t1','28','П28',1,'active',?,'u28')", (CLOCK["t"],))
    # Канал 1 — с расписанием (каждый день 12:00), канал 2 — без расписания.
    c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,lang,tz,schedule,deferred,deferred_limit,"
              "horizon_days,profile_id,created,updated) VALUES(1,'t1',?,'Канал 1','pl','Europe/Warsaw',"
              "?,1,0,3,7,?,?)", (UC["a"], json.dumps({"*": ["12:00"]}), CLOCK["t"], CLOCK["t"]))
    c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,lang,tz,schedule,deferred,deferred_limit,"
              "horizon_days,profile_id,created,updated) VALUES(2,'t1',?,'Канал 2','pl','Europe/Warsaw',"
              "'{}',1,0,3,7,?,?)", (UC["b"], CLOCK["t"], CLOCK["t"]))
    c.commit(); c.close()


def release(rid, uc, *, job_status="repair", video_id="Vid00000001", rel_status="blocked",
            hand=False, cover=COVER, slot_at=None, schedule_iso=None, channel=1,
            tries=None, last=None, error="SCHEDULED, но время в подтверждении не совпало с заданием",
            claim=None):
    N["i"] += 1
    n = N["i"]
    # Выход у каждого свой: (канал, выход) в плане уникальны.
    if slot_at is None:
        slot_at = FAR_AT + n * 86400
    if schedule_iso is None:
        schedule_iso = dt.datetime.fromtimestamp(slot_at, tz=ZoneInfo("Europe/Warsaw")).isoformat()
    c = conn()
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,donor_video_id,"
              "donor_title,slot_at,status,created,updated) VALUES(?,'t1',?,1,'u','v','Д',?,'error',?,?)",
              (n, channel, slot_at, CLOCK["t"], CLOCK["t"]))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,channel_id,"
              "uc,slot_at,project_id,status,phase,attempts,deadline,blocked_reason,created,updated,"
              "hand_confirmed) VALUES(?,?,?,1,?,?,?,?,?,'upload_running',0,?,?,?,?,?)",
              (rid, "t1", n, channel, uc, slot_at, f"p{n}", rel_status, CLOCK["t"] + 1800,
               error if rel_status == "blocked" else None, CLOCK["t"], CLOCK["t"],
               CLOCK["t"] if hand else None))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,host_id,"
              "channel_url,title,description,schedule_iso,visibility,video_path,video_sha256,cover_path,"
              "slot_at,status,verdict,video_id,error,finalize_tries,finalize_last,claim_token,claimed,"
              "created,updated) VALUES(?,?,?,?,7,1,?,?,'',?,'public','/v.mp4','ab'||'cd',?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "t1", channel, uc, f"https://studio.youtube.com/channel/{uc}/", f"ролик {rid}",
               schedule_iso, cover, slot_at, job_status, "needs_repair" if video_id else None,
               video_id or None, error, tries, last, claim, CLOCK["t"] - 1000 if claim else None,
               CLOCK["t"] - 1000, CLOCK["t"] - 1000))
    c.commit(); c.close()
    return rid


def job(rid):
    c = conn()
    try:
        return dict(c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?", (rid,)).fetchone())
    finally:
        c.close()


def rel(rid):
    c = conn()
    try:
        return c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone()
    finally:
        c.close()


def journal(rid):
    c = conn()
    try:
        return [m[0] for m in c.execute("SELECT message FROM pub_release_log WHERE release_id=? ORDER BY id", (rid,))]
    finally:
        c.close()


def claim():
    r = client.post("/api/upload/claim", json={"host": "minsk", "worker": "1"}, headers=H)
    return r.json().get("task")


def result(rid, token, body):
    return client.post(f"/api/upload/{rid}/result", json=body,
                       headers={**H, "X-Upload-Claim": token})


def fail_body(err="strict_finalize_not_found: expected exactly one known video ID; no file upload"):
    return {"result": {"success": False, "video_id": "Vid00000001", "do_not_reupload": True,
                       "phase": "video_id_known", "error": err}}


def ok_body(video_id, uc, iso):
    return {"result": {"success": True, "video_id": video_id,
                       "exact_studio_evidence": {"studio_state": "SCHEDULED", "row_video_id": video_id,
                                                 "verified_channel_uc": uc,
                                                 "scheduled_time_seconds": int(dt.datetime.fromisoformat(iso).timestamp())}}}


class Svc:
    """Настоящий do_scheduled без остального PubServices."""
    _upload_job = S.PubServices._upload_job
    do_scheduled = S.PubServices.do_scheduled


def scheduled(rid):
    c = conn()
    try:
        return Svc().do_scheduled(c, rel(rid))
    except E.ItemFailed as exc:
        return f"ItemFailed: {exc}"
    finally:
        c.close()


def main() -> int:
    seed_base()
    print("Кого НЕ выдаём на доведение (эти заведены раньше — очередь дошла бы до них первыми)")
    release("rel-novid", UC["c"], video_id="")                       # номера нет
    release("rel-hand", UC["d"], hand=True)                          # подтверждён руками
    release("rel-nocover", UC["e"], cover="")                        # обложки нет — строгий режим не стартует
    release("rel-error", UC["f"], job_status="error")                # вердикт failed — решает человек
    release("rel-cancel", UC["g"], rel_status="cancelled")           # выпуск отменён
    release("rel-fin", UC["a"])
    OPEN["uids"] = {"u28"}
    check("профиль открыт посторонним — доведение не выдаётся", claim(), None)
    OPEN["uids"] = set()

    print("\nВыдача задачи доведения")
    t = claim()
    check("выдан именно выпуск с номером, обложкой и живым заданием", (t or {}).get("release_id"), "rel-fin")
    check("в теле — признак доведения и номер", (t.get("finalize"), t.get("finalize_video_id")), (True, "Vid00000001"))
    check("отпечатка файла нет — машина файл не качает", t.get("video_sha256"), "")
    check("время выхода и профиль — как у обычного задания", (t.get("schedule"), t.get("profile")),
          (job("rel-fin")["schedule_iso"], 28))
    j = job("rel-fin")
    check("задание running с клеймом, попытка 1, время попытки записано",
          (j["status"], bool(j["claim_token"]), j["finalize_tries"], j["finalize_last"] == CLOCK["t"]),
          ("running", True, 1, True))
    tok = j["claim_token"]
    check("второй опрос — то же задание (ответ не доехал)", (claim() or {}).get("release_id"), "rel-fin")
    r = client.get("/api/upload/rel-fin/file", params={"kind": "video"}, headers={**H, "X-Upload-Claim": tok})
    check("файл ролика НЕ отдаётся", r.status_code, 409)
    r = client.get("/api/upload/rel-fin/file", params={"kind": "cover"}, headers={**H, "X-Upload-Claim": tok})
    check("обложка отдаётся", r.status_code, 200)
    r = client.post("/api/upload/rel-fin/progress", json={"phase": json.dumps(
        {"upload_phase_schema_version": 1, "phase": "video_id_known", "video_id": "Vid00000001"})},
        headers={**H, "X-Upload-Claim": tok})
    check("фаза принимается как обычно", r.status_code, 200)
    check("в журнале сказано про доведение", any("ДОВЕДЕНИЕ" in m for m in journal("rel-fin")), True)
    check("выпуск в разборе, пока машина доводит, движок ждёт", (rel("rel-fin")["status"], scheduled("rel-fin")),
          ("blocked", {"wait": True}))

    print("\nОтказ возвращает в repair с номером; не чаще раза в 6 часов; три попытки")
    r = result("rel-fin", tok, fail_body())
    check("вердикт принят", (r.status_code, r.json().get("verdict")), (200, "needs_repair"))
    j = job("rel-fin")
    check("статус repair, номер на месте, клейм снят", (j["status"], j["video_id"], j["claim_token"]),
          ("repair", "Vid00000001", None))
    check("причина — со счётом попыток", "попытка 1 из 3" in (j["error"] or ""), True)
    check("выпуск остался в разборе", rel("rel-fin")["status"], "blocked")
    check("сразу второй раз — не выдаётся", claim(), None)
    CLOCK["t"] += 6 * 3600 - 60
    check("через 5 ч 59 мин — ещё нет", claim(), None)
    CLOCK["t"] += 120
    t = claim()
    check("через 6 часов — вторая попытка", ((t or {}).get("release_id"), job("rel-fin")["finalize_tries"]), ("rel-fin", 2))
    result("rel-fin", t["claim_token"], {"result": {"success": False, "error": "LEASE_LOST: бронь потеряна"}})
    j = job("rel-fin")
    check("отказ без номера в ответе номер не стирает и в error не уводит", (j["status"], j["video_id"]),
          ("repair", "Vid00000001"))
    CLOCK["t"] += 6 * 3600 + 1
    t = claim()
    check("третья попытка выдана", ((t or {}).get("release_id"), job("rel-fin")["finalize_tries"]), ("rel-fin", 3))
    result("rel-fin", t["claim_token"], fail_body("strict_existing_blocker: PRIVATE"))
    j = job("rel-fin")
    check("после третьей — причина человеку", (j["error"] or "").startswith("доведение по номеру не удалось 3 раза"), True)
    check("и исходная причина в ней сохранена", "PRIVATE" in (j["error"] or ""), True)
    CLOCK["t"] += 6 * 3600 + 1
    check("четвёртой выдачи нет", claim(), None)
    check("движок больше не ждёт: отказ словами", scheduled("rel-fin"), f"ItemFailed: {j['error']}")

    print("\nУспех: вердикт scheduled закрывает задание и поднимает выпуск")
    release("rel-ok", UC["h"], video_id="Vid00000002")
    t = claim()
    check("выдано доведение rel-ok", (t or {}).get("release_id"), "rel-ok")
    r = result("rel-ok", t["claim_token"], ok_body("Vid00000002", UC["h"], job("rel-ok")["schedule_iso"]))
    check("вердикт scheduled", r.json().get("verdict"), "scheduled")
    j = job("rel-ok")
    check("задание done с номером", (j["status"], j["verdict"], j["video_id"]), ("done", "scheduled", "Vid00000002"))
    check("выпуск поднят из разбора", rel("rel-ok")["status"], "running")
    out = scheduled("rel-ok")
    check("движок видит подтверждение и идёт дальше", out.get("video_id") if isinstance(out, dict) else out, "Vid00000002")
    check("в журнале — «доведён до расписания по номеру»", any("доведён до расписания" in m for m in journal("rel-ok")), True)

    print("\nВыход прошёл: переносим на ближайший свободный, некуда — держим с одной строкой в журнале")
    release("rel-late", UC["a"], video_id="Vid00000003", slot_at=CLOCK["t"] - 100,
            schedule_iso="2026-01-01T12:00:00+01:00", channel=1)
    t = claim()
    j = job("rel-late")
    check("выдано с новым временем выхода", ((t or {}).get("release_id"), t.get("schedule") == j["schedule_iso"]),
          ("rel-late", True))
    check("новый выход впереди с запасом", j["slot_at"] > CLOCK["t"] + 2400, True)
    check("выпуск и строка плана переехали", (rel("rel-late")["slot_at"], ), (j["slot_at"],))
    result("rel-late", t["claim_token"], ok_body("Vid00000003", UC["a"], j["schedule_iso"]))
    check("сверка времени идёт по новому выходу — scheduled", job("rel-late")["verdict"], "scheduled")
    release("rel-noslot", UC["b"], video_id="Vid00000004", slot_at=CLOCK["t"] - 100,
            schedule_iso="2026-01-01T12:00:00+01:00", channel=2)
    check("канал без расписания — доведение не выдаётся", claim(), None)
    claim()
    j = job("rel-noslot")
    check("задание осталось в repair с номером и пометкой", (j["status"], j["video_id"], j["blocked_why"] == U.FINALIZE_HOLD_SLOT),
          ("repair", "Vid00000004", True))
    check("в журнале об этом одна строка, не на каждый опрос",
          sum(1 for m in journal("rel-noslot") if m == U.FINALIZE_HOLD_SLOT), 1)

    print("\ndo_scheduled: ждёт, пока есть чем доводить; падает словами, когда нечем")
    release("rel-wait", UC["i"], video_id="Vid00000005", rel_status="running", channel=2)
    check("repair с номером и обложкой, попыток 0 — ждём", scheduled("rel-wait"), {"wait": True})
    release("rel-nocov2", UC["j"], video_id="Vid00000006", rel_status="running", cover="", channel=2)
    out = scheduled("rel-nocov2")
    check("без обложки — отказ с объяснением", out.startswith("ItemFailed:") and "обложки" in out, True)
    out = scheduled("rel-novid")
    check("без номера — прежний отказ текстом задания", out, "ItemFailed: SCHEDULED, но время в подтверждении не совпало с заданием")

    print("\nМолчание машины при доведении — обратно в repair, поздний успех закрывает")
    c = conn()
    c.execute("UPDATE pub_upload_jobs SET status='running', claim_token='tok-w', claimed=?, finalize_tries=1, "
              "finalize_last=?, lease_until=? WHERE release_id='rel-wait'", (CLOCK["t"], CLOCK["t"], CLOCK["t"] + 10))
    c.commit()
    swept = U.sweep_stuck(c, CLOCK["t"] + 20)
    c.commit(); c.close()
    j = job("rel-wait")
    check("снято по сроку в repair, номер и клейм на месте", (swept, j["status"], j["video_id"], j["claim_token"]),
          (["rel-wait"], "repair", "Vid00000005", "tok-w"))
    check("причина — про молчание при доведении", "молчит" in (j["error"] or "") and "попытка 1 из 3" in j["error"], True)
    r = result("rel-wait", "tok-w", ok_body("Vid00000005", UC["i"], job("rel-wait")["schedule_iso"]))
    check("поздний успех после молчания принят", (r.status_code, r.json().get("verdict")), (200, "scheduled"))
    check("задание закрыто", job("rel-wait")["status"], "done")
    r = result("rel-wait", "tok-w", fail_body())
    j = job("rel-wait")
    check("ещё более поздний отказ итог не трогает (клейм закрытого задания уже не свой)",
          (r.status_code, j["status"], j["verdict"]), (403, "done", "scheduled"))

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
