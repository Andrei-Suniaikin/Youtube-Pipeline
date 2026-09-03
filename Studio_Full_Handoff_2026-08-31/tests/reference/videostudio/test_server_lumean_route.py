import asyncio
import importlib.util
import json
import os
import time
import urllib.parse
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[3]
MAIN_PY = os.environ.get("VS_MAIN_PY", str(PACKAGE_ROOT / "source/vsgen_server/main.py"))
TEMPLATE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_TEMPLATE_ID = "22222222-2222-4222-8222-222222222222"


class FormRequest:
    def __init__(self, data):
        self._body = urllib.parse.urlencode(data).encode("utf-8")

    async def body(self):
        return self._body


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    root = tmp_path_factory.mktemp("vs_server_lumean")
    env = {
        "VS_SERVER_DB": str(root / "vsgen.db"),
        "VS_SERVER_KEYS_PATH": str(root / "keys.json"),
        "VS_SERVER_DISABLE_BACKGROUND": "1",
    }
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("vs_server_lumean_under_test", MAIN_PY)
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


def test_lumean_is_a_separate_engine_provider_and_credential_family(srv, monkeypatch):
    assert srv._task_provider("tts", {"engine": "lumean"}) == "lumean"
    assert srv._task_provider("tts", {"engine": "tts"}) == "elevenlabs"
    assert srv._provider_credential_family("lumean") == "lumean"
    assert srv._provider_credential_family("elevenlabs") == "elevenlabs"
    assert srv._CREDENTIAL_ENV_BY_FAMILY["lumean"] == "LUMEAN_API_KEY"
    assert srv._CREDENTIAL_ENV_BY_FAMILY["elevenlabs"] == "ELEVENLABS_API_KEY"
    assert "lumean" in srv.TTS_ENGINES
    assert srv._ENGINE_AUDIO_DIR["lumean"] == "_FINAL_AUDIO_LUMEAN"
    assert srv._ENGINE_AUDIO_DIR["tts"] == "_FINAL_AUDIO_ELEVENLABS"
    assert srv._PROVIDER_HARD_JOB_CAPS["lumean"] == 10

    monkeypatch.setattr(
        srv,
        "keys",
        lambda: {"prompt_provider": "openai", "prompt_threads": 1},
    )
    user = {"id": 1, "role": "admin", "keys": "{}"}
    env = srv._key_env(
        user,
        shared=True,
        credential_overrides={"lumean": "server-lumean-test"},
        prompt_provider="openai",
    )
    assert env["LUMEAN_API_KEY"] == "server-lumean-test"
    assert "ELEVENLABS_API_KEY" not in env

    legacy_env = srv._key_env(
        user,
        shared=True,
        credential_overrides={"elevenlabs": "server-eleven-test"},
        prompt_provider="openai",
    )
    assert legacy_env["ELEVENLABS_API_KEY"] == "server-eleven-test"
    assert "LUMEAN_API_KEY" not in legacy_env


def test_lumean_build_cmd_is_one_order_heygen_only_and_legacy_is_unchanged(srv, tmp_path):
    (tmp_path / "source.txt").write_text("Тестовый текст.", encoding="utf-8")
    template_id = "33333333-3333-4333-8333-333333333333"

    worker, argv = srv._build_cmd(
        "tts",
        str(tmp_path),
        {
            "engine": "lumean",
            "voice": template_id,
            "speed": "0.9",
            "use_cache": "1",
        },
    )
    assert worker == "tts_worker.py"
    assert argv[argv.index("--template_id") + 1] == template_id
    assert "--full_text" in argv
    assert argv[argv.index("--required_service") + 1] == "heygen"
    assert argv[argv.index("--audio_dir_name") + 1] == "_FINAL_AUDIO_LUMEAN"
    assert "--use_cache" in argv

    with pytest.raises(RuntimeError, match="Template ID"):
        srv._build_cmd("tts", str(tmp_path), {"engine": "lumean"})

    legacy_worker, legacy_argv = srv._build_cmd(
        "tts", str(tmp_path), {"engine": "tts", "voice": "legacy-template"},
    )
    assert legacy_worker == "tts_worker.py"
    assert "--full_text" not in legacy_argv
    assert "--required_service" not in legacy_argv
    assert "--audio_dir_name" not in legacy_argv


def test_header_shows_lumean_badge_only_for_a_server_credential(srv, monkeypatch):
    user = {"name": "tester"}

    monkeypatch.setattr(
        srv, "_provider_has_server_credential", lambda provider: provider == "lumean",
    )
    header = srv._header(user)
    assert "Lumean · HeyGen работает" in header
    assert "генерация разрешена только через HeyGen" in header

    monkeypatch.setattr(srv, "_provider_has_server_credential", lambda _provider: False)
    assert "Lumean · HeyGen работает" not in srv._header(user)


def test_lumean_normalization_selects_the_only_valid_uuid_and_rejects_conflicts(srv):
    from_voice = srv._normalize_lumean_settings(
        {"engine": " LUMEAN ", "voice": TEMPLATE_ID, "voice_uuid": "legacy-id"}
    )
    assert from_voice["engine"] == "lumean"
    assert from_voice["voice"] == TEMPLATE_ID
    assert from_voice["voice_uuid"] == TEMPLATE_ID

    from_uuid = srv._normalize_lumean_settings(
        {"engine": "lumean", "voice": "legacy-id", "voice_uuid": TEMPLATE_ID}
    )
    assert from_uuid["voice"] == TEMPLATE_ID
    assert from_uuid["voice_uuid"] == TEMPLATE_ID

    with pytest.raises(srv.LumeanTemplateError, match="разные UUID"):
        srv._normalize_lumean_settings(
            {"engine": "lumean", "voice": TEMPLATE_ID, "voice_uuid": OTHER_TEMPLATE_ID}
        )

    legacy = {
        "engine": "tts", "voice": "direct-eleven-id", "voice_uuid": "leave-me",
        "model_uuid": "legacy-model",
    }
    assert srv._normalize_lumean_settings(legacy) == legacy


def _insert_project(srv, pid, settings):
    now = time.time()
    c = srv.db()
    c.execute(
        "INSERT INTO projects(id,tenant_id,name,dir_path,status,created,updated,settings) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (pid, "tenant-test", pid, str(srv.TENANTS_DIR), "draft", now, now,
         json.dumps(settings, ensure_ascii=False)),
    )
    c.commit()
    c.close()


def _settings_row(srv, table, where, params):
    c = srv.db()
    row = c.execute(f"SELECT settings FROM {table} WHERE {where}", params).fetchone()
    c.close()
    return json.loads(row["settings"])


def test_project_snapshot_saves_active_form_and_updates_project_atomically(srv, monkeypatch):
    pid = "p_snapshot_lumean"
    _insert_project(
        srv,
        pid,
        {
            "engine": "voicegencore",
            "voice": "legacy-voice-id",
            "voice_uuid": "old-template-must-be-overridden",
            "voice_engine": "elevenLabsV3",
            "stability": "0.55",
            "thread_count": "4",
            "style": "keep-this-stage-setting",
            "deferred_pending": 99,
            "_runtime_marker": "keep-only-on-project",
        },
    )
    monkeypatch.setattr(
        srv, "require_web_user", lambda _request: {"id": 101, "tenant_id": "tenant-test"},
    )

    response = asyncio.run(
        srv.preset_save(
            FormRequest(
                {
                    "pid": pid,
                    "tab": "tts",
                    "preset_name": "snapshot from active tts form",
                    "setting__engine": "lumean",
                    "setting__voice": TEMPLATE_ID,
                    "setting__speed": "0.9",
                    "setting__use_cache": "1",
                    "setting__api_key": "must-not-be-stored",
                    "setting__provider": "lumean",
                    "setting__authorization": "Bearer must-not-be-stored",
                }
            )
        )
    )
    assert response.status_code == 302

    c = srv.db()
    preset = c.execute(
        "SELECT id,settings FROM presets WHERE tenant_id='tenant-test' AND name=?",
        ("snapshot from active tts form",),
    ).fetchone()
    project = c.execute("SELECT settings FROM projects WHERE id=?", (pid,)).fetchone()
    c.close()
    preset_settings = json.loads(preset["settings"])
    project_settings = json.loads(project["settings"])

    for values in (preset_settings, project_settings):
        assert values["engine"] == "lumean"
        assert values["voice"] == TEMPLATE_ID
        assert values["voice_uuid"] == TEMPLATE_ID
        assert values["speed"] == "0.9"
        assert values["style"] == "keep-this-stage-setting"
        assert "voice_engine" not in values
        assert "stability" not in values
        assert "thread_count" not in values
        assert "api_key" not in values
        assert "provider" not in values
        assert "authorization" not in values
    assert project_settings["_applied_preset"] == preset["id"]
    assert project_settings["deferred_pending"] == 99
    assert project_settings["_runtime_marker"] == "keep-only-on-project"
    assert "deferred_pending" not in preset_settings
    assert "_runtime_marker" not in preset_settings


def test_invalid_lumean_snapshot_rolls_back_preset_and_project(srv, monkeypatch):
    pid = "p_snapshot_invalid_lumean"
    original = {"engine": "edge", "voice": "ru-RU-DmitryNeural", "style": "unchanged"}
    _insert_project(srv, pid, original)
    monkeypatch.setattr(
        srv, "require_web_user", lambda _request: {"id": 101, "tenant_id": "tenant-test"},
    )
    c = srv.db()
    count_before = c.execute("SELECT COUNT(*) FROM presets").fetchone()[0]
    c.close()

    with pytest.raises(srv.HTTPException) as caught:
        asyncio.run(
            srv.preset_save(
                FormRequest(
                    {
                        "pid": pid,
                        "preset_name": "must rollback",
                        "setting__engine": "lumean",
                        "setting__voice": "LEGACY_NON_UUID_ID_A",
                    }
                )
            )
        )
    assert caught.value.status_code == 400
    assert "UUID Template ID" in caught.value.detail
    c = srv.db()
    assert c.execute("SELECT COUNT(*) FROM presets").fetchone()[0] == count_before
    after = json.loads(c.execute("SELECT settings FROM projects WHERE id=?", (pid,)).fetchone()[0])
    c.close()
    assert after == original


def test_apply_preset_is_fail_closed_and_does_not_keep_old_project_tts(srv, monkeypatch):
    pid = "p_apply_lumean"
    _insert_project(
        srv, pid,
        {"engine": "voicegencore", "voice": "stale", "deferred_pending": 12},
    )
    monkeypatch.setattr(
        srv, "require_web_user", lambda _request: {"id": 101, "tenant_id": "tenant-test"},
    )
    c = srv.db()
    cur = c.execute(
        "INSERT INTO presets(tenant_id,name,settings,created) VALUES(?,?,?,?)",
        (
            "tenant-test", "valid apply",
            json.dumps({"engine": "lumean", "voice": TEMPLATE_ID, "speed": "1.0"}),
            time.time(),
        ),
    )
    valid_id = cur.lastrowid
    cur = c.execute(
        "INSERT INTO presets(tenant_id,name,settings,created) VALUES(?,?,?,?)",
        (
            "tenant-test", "invalid apply",
            json.dumps({"engine": "lumean", "voice": "LEGACY_NON_UUID_ID_B"}),
            time.time(),
        ),
    )
    invalid_id = cur.lastrowid
    c.commit()
    c.close()

    response = asyncio.run(
        srv.preset_apply(valid_id, FormRequest({"pid": pid, "tab": "tts"}))
    )
    assert response.status_code == 302
    applied = _settings_row(srv, "projects", "id=?", (pid,))
    assert applied["engine"] == "lumean"
    assert applied["voice"] == TEMPLATE_ID
    assert applied["voice_uuid"] == TEMPLATE_ID
    assert applied["speed"] == "1.0"
    assert applied["model_id"] == ""
    assert applied["model_uuid"] == ""
    assert applied["_applied_preset"] == valid_id
    assert "deferred_pending" not in applied

    with pytest.raises(srv.HTTPException) as caught:
        asyncio.run(
            srv.preset_apply(invalid_id, FormRequest({"pid": pid, "tab": "tts"}))
        )
    assert caught.value.status_code == 400
    assert _settings_row(srv, "projects", "id=?", (pid,)) == applied


def test_project_js_captures_active_settings_and_filters_legacy_ids_for_lumean(srv):
    assert "function presetSnapshot(form)" in srv._PROJ_JS
    assert "new FormData(source)" in srv._PROJ_JS
    assert "data-project-settings" in srv._PROJ_JS
    assert "data-lumean" in srv._PROJ_JS
    assert "isLumeanTemplateId(cur)" in srv._PROJ_JS
    assert "e==='voicegencore'||e==='voicer'" in srv._PROJ_JS
    assert "data-preset-setting" in srv._PROJ_JS
