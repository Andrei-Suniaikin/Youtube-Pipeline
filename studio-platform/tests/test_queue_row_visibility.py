#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строка очереди объясняет себя: имя, стадия, причина отказа, сколько уже идёт.

Повод (20.09.2026). Владелец вставил в проекте ссылку, нажал «Получить текст» и
увидел «просто задачу в очереди, даже без названия». В базе соседа в это время было
другое: задача 0a6c6716dbd1 создана в 18:44, взята в работу в 18:48, в логе «Подготовка
→ Обработка… → ru: старт — 1 частей»; предыдущая 5a6a1f2eb6ba упала на сети
(«Ошибка: сеть недоступна (E-NET)»). Работа шла — не видно её было в пульте.

Причина нашлась в одной букве: рерайт отдаёт в списке `job_id` (rewrite/api.py
`_job_to_summary`), а Cover — `id` (cover/api_cover.py `ApiJobListItem`). Пульт читал
у обоих `id` и у рерайта получал пусто: ни номера, ни названия. Ни стадии, ни причины
списочная ручка соседа не отдаёт вовсе — они живут в логе задачи.

Вторая находка (рецензент, 20.09). Списочная ручка Cover времён работы не отдаёт
вовсе, и пульт превращал это незнание в утверждение: законченная обложка говорила
«в работу не бралась», идущая — «в очереди 13 мин». Здесь проверяется, что молчание
соседа называется молчанием, а настоящая нулевая отметка начала (рерайт поле отдаёт
всегда) по-прежнему читается как «не бралась».

Третья. Бюджет дочитываний уходил ДО фильтров: на экране «Обложки» сеть тратилась на
задачи рерайта, которых там не будет. Здесь проверяется, что на отфильтрованном экране
к чужому сервису не уходит ни одного запроса.

Ни сети, ни соседей, ни стенда: `urlopen` подменён, база временная.

    .venv/bin/python tests/test_queue_row_visibility.py
"""
from __future__ import annotations

import collections
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
    if needle in haystack:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали:   {needle!r}\n        в строке: {haystack!r}")
        FAILS.append(label)


CHILD = r'''
# -*- coding: utf-8 -*-
"""Поднимает пульт на временной базе, подменяет соседей и снимает ответ /ui/queue."""
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
CREATE TABLE pub_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT,
  name TEXT, lang TEXT, schedule TEXT, tz TEXT, horizon_days INTEGER, uc TEXT,
  preset_rewrite TEXT);
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


def build():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO admin_state VALUES('pub_key_rewrite','k-rw',0)")
    c.execute("INSERT INTO admin_state VALUES('pub_key_cover','k-cov',0)")
    c.execute("INSERT INTO users VALUES(1,'dev','dev')")
    c.execute("INSERT INTO pub_channels(id,tenant_id,name,lang,schedule,tz,horizon_days,uc)"
              " VALUES(7,'dev','Демо канал','ru','{}','Europe/Warsaw',3,'uc-1')")
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_title,position,slot_at,status,created,updated)"
              " VALUES(11,'dev',7,1,'https://youtu.be/Ng0hmsrr7l4',"
              "'Полное название ролика донора',1,?,'frozen',?,?)", (NOW, NOW, NOW))
    # Выпуск канала: у него есть строка плана, значит есть и название ролика.
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
              "attempts,created,updated) VALUES('r-1','dev',11,1,7,'uc-1',?,'p_rel',"
              "'running','source_ready',0,?,?)", (NOW, NOW, NOW - 3600))
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,"
              "status,created,updated) VALUES('r-1','rewrite','job','rw-release',"
              "'done',?,?)", (NOW, NOW))
    # Проект владельца: задачу поставили ссылкой с экрана проекта, названия ролика
    # в кабинете нет — есть только имя проекта.
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated)"
              " VALUES('p_a66a80ca50','dev','Проект от 20.09 18:40','draft',?,?)",
              (NOW - 900, NOW - 900))
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated)"
              " VALUES('p_bc85c4bc85','dev','Проект от 20.09 17:16','draft',?,?)",
              (NOW - 7000, NOW - 7000))
    c.execute("INSERT INTO projects(id,tenant_id,name,status,created,updated)"
              " VALUES('p_rel','dev','Проект выпуска','draft',?,?)", (NOW, NOW))
    for pid, jid, when in (("p_a66a80ca50", "0a6c6716dbd1", NOW - 900),
                           ("p_bc85c4bc85", "5a6a1f2eb6ba", NOW - 7000),
                           ("p_a66a80ca50", "cov-1", NOW - 800)):
        svc = "cover" if jid.startswith("cov") else "rewrite"
        c.execute("INSERT INTO project_service_jobs(tenant_id,project_id,service,"
                  "job_id,created) VALUES('dev',?,?,?,?)", (pid, svc, jid, when))
    # Своя задача кабинета: у проекта нет выпуска, и раньше строка подписывалась
    # идентификатором проекта.
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,"
              "started,finished,uid,progress,stage,provider,error)"
              " VALUES(41,'dev','p_a66a80ca50','images','running',?,?,NULL,1,0.5,"
              "'кадр 3 из 6','openai','')", (NOW - 500, NOW - 200))
    # Задачу отменили ДО того, как её взяли в работу: времени начала у неё нет, и по
    # одним числам она читается как «в очереди второй час» — про то, чего в очереди
    # давно нет (боевые строки 17 и 18 стенда 20.09).
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,"
              "started,finished,uid,progress,stage,provider,error)"
              " VALUES(18,'dev','p_bc85c4bc85','images','cancelled',?,NULL,NULL,1,NULL,"
              "'','','')", (NOW - 6500,))
    c.commit()
    c.close()


REW_LOG_RUN = (
    "[18:44:24] Задача принята\n[18:44:24] Подготовка\n[18:46:25] Обработка…\n"
    "[18:46:52] ru: старт — 1 частей\n\n"
    "[2026-09-20 18:48:24] [migrate 001] возвращено в очередь после рестарта — "
    "продолжит с места обрыва[18:48:24] Задача принята\n[18:48:25] Подготовка\n"
    "[18:50:25] Обработка…\n[18:50:52] ru: старт — 1 частей\n")
REW_LOG_FAIL = (
    "[17:24:40] Обработка…\n[17:24:42] Ошибка: сеть недоступна (E-NET)\n"
    "[17:24:42] Проверка качества\n[17:24:42] ru: не удалось завершить (E-PART)\n"
    "[17:24:42] Повторная попытка\n[17:25:02] Подготовка\n"
    "[17:27:33] Ошибка: сеть недоступна (E-NET)\n"
    "[17:27:33] ru: не удалось завершить (E-PART)\n"
    "[17:27:33] Ошибка: задача не завершена (E-FAIL)\n")

REWRITE_ITEMS = [
    # Живая задача владельца: `job_id`, а не `id`; progress_detail пуст — стадия
    # только в логе.
    {"job_id": "0a6c6716dbd1", "status": "running", "progress_percent": 0,
     "progress_detail": "", "model": "gpt-4.1-mini", "files_count": 1,
     "languages_count": 1, "created_at": NOW - 900, "started_at": NOW - 660,
     "finished_at": 0.0},
    {"job_id": "5a6a1f2eb6ba", "status": "failed", "progress_percent": 100,
     "progress_detail": "1/1 — Ng0hmsrr7l4.txt (ru)", "model": "gpt-4.1-mini",
     "files_count": 1, "languages_count": 1, "created_at": NOW - 7000,
     "started_at": NOW - 7000, "finished_at": NOW - 6400},
    {"job_id": "rw-release", "status": "completed", "progress_percent": 100,
     "progress_detail": "", "created_at": NOW - 9000, "started_at": NOW - 9000,
     "finished_at": NOW - 8000},
    {"job_id": "rw-orphan", "status": "running", "progress_percent": 0,
     "progress_detail": "", "created_at": NOW - 120, "started_at": 0.0,
     "finished_at": 0.0},
    # Задача, которую сняли до работы: поле `started_at` у рерайта ЕСТЬ, и ноль в
    # нём — это факт «не бралась», а не молчание. Строка-страж: если «не бралась»
    # заменить на «сосед не сообщает» везде подряд, сломается именно она.
    {"job_id": "rw-dropped", "status": "cancelled", "progress_percent": 0,
     "progress_detail": "", "created_at": NOW - 400, "started_at": 0.0,
     "finished_at": NOW - 390},
    # Отказ, у которого первый заход за причиной срывается сетью: неудачу нельзя
    # запоминать — иначе она спрячет причину на весь срок жизни кэша.
    {"job_id": "rw-flaky", "status": "failed", "progress_percent": 100,
     "progress_detail": "", "created_at": NOW - 300, "started_at": NOW - 300,
     "finished_at": NOW - 200},
]
# Двадцать старых отказов: бюджет подробностей должен кончиться на них, а не на
# свежих строках.
REWRITE_ITEMS += [
    {"job_id": f"old-{n:02d}", "status": "failed", "progress_percent": 100,
     "progress_detail": "", "created_at": NOW - 100000 - n,
     "started_at": NOW - 100000 - n, "finished_at": NOW - 99000 - n}
    for n in range(20)
]
# У Cover в списке нет ни `started_at`, ни `finished_at` — так отвечает
# `ApiJobListItem`, и подделка обязана молчать ровно так же.
COVER_ITEMS = [
    {"id": "cov-1", "status": "running", "created_at": NOW - 800,
     "progress_percent": 40, "results_count": 0},
    {"id": "cov-done", "status": "completed", "created_at": NOW - 5000,
     "progress_percent": 100, "results_count": 3},
    # Идущая обложка, которой дочитывать нечего: стадия у неё в списке уже есть,
    # значит времена так и останутся неизвестными — и сказать про них надо честно.
    {"id": "cov-run", "status": "running", "created_at": NOW - 600,
     "progress_percent": 60, "results_count": 1},
    # Обложка, которая ещё ждёт своей очереди. Времён работы у неё нет по существу,
    # а не по молчанию соседа: «в очереди 5 мин» — правда, и подменять её на «сосед
    # не сообщает» значит потерять единственное, что человек тут и хотел узнать.
    {"id": "cov-wait", "status": "pending", "created_at": NOW - 300,
     "progress_percent": 0, "results_count": 0},
]

ASKED = []
FLAKY_FAILED = []


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    ASKED.append(url)
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    if "/jobs?" in url:
        # Кто есть кто — по адресу соседа, а не по слову в ссылке: базы задаются
        # окружением и различаются только портом.
        items = REWRITE_ITEMS if url.startswith(REWRITE_BASE) else COVER_ITEMS
        return FakeResponse({"items": items, "total": len(items), "limit": 50,
                             "offset": 0})
    job = path.rsplit("/jobs/", 1)[-1].split("?")[0]
    # Лог рерайт отдаёт ТОЛЬКО по `include_log=true` (rewrite/api.py
    # get_job_detail: без него лог задачи из базы не читается вовсе —
    # FIX-2026-09-13 log-bloat). Подделка обязана вести себя так же, иначе
    # тест пройдёт и на коде, который забыл этот параметр попросить.
    log = (lambda text: text if "include_log=true" in url else "")
    if job == "0a6c6716dbd1":
        return FakeResponse({"job_id": job, "status": "running", "progress_percent": 0,
                             "progress_detail": "", "error": "", "warning": "",
                             "created_at": NOW - 900, "started_at": NOW - 660,
                             "finished_at": 0.0, "log_tail": log(REW_LOG_RUN)})
    if job == "5a6a1f2eb6ba":
        return FakeResponse({"job_id": job, "status": "failed", "progress_percent": 100,
                             "progress_detail": "1/1 — Ng0hmsrr7l4.txt (ru)",
                             "error": "1 из 1 задач не выполнено | no output files saved (0/1)",
                             "warning": "", "created_at": NOW - 7000,
                             "started_at": NOW - 7000, "finished_at": NOW - 6400,
                             "log_tail": log(REW_LOG_FAIL)})
    if job == "rw-orphan":
        return FakeResponse({"job_id": job, "status": "running", "progress_percent": 0,
                             "progress_detail": "", "error": "", "warning": "",
                             "created_at": NOW - 120, "started_at": 0.0,
                             "log_tail": log("[19:00:01] Задача принята\n")})
    if job == "rw-flaky":
        if not FLAKY_FAILED:
            FLAKY_FAILED.append(1)
            raise urllib.error.HTTPError(url, 500, "сосед споткнулся", {}, None)
        return FakeResponse({"job_id": job, "status": "failed", "error": "",
                             "created_at": NOW - 300, "started_at": NOW - 300,
                             "finished_at": NOW - 200,
                             "log_tail": log("[19:05:00] Ошибка: сеть недоступна (E-NET)\n")})
    if job == "rw-dropped":
        return FakeResponse({"job_id": job, "status": "cancelled", "error": "",
                             "created_at": NOW - 400, "started_at": 0.0,
                             "finished_at": NOW - 390,
                             "log_tail": log("[19:10:00] Задача снята\n")})
    if job == "cov-1":
        return FakeResponse({"id": job, "status": "running", "progress_percent": 40,
                             "progress_detail": "1/3 обложек", "error": "",
                             "created_at": NOW - 800, "started_at": NOW - 780,
                             "finished_at": 0.0})
    return FakeResponse({"job_id": job, "status": "failed", "error": "старый отказ",
                         "created_at": NOW - 100000, "log_tail": ""})


build()
sys.path[:0] = [os.path.join(os.environ["VS_BASE"], "web"),
                os.path.join(os.environ["VS_BASE"], "server"),
                os.path.join(os.environ["VS_BASE"], "workers")]
import app

REWRITE_BASE = app.REWRITE_BASE
urllib.request.urlopen = fake_urlopen

first = app.ui_queue()
first_asked = list(ASKED)
ASKED.clear()
second = app.ui_queue()
second_asked = list(ASKED)

# Третий показ — после того, как короткий срок жизни подробностей идущей задачи
# истёк. Руками, а не ожиданием: тест не должен спать пять секунд, чтобы проверить
# правило, которое и так записано числом.
ASKED.clear()
live_key = ("rewrite", "0a6c6716dbd1")
stale = app._DETAIL_CACHE.get(live_key)
if stale is not None:
    app._DETAIL_CACHE[live_key] = (time.time() - 1, stale[1], stale[2])
third = app.ui_queue()
third_asked = list(ASKED)

# Четвёртый показ — с фильтром «Обложки». Кэш подробностей чистим нарочно: дочитанное
# на прошлых показах спрятало бы лишние запросы, и тест прошёл бы на коде, который
# тратит бюджет до фильтров.
app._DETAIL_CACHE.clear()
ASKED.clear()
cover_only = app.ui_queue(service="cover")
cover_asked = [u for u in ASKED if "/jobs/" in u]

out = {
    "rows": {r["id"]: r for r in first["rows"]},
    "details": first["details"],
    "asked": first_asked,
    "asked_detail": [u for u in first_asked if "/jobs/" in u],
    "asked_detail_second": [u for u in second_asked if "/jobs/" in u],
    "asked_detail_third": [u for u in third_asked if "/jobs/" in u],
    "ttl": [app.DETAIL_TTL_LIVE, app.DETAIL_TTL_DONE],
    "flaky_first": {k: v for k, v in
                    ({r["id"]: r for r in first["rows"]}.get("rw-flaky") or {}).items()
                    if k in ("error", "reason_pending")},
    "flaky_second": {k: v for k, v in
                     ({r["id"]: r for r in second["rows"]}.get("rw-flaky") or {}).items()
                     if k in ("error", "reason_pending")},
    "services": first["services"],
    "cover_only_rows": sorted(r["id"] for r in cover_only["rows"]),
    "cover_only_details": cover_only["details"],
    "cover_only_asked": sorted(u.rsplit("/jobs/", 1)[-1].split("?")[0]
                               for u in cover_asked),
    "cover_only_from_rewrite": [u for u in cover_asked if u.startswith(REWRITE_BASE)],
    "by_channel": [r["id"] for r in app.ui_queue(channel=7)["rows"]],
    "states": {s: app._state(s) for s in
               ("completed", "completed_partial", "partial", "interrupted", "failed",
                "resetting", "pending", "done", "running", "cancelled")},
    "span": {s: app._span_ru(s) for s in (0, 41, 200, 3600, 7800)},
    "stage_glued": app._stage_from_log(REW_LOG_RUN),
    # Момент сразу после пометки о рестарте: склеенная запись ОКАЗЫВАЕТСЯ
    # последней, и именно её человек увидит стадией.
    "stage_glued_tail": app._stage_from_log(REW_LOG_RUN.split("[18:48:25]")[0]),
    "reason": app._reason_from_log(REW_LOG_FAIL),
    "reason_empty": app._reason_from_log("[10:00:00] Подготовка\n"),
}
print("<<<JSON>>>" + json.dumps(out, ensure_ascii=False, default=str))
'''


def run_child():
    env = dict(os.environ)
    env["HOME"] = os.path.join(BASE, "dev", "home")
    env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
    env["VS_BASE"] = BASE
    env["VS_DEV_TENANT"] = "dev"
    with tempfile.TemporaryDirectory() as tmp:
        env["VS_DEV_DB"] = os.path.join(tmp, "queue.db")
        env["VS_SERVER_DB"] = os.path.join(tmp, "queue.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        script = os.path.join(tmp, "child.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(CHILD))
        done = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              env=env, timeout=300)
    if "<<<JSON>>>" not in done.stdout:
        raise SystemExit("пульт не поднялся:\n" + (done.stderr or done.stdout)[-2000:])
    return json.loads(done.stdout.split("<<<JSON>>>", 1)[1].splitlines()[0])


# Строка, которой в ответе не оказалось. Нужна, чтобы пропавшая строка давала один
# внятный провал, а не обрывала проверку на первом же обращении: когда ломается чтение
# номера задачи, пропадают СРАЗУ ВСЕ строки соседа, и увидеть это надо целиком.
MISSING = {"id": "", "service": "", "title": "", "title_why": "", "stage": "",
           "error": "", "warning": "", "state": "", "channel": "", "lang": "",
           "project_id": "", "release_id": "", "age_text": "", "waiting": None,
           "working": None, "started": None, "finished": None, "reason_pending": None,
           "times_known": None}


def main() -> int:
    got = run_child()
    rows = collections.defaultdict(lambda: dict(MISSING), got["rows"])

    print("Номер задачи рерайта читается, хотя сосед называет поле иначе")
    check("задача владельца есть в очереди под своим номером",
          "0a6c6716dbd1" in rows, True)
    check("сервис строки — рерайт", rows["0a6c6716dbd1"]["service"], "rewrite")
    check("пустых номеров в очереди нет",
          [r for r in got["rows"] if not r], [])

    print("\nНазвание: человек должен понимать, что это за задача")
    check("задача выпуска подписана полным названием ролика",
          rows["rw-release"]["title"], "Полное название ролика донора")
    check("у неё нет оговорки — название настоящее",
          rows["rw-release"]["title_why"], "")
    check("задача с экрана проекта подписана именем проекта",
          rows["0a6c6716dbd1"]["title"], "Проект от 20.09 18:40")
    check_in("и объясняет, почему имя именно такое",
             "название ролика не сохранено", rows["0a6c6716dbd1"]["title_why"])
    check("ничьей задаче остаётся номер, а не пустота",
          rows["rw-orphan"]["title"], "задача rw-orphan")
    check_in("и она честно говорит, что названия взять негде",
             "не из кабинета", rows["rw-orphan"]["title_why"])
    check("«без названия» в очереди не осталось нигде",
          [r["id"] for r in got["rows"].values() if not r["title"].strip()], [])

    print("\nКанал и проект у задачи соседа больше не теряются")
    check("канал выпуска доехал до строки рерайта",
          rows["rw-release"]["channel"], "Демо канал")
    check("язык тоже", rows["rw-release"]["lang"], "RU")
    check("фильтр «по каналу» теперь находит задачу соседа",
          "rw-release" in got["by_channel"], True)
    check("проект задачи виден — с него и начинают разбор",
          rows["0a6c6716dbd1"]["project_id"], "p_a66a80ca50")

    print("\nСтадия: последняя живая строка лога соседа, а не пустота")
    check("идущая задача показывает, чем занята",
          rows["0a6c6716dbd1"]["stage"], "ru: старт — 1 частей")
    check("склейка лога без перевода строки разобрана",
          got["stage_glued"], "ru: старт — 1 частей")
    check("и разобрана даже когда склеенная запись — последняя",
          got["stage_glued_tail"], "Задача принята")
    check("у обложек стадия берётся из подробностей соседа",
          rows["cov-1"]["stage"], "1/3 обложек")

    print("\nПричина отказа видна в очереди, а не в чужом сервисе")
    check_in("названа настоящая причина, а не только итог",
             "сеть недоступна (E-NET)", rows["5a6a1f2eb6ba"]["error"])
    check_in("итог соседа тоже сохранён",
             "1 из 1 задач не выполнено", rows["5a6a1f2eb6ba"]["error"])
    check("E-FAIL не вытесняет причину",
          got["reason"], "Ошибка: сеть недоступна (E-NET)")
    check("в чистом логе причины нет и выдумывать её нечем",
          got["reason_empty"], "")
    check("этап упавшей задачи говорит, ГДЕ она встала, а не повторяет причину",
          rows["5a6a1f2eb6ba"]["stage"], "Подготовка")

    print("\n«Висит» и «идёт давно» — разные ответы")
    check("у живой задачи записано, когда её взяли в работу",
          bool(rows["0a6c6716dbd1"]["started"]), True)
    check("ожидание очереди посчитано отдельно от работы",
          rows["0a6c6716dbd1"]["waiting"], 240)
    check_in("и сказано словами", "идёт ", rows["0a6c6716dbd1"]["age_text"])
    check_in("с упоминанием, сколько задача пролежала в очереди",
             "ждала очереди 4 мин", rows["0a6c6716dbd1"]["age_text"])
    check("не взятая в работу задача так и называется — в очереди",
          rows["rw-orphan"]["age_text"].startswith("в очереди"), True)
    check("у законченной задачи — сколько она заняла",
          rows["5a6a1f2eb6ba"]["age_text"].startswith("заняла"), True)
    check("отменённая до работы задача не притворяется очередью",
          rows["18"]["age_text"], "в работу не бралась")
    check("своя задача кабинета считается по тому же правилу",
          rows["41"]["age_text"].startswith("идёт"), True)
    check("длительности словами", got["span"],
          {"0": "0 с", "41": "41 с", "200": "3 мин", "3600": "60 мин",
           "7800": "2 ч 10 мин"})

    print("\nЧего сосед не сообщил, того не выдумываем")
    check("законченная обложка не объявлена невзятой в работу",
          rows["cov-done"]["age_text"], "сосед не сообщает, когда взял в работу")
    check("молчание не превратилось и в число: ожидания очереди у неё нет",
          rows["cov-done"]["waiting"], None)
    check("идущая обложка тоже не притворяется очередью",
          rows["cov-run"]["age_text"], "сосед не сообщает, когда взял в работу")
    check("а у дочитанной обложки времена уже настоящие",
          [rows["cov-1"]["times_known"],
           rows["cov-1"]["age_text"].startswith("идёт")], [True, True])
    check("нулевая отметка начала у рерайта по-прежнему значит «не бралась»",
          rows["rw-dropped"]["age_text"], "в работу не бралась")
    check("а ждущая обложка так и говорит: в очереди — это про неё правда",
          rows["cov-wait"]["age_text"], "в очереди 5 мин")

    print("\nЗаконченная задача соседа больше не притворяется очередью")
    check("completed — это готово", got["states"]["completed"], "done")
    check("completed_partial — готово частично",
          got["states"]["completed_partial"], "partial")
    check("interrupted — тоже частично, файлы у неё есть",
          got["states"]["interrupted"], "partial")
    check("resetting — работа идёт", got["states"]["resetting"], "running")
    check("pending — в очереди", got["states"]["pending"], "queued")
    check("строка законченной задачи выпуска",
          rows["rw-release"]["state"], "done")

    print("\nЭкран очереди не ходит к соседу за логом на каждую строку")
    check("строк в очереди много", len(got["rows"]) >= 25, True)
    check("подробностей спрошено не больше бюджета",
          got["details"]["asked"] <= got["details"]["budget"], True)
    check("и бюджет израсходован весь — он мал, а строк больше",
          got["details"]["asked"], got["details"]["budget"])
    check("запросов подробностей ровно столько же",
          len(got["asked_detail"]), got["details"]["asked"])
    check("нуждавшихся было больше бюджета",
          got["details"]["needed"] > got["details"]["budget"], True)
    check("свежие строки дочитаны, а не вытеснены старыми отказами",
          [rows["0a6c6716dbd1"]["reason_pending"],
           rows["5a6a1f2eb6ba"]["reason_pending"]], [False, False])
    check("кому не хватило бюджета — помечен, а не объявлен беспричинным",
          rows["old-19"]["reason_pending"], True)
    check("и причина у такой строки пуста — мы её не выдумали",
          rows["old-19"]["error"], "")

    print("\nБюджет дочитываний тратится на то, что человек увидит")
    check("на экране «Обложки» видны только обложки",
          got["cover_only_rows"], ["cov-1", "cov-done", "cov-run", "cov-wait"])
    check("дочитывали только обложку, которой не хватает стадии",
          got["cover_only_asked"], ["cov-1"])
    check("к рерайту на этом экране не ушло ни одного запроса",
          got["cover_only_from_rewrite"], [])
    check("и счёт дочитываний считает только видимые строки",
          [got["cover_only_details"]["needed"], got["cover_only_details"]["asked"]],
          [1, 1])

    print("\nПовторный показ не переспрашивает то же самое")
    check("второй показ подряд переспрашивает только то, что сорвалось",
          [u.rsplit("/", 1)[-1].split("?")[0] for u in got["asked_detail_second"]],
          ["rw-flaky"])
    check("сорвавшийся запрос не выдаёт себя за отсутствие причины",
          got["flaky_first"], {"error": "", "reason_pending": True})
    check("и на следующем показе причину спрашивают заново, а не берут из кэша",
          got["flaky_second"],
          {"error": "Ошибка: сеть недоступна (E-NET)", "reason_pending": False})
    check("идущая задача живёт в кэше недолго, законченная — долго",
          got["ttl"][0] < got["ttl"][1], True)
    check("как только срок вышел, стадию идущей задачи спрашивают заново",
          bool([u for u in got["asked_detail_third"] if "0a6c6716dbd1" in u]), True)
    check("а законченную — по-прежнему нет: её лог уже не изменится",
          [u for u in got["asked_detail_third"] if "5a6a1f2eb6ba" in u], [])

    print("\nСоседи остались на своих местах")
    check("оба сервиса ответили",
          [s["ok"] for s in got["services"] if s["key"] in ("rewrite", "cover")],
          [True, True])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  •", f)
        return 1
    print("Всё сошлось.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
