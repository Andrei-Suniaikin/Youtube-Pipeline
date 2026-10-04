#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Допуск ролика привязан к версии проекта и файла (29.09.2026).

Что проверяется и почему это опасно.

Задание заливки хранило только ПУТЬ к ролику. Путь у рендера постоянный: приём
результата кладёт mp4 в `render/<номер>.mp4` через `os.replace`, а перезапуск того же
рендера пишет туда же. Между допуском и отправкой проходят часы — задание лежит в
очереди, пока не освободится профиль AdsPower, — и всё это время пересборка проекта
молча подменяла принятый ролик. Нода получала то, что лежит по пути ПРЯМО СЕЙЧАС, и
на канал уходила версия, которую никто не принимал. Отменить это нельзя: ролик уже
в YouTube, а заливать повторно запрещено навсегда.

Поэтому в момент допуска записывается опознание артефакта — версия сборки (какой
рендер дал файл и какова его квитанция) плюс отпечаток файла (размер и края
содержимого), — и оно сверяется дважды: перед выдачей задания и перед тем, как байты
уйдут с сервера. Ловятся три разных пересборки, и одной проверки на все не хватает:

  1. файл подменили по тому же пути (перезапуск того же рендера, ручная замена) —
     видно только по отпечатку: квитанция выпуска при этом не меняется;
  2. проект пересобрали НОВЫМ рендером — видно только по версии сборки: старый файл
     лежит на месте целым, отпечаток сходится, но он больше не последняя версия;
  3. рендер запущен заново и ещё идёт — принятой версии сейчас нет вовсе.

Отдельно закреплены четыре вещи, которые легко сломать «улучшением»:

  * **убранная строка очереди — не пересборка.** Кнопка «Убрать завершённые задачи»
    (`server/main.py`, /api/queue/clear) удаляет строки завершённых рендеров. Пока
    отсутствие строки читалось как «рендер запущен заново», уборка списка убивала все
    стоящие в очереди заливки с ложной причиной. Отсутствие — «не знаем»; ручается
    отпечаток файла;
  * **отпечаток идёт ветвью хвоста.** У боевого ролика мегабайты, и края читаются
    двумя кусками. Проверки на файлах по 4 КБ эту ветку не исполняют вовсе: чтение
    забирает такой файл целиком. Поэтому здесь есть ролик БОЛЬШЕ окна края;

  * **время записи файла в сверке не участвует.** Восстановление из бэкапа и обычный
    `cp` переставляют mtime, не трогая байты. Ложная тревога здесь дороже: готовый
    выпуск встанет и будет ждать человека;
  * **задания без опознания выдаются как раньше.** Пустое значение означает «не
    знаем», а не «пересобран»: иначе выкатка остановила бы всё, что уже стоит в очереди.

И требование «повтор допуска не создаёт вторую работу»: проверка в коде и уникальность
`release_id` в базе обязаны работать обе, потому что между ними помещается второй тик.

Сеть не трогается: генератор названий подменён, AdsPower не спрашивается вовсе
(у профилей нет машины) и обязан не спрашиваться — его транспорт заряжен взрывом.

    .venv/bin/python tests/test_upload_artifact.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="upload-artifact-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
# Очередь кабинета: колонки те же, что в бою (`server/main.py`), включая квитанцию
# рендера — именно она и есть «версия сборки».
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS presets(id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT, name TEXT, settings TEXT, created REAL);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    project_id TEXT, kind TEXT, status TEXT, output_rel TEXT, render_sha256 TEXT,
    render_size INTEGER, created REAL, finished REAL);
""")
_c.execute("INSERT INTO users(name,role,tenant_id) VALUES('хозяин','admin','t1')")
_c.commit()
_c.close()

import pub_adspower                                               # noqa: E402
import pub_artifact                                               # noqa: E402
import pub_engine                                                 # noqa: E402
import pub_services as S                                          # noqa: E402
from pub_upload_api import register_upload_routes                 # noqa: E402

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

FAILS = []
TENANT = "t1"
TOKEN = "upl_artifact_token"
NOW = {"t": time.time()}
TITLE = "Название ролика для теста"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, cond):
    check(label, bool(cond), True)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def sql(q, args=()):
    c = conn()
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def run(q, args=()):
    c = conn()
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def job(rid):
    rows = sql("SELECT * FROM pub_upload_jobs WHERE release_id=?", (rid,))
    return rows[0] if rows else None


def journal(rid):
    return " | ".join(r["message"] for r in sql(
        "SELECT message FROM pub_release_log WHERE release_id=? ORDER BY id", (rid,)))


# ─────────────────────────── стенд ───────────────────────────

def boom(*_a, **_kw):
    raise AssertionError("ворота полезли в AdsPower, хотя у профилей нет машины")


pub_adspower.active_profiles = boom

# Генератор названий ходит в модель за деньги. Подменяем целиком: проверяем допуск,
# а не заголовок, и платить за это тест не должен.
S.make_title = lambda *a, **k: TITLE
S.make_title_codex = S.make_title   # 04.10.2026: названия по умолчанию — подписка Codex
S.chapters_enabled = lambda *a, **k: False

app = FastAPI()
register_upload_routes(app, {"db": conn, "_now": lambda: NOW["t"],
                             "upload_token": lambda: TOKEN})
client = TestClient(app, raise_server_exceptions=False)


def claim(worker="1", host="minsk"):
    r = client.post("/api/upload/claim", json={"host": host, "worker": worker},
                    headers={"X-Upload-Token": TOKEN})
    return r.json().get("task")


def detail(resp) -> str:
    """Объяснение отказа из ответа. Ответ не отказ — пустая строка, а не падение."""
    try:
        return str(resp.json().get("detail") or "")
    except Exception:
        return ""


def fetch(rid, token, kind="video"):
    return client.get(f"/api/upload/{rid}/file", params={"kind": kind},
                      headers={"X-Upload-Token": TOKEN, "X-Upload-Claim": token})


services = S.PubServices({
    "create_project": lambda *a, **k: "",
    "enqueue": lambda *a, **k: 0,
    "project_dir": lambda tenant, pid: os.path.join(TMP, "projects", pid),
    "openai_key": lambda: "ключ-не-настоящий",
})


def body_of(mark: str, size: int) -> bytes:
    """Тело ролика: узнаваемое начало, узнаваемый конец и длина по заказу.

    Края важны: отпечаток берёт по мегабайту с начала и с конца, и подмена, не
    тронувшая края, обязана быть поймана хотя бы по размеру.
    """
    head = f"[{mark}-начало]".encode("utf-8")
    tail = f"[{mark}-конец]".encode("utf-8")
    return head + b"." * max(size - len(head) - len(tail), 0) + tail


def write_video(pid, jid, body: bytes) -> str:
    """Положить mp4 туда же, куда его кладёт приём результата рендера."""
    folder = os.path.join(TMP, "projects", pid, "render")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{jid}.mp4")
    with open(path, "wb") as f:
        f.write(body)
    return path


def write_sparse_video(pid, jid, head: bytes, tail: bytes, size: int,
                       mid: bytes = b"") -> str:
    """Многомегабайтный ролик дёшево: края настоящие, середина — дыра в файле.

    Гонять мегабайты через память незачем, а размер важен: пока файл короче окна края,
    чтение забирает его целиком и боевая ветка хвоста не исполняется вовсе.
    """
    folder = os.path.join(TMP, "projects", pid, "render")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{jid}.mp4")
    with open(path, "wb") as f:
        f.write(head)
        f.truncate(size)
        if mid:
            f.seek((size - len(mid)) // 2)
            f.write(mid)
        f.seek(size - len(tail))
        f.write(tail)
    return path


def render_receipt(jid, pid, path, status="done"):
    """Квитанция рендера: то, что пишет приём результата, — путь, SHA и размер."""
    import hashlib
    body = open(path, "rb").read()
    run("INSERT OR REPLACE INTO queue_tasks(job_id,tenant_id,project_id,kind,status,"
        "output_rel,render_sha256,render_size,created,finished) "
        "VALUES(?,?,?,'render',?,?,?,?,?,?)",
        (jid, TENANT, pid, status, f"render/{jid}.mp4",
         hashlib.sha256(body).hexdigest(), len(body), NOW["t"], NOW["t"]))


def make_release(n: int, body: bytes):
    """Канал со своим профилем, строка плана, выпуск и готовый рендер под него."""
    rid, pid, jid = f"rel-{n}", f"p{n}", 500 + n
    uc = "UC" + str(n) * 22
    c = conn()
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,status,created) "
              "VALUES(?,?,?,'active',?)", (TENANT, str(n), f"Профиль {n}", NOW["t"]))
    prof = c.execute("SELECT id FROM pub_profiles WHERE serial=?", (str(n),)).fetchone()[0]
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,profile_id,tz,schedule,"
              "deferred,deferred_limit,created,updated) "
              "VALUES(?,?,?,'ru',?,'Europe/Warsaw','{}',1,0,?,?)",
              (TENANT, uc, f"Канал {n}", prof, NOW["t"], NOW["t"]))
    ch = c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_title,slot_at,status,created,updated) "
              "VALUES(?,?,?,1,'https://donor/x','Донорское название',?,'running',?,?)",
              (n, TENANT, ch, NOW["t"] + 3 * 86400, NOW["t"], NOW["t"]))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,created,updated) "
              "VALUES(?,?,?,1,?,?,?,?,'running','cover_ready',?,?)",
              (rid, TENANT, n, ch, uc, NOW["t"] + 3 * 86400, pid, NOW["t"], NOW["t"]))
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "created,updated) VALUES(?,'vsgen','render',?,'done',?,?)",
              (rid, str(jid), NOW["t"], NOW["t"]))
    c.commit()
    c.close()
    path = write_video(pid, jid, body)
    render_receipt(jid, pid, path)
    return {"rid": rid, "pid": pid, "jid": jid, "path": path, "uc": uc}


def make_big_release(n: int, mark: str, size: int, mid: bytes = b""):
    """Выпуск с роликом больше окна края: отпечаток обязан пойти ветвью хвоста."""
    rel = make_release(n, body_of(mark, 4096))
    path = write_sparse_video(rel["pid"], rel["jid"], f"[{mark}-начало]".encode("utf-8"),
                              f"[{mark}-конец]".encode("utf-8"), size, mid)
    render_receipt(rel["jid"], rel["pid"], path)       # квитанция под нынешний файл
    return rel


def clear_queue_row(jid):
    """То же, что кнопка «Убрать завершённые задачи»: строка завершённой задачи уходит.

    Условие один в один с ручкой /api/queue/clear (`server/main.py`): активные задачи
    она не трогает, файлы проектов не удаляет — убирает только запись из списка.
    """
    run("DELETE FROM queue_tasks WHERE job_id=? AND status NOT IN ('queued','running')",
        (jid,))


def rel_of(rid):
    """Выпуск в том же виде, в каком его получает обработчик фазы от движка."""
    c = conn()
    try:
        return [r for r in pub_engine.live_releases(c) if r["release_id"] == rid][0]
    finally:
        c.close()


def admit(rid):
    """Провести допуск — ровно тот же вызов, что делает движок на шаге заливки."""
    c = conn()
    try:
        out = services.do_upload_pending(c, rel_of(rid))
        c.commit()
        return out
    finally:
        c.close()


def revive(rid):
    """Вернуть выпуск из разбора — тем же вызовом, что и кнопка в кабинете."""
    c = conn()
    try:
        out = pub_engine.revive(c, rid, now=NOW["t"])
        c.commit()
        return out
    finally:
        c.close()


def park(rid):
    """Убрать чужое задание с дороги: очередь выдаёт по одному, профили независимы."""
    run("UPDATE pub_upload_jobs SET status='done', claim_token=NULL WHERE release_id=?",
        (rid,))


# ─────────────────────────── проверки ───────────────────────────

def main() -> int:
    print("Допуск запоминает, какую именно версию ролика приняли")
    a = make_release(1, body_of("один", 4096))
    admit(a["rid"])
    mark = pub_artifact.load(job(a["rid"])["video_passport"])
    check("запомнен номер рендера", mark.get("build", {}).get("job"), str(a["jid"]))
    check("запомнен размер файла", mark.get("file", {}).get("size"),
          os.path.getsize(a["path"]))
    check("запомнены края содержимого",
          len(mark.get("file", {}).get("edges") or ""), 64)
    check("полный хэш взят из квитанции рендера, а не посчитан заново",
          job(a["rid"])["video_sha256"],
          sql("SELECT render_sha256 s FROM queue_tasks WHERE job_id=?",
              (a["jid"],))[0]["s"])
    check_true("в журнале сказано, что допущено", "допущена версия" in journal(a["rid"]))

    print("Повтор допуска не создаёт вторую работу")
    admit(a["rid"])
    check("задание по-прежнему одно", len(sql(
        "SELECT id FROM pub_upload_jobs WHERE release_id=?", (a["rid"],))), 1)
    # Гонка: проверка «задание уже есть» не сработала (два тика разошлись между
    # SELECT и INSERT). Единственное, что тогда держит выпуск, — уникальность в базе,
    # и падать на ней нельзя: движок ловит любое исключение и уводит выпуск в разбор.
    was = services._upload_job
    services._upload_job = lambda *a, **k: None
    blew_up = ""
    try:
        admit(a["rid"])
    except Exception as exc:                                       # noqa: BLE001
        blew_up = f"{type(exc).__name__}: {exc}"
    finally:
        services._upload_job = was
    check("гонка не уронила обработчик", blew_up, "")
    check("и при разошедшейся гонке задание тоже одно", len(sql(
        "SELECT id FROM pub_upload_jobs WHERE release_id=?", (a["rid"],))), 1)

    print("Неизменённый ролик уходит, и ровно один раз")
    task = claim()
    check("задание выдано", (task or {}).get("release_id"), a["rid"])
    again = claim()
    check("повторный опрос вернул то же задание", (again or {}).get("release_id"), a["rid"])
    check_true("и отметил его как повтор", (again or {}).get("repeat"))
    check("второго задания не завелось", len(sql(
        "SELECT id FROM pub_upload_jobs WHERE release_id=?", (a["rid"],))), 1)
    got = fetch(a["rid"], task["claim_token"])
    check("файл отдан", got.status_code, 200)
    check("и это тот самый файл", got.content, open(a["path"], "rb").read())

    print("Переставленное время записи файла — не повод отказывать")
    os.utime(a["path"], (NOW["t"] + 7200, NOW["t"] + 7200))
    got = fetch(a["rid"], task["claim_token"])
    check("файл всё так же отдан", got.status_code, 200)
    check("задание не снято", job(a["rid"])["status"], "running")
    park(a["rid"])

    print("Пересобранный после приёмки файл не уходит")
    b = make_release(2, body_of("два", 4096))
    admit(b["rid"])
    write_video(b["pid"], b["jid"], body_of("два-заново", 9000))
    check("задание не выдано", claim(), None)
    row = job(b["rid"])
    check("оно снято с заливки", row["status"], "error")
    check_true("причина про пересборку", "пересобран после приёмки" in (row["error"] or ""))
    check_true("сказано про новую приёмку", "нужна новая приёмка" in (row["error"] or ""))
    check_true("сказано, что деньги не потрачены",
               "деньги не потрачены" in (row["error"] or ""))
    check_true("названы оба размера — и прежний, и нынешний",
               f"{4096} Б" in (row["error"] or "")
               and f"{9000} Б" in (row["error"] or ""))
    check_true("и всё это попало в журнал выпуска",
               "пересобран после приёмки" in journal(b["rid"]))
    check("клейм не выдавался", row["claim_token"], None)

    print("Подмена той же длины — размер сходится, а ролик другой")
    k = make_release(8, body_of("восемь", 6000))
    admit(k["rid"])
    swap = body_of("восемь-подменённый", 6000)
    check("подмена ровно той же длины", len(swap), os.path.getsize(k["path"]))
    write_video(k["pid"], k["jid"], swap)
    check("задание не выдано", claim(), None)
    check_true("причина говорит, что дело в содержимом",
               "содержимое другое" in (job(k["rid"])["error"] or ""))

    print("Проект пересобран новым рендером — старый файл цел, а приёмка недействительна")
    d = make_release(3, body_of("три", 4096))
    admit(d["rid"])
    fresh_jid = d["jid"] + 1000
    fresh = write_video(d["pid"], fresh_jid, body_of("три-вторая", 7000))
    render_receipt(fresh_jid, d["pid"], fresh)
    run("UPDATE pub_release_jobs SET remote_job_id=? WHERE release_id=? AND kind='render'",
        (str(fresh_jid), d["rid"]))
    check("задание не выдано", claim(), None)
    row = job(d["rid"])
    check("оно снято с заливки", row["status"], "error")
    why = row["error"] or ""
    check_true("причина называет оба рендера",
               f"№{d['jid']}" in why and f"№{fresh_jid}" in why)
    check_true("и говорит про пересборку проекта",
               "проект пересобран после приёмки" in (row["error"] or ""))

    print("Рендер запущен заново по тому же номеру — принятой версии сейчас нет")
    e = make_release(4, body_of("четыре", 4096))
    admit(e["rid"])
    run("UPDATE queue_tasks SET status='running', render_sha256=NULL, render_size=NULL "
        "WHERE job_id=?", (e["jid"],))
    check("задание не выдано", claim(), None)
    why = job(e["rid"])["error"] or ""
    check_true("причина называет рендер", f"№{e['jid']}" in why)
    check_true("и говорит, что принятой версии нет", "принятой версии ролика" in why)
    # Причина обязана называть то состояние, которое ВИДНО в очереди, а не догадку:
    # «запущен заново» про рендер, упавший с ошибкой, — неправда.
    check_true("и называет нынешнее состояние рендера", "идёт" in why)

    print("Рендер упал с ошибкой — так и сказано, без догадок про перезапуск")
    n = make_release(11, body_of("одиннадцать", 4096))
    admit(n["rid"])
    run("UPDATE queue_tasks SET status='error' WHERE job_id=?", (n["jid"],))
    check("задание не выдано", claim(), None)
    why = job(n["rid"])["error"] or ""
    check_true("сказано про ошибку рендера", "ошибка" in why)
    check_true("и не выдумано, что рендер идёт заново", "запущен заново" not in why)

    print("Файл переписан ПОСЛЕ выдачи задания — байты не уходят")
    f = make_release(5, body_of("пять", 4096))
    admit(f["rid"])
    task = claim()
    check("задание выдано", (task or {}).get("release_id"), f["rid"])
    write_video(f["pid"], f["jid"], body_of("пять-заново", 9000))
    got = fetch(f["rid"], task["claim_token"])
    check("сервер отказался отдавать ролик", got.status_code, 409)
    check_true("и объяснил человеку, почему",
               "пересобран после приёмки" in detail(got))
    check("задание снято", job(f["rid"])["status"], "error")
    check_true("клейм сохранён — поздний вердикт ноды обязан доехать",
               bool(job(f["rid"])["claim_token"]))

    print("Задание без опознания выдаётся как раньше")
    g = make_release(6, body_of("шесть", 4096))
    admit(g["rid"])
    run("UPDATE pub_upload_jobs SET video_passport=NULL WHERE release_id=?", (g["rid"],))
    write_video(g["pid"], g["jid"], body_of("шесть-другой", 9000))
    check("задание выдано", (claim() or {}).get("release_id"), g["rid"])
    park(g["rid"])

    print("Возврат из разбора отменяет прежнюю приёмку и допускает заново")
    run("UPDATE pub_releases SET status='blocked', phase='upload_pending', "
        "blocked_reason='файл пересобран' WHERE release_id=?", (b["rid"],))
    out = revive(b["rid"])
    check("выпуск вернулся в работу", out["ok"], True)
    check("и вернулся на шаг до заливки", out["phase"], "cover_ready")
    check("прежнее задание убрано", job(b["rid"]), None)
    admit(b["rid"])
    mark = pub_artifact.load(job(b["rid"])["video_passport"])
    check("новая приёмка опознала нынешний файл", mark.get("file", {}).get("size"),
          os.path.getsize(b["path"]))
    check("и задание снова одно", len(sql(
        "SELECT id FROM pub_upload_jobs WHERE release_id=?", (b["rid"],))), 1)
    check("теперь ролик уходит", (claim() or {}).get("release_id"), b["rid"])

    print("Живую заливку возврат из разбора не трогает")
    h = make_release(7, body_of("семь", 4096))
    admit(h["rid"])
    run("UPDATE pub_upload_jobs SET status='running', claim_token='upc-живой' "
        "WHERE release_id=?", (h["rid"],))
    run("UPDATE pub_releases SET status='blocked', phase='upload_pending' "
        "WHERE release_id=?", (h["rid"],))
    revive(h["rid"])
    check_true("задание живой ноды на месте", job(h["rid"]) is not None)
    check("и фаза не откатилась", sql(
        "SELECT phase FROM pub_releases WHERE release_id=?", (h["rid"],))[0]["phase"],
        "upload_pending")

    park(b["rid"])
    park(h["rid"])

    print("Молчащая нода держит клейм — задание не сносим даже из разбора")
    m = make_release(9, body_of("девять", 5000))
    admit(m["rid"])
    task = claim()
    check("задание выдано", (task or {}).get("release_id"), m["rid"])
    # Так задание снимает `sweep_stuck`: статус «ошибка», а клейм СОХРАНЁН — нода
    # могла молча довести мастер до конца, и её поздний вердикт обязан доехать.
    run("UPDATE pub_upload_jobs SET status='error', error='нода молчит дольше аренды' "
        "WHERE release_id=?", (m["rid"],))
    run("UPDATE pub_releases SET status='blocked', phase='upload_pending' "
        "WHERE release_id=?", (m["rid"],))
    revive(m["rid"])
    check_true("задание на месте — по нему ещё ждут вердикт", job(m["rid"]) is not None)
    check_true("клейм цел", bool((job(m["rid"]) or {}).get("claim_token")))
    check("и фаза не откатилась", sql(
        "SELECT phase FROM pub_releases WHERE release_id=?", (m["rid"],))[0]["phase"],
        "upload_pending")

    print("Уборка списка завершённых задач не отменяет приёмку")
    p1 = make_release(12, body_of("двенадцать", 4096))
    admit(p1["rid"])
    before = open(p1["path"], "rb").read()
    clear_queue_row(p1["jid"])
    check("строка рендера действительно убрана", sql(
        "SELECT job_id FROM queue_tasks WHERE job_id=?", (p1["jid"],)), [])
    task = claim()
    check("задание выдано — ролик на месте и тот же",
          (task or {}).get("release_id"), p1["rid"])
    got = fetch(p1["rid"], (task or {}).get("claim_token"))
    check("и байты отданы", got.status_code, 200)
    check("именно те самые", got.content, before)
    check("задание не снято с заливки", job(p1["rid"])["status"], "running")
    check_true("и в журнале нет выдумки про пересборку",
               "пересобран" not in journal(p1["rid"]))
    park(p1["rid"])

    print("Строку убрали, а файл подменили — отказ, но причина именно про файл")
    p2 = make_release(13, body_of("тринадцать", 4096))
    admit(p2["rid"])
    clear_queue_row(p2["jid"])
    write_video(p2["pid"], p2["jid"], body_of("тринадцать-заново", 9000))
    check("задание не выдано", claim(), None)
    why = job(p2["rid"])["error"] or ""
    check_true("причина про файл", "файл пересобран после приёмки" in why)
    check_true("и ни слова про рендер, о котором мы ничего не знаем",
               "рендер" not in why)

    print("Опознание в одиночку: пропавшая строка рендера — это «не знаем»")
    same_file = {"size": 100, "edges": "края", "mtime": 1}
    kept = {"build": {"job": "7", "row": True, "status": "done", "out": "render/7.mp4",
                      "sha256": "abc", "size": 100}, "file": same_file}
    gone = {"build": {"job": "7", "row": False, "status": "", "out": "", "sha256": "",
                      "size": None}, "file": dict(same_file)}
    check("строки нет, а отпечаток сошёлся — молчим",
          pub_artifact.mismatch(kept, gone), "")
    # Опознания, записанные до 29.09.2026, ключа `row` не содержат вовсе, и читать их
    # надо так же: иначе выкатка снесла бы всё, что уже стоит в очереди.
    old_style = {"build": {k: v for k, v in kept["build"].items() if k != "row"},
                 "file": same_file}
    check("опознание старого образца читается так же",
          pub_artifact.mismatch(old_style, gone), "")
    # …и сверять их по-прежнему надо целиком: строка рендера на месте, но квитанция
    # другая — это пересборка, и старый образец опознания не повод её пропустить.
    redone = {"build": {"job": "7", "row": True, "status": "done",
                        "out": "render/7.mp4", "sha256": "другой", "size": 100},
              "file": dict(same_file)}
    check_true("у старого образца квитанция всё так же сверяется",
               "квитанция другая" in pub_artifact.mismatch(old_style, redone))
    blind = {"build": dict(kept["build"]), "file": {}}
    why = pub_artifact.mismatch(blind, gone)
    check_true("а без отпечатка сказано честно: подтвердить нечем",
               "не удалось подтвердить версию" in why)
    check_true("и это не объявлено пересборкой", "пересобран" not in why)

    print("Ролик больше окна края: хвост читается, подмена в конце видна")
    BIG = 3 << 20
    check_true("проверяем именно ветку хвоста", BIG > pub_artifact.EDGE_BYTES)
    q1 = make_big_release(14, "четырнадцать", BIG)
    check("ролик нужной длины", os.path.getsize(q1["path"]), BIG)
    admit(q1["rid"])
    check("запомнен размер многомегабайтного ролика",
          pub_artifact.load(job(q1["rid"])["video_passport"])["file"]["size"], BIG)
    # Подмена в самом конце: длина та же, начало то же. Заметить это можно только
    # чтением хвоста. Квитанцию рендера не трогаем — молчаливая подмена файла её и
    # не меняет, ровно на этом и держится смысл отпечатка.
    write_sparse_video(q1["pid"], q1["jid"], "[четырнадцать-начало]".encode("utf-8"),
                       "[четырнадцать-КОНЕЦ!]".encode("utf-8"), BIG)
    check("длина не изменилась", os.path.getsize(q1["path"]), BIG)
    check("задание не выдано", claim(), None)
    check_true("причина про подменённое содержимое",
               "содержимое другое" in (job(q1["rid"])["error"] or ""))

    print("Подмена внутри окна хвоста, но не в самых последних байтах — тоже видна")
    q2 = make_big_release(15, "пятнадцать", BIG)
    admit(q2["rid"])
    with open(q2["path"], "r+b") as f:
        f.seek(BIG - pub_artifact.EDGE_BYTES + 4096)   # в хвосте, далеко от конца
        f.write("подменённый кадр".encode("utf-8"))
    check("длина не изменилась", os.path.getsize(q2["path"]), BIG)
    check("задание не выдано", claim(), None)
    check_true("причина про подменённое содержимое",
               "содержимое другое" in (job(q2["rid"])["error"] or ""))

    print("И начало у большого ролика читается по-прежнему")
    q3 = make_big_release(16, "шестнадцать", BIG)
    admit(q3["rid"])
    with open(q3["path"], "r+b") as f:
        f.seek(0)
        f.write("ДРУГОЕ НАЧАЛО".encode("utf-8"))
    check("длина не изменилась", os.path.getsize(q3["path"]), BIG)
    check("задание не выдано", claim(), None)
    check_true("причина про подменённое содержимое",
               "содержимое другое" in (job(q3["rid"])["error"] or ""))

    print("Середину большого ролика отпечаток не читает — осознанный размен")
    # Это не недосмотр: сверка идёт на каждой выдаче задания и на каждой отдаче байтов,
    # и чтение всего ролика упиралось бы в диск на минуты. Пересборку, которая
    # переписывает файл целиком, ловят и края, и версия сборки.
    q4 = make_big_release(17, "семнадцать", BIG)
    admit(q4["rid"])
    with open(q4["path"], "r+b") as f:
        f.seek(BIG // 2)                               # ровно между окнами края
        f.write("правка в середине".encode("utf-8"))
    check("длина не изменилась", os.path.getsize(q4["path"]), BIG)
    check("задание выдано: края те же", (claim() or {}).get("release_id"), q4["rid"])
    park(q4["rid"])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
