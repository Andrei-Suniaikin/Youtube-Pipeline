"""Кнопка «Пожаловаться» в кабинете: приём жалобы и показ итога на странице проекта.

Гоняется против копии БОЕВОГО main.py (локальный server/main.py отстаёт от VPS).

Инварианты:
  1) пожаловаться может только вошедший пользователь и только на свой проект;
  2) пока прошлая жалоба разбирается и 30 минут после неё — новая не принимается;
  3) у аккаунта есть дневной потолок жалоб;
  4) на странице проекта есть кнопка, а итог разбора показан экранированным текстом.
"""

import importlib.util
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MAIN_PY = os.environ.get("VS_MAIN_PY", str(Path(__file__).resolve().parents[1] / "server" / "main.py"))


def _load(root, name):
    env = {
        "VS_SERVER_DB": str(root / "vsgen.db"),
        "VS_SERVER_KEYS_PATH": str(root / "keys.json"),
        "VS_SERVER_DISABLE_BACKGROUND": "1",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, MAIN_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mk_user(m, name, tenant):
    c = m.db()
    c.execute("INSERT INTO users(name,token,role,created,tenant_id,password_hash) VALUES(?,?,?,?,?,?)",
              (name, "usr_" + name, "user", "2026-09-13", tenant, m.hash_password("secret123")))
    c.commit()
    uid = c.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()["id"]
    c.close()
    return uid


def _mk_project(m, tenant, pid, name="Кварц"):
    d = Path(m.TENANTS_DIR) / tenant / "projects" / pid
    d.mkdir(parents=True, exist_ok=True)
    c = m.db()
    c.execute("INSERT INTO projects(id,tenant_id,name,dir_path,status,created,updated,settings) VALUES(?,?,?,?,?,?,?,?)",
              (pid, tenant, name, str(d), "draft", "2026-09-13", "2026-09-13", "{}"))
    c.commit()
    c.close()


def _complaints(m):
    c = m.db()
    rows = [dict(r) for r in c.execute("SELECT * FROM complaints ORDER BY id")]
    c.close()
    return rows


def _set(m, cid, **fields):
    c = m.db()
    c.execute("UPDATE complaints SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), cid))
    c.commit()
    c.close()


@pytest.fixture
def m(tmp_path):
    module = _load(tmp_path, f"complaints_{tmp_path.name}")
    module.TENANTS_DIR = str(tmp_path / "tenants")
    return module


def test_complaint_needs_login_and_own_project(m):
    mama = _mk_user(m, "mama", "14")
    _mk_user(m, "other", "7")
    _mk_project(m, "14", "p_mama")
    _mk_project(m, "7", "p_other")
    with TestClient(m.app) as client:
        assert client.post("/api/projects/p_mama/complaint", data={"text": "картинки не идут"}).status_code == 401
        client.cookies.set(m.SESSION_COOKIE, m.create_session(mama))
        assert client.post("/api/projects/p_other/complaint", data={"text": "картинки не идут"}).status_code == 404
        assert client.post("/api/projects/p_mama/complaint", data={"text": "ок"}).status_code == 400
        ok = client.post("/api/projects/p_mama/complaint", data={"text": "картинки не идут уже день"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert [(r["tenant_id"], r["uid"], r["project_id"], r["status"], r["text"]) for r in _complaints(m)] == [
        ("14", mama, "p_mama", "queued", "картинки не идут уже день")]


def test_next_complaint_waits_for_the_previous_one_and_30_minutes(m):
    mama = _mk_user(m, "mama", "14")
    _mk_project(m, "14", "p_mama")
    with TestClient(m.app) as client:
        client.cookies.set(m.SESSION_COOKIE, m.create_session(mama))
        first = client.post("/api/projects/p_mama/complaint", data={"text": "картинки не идут"})
        assert first.status_code == 200
        busy = client.post("/api/projects/p_mama/complaint", data={"text": "ну что там?"})
        assert busy.status_code == 429 and "ещё разбирается" in busy.json()["detail"]

        cid = first.json()["id"]
        _set(m, cid, status="fixed", created=time.time() - 10 * 60)
        early = client.post("/api/projects/p_mama/complaint", data={"text": "опять не идут"})
        assert early.status_code == 429 and "через 20 мин" in early.json()["detail"]

        _set(m, cid, created=time.time() - 31 * 60)
        later = client.post("/api/projects/p_mama/complaint", data={"text": "опять не идут"})
        assert later.status_code == 200
    assert [r["status"] for r in _complaints(m)] == ["fixed", "queued"]


def test_account_has_a_daily_cap(m):
    mama = _mk_user(m, "mama", "14")
    for pid in ("p_1", "p_2", "p_3"):
        _mk_project(m, "14", pid)
    m._COMPLAINT_TENANT_DAILY = 2
    with TestClient(m.app) as client:
        client.cookies.set(m.SESSION_COOKIE, m.create_session(mama))
        assert client.post("/api/projects/p_1/complaint", data={"text": "картинки не идут"}).status_code == 200
        assert client.post("/api/projects/p_2/complaint", data={"text": "картинки не идут"}).status_code == 200
        capped = client.post("/api/projects/p_3/complaint", data={"text": "картинки не идут"})
    assert capped.status_code == 429 and "Лимит жалоб" in capped.json()["detail"]
    assert len(_complaints(m)) == 2


def test_project_page_shows_button_and_escaped_result(m):
    mama = _mk_user(m, "mama", "14")
    _mk_project(m, "14", "p_mama")
    c = m.db()
    c.execute("INSERT INTO complaints(tenant_id,uid,project_id,text,status,created,user_message) VALUES(?,?,?,?,?,?,?)",
              ("14", mama, "p_mama", "картинки", "fixed", time.time(), "Перезапустил <картинки> заново"))
    c.commit()
    c.close()
    with TestClient(m.app) as client:
        client.cookies.set(m.SESSION_COOKIE, m.create_session(mama))
        page = client.get("/p/p_mama")
    assert page.status_code == 200
    body = page.text
    assert "Пожаловаться" in body and "/api/projects/p_mama/complaint" in body
    assert "починено" in body
    assert "Перезапустил &lt;картинки&gt; заново" in body and "<картинки>" not in body


def test_nothing_broken_is_shown_as_no_problem_not_as_failure(m):
    mama = _mk_user(m, "mama", "14")
    _mk_project(m, "14", "p_mama")
    c = m.db()
    c.execute("INSERT INTO complaints(tenant_id,uid,project_id,text,status,created,user_message) VALUES(?,?,?,?,?,?,?)",
              ("14", mama, "p_mama", "ничего не происходит", "no_problem", time.time(), "Озвучка ещё не запускалась."))
    c.commit()
    c.close()
    with TestClient(m.app) as client:
        client.cookies.set(m.SESSION_COOKIE, m.create_session(mama))
        body = client.get("/p/p_mama").text
    assert "проблем не нашёл" in body and "Озвучка ещё не запускалась." in body
    assert "передано владельцу" not in body
