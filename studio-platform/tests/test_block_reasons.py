#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Причина остановки словами: чья беда и что делать (03.10.2026).

Владелец: «нужно чётко показывать, что не выбран пресет обложек» и «нужно чётко
показывать, что проблема в доноре, чтобы человек понимал». Экран печатал причину как есть
— «у ролика нет субтитров (VideoUnplayable)» — и ставил рядом «Вернуть в работу», которая
дала бы тот же отказ.

Проверяется: перевод каждой причины, которую движок пишет на самом деле (строки взяты из
базы VPS 03.10); незнакомая причина не выдумывается; выпуск без пресета обложек сам
возвращается в работу, когда пресет выбран, — и только он; сводка говорит теми же словами.

    .venv/bin/python tests/test_block_reasons.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import block_reasons as B                                         # noqa: E402
import pub_digest                                                 # noqa: E402
import pub_engine                                                 # noqa: E402
import pub_schema                                                 # noqa: E402

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


def release(c, n, reason, status="blocked", phase="media_ready", preset_cover=""):
    uc = "UC" + str(n) * 22
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,preset_cover,created,updated) "
              "VALUES('t1',?,?,'pl','Europe/Warsaw','{}',1,0,?,?,?)",
              (uc, f"Канал {n}", preset_cover, NOW, NOW))
    ch = c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_video_id,donor_title,slot_at,status,created,updated) "
              "VALUES(?,'t1',?,1,?,?,'Донор',?,'error',?,?)",
              (n, ch, f"https://youtu.be/donor{n}aaaaa", f"donor{n}aaaaa",
               NOW + 86400, NOW, NOW))
    rid = f"rel-{n}"
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
              "blocked_reason,created,updated,attempts) "
              "VALUES(?,'t1',?,1,?,?,?,?,?,?,?,?,?,2)",
              (rid, n, ch, uc, NOW + 86400, f"p{n}", status, phase, reason, NOW, NOW))
    c.commit()
    return rid, ch


def main() -> int:
    print("Пресет обложек — «настройка канала», и сказано, где чинить")
    b = B.explain("у канала не выбран пресет обложек")
    check("чья беда", (b["kind"], b["tag"]), ("channel", "Настройка канала"))
    check("что делать — настройки канала", "настройках канала" in b["fix"], True)
    check("и что выпуски вернутся сами", "вернутся в работу сами" in b["fix"], True)
    check("возврат в работу имеет смысл", b["revive"], True)
    check("строка для сводки", b["short"],
          "настройка канала: у канала не выбран пресет обложек — выбрать в настройках канала")

    print("\nДонор — «проблема в доноре», имя исключения переведено")
    реальные = {   # дословно из pub_releases.blocked_reason на VPS 03.10.2026
        "у ролика нет субтитров (VideoUnplayable)": "ролик удалён или закрыт автором",
        "у ролика нет субтитров (TranscriptsDisabled)": "автор ролика отключил субтитры",
        "у ролика нет субтитров (NoTranscriptFound)": "у ролика нет субтитров ни на одном языке",
        "у ролика нет субтитров (AgeRestricted)":
            "у ролика ограничение по возрасту — YouTube не отдаёт его субтитры",
        "у ролика нет субтитров": "у ролика нет субтитров",
        "у ролика нет субтитров (ЧтоТоНовое)": "у ролика нет субтитров",
        "субтитры донора слишком короткие или пустые":
            "у ролика только обрывок субтитров — текста на выпуск не хватит",
        "не разобрал ссылку на ролик: https://example.com/x": "ссылка не ведёт на ролик YouTube",
    }
    for сырая, смысл in реальные.items():
        b = B.explain(сырая)
        check(f"«{сырая[:44]}» → донор", b.get("kind"), "donor")
        check("   и смысл словами", b.get("title"), смысл)
    b = B.explain("у ролика нет субтитров (VideoUnplayable)")
    check("имя исключения в заголовок не попало", "VideoUnplayable" in b["title"], False)
    check("сказано, что это не сбой студии", "не сбой студии" in b["what"], True)
    check("и что делать: другой ролик", "другой ролик-донор" in b["fix"], True)
    check("«вернуть в работу» не предлагается", b["revive"], False)
    check("строка для сводки",
          b["short"], "проблема в доноре: ролик удалён или закрыт автором — нужен другой ролик")

    print("\nНезнакомое не выдумываем")
    for сырая in ("", None, "зависло на шаге «upload_running» и попытки исчерпаны",
                  "ждём: очередь не приняла этап «render»",
                  "рерайт не справился: 1 из 1 задач не выполнено",
                  # «нет субтитров» внутри чужой фразы — не наша формулировка донора
                  "YouTube не отдал субтитры (VideoUnplayable), но ролик на месте"):
        check(f"«{str(сырая)[:40]}» → пусто", B.explain(сырая), {})

    print("\nПресет выбран — выпуски возвращаются сами, и только они")
    c = fresh()
    без, ch_без = release(c, 1, B.COVER_PRESET_MISSING)
    выбран, ch_выбран = release(c, 2, B.COVER_PRESET_MISSING)
    донор, _ = release(c, 3, "у ролика нет субтитров (VideoUnplayable)", phase="created")
    чужое, _ = release(c, 4, "зависло на шаге «cover_ready» и попытки исчерпаны")
    check("пока пресета нет — никого не трогаем",
          pub_engine.revive_cover_preset_chosen(c, NOW), [])
    c.execute("UPDATE pub_channels SET preset_cover='Default RU' WHERE id=?", (ch_выбран,))
    c.execute("UPDATE pub_channels SET preset_cover='Default RU' WHERE id IN "
              "(SELECT channel_id FROM pub_releases WHERE release_id IN (?,?))",
              (донор, чужое))
    back = pub_engine.revive_cover_preset_chosen(c, NOW)
    check("вернулся ровно тот, у чьего канала пресет выбран", back, [выбран])
    st = {r["release_id"]: (r["status"], r["blocked_reason"], r["attempts"])
          for r in c.execute("SELECT * FROM pub_releases")}
    check("он снова в работе, причина снята", st[выбран][:2], ("pending", None))
    check("попытки обнулены", st[выбран][2], 0)
    check("фаза не откатилась",
          c.execute("SELECT phase FROM pub_releases WHERE release_id=?", (выбран,)
                    ).fetchone()[0], "media_ready")
    check("без пресета — стоит", st[без][0], "blocked")
    check("донор — стоит, хоть пресет у канала и есть", st[донор][0], "blocked")
    check("чужая причина — стоит", st[чужое][0], "blocked")
    msg = c.execute("SELECT message FROM pub_release_log WHERE release_id=? AND step='обложка'",
                    (выбран,)).fetchone()
    check("в журнале выпуска сказано, почему вернулся",
          bool(msg) and "Default RU" in msg[0] and "сам" in msg[0], True)
    check("второй проход ничего не делает", pub_engine.revive_cover_preset_chosen(c, NOW), [])

    print("\n  пресет пришёл из пресета производства, а не из канала")
    c = fresh()
    rid, ch = release(c, 5, B.COVER_PRESET_MISSING)
    saved = pub_engine.preset_links.resolve
    pub_engine.preset_links.resolve = lambda c_, pid: (
        {"preset_cover": "Из пресета"} if pid == 77 else {})
    try:
        c.execute("UPDATE pub_channels SET preset_vsgen_id=77 WHERE id=?", (ch,))
        check("вернулся по пресету производства",
              pub_engine.revive_cover_preset_chosen(c, NOW), [rid])
    finally:
        pub_engine.preset_links.resolve = saved

    print("\n  движок зовёт это на каждом тике")
    import inspect
    check("tick вызывает revive_cover_preset_chosen",
          "revive_cover_preset_chosen(c, now)" in inspect.getsource(pub_engine.tick), True)

    print("\nСводка говорит теми же словами, что экран")
    c = fresh()
    rid, ch = release(c, 6, "у ролика нет субтитров (TranscriptsDisabled)", phase="created")
    rows = list(c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)))
    chans = [{"id": ch, "name": "Канал 6"}]
    out = pub_digest._problems(c, chans, {ch: rows}, {}, {}, NOW, "Europe/Warsaw")
    check("донор назван донором",
          out and out[0]["why"].startswith("проблема в доноре: автор ролика отключил субтитры"),
          True)
    c.execute("UPDATE pub_releases SET blocked_reason='зависло на шаге «x»'")
    rows = list(c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)))
    out = pub_digest._problems(c, chans, {ch: rows}, {}, {}, NOW, "Europe/Warsaw")
    check("незнакомая причина — как раньше, текстом движка",
          out and out[0]["why"], "зависло на шаге «x»")

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
