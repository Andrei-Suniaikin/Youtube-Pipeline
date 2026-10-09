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
    check("чья беда", (b["kind"], b["tag"]), ("channel", "Настройки канала"))
    check("что делать — настройки канала", "настройках канала" in b["fix"], True)
    check("и что выпуски вернутся сами", "вернутся в работу сами" in b["fix"], True)
    check("возврат в работу имеет смысл", b["revive"], True)
    check("строка для сводки", b["short"],
          "настройки канала: не выбран пресет обложек — выбрать в настройках канала")

    print("\nДонор — «проблема в доноре», имя исключения переведено")
    реальные = {   # дословно из pub_releases.blocked_reason на VPS 03.10.2026
        # 09.10.2026, владелец: показывать настоящую ошибку — ролика нет, а не субтитров.
        "у ролика нет субтитров (VideoUnplayable)": "донор удалён или скрыт на YouTube",
        "у ролика нет субтитров (VideoUnavailable)": "донор удалён или скрыт на YouTube",
        "ролик-донор удалён или скрыт на YouTube (VideoUnplayable)":
            "донор удалён или скрыт на YouTube",
        "у ролика нет субтитров (TranscriptsDisabled)": "у донора отключены субтитры",
        "у ролика нет субтитров (NoTranscriptFound)": "у донора нет субтитров",
        "у ролика нет субтитров (AgeRestricted)":
            "донор 18+ — YouTube не отдаёт субтитры",
        "у ролика нет субтитров": "у донора нет субтитров",
        "у ролика нет субтитров (ЧтоТоНовое)": "у донора нет субтитров",
        "субтитры донора слишком короткие или пустые":
            "у донора обрывок субтитров — на текст не хватит",
        "не разобрал ссылку на ролик: https://example.com/x": "ссылка не на ролик YouTube",
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
    check("строка для сводки", b["short"],
          "донор: донор удалён или скрыт на YouTube — нужен другой ролик")
    check("сказано, что Минск не поможет", "минской машине" in b["what"], True)

    print("\nЗаливка: вход в Google и AdsPower — словами и с тем, что делать (09.10.2026)")
    вход = {   # дословно из pub_releases.blocked_reason на VPS 08.10.2026
        "AdsPower: [k1bouve5] is being used by [vova04.2007@gmail.com] and is not allowed to open":
            "профиль AdsPower открыт у vova04.2007@gmail.com",
        "not logged in / no access: https://studio.youtube.com/channel/UCDrgZuUJTA-USD4jiFrmHdA/ "
        "| account recovery blocked: RecoveryBlocked":
            "в профиле нет входа в Google",
        "Studio не отдала рабочее состояние канала (доступ не подтверждён): "
        "https://studio.youtube.com/channel/UCWazqTA6b8zOMS7SyrzPbaA/ | account recovery "
        "blocked: RecoveryBlocked": "Studio канала не открылась — нет входа",
        "account recovery needs the owner: Google requires an additional sign-in step":
            "Google просит подтвердить вход",
        # 09.10.2026: так заливщик отвечает на каждую следующую попытку после первой.
        "account recovery needs the owner: owner help already requested for this account "
        "recently": "автовход ждёт человека",
        "dedup не выполнен: состояние канала не прочитано — заливку не начинаю":
            "не прочитан список роликов канала",
        "account recovery needs the owner: no exact account binding for this profile+channel":
            "нет почты канала для автовхода",
    }
    for сырая, смысл in вход.items():
        b = B.explain(сырая)
        check(f"«{сырая[:44]}» → вход и профиль", (b.get("kind"), b.get("title")), ("access", смысл))
        check("   сказано, кто чинит", bool(b.get("who")), True)
        check("   есть что делать и возврат в работу", (bool(b.get("fix")), b.get("revive")),
              (True, True))
    b = B.explain(next(iter(вход)))
    check("в объяснении названа учётка, у которой открыт профиль",
          "vova04.2007@gmail.com" in b["what"], True)
    check("и она же — кто чинит", b["who"].startswith("vova04.2007@gmail.com"), True)
    print("\nУ каждой узнанной причины сказано, кто её чинит (09.10.2026)")
    for сырая in ("у ролика нет субтитров (VideoUnplayable)", "у канала не выбран пресет обложек",
                  "зависло на шаге «x» и попытки исчерпаны", "AdsPower: Check Proxy Fail",
                  "обложка не нарисовалась дважды: 0/1", "ролик залит, но Studio показывает «неизвестно»"):
        check(f"«{сырая[:40]}» → кто чинит", bool(B.explain(сырая).get("who")), True)
    b = B.explain("Studio не отдала рабочее состояние канала (доступ не подтверждён): x | "
                  "account recovery blocked: RecoveryBlocked")
    check("про заблокированный автовход сказано", "Автовход" in b["what"], True)
    for сырая in ("SCHEDULED, но нет точного подтверждения Studio (только строковое)",
                  "ролик залит, но Studio показывает «неизвестно»"):
        b = B.explain(сырая)
        check(f"«{сырая[:40]}» → проверить на YouTube, без возврата",
              (b.get("kind"), b.get("revive")), ("check", False))

    print("\nНаша беда, беда канала и обложки — словами (04.10.2026: «разгребём, что наше»)")
    for сырая, вид in (("зависло на шаге «upload_running» и попытки исчерпаны", "studio"),
                       ("рерайт не справился: 1 из 1 задач не выполнено", "studio"),
                       ("задача рерайта прервалась — рерайт перезапускали", "studio"),
                       ("этап «images» завершился со статусом error", "studio"),
                       ("к времени выхода не успели — задание состарилось в очереди", "studio"),
                       ("account recovery needs the owner: no exact account binding for "
                        "this profile+channel", "access"),
                       ("AdsPower: Check Proxy Fail", "access"),
                       ("PHONE_VERIFICATION_REQUIRED_LONG_VIDEO: канал UCx не подтверждён", "access"),
                       ("обложка деградировала дважды: текст наложен поверх картинки", "cover"),
                       ("обложка не нарисовалась дважды: 0/1 обложек", "cover")):
        b = B.explain(сырая)
        check(f"«{сырая[:40]}» → {вид}", b.get("kind"), вид)
    check("сбой студии возвращается в работу",
          B.explain("зависло на шаге «created» и попытки исчерпаны")["revive"], True)
    check("обложку «вернуть в работу» не лечит",
          B.explain("обложка деградировала дважды: текст наложен")["revive"], False)
    check("доступ к каналу — после починки вернуть в работу",
          B.explain("AdsPower: Check Proxy Fail")["revive"], True)

    print("\nНезнакомое не выдумываем")
    for сырая in ("", None,
                  "ждём: очередь не приняла этап «render»",
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
          out and out[0]["why"].startswith("донор: у донора отключены субтитры"),
          True)
    c.execute("UPDATE pub_releases SET blocked_reason='зависло на шаге «x»'")
    rows = list(c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)))
    out = pub_digest._problems(c, chans, {ch: rows}, {}, {}, NOW, "Europe/Warsaw")
    check("незнакомая причина — как раньше, текстом движка",
          out and out[0]["why"], "зависло на шаге «x»")

    print("\nНедосмотры канала: когда о них говорить")
    G = lambda *a, **k: [g["title"] for g in B.channel_gaps(*a, **k)]
    check("нет текста — пока текст не написан", G("", "X", phase="created", status="running"),
          ["не выбран пресет текста"])
    check("текст уже написан — о нём поздно", G("", "X", phase="source_ready", status="running"), [])
    check("нет обложек — пока до обложки не дошли",
          G("X", "", phase="media_ready", status="running"), ["не выбран пресет обложек"])
    check("уже на обложке — не о чем предупреждать", G("X", "", phase="cover_ready", status="running"), [])
    check("остановлен этой же причиной — не дублируем",
          G("X", "", phase="media_ready", status="blocked",
            block=B.explain(B.COVER_PRESET_MISSING)), [])
    check("готовый/отменённый — молчим", G("", "", phase="created", status="done"), [])
    check("всё выбрано — пусто", G("X", "Y", phase="created", status="pending"), [])

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
