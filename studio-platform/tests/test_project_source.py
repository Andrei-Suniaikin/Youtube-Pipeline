#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Источник проекта: ссылка донора запоминается, а поле разбирается сервером.

Повод (20.09.2026, владелец): «щас попробовал видос по новой запустить, ссылку на
донора не сохранило, плюс на этой странице не понимаю почему у нас сверху есть ссылка
на донора а потом снизу висит исходный текст какой то… желательно чтобы в строку он и
текст и ссылку распознавал».

Так и было: `ui_text_create` превращал ссылку в текст (`donor_text`) и забывал её —
у проекта не оставалось ни строчки о том, из чего он сделан. Здесь проверяется
обратное:

  * ссылка запоминается ДО похода за субтитрами (донор молчит — поле всё равно не
    пустеет) и помечается «по ней заказывали» только после удачного заказа;
  * `GET /ui/projects/{pid}` отдаёт её в `origin`, а `projects.settings` остаются
    чистыми — иначе ссылка уехала бы в следующий проект по наследованию настроек
    кабинета (`server/main.py` ≈4872) и в снимок пресета (≈14275);
  * `POST /ui/source/detect` отвечает, ссылка это или текст, БЕЗ сети и без денег;
  * сохранение сценария не сдвигает пометку источника: проект, сделанный по ссылке,
    остаётся сделанным по ней и после «Взять в проект».

Ни сети, ни соседей, ни стенда: `urlopen` и `donor_text` подменены, база временная.

    .venv/bin/python tests/test_project_source.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_in(label, needle, haystack):
    if needle in (haystack or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали:   {needle!r}\n        в строке: {haystack!r}")
        FAILS.append(label)


CHILD = r'''
# -*- coding: utf-8 -*-
"""Поднимает пульт на временной базе, подменяет соседей и снимает поведение источника."""
import json, os, sqlite3, sys, time, urllib.error, urllib.request

DB = os.environ["VS_DEV_DB"]
NOW = time.time()

SCHEMA = """
CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT);
CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, dir_path TEXT,
  status TEXT, created REAL, updated REAL, settings TEXT);
CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT, project_id TEXT,
  kind TEXT, status TEXT, created REAL, started REAL, finished REAL, params TEXT,
  uid INTEGER, progress REAL, output_rel TEXT, error TEXT, claimed REAL, stage TEXT,
  claim_token TEXT, provider TEXT, lane TEXT);
CREATE TABLE project_service_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT,
  project_id TEXT, service TEXT, job_id TEXT, created REAL);
CREATE TABLE presets(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, name TEXT,
  settings TEXT, created REAL);
CREATE TABLE pub_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT,
  name TEXT, lang TEXT, schedule TEXT, tz TEXT, horizon_days INTEGER, uc TEXT,
  preset_rewrite TEXT, preset_cover TEXT, preset_vsgen_id INTEGER);
CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT,
  channel_id INTEGER, version INTEGER, donor_url TEXT, donor_title TEXT, position REAL,
  slot_at REAL, status TEXT, created REAL, updated REAL);
CREATE TABLE pub_releases(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
  tenant_id TEXT, plan_item_id INTEGER, plan_item_version INTEGER, channel_id INTEGER,
  uc TEXT, slot_at REAL, project_id TEXT, status TEXT, phase TEXT, video_id TEXT,
  blocked_reason TEXT, attempts INTEGER, created REAL, updated REAL);
CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
  service TEXT, kind TEXT, remote_job_id TEXT, status TEXT, payload TEXT, error TEXT,
  created REAL, updated REAL);
"""

# Один и тот же ролик в двух написаниях: для человека это один донор, и для повторного
# заказа — тоже. Разойтись здесь значит заплатить за один текст дважды.
LINK = "https://www.youtube.com/watch?v=Ng0hmsrr7l4"
LINK_SAME = "https://youtu.be/Ng0hmsrr7l4?si=ZZZ"
LINK_OTHER = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
LINK_REL2 = "https://www.youtube.com/watch?v=aBcDeFgHiJk"
# Чужой адрес, у которого хвост пути — ровно одиннадцать разрешённых знаков. Для
# `pub_ids.video_id` это «идентификатор ролика», и до 20.09.2026 пульт объявлял такую
# ссылку роликом YouTube, а `donor_subs` шла с огрызком в YouTube и жгла выход.
LINK_FAKE_TAIL = "https://example.com/watch/aAbBcCdDeEf"


def build():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO admin_state VALUES('pub_key_rewrite','k-rw',0)")
    c.execute("INSERT INTO users VALUES(1,'dev','dev')")
    # Проект, заведённый руками: выпуска у него нет, и источник ему взять неоткуда,
    # кроме как с экрана.
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated,settings)"
              " VALUES('p_hand','dev','Проект от 20.09 21:10','draft',?,?,"
              "'{\"engine\":\"lumean\"}')", (NOW, NOW))
    # Второй такой же — на нём проверяется, что вставленный текст не сочиняет проекту
    # ссылку и не трогает чужую память.
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated,settings)"
              " VALUES('p_paste','dev','Проект со вставленным текстом','draft',?,?,'{}')",
              (NOW, NOW))
    # Проект под пакетный запуск: источник ему даёт карточка пачки.
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated,settings)"
              " VALUES('p_batch','dev','Проект под пачку','draft',?,?,'{}')", (NOW, NOW))
    # Проект ночного движка: его источник — ссылка выпуска, и она записана в плане.
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated,settings)"
              " VALUES('p_rel','dev','Проект выпуска','draft',?,?,'{}')", (NOW, NOW))
    # Второй такой же — на нём проверяется, что «Сохранить текст» не прячет ссылку
    # выпуска: она у проекта единственная, и по ней работает движок.
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated,settings)"
              " VALUES('p_rel2','dev','Проект выпуска со сценарием','draft',?,?,'{}')",
              (NOW, NOW))
    c.execute("INSERT INTO pub_channels(id,tenant_id,name,lang,schedule,tz,horizon_days,"
              "uc) VALUES(7,'dev','Демо канал','ru','{}','Europe/Warsaw',3,'uc-1')")
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_title,position,slot_at,status,created,updated)"
              " VALUES(11,'dev',7,1,?,'',1,?,'frozen',?,?)", (LINK_OTHER, NOW, NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,attempts,"
              "created,updated) VALUES('r-1','dev',11,1,7,'uc-1',?,'p_rel','running',"
              "'source_ready',0,?,?)", (NOW, NOW, NOW))
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_title,position,slot_at,status,created,updated)"
              " VALUES(12,'dev',7,1,?,'',2,?,'frozen',?,?)", (LINK_REL2, NOW, NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,attempts,"
              "created,updated) VALUES('r-2','dev',12,1,7,'uc-1',?,'p_rel2','running',"
              "'source_ready',0,?,?)", (NOW, NOW, NOW))
    c.commit()
    c.close()


def calls_schema():
    """Журнал платных вызовов — настоящей схемой соседа, а не упрощённой копией:
    пакетный запуск без него не работает вовсе, и подделка проверяла бы подделку."""
    import pub_calls
    c = sqlite3.connect(DB)
    pub_calls.ensure_calls_schema(c)
    c.commit()
    c.close()


class FakeResponse:
    def __init__(self, payload, status=200, url=""):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status
        self._url = url

    def read(self):
        return self.payload

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


ORDERS = []


def fake_urlopen(req, timeout=None):
    """Сосед-рерайт: принимает загрузку и заказ. Ничего не считает и не платит."""
    url = req.full_url if hasattr(req, "full_url") else str(req)
    ORDERS.append(url)
    if url.endswith("/upload"):
        return FakeResponse({"filename": "донор.txt"})
    if url.endswith("/jobs/rewrite"):
        return FakeResponse({"job_id": "rw-" + str(len(ORDERS))})
    if url.endswith("/jobs/generation"):
        return FakeResponse({"job_id": "gen-" + str(len(ORDERS))})
    return FakeResponse({})


class Req:
    """Минимальный запрос: обработчикам нужен только `state.sid`."""

    class State:
        sid = "sid-test"

    state = State()


build()
sys.path[:0] = [os.path.join(os.environ["VS_BASE"], "web"),
                os.path.join(os.environ["VS_BASE"], "server"),
                os.path.join(os.environ["VS_BASE"], "workers")]
import app
from fastapi import HTTPException

calls_schema()

urllib.request.urlopen = fake_urlopen

# Кабинет в этом прогоне не поднят: сценарий писать некуда и незачем — проверяется,
# что его зовут ровно тогда, когда прислали текст, и не зовут, когда прислали ссылку.
CABINET = []


def fake_cabinet_form(path, fields, request, timeout=0.0, expect_json=True):
    CABINET.append((path, dict(fields)))
    return {"ok": True, "url": ""}


app._cabinet_form = fake_cabinet_form

out = {"detect": {}, "steps": []}

# ── 0. Чтение — горячий путь и писать не должно ──────────────────────────────
# Экран проекта и разбор поля читают источник постоянно. Если бы чтение заводило
# схему, на занятой базе оно упиралось бы в «database is locked» и поле пустело бы
# именно тогда, когда работы много.
c = app.db()
out["read_before_write"] = app._project_origin(c, "p_hand")
out["tables_after_read"] = sorted(
    r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))
c.close()

# ── 1. Разбор поля: что это вообще такое ──────────────────────────────────────
for name, value in (
        ("пусто", "   "),
        ("ссылка", LINK),
        ("ссылка с мусором вокруг", "  " + LINK_SAME + "  "),
        ("голый идентификатор", "Ng0hmsrr7l4"),
        ("тема из одиннадцати знаков", "Top-10-2026"),
        ("слово из одиннадцати букв", "programming"),
        ("чужая ссылка", "https://vk.com/video-1_2"),
        ("чужая ссылка с хвостом в 11 знаков", LINK_FAKE_TAIL),
        ("слово youtube в чужом адресе",
         "https://evil.example/?u=youtube.com/watch?v=Ng0hmsrr7l4"),
        ("короткая ссылка телефона", "https://youtu.be/Ng0hmsrr7l4"),
        ("shorts", "https://www.youtube.com/shorts/Ng0hmsrr7l4"),
        ("трансляция", "https://www.youtube.com/live/Ng0hmsrr7l4"),
        ("канал с именем в 11 букв", "https://www.youtube.com/c/SomeChannel"),
        ("плейлист", "https://www.youtube.com/playlist?list=PLabcdefghijk"),
        ("мобильный поддомен", "https://m.youtube.com/watch?v=Ng0hmsrr7l4"),
        ("две ссылки", LINK + "\n" + LINK_OTHER),
        ("тема", "Как строили пирамиды"),
        ("сценарий", "Первая строка сценария.\n" * 20),
        ("текст со ссылкой внутри", "смотри " + LINK + " и перескажи")):
    r = app.detect_source(value)
    out["detect"][name] = {k: r[k] for k in ("kind", "mode", "video_id", "url",
                                             "topic_possible")}
    out["detect"][name]["says"] = r["problem"] or r["note"]

# ── 2. Донор не отдал расшифровку: ссылка всё равно запомнена ────────────────
def boom(url):
    raise RuntimeError("субтитры недоступны")


app.donor_text = boom
try:
    app.ui_text_create(app.TextIn(mode="link", source=LINK, languages=["ru"],
                                  project_id="p_hand"), Req())
    out["steps"].append("отказа не было")
except HTTPException as exc:
    out["steps"].append(f"{exc.status_code}: {exc.detail}")
c = app.db()
out["after_fail"] = app._project_origin(c, "p_hand")
c.close()

# ── 3. Заказ удался: та же ссылка, теперь с отметкой об оплате ───────────────
app.donor_text = lambda url: "расшифровка ролика " * 50
ORDERS.clear()
answer = app.ui_text_create(app.TextIn(mode="link", source=LINK, languages=["ru"],
                                       project_id="p_hand"), Req())
out["order_answer"] = {"chars": answer.get("chars"),
                       "origin_kind": (answer.get("origin") or {}).get("kind"),
                       "origin_value": (answer.get("origin") or {}).get("value")}
c = app.db()
out["after_order"] = app._project_origin(c, "p_hand")
out["settings_hand"] = c.execute("SELECT settings FROM projects WHERE id='p_hand'"
                                 ).fetchone()["settings"]
c.close()

# ── 4. Повтор по той же ссылке в другом написании — предупреждение до денег ──
out["detect_repeat"] = app.ui_source_detect(app.DetectIn(value=LINK_SAME,
                                                         project_id="p_hand"))
out["detect_new_link"] = app.ui_source_detect(app.DetectIn(value=LINK_OTHER,
                                                           project_id="p_hand"))
out["detect_no_project"] = app.ui_source_detect(app.DetectIn(value=LINK))

# ── 5. Экран проекта отдаёт источник вместе со сценарием ────────────────────
screen = app.ui_project("p_hand")
out["screen_origin"] = screen["origin"]
out["screen_has_source_field"] = "source" in screen

# ── 6. Вставленный текст не стирает ссылку, из которой сделан проект ────────
ORDERS.clear()
app.ui_text_create(app.TextIn(mode="text", source="свой текст, без донора",
                              languages=["ru"], project_id="p_hand"), Req())
c = app.db()
out["after_paste_hand"] = app._project_origin(c, "p_hand")
c.close()
# А проекту, у которого источника не было, вставленный текст его и назначает.
app.ui_text_create(app.TextIn(mode="text", source="совсем свой текст",
                              languages=["ru"], project_id="p_paste"), Req())
c = app.db()
out["after_paste_new"] = app._project_origin(c, "p_paste")
c.close()

# ── 7. Тема генерации с нуля — та же память ─────────────────────────────────
app.ui_text_create(app.TextIn(mode="topic", source="Как строили пирамиды",
                              languages=["ru"], preset="бриф"), Req())   # без проекта
app.ui_text_create(app.TextIn(mode="topic", source="Как строили пирамиды",
                              languages=["ru"], preset="бриф",
                              project_id="p_paste"), Req())
c = app.db()
out["after_topic"] = app._project_origin(c, "p_paste")
c.close()

# ── 8. Сохранение источника руками ──────────────────────────────────────────
CABINET.clear()
app.ui_project_source_save("p_paste", app.SourceIn(link=LINK_OTHER), Req())
c = app.db()
out["saved_link"] = app._project_origin(c, "p_paste")
c.close()
out["cabinet_after_link"] = list(CABINET)

CABINET.clear()
app.ui_project_source_save("p_paste", app.SourceIn(text="сценарий руками"), Req())
c = app.db()
out["after_text_save"] = app._project_origin(c, "p_paste")
c.close()
out["cabinet_after_text"] = [p for p, _f in CABINET]

CABINET.clear()
app.ui_project_source_save("p_paste", app.SourceIn(text="из файла",
                                                   file="сценарий.txt"), Req())
c = app.db()
out["after_file_save"] = app._project_origin(c, "p_paste")
c.close()

app.ui_project_source_save("p_paste", app.SourceIn(link=""), Req())
c = app.db()
out["after_link_clear"] = app._project_origin(c, "p_paste")
c.close()

for name, body in (("пусто", app.SourceIn()),
                   ("не ссылка", app.SourceIn(link="вообще не ссылка")),
                   ("чужая ссылка", app.SourceIn(link="https://vk.com/video-1_2"))):
    try:
        app.ui_project_source_save("p_paste", body, Req())
        out.setdefault("refusals", {})[name] = "отказа не было"
    except HTTPException as exc:
        out.setdefault("refusals", {})[name] = f"{exc.status_code}: {exc.detail}"

# ── 9. Проект выпуска: источник берётся из плана, ничего не записывая ───────
c = app.db()
out["release_origin"] = app._project_origin(c, "p_rel")
c.close()
# А если человек задал выпускному проекту СВОЮ ссылку — про расхождение надо сказать.
app.ui_project_source_save("p_rel", app.SourceIn(link=LINK), Req())
c = app.db()
out["release_conflict"] = app._project_origin(c, "p_rel")
c.close()

# ── 9а. Проект движка: сохранение сценария НЕ прячет ссылку выпуска ────────
# Находка рецензента. «Сохранить текст» проставляет kind='text', и запись проекта
# перекрывала ссылку выпуска: поле источника уходило в сценарий, а `release_url`
# считался пустым «как совпадающий». У проекта ночного движка ссылка исчезала
# с экрана целиком — там, где по ней и работают.
c = app.db()
out["rel2_before"] = app._project_origin(c, "p_rel2")
c.close()
CABINET.clear()
app.ui_project_source_save("p_rel2", app.SourceIn(text="сценарий взят руками"), Req())
c = app.db()
out["rel2_after_text"] = app._project_origin(c, "p_rel2")
c.close()

# ── 9б. Пакетный запуск: ссылка карточки тоже запоминается ─────────────────
# Та же потеря была у второй двери: карточка пачки раздаёт ссылку отмеченным
# проектам, и после оплаты проект опять не знал, из чего он сделан.
app.donor_subs = lambda url, **kw: {"text": "расшифровка донора " * 40, "cached": True}
app.rewrite_preset_config = lambda tenant, preset: {"model": "gpt-4.1-nano"}
batch = app.ui_projects_batch_text(
    app.BatchTextIn(pids=["p_batch"], languages=["ru"], source_url=LINK), Req())
out["batch"] = [{k: v for k, v in item.items() if k in ("id", "action", "why", "source")}
                for item in batch["projects"]]
c = app.db()
out["after_batch"] = app._project_origin(c, "p_batch")
c.close()

# ── 9в. Удалённый проект не оставляет за собой памяти об источнике ─────────
# Чужой таблице (`projects`) пульт триггеров не ставит, поэтому осиротевшие строки
# подметаются на записи. Проверяется именно это: проект удалили — строка ушла.
c = app.db()
c.execute("DELETE FROM projects WHERE id='p_batch'")
c.commit()
c.close()
app._origin_link("p_hand", LINK)          # любая запись — она же и уборка
c = app.db()
out["orphans"] = [r[0] for r in c.execute(
    "SELECT project_id FROM project_origin ORDER BY project_id")]
c.close()

# ── 9г. Уборка упала — ссылка всё равно записана ───────────────────────────
# Находка рецензента: запись ссылки и подметание осиротевших строк лежали в одном
# `try` и под одним `commit`. `projects` — чужая таблица кабинета, и её недоступность
# откатывала ВМЕСТЕ с уборкой саму запись ссылки — уже после того, как за текст
# заплатили. Прячем таблицу и смотрим, переживёт ли ссылка падение уборки.
c = app.db()
c.execute("ALTER TABLE projects RENAME TO projects_hidden")
c.commit()
c.close()
app._origin_link("p_paste", LINK_OTHER, ordered=True)
c = app.db()
out["after_sweep_fail"] = app._origin_read(c, "p_paste")
c.execute("ALTER TABLE projects_hidden RENAME TO projects")
c.commit()
c.close()

# ── 10. Очередь называет задачу ссылкой, а не именем проекта ────────────────
c = app.db()
c.execute("INSERT INTO project_service_jobs(tenant_id,project_id,service,job_id,created)"
          " VALUES('dev','p_hand','rewrite','rw-queue',?)", (NOW,))
c.commit()
rows = [{"id": "rw-queue", "service": "rewrite"}]
app._name_neighbour_rows(c, "rewrite", rows)
out["queue_row"] = {"title": rows[0]["title"], "why": rows[0]["title_why"]}
c.close()

print("<<<JSON>>>" + json.dumps(out, ensure_ascii=False, default=str))
'''


def run_child(extra_env=None):
    env = dict(os.environ)
    env["HOME"] = os.path.join(BASE, "dev", "home")
    env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
    env["VS_BASE"] = BASE
    env["VS_DEV_TENANT"] = "dev"
    env.update(extra_env or {})
    with tempfile.TemporaryDirectory() as tmp:
        env["VS_DEV_DB"] = os.path.join(tmp, "source.db")
        env["VS_SERVER_DB"] = os.path.join(tmp, "source.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        script = os.path.join(tmp, "child.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(CHILD))
        done = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              env=env, timeout=300)
    if "<<<JSON>>>" not in done.stdout:
        raise SystemExit("пульт не поднялся:\n" + (done.stderr or done.stdout)[-3000:])
    return json.loads(done.stdout.split("<<<JSON>>>", 1)[1].splitlines()[0])


LINK = "https://www.youtube.com/watch?v=Ng0hmsrr7l4"
LINK_OTHER = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
LINK_REL2 = "https://www.youtube.com/watch?v=aBcDeFgHiJk"
LINK_FAKE_TAIL = "https://example.com/watch/aAbBcCdDeEf"


def main() -> int:
    got = run_child()
    d = got["detect"]

    print("Чтение источника ничего не пишет и на пустой базе не падает")
    check("до первой записи источника нет",
          (got["read_before_write"]["kind"], got["read_before_write"]["donor_url"]),
          ("", ""))
    check("и таблица от чтения не заводится",
          "project_origin" in got["tables_after_read"], False)

    print("\nРазбор поля: сервер сам решает, ссылка это или текст")
    check("ссылка опознана", (d["ссылка"]["kind"], d["ссылка"]["mode"]),
          ("link", "link"))
    check("идентификатор ролика достали", d["ссылка"]["video_id"], "Ng0hmsrr7l4")
    check("пробелы и хвост ?si= ссылке не мешают",
          d["ссылка с мусором вокруг"]["video_id"], "Ng0hmsrr7l4")
    check("короткая ссылка телефона — тот же ролик",
          (d["короткая ссылка телефона"]["kind"],
           d["короткая ссылка телефона"]["video_id"]), ("link", "Ng0hmsrr7l4"))
    check("shorts — тот же ролик",
          (d["shorts"]["kind"], d["shorts"]["video_id"]), ("link", "Ng0hmsrr7l4"))
    check("мобильный поддомен — тот же ролик",
          (d["мобильный поддомен"]["kind"], d["мобильный поддомен"]["video_id"]),
          ("link", "Ng0hmsrr7l4"))
    check("трансляция — тоже ролик",
          (d["трансляция"]["kind"], d["трансляция"]["video_id"]),
          ("link", "Ng0hmsrr7l4"))
    check("канал с одиннадцатибуквенным именем роликом не объявляется",
          (d["канал с именем в 11 букв"]["kind"],
           d["канал с именем в 11 букв"]["video_id"]), ("link_other", ""))
    check("плейлист — не ролик",
          (d["плейлист"]["kind"], d["плейлист"]["video_id"]), ("link_other", ""))
    check("голые одиннадцать знаков роликом не считаются",
          (d["голый идентификатор"]["kind"], d["голый идентификатор"]["video_id"]),
          ("text", ""))
    check_in("и сказано, что делать, если это всё-таки ролик", "вставьте ссылку целиком",
             d["голый идентификатор"]["says"])
    check("тема из одиннадцати знаков остаётся темой",
          (d["тема из одиннадцати знаков"]["kind"],
           d["тема из одиннадцати знаков"]["topic_possible"]), ("text", True))
    check("слово из одиннадцати букв роликом не считается",
          d["слово из одиннадцати букв"]["kind"], "text")
    check("чужая ссылка не выдаётся за донора",
          (d["чужая ссылка"]["kind"], d["чужая ссылка"]["mode"]), ("link_other", ""))
    check_in("и объясняет, почему так заказывать нельзя", "субтитры брать неоткуда",
             d["чужая ссылка"]["says"])
    check("чужой адрес с хвостом в 11 знаков роликом YouTube не объявляется",
          (d["чужая ссылка с хвостом в 11 знаков"]["kind"],
           d["чужая ссылка с хвостом в 11 знаков"]["video_id"]), ("link_other", ""))
    check("слово youtube в чужом адресе ничего не доказывает",
          (d["слово youtube в чужом адресе"]["kind"],
           d["слово youtube в чужом адресе"]["video_id"]), ("link_other", ""))
    check("две ссылки в поле — отказ, а не выбор наугад",
          (d["две ссылки"]["kind"], d["две ссылки"]["mode"]), ("many_links", ""))
    check("пустое поле называется пустым", d["пусто"]["kind"], "empty")
    check("короткая строка — текст, но про тему сказано",
          (d["тема"]["kind"], d["тема"]["topic_possible"]), ("text", True))
    check("длинный сценарий темой не притворяется",
          (d["сценарий"]["kind"], d["сценарий"]["topic_possible"]), ("text", False))
    check("ссылка внутри текста оставляет его текстом",
          (d["текст со ссылкой внутри"]["kind"],
           d["текст со ссылкой внутри"]["topic_possible"]), ("text", False))

    print("\nСсылка запоминается ДО похода за субтитрами")
    check_in("донор молчит — это отказ заказа", "текст ролика не получен",
             got["steps"][0])
    check("но ссылка у проекта осталась", got["after_fail"]["donor_url"], LINK)
    check("и не помечена как оплаченная", got["after_fail"]["ordered"], 0)
    check("поле экрана подставит именно её",
          (got["after_fail"]["kind"], got["after_fail"]["value"]), ("link", LINK))

    print("\nУдачный заказ: ссылка та же, отметка об оплате появилась")
    check("ответ вернул источник экрану",
          (got["order_answer"]["origin_kind"], got["order_answer"]["origin_value"]),
          ("link", LINK))
    check("отметка «по ней заказывали» поставлена",
          got["after_order"]["ordered"] > 0, True)
    check_in("и видна человеком", "заказывали текст", got["after_order"]["why"])

    print("\nНастройки проекта ссылкой не пачкаются (иначе она уедет в другой проект)")
    check("ссылки в projects.settings нет",
          "Ng0hmsrr7l4" in (got["settings_hand"] or ""), False)
    check("чужие настройки на месте", "lumean" in (got["settings_hand"] or ""), True)

    print("\nПовтор по той же ссылке предупреждает ДО нажатия")
    rep = got["detect_repeat"]
    check("другое написание — тот же донор", rep["project"]["same_link"], True)
    check_in("и про второй платный рерайт сказано", "второй платный рерайт",
             rep["warn"])
    check("новая ссылка повтором не считается",
          got["detect_new_link"]["project"]["same_link"], False)
    check_in("и предупреждает о замене", "заказ по новой её заменит",
             got["detect_new_link"]["warn"])
    check("без проекта разбор ничего про проект не выдумывает",
          "project" in got["detect_no_project"], False)

    print("\nЭкран проекта получает источник вместе со сценарием")
    check("origin отдан", got["screen_origin"]["donor_url"], LINK)
    check("сценарий по-прежнему отдельным полем", got["screen_has_source_field"], True)

    print("\nВставленный текст не стирает ссылку, из которой сделан проект")
    check("ссылка на месте", got["after_paste_hand"]["donor_url"], LINK)
    check("пометка источника не сдвинулась", got["after_paste_hand"]["kind"], "link")
    check("а у проекта без источника текст им и становится",
          got["after_paste_new"]["kind"], "text")

    print("\nТема генерации с нуля тоже запоминается")
    check("тема записана", got["after_topic"]["topic"], "Как строили пирамиды")
    check("и подставится в поле",
          (got["after_topic"]["kind"], got["after_topic"]["value"]),
          ("topic", "Как строили пирамиды"))

    print("\nСохранение источника руками")
    check("ссылка сохранена без единого похода в кабинет",
          (got["saved_link"]["donor_url"], got["cabinet_after_link"]),
          (LINK_OTHER, []))
    check("сценарий по-прежнему уходит в кабинет",
          got["cabinet_after_text"], ["/api/projects/p_paste/source"])
    check("и пометку источника не двигает", got["after_text_save"]["kind"], "link")
    check("файл источником считается",
          (got["after_file_save"]["kind"], got["after_file_save"]["file_name"]),
          ("file", "сценарий.txt"))
    check("пустая ссылка забывает ссылку, а не проект",
          (got["after_link_clear"]["donor_url"], got["after_link_clear"]["kind"]),
          ("", "file"))
    check_in("сохранять нечего — отказ", "нечего сохранять", got["refusals"]["пусто"])
    check_in("текст вместо ссылки — отказ с причиной", "в поле ссылки не ссылка, а текст",
             got["refusals"]["не ссылка"])
    check_in("чужая ссылка — отказ с причиной", "субтитры брать неоткуда",
             got["refusals"]["чужая ссылка"])

    print("\nПроект выпуска: источник читается из плана канала")
    check("ссылка выпуска показана без всякой записи",
          (got["release_origin"]["donor_url"], got["release_origin"]["from"]),
          (LINK_OTHER, "release"))
    check_in("и сказано, откуда она", "ссылка выпуска канала",
             got["release_origin"]["why"])
    check("своя ссылка выпускную не прячет",
          (got["release_conflict"]["donor_url"], got["release_conflict"]["release_url"]),
          (LINK, LINK_OTHER))
    check_in("о расхождении сказано прямо", "движок берёт её",
             got["release_conflict"]["why"])

    print("\nСсылка выпуска не исчезает от «Сохранить текст»")
    check("до сохранения она стоит прямо в поле",
          (got["rel2_before"]["kind"], got["rel2_before"]["value"]),
          ("link", LINK_REL2))
    check("после сохранения поле ушло в сценарий",
          got["rel2_after_text"]["kind"], "text")
    check("но ссылка выпуска осталась видимой отдельной строкой",
          got["rel2_after_text"]["release_url"], LINK_REL2)
    check_in("и сказано, кто по ней работает", "ночной движок",
             got["rel2_after_text"]["why"])

    print("\nПадение уборки не отменяет саму запись ссылки")
    check("ссылка записана, хотя чужая таблица недоступна",
          got["after_sweep_fail"].get("donor_url"), LINK_OTHER)
    check("и отметка об оплате вместе с ней",
          (got["after_sweep_fail"].get("ordered") or 0) > 0, True)

    print("\nПакетный запуск помнит ссылку карточки")
    check("задача поставлена, и источник — ссылка карточки",
          [[i["action"], i["source"]] for i in got["batch"]],
          [["ordered", "replacement-url"]])
    check("и ссылка карточки записана проекту",
          (got["after_batch"]["kind"], got["after_batch"]["donor_url"]), ("link", LINK))
    check("с отметкой об оплате", got["after_batch"]["ordered"] > 0, True)

    check("память удалённого проекта подметена",
          got["orphans"], ["p_hand", "p_paste", "p_rel", "p_rel2"])

    print("\nОчередь называет задачу ссылкой, а не именем проекта")
    check("в строке очереди стоит ссылка", got["queue_row"]["title"], LINK)
    check_in("и объяснено, почему не название ролика", "названия ролика у нас нет",
             got["queue_row"]["why"])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО {len(FAILS)}: " + "; ".join(FAILS))
        return 1
    print("всё сходится")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
