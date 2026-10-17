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

    print("\nЗаписи заливщика, которые экран показывал по-английски (база VPS 15.10)")
    заливщик = {
        "account recovery needs the owner: signed in but exact channel not confirmed":
            ("access", True, "вошли, но канал не подтвердился"),
        "account recovery needs the owner: owner help already requested for this "
        "account recently": ("access", True, "автовход ждёт человека"),
        "account recovery needs the owner: unexpected sign-in destination: "
        "myaccount.google.com/verification/u//selfie/precollection":
            ("access", True, "Google просит подтвердить личность"),
        "AUTH_GATE: native confirmation stopped; inspect non-secret auth outcome; "
        "existing upload preserved": ("access", True, "Google просит подтвердить личность"),
        "PROFILE_BUSY: профиль 54 не освободился за 180 мин (профиль уже открыт на этой "
        "машине (руками или остался от прошлой заливки)); файл в YouTube не передавался, "
        "повтор безопасен": ("access", True, "профиль открыт на машине заливки и не закрыт"),
        # Внутри есть «AUTH_GATE», но файл, возможно, уже ушёл: повтор = риск дубля.
        "UPLOAD_IN_PROGRESS_PROFILE_RETAINED: MP4 completion unproven; browser left open, "
        "shared lease must be retained; do not reupload; AUTH_GATE: Google requires "
        "identity confirmation (before_thumbnail)":
            ("check", False, "заливка оборвалась — неизвестно, дошёл ли файл"),
        "нода молчит дольше 45 минут — нужен разбор":
            ("check", False, "машина заливки замолчала посреди заливки"),
        "HTTPConnectionPool(host='localhost', port=36587): Read timed out. "
        "(read timeout=120)": ("check", False, "браузер заливки завис — неизвестно, дошёл ли файл"),
        "обложка не сделалась: Error code: 402 - {'error': {'message': \"API key token "
        "allowance exhausted.": ("cover", False, "сервис обложек дал сбой"),
    }
    for сырая, (сторона, вернуть, смысл) in заливщик.items():
        b = B.explain(сырая)
        check(f"«{сырая[:44]}» → {сторона}", (b.get("kind"), b.get("revive")),
              (сторона, вернуть))
        check("   и смысл словами", b.get("title"), смысл)

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
        "blocked: RecoveryBlocked": "Studio канала не открылась — доступ не подтверждён",
        "account recovery needs the owner: Google requires an additional sign-in step":
            "Google просит подтвердить вход",
        # 09.10.2026: так заливщик отвечает на каждую следующую попытку после первой.
        "account recovery needs the owner: owner help already requested for this account "
        "recently": "автовход ждёт человека",
        "dedup не выполнен: состояние канала не прочитано — заливку не начинаю":
            "не прочитан список роликов канала",
        "account recovery needs the owner: no exact account binding for this profile+channel":
            "нет привязки почты к каналу на Минске",
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

    print("Слова заливщика о входе — дословно (15.10.2026)")
    said = B.explain("Studio не отдала рабочее состояние канала (доступ не подтверждён): u | "
                     "account recovery blocked: RecoveryBlocked: sign-in did not complete")["what"]
    check("текст RecoveryBlocked виден", "Дословно от заливщика: RecoveryBlocked: sign-in did not complete." in said, True)
    step = B.explain("account recovery needs the owner: Google requires an additional sign-in step: "
                     "stage=selection; options=12=Confirm your recovery email — загрузка НЕ выполняется")["what"]
    check("какой шаг спросил Google — виден, хвост «загрузка НЕ выполняется» отрезан",
          step.endswith("options=12=Confirm your recovery email."), True)
    bare = B.explain("Studio не отдала рабочее состояние канала (доступ не подтверждён): u | "
                     "account recovery blocked: RecoveryBlocked")["what"]
    check("одно имя класса — без «дословно»", "Дословно" in bare, False)

    print("\nПричины, появившиеся 17.10.2026: «нет доступа», канал не подтвердился, нет привязки")
    nop = B.explain("активный аккаунт не управляет каналом: Studio показала «нет доступа», "
                    "канал бренда в переключателе не выбран: https://studio.youtube.com/channel/UCx/")
    check("«нет доступа» — настройки канала, возврат после починки",
          (nop.get("kind"), nop.get("revive")), ("channel", True))
    check("   названо: аккаунт профиля каналом не управляет",
          nop.get("title"), "аккаунт профиля не управляет каналом")
    check("   сказано про неверный номер канала", "не тот номер канала" in nop["what"], True)
    check("   что делать: лента по номеру канала, потом права менеджера",
          "feeds/videos.xml?channel_id=" in nop["fix"] and "менеджера" in nop["fix"], True)
    check("   кто чинит — оператор канала, потом права", nop["who"].startswith("оператор канала"), True)

    conf = B.explain("account recovery needs the owner: signed in but exact channel not confirmed: "
                     "host=studio.youtube.com path=/channel/UCx app_uc= slots=0 — загрузка НЕ выполняется")
    nob = B.explain("account recovery needs the owner: no exact account binding for this "
                    "profile+channel — загрузка НЕ выполняется")
    check("«не подтвердился» и «нет привязки» — разные заголовки",
          conf["title"] != nob["title"], True)
    check("не подтвердился: про номер канала и канал бренда",
          "номер канала" in conf["what"] and "канал бренда" in conf["what"], True)
    check("   что делать начинается с проверки номера", conf["fix"].startswith("Проверьте номер канала"), True)
    check("   что видела Studio — дословно", "Дословно от заливщика: signed in but exact channel not confirmed: host=studio.youtube.com" in conf["what"], True)
    check("нет привязки: про реестр на Минске и пару профиль+канал",
          "реестре привязок" in nob["what"] and "профиль + канал" in nob["what"], True)
    check("   что передать: почта, пароль, секрет кода — не в студию",
          all(w in nob["fix"] for w in ("почту", "пароль", "секрет кода", "в студию их не вводить")), True)
    check("   оба — вход и профиль, возврат после починки",
          [(b["kind"], b["revive"]) for b in (conf, nob)], [("access", True)] * 2)

    st = B.explain("Studio не отдала рабочее состояние канала (доступ не подтверждён): u")
    check("Studio не отдала состояние: обычно достаточно вернуть в работу",
          st["fix"].startswith("Обычно достаточно «Вернуть в работу»"), True)
    check("   и сказано, что с 17.10 заливщик знает новую шапку", "17.10" in st["what"], True)

    cov = B.explain("обложка: генератор не нарисовал после 3 смягчений надписи — нужен человек")
    check("обложка после 3 смягчений — обложка, возврат уместен после правки",
          (cov.get("kind"), cov.get("revive")), ("cover", True))
    check("   без правки возврат бесполезен — сказано", "Без правки" in cov["fix"], True)
    check("остальные причины обложки возврата не предлагают",
          [B.explain(r)["revive"] for r in ("обложка деградировала дважды: x",
                                            "обложка не сделалась: 402",
                                            "обложка не нарисовалась дважды: 0/1")],
          [False, False, False])
    check("строка журнала «номер канала исправлен» — не причина, не выдумываем",
          B.explain("номер канала исправлен: UCPyBo_VBk8-xMZbpXBk1eiQ → UC38dp5WnQGTF0TkYi_FnXFg"), {})

    print("\n  сводка складывает их по тем же заголовкам, что экран")
    rels = [{"blocked_reason": "активный аккаунт не управляет каналом: Studio показала «нет доступа»: a"},
            {"blocked_reason": "активный аккаунт не управляет каналом: Studio показала «нет доступа»: b"},
            {"blocked_reason": "account recovery needs the owner: no exact account binding for this profile+channel"}]
    groups = pub_digest._blockers({1: rels[:2], 2: rels[2:]}, {1: {"name": "А"}, 2: {"name": "Б"}})
    check("две группы, заголовки человеческие",
          [(g["title"], g["count"]) for g in groups],
          [("аккаунт профиля не управляет каналом", 2), ("нет привязки почты к каналу на Минске", 1)])
    check("у каждой — метка стороны и что делать",
          [(g["tag"], bool(g["fix"])) for g in groups],
          [("Настройки канала", True), ("Вход и профиль", True)])

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
