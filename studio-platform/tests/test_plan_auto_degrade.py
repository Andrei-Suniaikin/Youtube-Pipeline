#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""План сцен не справился с ОДНОЙ галочкой — её выключают один раз, а не теряют этап.

Воркер отклоняет план двух пород. Первая — канон разбора персонажей и мест: одно имя у
персонажа и у места («DuPont» как фамилия и как компания), каноническое место, не
отмеченное в сцене. Вторая — смысловая нарезка длинных предложений не сошлась после всех
повторов блока. Обе порождены настройками проекта: у плана того же текста без
`analyze_story` / `split_long_sentences` таких проверок нет ВООБЩЕ.

До этой правки обе породы приезжали в кабинет безликим кодом 1, и вотчер отложенных
картинок делал единственное, что умел: снимал этап целиком. Пользователь видел «конвейер
оборван» и пересоздавал проект руками, заново оплачивая промпты, — в бою 12.09 так легло
79 планов.

Теперь воркер отвечает 11 (канон) и 12 (нарезка), а кабинет ОДИН раз выключает виноватую
галочку, пишет это в настройки проекта и в лог человеческими словами и переставляет
промпты; картинки продолжают ждать. Второй откат той же галочки запрещён: каждый переплан
— оплаченный вызов модели, и подбор за чужой счёт хуже честного падения.

Четыре проверки здесь стерегут не поведение, а деньги и доверие к квитанции:
  * отметка «попытка израсходована» лежит в БД уже в момент постановки, а не в конце тика
    — обрыв между ними иначе оплачивал бы второй переплан;
  * откат не перевыпускает набор референсов, иначе квитанция image_receipts.v1.json
    начинает описывать другой набор фото, чем заказывали;
  * заблокированный аккаунт не тратит единственную попытку: задачи нет — отката нет;
  * объяснение лежит в логе УПАВШЕЙ задачи, красную карточку которой открывает человек.

Запуск процессов здесь подменён: воркер вызывается функциями, сеть — заглушкой, кабинет
работает на временной копии схемы.

    .venv/bin/python tests/test_plan_auto_degrade.py
"""
from __future__ import annotations

import io as _io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

# Своя БД и свой каталог тенантов: тест пишет проекты и задачи, а рядом работают
# чужие стенды на dev/vsgen.dev.db — трогать её нельзя.
ROOT = tempfile.mkdtemp(prefix="plan-degrade-")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(ROOT, "vsgen.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(ROOT, "tenants")
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))

sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

from PIL import Image                                             # noqa: E402
import reference_contract                                         # noqa: E402
import googler_worker as W                                        # noqa: E402
import main as M                                                  # noqa: E402

FAILS = []
TID, PID, UID = "t-degrade", "p_degrade", 4242
SOURCE = "Первое предложение теста. Второе предложение теста подлиннее, чтобы было что делить.\n"
SETTINGS = {
    "style": "minimalistic, cinematic, photorealistic",
    "model": "OPENAI",
    "sentences_per_prompt": "3",
    "analyze_story": "1",
    "split_long_sentences": "1",
}


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ── половина воркера: код выхода отличает «галочка не справилась» от общего провала ──

def worker_exit_code(exc_class):
    """Код выхода googler_worker.main(), когда команда падает данной ошибкой плана."""
    saved_cmd, saved_argv = W.cmd_generate_prompts, sys.argv

    def raiser(_args):
        raise exc_class("тестовый отказ плана")

    W.cmd_generate_prompts = raiser
    sys.argv = ["googler_worker", "prompts", "--source_file", os.path.join(ROOT, "s.txt"),
                "--project_dir", ROOT, "--style", "test"]
    try:
        W.main()
        return 0
    except SystemExit as exc:
        return exc.code
    finally:
        W.cmd_generate_prompts, sys.argv = saved_cmd, saved_argv


def canon_error_from_real_check():
    """Настоящая проверка канона: в тексте есть «DuPont», а в scene.entities его нет."""
    bible = {"characters": [{"id": "c1", "names": ["DuPont"], "role": "main"}], "locations": []}
    scene = {"text_segment": "DuPont came home.", "visual_prompt": "a man",
             "visual_description": "a man at home", "entities": []}
    try:
        W._validate_explicit_alias_entity_coverage([scene], bible)
    except Exception as exc:
        return type(exc)
    return None


def semantic_error_from_real_check():
    """Настоящая смысловая нарезка: модель отвечает пустым блоком, повторы не помогают."""
    saved = (W.SCENE_SCHEMA_MAX_RETRIES, W.SCENE_SCHEMA_RETRY_DELAY,
             W._prompt_provider, W._scene_llm_request)
    W.SCENE_SCHEMA_MAX_RETRIES, W.SCENE_SCHEMA_RETRY_DELAY = 0, 0
    W._prompt_provider = lambda: "openai"
    W._scene_llm_request = lambda payload, timeout, label: {
        "choices": [{"message": {"content": '{"units": []}'}}]}
    try:
        W._plan_semantic_beats(SOURCE, 6.0, "1", False)
        return None
    except Exception as exc:
        return type(exc)
    finally:
        (W.SCENE_SCHEMA_MAX_RETRIES, W.SCENE_SCHEMA_RETRY_DELAY,
         W._prompt_provider, W._scene_llm_request) = saved


# ── половина кабинета: один откат вместо снятия этапа ──

def ref_photo(color):
    """Настоящий кадр: контракт референсов декодирует байты Pillow и хеширует результат."""
    buf = _io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buf, format="PNG")
    checked = reference_contract.canonicalize_reference_image(buf.getvalue())
    return {"sha256": checked.sha256, "bytes": checked.jpeg_bytes,
            "width": checked.width, "height": checked.height}


def commit_refs(projdir, heroes):
    """heroes = {имя героя: [цвет кадра, ...]} → сохранённое состояние папки референсов."""
    state = {"characters": [{"group_id": M._reference_group_id(name), "name": name,
                             "sources": [ref_photo(color) for color in colors]}
                            for name, colors in heroes.items()],
             "style": []}
    M._reference_commit_state(projdir, state)


def seed(settings, error, reason, heroes=None):
    """Проект, у которого последняя задача промптов упала с данным кодом воркера."""
    projdir = os.path.join(ROOT, "proj")
    shutil.rmtree(projdir, ignore_errors=True)
    os.makedirs(os.path.join(projdir, "googler"), exist_ok=True)
    with open(os.path.join(projdir, "source.txt"), "w", encoding="utf-8") as fh:
        fh.write(SOURCE)
    M._project_dir = lambda _tid, _pid: projdir

    c = M.db()
    c.execute("DELETE FROM users WHERE id=?", (UID,))
    c.execute("INSERT INTO users(id,name,token,role,created,tenant_id) VALUES(?,?,?,?,?,?)",
              (UID, "degrade", f"tok-{UID}", "user", "2026-09-19T00:00:00", TID))
    for table in ("queue_tasks", "gen_jobs", "job_logs", "projects"):
        c.execute(f"DELETE FROM {table}")
    deferred = {k: v for k, v in settings.items() if k != M._PLAN_DEGRADE_SETTING}
    if heroes:
        commit_refs(projdir, heroes)
        # Заявка ЗАКРЕПЛЯЕТ набор референсов: кабинет чеканит bundle один раз и дальше
        # носит его id в отложенных параметрах. Квитанция картинок ссылается на него.
        deferred[M._REFERENCE_BUNDLE_PARAM] = M._snapshot_reference_bundle(projdir, deferred)
    stored = dict(settings)
    stored.update({"deferred_images_pending": UID, "deferred_images_params": deferred})
    c.execute("INSERT INTO projects(id,tenant_id,name,dir_path,status,created,updated,settings)"
              " VALUES(?,?,?,?,?,?,?,?)",
              (PID, TID, "Degrade", projdir, "new", 1.0, 1.0,
               json.dumps(stored, ensure_ascii=False)))
    c.commit()
    c.close()

    failed = M._enqueue(UID, TID, PID, "prompts", deferred)
    c = M.db()
    c.execute("UPDATE queue_tasks SET status='error', error=? WHERE job_id=?", (error, failed))
    c.execute("UPDATE gen_jobs SET status='error' WHERE id=?", (failed,))
    c.commit()
    c.close()
    M._log_line(failed, f"[12:00:00] ❌ План сцен отклонён: {reason}")
    return failed


def fail_task(jid, error, reason):
    """Уронить уже стоящую задачу промптов — как это делает _run_task по коду выхода."""
    c = M.db()
    c.execute("UPDATE queue_tasks SET status='error', error=? WHERE job_id=?", (error, jid))
    c.execute("UPDATE gen_jobs SET status='error' WHERE id=?", (jid,))
    c.commit()
    c.close()
    M._log_line(jid, f"[12:05:00] ❌ План сцен отклонён: {reason}")


def set_disabled(flag):
    c = M.db()
    c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if flag else 0, UID))
    c.commit()
    c.close()


def tick():
    M._LAST_DEFERRED[0] = 0
    M._deferred_render_tick()


def tasks(kind):
    c = M.db()
    rows = c.execute("SELECT job_id,status,params FROM queue_tasks WHERE tenant_id=? AND "
                     "project_id=? AND kind=? ORDER BY job_id", (TID, PID, kind)).fetchall()
    c.close()
    return [{"job_id": r["job_id"], "status": r["status"],
             "params": json.loads(r["params"] or "{}")} for r in rows]


def settings_now():
    return M._psettings(M._get_project(TID, PID))


def logs(jid=None):
    c = M.db()
    if jid is None:
        rows = c.execute("SELECT line FROM job_logs ORDER BY id").fetchall()
    else:
        rows = c.execute("SELECT line FROM job_logs WHERE job_id=? ORDER BY id", (jid,)).fetchall()
    c.close()
    return [r["line"] for r in rows]


def has_note(jid, needle="выключил её в проекте"):
    return any(needle in line for line in logs(jid))


def main() -> int:
    print("Воркер: код выхода называет породу отказа")

    check("канон разбора персонажей и мест → 11",
          worker_exit_code(W.ScenePlanCanonConflictError), 11)
    check("канон, не исправленный повторами, тоже 11",
          worker_exit_code(W.ScenePlanCanonSchemaError), 11)
    check("смысловая нарезка → 12",
          worker_exit_code(W.ScenePlanSemanticExhaustedError), 12)
    check("любой другой отказ плана остаётся 1 — кабинет ничего не выключает",
          worker_exit_code(W.ScenePlanTerminalError), 1)
    check("и обычная поломка схемы тоже 1",
          worker_exit_code(W.ScenePlanSchemaError), 1)
    check("временный сбой по-прежнему 75 (повтор, а не откат)",
          worker_exit_code(W.ScenePlanTransientError), 75)

    print("\nКоды берутся из настоящих проверок, а не из отдельной ветки для теста")

    check("непомеченный канонический alias даёт канонический класс",
          canon_error_from_real_check(), W.ScenePlanCanonConflictError)
    check("исчерпанная смысловая нарезка даёт свой класс",
          semantic_error_from_real_check(), W.ScenePlanSemanticExhaustedError)

    print("\nКабинет: код 11 выключает разбор персонажей и переставляет задачу")

    failed = seed(SETTINGS, "rc=11", "story_bible cache: канонический alias 'DuPont' неоднозначен")
    tick()
    prompts = tasks("prompts")
    st = settings_now()
    check("промпты переставлены, а не брошены", [t["status"] for t in prompts],
          ["error", "queued"])
    check("новая задача идёт без разбора персонажей",
          prompts[-1]["params"].get("analyze_story"), "0")
    check("вторая галочка не тронута — виновата не она",
          prompts[-1]["params"].get("split_long_sentences"), "1")
    check("выключение видно в самом проекте", st.get("analyze_story"), "0")
    check("откат записан в настройки", st.get(M._PLAN_DEGRADE_SETTING), ["analyze_story"])
    check("картинки по-прежнему ждут план", str(st.get("deferred_images_pending")), str(UID))
    check("отложенные параметры картинок тоже без разбора",
          (st.get("deferred_images_params") or {}).get("analyze_story"), "0")
    check("этап не снят, маркера обрыва нет", tasks("images"), [])

    # Замечание 4: _project_note вешает строку на последнюю задачу по job_id DESC, а к
    # этому моменту последняя — уже НОВАЯ. Красную карточку человек открывает у упавшей,
    # и объяснение должно лежать именно там.
    note = [line for line in logs(failed) if "выключил её в проекте" in line]
    check("объяснение лежит в логе УПАВШЕЙ задачи", len(note), 1)
    check("строка называет виноватую настройку",
          "разбор персонажей и мест" in (note[0] if note else ""), True)
    check("и объясняет причину словами воркера", "DuPont" in (note[0] if note else ""), True)
    check("дубль есть и в новой задаче — её лог начинается с причины",
          has_note(prompts[-1]["job_id"]), True)

    print("\nКабинет: код 12 выключает дробление длинных предложений")

    failed = seed(SETTINGS, "rc=12", "semantic block 1/1: схема не исправлена после 3 повторов")
    tick()
    prompts = tasks("prompts")
    st = settings_now()
    check("промпты переставлены", prompts[-1]["status"], "queued")
    check("новая задача идёт без дробления",
          prompts[-1]["params"].get("split_long_sentences"), "0")
    check("разбор персонажей не тронут", prompts[-1]["params"].get("analyze_story"), "1")
    check("выключение видно в проекте", st.get("split_long_sentences"), "0")
    check("откат записан", st.get(M._PLAN_DEGRADE_SETTING), ["split_long_sentences"])
    check("строка лога про нужную настройку — у упавшей задачи",
          any("дробление длинных предложений" in line and "выключил её в проекте" in line
              for line in logs(failed)), True)

    print("\nДеньги: отметка о потраченной попытке лежит в БД уже в момент постановки")

    # Обрыв между оплаченной постановкой и концом обхода проекта (деплой, рестарт,
    # исключение на соседнем этапе того же проекта). Подменяем шаг, который идёт сразу
    # после записи отметки, — он здесь стоит за «процесс умер до общего апдейта тика».
    failed = seed(SETTINGS, "rc=11", "канон отклонил план")
    saved_reason = M._plan_failure_reason

    def _die(_jid):
        raise RuntimeError("обрыв тика сразу после оплаченной постановки")

    M._plan_failure_reason = _die
    try:
        tick()
    finally:
        M._plan_failure_reason = saved_reason
    st = settings_now()
    replan = tasks("prompts")[-1]["job_id"]
    check("задача переплана поставлена — деньги заказаны",
          [t["status"] for t in tasks("prompts")], ["error", "queued"])
    check("отметка о попытке уцелела после обрыва",
          st.get(M._PLAN_DEGRADE_SETTING), ["analyze_story"])
    check("а общий апдейт тика до настроек не доехал — значит отметка писалась отдельно",
          st.get("analyze_story"), "1")

    # Теперь новая задача падает тем же кодом. Без отдельной записи отметки кабинет
    # решил бы, что откат ещё не делали, и оплатил бы второй переплан.
    fail_task(replan, "rc=11", "канон снова отклонил план")
    tick()
    check("второго оплаченного переплана нет",
          [t["status"] for t in tasks("prompts")], ["error", "error"])
    check("этап снят честно", "deferred_images_pending" in settings_now(), False)

    print("\nЗаблокированный аккаунт не тратит единственную попытку")

    failed = seed(SETTINGS, "rc=11", "канон отклонил план")
    set_disabled(True)
    try:
        tick()
    finally:
        set_disabled(False)
    st = settings_now()
    check("задача не встала", [t["status"] for t in tasks("prompts")], ["error"])
    check("отметка о потраченной попытке не появилась",
          M._PLAN_DEGRADE_SETTING in st, False)
    check("галочка осталась включённой — откат не состоялся", st.get("analyze_story"), "1")
    check("ожидание картинок снято целиком, а не переписано откатом",
          "deferred_images_params" in st, False)
    check("этап снят видимо, а не молча", [t["status"] for t in tasks("images")], ["error"])
    check("объяснения про откат в логе упавшей нет — откатывать было нечем",
          has_note(failed), False)

    print("\nОткат не перевыпускает набор референсов: квитанция про тот же набор")

    # Пользователь добавил второй ракурс героя МЕЖДУ заявкой и падением плана. Откат
    # обязан остаться на закреплённом наборе: иначе image_receipts.v1.json опишет фото,
    # которых в заказе не было.
    failed = seed(SETTINGS, "rc=11", "канон отклонил план", heroes={"Герой": [(10, 20, 30)]})
    pinned = tasks("prompts")[0]["params"].get(M._REFERENCE_BUNDLE_PARAM)
    check("заявка вообще закрепила набор", isinstance(pinned, str) and len(pinned) > 0, True)
    projdir = M._project_dir(TID, PID)
    commit_refs(projdir, {"Герой": [(10, 20, 30), (200, 100, 50)]})
    tick()
    prompts = tasks("prompts")
    live = M._snapshot_reference_bundle(projdir, {**SETTINGS, "analyze_story": "0"})
    check("живая папка референсов и правда стала другой", live != pinned, True)
    check("переплан идёт на ЗАКРЕПЛЁННОМ наборе, а не на живой папке",
          prompts[-1]["params"].get(M._REFERENCE_BUNDLE_PARAM), pinned)
    check("и отложенные параметры картинок держат его же",
          (settings_now().get("deferred_images_params") or {}).get(M._REFERENCE_BUNDLE_PARAM),
          pinned)

    print("\nНесовместимый с откатом набор героев: fail closed, а не молчаливый переплан")

    # Двух именованных героев без разбора сцены связать нечем — это блокирующая проверка
    # _reference_gate. Откат обязан отказаться, а не поставить заведомо мусорную задачу.
    failed = seed(SETTINGS, "rc=11", "канон отклонил план",
                  heroes={"Первый": [(10, 20, 30)], "Второй": [(200, 100, 50)]})
    tick()
    st = settings_now()
    check("переплана нет", [t["status"] for t in tasks("prompts")], ["error"])
    check("отметка о попытке не потрачена", M._PLAN_DEGRADE_SETTING in st, False)
    check("галочка не выключена", st.get("analyze_story"), "1")
    check("причина отказа — в логе упавшей задачи",
          any("контракт референсов" in line for line in logs(failed)), True)
    check("этап снят видимо", [t["status"] for t in tasks("images")], ["error"])

    print("\nОстальные отказы по-прежнему снимают этап видимо")

    seed(SETTINGS, "rc=1", "план сцен не построен")
    tick()
    st = settings_now()
    images = tasks("images")
    check("самовольного переплана нет", [t["status"] for t in tasks("prompts")], ["error"])
    check("ожидание картинок снято", "deferred_images_pending" in st, False)
    check("галочки не трогали", st.get("analyze_story"), "1")
    check("маркер обрыва создан", [t["status"] for t in images], ["error"])
    check("откат в настройках не появился", M._PLAN_DEGRADE_SETTING in st, False)

    print("\nВторой откат той же галочки запрещён — задача падает честно")

    seed({**SETTINGS, M._PLAN_DEGRADE_SETTING: ["split_long_sentences"]}, "rc=12",
         "semantic block 1/1: схема не исправлена после 3 повторов")
    tick()
    check("переплана нет", [t["status"] for t in tasks("prompts")], ["error"])
    check("ожидание картинок снято", "deferred_images_pending" in settings_now(), False)

    print("\nУже выключенную галочку не откатывают: план упал не из-за неё")

    seed({**SETTINGS, "analyze_story": "0"}, "rc=11", "канон отклонил план")
    tick()
    check("переплана нет", [t["status"] for t in tasks("prompts")], ["error"])
    check("ожидание картинок снято", "deferred_images_pending" in settings_now(), False)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
