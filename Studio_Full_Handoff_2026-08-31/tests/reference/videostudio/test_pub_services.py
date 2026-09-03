#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты связки движка с реальными стадиями кабинета."""

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from pub_engine import ItemFailed, ResourceDown, phase_history, tick  # noqa: E402
from pub_schema import ensure_pub_schema  # noqa: E402
from pub_services import MEDIA_STAGES, PubServices  # noqa: E402

NOW = 1786900000.0
DAY = 86400


@pytest.fixture()
def env(tmp_path):
    """Файловая база: связка открывает своё соединение на каждый вызов, как в бою."""
    path = str(tmp_path / "t.db")

    def db():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    c = db()
    ensure_pub_schema(c)
    c.execute("CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, status TEXT)")
    c.execute("CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    cid = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,lead_days,preset_vsgen_id,created,updated)"
        " VALUES('t1','UC" + "a" * 22 + "','Канал',2,7,?,?)", (NOW, NOW)).lastrowid
    c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,created,updated)"
              " VALUES('t1',?,'https://donor/1',?,?,?)", (cid, NOW + DAY, NOW, NOW))
    c.commit()
    c.close()

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "source.txt").write_text("готовый текст ролика", encoding="utf-8")
    (proj / "googler").mkdir()
    (proj / "googler" / "001.jpg").write_bytes(b"x")   # этап картинок проверяется по продукту

    state = {"projects": [], "jobs": [], "queue_state": "queued"}

    def create_project(tid, name, source_text="", settings=None):
        pid = f"p_{len(state['projects'])}"
        state["projects"].append({"pid": pid, "name": name, "text": source_text,
                                  "settings": settings})
        return pid

    def enqueue(uid, tid, pid, kind, params):
        jid = 100 + len(state["jobs"])
        state["jobs"].append({"kind": kind, "job_id": jid, "project": pid})
        x = db()
        x.execute("INSERT INTO queue_tasks(job_id,status) VALUES(?,?)",
                  (jid, state["queue_state"]))
        x.commit()
        x.close()
        return jid

    services = PubServices({"db": db, "create_project": create_project, "enqueue": enqueue,
                            "project_dir": lambda tid, pid: str(proj), "owner_uid": 1})
    def fake_rewrite(c, rel):
        """Подставной рерайт: кладёт текст туда же, куда положит настоящий."""
        services.store_source_text(c, rel["release_id"], "готовый текст ролика", NOW)
        return {}

    services.do_source_ready = fake_rewrite

    def run(n=1, now=NOW):
        out = None
        for _ in range(n):
            x = db()
            out = tick(x, services, now)
            x.close()
        return out

    def rel():
        x = db()
        row = x.execute("SELECT * FROM pub_releases").fetchone()
        x.close()
        return row

    def set_queue(status):
        x = db()
        x.execute("UPDATE queue_tasks SET status=?", (status,))
        x.commit()
        x.close()
        state["queue_state"] = status

    return {"db": db, "run": run, "rel": rel, "state": state, "services": services,
            "set_queue": set_queue}


# ─── неподключённые стадии ───

def test_without_key_release_waits_not_fails(env):
    """Ключа доступа к рерайту нет — это отказ общего ресурса, а не сбой строки."""
    env["services"].do_source_ready = PubServices.do_source_ready.__get__(env["services"])
    env["run"](2)
    r = env["rel"]()
    assert r["status"] == "pending"
    assert "ключ" in r["blocked_reason"]


def test_waiting_release_keeps_its_plan_row_alive(env):
    env["services"].do_source_ready = PubServices.do_source_ready.__get__(env["services"])
    env["run"](3)
    x = env["db"]()
    assert x.execute("SELECT status FROM pub_plan_items").fetchone()[0] == "frozen"
    x.close()


def test_profile_prompt_pause_blocks_new_publication_rewrite(env):
    """Персональная пауза должна закрывать и отдельный Rewrite контура «Каналы»."""
    env["services"].do_source_ready = PubServices.do_source_ready.__get__(env["services"])
    x = env["db"]()
    x.execute(
        "INSERT INTO admin_state(k,v,updated) VALUES('queue_pause',?,?)",
        (json.dumps({"all": False, "providers": [], "prompt_uids": [1]}), NOW),
    )
    x.commit()
    rel = {"release_id": "r_paused", "tenant_id": "t1"}
    with pytest.raises(ResourceDown, match="приостановлена"):
        env["services"].do_source_ready(x, rel)
    assert x.execute(
        "SELECT COUNT(*) FROM pub_release_jobs WHERE release_id='r_paused'"
    ).fetchone()[0] == 0
    x.close()


def test_publish_check_still_waits(env):
    """Проверка выхода в PUBLIC сознательно не подключена: ждёт, а не притворяется."""
    with pytest.raises(ResourceDown):
        env["services"].do_published(None, None)


def test_upload_needs_profile(env):
    """Без профиля AdsPower заливать нечем — это сбой строки, а не общий ресурс."""
    x = env["db"]()
    with pytest.raises(ItemFailed):
        env["services"].do_upload_pending(x, {"release_id": "r_x", "profile_id": None,
                                              "project_id": "p_0"})
    x.close()


def test_upload_needs_video(env, tmp_path):
    """mp4 нет — не заливаем и честно говорим почему."""
    empty = tmp_path / "nofilm"
    empty.mkdir()
    env["services"].project_dir = lambda tid, pid: str(empty)
    x = env["db"]()
    row = {"release_id": "r_x", "profile_id": 9002, "project_id": "p_0", "tenant_id": "t1",
           "deferred": 0, "deferred_limit": 5, "channel_id": 1}
    with pytest.raises(ItemFailed):
        env["services"].do_upload_pending(x, row)
    x.close()


def test_deferred_limit_makes_it_wait(env, tmp_path, monkeypatch):
    """Лимит отложки исчерпан — готовый ролик ЖДЁТ места, а не льётся вне расписания."""
    proj = tmp_path / "p"
    (proj / "render").mkdir(parents=True)
    (proj / "render" / "1.mp4").write_bytes(b"x" * 10)
    env["services"].project_dir = lambda tid, pid: str(proj)
    x = env["db"]()
    # два уже подтверждённых будущих ролика при лимите 2
    for i, rid in enumerate(("r_a", "r_b")):
        x.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
                  "channel_id,uc,slot_at,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                  (rid, "t1", 100 + i, 1, 1, "UC_x", NOW + 9e5, NOW, NOW))
        x.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
                  "title,video_path,slot_at,verdict,created,updated)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, "t1", 1, "UC_x", "u", "t", "/v.mp4", NOW + 9e5, "scheduled", NOW, NOW))
    x.commit()
    row = {"release_id": "r_new", "profile_id": 9002, "project_id": "p_0", "tenant_id": "t1",
           "deferred": 1, "deferred_limit": 2, "channel_id": 1, "lang": "pl",
           "donor_title": "Оригинальное название", "tz": "Europe/Warsaw",
           "slot_at": NOW + 9e5, "preset_vsgen_id": None, "collaborator": "", "uc": "UC_x",
           "host_id": None}
    with pytest.raises(ResourceDown):
        env["services"].do_upload_pending(x, row)
    x.close()


def test_cover_without_key_waits(env):
    x = env["db"]()
    row = {"preset_cover": "какой-то", "release_id": "r_1", "lang": "pl",
           "tenant_id": "t1", "project_id": "p_0", "slot_at": NOW, "ch_name": "К",
           "updated": NOW}
    with pytest.raises(ResourceDown):
        env["services"].do_cover_ready(x, row)
    x.close()


def test_cover_without_preset_is_item_failure(env):
    """Пресет обложек не выбран — это про эту строку, а не про общий ресурс."""
    x = env["db"]()
    with pytest.raises(ItemFailed):
        env["services"].do_cover_ready(x, {"preset_cover": "", "release_id": "r_1"})
    x.close()


def test_terminal_rewrite_success_updates_durable_mirror(env, monkeypatch):
    import pub_services as ps
    env["services"].do_source_ready = PubServices.do_source_ready.__get__(env["services"])

    class RewriteDone:
        def __init__(self, key):
            pass

        def job(self, job_id):
            return {"status": "done"}

        def result_code(self, info, lang):
            return "pl"

        def result_text(self, job_id, code):
            return "готовый текст " * 60

    monkeypatch.setattr(ps, "RewriteClient", RewriteDone)
    env["services"]._clock = lambda: NOW
    x = env["db"]()
    x.execute("INSERT INTO admin_state(k,v,updated) VALUES('pub_key_rewrite','rw',?)", (NOW,))
    x.execute(
        "INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,created,updated) "
        "VALUES('r_rw','rewrite','job','remote-rw','running',?,?)",
        (NOW, NOW),
    )
    env["services"].do_source_ready(
        x, {"release_id": "r_rw", "tenant_id": "t1", "lang": "pl", "updated": NOW}
    )
    assert x.execute(
        "SELECT status,error FROM pub_release_jobs WHERE release_id='r_rw' AND kind='job'"
    ).fetchone()["status"] == "done"
    x.close()


def test_terminal_rewrite_failure_updates_durable_mirror(env, monkeypatch):
    import pub_services as ps
    env["services"].do_source_ready = PubServices.do_source_ready.__get__(env["services"])

    class RewriteFailed:
        def __init__(self, key):
            pass

        def job(self, job_id):
            return {"status": "failed", "error": "bad source"}

    monkeypatch.setattr(ps, "RewriteClient", RewriteFailed)
    env["services"]._clock = lambda: NOW
    x = env["db"]()
    x.execute("INSERT INTO admin_state(k,v,updated) VALUES('pub_key_rewrite','rw',?)", (NOW,))
    x.execute(
        "INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,created,updated) "
        "VALUES('r_rw_bad','rewrite','job','remote-rw','running',?,?)",
        (NOW, NOW),
    )
    with pytest.raises(ItemFailed, match="bad source"):
        env["services"].do_source_ready(
            x, {"release_id": "r_rw_bad", "tenant_id": "t1", "lang": "pl",
                "updated": NOW}
        )
    row = x.execute(
        "SELECT status,error FROM pub_release_jobs WHERE release_id='r_rw_bad'"
    ).fetchone()
    assert row["status"] == "error" and "bad source" in row["error"]
    x.close()


def test_terminal_cover_success_updates_durable_mirror(env, monkeypatch):
    import pub_services as ps

    class CoverDone:
        def __init__(self, key):
            pass

        def job(self, job_id):
            return {"status": "done", "results_count": 1}

        def download(self, job_id, lang):
            return b"c" * 6000

    monkeypatch.setattr(ps, "CoverClient", CoverDone)
    env["services"]._clock = lambda: NOW
    x = env["db"]()
    x.execute("INSERT INTO admin_state(k,v,updated) VALUES('pub_key_cover','cover',?)", (NOW,))
    x.execute(
        "INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,created,updated) "
        "VALUES('r_cover','cover','job','remote-cover','running',?,?)",
        (NOW, NOW),
    )
    env["services"].do_cover_ready(
        x, {"release_id": "r_cover", "preset_cover": "preset", "donor_url": "https://d", 
            "lang": "pl", "tenant_id": "t1", "project_id": "p_0", "updated": NOW}
    )
    assert x.execute(
        "SELECT status FROM pub_release_jobs WHERE release_id='r_cover'"
    ).fetchone()[0] == "done"
    x.close()


def test_terminal_cover_failure_updates_durable_mirror(env, monkeypatch):
    import pub_services as ps

    class CoverFailed:
        def __init__(self, key):
            pass

        def job(self, job_id):
            return {"status": "failed", "error": "provider rejected"}

    monkeypatch.setattr(ps, "CoverClient", CoverFailed)
    env["services"]._clock = lambda: NOW
    x = env["db"]()
    x.execute("INSERT INTO admin_state(k,v,updated) VALUES('pub_key_cover','cover',?)", (NOW,))
    x.execute(
        "INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,created,updated) "
        "VALUES('r_cover_bad','cover','job','remote-cover','running',?,?)",
        (NOW, NOW),
    )
    with pytest.raises(ItemFailed, match="provider rejected"):
        env["services"].do_cover_ready(
            x, {"release_id": "r_cover_bad", "preset_cover": "preset",
                "donor_url": "https://d", "lang": "pl", "tenant_id": "t1",
                "project_id": "p_0", "updated": NOW}
        )
    assert x.execute(
        "SELECT status FROM pub_release_jobs WHERE release_id='r_cover_bad'"
    ).fetchone()[0] == "error"
    x.close()


# ─── проект ───

def test_project_is_created_from_text(env):
    env["run"](2)
    assert env["state"]["projects"], "проект должен создаться"
    assert env["state"]["projects"][0]["text"] == "готовый текст ролика"
    assert env["rel"]()["project_id"] == "p_0"


def test_project_uses_channel_preset(env):
    env["run"](2)
    assert env["state"]["projects"][0]["settings"] == {"_applied_preset": 7}


def test_project_created_once_even_on_repeat_ticks(env):
    env["run"](6)
    assert len(env["state"]["projects"]) == 1


def test_missing_text_quarantines_the_row(env, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    env["services"].project_dir = lambda tid, pid: str(empty)
    env["services"].do_source_ready = lambda c, rel: {}   # рерайт «прошёл», а текста нет
    env["run"](3)
    r = env["rel"]()
    assert r["status"] == "blocked"
    assert "нет текста" in r["blocked_reason"]


# ─── этапы ролика ───

def test_stages_are_queued_one_by_one(env):
    env["run"](3)
    assert [j["kind"] for j in env["state"]["jobs"]] == ["tts"]


def test_next_stage_starts_only_after_previous_is_done(env):
    env["run"](3)
    env["set_queue"]("done")
    env["run"](1)
    # Production запускает images рядом с tts. В тесте очередь уже отвечает done,
    # поэтому в этот же тик становится готов к постановке render.
    assert [j["kind"] for j in env["state"]["jobs"]] == ["tts", "images", "render"]


def test_all_stages_pass_and_phase_moves_on(env):
    for _ in range(len(MEDIA_STAGES)):
        env["run"](2)
        env["set_queue"]("done")
    env["run"](2)
    assert [j["kind"] for j in env["state"]["jobs"]] == list(MEDIA_STAGES)
    assert env["rel"]()["phase"] == "media_ready"


def test_failed_stage_quarantines_the_row(env):
    env["run"](3)
    env["set_queue"]("error")
    env["run"](2)
    r = env["rel"]()
    assert r["status"] == "blocked"
    assert "tts" in r["blocked_reason"]


def test_stage_jobs_are_remembered(env):
    env["run"](3)
    x = env["db"]()
    rows = x.execute("SELECT kind, remote_job_id FROM pub_release_jobs"
                     " WHERE service='vsgen'").fetchall()
    x.close()
    assert [r["kind"] for r in rows] == ["tts"]


def test_queue_refusal_is_resource_wait_not_quarantine(env):
    env["services"].enqueue = lambda *a, **kw: None
    env["run"](3)
    r = env["rel"]()
    assert r["status"] == "pending"
    assert "ждём" in (r["blocked_reason"] or "")
    assert "resource_wait" in phase_history(env["db"](), r["release_id"])


def test_media_without_project_is_item_failure(env):
    s = env["services"]
    with pytest.raises(ItemFailed):
        s.do_media_ready(None, {"project_id": None, "release_id": "r_x"})


# ─── заголовок обложки ───

def test_cover_needs_donor_link(env):
    """Обложка делается от донорской ссылки — без неё это сбой строки."""
    x = env["db"]()
    x.execute("INSERT INTO admin_state(k,v,updated) VALUES('pub_key_cover','cvr_x',?)", (NOW,))
    with pytest.raises(ItemFailed):
        env["services"].do_cover_ready(x, {"preset_cover": "мой", "release_id": "r_1",
                                           "donor_url": "", "lang": "pl"})
    x.close()


def test_chapters_failure_does_not_stop_release(env):
    """Главы собрались не всегда — ролик всё равно должен уйти в публикацию."""
    from pub_services import MEDIA_STAGES, OPTIONAL_STAGES
    assert "chapters" in MEDIA_STAGES and "chapters" in OPTIONAL_STAGES
    assert MEDIA_STAGES.index("chapters") > MEDIA_STAGES.index("render"), \
        "главы считаются по транскрипту, который приносит рендер"

    for _ in range(len(MEDIA_STAGES)):
        env["run"](2)
        env["set_queue"]("done")        # каждый поставленный этап проходит

    # А теперь роняем ТОЛЬКО главы, остальные этапы остаются успешными.
    x = env["db"]()
    x.execute("UPDATE queue_tasks SET status='error' WHERE job_id=("
              "SELECT remote_job_id FROM pub_release_jobs WHERE kind='chapters')")
    x.commit()
    x.close()
    env["run"](2)
    r = env["rel"]()
    # Выпуск прошёл этап роликов насквозь: фаза сдвинулась дальше сборки,
    # и ни одна остановка не связана с главами.
    assert r["phase"] in ("media_ready", "cover_ready"), r["phase"]
    assert "chapters" not in (r["blocked_reason"] or ""), r["blocked_reason"]


def test_past_slot_is_refused_before_tasking(env, tmp_path, monkeypatch):
    """Ролик не успел к своему времени — отказ ДО задания, а не заливка в прошлое."""
    proj = tmp_path / "p2"
    (proj / "render").mkdir(parents=True)
    (proj / "render" / "1.mp4").write_bytes(b"x" * 10)
    env["services"].project_dir = lambda tid, pid: str(proj)
    env["services"]._clock = lambda: NOW + 10 * 86400          # «сейчас» позже слота
    x = env["db"]()
    row = {"release_id": "r_late", "profile_id": 9002, "project_id": "p_0", "tenant_id": "t1",
           "deferred": 1, "deferred_limit": 5, "channel_id": 1, "lang": "pl",
           "donor_title": "Название", "tz": "Europe/Warsaw", "slot_at": NOW + DAY,
           "preset_vsgen_id": None, "collaborator": "", "uc": "UC_x", "host_id": None}
    with pytest.raises(ItemFailed) as err:
        env["services"].do_upload_pending(x, row)
    assert "не успели" in str(err.value)
    x.close()


def test_full_tick_reaches_upload_task_with_real_release_row(env, tmp_path, monkeypatch):
    """Сквозной прогон БЕЗ подмены выпуска словарём.

    Ровно этот тест ловит пропавшие колонки live_releases (deferred, donor_title):
    подставные словари в других тестах скрывали их отсутствие, и первый настоящий
    выпуск умер бы на передаче в заливку.
    """
    import pub_services as ps

    x = env["db"]()
    x.execute("UPDATE pub_channels SET profile_id=9002, deferred=1, deferred_limit=5, "
              "tz='Europe/Warsaw', lead_days=2")
    # Слот через сутки: выпуск «созрел» (готовим за 2 дня), но время ещё не прошло.
    x.execute("UPDATE pub_plan_items SET donor_title='Оригинальное название донора', "
              "slot_at=?", (NOW + 86400,))
    x.commit()
    x.close()

    # готовый ролик и обложка в папке проекта
    proj_dir = tmp_path / "proj"
    (proj_dir / "render").mkdir(parents=True, exist_ok=True)
    (proj_dir / "render" / "555.mp4").write_bytes(b"v" * 100)
    (proj_dir / "cover.jpg").write_bytes(b"c" * 10)
    (proj_dir / "googler").mkdir(exist_ok=True)
    (proj_dir / "googler" / "001.jpg").write_bytes(b"x")
    env["services"].project_dir = lambda tid, pid: str(proj_dir)
    env["services"]._clock = lambda: NOW
    monkeypatch.setattr(ps, "make_title", lambda *a, **k: "Przepisany tytuł dawcy")
    env["services"].openai_key = lambda: "sk-test"
    env["services"].do_cover_ready = lambda c, rel: {}

    for _ in range(16):
        env["run"](1)
        env["set_queue"]("done")
        r = env["rel"]()
        if r and r["phase"] == "upload_pending":
            break
    r = env["rel"]()
    assert r["phase"] == "upload_pending", (r["phase"], r["blocked_reason"])

    x = env["db"]()
    job = x.execute("SELECT * FROM pub_upload_jobs").fetchone()
    x.close()
    assert job is not None, "задание на заливку обязано появиться"
    assert job["title"] == "Przepisany tytuł dawcy"
    assert job["video_path"].endswith(".mp4")
    assert job["schedule_iso"], "отложка включена — время выхода обязано быть в задании"
    assert "+0" in job["schedule_iso"] or "+1" in job["schedule_iso"] or "+2" in job["schedule_iso"]


def test_project_gets_full_preset_settings(env):
    """Пресет канала должен приехать в проект целиком: голос, движок, стиль.

    Раньше в проект уходила ОДНА пометка «пресет такой-то»: на странице проекта он
    выглядел выбранным, а озвучка и картинки шли на умолчаниях кабинета.
    """
    import json as _json
    c = env["db"]()
    c.execute("CREATE TABLE presets(id INTEGER PRIMARY KEY, settings TEXT)")
    c.execute("INSERT INTO presets(id,settings) VALUES(7,?)", (_json.dumps(
        {"engine": "voicegencore", "voice": "7wG1Xb86NcD408WGmfbJ",
         "model_id": "eleven_multilingual_v2"}, ensure_ascii=False),))
    c.commit()
    c.close()

    env["run"](2)
    got = env["state"]["projects"][0]["settings"]
    assert got["voice"] == "7wG1Xb86NcD408WGmfbJ", "голос пресета не доехал до проекта"
    assert got["engine"] == "voicegencore"
    assert got["_applied_preset"] == 7, "пометка нужна тоже: по ней рисуется селект пресета"


def test_missing_preset_does_not_kill_release_but_complains(env):
    """Пресет удалили между планом и запуском — выпуск идёт дальше, но жалуется в журнал."""
    env["run"](2)
    assert env["state"]["projects"], "проект должен создаться даже без пресета"
    assert env["state"]["projects"][0]["settings"] == {"_applied_preset": 7}
    c = env["db"]()
    строки = [r[0] for r in c.execute(
        "SELECT message FROM pub_release_log WHERE release_id=(SELECT release_id FROM pub_releases)")]
    c.close()
    assert any("не прочитались" in (t or "") for t in строки), \
        "молчаливые умолчания недопустимы — в журнале должна быть жалоба"
