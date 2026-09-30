import os
import subprocess
import sys
from pathlib import Path


def test_web_and_api_validation_never_reflect_or_persist_request_secret(tmp_path):
    project = Path(__file__).parents[1]
    script = r'''
import contextlib
import io
import json
import logging
import os
from pathlib import Path

sentinel = "validation-secret-4c71ef88"
application_log = io.StringIO()
stdout_capture = io.StringIO()
stderr_capture = io.StringIO()
handler = logging.StreamHandler(application_log)
root_logger = logging.getLogger()
root_logger.addHandler(handler)

with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
    from fastapi.testclient import TestClient
    import cover_server
    from job_queue_cover import Job

    client = TestClient(cover_server.app)
    baseline_job_id = cover_server.queue.add_job(
        Job(user_id="baseline-user", log="baseline application job log")
    )
    setup = client.post(
        "/auth/setup",
        json={"username": "validation_admin", "password": "safe-pass-123"},
    )
    assert setup.status_code == 200
    session_headers = {"X-Session-Id": setup.json()["session_id"]}

    web_body = {
        "mode": "seed",
        "languages": ["Русский"],
        "seeds_per_lang": {"Русский": "sanitization seed"},
        "n_variants": 1,
        "preset_name": "default",
        "preset_data": {
            "name": "default",
            "generation_provider": "flow",
            "generation_model": "GEM_PIX_2",
        },
        "veo_api_key": sentinel,
    }
    web_response = client.post(
        "/jobs/cover",
        headers=session_headers,
        json=web_body,
    )
    assert web_response.status_code == 422

    cover_server.ApiAuthState.configure(cover_server.DB_PATH)
    cover_server.ApiAuthState.init_db()
    key_response = client.post(
        "/api-keys",
        headers=session_headers,
        json={"name": "validation-test"},
    )
    assert key_response.status_code == 200
    api_headers = {"X-API-Key": key_response.json()["plain_key"]}
    api_response = client.post(
        "/api/v1/jobs",
        headers=api_headers,
        json={
            "mode": "url",
            "preset_name": "Default RU",
            "urls": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
            "languages": ["Русский"],
            "openai_api_key": sentinel,
        },
    )
    assert api_response.status_code == 422

root_logger.removeHandler(handler)
expected = {
    "detail": [
        {
            "type": "request_validation_error",
            "loc": ["body"],
            "msg": "Request validation failed",
        }
    ]
}
assert web_response.json() == expected
assert api_response.json() == expected
assert sentinel not in web_response.text
assert sentinel not in api_response.text

jobs = cover_server.queue.get_all_jobs()
assert len(jobs) == 1
assert jobs[0].id == baseline_job_id
job_log_hits = sum(str(job.log).count(sentinel) for job in jobs)
assert job_log_hits == 0

active_db_paths = []
for env_name in ("COVER_DB_PATH", "COVER_SESSION_DB"):
    db_path = Path(os.environ[env_name])
    active_db_paths.extend([
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ])
sentinel_bytes = sentinel.encode()
db_hits = sum(
    path.read_bytes().count(sentinel_bytes)
    for path in active_db_paths
    if path.exists()
)
assert db_hits == 0

captured = (
    application_log.getvalue()
    + stdout_capture.getvalue()
    + stderr_capture.getvalue()
)
captured_log_hits = captured.count(sentinel)
assert captured_log_hits == 0
'''
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(project),
        "COVER_SERVER_DIR": str(tmp_path),
        "COVER_DB_PATH": str(tmp_path / "jobs.db"),
        "COVER_SESSION_DB": str(tmp_path / "sessions.db"),
        "COVER_STATIC_DIR": str(project / "static"),
        "COVER_OPENAI_KEY": "test-openai",
        "COVER_FAST_GEN_KEY": "test-fastgen",
        "COVER_VEO_KEY": "test-veo",
    })
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
