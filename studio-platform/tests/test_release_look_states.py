#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кабинет не называет выходом подтверждённое расписание (ТЗ §11, «done ≠ опубликовано»).

Беда, ради которой написан тест. `pub_ui.release_look` решала судьбу выпуска по
СТАТУСУ: `status='done'` — «выложен на канал», зелёным. А движок закрывает выпуск
статусом `done` уже на фазе `scheduled`, и это означает ровно одно: Studio показала
SCHEDULED на нужном канале в нужное время. Ролик при этом может стоять приватным,
открываться только по ссылке или быть удалённым — расписание об этом не знает. Хуже
того, ветка про `blocked_reason` стояла НИЖЕ этого возврата: выпуск, который поздняя
проверка уже пометила «доступен только по ссылке», кабинет всё равно показывал
выложенным. В пульте эту же неправду убрали раньше (коммит 3f64785), в кабинете она
осталась.

Что здесь проверяется и почему именно это.

  • ФАКТ ЧИТАЕТСЯ ПО ФАЗЕ, А НЕ ПО СТАТУСУ. Подтверждает выход только поздняя
    проверка (`server/pub_verify.py`), и признак у неё один — фаза `published`.
    Пока обе фазы читались по статусу, сотня каналов разом сообщала о выходе,
    которого никто не спрашивал.
  • ТРИ СОСТОЯНИЯ — ТРИ РАЗНЫЕ ПОДПИСИ, и ни одна из двух неподтверждённых не
    содержит слов «вышел» и «выложен». Совпадение подписей и есть сам дефект:
    различить их на экране было нечем.
  • ДЕФЕКТ ПОЗДНЕЙ ПРОВЕРКИ ВИДЕН. «Только по ссылке» и «ключом API не виден» —
    причины на уже оплаченном выпуске. Показать их важнее слова «назначено»:
    больше владелец не увидит их нигде, кроме журнала ролика.
  • ЧАСЫ НЕ РЕШАЮТ. Время слота в подпись не входит. «Слот прошёл — значит вышел» —
    ровно тот запрещённый вывод, из-за которого правку и делали: часы идут
    независимо от YouTube.
  • ОБА ЭКРАНА, НАСТОЯЩИЕ. Карточка канала `/channels/{cid}` и «Журнал ролика»
    `/releases/{id}/log` поднимаются как есть (`register_pub_routes` на своём
    FastAPI), потому что дефект был именно в отрисовке: цвет точки для нового
    состояния мог просто не найтись в таблице цветов.
  • ПОДПИСЬ ФАКТА ОДНА НА ДВА ЭКРАНА. Она же лежит в пульте (`web/index.html`).
    Разъехавшиеся формулировки одного и того же состояния — это снова два разных
    ответа на один вопрос владельца.

Сеть не трогается: база — файл во временном каталоге, соседние сервисы (пресеты
рерайта и обложек) при недоступности честно отдают пустой список, и в тесте они
недоступны.

    .venv/bin/python tests/test_release_look_states.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "server"))

WORK = tempfile.mkdtemp(prefix="release-look-")
DB = os.path.join(WORK, "cabinet.db")
os.environ.setdefault("VS_DEV_DB", DB)

from fastapi import FastAPI                                        # noqa: E402
from fastapi.responses import HTMLResponse                         # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

from pub_schema import ensure_pub_schema                           # noqa: E402
import pub_ui as U                                                 # noqa: E402

FAILS = []
TENANT = "t1"
NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.timezone.utc).timestamp()
# Слот назначается от НАСТОЯЩИХ часов, а не от выдуманного «сейчас»: карточка канала
# считает свои выходы сама (`pub_tags.coverage` зовётся без времени), и слот в
# выдуманной эпохе просто не попал бы на экран — проверка подписей доказывала бы
# тогда пустую страницу. Два часа вперёд: выход ещё не наступил, а именно в этом
# случае прежний кабинет и объявлял ролик выложенным.
_soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).replace(
    second=0, microsecond=0)
SLOT_HHMM = _soon.strftime("%H:%M")
SLOT_AT = float(round(_soon.timestamp()))

# Подписи-ожидания вынесены в одно место: они же сверяются с пультом.
OUT_CONFIRMED = "вышел публично — подтверждено"
OUT_SCHEDULED = "назначено, выход ещё не подтверждён"
UNLISTED = "ролик доступен только по ссылке, а должен быть публичным"
NO_KEY = "выход не подтверждён: ключ API ролика не видит"
# Цвета точки в строке канала: зелёный — факт, жёлтый — замысел.
GREEN = "#5fa463"
YELLOW = "#e0a33e"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, cond):
    check(label, bool(cond), True)


def dotted(color: str, text: str) -> str:
    """Точка нужного цвета ВПЛОТНУЮ к подписи — так её рисует карточка канала."""
    return f'background:{color}"></span>{text}'


# ─────────────────────────── база ───────────────────────────

def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


_c = conn()
ensure_pub_schema(_c)
# Очередь кабинета живёт в main.py, а карточка канала соединяется с ней за статусами
# этапов. Без этой таблицы страница падает пятисоткой — и проверка подписей тогда
# доказывала бы только то, что экран не открылся.
_c.executescript("""
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS presets(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT);
""")
_c.commit()
_c.close()


def add_channel(schedule: dict) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,tz,schedule,horizon_days,status,"
        "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
        (TENANT, "UC" + "x" * 22, "Канал проверки", "UTC", json.dumps(schedule), 3,
         "active", NOW, NOW))
    c.commit()
    cid = cur.lastrowid
    c.close()
    return cid


def add_release(cid: int, rid: str, slot_at: float, phase: str, status: str,
                reason: str | None = None, video_id: str | None = "vid123") -> None:
    """Строка плана и выпуск на ней — так, как их видит карточка канала."""
    c = conn()
    cur = c.execute(
        "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,donor_title,slot_at,"
        "status,created,updated) VALUES(?,?,?,?,?,?,?,?)",
        (TENANT, cid, "https://youtu.be/donor", "Донорский ролик " + rid, slot_at,
         "done" if status == "done" else "running", NOW, NOW))
    item = cur.lastrowid
    c.execute(
        "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
        "channel_id,uc,slot_at,project_id,status,phase,video_id,blocked_reason,"
        "created,updated) VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
        (rid, TENANT, item, cid, "UC" + "x" * 22, slot_at, "prj-" + rid, status, phase,
         video_id, reason, NOW, NOW))
    c.execute("INSERT INTO pub_release_events(release_id,seq,phase,created) "
              "VALUES(?,1,?,?)", (rid, phase, NOW))
    c.commit()
    c.close()


def set_phase(rid: str, phase: str, status: str, reason: str | None = None) -> None:
    c = conn()
    c.execute("UPDATE pub_releases SET phase=?,status=?,blocked_reason=?,updated=? "
              "WHERE release_id=?", (phase, status, reason, NOW, rid))
    c.commit()
    c.close()


def row_of(rid: str):
    c = conn()
    try:
        return c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone()
    finally:
        c.close()


# ─────────────────────────── 1. состояния в данных ───────────────────────────

print("1. три состояния выхода различимы, и ни одно неподтверждённое не зовётся выходом")

CID = add_channel({"*": [SLOT_HHMM]})
# Расписание подтверждено, а выход ещё впереди — самый опасный случай: прежний кабинет
# объявлял выложенным ролик, которому выходить только предстоит.
add_release(CID, "r-sched", SLOT_AT, "scheduled", "done")

looks = {}
for name, phase, status, reason in (
        ("готов", "upload_pending", "running", None),
        ("назначено", "scheduled", "done", None),
        ("вышел", "published", "done", None),
):
    set_phase("r-sched", phase, status, reason)
    looks[name] = U.release_look(row_of("r-sched"), "")

check("файл готов — своя подпись", looks["готов"], ("готов, жду заливки", "work"))
check("расписание подтверждено — не выход", looks["назначено"], (OUT_SCHEDULED, "warn"))
check("выход подтверждён проверкой", looks["вышел"], (OUT_CONFIRMED, "ok"))
check("три состояния — три разные подписи", len({v[0] for v in looks.values()}), 3)
check("три состояния — три разных цвета", len({v[1] for v in looks.values()}), 3)
for name in ("готов", "назначено"):
    text = looks[name][0]
    check_true(f"в подписи «{name}» нет слова «вышел»", "выш" not in text)
    check_true(f"в подписи «{name}» нет слова «выложен»", "выложен" not in text)

# Поздняя проверка фазу не понижает, а пишет причину. Причина — про уже оплаченный
# выпуск, поэтому она вытесняет слово «назначено», а не прячется под ним.
set_phase("r-sched", "scheduled", "done", UNLISTED)
check("дефект «только по ссылке» виден в строке", U.release_look(row_of("r-sched"), ""),
      (UNLISTED, "warn"))
set_phase("r-sched", "scheduled", "done", NO_KEY)
check("«ключом API не виден» виден в строке", U.release_look(row_of("r-sched"), ""),
      (NO_KEY, "warn"))
# «ждём: …» — это ожидание общего ресурса, а не ответ проверки: подменять им
# состояние выхода нельзя, иначе пауза кредитов читалась бы как дефект публикации.
set_phase("r-sched", "scheduled", "done", "ждём: кредиты кончились")
check("ожидание ресурса не выдаётся за дефект выхода",
      U.release_look(row_of("r-sched"), ""), (OUT_SCHEDULED, "warn"))
# Карантин остаётся главнее всего: остановленный выпуск — это сбой, а не срок.
set_phase("r-sched", "media_ready", "blocked", "нет обложки")
check("карантин показывается сбоем", U.release_look(row_of("r-sched"), ""),
      ("сбой: нет обложки", "err"))


# ─────────────────────────── 2. настоящие экраны ───────────────────────────

print("2. карточка канала и журнал ролика — как их видит владелец")


def _page(title, body):
    return HTMLResponse(f"<!doctype html><html><head><title>{title}</title></head>"
                        f"<body>{body}</body></html>")


def _header(u, active):
    return f"<nav>{active}</nav>"


USER = {"id": "u1", "login": "dev", "tenant_id": TENANT}

app = FastAPI()
U.register_pub_routes(app, {
    "db": conn, "_page": _page, "_header": _header, "_tid": lambda u: TENANT,
    "_now": lambda: NOW, "web_user": lambda request: USER,
    "require_web_user": lambda request: USER,
})
client = TestClient(app, raise_server_exceptions=False)

set_phase("r-sched", "scheduled", "done", None)
card = client.get(f"/channels/{CID}")
check("карточка канала открылась", card.status_code, 200)
html_card = card.text
check_true("в карточке стоит честная подпись срока", OUT_SCHEDULED in html_card)
check_true("карточка не зовёт это выходом", "выложен на канал" not in html_card)
check_true("карточка не зовёт это выходом (2)", OUT_CONFIRMED not in html_card)
# Цвет ищется вплотную к подписи, а не по всей странице: оба цвета есть в таблице
# стилей всегда, и «жёлтый встречается» ничего не доказывало бы.
check_true("точка у неподтверждённого срока жёлтая", dotted(YELLOW, OUT_SCHEDULED)
           in html_card)
check_true("зелёной точки на этой строке нет", dotted(GREEN, OUT_SCHEDULED)
           not in html_card)

# Тот же выпуск после поздней проверки — и та же карточка. Строка обязана измениться:
# это единственное, что отличает подтверждённый выход от назначенного срока.
set_phase("r-sched", "published", "done", None)
html_pub = client.get(f"/channels/{CID}").text
check_true("подтверждённый выход назван выходом", OUT_CONFIRMED in html_pub)
check_true("после подтверждения срок больше не показывается", OUT_SCHEDULED not in html_pub)
check_true("точка подтверждённого выхода зелёная", dotted(GREEN, OUT_CONFIRMED)
           in html_pub)

# Журнал ролика: то же состояние словами и цветом класса.
set_phase("r-sched", "scheduled", "done", None)
log = client.get("/releases/r-sched/log")
check("журнал ролика открылся", log.status_code, 200)
html_log = log.text
check_true("в журнале срок назван сроком", OUT_SCHEDULED in html_log)
check_true("в журнале нет слова «выложен»", "выложен на канал" not in html_log)
check_true("класс цвета — жёлтый pub-warn", "pub-warn" in html_log)

set_phase("r-sched", "scheduled", "done", UNLISTED)
html_bad = client.get("/releases/r-sched/log").text
check_true("дефект проверки виден в журнале", UNLISTED in html_bad)
check_true("дефект не прикрыт словом «назначено»", OUT_SCHEDULED not in html_bad)

set_phase("r-sched", "published", "done", None)
html_ok = client.get("/releases/r-sched/log").text
check_true("подтверждённый выход виден в журнале", OUT_CONFIRMED in html_ok)
check_true("класс цвета — зелёный pub-ok", "pub-ok" in html_ok)


# ─────────────────────────── 3. одна подпись на два экрана ───────────────────────────

print("3. пульт и кабинет говорят о выходе одними словами")

pult = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
check_true("подпись подтверждённого выхода та же, что в пульте", OUT_CONFIRMED in pult)

# Документ передачи обещает владельцу три разные подписи на экране. Обещание держится
# кодом обоих экранов, поэтому проверяется вместе с ним — и проверяется именно тот
# раздел, а не файл целиком: слово, найденное где угодно, не доказывает обещания.
peredacha = open(os.path.join(BASE, "deploy", "ПЕРЕДАЧА.md"), encoding="utf-8").read()
section = re.search(r"## Выход ролика.*?(?=\n## )", peredacha, re.S)
check_true("в ПЕРЕДАЧА.md есть раздел про выход ролика", section is not None)
part = section.group(0) if section else ""
check_true("раздел обещает три разные подписи", "три разные подписи" in part)
# Слов «пульт» и «кабинет» в документах заказчика быть не должно: у нас ОДНА студия
# (решение владельца, его же проверяет tests/test_one_studio_texts.py). Раздел обязан
# назвать все три места, но словами самой студии.
for word in ("ленте канала", "карточке канала", "Журнале ролика"):
    check_true(f"раздел называет экран: {word}", word in part)
# Раньше тест требовал от документа фразу «вместо даты стоит сама причина» — а код так
# не делает: дата остаётся, причина показывается рядом. Тест закреплял ошибку документа,
# то есть сторожил ровно ту неправду, ради искоренения которой и заводился.
check_true("раздел объясняет, что делает проверка с найденным дефектом",
           "причина пишется в сам выпуск" in part)
check_true("и что фаза при этом не повышается", "Фаза не повышается" in part)

print("Незнакомый цвет состояния не роняет карточку канала")
# Цвет в карточке брался квадратными скобками, без запасного значения: любое новое
# состояние — а их добавляли уже дважды — роняло бы /channels/{cid} пятисоткой. Экран про
# сотню каналов не должен падать из-за одного незнакомого слова.
_real_look = U.release_look
U.release_look = lambda rel, tz: ("состояние из будущего", "цвета-такого-нет")
try:
    r = client.get(f"/channels/{CID}")
    check("страница всё равно открылась", r.status_code, 200)
    check_true("и показала само состояние", "состояние из будущего" in r.text)
finally:
    U.release_look = _real_look

print()
if FAILS:
    print(f"НЕ ПРОШЛО: {len(FAILS)}")
    for f in FAILS:
        print(f"  · {f}")
    sys.exit(1)
print("все проверки прошли")
sys.exit(0)
