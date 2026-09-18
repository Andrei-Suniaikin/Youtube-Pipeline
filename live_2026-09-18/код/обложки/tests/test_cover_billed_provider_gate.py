# -*- coding: utf-8 -*-
"""Гейт платных генераторов (grok_api — личный счёт владельца).

Раньше доступ был «только админ», и проверка стояла ровно в одном месте —
при создании задачи. `/jobs/{id}/resume` принимал `override_provider` без
проверки вообще, а интерфейс строил три из четырёх списков моделей из полного
GENERATION_OPTIONS, то есть платный пункт был виден всем.

Тесты фиксируют оба конца: обычному юзеру закрыты и создание, и resume;
юзеру из COVER_GROK_API_USER_IDS открыты оба, и /me/extra-models это
подтверждает. Второй сценарий (разрешённый НЕ админ) — основной здесь:
именно он появился, ради него всё и менялось.
"""

import os
import subprocess
import sys
from pathlib import Path


_SCRIPT = r'''
import json
from fastapi.testclient import TestClient
import cover_server

client = TestClient(cover_server.app)

# Первый заведённый аккаунт становится админом.
r = client.post("/auth/setup", json={"username": "owner", "password": "safe-pass-123"})
assert r.status_code == 200, r.text
admin_h = {"X-Session-Id": r.json()["session_id"]}

# Обычный юзер, заведённый админом.
r = client.post("/auth/accounts", headers=admin_h,
                json={"username": "worker", "password": "safe-pass-456", "user_name": "Worker"})
assert r.status_code == 200, r.text
r = client.post("/auth/login-password", json={"username": "worker", "password": "safe-pass-456"})
assert r.status_code == 200, r.text
user_sid = r.json()["session_id"]
user_uid = r.json()["user_id"]
user_h = {"X-Session-Id": user_sid}

BILLED = {"name": "default", "generation_provider": "grok_api", "generation_model": ""}
def job_body(preset):
    return {
        "mode": "seed",
        "languages": ["Русский"],
        "seeds_per_lang": {"Русский": "ТЕСТОВОЕ СЕМЯ ДЛЯ ПРОВЕРКИ"},
        "n_variants": 1,
        "preset_name": "default",
        "preset_data": preset,
        "refs_per_language": {},
    }

# ── 1. Обычный юзер: не видит и не может ────────────────────────────────────
r = client.get("/me/extra-models", headers=user_h)
assert r.status_code == 200, r.text
assert r.json()["providers"] == [], r.text

r = client.post("/jobs/cover", headers=user_h, json=job_body(BILLED))
assert r.status_code == 403, ("создание задачи с grok_api должно быть закрыто", r.status_code, r.text)

# resume с подменой провайдера — та самая дырка
r = client.post("/jobs/cover", headers=user_h,
                json=job_body({"name": "default", "generation_provider": "flow",
                               "generation_model": "GEM_PIX_2"}))
assert r.status_code == 200, r.text
job_id = r.json()["job_id"]
assert client.post("/jobs/" + job_id + "/cancel", headers=user_h).status_code == 200
r = client.post("/jobs/" + job_id + "/resume", headers=user_h,
                json={"override_provider": "grok_api", "override_model": ""})
assert r.status_code == 403, ("resume с grok_api должен быть закрыт", r.status_code, r.text)

# ── 2. Админу открыто всегда ────────────────────────────────────────────────
r = client.get("/me/extra-models", headers=admin_h)
assert r.json()["providers"] == ["grok_api"], r.text

# ── 3. Юзер из COVER_GROK_API_USER_IDS: открыто без прав админа ────────────
cover_server.os.environ["COVER_GROK_API_USER_IDS"] = user_uid
r = client.get("/me/extra-models", headers=user_h)
assert r.json()["providers"] == ["grok_api"], r.text
r = client.post("/jobs/" + job_id + "/resume", headers=user_h,
                json={"override_provider": "grok_api", "override_model": ""})
assert r.status_code != 403, ("разрешённому юзеру resume не должен давать 403", r.status_code, r.text)

# Список читается на каждый запрос — снятие из env закрывает доступ обратно.
cover_server.os.environ["COVER_GROK_API_USER_IDS"] = ""
r = client.get("/me/extra-models", headers=user_h)
assert r.json()["providers"] == [], r.text
r = client.post("/jobs/cover", headers=user_h, json=job_body(BILLED))
assert r.status_code == 403, (r.status_code, r.text)

# Разделитель ';' и пробелы в списке тоже понимаются.
cover_server.os.environ["COVER_GROK_API_USER_IDS"] = "aaa ; " + user_uid + " , bbb"
assert client.get("/me/extra-models", headers=user_h).json()["providers"] == ["grok_api"]

print("OK")
'''


def test_billed_provider_gate(tmp_path):
    project = Path(__file__).parents[1]
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(project),
        "COVER_DB_PATH": str(tmp_path / "queue.db"),
        "COVER_SESSION_DB": str(tmp_path / "sessions.db"),
        "COVER_OUTPUT_DIR": str(tmp_path / "outputs"),
        "COVER_UPLOAD_DIR": str(tmp_path / "uploads"),
        "COVER_PRESETS_DIR": str(tmp_path / "presets"),
        "COVER_EVENTS_LOG": str(tmp_path / "events.log"),
        "COVER_FAST_GEN_KEY": "test-fastgen-key",
        "COVER_XAI_KEY": "test-xai-key",
        "COVER_GROK_API_USER_IDS": "",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=str(project), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
