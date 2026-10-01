#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ворота приёмки моста манхвы (01.10.2026): что в очередь заливки не попадёт.

Чем это опасно. До 01.10 мост ставил на заливку всё, под что подходил glob
`projects/*/out/video.mp4`: ролик, не прошедший сборку, ролик без названия, канал,
опознанный по похожему имени. Единственным, что держало брак, был отказ 402 от
прокси названий — то есть пустой счёт. Это не предохранитель: пополнение счёта
выпустило бы на 20 каналов всё накопленное, по ролику в день на канал, и отозвать
опубликованное нельзя.

Поэтому здесь закреплены четыре вещи, которые легко сломать «улучшением»:

  * **порядок.** Ни один непринятый ролик не должен дойти до `make_title`. Проверка
    после платного вызова деньги уже не спасает, а выглядит точно так же зелёной.
    Подменённый `make_title` считает вызовы, и в конце прогона их должно быть ровно
    столько, сколько роликов допущено;
  * **канал по идентификатору.** Прежний мост делал `NAME_TO_UC.get(name)` и шёл
    дальше: имя, записанное в проекте манхвы руками, решало, на какой канал уедет
    ролик. Два канала с одним названием обязаны дать отказ, а не первый из них;
  * **пустота — не «проверено».** Нет `out/acceptance.json` — приёмка неизвестна, а
    неизвестна это не «прошла». Ролики, залитые прежней версией моста, помечаются
    «версия неизвестна» явно;
  * **пересборка отменяет приёмку, но ложная тревога здесь дорогая.** Отпечаток
    ловит подмену файла по тому же пути, а `cp` и восстановление из резервной копии
    переставляют только mtime — и выпуск от этого встать не должен. Отдельно: ролик
    в тесте БОЛЬШЕ мегабайтного окна края, иначе ветка чтения хвоста не исполняется
    вовсе (файл в 4 КБ читается целиком первым же куском).

`pub_publish` на боевой машине лежит в /root/VideoStudio/server; у нас его нет, он
подменяется. Схема таблиц берётся не своя, а настоящая — из снимка production
`live_2026-09-30/кабинет/pub_schema.py`: мост пишет в чужую базу, и расхождение с её
схемой должно падать здесь, а не на VPS.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
REPO = os.path.dirname(BRIDGE)
sys.path.insert(0, BRIDGE)

CALLS = []                      # каждый вызов платной модели названий


def _make_title(source, lang, llm):
    CALLS.append((source, lang))
    return "НАЗВАНИЕ %s [%s]" % (str(source)[:30], lang)


_stub = types.ModuleType("pub_publish")
_stub.make_title = _make_title
_stub.make_description = lambda title: "описание для " + title
_stub.schedule_iso = lambda slot, tz: "2026-10-05T10:00:00+02:00"
sys.modules["pub_publish"] = _stub

import manga_gate as gate                                            # noqa: E402
import manga_bridge as bridge                                        # noqa: E402

FAILS = []


def check(what, got, want):
    if got == want:
        print("OK   " + what)
    else:
        FAILS.append(what)
        print("FAIL %s: получили %r, ждали %r" % (what, got, want))


def check_has(what, text, needle):
    if needle in (text or ""):
        print("OK   " + what)
    else:
        FAILS.append(what)
        print("FAIL %s: в %r нет «%s»" % (what, text, needle))


def check_raises(what, exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as got:
        print("OK   %s (%s)" % (what, str(got)[:60]))
        return
    except Exception as got:
        FAILS.append(what)
        print("FAIL %s: другая ошибка %r" % (what, got))
        return
    FAILS.append(what)
    print("FAIL %s: ошибки не было вовсе" % what)


# ── настоящая схема кабинета из снимка production ─────────────────────────────────

def real_schema():
    path = os.path.join(REPO, "live_2026-09-30", "кабинет", "pub_schema.py")
    spec = importlib.util.spec_from_file_location("pub_schema_live", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ensure_pub_schema


UC = dict(bridge.NAME_TO_UC)
UC_TWIN_A, UC_TWIN_B = "UCtwinAAAAAAAAAAAAAAAAA", "UCtwinBBBBBBBBBBBBBBBBB"
UC_NOPROF, UC_NOHOST = "UCnoprofileAAAAAAAAAAAA", "UCnohostAAAAAAAAAAAAAAA"
UC_COVER = "UCcoverAAAAAAAAAAAAAAAA"

# название, UC, хендл, профиль, машина, состояние
CHANNELS = [
    ("Хару Манхва", UC["Хару Манхва"], "@harumanhwa", 21, 7, "active"),
    ("Brama Potworów", UC["Brama Potworów"], "@bramapotworow", 21, 7, "paused"),
    ("Без профиля", UC_NOPROF, "@bezprofilya", None, 7, "active"),
    ("Без машины", UC_NOHOST, "@bezmashiny", 99, None, "active"),
    ("Двойник Манхва", UC_TWIN_A, "@dvoynikA", 21, 7, "active"),
    ("Двойник Манхва", UC_TWIN_B, "@dvoynikB", 21, 7, "active"),
    ("Обложка нужна", UC_COVER, "@oblozhka", 21, 7, "active"),
]


def make_db(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    real_schema()(c)
    now = time.time()
    for name, uc, handle, profile, host, status in CHANNELS:
        c.execute("INSERT INTO pub_channels(tenant_id,uc,name,handle,lang,niche,profile_id,"
                  "host_id,tz,schedule,horizon_days,video_mode,status,created,updated) "
                  "VALUES('3',?,?,?,'ru','манхва',?,?,'Europe/Warsaw',?,18,'slideshow',?,?,?)",
                  (uc, name, handle, profile, host,
                   json.dumps({"*": ["10:00"]}), status, now, now))
    c.commit()
    return c


def project(root, pid, channel_name="Хару Манхва", title="История о возвращении",
            acceptance=True, cover=True, channel_key=None, uc=None, big=False,
            problems=None, broken_meta=False):
    """Папка проекта манхвы так, как её оставляет после себя конвейер."""
    folder = os.path.join(root, pid)
    os.makedirs(os.path.join(folder, "out"), exist_ok=True)
    meta = {"id": pid, "title": title, "minutes": 70, "language": "Russian"}
    if channel_name is not None:
        meta["channel_name"] = channel_name
    if channel_key:
        meta["channel"] = channel_key
    if uc:
        meta["channel_uc"] = uc
    with open(os.path.join(folder, "project.json"), "w", encoding="utf-8") as f:
        if broken_meta:
            f.write('{"id": "' + pid + '", "title": "обрез')
        else:
            json.dump(meta, f, ensure_ascii=False)
    # Ролик больше окна края (1 МБ), иначе ветка чтения хвоста не исполняется.
    body = bytes(range(256)) * (6250 if big else 4)
    with open(os.path.join(folder, "out", "video.mp4"), "wb") as f:
        f.write(body)
    if acceptance is not None:
        acc = {"ok": bool(acceptance), "duration": 4200.0, "width": 1920, "height": 1080,
               "problems": problems if problems is not None
               else ([] if acceptance else ["в файле нет звуковой дорожки"])}
        with open(os.path.join(folder, "out", "acceptance.json"), "w", encoding="utf-8") as f:
            json.dump(acc, f, ensure_ascii=False)
    if cover:
        with open(os.path.join(folder, "out", "cover.png"), "wb") as f:
            f.write(b"PNG-cover" * 10)
    return folder


def paths(folder):
    return (os.path.join(folder, "out", "video.mp4"),
            os.path.join(folder, "out", "cover.png"))


# ── отпечаток и сверка версий ─────────────────────────────────────────────────────

def test_marks(tmp):
    print("\n── отпечаток файла и сверка версий ──")
    check("размер неизвестен — так и сказано", gate.human_size(None), "неизвестно")
    check("размер в гигабайтах", gate.human_size(2 * 1024 ** 3), "2,0 ГБ")
    check("размер в мегабайтах", gate.human_size(5 * 1024 ** 2), "5 МБ")
    check("размер в байтах", gate.human_size(10), "10 Б")
    check("нет пути — отпечатка нет", gate.file_mark(""), {})
    check("нет файла — отпечатка нет", gate.file_mark(os.path.join(tmp, "нет.mp4")), {})

    folder = project(tmp, "p_mark", big=True)
    video, cover = paths(folder)
    was = gate.passport(folder, video, cover)
    check("ролик больше окна края", was["video"]["size"] > gate.EDGE_BYTES, True)
    check("сборка принята", was["build"]["ok"], True)
    check("отпечаток сборки записан", len(was["build"]["digest"]), 16)
    check("та же версия — расхождений нет", gate.mismatch(
        gate.taken("m_x", "UC", was, 1.0), gate.passport(folder, video, cover)), "")

    os.utime(video, (1, 1))                     # cp и восстановление из копии
    check("переставленный mtime — не пересборка", gate.mismatch(
        gate.taken("m_x", "UC", was, 1.0), gate.passport(folder, video, cover)), "")

    # Хвост: размер тот же, байты другие. Байт нарочно не из шаблона bytes(range(256)),
    # иначе «подмена» записала бы то же значение и проверять было бы нечего.
    with open(video, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        f.write(b"\x2a")
    said = gate.mismatch(gate.taken("m_x", "UC", was, 1.0),
                         gate.passport(folder, video, cover))
    check_has("подмена хвоста поймана", said, "размер прежний, а содержимое другое")

    with open(video, "ab") as f:
        f.write(b"more")
    said = gate.mismatch(gate.taken("m_x", "UC", was, 1.0),
                         gate.passport(folder, video, cover))
    check_has("другой размер поймали", said, "ролик пересобран после приёмки")
    check_has("и назвали оба размера", said, "сейчас 1 МБ")

    os.unlink(video)
    check_has("пропавший ролик назван", gate.mismatch(
        gate.taken("m_x", "UC", was, 1.0), gate.passport(folder, video, cover)),
        "больше нет на месте")


def test_build_and_cover(tmp):
    print("\n── приёмка сборки и обложка ──")
    folder = project(tmp, "p_build")
    video, cover = paths(folder)
    check("вердикта нет — отказ словами", gate.build_refusal({}) != "", True)
    check_has("и словами про «неизвестно»", gate.build_refusal({}),
              "неизвестно это не «прошёл»")
    bad = gate.build_mark(project(tmp, "p_bad", acceptance=False))
    check("непрошедшая сборка — отказ", bad["ok"], False)
    check_has("причина взята из файла манхвы", gate.build_refusal(bad),
              "в файле нет звуковой дорожки")
    check("прошедшая сборка — пропуск", gate.build_refusal(gate.build_mark(folder)), "")

    nothing = project(tmp, "p_noacc", acceptance=None)
    check("нет acceptance.json — пусто, а не «прошло»", gate.build_mark(nothing), {})
    with open(os.path.join(nothing, "out", "acceptance.json"), "w", encoding="utf-8") as f:
        f.write("{обрез")
    check("битый acceptance.json — тоже пусто", gate.build_mark(nothing), {})

    was = gate.passport(folder, video, cover)
    os.unlink(cover)
    check_has("пропавшая принятая обложка названа", gate.mismatch(
        gate.taken("m_x", "UC", was, 1.0), gate.passport(folder, video, cover)),
        "обложка, принятая вместе с роликом, исчезла")
    # Шаг «Обложка» у манхвы идёт ПОСЛЕ сборки: появившаяся потом обложка не повод
    # останавливать готовый выпуск.
    без = gate.passport(folder, video, cover)
    with open(cover, "wb") as f:
        f.write(b"PNG")
    check("появившаяся потом обложка — не пересборка", gate.mismatch(
        gate.taken("m_x", "UC", без, 1.0), gate.passport(folder, video, cover)), "")

    acc = os.path.join(folder, "out", "acceptance.json")
    was = gate.passport(folder, video, cover)
    with open(acc, "w", encoding="utf-8") as f:
        json.dump({"ok": True, "duration": 9999.0, "problems": []}, f)
    check_has("другой вердикт сборки — пересборка", gate.mismatch(
        gate.taken("m_x", "UC", was, 1.0), gate.passport(folder, video, cover)),
        "машинная приёмка сборки дала другой ответ")
    os.unlink(acc)
    check_has("пропавший вердикт сборки назван", gate.mismatch(
        gate.taken("m_x", "UC", was, 1.0), gate.passport(folder, video, cover)),
        "подтвердить версию нечем")

    check("нет названия — отказ", gate.title_refusal({"title": "  "}) != "", True)
    check("есть название — пропуск", gate.title_refusal({"title": "Рассказ"}), "")
    check("обложки нет — отказ", gate.cover_refusal({}) != "", True)
    check_has("пустая обложка названа пустой", gate.cover_refusal({"size": 0}), "0 байт")
    check("обложка есть — пропуск", gate.cover_refusal({"size": 10}), "")


def test_passports(tmp):
    print("\n── пустота не читается как «проверено» ──")
    check("пусто — не принят", gate.is_accepted({}), False)
    check("None — не принят", gate.is_accepted(None), False)
    check("мусор — не принят", gate.is_accepted("ok"), False)
    check("«версия неизвестна» — не принят", gate.is_accepted(gate.unknown_version(1.0)),
          False)
    check("паспорт допуска — принят",
          gate.is_accepted(gate.taken("m_x", "UC", {"video": {}}, 1.0)), True)
    check("у старого ролика версия названа словом",
          gate.unknown_version(1.0)["version"], "unknown")
    check("паспорта нет — сверять не с чем, молчим",
          gate.mismatch({}, {"video": {"size": 1}}), "")
    check("«версия неизвестна» не выдаётся за пересборку",
          gate.mismatch(gate.unknown_version(1.0), {"video": {"size": 1}}), "")

    store = os.path.join(tmp, "паспорта.json")
    check("нет файла — пусто", gate.load_store(store), {})
    gate.save_store(store, {"a": gate.taken("m_a", "UC", {"video": {}}, 1.0)})
    check("записали и прочли", gate.load_store(store)["a"]["release_id"], "m_a")
    with open(store, "w", encoding="utf-8") as f:
        f.write("{обрез")
    check_raises("битые паспорта — отказ, а не пустота", gate.GateError,
                 gate.load_store, store)
    with open(store, "w", encoding="utf-8") as f:
        f.write("[]")
    check_raises("не та форма паспортов — отказ", gate.GateError, gate.load_store, store)


def test_settings(tmp):
    print("\n── настройки ворот рядом с мостом ──")
    s = gate.load_settings(os.path.join(tmp, "нет.json"))
    check("нет файла — обложка никому не обязательна", s["covers"], set())
    check("нет файла — машина обязательна", s["host_required"], True)

    path = os.path.join(tmp, "manga_gate.json")

    def write(data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    write({"обложка_обязательна": ["@Oblozhka", UC_TWIN_A], "машина_обязательна": False})
    s = gate.load_settings(path)
    check("хендл в настройках без регистра и @", "oblozhka" in s["covers"], True)
    check("машину выключили", s["host_required"], False)
    write({"_зачем": "пояснение", "обложка_обязательна": []})
    check("ключ-пояснение не мешает", gate.load_settings(path)["covers"], set())
    write({"обложка_обязательна": "@oblozhka"})
    check_raises("строка вместо списка — отказ", gate.GateError, gate.load_settings, path)
    write({"обложка_обязательна_везде": "да"})
    check_raises("«да» вместо true — отказ", gate.GateError, gate.load_settings, path)
    write({"обложка_обязательно": ["@oblozhka"]})
    check_raises("опечатка в ключе — отказ, а не тишина", gate.GateError,
                 gate.load_settings, path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{обрез")
    check_raises("битый json — отказ", gate.GateError, gate.load_settings, path)


def test_channel(tmp):
    print("\n── канал по идентификатору, а не по подписи ──")
    c = make_db(os.path.join(tmp, "канал.db"))
    rows = {r["uc"]: r for r in c.execute("SELECT * FROM pub_channels")}

    def who(meta):
        ch, why = gate.resolve_channel(c, meta, bridge.NAME_TO_UC)
        return (ch["uc"] if ch else None), why

    check("по UC из паспорта проекта", who({"channel_uc": UC_TWIN_A}),
          (UC_TWIN_A, ""))
    check("UC старым именем поля тоже понимаем", who({"uc": UC_TWIN_B}), (UC_TWIN_B, ""))
    got, why = who({"channel_uc": "UCнетуНичегоТакого"})
    check("неизвестный UC — отказ", got, None)
    check_has("и UC назван", why, "UCнетуНичегоТакого")
    check("по хендлу", who({"channel": "harumanhwa", "channel_name": "как угодно"}),
          (UC["Хару Манхва"], ""))
    check("хендл можно с собакой", who({"channel": "@harumanhwa"}),
          (UC["Хару Манхва"], ""))
    check("по имени через справочник моста", who({"channel_name": "Хару Манхва"}),
          (UC["Хару Манхва"], ""))
    check("по имени прямо из кабинета", who({"channel_name": "Обложка нужна"}),
          (UC_COVER, ""))
    got, why = who({"channel_name": "Двойник Манхва"})
    check("два канала с одним именем — отказ", got, None)
    check_has("и названы оба", why, UC_TWIN_A)
    check_has("и сказано, кто решает", why, "решает человек")
    got, why = who({"channel": "dvoynikA", "channel_name": "Двойник Манхва"})
    check("хендл разводит одноимённые каналы", got, UC_TWIN_A)
    got, why = who({})
    check("канала в паспорте нет — отказ", got, None)
    check_has("и сказано, чего не хватает", why, "ни UC, ни хендла, ни названия")
    got, why = who({"channel": "чужой", "channel_name": "Чужой канал"})
    check("ни хендла, ни имени в кабинете — отказ", got, None)
    check_has("названы обе попытки", why, "@чужой / «Чужой канал»")

    print("\n── разрешение на канал ──")
    strict = {"host_required": True, "covers": set()}
    loose = {"host_required": False, "covers": set()}
    check("исправный канал разрешён",
          gate.channel_allowed(c, rows[UC["Хару Манхва"]], strict), "")
    check_has("канал на паузе — отказ словами",
              gate.channel_allowed(c, rows[UC["Brama Potworów"]], strict),
              "канал не в работе")
    check_has("без профиля AdsPower — отказ",
              gate.channel_allowed(c, rows[UC_NOPROF], strict), "профиля AdsPower")
    check_has("без машины — отказ с объяснением",
              gate.channel_allowed(c, rows[UC_NOHOST], strict), "машина заливки")
    check("требование машины снимается настройкой",
          gate.channel_allowed(c, rows[UC_NOHOST], loose), "")
    c.execute("INSERT INTO pub_profiles(id,tenant_id,serial,title,default_host_id,created) "
              "VALUES(99,'3','99','профиль манхвы',7,?)", (time.time(),))
    check("машина по умолчанию у профиля годится",
          gate.channel_allowed(c, rows[UC_NOHOST], strict), "")
    c.execute("UPDATE pub_channels SET uc='' WHERE uc=?", (UC_NOPROF,))
    rows2 = {r["name"]: r for r in c.execute("SELECT * FROM pub_channels")}
    check_has("канал без UC — отказ",
              gate.channel_allowed(c, rows2["Без профиля"], loose), "нет UC")

    print("\n── порядок ворот: сначала бесплатное ──")
    folder = project(tmp, "p_order", channel_name="Brama Potworów", acceptance=None)
    video, cover = paths(folder)
    mark = gate.passport(folder, video, cover)
    with open(os.path.join(folder, "project.json"), encoding="utf-8") as f:
        meta = json.load(f)
    ch, why = gate.admit(c, meta, mark, strict, bridge.NAME_TO_UC)
    check("канал на паузе, но первым назван вопрос подешевле", ch, None)
    check_has("а именно приёмка сборки", why, "машинной приёмки сборки нет")
    c.close()


# ── прогон целиком ────────────────────────────────────────────────────────────────

def run(apply_it, limit=0):
    argv = sys.argv
    sys.argv = ["manga_bridge.py"] + (["--apply"] if apply_it else []) \
        + (["--limit", str(limit)] if limit else [])
    try:
        bridge.main()
    finally:
        sys.argv = argv


def rows(db, sql, args=()):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def test_run(tmp):
    print("\n── прогон моста целиком ──")
    db = os.path.join(tmp, "vsgen.db")
    make_db(db).close()
    projects = os.path.join(tmp, "projects")
    os.makedirs(projects)
    store = os.path.join(tmp, "manga_passports.json")
    settings = os.path.join(tmp, "manga_gate.json")
    seen = os.path.join(tmp, "manga_seen.json")
    keys = os.path.join(tmp, "keys.json")
    with open(keys, "w", encoding="utf-8") as f:
        json.dump({"alt_llm": "sk-тест", "alt_llm_base": "https://прокси",
                   "pub_title_model": "модель"}, f, ensure_ascii=False)
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"обложка_обязательна": [UC_COVER]}, f, ensure_ascii=False)
    with open(seen, "w", encoding="utf-8") as f:
        json.dump({"p_legacy": {"status": "ok"}, "p_noretry": {"no_retry": True}},
                  f, ensure_ascii=False)

    good = project(projects, "p_good", big=True)
    project(projects, "p_noacc", acceptance=None)
    project(projects, "p_badacc", acceptance=False)
    project(projects, "p_notitle", title="")
    project(projects, "p_paused", channel_name="Brama Potworów")
    project(projects, "p_noprof", channel_name="Без профиля")
    project(projects, "p_nohost", channel_name="Без машины")
    project(projects, "p_twins", channel_name="Двойник Манхва")
    project(projects, "p_cover", channel_name="Обложка нужна", cover=False)
    cover_ok = project(projects, "p_coverok", channel_name="Обложка нужна")
    project(projects, "p_legacy")
    project(projects, "p_noretry")
    project(projects, "p_brokenmeta", broken_meta=True)

    bridge.DB, bridge.PROJECTS, bridge.SEEN = db, projects, seen
    bridge.PASSPORTS, bridge.SETTINGS, bridge.KEYS = store, settings, keys

    del CALLS[:]
    run(False)
    check("без --apply заданий не создано",
          rows(db, "SELECT COUNT(*) n FROM pub_upload_jobs")[0]["n"], 0)
    check("без --apply модель не вызывалась", len(CALLS), 0)
    check("без --apply паспорта не записаны", os.path.exists(store), False)

    run(True)
    jobs = rows(db, "SELECT * FROM pub_upload_jobs ORDER BY release_id")
    check("допущены ровно два ролика", [j["release_id"] for j in jobs],
          ["m_p_coverok", "m_p_good"])
    check("модель позвали ровно два раза", len(CALLS), 2)
    check("и только за допущенные ролики", sorted(s for s, _ in CALLS),
          ["История о возвращении", "История о возвращении"])
    check("видимость осталась public", sorted({j["visibility"] for j in jobs}),
          ["public"])
    check("обложка допущенного ролика в задании",
          jobs[0]["cover_path"].endswith("cover.png"), True)
    plan = rows(db, "SELECT * FROM pub_plan_items")
    check("строка плана заморожена", sorted({p["frozen"] for p in plan}), [1])
    rel = rows(db, "SELECT * FROM pub_releases ORDER BY release_id")
    check("выпуск сразу на заливке", sorted({r["phase"] for r in rel}),
          ["upload_pending"])

    saved = gate.load_store(store)
    check("паспорт допущенного ролика записан", gate.is_accepted(saved["p_good"]), True)
    check("и помнит отпечаток ролика", saved["p_good"]["video"]["size"] > gate.EDGE_BYTES,
          True)
    check("и номер выпуска", saved["p_good"]["release_id"], "m_p_good")
    check("старый залитый ролик помечен «версия неизвестна»",
          saved["p_legacy"]["version"], "unknown")
    check("и принятым не считается", gate.is_accepted(saved["p_legacy"]), False)
    check("непринятым роликам паспорта не выдавались",
          sorted(k for k in saved if k not in ("p_good", "p_coverok", "p_legacy")), [])

    print("\n── повтор допуска не создаёт вторую работу ──")
    run(True)
    check("заданий по-прежнему два",
          rows(db, "SELECT COUNT(*) n FROM pub_upload_jobs")[0]["n"], 2)
    check("выпусков по-прежнему два",
          rows(db, "SELECT COUNT(*) n FROM pub_releases")[0]["n"], 2)
    check("модель больше не звали", len(CALLS), 2)

    print("\n── пересборка отменяет приёмку ──")
    with open(os.path.join(good, "out", "video.mp4"), "ab") as f:
        f.write(b"rebuilt-again")
    run(True)
    rel = rows(db, "SELECT * FROM pub_releases WHERE release_id='m_p_good'")[0]
    job = rows(db, "SELECT * FROM pub_upload_jobs WHERE release_id='m_p_good'")[0]
    check("выпуск уведён в разбор", rel["status"], "blocked")
    check_has("причина названа словами", rel["blocked_reason"], "пересобран после приёмки")
    check("задание снято до выдачи", job["status"], "cancelled")
    check_has("и в задании та же причина", job["error"], "пересобран после приёмки")
    check("второго задания не появилось",
          rows(db, "SELECT COUNT(*) n FROM pub_upload_jobs")[0]["n"], 2)
    check("модель на пересобранный ролик не звали", len(CALLS), 2)

    print("\n── оживление не воскрешает непринятую версию ──")
    run(True)
    rel = rows(db, "SELECT * FROM pub_releases WHERE release_id='m_p_good'")[0]
    job = rows(db, "SELECT * FROM pub_upload_jobs WHERE release_id='m_p_good'")[0]
    check("остался в разборе", rel["status"], "blocked")
    check("задание осталось снятым", job["status"], "cancelled")
    # Оживление обязано проверять версию САМО. Выше его прикрывает порядок проверок
    # в main, но порядок — это то, что ломается первым при следующей правке.
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    check("оживление не верит порядку в main, а сверяет версию само",
          bridge.revive(c, "m_p_good", time.time(), gate.load_store(store)["p_good"],
                        gate.passport(good, *paths(good))), False)
    c.close()

    print("\n── а сорванную не по вине ролика заливку оживляем ──")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("UPDATE pub_releases SET status='blocked', blocked_reason='браузер умер' "
              "WHERE release_id='m_p_coverok'")
    c.execute("UPDATE pub_upload_jobs SET status='error', error='AdsPower не ответил' "
              "WHERE release_id='m_p_coverok'")
    c.commit(); c.close()
    run(True)
    rel = rows(db, "SELECT * FROM pub_releases WHERE release_id='m_p_coverok'")[0]
    job = rows(db, "SELECT * FROM pub_upload_jobs WHERE release_id='m_p_coverok'")[0]
    check("выпуск вернулся в работу", rel["status"], "running")
    check("задание снова в очереди", job["status"], "queued")
    check("причина простоя снята", rel["blocked_reason"], None)
    check("и это не стоило нового вызова модели", len(CALLS), 2)

    print("\n── ролик, который уже уходил в YouTube, не трогаем ──")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("UPDATE pub_upload_jobs SET video_id='ytAbCd', status='done', "
              "verdict='scheduled' WHERE release_id='m_p_coverok'")
    c.execute("UPDATE pub_releases SET status='running' WHERE release_id='m_p_coverok'")
    c.commit()
    said = bridge.hold(c, "m_p_coverok", time.time(), "ролик пересобран после приёмки")
    check_has("сказано, что отзывать нечего", said, "уже уходил")
    check_has("и что повтор дал бы второй ролик", said, "второй ролик")
    job = c.execute("SELECT * FROM pub_upload_jobs WHERE release_id='m_p_coverok'"
                    ).fetchone()
    check("задание не тронуто", job["status"], "done")
    check("и выпуск не тронут", c.execute(
        "SELECT status FROM pub_releases WHERE release_id='m_p_coverok'"
    ).fetchone()[0], "running")
    c.close()

    print("\n── битые настройки останавливают мост, а не открывают ворота ──")
    with open(settings, "w", encoding="utf-8") as f:
        f.write("{обрез")
    было = rows(db, "SELECT COUNT(*) n FROM pub_upload_jobs")[0]["n"]
    check_raises("мост остановился", SystemExit, run, True)
    check("и ничего не добавил",
          rows(db, "SELECT COUNT(*) n FROM pub_upload_jobs")[0]["n"], было)


def main() -> int:
    print("Проверка: мост манхвы берёт только принятую версию ролика")
    with tempfile.TemporaryDirectory() as tmp:
        def своя(name):
            folder = os.path.join(tmp, name)
            os.makedirs(folder)
            return folder

        test_marks(своя("отпечаток"))
        test_build_and_cover(своя("сборка"))
        test_passports(своя("паспорта"))
        test_settings(своя("настройки"))
        test_channel(своя("канал"))
        test_run(своя("прогон"))
    print()
    if FAILS:
        print("ПРОВАЛЕНО: %d" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
