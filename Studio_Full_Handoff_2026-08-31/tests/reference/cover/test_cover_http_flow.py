import os
import subprocess
import sys
from pathlib import Path


def test_login_submit_list_cancel_retry_and_secret_boundary(tmp_path):
    project = Path(__file__).parents[1]
    script = r'''
import os
from fastapi.testclient import TestClient
import cover_server

client = TestClient(cover_server.app)
assert client.get("/").status_code == 200
assert client.get("/static/index.html").status_code == 200
assert client.get("/static/index.html.bak").status_code == 404
assert client.get("/static/index.html.bak2").status_code == 404
response = client.post(
    "/auth/setup",
    json={"username": "smoke_admin", "password": "safe-pass-123"},
)
assert response.status_code == 200, response.text
headers = {"X-Session-Id": response.json()["session_id"]}

body = {
    "mode": "seed",
    "languages": ["Русский"],
    "seeds_per_lang": {"Русский": "ТЕСТОВОЕ СЕМЯ ДЛЯ ПРОВЕРКИ"},
    "n_variants": 1,
    "preset_name": "default",
    "preset_data": {
        "name": "default",
        "generation_provider": "flow",
        "generation_model": "GEM_PIX_2",
    },
    "refs_per_language": {},
}
response = client.post("/jobs/cover", headers=headers, json=body)
assert response.status_code == 200, response.text
job_id = response.json()["job_id"]

response = client.get("/jobs", headers=headers)
assert response.status_code == 200
assert any(item["id"] == job_id for item in response.json())

response = client.get(f"/jobs/{job_id}", headers=headers)
assert response.status_code == 200
assert not ({"openai_api_key", "fast_gen_api_key", "veo_api_key"} & set(response.json()))

assert client.post(f"/jobs/{job_id}/cancel", headers=headers).status_code == 200
assert client.post(f"/jobs/{job_id}/retry", headers=headers).status_code == 200
assert client.post(f"/jobs/{job_id}/cancel", headers=headers).status_code == 200

bad = dict(body)
bad["veo_api_key"] = "client-secret"
assert client.post("/jobs/cover", headers=headers, json=bad).status_code == 422

settings = client.get("/admin/gen-settings", headers=headers)
assert settings.status_code == 200, settings.text
settings_data = settings.json()
assert settings_data["effective"]["queue_limit_per_user"] == 100
assert settings_data["effective"]["max_parallel_jobs"] == 10
assert settings_data["effective"]["fastgen_weighted_budget_per_key"] == 8
assert settings_data["effective"]["veo_inflight_per_key"] == 4
assert settings_data["effective"]["gpt_image2_inflight_per_key"] == 2
assert settings_data["effective"]["provider_job_slots"] == {
    "fastgen": 10,
    "veo": 4,
    "gpt_image2": 2,
    "xai": 2,
}
assert settings_data["effective"]["credential_pool"]["fastgen"]["unique_keys"] == 1
assert "max_parallel_jobs" in settings_data["restart_required"]
assert "provider_job_slots" in settings_data["restart_required"]
assert settings_data["env_overrides"]["queue_limit_per_user"] is False

# Resume must validate effective provider/key before deleting fallback files.
job = cover_server.queue.get_job(job_id)
fallback = os.path.join(job.output_dir, "must-survive.jpg")
os.makedirs(job.output_dir, exist_ok=True)
open(fallback, "wb").write(b"safe")
job.output_files = '["must-survive.jpg"]'
job.fallback_files = '["must-survive.jpg"]'
assert cover_server.queue.update_job(job, expected_status="cancelled")
os.environ.pop("COVER_VEO_KEY", None)
response = client.post(
    f"/jobs/{job_id}/resume",
    headers=headers,
    json={
        "override_provider": "veo",
        "override_model": "GEM_PIX_2",
        "regenerate_fallbacks": True,
    },
)
assert response.status_code == 503, response.text
assert os.path.exists(fallback)
assert "must-survive.jpg" in cover_server.queue.get_job(job_id).fallback_files

# Forced stage-CAS failure: no referenced file may be deleted.
os.environ["COVER_VEO_KEY"] = "test-veo"
real_update = cover_server.queue.update_job
calls = {"n": 0}
def fail_stage(job, *args, **kwargs):
    calls["n"] += 1
    if calls["n"] == 2:
        return False
    return real_update(job, *args, **kwargs)
cover_server.queue.update_job = fail_stage
response = client.post(
    f"/jobs/{job_id}/resume",
    headers=headers,
    json={"regenerate_fallbacks": True},
)
assert response.status_code == 409, response.text
assert os.path.exists(fallback)
stored = cover_server.queue.get_job(job_id)
assert stored.status == "resetting"
assert "must-survive.jpg" in stored.fallback_files
cover_server.queue.update_job = real_update
assert cover_server.queue.recover_interrupted_jobs()["requeued"] == 1
assert cover_server.queue.cancel_job(job_id)

# Forced final-CAS failure: deleted file was already removed from DB metadata,
# and startup recovery can safely requeue the RESETTING row.
open(fallback, "wb").write(b"safe-again")
job = cover_server.queue.get_job(job_id)
job.output_files = '["must-survive.jpg"]'
job.fallback_files = '["must-survive.jpg"]'
assert cover_server.queue.update_job(job, expected_status="cancelled")
calls = {"n": 0}
def fail_final(job, *args, **kwargs):
    calls["n"] += 1
    if calls["n"] == 3:
        return False
    return real_update(job, *args, **kwargs)
cover_server.queue.update_job = fail_final
response = client.post(
    f"/jobs/{job_id}/resume",
    headers=headers,
    json={"regenerate_fallbacks": True},
)
assert response.status_code == 409, response.text
assert not os.path.exists(fallback)
stored = cover_server.queue.get_job(job_id)
assert stored.status == "resetting"
assert "must-survive.jpg" not in stored.output_files
assert stored.fallback_files == "[]"
cover_server.queue.update_job = real_update
assert cover_server.queue.recover_interrupted_jobs()["requeued"] == 1
assert cover_server.queue.cancel_job(job_id)

# Default technical cap accepts exactly 100 pending jobs.
for _ in range(100):
    response = client.post("/jobs/cover", headers=headers, json=body)
    assert response.status_code == 200, response.text
response = client.post("/jobs/cover", headers=headers, json=body)
assert response.status_code == 429, response.text

raw = open(os.environ["COVER_DB_PATH"], "rb").read()
assert b"test-openai" not in raw
assert b"test-fastgen" not in raw
assert b"test-veo" not in raw
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
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
