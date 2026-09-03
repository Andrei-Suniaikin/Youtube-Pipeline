"""Remote render lifecycle must update both queue and accounting projections."""

import importlib.util
import os
import sys
import time


MAIN_PY = os.environ.get("VS_MAIN_PY", "/Users/owner/VideoStudio/server/main.py")


def _load(tmp_path, name):
    env = {
        "VS_SERVER_DB": str(tmp_path / "vsgen.db"),
        "VS_SERVER_KEYS_PATH": str(tmp_path / "keys.json"),
        "VS_SERVER_DISABLE_BACKGROUND": "1",
        "PULLER_TOKEN": "puller-test-token",
    }
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    server_dir = os.path.dirname(MAIN_PY)
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    try:
        spec = importlib.util.spec_from_file_location(name, MAIN_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _render_task(module, jid, status="running"):
    c = module.db()
    c.execute(
        "INSERT INTO gen_jobs(id,user_id,type,status,ts) VALUES(?,?,?,?,?)",
        (jid, 1, "render", status, "2026-08-25T12:00:00"),
    )
    c.execute(
        "INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,started,"
        "uid,provider,params,claim_token) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (jid, "owner", "project-1", "render", status, time.time(), time.time(),
         1, "render", "{}", f"claim-{jid}"),
    )
    c.commit()
    c.close()


def _statuses(module, jid):
    c = module.db()
    queue_status = c.execute(
        "SELECT status,error FROM queue_tasks WHERE job_id=?", (jid,)
    ).fetchone()
    gen_status = c.execute(
        "SELECT status,error FROM gen_jobs WHERE id=?", (jid,)
    ).fetchone()
    c.close()
    return queue_status, gen_status


def test_render_error_updates_queue_and_gen_jobs(tmp_path):
    from fastapi.testclient import TestClient

    module = _load(tmp_path, "render_projection_error")
    _render_task(module, 701)
    headers = {
        "X-Puller-Token": "puller-test-token",
        "X-Render-Claim": "claim-701",
    }
    with TestClient(module.app) as client:
        response = client.post(
            "/api/render/701/result?status=error&error=ffmpeg%20encoder%20exploded",
            headers=headers,
        )
    assert response.status_code == 200
    queue_row, gen_row = _statuses(module, 701)
    assert queue_row["status"] == gen_row["status"] == "error"
    assert "ffmpeg encoder exploded" in queue_row["error"]
    assert gen_row["error"] == queue_row["error"]


def test_render_success_updates_queue_and_gen_jobs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    module = _load(tmp_path, "render_projection_done")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(module, "_project_dir", lambda *_args: str(project_dir))
    _render_task(module, 702)
    headers = {
        "X-Puller-Token": "puller-test-token",
        "X-Render-Claim": "claim-702",
        "Content-Type": "video/mp4",
    }
    with TestClient(module.app) as client:
        response = client.post(
            "/api/render/702/result?status=done", headers=headers, content=b"x" * 2048
        )
    assert response.status_code == 200
    queue_row, gen_row = _statuses(module, 702)
    assert queue_row["status"] == gen_row["status"] == "done"
    assert gen_row["error"] is None

