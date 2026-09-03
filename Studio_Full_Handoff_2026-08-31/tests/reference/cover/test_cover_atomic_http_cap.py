import os
import subprocess
import sys
from pathlib import Path


def test_web_and_api_v1_concurrent_post_at_99_accept_exactly_one(tmp_path):
    project = Path(__file__).parents[1]
    script = r'''
import threading
import os
from fastapi.testclient import TestClient
import cover_server
from job_queue_cover import Job

client = TestClient(cover_server.app)
setup = client.post(
    "/auth/setup",
    json={"username": "cap_admin", "password": "safe-pass-123"},
)
assert setup.status_code == 200, setup.text
session_id = setup.json()["session_id"]
session_headers = {"X-Session-Id": session_id}
user_id = cover_server.license_mgr.get_user_id(session_id)
assert user_id

web_body = {
    "mode": "seed",
    "languages": ["Русский"],
    "seeds_per_lang": {"Русский": "atomic cap seed"},
    "n_variants": 1,
    "preset_name": "default",
    "preset_data": {
        "name": "default",
        "generation_provider": "flow",
        "generation_model": "GEM_PIX_2",
    },
}

def run_race(path, headers, body, accepted_status):
    for _ in range(99):
        cover_server.queue.add_job(
            Job(user_id=user_id),
            max_pending_for_user=100,
        )
    barrier = threading.Barrier(2)
    statuses = []
    lock = threading.Lock()

    def post_once():
        local = TestClient(cover_server.app)
        barrier.wait()
        response = local.post(path, headers=headers, json=body)
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=post_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(statuses) == sorted([accepted_status, 429]), statuses
    assert cover_server.queue.count_reserved_for_user(user_id) == 100

def clear_jobs():
    for queued in cover_server.queue.get_all_jobs():
        cover_server.queue.delete_job(queued.id)
    assert cover_server.queue.count_reserved_for_user(user_id) == 0

run_race("/jobs/cover", session_headers, web_body, 200)
clear_jobs()

# API auth is also used to verify the API v1 retry transition.
cover_server.ApiAuthState.configure(cover_server.DB_PATH)
cover_server.ApiAuthState.init_db()
key_response = client.post(
    "/api-keys",
    headers=session_headers,
    json={"name": "cap-test"},
)
assert key_response.status_code == 200, key_response.text
api_headers = {"X-API-Key": key_response.json()["plain_key"]}
presets = client.get("/api/v1/presets", headers=api_headers)
assert presets.status_code == 200, presets.text
assert "Default RU" in presets.json()
api_body = {
    "mode": "url",
    "preset_name": "Default RU",
    "urls": ["https://www.youtube.com/watch?v=DEMO0000001"],
    "languages": ["Русский"],
}

# 100 reserved jobs reject every terminal→reserved web/API transition.
terminal = Job(
    user_id=user_id,
    mode="seed",
    generation_provider="flow",
    preset_data='{"generation_provider":"flow"}',
)
terminal_id = cover_server.queue.add_job(
    terminal,
    max_pending_for_user=100,
)
assert cover_server.queue.cancel_job(terminal_id)
for _ in range(100):
    cover_server.queue.add_job(
        Job(user_id=user_id),
        max_pending_for_user=100,
    )
assert cover_server.queue.count_reserved_for_user(user_id) == 100

stored = cover_server.queue.get_job(terminal_id)
stored.output_dir = str(cover_server._get_user_output_dir(user_id))
fallback_path = os.path.join(stored.output_dir, "cap-must-survive.jpg")
open(fallback_path, "wb").write(b"safe")
stored.output_files = '["cap-must-survive.jpg"]'
stored.fallback_files = '["cap-must-survive.jpg"]'
assert cover_server.queue.update_job(stored, expected_status="cancelled")

response = client.post(
    f"/jobs/{terminal_id}/retry",
    headers=session_headers,
)
assert response.status_code == 429, response.text
assert cover_server.queue.count_reserved_for_user(user_id) == 100
assert cover_server.queue.get_job(terminal_id).status == "cancelled"

response = client.post(
    f"/jobs/{terminal_id}/resume",
    headers=session_headers,
    json={"regenerate_fallbacks": True},
)
assert response.status_code == 429, response.text
assert os.path.exists(fallback_path)
assert cover_server.queue.count_reserved_for_user(user_id) == 100
assert cover_server.queue.get_job(terminal_id).status == "cancelled"

response = client.post(
    f"/api/v1/jobs/{terminal_id}/retry",
    headers=api_headers,
)
assert response.status_code == 429, response.text
assert cover_server.queue.count_reserved_for_user(user_id) == 100
assert cover_server.queue.get_job(terminal_id).status == "cancelled"

# At 99, add and retry contend for the same final slot: exactly one wins.
pending = next(
    queued for queued in cover_server.queue.get_all_jobs()
    if queued.status == "pending"
)
assert cover_server.queue.cancel_job(pending.id)
assert cover_server.queue.count_reserved_for_user(user_id) == 99
barrier = threading.Barrier(2)
statuses = []
lock = threading.Lock()

def add_one():
    local = TestClient(cover_server.app)
    barrier.wait()
    response = local.post(
        "/jobs/cover",
        headers=session_headers,
        json=web_body,
    )
    with lock:
        statuses.append(response.status_code)

def retry_one():
    local = TestClient(cover_server.app)
    barrier.wait()
    response = local.post(
        f"/jobs/{terminal_id}/retry",
        headers=session_headers,
    )
    with lock:
        statuses.append(response.status_code)

threads = [threading.Thread(target=add_one), threading.Thread(target=retry_one)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=10)
    assert not thread.is_alive()
assert sorted(statuses) == [200, 429], statuses
assert cover_server.queue.count_reserved_for_user(user_id) == 100

clear_jobs()
run_race("/api/v1/jobs", api_headers, api_body, 201)
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
    env.pop("COVER_MAX_PENDING", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
