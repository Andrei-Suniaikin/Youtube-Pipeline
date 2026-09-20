#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Нет сценария — не платим»: кабинет не ставит платные этапы пустому проекту.

Дефект, ради которого это писалось (20.09.2026). Владелец нажал «Сделать всё» на
проекте `p_bc85c4bc85`, у которого в папке НЕ БЫЛО `source.txt` вообще. В очередь
встали `prompts` и `tts`: модель начала платно раскадровывать пустоту, озвучка —
озвучивать пустоту, а сценария и обложки владелец так и не дождался, потому что их
кабинет не делает вовсе. Проверки «есть ли текст» не было ни в батче
(`/api/batch/{kind}`), ни у одиночных кнопок (`/api/projects/{pid}/{kind}`).

Проверяется здесь именно то, что стоит денег:

  * пустой проект не получает НИ ОДНОЙ постановки — ни «Сделать всё», ни озвучкой,
    ни картинками, ни поодиночке;
  * отказ назван словами и доезжает до человека (400 с причиной, либо счётчик
    `blocked` + `block_reason` в ответе пачки) — молчаливого «кнопка не сработала»
    быть не должно;
  * пустому проекту не ставятся и ОТЛОЖЕННЫЕ авто-запуски: иначе вотчер сам поднял
    бы видео и рендер позже, и запрет обошёлся бы сам собой;
  * беда одного проекта не уносит пачку: соседи с текстом стартуют как обычно;
  * законные пути БЕЗ текста не закрыты: картинки по своим промптам и Bing по тегам
    сценарий не читают и ставиться обязаны;
  * БОЕВОЙ КОНВЕЙЕР НЕ ОСТАНОВЛЕН: выпуск, созданный движком уже с готовым текстом
    (`_create_project(..., source_text=...)`, как это делает `pub_services.
    do_project_ready`), проходит правило насквозь; проходит и проект, которому текст
    вписали позже штатной ручкой `/api/projects/{pid}/source`;
  * ответ «Сделать всё» сам говорит, чего он НЕ делает: сценарий и обложка — соседние
    сервисы (поля `stages` и `note`);
  * правило стоит на ВСЕХ человеческих входах к платной постановке, а не на двух из
    четырёх: кроме батча и одиночных кнопок это «Повторить» в очереди
    (`POST /api/jobs/{jid}/retry`) и «Повторить» в админ-панели (`_adm_act_retry_job`,
    ставит задачу в очередь владельца и тратит его деньги). Строка очереди хранит
    параметры момента падения, а сценарий с тех пор могли стереть;
  * новый проект НЕ наследует чужие отложенные авто-запуски: `_create_project`
    копировал settings последнего проекта тенанта целиком, вместе с
    `deferred_video_pending`/`deferred_pending`, и вотчер (`_deferred_render_tick`
    ищет работу по `settings LIKE '%deferred_%'`) сам ставил свежему проекту платное
    видео и рендер, которых человек не заказывал.

Сети и стенда здесь нет. База — своя копия `dev/vsgen.dev.db` во временном каталоге,
папки проектов — свои, после себя тест всё убирает: стенд владельца на 8080 и его
проекты не трогаются. Подменены ровно два соседа правила: постановка в очередь
(`_enqueue` — чтобы видеть, ставили или нет, и не создавать настоящих задач) и
проверка ключей провайдера (`_provider_block_reason` — иначе исход теста решали бы
ключи в базе, а не текст проекта).

    .venv/bin/python tests/test_no_source_guard.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
SEED = os.path.join(BASE, "dev", "vsgen.dev.db")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Headers(dict):
    """Заголовки без учёта регистра — как у настоящего Starlette.

    Ручки кабинета спрашивают признак fetch-запроса в разном написании
    (`x-requested-with` в батче, `X-Requested-With` в повторе задачи). Простой dict
    молча отвечал бы «не fetch» половине из них, и тест проверял бы не ту ветку.
    """

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class Req:
    """Запрос ровно в том объёме, в каком его читают ручки кабинета."""

    def __init__(self, body: str = "", fetch: bool = True):
        self._body = body.encode("utf-8")
        self.headers = Headers({"x-requested-with": "fetch"} if fetch else {})
        self.state = types.SimpleNamespace()
        self.cookies = {}

    async def body(self):
        return self._body


def form(pids, **values) -> str:
    """Тело формы батча: отмеченные проекты плюс непустые поля панели."""
    import urllib.parse
    fields = [("pid", p) for p in pids]
    fields += [(k, str(v)) for k, v in values.items()]
    return urllib.parse.urlencode(fields)


def main() -> int:
    if not os.path.exists(SEED):
        print(f"НЕ ПРОВЕРЕНО: нет базы-образца {SEED}")
        return 1
    work = tempfile.mkdtemp(prefix="no-source-guard-")
    try:
        return run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run(work: str) -> int:
    db_path = os.path.join(work, "vsgen.db")
    shutil.copy(SEED, db_path)
    tenants = os.path.join(work, "tenants")
    os.environ["VS_SERVER_DB"] = db_path
    os.environ["VS_TENANTS_DIR"] = tenants
    os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
    # Кабинет тянет движки по зашитому пути ~/VideoStudio/src — тот же изолированный
    # HOME, что и у dev/run.sh (см. tests/test_project_stage_status.py).
    os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
    sys.path.insert(0, os.path.join(BASE, "server"))
    sys.path.insert(0, os.path.join(BASE, "workers"))
    import main as M                                               # noqa: E402

    from fastapi import HTTPException                              # noqa: E402

    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    user = c.execute("SELECT * FROM users WHERE id=1").fetchone()
    c.close()
    tid = M._tid(user)

    # ── подмена соседей правила ──────────────────────────────────────────────
    put = []                       # (project_id, kind) каждой ПОСТАНОВКИ

    def fake_enqueue(uid, tenant_id, project_id, kind, params, **kw):
        put.append((project_id, kind))
        return f"job-{len(put)}"

    M._enqueue = fake_enqueue
    M._provider_block_reason = lambda u, kind, params, project_id=None: ""
    M.require_web_user = lambda request: user
    M.web_user = lambda request: user

    def mkproject(name, text="", settings=None):
        pid = M._create_project(tid, name, source_text=text, settings=settings or {})
        os.makedirs(M._project_dir(tid, pid), exist_ok=True)
        return pid

    def settings_of(pid):
        return M._psettings(M._get_project(tid, pid) or {})

    def batch(kind, pids, **values):
        put.clear()
        return asyncio.run(M.batch_run(kind, Req(form(pids, **values))))

    def single(pid, kind, **values):
        put.clear()
        return asyncio.run(M.gen_submit(pid, kind, Req(form([], **values))))

    # ── 1. чистые функции правила ────────────────────────────────────────────
    print("\n1. Что считается «текст есть»")
    empty = mkproject("Пустой")                      # ровно случай p_bc85c4bc85
    blank = mkproject("Из пробелов", text="   \n\t\n  ")
    lead = mkproject("С шапкой пробелов", text="\n\n   \n\nнастоящий сценарий")
    good = mkproject("Со сценарием", text="Первая строка сценария. Вторая строка.")
    check("папки/файла нет — текста нет", M._has_source_text(tid, empty), False)
    check("файл из пробелов — текста нет", M._has_source_text(tid, blank), False)
    check("шапка из пробелов текст не прячет", M._has_source_text(tid, lead), True)
    check("обычный сценарий — текст есть", M._has_source_text(tid, good), True)
    check("чужой проект — текста нет", M._has_source_text("нет-такого", good), False)

    print("\n2. Какие этапы читают сценарий")
    check("озвучка читает", M._stage_reads_source("tts", {}), True)
    check("промпты читают", M._stage_reads_source("prompts", {}), True)
    check("картинки по плану сцен читают", M._stage_reads_source("images", {}), True)
    check("картинки по своим промптам — нет",
          M._stage_reads_source("images", {"custom_prompts": "кадр один\nкадр два"}), False)
    check("Bing по тегам — нет",
          M._stage_reads_source("images", {"img_source": "bing", "tags": "война, танк"}), False)
    check("Bing без тегов идёт по плану сцен — читает",
          M._stage_reads_source("images", {"img_source": "bing"}), True)
    for kind in ("video", "render", "chapters", "cards", "regen"):
        check(f"«{kind}» сценарий не читает", M._stage_reads_source(kind, {}), False)

    # ── 3. «Сделать всё» по пустому проекту ──────────────────────────────────
    print("\n3. «Сделать всё» на проекте без сценария")
    try:
        batch("pipeline", [empty])
        check("пустой проект: отказ", "поставилось молча", "HTTPException 400")
    except HTTPException as exc:
        check("пустой проект: код отказа", exc.status_code, 400)
        check("пустой проект: причина названа словами",
              "нет сценария" in str(exc.detail), True)
        check("пустой проект: сказано, что делать дальше",
              "«Текст»" in str(exc.detail), True)
    check("пустой проект: ни одной платной постановки", put, [])
    st = settings_of(empty)
    check("пустой проект: отложенных авто-запусков тоже нет",
          [k for k in M._DEFERRED_KEYS if k in st], [])

    # ── 4. пачка: сосед с текстом уезжает, пустой пропущен ───────────────────
    print("\n4. Пачка: беда одного проекта не уносит остальных")
    out = batch("pipeline", [empty, good])
    body = out.body.decode("utf-8")
    import json
    ans = json.loads(body)
    check("пачка: поставлено два этапа соседа", sorted(put), [(good, "images"), (good, "tts")])
    check("пачка: счётчик поставленных", ans["enqueued"], 2)
    check("пачка: пустой проект посчитан заблокированным", ans["blocked"], 1)
    check("пачка: причина уехала в ответ", "нет сценария" in ans["block_reason"], True)
    check("пачка: у пустого проекта по-прежнему нет отложенных",
          [k for k in M._DEFERRED_KEYS if k in settings_of(empty)], [])
    check("пачка: соседу отложены видео и рендер",
          sorted(k for k in ("deferred_video_pending", "deferred_pending")
                 if k in settings_of(good)), ["deferred_pending", "deferred_video_pending"])

    print("\n5. Ответ «Сделать всё» говорит, чего он НЕ делает")
    check("названы этапы кабинета", ans.get("stages"), ["tts", "images", "video", "render"])
    check("сказано про сценарий и обложку",
          all(w in (ans.get("note") or "") for w in ("Сценарий", "обложка")), True)
    check("и куда за ними идти",
          all(w in (ans.get("note") or "") for w in ("«Текст»", "«Обложка»")), True)

    # ── 6. одиночные кнопки: та же дыра ──────────────────────────────────────
    print("\n6. Одиночные кнопки в обход «Сделать всё»")
    before = settings_of(empty)
    for kind in ("tts", "images"):
        try:
            single(empty, kind)
            check(f"одиночный «{kind}»: отказ", "поставилось молча", "HTTPException 400")
        except HTTPException as exc:
            check(f"одиночный «{kind}»: код отказа", exc.status_code, 400)
            check(f"одиночный «{kind}»: причина названа", "нет сценария" in str(exc.detail), True)
        check(f"одиночный «{kind}»: ни одной постановки", put, [])
    # Отказ происходит ДО `_save_project_settings`: поля формы отклонённой задачи не
    # должны оседать в проекте, иначе следующий запуск пойдёт на чужих настройках.
    check("одиночный отказ ничего не записал в проект", settings_of(empty), before)

    single(good, "tts", engine="edge")
    check("проект со сценарием озвучивается как обычно", put, [(good, "tts")])

    # ── 7. законные пути без сценария закрывать нельзя ───────────────────────
    print("\n7. Картинки, которым сценарий не нужен")
    def batch_quiet(kind, pids):
        """Пачка, чей отказ — это результат проверки, а не падение теста."""
        try:
            batch(kind, pids)
        except HTTPException as exc:
            return [("ОТКАЗ", str(exc.detail)[:60])]
        return put

    cp = mkproject("Свои промпты", settings={"custom_prompts": "кадр один\nкадр два"})
    check("свои промпты ставятся и без сценария",
          batch_quiet("images", [cp]), [(cp, "images")])
    bing = mkproject("Bing по тегам", settings={"img_source": "bing", "tags": "война, танк"})
    check("Bing по тегам ставится и без сценария",
          batch_quiet("images", [bing]), [(bing, "images")])

    # ── 8. боевой конвейер: текст приходит штатным путём ─────────────────────
    print("\n8. Боевой конвейер не остановлен")
    # Так выпуск заводит движок: проект создаётся УЖЕ с готовым текстом рерайта
    # (pub_services.do_project_ready → create_project(source_text=...)), а следом
    # тем же `_enqueue` ставятся озвучка и картинки.
    rel = mkproject("Выпуск движка", text="Текст, полученный рерайтом от донора.")
    batch("pipeline", [rel])
    check("выпуск движка проходит правило насквозь",
          sorted(put), [(rel, "images"), (rel, "tts")])
    # А так текст приходит позже: проект завели пустым, сценарий вписали штатной
    # ручкой. Правило обязано пускать его сразу после записи, без рестартов.
    late = mkproject("Текст позже")
    try:
        batch("pipeline", [late])
        check("до текста — отказ", "поставилось", "HTTPException 400")
    except HTTPException:
        check("до текста — отказ", True, True)
    asyncio.run(M.project_source(late, Req(form([], source="Сценарий, вписанный позже."))))
    batch("pipeline", [late])
    check("после записи сценария — ставится",
          sorted(put), [(late, "images"), (late, "tts")])

    # ── 9. «Повторить» в очереди — третий человеческий вход ──────────────────
    print("\n9. «Повторить» в очереди (POST /api/jobs/{jid}/retry)")

    def mktask(project_id, kind, params=None, status="error"):
        """Строка очереди, какой её оставляет упавшая задача: параметры момента падения."""
        con = sqlite3.connect(db_path)
        cur = con.execute(
            "INSERT INTO queue_tasks(tenant_id,project_id,kind,status,created,uid,params) "
            "VALUES(?,?,?,?,?,?,?)",
            (tid, project_id, kind, status, time.time(), user["id"],
             json.dumps(params or {}, ensure_ascii=False)))
        con.commit()
        jid = cur.lastrowid
        con.close()
        return jid

    def retry(jid):
        put.clear()
        return asyncio.run(M.job_retry(jid, Req()))

    # Так выглядит настоящая беда: озвучка упала, сценарий после этого стёрли (или
    # проект пересоздали пустым), а строка очереди о смерти текста ничего не знает.
    for kind in ("tts", "prompts", "images"):
        jid = mktask(empty, kind)
        try:
            retry(jid)
            check(f"повтор «{kind}» на пустом: отказ", "поставилось молча", "HTTPException 400")
        except HTTPException as exc:
            check(f"повтор «{kind}» на пустом: код отказа", exc.status_code, 400)
            check(f"повтор «{kind}» на пустом: причина названа",
                  "нет сценария" in str(exc.detail), True)
        check(f"повтор «{kind}» на пустом: ни одной постановки", put, [])

    jid = mktask(good, "tts")
    retry(jid)
    check("повтор на проекте со сценарием работает как раньше", put, [(good, "tts")])

    # Законные пути повтора БЕЗ текста закрывать нельзя. Параметры берутся из СТРОКИ
    # очереди, а не из настроек проекта: свои промпты в упавшей задаче — это свои
    # промпты и в повторе.
    jid = mktask(empty, "images", {"custom_prompts": "кадр один\nкадр два"})
    retry(jid)
    check("повтор картинок по своим промптам не заблокирован", put, [(empty, "images")])
    jid = mktask(empty, "regen", {"image_rel": "googler/0001.jpg"})
    retry(jid)
    check("повтор перегенерации кадра не заблокирован", put, [(empty, "regen")])
    # Рендер сценарий не читает (`_render_inputs_ready` — его собственная проверка),
    # и правило обязано его пропускать. Свой проект — чтобы отложенный флаг, который
    # повтор рендера ставит штатно, не пачкал соседние проверки.
    empty_render = mkproject("Пустой, повтор рендера")
    jid = mktask(empty_render, "render")
    out = retry(jid)
    check("повтор рендера правилом не блокируется", out.get("deferred"), True)

    # ── 10. «Повторить» в админ-панели — четвёртый вход ──────────────────────
    print("\n10. «Повторить» в админ-панели (_adm_act_retry_job)")
    # Повтор от админа ставит задачу в очередь ВЛАДЕЛЬЦА и тратит его деньги, а сам
    # админ видит строку очереди, но не видит, что у проекта стёрли сценарий.
    jid = mktask(empty, "tts")
    put.clear()
    try:
        M._adm_act_retry_job(user, {"job_id": jid})
        check("админ-повтор на пустом: отказ", "поставилось молча", "HTTPException 400")
    except HTTPException as exc:
        check("админ-повтор на пустом: код отказа", exc.status_code, 400)
        check("админ-повтор на пустом: причина названа",
              "нет сценария" in str(exc.detail), True)
    check("админ-повтор на пустом: ни одной постановки", put, [])

    jid = mktask(good, "tts")
    put.clear()
    M._adm_act_retry_job(user, {"job_id": jid})
    check("админ-повтор на проекте со сценарием работает как раньше", put, [(good, "tts")])

    # ── 11. Новый проект не наследует чужие авто-запуски ─────────────────────
    print("\n11. Наследование настроек не тащит отложенные авто-запуски")
    # Донор — живой проект, которому ЧЕСТНО обещали авто-видео и авто-рендер. Он
    # создаётся последним, потому что наследование берёт именно последний по created
    # проект тенанта с настройками.
    donor = mkproject("Донор настроек", text="Текст донора.",
                      settings={"sentences_per_prompt": "7"})
    M._save_project_settings(tid, donor, {
        "deferred_pending": user["id"],
        "deferred_video_pending": user["id"],
        "deferred_video_params": {"video_method": "veo"},
    })
    check("у донора отложки на месте",
          sorted(k for k in M._DEFERRED_KEYS if k in settings_of(donor)),
          ["deferred_pending", "deferred_video_params", "deferred_video_pending"])

    heir = M._create_project(tid, "Наследник")          # settings=None → наследование
    st_heir = settings_of(heir)
    check("обычные настройки унаследованы", st_heir.get("sentences_per_prompt"), "7")
    check("отложенных авто-запусков не унаследовано",
          [k for k in M._DEFERRED_KEYS if k in st_heir], [])
    # Прямая проверка того, чем ловит вотчер: он ищет работу запросом
    # `settings LIKE '%deferred_%'`, поэтому подстроки в JSON быть не должно вовсе.
    con = sqlite3.connect(db_path)
    raw = con.execute("SELECT settings FROM projects WHERE id=?", (heir,)).fetchone()[0]
    con.close()
    check("вотчер такой проект не найдёт", "deferred_" in (raw or ""), False)

    # Тот же запрет на явном пути (пресет/движок): отложка — состояние проекта, а не
    # настройка, и приехать снаружи она не может ни по какой дороге.
    explicit = mkproject("С явными настройками",
                         settings={"sentences_per_prompt": "4",
                                   "deferred_pending": user["id"]})
    st_exp = settings_of(explicit)
    check("явные настройки: обычное поле дошло", st_exp.get("sentences_per_prompt"), "4")
    check("явные настройки: отложка отброшена",
          [k for k in M._DEFERRED_KEYS if k in st_exp], [])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print(f"  · {f}")
        return 1
    print("ВСЁ ЗЕЛЁНОЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
