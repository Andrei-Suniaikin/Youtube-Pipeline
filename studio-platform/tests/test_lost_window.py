#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Окно потеряно — залить заново» (17.10.2026).

Выпуск, у которого загрузка начиналась, но номера ролика нет, а окно заливки на машине
закрыто: обычный возврат запрещён, подтвердить руками нечем. Проверяем в обе стороны —
что действие работает там, где должно, и НЕ работает там, где за ним второй ролик.

    .venv/bin/python tests/test_lost_window.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.abspath(os.path.join(HERE, "..")), "server"))

import pub_engine                                                  # noqa: E402
import pub_schema                                                  # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    return c


def release(c, n, *, job_status="repair", job_video=None, claim="tok"):
    uc = "UC" + str(n) * 22
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) VALUES('t1',?,?,'ru','Europe/Warsaw','{}',1,0,?,?)",
              (uc, f"Канал {n}", NOW, NOW))
    ch = c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_video_id,donor_title,slot_at,status,created,updated) "
              "VALUES(?,'t1',?,1,?,?,'Донор',?,'error',?,?)",
              (n, ch, f"https://youtu.be/donor{n}aaaaa", f"donor{n}aaaaa", NOW + 3600, NOW, NOW))
    rid = f"rel-{n}"
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,blocked_reason,created,updated) "
              "VALUES(?,'t1',?,1,?,?,?,?,'blocked','upload_running',"
              "'UPLOAD_IN_PROGRESS_PROFILE_RETAINED: …',?,?)",
              (rid, n, ch, uc, NOW + 3600, f"p{n}", NOW, NOW))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
              "title,video_path,slot_at,status,phase,video_id,claim_token,created,updated) "
              "VALUES(?,'t1',?,?,'http://c','Ролик','/v.mp4',?,?,?,?,?,?,?)",
              (rid, ch, uc, NOW + 3600, job_status,
               json.dumps({"upload_phase_schema_version": 1, "phase": "upload_started", "video_id": job_video or ""}),
               job_video, claim, NOW, NOW))
    for seq, ph in enumerate(pub_engine.PHASES[:pub_engine.PHASE_INDEX["upload_running"] + 1], start=1):
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,created,repeats,last_seen) "
                  "VALUES(?,?,?,?,1,?)", (rid, seq, ph, NOW - 3600, NOW - 3600))
    c.commit()
    return rid


def rel(c, rid):
    return c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone()


def main() -> int:
    print("Окно потеряно: загрузка начиналась, номера нет, задание в repair")
    c = fresh()
    rid = release(c, 1)
    check("до кнопки выпуск необратим", pub_engine.reached_irreversible(c, rid), True)
    check("обычный возврат отказывает", pub_engine.revive(c, rid, NOW)["ok"], False)
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("«окно потеряно» принято", out["ok"], True)
    r = rel(c, rid)
    check("выпуск снова в работе", r["status"], "pending")
    check("с фазы «обложка готова» — комплект соберётся заново", r["phase"], "cover_ready")
    check("старое задание убрано", c.execute("SELECT COUNT(*) FROM pub_upload_jobs WHERE release_id=?", (rid,)).fetchone()[0], 0)
    check("после отметки выпуск больше не необратим", pub_engine.reached_irreversible(c, rid), False)
    hist = pub_engine.phase_history(c, rid)
    check("отметка lost_window в истории фаз есть", "lost_window" in hist, True)
    msgs = " | ".join(m[0] for m in c.execute("SELECT message FROM pub_release_log WHERE release_id=?", (rid,)))
    check("в журнале сказано, что человек разрешил новую заливку", "разрешил новую заливку" in msgs, True)

    print("\nЗащиты: где за кнопкой второй ролик, она не работает")
    c = fresh()
    rid = release(c, 2, job_video="Abc-123_xyz")
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("есть номер ролика — отказ", out["ok"], False)
    check("и сказано идти в «подтвердить руками»", "Подтвердить руками" in out["why"], True)
    check("выпуск не тронут", rel(c, rid)["status"], "blocked")

    c = fresh()
    rid = release(c, 3, job_status="running")
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("машина держит задание — отказ", out["ok"], False)
    check("выпуск не тронут", rel(c, rid)["status"], "blocked")

    c = fresh()
    rid = release(c, 4)
    c.execute("UPDATE pub_releases SET hand_confirmed=? WHERE release_id=?", (NOW, rid)); c.commit()
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("подтверждённый руками — отказ", out["ok"], False)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
