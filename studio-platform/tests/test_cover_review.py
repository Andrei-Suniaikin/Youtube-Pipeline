#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложка выпуска глазами человека: что сказала сверка, «взять эту», «перезаказать».

04.10.2026, владелец: «как мне понять, какие обложки не прошли проверку». Обложку выпуска
заказывает движок под ключом студии; во вкладке «Обложка» проекта её не видно. На
странице выпуска теперь — сама картинка, вердикт сверки словами и два решения.

Что проверяется:
  • сервис обложек отдаёт строки журнала, объясняющие итог (check_notes);
  • вердикт: «не успели сверить» (в т.ч. у задач до 04.10 — по журналу), «сверка не
    приняла», «картинки нет», «прошла»;
  • «взять эту»: файл — в проект, задача обложки закрыта, выпуск в работе, в журнале —
    кто взял; движок потом НЕ спрашивает старую задачу и НЕ перезаказывает;
  • «перезаказать»: старая задача забыта, выпуск в работе — движок закажет новую;
  • с выпуском не в разборе оба решения отказывают.

    .venv/bin/python tests/test_cover_review.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="cover-review-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path[:0] = [os.path.join(BASE, "server"), os.path.join(BASE, "web"),
                os.path.join(BASE, "cover"), os.path.join(BASE, "shared")]

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
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
import pub_services as S                                          # noqa: E402
from fastapi import HTTPException                                 # noqa: E402

FAILS = []
NOW = 1_800_000_000.0
PDIR = os.path.join(TMP, "p1")
os.makedirs(PDIR, exist_ok=True)


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


fake_main = types.ModuleType("main")
fake_main._project_dir = lambda tenant, pid: PDIR
sys.modules["main"] = fake_main


class FakeCover:
    info = {}
    data = b"\xff\xd8" + b"x" * 9000
    calls = []

    def __init__(self, key):
        pass

    def job(self, jid):
        FakeCover.calls.append(("job", jid))
        return dict(FakeCover.info)

    def download(self, jid, lang):
        FakeCover.calls.append(("download", jid, lang))
        return FakeCover.data

    def create_job(self, preset, lang, url):
        FakeCover.calls.append(("create", url))
        return "new-1"


W.CoverClient = FakeCover
W.get_key = lambda c, name: "k"


class Req:
    class state:
        user = {"name": "Андрей"}


def seed(status="blocked", reason="обложка деградировала дважды: текст наложен поверх картинки"):
    c = W.db()
    c.execute("DELETE FROM pub_releases"); c.execute("DELETE FROM pub_release_jobs")
    c.execute("DELETE FROM pub_plan_items"); c.execute("DELETE FROM pub_channels")
    c.execute("DELETE FROM pub_release_log")
    c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,lang,tz,schedule,created,updated) "
              "VALUES(1,'t1','UC1','Канал','pl','Europe/Warsaw','{}',?,?)", (NOW, NOW))
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,donor_url,slot_at,status,"
              "created,updated) VALUES(1,'t1',1,'https://youtu.be/x',?,'error',?,?)",
              (NOW + 86400, NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,blocked_reason,created,updated) "
              "VALUES('r1','t1',1,1,1,'UC1',?,'p1',?,'media_ready',?,?,?)",
              (NOW + 86400, status, reason if status == "blocked" else None, NOW, NOW))
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,error,"
              "created,updated) VALUES('r1','cover','job','cj1','error','re-ordered-degraded',?,?)",
              (NOW, NOW))
    c.commit()
    c.close()
    try:
        os.remove(os.path.join(PDIR, "cover.jpg"))
    except OSError:
        pass


def main() -> int:
    print("Сервис обложек объясняет итог строками журнала")
    import api_cover as A
    log = ("📌 Ссылка 1/1\n✅ Проверка текста (попытка 1/2)...\n"
           "   ❌ Проблема: EKSKLUZYWNY -> EKSKLUZYWNE\n"
           "   ⚠ Попытка 2/2: Fast Gen не отдал картинку — Generation failed\n💾 Сохранено")
    notes = A.check_notes_of(log)
    check("взяты только строки итога", notes,
          ["❌ Проблема: EKSKLUZYWNY -> EKSKLUZYWNE",
           "⚠ Попытка 2/2: Fast Gen не отдал картинку — Generation failed"])

    print("\nВердикт словами")
    f = "a/pl_x.jpg"
    check("сверка прошла", W._cover_verdict({"output_files": [f]}), "ok")
    check("не успели сверить (новые задачи)",
          W._cover_verdict({"output_files": [f], "fallback_files": [f], "unchecked_files": [f]}),
          "unchecked")
    check("не успели сверить (задачи до 04.10 — по журналу)",
          W._cover_verdict({"output_files": [f], "fallback_files": [f],
                            "check_notes": ["⚠ проверка текста не выполнена: TimeoutError"]}),
          "unchecked")
    check("сверка не приняла",
          W._cover_verdict({"output_files": [f], "fallback_files": [f],
                            "check_notes": ["❌ Проблема: EKSKLUZYWNY -> EKSKLUZYWNE"]}),
          "rejected")
    check("картинки нет", W._cover_verdict({"output_files": []}), "nothing")

    print("\nЭкран выпуска получает обложку и вердикт")
    seed()
    FakeCover.info = {"status": "completed_partial", "output_files": [f], "fallback_files": [f],
                      "check_notes": ["❌ Проблема: EKSKLUZYWNY -> EKSKLUZYWNE"]}
    d = W.ui_release_cover("r1")
    check("вердикт", d["verdict"], "rejected")
    check("строки сверки", d["notes"], ["❌ Проблема: EKSKLUZYWNY -> EKSKLUZYWNE"])
    check("адрес картинки пульта", d["image"], "/ui/releases/r1/cover/image")
    check("выпуск в разборе — решения доступны", d["blocked"], True)
    img = W.ui_release_cover_image("r1")
    check("картинка отдаётся", (img.media_type, len(img.body)), ("image/jpeg", 9002))
    check("язык канала — для скачивания", FakeCover.calls[-1], ("download", "cj1", "pl"))

    print("\n«Взять эту обложку»")
    out = W.ui_release_cover_accept("r1", Req())
    c = W.db()
    rel = c.execute("SELECT status, blocked_reason FROM pub_releases").fetchone()
    job = c.execute("SELECT status, error FROM pub_release_jobs").fetchone()
    log = [r[0] for r in c.execute("SELECT message FROM pub_release_log")]
    c.close()
    check("выпуск в работе", (out["ok"], rel["status"], rel["blocked_reason"]), (True, "pending", None))
    check("файл в проекте", os.path.getsize(os.path.join(PDIR, "cover.jpg")), 9002)
    check("задача обложки закрыта и подписана", (job["status"], job["error"]),
          ("done", "принята человеком: Андрей"))
    check("в журнале — кто взял", any("обложку взял человек (Андрей)" in m for m in log), True)

    print("\n  движок потом не спрашивает старую задачу и не перезаказывает")
    FakeCover.calls = []
    svc = S.PubServices({"create_project": None, "enqueue": None,
                         "project_dir": lambda *a, **k: PDIR})
    was = (S.CoverClient, S.get_key)
    S.CoverClient, S.get_key = FakeCover, (lambda c, n: "k")
    try:
        c = W.db()
        res = svc.do_cover_ready(c, {"release_id": "r1", "tenant_id": "t1", "project_id": "p1",
                                     "lang": "pl", "preset_cover": "обложки",
                                     "donor_url": "https://youtu.be/x", "updated": NOW})
        c.close()
    finally:
        S.CoverClient, S.get_key = was
    check("обложка принята как есть", res, {"payload": {"cover_bytes": 9002}})
    check("к сервису обложек не ходили", FakeCover.calls, [])

    print("\n«Перезаказать»")
    seed()
    out = W.ui_release_cover_reorder("r1", Req())
    c = W.db()
    left = c.execute("SELECT COUNT(*) FROM pub_release_jobs WHERE service='cover'").fetchone()[0]
    rel = c.execute("SELECT status FROM pub_releases").fetchone()[0]
    log = [r[0] for r in c.execute("SELECT message FROM pub_release_log")]
    c.close()
    check("старая задача забыта", left, 0)
    check("выпуск в работе — движок закажет новую", (out["ok"], rel), (True, "pending"))
    check("в журнале — кто перезаказал", any("перезаказал человек (Андрей)" in m for m in log), True)

    print("\nВыпуск не в разборе — решения отказывают")
    seed(status="running")
    for name, fn in (("взять", W.ui_release_cover_accept), ("перезаказать", W.ui_release_cover_reorder)):
        try:
            fn("r1", Req())
            got = "прошло"
        except HTTPException as exc:
            got = exc.status_code
        check(f"«{name}» — 409", got, 409)
    check("и файл не положен", os.path.exists(os.path.join(PDIR, "cover.jpg")), False)
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f_ in FAILS:
            print("  -", f_)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
