#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран проекта — API пульта (`web/app.py`), батч C, Ф2.

Интеграционный: требует поднятый стенд (см. `task-spec.md` §12 — свой порт и своя
копия базы для Ф2) и пароль кабинета из `VS_DEV_PASSWORD`. Устроен тем же способом,
что и `tests/test_web_pult.py`: свой минимальный HTTP-клиент, `check()`/`FAILS`,
прямые чтения базы там, где через API это не проверить (сырые `projects.settings`,
`job_logs`, чужая задача Cover).

    VS_PULT_URL=http://127.0.0.1:8084/studio VS_PLATFORM_BASE=http://127.0.0.1:8084 \\
    VS_DEV_DB=/tmp/t-c2.db VS_SERVER_DB=/tmp/t-c2.db \\
    VS_TENANTS_DIR="$PWD/dev/data/tenants" VS_DEV_PASSWORD=proba-123 \\
    HOME="$PWD/dev/home" .venv/bin/python tests/test_project_screen_api.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8084/studio")
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")
TENANT = os.environ.get("VS_DEV_TENANT", "dev")

# Cover — в объединённой платформе живёт в том же процессе, база и вывод — по
# умолчанию модуля (см. `cover/cover_server.py:DB_PATH/OUTPUT_DIR`), если явно не
# переопределены окружением. Общий файл на все параллельные стенды батча C — поэтому
# идентификаторы фабрикуемых задач тестовые и заведомо не пересекутся, а за собой
# тест убирает и строку, и файлы.
COVER_DB = os.environ.get("COVER_DB_PATH", os.path.join(BASE, "cover", "cover_job_queue.db"))
COVER_OUTPUT_DIR = os.environ.get("COVER_OUTPUT_DIR", os.path.join(BASE, "cover", "outputs"))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Client:
    """Тот же минимальный «браузер», что и в `tests/test_web_pult.py`: помнит куки,
    отдаёт код и разобранное тело."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, payload=None, method=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(PULT + urllib.parse.quote(path, safe="/?&="),
                                     data=data,
                                     method=method or ("POST" if data is not None else "GET"))
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except ValueError:
                return exc.code, raw[:200]
        except urllib.error.URLError as exc:
            raise SystemExit(f"пульт {PULT} не отвечает: {exc.reason}. Сначала подними стенд Ф2.")


def db():
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def project_row(pid: str):
    c = db()
    try:
        return c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    finally:
        c.close()


def project_settings(pid: str) -> dict:
    row = project_row(pid)
    if not row or not row["settings"]:
        return {}
    try:
        return json.loads(row["settings"])
    except ValueError:
        return {}


def drop_project(pid: str) -> None:
    row = project_row(pid)
    if row and row["dir_path"]:
        shutil.rmtree(row["dir_path"], ignore_errors=True)
    c = db()
    try:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
        # Лог и парная gen_jobs — вместе с очередью: осиротевший лог прилипает к
        # следующей задаче, получившей тот же номер.
        c.execute("DELETE FROM job_logs WHERE job_id IN "
                  "(SELECT job_id FROM queue_tasks WHERE project_id=?)", (pid,))
        c.execute("DELETE FROM gen_jobs WHERE rowid IN "
                  "(SELECT job_id FROM queue_tasks WHERE project_id=?)", (pid,))
        c.execute("DELETE FROM queue_tasks WHERE project_id=?", (pid,))
        c.commit()
    finally:
        c.close()


def insert_queue_task(job_id: int, pid: str, kind: str, status: str, tenant=TENANT) -> None:
    c = db()
    try:
        # claimed = сейчас: иначе включённый планировщик сочтёт 'running' зависшей и
        # запишет в job_logs под этим номером (так смешались логи #14/#15 на стенде).
        now = time.time()
        c.execute("INSERT INTO queue_tasks(job_id, tenant_id, project_id, kind, status, created, "
                  "claimed) VALUES(?,?,?,?,?,?,?)", (job_id, tenant, pid, kind, status, now, now))
        c.commit()
    finally:
        c.close()


def drop_queue_task(job_id: int) -> None:
    c = db()
    try:
        c.execute("DELETE FROM queue_tasks WHERE job_id=?", (job_id,))
        c.execute("DELETE FROM job_logs WHERE job_id=?", (job_id,))
        c.commit()
    finally:
        c.close()


def insert_job_logs(job_id: int, n: int) -> None:
    c = db()
    try:
        c.executemany("INSERT INTO job_logs(job_id, ts, line) VALUES(?,?,?)",
                      [(job_id, time.time(), f"строка {i}") for i in range(n)])
        c.commit()
    finally:
        c.close()


def presets_count() -> int:
    c = db()
    try:
        return c.execute("SELECT COUNT(*) FROM presets WHERE tenant_id=?", (TENANT,)).fetchone()[0]
    finally:
        c.close()


def drop_preset_by_name(name: str) -> None:
    c = db()
    try:
        c.execute("DELETE FROM presets WHERE tenant_id=? AND name=?", (TENANT, name))
        c.commit()
    finally:
        c.close()


# ─────────────────────────── фабрика задач Cover (только для теста) ───────────────────────────

def cover_insert_job(job_id: str, user_id: str, filename: str, content: bytes) -> str:
    """Кладёт минимальную строку `jobs` Cover напрямую в его SQLite — без похода
    через очередь/воркер: тесту нужна только готовая, «уже сделанная» задача с одним
    файлом. Возвращает путь к output_dir, чтобы тест мог убрать его за собой."""
    out_dir = os.path.join(COVER_OUTPUT_DIR, f"_test_f2_{job_id}")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, filename), "wb") as f:
        f.write(content)
    data = {
        "id": job_id, "status": "completed", "created_at": time.time(),
        "started_at": time.time(), "finished_at": time.time(),
        "urls": "[]", "preset_data": "{}", "mode": "url", "titles_per_lang": "{}",
        "extra_prompt": "", "seeds_per_lang": "{}", "n_variants": 1,
        "output_dir": out_dir, "languages": "[]", "refs_per_language": "{}",
        "max_threads": 1, "parallel": False, "user_id": user_id,
        "generation_provider": "flow", "cancel_requested": False, "run_token": "",
        "progress_percent": 100, "progress_detail": "", "log": "", "error": "",
        "results_count": 1, "output_files": json.dumps([filename]), "fallback_files": "[]",
    }
    c = sqlite3.connect(COVER_DB, timeout=5)
    try:
        c.execute("INSERT INTO jobs(id, data, status, created_at, user_id, provider, "
                 "cancel_requested, run_token) VALUES(?,?,?,?,?,?,0,'')",
                 (job_id, json.dumps(data, ensure_ascii=False), "completed",
                  time.time(), user_id, "flow"))
        c.commit()
    finally:
        c.close()
    return out_dir


def cover_drop_job(job_id: str, out_dir: str) -> None:
    shutil.rmtree(out_dir, ignore_errors=True)
    c = sqlite3.connect(COVER_DB, timeout=5)
    try:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        c.commit()
    finally:
        c.close()


def main() -> int:
    me = Client()
    code, body = me.go("/ui/login", {"login": LOGIN, "password": PASSWORD})
    if code != 200:
        raise SystemExit(f"вход не удался ({code}): {body}. Задайте VS_DEV_PASSWORD.")

    print("Проект для проверок экрана")
    code, body = me.go("/ui/projects", {"name": "тест Ф2: экран проекта",
                                        "text": "Раз. Два. Три."})
    check("проект заведён", code, 200)
    pid = str((body or {}).get("id") or "")
    if not pid:
        raise SystemExit("проект не завёлся — дальше проверять нечего")

    try:
        print("\nGET /ui/projects/{pid} — расширенный контракт (§5.9)")
        code, body = me.go(f"/ui/projects/{pid}")
        check("проект открылся", code, 200)
        proj = (body or {}).get("project") or {}
        stage = proj.get("stage") or {}
        segs = stage.get("segments") or []
        if len(segs) == 5:
            check("порядок сегментов",
                  [s["stage"] for s in segs], ["text", "tts", "images", "video", "render"])
            check("текст уже есть — text done", segs[0]["state"], "done")
        else:
            print("  NOTE Ф1 ещё не выкатил project_stage_status — segments пуст, "
                  "прогнать этот блок заново после DONE Ф1")
        check("applied_preset пуст на свежем проекте", proj.get("applied_preset"), None)
        check("source — то, что дали при заведении", body.get("source"), "Раз. Два. Три.")

        print("\nСтарт этапа: пять исходов (§5.4)")
        code, body = me.go(f"/ui/projects/{pid}/stage/no-such-project", {})
        check("неизвестный kind — 404", code, 404)

        code, body = me.go(f"/ui/projects/{pid}/stage/tts", {"settings": {"engine": "edge"}})
        check("старт озвучки (edge, бесплатно) — 200", code, 200)
        check("исход — started", (body or {}).get("outcome"), "started")
        check("job_id пришёл", isinstance((body or {}).get("job_id"), int), True)
        tts_jid = (body or {}).get("job_id")

        code, body = me.go(f"/ui/projects/{pid}/stage/tts", {})
        check("повторный старт сразу — duplicate", (body or {}).get("outcome"), "duplicate")
        if isinstance(tts_jid, int):
            # Освобождаем kind='tts' для следующей проверки — иначе она снова
            # упрётся в «уже идёт» раньше, чем дойдёт до сохранения настроек.
            drop_queue_task(tts_jid)

        code, body = me.go(f"/ui/projects/{pid}/stage/render", {})
        check("рендер без готовых материалов — render_deferred",
              (body or {}).get("outcome"), "render_deferred")

        busy_jid = 9010001
        insert_queue_task(busy_jid, pid, "regen", "running")
        try:
            code, body = me.go(f"/ui/projects/{pid}/stage/images", {})
            check("правка картинок занята — images_busy", (body or {}).get("outcome"), "images_busy")
        finally:
            drop_queue_task(busy_jid)

        print("\nПустая форма старта не меняет движок озвучки проекта (поправка оркестратора к §5.4)")
        me.go(f"/ui/projects/{pid}/settings", {"values": {"engine": "voicegencore"}})
        check("движок сохранился", project_settings(pid).get("engine"), "voicegencore")
        code, body = me.go(f"/ui/projects/{pid}/stage/tts", {})   # тело пустое — как «Начать» без правок формы
        # Issue 2 ревью (цикл 1): раньше код ответа не проверялся, и совпадение было
        # неявным — на стенде нет серверного ключа VoiceGenCore, поэтому кабинет
        # отказывает ДО _save_project_settings (main.py ≈12849–12871) 400-кой «серверного
        # ключа нет», и до перезаписи engine=edge дело просто не доходит. Разбираем
        # оба возможных исхода явно, чтобы из кода теста было видно, какой из них
        # доказывает поправку (1), а не полагаться на не проверяемый код ответа:
        #  - 400: значит в кабинет ушёл именно voicegencore (иначе не было бы этой
        #    конкретной ошибки о ключе VoiceGenCore) — форма старта не заменила его
        #    на edge, что и требовалось доказать;
        #  - 200/started: ключ на стенде нашёлся — тогда доказательство прямое:
        #    engine в настройках проекта не изменился.
        if code == 400:
            check("400 — именно нет ключа VoiceGenCore (значит форма ушла с engine=voicegencore, не edge)",
                  "VoiceGenCore" in str((body or {}).get("detail", "")), True)
        else:
            check("старт прошёл (200/started) — ключ VoiceGenCore на стенде нашёлся", code, 200)
            check("движок НЕ стал edge", project_settings(pid).get("engine"), "voicegencore")
            new_tts_jid = (body or {}).get("job_id")
            if isinstance(new_tts_jid, int):
                drop_queue_task(new_tts_jid)   # чтобы не мешал следующим прогонам этого теста
        check("движок в настройках проекта не тронут в любом случае", project_settings(pid).get("engine"), "voicegencore")

        print("\n400 кабинета пробрасывается как есть, не своим текстом (§7.8)")
        # Свободная (без ключей) проверка: `engine` — легальное поле группы «Озвучка»,
        # но кабинет отвергает неизвестное значение ДО постановки в очередь и ДО
        # обращения к провайдеру (main.py ≈12871) — 400 с текстом кабинета, ничего
        # не встаёт в очередь и подменять сообщение своим нельзя (принцип 3 §5).
        code, body = me.go(f"/ui/projects/{pid}/stage/tts",
                           {"settings": {"engine": "no-such-project-движка"}})
        check("неизвестный движок озвучки — 400", code, 400)
        check("текст ошибки — от кабинета (не выдуман пультом)",
              "неизвестный TTS-движок" in str((body or {}).get("detail", "")), True)

        print("\nСохранение текста (§5.5)")
        code, body = me.go(f"/ui/projects/{pid}/source", {"text": "Новый текст сценария."})
        check("текст сохранён", code, 200)
        srcfile = os.path.join(project_row(pid)["dir_path"], "source.txt")
        check("source.txt содержит новый текст",
              open(srcfile, encoding="utf-8").read(), "Новый текст сценария.")

        print("\nПресеты: снимок и применение (§5.6, §7.1)")
        before = presets_count()
        code, body = me.go(f"/ui/projects/{pid}/preset/snapshot", {"name": "   "})
        check("пустое имя снимка — 400 (пульт, до кабинета)", code, 400)
        check("пресет не создан", presets_count(), before)

        snap_name = "тест Ф2: снимок пресета"
        # Issue 1 ревью (цикл 1): настройки проекта нужно запомнить ДО вызова снимка —
        # кабинетная preset_save (main.py:12176–12230) пишет effective и в пресет, и
        # обратно в projects.settings ОДНИМ и тем же вызовом, поэтому чтение настроек
        # ПОСЛЕ снимка доказывает лишь то, что кабинет скопировал сам у себя, а не то,
        # что пульт передал в кабинет именно текущие настройки проекта (без setting__*
        # тест бы прошёл даже при отправке пультом произвольного мусора). Настройки
        # проекта на этот момент нетривиальны: выше уже сохранён engine=voicegencore
        # и висит deferred_pending с честного render_deferred — есть что испортить.
        pre_snapshot_settings = project_settings(pid)
        code, body = me.go(f"/ui/projects/{pid}/preset/snapshot", {"name": snap_name})
        check("снимок создан", code, 200)
        if code == 200:
            check("ровно один новый пресет", presets_count(), before + 1)
            crow = db()
            try:
                prow = crow.execute("SELECT id, settings FROM presets WHERE tenant_id=? AND name=?",
                                    (TENANT, snap_name)).fetchone()
            finally:
                crow.close()
            snap_settings = json.loads(prow["settings"]) if prow and prow["settings"] else {}
            # §5.6/поправка 3 оркестратора: без setting__* кабинет снимает пресет с
            # ТЕКУЩИХ эффективных настроек проекта как есть — снимок обязан совпасть
            # с настройками проекта, зафиксированными ДО вызова снимка (кроме
            # служебных ключей). Плюс `_DEFERRED_KEYS` (main.py ≈3306) — кабинет
            # фильтрует их только из снимка, а не из самих настроек проекта;
            # `deferred_pending` у нас в pre_snapshot_settings настоящий, он остался
            # с более раннего в этом же тесте честного `render_deferred`
            # (main.py ≈12846), а не утечка нашего кода.
            skip = {"_applied_preset", "deferred_pending", "deferred_video_pending",
                   "deferred_video_params", "deferred_render_batch", "deferred_render_params",
                   "deferred_wait_video", "deferred_images_pending", "deferred_images_params",
                   "deferred_images_replan", "deferred_images_prompt_provider"}
            check("снимок = настройки проекта ДО вызова снимка (не переписанные им же)",
                  {k: v for k, v in pre_snapshot_settings.items() if k not in skip and not k.startswith("_")},
                  {k: v for k, v in snap_settings.items() if k not in skip and not k.startswith("_")})
            # Контрольная проверка обратного направления: после снимка кабинет
            # переписывает projects.settings тем же effective — значит настройки
            # проекта ПОСЛЕ снимка тоже обязаны совпасть с пресетом (это не отменяет
            # проверку выше, а показывает, что до и после снимка ничего не разъехалось
            # по другой причине).
            post_snapshot_settings = project_settings(pid)
            check("настройки проекта после снимка не разъехались с pre_snapshot",
                  {k: v for k, v in post_snapshot_settings.items() if k not in skip and not k.startswith("_")},
                  {k: v for k, v in pre_snapshot_settings.items() if k not in skip and not k.startswith("_")})
            drop_preset_by_name(snap_name)

        code, body = me.go(f"/ui/projects/{pid}/preset/apply", {"preset_id": 999999999})
        check("применение несуществующего пресета — 404", code, 404)
        code, body = me.go(f"/ui/projects/no-such-project-проекта/preset/apply", {"preset_id": 1})
        check("применение на несуществующий проект — 404", code, 404)

        print("\nПрименённый пресет: ссылки и «удалён» (§5.9, functional req. 2, §7.2, §7.7)")
        ap_partial_name = "тест Ф2: частичный images (applied_preset)"
        code, body = me.go("/ui/presets/vsgen", {"name": ap_partial_name, "scope": "images"})
        check("частичный пресет заведён", code, 200)
        ap_partial_id = (body or {}).get("id")

        ap_full_name = "тест Ф2: целый со ссылкой (applied_preset)"
        code, body = me.go("/ui/presets/vsgen", {"name": ap_full_name})
        check("целый пресет заведён", code, 200)
        ap_full_id = (body or {}).get("id")

        if ap_partial_id and ap_full_id:
            me.go(f"/ui/presets/vsgen/{ap_partial_id}",
                  {"name": ap_partial_name, "values": {"style": "applied-preset-link-style"}})
            check("ссылка целого на частичный (images) поставлена",
                  me.go(f"/ui/presets/vsgen/{ap_full_id}/links",
                        {"part": "images", "preset_id": ap_partial_id})[0], 200)

            code, body = me.go(f"/ui/projects/{pid}/preset/apply", {"preset_id": ap_full_id})
            check("применение целого пресета со ссылкой — 200", code, 200)

            code, body = me.go(f"/ui/projects/{pid}")
            ap = (body.get("project") or {}).get("applied_preset") or {}
            check("applied_preset.id — id применённого целого", ap.get("id"), ap_full_id)
            check("applied_preset.name — имя применённого пресета", ap.get("name"), ap_full_name)
            check("applied_preset.missing — False, пресет на месте", ap.get("missing"), False)
            check("applied_preset.links — только реально разрешившаяся часть images "
                  "(§7.2 — честно, без сломанных ссылок)",
                  ap.get("links"), [{"part": "images", "title": "Картинки"}])
            check("значение поля из ЧАСТИЧНОГО дошло до настроек проекта, не своё поле целого",
                  project_settings(pid).get("style"), "applied-preset-link-style")

            # §7.7: пресет пропал (например, кем-то удалён) — applied_preset обязан
            # честно сказать «удалён» и сохранить id, а не тихо снять выбор или
            # выдумать имя.
            drop_preset_by_name(ap_full_name)
            code, body = me.go(f"/ui/projects/{pid}")
            ap = (body.get("project") or {}).get("applied_preset") or {}
            check("id применённого пресета сохранён и после удаления строки",
                  ap.get("id"), ap_full_id)
            check("имя — null, не выдумано (§7.7)", ap.get("name"), None)
            check("missing — True после удаления пресета", ap.get("missing"), True)

            drop_preset_by_name(ap_partial_name)

        print("\nЛог задачи (§5.8)")
        log_jid = 9010002
        insert_queue_task(log_jid, pid, "tts", "done")
        insert_job_logs(log_jid, 1500)
        try:
            code, body = me.go(f"/ui/jobs/{log_jid}/logs?after=0")
            check("лог отдан", code, 200)
            check("total = реальному числу строк", (body or {}).get("total"), 1500)
            check("длинный лог — truncated", (body or {}).get("truncated"), True)
            check("хвост — 1000 строк", len(body.get("lines") or []), 1000)
            last = (body or {}).get("next_after")
            code, body2 = me.go(f"/ui/jobs/{log_jid}/logs?after={last}")
            check("после последней строки — пусто", (body2 or {}).get("lines"), [])
        finally:
            drop_queue_task(log_jid)

        code, body = me.go("/ui/jobs/999999999/logs?after=0")
        check("чужой/несуществующий job_id — 404, не 403", code, 404)

        print("\n«Поставить обложкой» (§5.7, §6, поправка 2 оркестратора)")
        own_jid, own_dir = "f2own" + str(int(time.time())), ""
        foreign_jid = "f2foreign" + str(int(time.time()))
        own_dir = cover_insert_job(own_jid, TENANT, "variant.jpg", b"\xff\xd8own-cover-bytes")
        foreign_dir = cover_insert_job(foreign_jid, "другой-тенант-" + own_jid,
                                       "variant.jpg", b"\xff\xd8foreign-cover-bytes")
        try:
            code, body = me.go(f"/ui/projects/{pid}/cover",
                               {"service_job_id": foreign_jid, "name": "variant.jpg"})
            check("чужая задача Cover отвергнута", code in (403, 404), True)
            cover_path = os.path.join(project_row(pid)["dir_path"], "cover.jpg")
            check("cover.jpg не появился от чужой задачи", os.path.exists(cover_path), False)

            code, body = me.go(f"/ui/projects/{pid}/cover",
                               {"service_job_id": own_jid, "name": "variant.jpg"})
            check("своя задача Cover принята", code, 200)
            check("байты совпали", (body or {}).get("bytes"), len(b"\xff\xd8own-cover-bytes"))
            check("cover.jpg содержит именно этот файл",
                  open(cover_path, "rb").read(), b"\xff\xd8own-cover-bytes")

            code, body = me.go(f"/ui/projects/{pid}")
            covers_group = [g for g in (body.get("files") or []) if g["code"] == "covers"]
            check("категория covers видна в файлах проекта", len(covers_group), 1)
            if covers_group:
                check("вид — image (миниатюра, не ссылка)", covers_group[0]["kind"], "image")

            # Повторная попытка чужой задачей не должна перезаписать уже стоящую обложку.
            code, body = me.go(f"/ui/projects/{pid}/cover",
                               {"service_job_id": foreign_jid, "name": "variant.jpg"})
            check("чужая задача всё ещё отвергнута", code in (403, 404), True)
            check("cover.jpg не изменился",
                  open(cover_path, "rb").read(), b"\xff\xd8own-cover-bytes")
        finally:
            cover_drop_job(own_jid, own_dir)
            cover_drop_job(foreign_jid, foreign_dir)

        print("\nЗадачи текста и обложек проекта — по связи project_service_jobs (17.09.2026)")
        stamp = str(int(time.time()))
        older, newer, stray = "psjold" + stamp, "psjnew" + stamp, "psjstray" + stamp
        dirs = [cover_insert_job(j, TENANT, "v.jpg", b"\xff\xd8x") for j in (older, newer, stray)]
        c = db()
        try:
            now = time.time()
            c.executemany("INSERT INTO project_service_jobs(tenant_id, project_id, service, "
                          "job_id, created) VALUES (?,?,?,?,?)",
                          [(TENANT, pid, "cover", older, now - 10),
                           (TENANT, pid, "cover", newer, now)])
            c.commit()
        finally:
            c.close()
        try:
            code, body = me.go(f"/ui/covers?project_id={pid}")
            check("список обложек проекта отдаётся", code, 200)
            check("только связанные задачи, новые первыми",
                  [j["id"] for j in (body or {}).get("jobs") or []], [newer, older])
            code, body = me.go("/ui/covers")
            check("без project_id — все задачи аккаунта, как раньше",
                  stray in [j["id"] for j in (body or {}).get("jobs") or []], True)
            code, body = me.go(f"/ui/text?project_id={pid}")
            check("у проекта задач текста нет — пустой список, не ошибка",
                  (code, (body or {}).get("jobs")), (200, []))
            # Старая задача проекта выпала из списка сервиса (Cover отдаёт пульту 50
            # последних): пульт дочитывает её по одной, а не теряет.
            filler = [f"psjfill{i}x{stamp}" for i in range(52)]
            filler_dirs = [cover_insert_job(j, TENANT, "v.jpg", b"\xff\xd8x") for j in filler]
            c = sqlite3.connect(COVER_DB, timeout=5)
            try:
                c.execute("UPDATE jobs SET created_at=? WHERE id=?", (1.0, older))
                c.commit()
            finally:
                c.close()
            try:
                code, body = me.go("/ui/covers")
                check("старая задача действительно не попадает в список аккаунта",
                      older in [j["id"] for j in (body or {}).get("jobs") or []], False)
                code, body = me.go(f"/ui/covers?project_id={pid}")
                check("а в списке проекта она есть — дочитана по одной",
                      [j["id"] for j in (body or {}).get("jobs") or []], [newer, older])
            finally:
                for j, d in zip(filler, filler_dirs):
                    cover_drop_job(j, d)

            code, _b = me.go("/ui/covers?project_id=no-such-project")
            check("чужой или несуществующий проект — 404", code, 404)

            before = sqlite3.connect(COVER_DB).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            code, _b = me.go("/ui/covers", {"mode": "seed", "languages": ["Русский"],
                                            "seed": "x", "project_id": "no-such-project"})
            check("постановка обложки в несуществующий проект — 404 до создания", code, 404)
            after = sqlite3.connect(COVER_DB).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            check("задача Cover не создана", after, before)
            code, _b = me.go("/ui/text", {"mode": "text", "source": "раз", "languages": ["ru"],
                                          "project_id": "no-such-project"})
            check("постановка текста в несуществующий проект — 404 до создания", code, 404)
        finally:
            for j, d in zip((older, newer, stray), dirs):
                cover_drop_job(j, d)
    finally:
        drop_project(pid)
        check("проект убран за собой", project_row(pid) is None, True)
        c = db()
        try:
            left = c.execute("SELECT COUNT(*) FROM project_service_jobs WHERE project_id=?",
                             (pid,)).fetchone()[0]
        finally:
            c.close()
        check("связи задач ушли вместе с проектом (триггер)", left, 0)

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
