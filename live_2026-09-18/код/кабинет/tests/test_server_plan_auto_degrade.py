"""Отложенные картинки: план сцен должен сходиться сам с собой, а снятый этап — быть виден.

Боевой случай (проект p_d70e1275a1, тенант 6): партнёр ставит картинки по API, послав НЕ всю
форму (только model/sentences_per_prompt/aspect_ratio, без галочек analyze_story и
split_long_sentences). Дальше:

  1) _enqueue видит, что плана нет, и ставит отдельную задачу «промпты» с params ИЗ ЗАПРОСА;
  2) промпты пишут scenes.json + паспорт scenes.meta.json, где analyze_story=false;
  3) _deferred_render_tick сверяет паспорт с НАСТРОЙКАМИ ПРОЕКТА (там analyze_story=1) →
     не совпало;
  4) ветка «elif not prompts_active» снимала ожидание и НЕ ставила картинки.

Итог: промпты оплачены, валидный план лежит на диске, а этап картинок исчезал молча —
ни ошибки, ни строки в логе, ни задачи в очереди.

Инварианты, которые держат эти тесты:

  1) задача промптов и последующая проверка готовности считают ОДИН набор параметров
     (настройки проекта + поля запроса) — паспорт заведомо сходится сам с собой;
  2) план есть, но не совпал с параметрами → план ПЕРЕПИСЫВАЕТСЯ (ограниченное число раз),
     а не бросается этап;
  3) любое снятие этапа картинок пишет причину в лог задачи;
  4) сырое multipart-тело не превращается в имя настройки/параметра воркера.
"""


import asyncio


import hashlib


import importlib.util


import json


import os


import sys


from pathlib import Path


from types import SimpleNamespace


import pytest


class FakeRequest:
    def __init__(self, *, body=b"", headers=None):
        self._body = body
        self.cookies = {}
        self.headers = headers or {}
        self.state = SimpleNamespace()

    async def body(self):
        return self._body


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    root = tmp_path_factory.mktemp("vs_server_deferred_images")
    env = {
        "VS_SERVER_DB": str(root / "vsgen.db"),
        "VS_SERVER_KEYS_PATH": str(root / "keys.json"),
        "VS_SERVER_DISABLE_BACKGROUND": "1",
    }
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    Path(env["VS_SERVER_KEYS_PATH"]).write_text(
        json.dumps({"googler": "server-googler-test", "openai": "server-openai-test"}),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(
        "vs_server_plan_degrade_under_test", __import__("os").environ.get("VS_MAIN_PY", str(__import__("pathlib").Path(__file__).resolve().parents[1] / "server" / "main.py"))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def clean(srv):
    c = srv.db()
    for table in ("queue_tasks", "gen_jobs", "job_logs", "projects", "users"):
        c.execute(f"DELETE FROM {table}")
    c.commit()
    c.close()
    srv._LAST_DEFERRED[0] = 0
    yield


TID = "tenant-9"


PID = "p_defer"


UID = 9


SOURCE = "текст сценария для теста\n"


PROJECT_SETTINGS = {
    "style": "minimalistic, cinematic, photorealistic",
    "model": "OPENAI",
    "sentences_per_prompt": "3",
    "analyze_story": "1",
    "split_long_sentences": "1",
    "max_solo_scene_seconds": "6",
}


REQUEST_PARAMS = {"model": "OPENAI", "sentences_per_prompt": "3", "aspect_ratio": "16:9"}


def add_user(srv, uid=UID, name="partner", role="user"):
    c = srv.db()
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.execute(
        "INSERT INTO users(id,name,token,role,created,keys,key_opts,password_hash,tenant_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, name, f"tok-{uid}", role, "2026-07-30T00:00:00", "{}", "{}", None, TID),
    )
    c.commit()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return row


def add_project(srv, projdir, settings=None):
    c = srv.db()
    c.execute("DELETE FROM projects WHERE id=?", (PID,))
    c.execute(
        "INSERT INTO projects(id,tenant_id,name,dir_path,status,created,updated,settings)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (PID, TID, "Project", str(projdir), "new", 1.0, 1.0,
         json.dumps(settings if settings is not None else PROJECT_SETTINGS, ensure_ascii=False)),
    )
    c.commit()
    c.close()


def make_projdir(tmp_path):
    (tmp_path / "source.txt").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "googler").mkdir(exist_ok=True)
    return tmp_path


def write_plan(srv, projdir, params):
    """Сымитировать задачу «промпты»: план + паспорт РОВНО по её params.

    Зеркало _save_plan_meta из googler_worker.py — паспорт пишется по параметрам ЗАДАЧИ,
    а не по настройкам проекта; в этом и была вся суть дефекта.
    """
    gd = Path(projdir) / "googler"
    gd.mkdir(exist_ok=True)
    scenes = [{"visual_prompt": "a wide cinematic shot"}]
    scene_bytes = json.dumps(scenes, ensure_ascii=False).encode("utf-8")
    (gd / "scenes.json").write_bytes(scene_bytes)
    source_bytes = SOURCE.encode("utf-8", "replace")
    analyze_story = bool(srv._truthy(params.get("analyze_story"), default=False))
    meta = {
        "plan_version": srv.SCENE_PLAN_VERSION,
        "scene_count": len(scenes),
        "scenes_sha256": hashlib.sha256(scene_bytes).hexdigest(),
        "model": str(params.get("model") or "OPENAI"),
        "style": str(params.get("style") or "minimalistic, cinematic, photorealistic"),
        "sentences_per_prompt": str(params.get("sentences_per_prompt") or "3"),
        "analyze_story": analyze_story,
        "auto_hero": bool(srv._truthy(params.get("auto_hero"), default=False)),
        "smart_mode": bool(srv._truthy(params.get("smart_mode"), default=False)),
        "source_sha1": hashlib.sha1(source_bytes).hexdigest(),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "manual_identity_present": False,
        "story_identity_present": False,
        "auto_hero_prompt_required": False,
        "prompt_artifacts": {
            "subject_anchor": {"subject": "bird"},
            "story_bible": ({"characters": [], "locations": []}
                            if analyze_story else None),
            "hero_reference": None,
        },
    }
    meta.update(srv._split_long_plan_contract(params))
    (gd / "scenes.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def tasks(srv, kind=None):
    c = srv.db()
    sql = "SELECT job_id, kind, params, status FROM queue_tasks WHERE project_id=? AND tenant_id=?"
    args = [PID, TID]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = c.execute(sql + " ORDER BY job_id", args).fetchall()
    c.close()
    return [dict(job_id=r["job_id"], kind=r["kind"], status=r["status"],
                 params=json.loads(r["params"] or "{}")) for r in rows]


def settings_now(srv):
    return srv._psettings(srv._get_project(TID, PID))


def logs(srv):
    c = srv.db()
    rows = c.execute("SELECT line FROM job_logs ORDER BY id").fetchall()
    c.close()
    return [r["line"] for r in rows]


def finish(srv, job_id, status="done"):
    c = srv.db()
    c.execute("UPDATE queue_tasks SET status=? WHERE job_id=?", (status, job_id))
    c.execute("UPDATE gen_jobs SET status=? WHERE id=?", (status, job_id))
    c.commit()
    c.close()


MULTIPART_BODY = (
    b"------WebKitFormBoundaryEJBm6p3eAemYuAPT\r\n"
    b'Content-Disposition: form-data; name="model"\r\n\r\n'
    b"OPENAI\r\n"
    b"------WebKitFormBoundaryEJBm6p3eAemYuAPT--\r\n"
)


JUNK_KEY = ("------WebKitFormBoundaryEJBm6p3eAemYuAPT\r\n"
            "Content-Disposition: form-data; name")


def _failed_prompts(srv, projdir, monkeypatch, *, settings, error,
                    reason="story_bible cache: канонический alias 'DuPont' неоднозначен"):
    add_user(srv)
    add_project(srv, projdir, {**settings,
                               "deferred_images_pending": UID,
                               "deferred_images_params": {k: v for k, v in settings.items()
                                                          if k != "auto_degraded_plan"}})
    monkeypatch.setattr(srv, "_project_dir", lambda _tid, _pid: str(projdir))
    failed = srv._enqueue(UID, TID, PID, "prompts", {k: v for k, v in settings.items()
                                                     if k != "auto_degraded_plan"})
    assert failed
    finish(srv, failed, "error")
    c = srv.db()
    c.execute("UPDATE queue_tasks SET error=? WHERE job_id=?", (error, failed))
    c.commit()
    c.close()
    srv._log_line(failed, f"[12:00:00] ❌ План сцен отклонён: {reason}")
    return failed


def _tick(srv):
    srv._LAST_DEFERRED[0] = 0
    srv._deferred_render_tick()


def test_canon_conflict_turns_off_story_analysis_once_and_keeps_images_waiting(srv, monkeypatch, tmp_path):
    projdir = make_projdir(tmp_path)
    _failed_prompts(srv, projdir, monkeypatch, settings=PROJECT_SETTINGS, error="rc=11")
    _tick(srv)

    prompts = tasks(srv, "prompts")
    assert [t["status"] for t in prompts] == ["error", "queued"]
    assert prompts[-1]["params"]["analyze_story"] == "0"
    assert prompts[-1]["params"]["split_long_sentences"] == "1", "выключается только виноватая настройка"
    st = settings_now(srv)
    assert st["analyze_story"] == "0", "выключение видно в самом проекте"
    assert st["auto_degraded_plan"] == ["analyze_story"]
    assert str(st["deferred_images_pending"]) == str(UID), "картинки по-прежнему ждут план"
    assert st["deferred_images_params"]["analyze_story"] == "0"
    assert st["deferred_images_params"].get(srv._REFERENCE_BUNDLE_PARAM)
    assert tasks(srv, "images") == [], "этап не снят и маркер обрыва не создан"
    assert any("разбор персонажей и мест: выключено автоматически" in line and "DuPont" in line
               for line in logs(srv))


def test_semantic_exhaustion_turns_off_long_sentence_split(srv, monkeypatch, tmp_path):
    projdir = make_projdir(tmp_path)
    _failed_prompts(srv, projdir, monkeypatch, settings=PROJECT_SETTINGS, error="rc=12",
                    reason="semantic block 5/10: схема не исправлена после 3 повторов")
    _tick(srv)

    prompts = tasks(srv, "prompts")
    assert prompts[-1]["status"] == "queued"
    assert prompts[-1]["params"]["split_long_sentences"] == "0"
    assert prompts[-1]["params"]["analyze_story"] == "1"
    st = settings_now(srv)
    assert st["split_long_sentences"] == "0"
    assert st["auto_degraded_plan"] == ["split_long_sentences"]
    assert any("дробление длинных предложений: выключено автоматически" in line for line in logs(srv))


def test_other_plan_failures_still_drop_the_stage_visibly(srv, monkeypatch, tmp_path):
    projdir = make_projdir(tmp_path)
    _failed_prompts(srv, projdir, monkeypatch, settings=PROJECT_SETTINGS, error="rc=1")
    _tick(srv)

    assert [t["status"] for t in tasks(srv, "prompts")] == ["error"], "никакого самовольного переплана"
    st = settings_now(srv)
    assert "deferred_images_pending" not in st
    assert st["analyze_story"] == "1"
    images = tasks(srv, "images")
    assert len(images) == 1 and images[0]["status"] == "error"
    assert any("не завершилась успешно" in line for line in logs(srv))


def test_same_failure_after_rollback_drops_the_stage(srv, monkeypatch, tmp_path):
    projdir = make_projdir(tmp_path)
    settings = {**PROJECT_SETTINGS, "split_long_sentences": "1", "auto_degraded_plan": ["split_long_sentences"]}
    _failed_prompts(srv, projdir, monkeypatch, settings=settings, error="rc=12")
    _tick(srv)

    assert [t["status"] for t in tasks(srv, "prompts")] == ["error"], "второй откат той же настройки запрещён"
    assert "deferred_images_pending" not in settings_now(srv)


def test_setting_already_off_is_not_rolled_back(srv, monkeypatch, tmp_path):
    projdir = make_projdir(tmp_path)
    _failed_prompts(srv, projdir, monkeypatch, settings={**PROJECT_SETTINGS, "analyze_story": "0"}, error="rc=11")
    _tick(srv)

    assert [t["status"] for t in tasks(srv, "prompts")] == ["error"]
    assert "deferred_images_pending" not in settings_now(srv)
