import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest
import requests
from pydantic import ValidationError

import cover_core
from api_cover import ApiCoverJobCreate
from api_cover_helpers import (
    ServerKeyConfigurationError,
    _reset_credential_pool_for_tests,
    acquire_server_keys_for_job,
    credential_pool_summary,
    ensure_server_keys_for_job,
    resolve_server_keys,
)
from cover_core import (
    _generate_via_veononstop,
    _provider_weight,
    generate_image_via_fast_gen,
)
from job_queue_cover import Job, JobQueue
from provider_policy import (
    InvalidGenerationProvider,
    canonical_generation_provider,
    effective_simple_mode_provider,
    provider_policy,
)


SECRET_FIELDS = ("openai_api_key", "fast_gen_api_key", "veo_api_key")


def _set_all_server_keys(monkeypatch):
    monkeypatch.setenv("COVER_OPENAI_KEY", "server-openai-test")
    monkeypatch.setenv("COVER_FAST_GEN_KEY", "server-fastgen-test")
    monkeypatch.setenv("COVER_VEO_KEY", "server-veo-test")


def test_job_dataclass_and_db_never_contain_vendor_credentials(tmp_path):
    assert all(name not in Job.__dataclass_fields__ for name in SECRET_FIELDS)
    queue = JobQueue(str(tmp_path / "jobs.db"))
    job_id = queue.add_job(
        Job(
            user_id="partner",
            mode="seed",
            preset_data=json.dumps({"generation_provider": "veo"}),
        )
    )
    raw = Path(tmp_path / "jobs.db").read_bytes()
    assert b"server-openai-test" not in raw
    assert b"server-fastgen-test" not in raw
    assert b"server-veo-test" not in raw
    stored = asdict(queue.get_job(job_id))
    assert all(name not in stored for name in SECRET_FIELDS)


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_web_model_rejects_nonempty_legacy_vendor_keys(field, monkeypatch, tmp_path):
    # Импорт внутри теста: cover_server создаёт queue при import, но не запускает worker.
    monkeypatch.setenv("COVER_SERVER_DIR", str(tmp_path))
    monkeypatch.setenv("COVER_DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("COVER_SESSION_DB", str(tmp_path / "sessions.db"))
    from cover_server import CoverJobRequest

    body = {
        "languages": ["Русский"],
        "mode": "url",
        "urls": ["https://youtu.be/test"],
        field: "do-not-accept",
    }
    with pytest.raises(ValidationError, match="Vendor API keys"):
        CoverJobRequest.model_validate(body)


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_public_api_model_rejects_nonempty_legacy_vendor_keys(field):
    body = {
        "preset_name": "Default RU",
        "languages": ["Русский"],
        "mode": "url",
        "urls": ["https://youtu.be/test"],
        field: "do-not-accept",
    }
    with pytest.raises(ValidationError, match="Vendor API keys"):
        ApiCoverJobCreate.model_validate(body)


def test_empty_legacy_fields_are_ignored_and_not_in_schema():
    body = ApiCoverJobCreate.model_validate({
        "preset_name": "Default RU",
        "languages": ["Русский"],
        "mode": "url",
        "urls": ["https://youtu.be/test"],
        "openai_api_key": "",
        "fast_gen_api_key": "",
        "max_threads": 20,
        "parallel": True,
    })
    dumped = body.model_dump()
    schema = ApiCoverJobCreate.model_json_schema()
    assert all(name not in dumped for name in SECRET_FIELDS)
    assert all(name not in schema.get("properties", {}) for name in SECRET_FIELDS)
    assert "max_threads" not in dumped
    assert "parallel" not in dumped


def test_server_key_resolution_is_provider_specific(monkeypatch):
    _set_all_server_keys(monkeypatch)
    assert resolve_server_keys("url", "flow") == (
        "server-openai-test", "server-fastgen-test"
    )
    assert resolve_server_keys("seed", "veo") == (
        "server-openai-test", "server-veo-test"
    )
    assert resolve_server_keys("seed", "gpt_image2") == (
        "server-openai-test", "server-openai-test"
    )


def test_missing_server_key_is_rejected_before_enqueue(monkeypatch):
    monkeypatch.delenv("COVER_OPENAI_KEY", raising=False)
    monkeypatch.delenv("COVER_FAST_GEN_KEY", raising=False)
    monkeypatch.delenv("COVER_VEO_KEY", raising=False)
    job = Job(
        user_id="partner",
        mode="seed",
        preset_data=json.dumps({"generation_provider": "veo"}),
        generation_provider="veo",
    )
    with pytest.raises(Exception) as exc:
        ensure_server_keys_for_job(job)
    assert getattr(exc.value, "status_code", None) == 503
    assert "COVER_VEO_KEY" in getattr(exc.value, "detail", "")


def test_worker_resolver_raises_non_http_configuration_error(monkeypatch):
    monkeypatch.delenv("COVER_OPENAI_KEY", raising=False)
    monkeypatch.delenv("COVER_FAST_GEN_KEY", raising=False)
    monkeypatch.delenv("COVER_VEO_KEY", raising=False)
    with pytest.raises(ServerKeyConfigurationError):
        resolve_server_keys("url", "flow")


def test_provider_policy_is_single_canonical_credential_dispatch_map():
    assert provider_policy("flow").credential_family == "fastgen"
    assert provider_policy("flow").dispatch == "fastgen"
    assert provider_policy("veo").credential_family == "veo"
    assert provider_policy("veo").dispatch == "veo"
    assert provider_policy("gpt_image2").credential_family == "openai"
    assert provider_policy("gpt_image2").dispatch == "gpt_image2"
    for bad in ("veo ", " veo", "VEO", "unknown"):
        with pytest.raises(InvalidGenerationProvider):
            canonical_generation_provider(bad)
    assert _provider_weight("openai_img") == 4
    assert _provider_weight("grok", "speed") == 1
    assert _provider_weight("grok", "quality") == 3


def test_unknown_or_whitespace_provider_is_rejected_before_dispatch(monkeypatch):
    _set_all_server_keys(monkeypatch)
    for bad in ("veo ", "unknown"):
        job = Job(
            user_id="partner",
            mode="seed",
            generation_provider=bad,
            preset_data=json.dumps({"generation_provider": bad}),
        )
        with pytest.raises(Exception) as exc:
            ensure_server_keys_for_job(job)
        assert getattr(exc.value, "status_code", None) == 422
        with pytest.raises(InvalidGenerationProvider):
            generate_image_via_fast_gen(
                "prompt", "must-not-be-sent", provider=bad
            )


def test_simple_mode_override_cannot_change_credential_family(monkeypatch):
    _set_all_server_keys(monkeypatch)
    assert effective_simple_mode_provider("flow", "flower") == "flower"
    with pytest.raises(InvalidGenerationProvider):
        effective_simple_mode_provider("flow", "veo")

    monkeypatch.setenv("SIMPLE_MODE_PROVIDER", "veo")
    job = Job(
        user_id="partner",
        mode="seed",
        generation_provider="flow",
        preset_data=json.dumps({
            "generation_provider": "flow",
            "simple_mode": True,
        }),
    )
    with pytest.raises(Exception) as exc:
        ensure_server_keys_for_job(job)
    assert getattr(exc.value, "status_code", None) == 422
    assert "cannot change credential family" in getattr(exc.value, "detail", "")


def test_provider_semaphore_wait_is_cancel_aware(monkeypatch):
    semaphore = threading.BoundedSemaphore(1)
    semaphore.acquire()
    monkeypatch.setattr("cover_core._VEO_SEMAPHORE", semaphore)
    started = time.monotonic()
    result = generate_image_via_fast_gen(
        "prompt",
        "server-veo",
        provider="veo",
        cancel_check=lambda: True,
    )
    assert result is None
    assert time.monotonic() - started < 1
    semaphore.release()


def test_veo_post_timeout_is_never_automatically_retried(monkeypatch):
    calls = []

    def timeout(*_args, **_kwargs):
        calls.append(1)
        raise requests.exceptions.Timeout()

    monkeypatch.setattr("cover_core.requests.post", timeout)
    result = _generate_via_veononstop(
        "test", "server-key", max_retries=20, log_fn=lambda _msg: None
    )
    assert result is None
    assert len(calls) == 1


def test_frontend_scrubs_legacy_local_storage_and_has_clean_payload():
    html = (Path(__file__).parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "localStorage.removeItem('cover_keys')" in html
    assert "localStorage.setItem('cover_keys'" not in html
    assert "id=\"openai-key\"" not in html
    assert "id=\"fast-gen-key\"" not in html
    assert "id=\"veo-key\"" not in html
    assert "openai_api_key:" not in html
    assert "fast_gen_api_key:" not in html
    assert "veo_api_key:" not in html
    assert html.index("localStorage.removeItem('cover_keys')") < html.index(
        "localStorage.getItem('cover_session_id')"
    )


def test_public_job_serializer_is_allowlist(monkeypatch, tmp_path):
    _set_all_server_keys(monkeypatch)
    monkeypatch.setenv("COVER_SERVER_DIR", str(tmp_path))
    monkeypatch.setenv("COVER_DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("COVER_SESSION_DB", str(tmp_path / "sessions.db"))
    from cover_server import _job_public_dict

    public = _job_public_dict(Job(id="x", user_id="private-user", output_dir="/secret/path"))
    assert set(public) == {
        "id", "status", "created_at", "started_at", "finished_at",
        "mode", "languages", "n_variants",
        "progress_percent", "progress_detail", "log", "error",
        "results_count", "output_files", "fallback_files",
    }
    assert "user_id" not in public
    assert "output_dir" not in public
    assert all(name not in public for name in SECRET_FIELDS)


def test_plural_key_pool_dedupes_and_least_loaded_rr(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "COVER_FAST_GEN_KEYS",
        '["fast-a", "fast-b", "fast-a"]',
    )
    # Scalar compatibility may coexist; identical value is not extra capacity.
    monkeypatch.setenv("COVER_FAST_GEN_KEY", "fast-a")
    monkeypatch.delenv("COVER_OPENAI_KEYS", raising=False)
    _reset_credential_pool_for_tests()
    job = Job(
        mode="seed",
        generation_provider="flow",
        preset_data=json.dumps({"generation_provider": "flow"}),
    )

    first = acquire_server_keys_for_job(job)
    second = acquire_server_keys_for_job(job)
    assert [first.generation_key, second.generation_key] == [
        "fast-a",
        "fast-b",
    ]
    first.release()
    third = acquire_server_keys_for_job(job)
    assert third.generation_key == "fast-a"
    second.release()
    third.release()
    third.release()  # idempotent

    summary = credential_pool_summary()
    assert summary["fastgen"]["unique_keys"] == 2
    assert "fast-a" not in json.dumps(summary)
    assert "fast-b" not in json.dumps(summary)
    queue = JobQueue(str(tmp_path / "pool-jobs.db"))
    queue.add_job(job)
    raw = (tmp_path / "pool-jobs.db").read_bytes()
    assert b"fast-a" not in raw
    assert b"fast-b" not in raw


def test_plural_lists_accept_commas_newlines_and_balance_families(monkeypatch):
    monkeypatch.setenv("COVER_OPENAI_KEYS", "open-a,\nopen-b")
    monkeypatch.setenv("COVER_OPENAI_KEY", "open-a")
    monkeypatch.setenv("COVER_VEO_KEYS", "veo-a\nveo-b\nveo-a")
    monkeypatch.setenv("COVER_VEO_KEY", "veo-a")
    _reset_credential_pool_for_tests()
    job = Job(
        mode="url",
        generation_provider="veo",
        preset_data=json.dumps({"generation_provider": "veo"}),
    )

    first = acquire_server_keys_for_job(job)
    second = acquire_server_keys_for_job(job)
    assert (first.openai_key, first.generation_key) == ("open-a", "veo-a")
    assert (second.openai_key, second.generation_key) == ("open-b", "veo-b")
    first.release()
    second.release()
    assert credential_pool_summary()["openai"]["unique_keys"] == 2
    assert credential_pool_summary()["veo"]["unique_keys"] == 2


def test_single_scalar_key_lease_is_backward_compatible(monkeypatch):
    _set_all_server_keys(monkeypatch)
    for plural in (
        "COVER_OPENAI_KEYS",
        "COVER_FAST_GEN_KEYS",
        "COVER_VEO_KEYS",
    ):
        monkeypatch.delenv(plural, raising=False)
    _reset_credential_pool_for_tests()
    job = Job(
        mode="url",
        generation_provider="flow",
        preset_data=json.dumps({"generation_provider": "flow"}),
    )
    lease = acquire_server_keys_for_job(job)
    assert (lease.openai_key, lease.generation_key) == (
        "server-openai-test",
        "server-fastgen-test",
    )
    lease.release()
    assert credential_pool_summary()["fastgen"]["unique_keys"] == 1


def test_provider_request_budgets_are_per_unique_key(monkeypatch):
    with cover_core._key_budgets_lock:
        cover_core._key_budgets.clear()
    monkeypatch.setattr(cover_core, "_fetch_key_capacity", lambda _key: 8)
    first_budget = cover_core._key_budget("fast-key-a")
    duplicate_budget = cover_core._key_budget("fast-key-a")
    second_budget = cover_core._key_budget("fast-key-b")
    assert first_budget is duplicate_budget
    assert first_budget is not second_budget
    assert first_budget.capacity == second_budget.capacity == 8

    first_veo = cover_core._provider_semaphore_for_key(
        "veo", "unique-veo-a"
    )
    duplicate_veo = cover_core._provider_semaphore_for_key(
        "veo", "unique-veo-a"
    )
    second_veo = cover_core._provider_semaphore_for_key(
        "veo", "unique-veo-b"
    )
    assert first_veo is duplicate_veo
    assert first_veo is not second_veo


def test_job_caps_scale_with_unique_pool_slots_only(monkeypatch):
    from cover_server import _server_provider_job_limits

    monkeypatch.setenv("COVER_JOB_SLOTS", "10")
    monkeypatch.setenv("COVER_VEO_INFLIGHT", "4")
    monkeypatch.setenv("COVER_GPT_IMAGE2_INFLIGHT", "2")
    monkeypatch.setenv("COVER_VEO_KEYS", '["veo-a", "veo-b", "veo-a"]')
    monkeypatch.setenv("COVER_VEO_KEY", "veo-a")
    monkeypatch.setenv("COVER_OPENAI_KEYS", "open-a,open-b,open-a")
    monkeypatch.setenv("COVER_OPENAI_KEY", "open-a")
    for name in (
        "COVER_FASTGEN_JOB_SLOTS",
        "COVER_VEO_JOB_SLOTS",
        "COVER_GPT_IMAGE2_JOB_SLOTS",
        "COVER_XAI_JOB_SLOTS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert _server_provider_job_limits() == {
        "fastgen": 10,
        "veo": 8,
        "gpt_image2": 4,
        # Прямой xAI: своя группа слотов, чужие пулы не трогает.
        "xai": 2,
    }


def test_key_budget_shrink_releases_original_reserved_units(monkeypatch):
    capacities = {"value": 8}
    monkeypatch.setattr(
        cover_core,
        "_fetch_key_capacity",
        lambda _key: capacities["value"],
    )
    with cover_core._key_budgets_lock:
        cover_core._key_budgets.clear()

    budget = cover_core._key_budget("shrink-key")
    reserved = budget.acquire(8)
    assert reserved == 8
    assert budget.used == 8

    capacities["value"] = 2
    assert cover_core._key_budget("shrink-key") is budget
    assert budget.capacity == 2
    assert budget.used == 8

    acquired_after_shrink = []
    waiter = threading.Thread(
        target=lambda: acquired_after_shrink.append(budget.acquire(8))
    )
    waiter.start()
    time.sleep(0.05)
    assert waiter.is_alive()
    assert acquired_after_shrink == []

    budget.release(reserved)
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert acquired_after_shrink == [2]
    assert budget.used == 2
    budget.release(acquired_after_shrink[0])
    assert budget.used == 0


def test_key_budget_growth_does_not_release_new_capacity_amount(monkeypatch):
    capacities = {"value": 2}
    monkeypatch.setattr(
        cover_core,
        "_fetch_key_capacity",
        lambda _key: capacities["value"],
    )
    with cover_core._key_budgets_lock:
        cover_core._key_budgets.clear()

    budget = cover_core._key_budget("grow-key")
    reserved = budget.acquire(8)
    assert reserved == 2
    assert budget.used == 2

    capacities["value"] = 8
    assert cover_core._key_budget("grow-key") is budget
    assert budget.capacity == 8
    reserved_from_new_capacity = budget.acquire(6)
    assert reserved_from_new_capacity == 6
    assert budget.used == 8

    budget.release(reserved)
    assert budget.used == 6
    budget.release(reserved_from_new_capacity)
    assert budget.used == 0

    reserved_after_growth = budget.acquire(8)
    assert reserved_after_growth == 8
    assert budget.used == 8
    budget.release(reserved_after_growth)
    assert budget.used == 0
