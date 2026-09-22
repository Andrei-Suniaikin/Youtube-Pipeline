import hashlib
import json
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tts_worker as worker  # noqa: E402


class _Response:
    def __init__(self, status=200, payload=None, content=b"", reason="", headers=None):
        self.status_code = status
        self._payload = payload
        self._content = content
        self.reason = reason
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise worker.requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self._content


@pytest.fixture(autouse=True)
def _isolated_api_key(monkeypatch):
    monkeypatch.delenv("LUMEAN_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "")
    monkeypatch.setattr(worker, "ELEVENLABS_API_KEY", "")


def test_create_task_uses_lumean_order_contract(monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return _Response(payload={"success": True, "data": {"id": "order-1", "status": "created"}})

    monkeypatch.setattr(worker, "BASE_URL", "https://api.lumean.app/api/public")
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker.requests, "request", fake_request)

    assert worker.create_task("Тест", "template-1", "eleven_multilingual_v2") == "order-1"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.lumean.app/api/public/orders"
    assert seen["kwargs"]["headers"]["X-API-KEY"] == "test-key"
    assert seen["kwargs"]["json"] == {
        "template_id": "template-1",
        "input_text": "Тест",
        "config_override": {
            "output_settings": {"audio_format": "mp3"},
            "tts_settings": {"model_id": "eleven_multilingual_v2"},
        },
    }


def test_create_task_drops_legacy_model_uuid(monkeypatch):
    seen = {}

    def fake_request(_method, _url, **kwargs):
        seen.update(kwargs["json"])
        return _Response(payload={"success": True, "data": {"id": "order-2"}})

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker.requests, "request", fake_request)

    worker.create_task("Text", "template-2", "deadbeef-old-model-uuid")
    assert seen == {
        "template_id": "template-2",
        "input_text": "Text",
        "config_override": {"output_settings": {"audio_format": "mp3"}},
    }


def test_poll_and_download_completed_order_atomically(monkeypatch, tmp_path):
    calls = []
    speed_calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/orders/order-3"):
            return _Response(payload={
                "success": True,
                "data": {"id": "order-3", "status": "completed", "result": {"files": ["result/audio.mp3"]}},
            })
        if url.endswith("/storage/url"):
            return _Response(payload={"success": True, "data": {"url": "https://signed.example/audio"}})
        raise AssertionError(url)

    def fake_get(url, **_kwargs):
        assert url == "https://signed.example/audio"
        return _Response(content=b"ID3audio")

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker.requests, "request", fake_request)
    monkeypatch.setattr(worker.requests, "get", fake_get)
    def fake_speed(_path, _speed, strict=False):
        speed_calls.append(strict)
        return True

    monkeypatch.setattr(worker, "apply_audio_speed", fake_speed)

    order = worker.wait_for_audio("order-3")
    output = tmp_path / "1.mp3"
    worker.get_audio_result("order-3", str(output), order=order)

    assert output.read_bytes() == b"ID3audio"
    assert not (tmp_path / "1.part.mp3").exists()
    assert calls[-1][2] == {"path": "result/audio.mp3", "download": True}
    assert speed_calls == [True]


def test_process_paragraph_never_recreates_paid_order_on_download_retry(monkeypatch, tmp_path):
    created = []
    downloads = []

    def fake_create(*_args, **_kwargs):
        created.append("order-4")
        return "order-4"

    def fake_download(_task_id, output_path, **_kwargs):
        downloads.append(1)
        if len(downloads) == 1:
            raise worker.requests.ConnectionError("temporary")
        Path(output_path).write_bytes(b"ID3audio")

    monkeypatch.setattr(worker, "create_task", fake_create)
    monkeypatch.setattr(worker, "wait_for_audio", lambda _task_id: {"status": "completed", "result": {"files": ["x"]}})
    monkeypatch.setattr(worker, "get_audio_result", fake_download)
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)

    output = tmp_path / "1.mp3"
    assert worker.process_paragraph((1, "Text", str(output), "template-4", None, False)) is True
    assert created == ["order-4"]
    assert len(downloads) == 2
    assert output.read_bytes() == b"ID3audio"
    receipt = json.loads((tmp_path / "1.mp3.lumean.json").read_text(encoding="utf-8"))
    assert receipt["order_id"] == "order-4"


@pytest.mark.parametrize("use_cache", [True, False])
def test_receipt_always_resumes_without_new_paid_post(monkeypatch, tmp_path, use_cache):
    output = tmp_path / "1.mp3"
    identity = worker._receipt_identity("Text", "template-5", None)
    worker._save_receipt(str(output), identity, "order-5")

    monkeypatch.setattr(worker, "create_task", lambda *_a, **_k: pytest.fail("must not create a second paid order"))
    monkeypatch.setattr(worker, "wait_for_audio", lambda order_id: {"id": order_id, "status": "completed"})
    def fake_download(order_id, output_path, **_kwargs):
        assert order_id == "order-5"
        Path(output_path).write_bytes(b"ID3cached")

    monkeypatch.setattr(worker, "get_audio_result", fake_download)

    assert worker.process_paragraph((1, "Text", str(output), "template-5", None, use_cache)) is True
    assert output.read_bytes() == b"ID3cached"


def test_partial_order_retries_failed_items_then_completes(monkeypatch):
    calls = []
    states = iter([
        {
            "id": "order-6",
            "status": "partially_completed",
            "items": [{"id": "item-1", "status": "failed"}],
        },
        {
            "id": "order-6",
            "status": "partially_completed",
            "items": [
                {"id": "item-1", "status": "failed"},
                {"id": "item-retry-1", "status": "pending"},
            ],
        },
        {
            "id": "order-6",
            "status": "partially_completed",
            "items": [
                {"id": "item-1", "status": "failed"},
                {"id": "item-retry-1", "status": "processing"},
            ],
        },
        {
            "id": "order-6",
            "status": "completed",
            "result": {"files": ["result/audio.mp3"]},
        },
    ])

    def fake_request(method, url, **_kwargs):
        calls.append((method, url))
        if url.endswith("/items/retry-failed"):
            return _Response(payload={
                "success": True,
                "data": {
                    "queued_count": 1,
                    "queued": [{"item_id": "item-1", "chunk_index": 0}],
                    "skipped_count": 0,
                    "skipped": [],
                },
            })
        return _Response(payload={"success": True, "data": next(states)})

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker.requests, "request", fake_request)
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)

    result = worker.wait_for_audio("order-6")
    assert result["status"] == "completed"
    assert calls.count(("POST", f"{worker.BASE_URL}/orders/order-6/items/retry-failed")) == 1


def test_poll_retries_429_using_retry_after(monkeypatch):
    responses = iter([
        _Response(
            status=429,
            payload={"success": False, "message": "quota", "reason": "token_quota_exceeded"},
            headers={"Retry-After": "7"},
        ),
        _Response(payload={
            "success": True,
            "data": {"id": "order-7", "status": "completed", "result": {"files": ["x.mp3"]}},
        }),
    ])
    sleeps = []

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker.requests, "request", lambda *_a, **_k: next(responses))
    monkeypatch.setattr(worker, "_sleep_with_heartbeat", lambda seconds, _task_id: sleeps.append(seconds))

    assert worker.wait_for_audio("order-7")["status"] == "completed"
    assert sleeps == [7.0]


def test_create_retries_explicit_429_without_capping_retry_after(monkeypatch):
    responses = iter([
        _Response(
            status=429,
            payload={"success": False, "message": "quota", "reason": "token_quota_exceeded"},
            headers={"Retry-After": "601"},
        ),
        _Response(payload={"success": True, "data": {"id": "order-created"}}),
    ])
    sleeps = []

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker.requests, "request", lambda *_a, **_k: next(responses))
    monkeypatch.setattr(worker, "_sleep_with_heartbeat", lambda seconds, task_id: sleeps.append((seconds, task_id)))

    assert worker.create_task("Text", "template-7") == "order-created"
    assert sleeps == [(601.0, "новый")]


def test_ambiguous_create_is_fail_closed_across_runs(monkeypatch, tmp_path):
    output = tmp_path / "1.mp3"
    calls = []

    def ambiguous_create(*_args, **_kwargs):
        calls.append(1)
        raise worker.requests.Timeout("response lost")

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker, "create_task", ambiguous_create)

    paragraph = (1, "Text", str(output), "template-8", None, False)
    assert worker.process_paragraph(paragraph) is False
    assert worker.process_paragraph(paragraph) is False
    assert calls == [1]
    receipt = json.loads((tmp_path / "1.mp3.lumean.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "creating"
    assert receipt["order_id"] == ""


def test_key_rotation_is_fail_closed_for_paid_receipt(monkeypatch, tmp_path):
    output = tmp_path / "1.mp3"
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "old-key")
    identity = worker._receipt_identity("Text", "template-9", None)
    worker._save_receipt(str(output), identity, "already-paid-order")
    monkeypatch.setenv("LUMEAN_API_KEY", "rotated-key")
    monkeypatch.setattr(worker, "create_task", lambda *_a, **_k: pytest.fail("must not double-charge"))

    assert worker.process_paragraph((1, "Text", str(output), "template-9", None, False)) is False


def test_manual_item_recovery_blocks_new_order_after_text_edit(monkeypatch, tmp_path):
    output = tmp_path / "1.mp3"
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    old_identity = worker._receipt_identity("Flagged text", "template-10", None)
    worker._save_receipt(
        str(output), old_identity, "paid-policy-order",
        state="manual_recovery", status="policy_flagged",
    )
    monkeypatch.setattr(worker, "create_task", lambda *_a, **_k: pytest.fail("must use free item retry"))

    assert worker.process_paragraph(
        (1, "Edited safe text", str(output), "template-10", None, False)
    ) is False


def test_expired_files_mark_receipt_terminal_for_safe_fresh_run(monkeypatch, tmp_path):
    output = tmp_path / "1.mp3"
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    identity = worker._receipt_identity("Text", "template-11", None)
    worker._save_receipt(str(output), identity, "expired-order")
    monkeypatch.setattr(worker, "wait_for_audio", lambda _order_id: {
        "id": "expired-order",
        "status": "completed",
        "files_expired": True,
        "result": {"files": ["gone.mp3"]},
    })

    assert worker.process_paragraph((1, "Text", str(output), "template-11", None, False)) is False
    receipt = json.loads((tmp_path / "1.mp3.lumean.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "terminal"
    assert receipt["status"] == "files_expired"


def test_key_is_read_dynamically_from_environment(monkeypatch):
    monkeypatch.setenv("LUMEAN_API_KEY", "rotated-key")
    assert worker.get_headers()["X-API-KEY"] == "rotated-key"


def test_missing_key_fails_loudly(monkeypatch):
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "")
    monkeypatch.setattr(worker, "ELEVENLABS_API_KEY", "")
    with pytest.raises(ValueError):
        worker.get_headers()


def test_required_service_accepts_heygen_and_rejects_direct_elevenlabs(monkeypatch):
    monkeypatch.setattr(
        worker,
        "_request_json_resilient",
        lambda *_a, **_k: {"service_key": "heygen", "service": {"code": "heygen"}},
    )
    assert worker.require_template_service("template-heygen", "heygen") == "heygen"

    monkeypatch.setattr(
        worker,
        "_request_json_resilient",
        lambda *_a, **_k: {"service_key": "elevenlabs", "service": {"code": "elevenlabs"}},
    )
    with pytest.raises(ValueError, match="разрешает только service=heygen"):
        worker.require_template_service("template-eleven", "heygen")


def test_required_service_needs_an_explicit_template():
    with pytest.raises(ValueError, match="template_id обязателен"):
        worker.require_template_service("", "heygen")


def test_full_text_cli_submits_one_joined_order(monkeypatch, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Первый абзац.\n\nВторой абзац.\n", encoding="utf-8")
    project = tmp_path / "project"
    seen = []

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker, "process_paragraph", lambda row: seen.append(row) or True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tts_worker.py", "--source_file", str(source), "--project_dir", str(project),
            "--template_id", "template-full", "--speed", "0.9", "--full_text", "--use_cache",
        ],
    )

    worker.main()

    assert len(seen) == 1
    idx, text, output, template_id, model_id, use_cache, identity_scope = seen[0]
    assert idx == 1
    assert text == "Первый абзац.\nВторой абзац."
    assert output == str(project / worker.AUDIO_DIR_NAME / "1.mp3")
    assert template_id == "template-full"
    assert model_id is None
    assert use_cache is True
    assert identity_scope == {"mode": "full_text_v1", "speed": "0.9"}
    assert worker.TTS_SPEED == 0.9


@pytest.mark.parametrize(
    "artifact_name",
    ["2.mp3", "2.mp3.lumean.json", "1.part.mp3", "2.part.mp3"],
)
def test_full_text_cli_refuses_mixed_paragraph_cache(monkeypatch, tmp_path, artifact_name):
    source = tmp_path / "source.txt"
    source.write_text("Текст.", encoding="utf-8")
    project = tmp_path / "project"
    audio = project / worker.AUDIO_DIR_NAME
    audio.mkdir(parents=True)
    (audio / artifact_name).write_bytes(b"paid-audio")

    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tts_worker.py", "--source_file", str(source), "--project_dir", str(project),
            "--template_id", "template-full", "--full_text",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        worker.main()

    assert exc.value.code == 1
    assert (audio / artifact_name).read_bytes() == b"paid-audio"


def test_project_marker_enables_one_bound_full_text_order(monkeypatch, tmp_path):
    project = tmp_path / "data" / "tenants" / "3" / "projects" / "p_exact"
    project.mkdir(parents=True)
    source = project / "source.txt"
    source.write_text("Первый абзац.\n\nВторой абзац.\n", encoding="utf-8")
    normalized = "Первый абзац.\nВторой абзац."
    (project / worker.FULL_TEXT_MARKER_NAME).write_text(
        json.dumps({
            "schema_version": 1,
            "mode": "lumean_full_text",
            "tenant_id": "3",
            "project_id": project.name,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "submission_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "template_id": "template-bound",
            "model_id": "",
            "speed": "0.9",
        }),
        encoding="utf-8",
    )
    seen = []
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker, "process_paragraph", lambda row: seen.append(row) or True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tts_worker.py", "--source_file", str(source), "--project_dir", str(project),
            "--template_id", "template-bound", "--speed", "0.9", "--use_cache",
        ],
    )

    worker.main()

    assert len(seen) == 1
    assert seen[0][1] == normalized
    assert seen[0][2] == str(project / worker.AUDIO_DIR_NAME / "1.mp3")


def test_project_marker_stale_source_fails_before_paid_order(monkeypatch, tmp_path):
    project = tmp_path / "data" / "tenants" / "3" / "projects" / "p_exact"
    project.mkdir(parents=True)
    source = project / "source.txt"
    source.write_text("Актуальный текст.", encoding="utf-8")
    (project / worker.FULL_TEXT_MARKER_NAME).write_text(
        json.dumps({
            "schema_version": 1,
            "mode": "lumean_full_text",
            "tenant_id": "3",
            "project_id": project.name,
            "source_sha256": "0" * 64,
            "submission_sha256": hashlib.sha256("Актуальный текст.".encode("utf-8")).hexdigest(),
            "template_id": "template-bound",
            "model_id": "",
            "speed": "1",
        }),
        encoding="utf-8",
    )
    seen = []
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    monkeypatch.setattr(worker, "process_paragraph", lambda row: seen.append(row) or True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tts_worker.py", "--source_file", str(source), "--project_dir", str(project),
            "--template_id", "template-bound",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        worker.main()

    assert exc.value.code == 1
    assert seen == []


def test_full_text_receipt_identity_is_speed_scoped():
    slow = worker._receipt_identity(
        "Text", "template-bound", None, {"mode": "full_text_v1", "speed": "0.9"},
    )
    normal = worker._receipt_identity(
        "Text", "template-bound", None, {"mode": "full_text_v1", "speed": "1"},
    )
    assert slow != normal


def test_full_text_orphan_receipt_with_foreign_identity_fails_closed(monkeypatch, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Точный текст.", encoding="utf-8")
    project = tmp_path / "project"
    audio = project / worker.AUDIO_DIR_NAME
    audio.mkdir(parents=True)
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    worker._save_receipt(
        str(audio / "1.mp3"),
        worker._receipt_identity(
            "Другой текст.", "template-full", None,
            {"mode": "full_text_v1", "speed": "0.9"},
        ),
        "order-foreign",
    )
    monkeypatch.setattr(worker, "process_paragraph", lambda _row: pytest.fail("paid path must not run"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tts_worker.py", "--source_file", str(source), "--project_dir", str(project),
            "--template_id", "template-full", "--speed", "0.9", "--full_text",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        worker.main()

    assert exc.value.code == 1
    receipt = json.loads((audio / "1.mp3.lumean.json").read_text(encoding="utf-8"))
    assert receipt["order_id"] == "order-foreign"


def test_full_text_exact_orphan_receipt_resumes_same_paid_order(monkeypatch, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Точный текст.", encoding="utf-8")
    project = tmp_path / "project"
    audio = project / worker.AUDIO_DIR_NAME
    audio.mkdir(parents=True)
    output = audio / "1.mp3"
    scope = {"mode": "full_text_v1", "speed": "0.9"}
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    worker._save_receipt(
        str(output),
        worker._receipt_identity("Точный текст.", "template-full", None, scope),
        "order-existing",
    )
    monkeypatch.setattr(worker, "create_task", lambda *_a, **_k: pytest.fail("must not create twice"))
    monkeypatch.setattr(
        worker,
        "wait_for_audio",
        lambda order_id: {"id": order_id, "status": "completed"},
    )

    def fake_download(order_id, output_path, **_kwargs):
        assert order_id == "order-existing"
        Path(output_path).write_bytes(b"ID3exact")

    monkeypatch.setattr(worker, "get_audio_result", fake_download)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tts_worker.py", "--source_file", str(source), "--project_dir", str(project),
            "--template_id", "template-full", "--speed", "0.9", "--full_text",
        ],
    )

    worker.main()

    assert output.read_bytes() == b"ID3exact"


def _terminal_receipt_run(monkeypatch, tmp_path, receipt_status, live_order):
    source = tmp_path / "source.txt"
    source.write_text("Точный текст.", encoding="utf-8")
    project = tmp_path / "project"
    audio = project / worker.AUDIO_DIR_NAME
    audio.mkdir(parents=True)
    scope = {"mode": "full_text_v1", "speed": "0.9"}
    monkeypatch.setattr(worker, "LUMEAN_API_KEY", "test-key")
    worker._save_receipt(
        str(audio / "1.mp3"),
        worker._receipt_identity("Точный текст.", "template-full", None, scope),
        "order-closed", state="terminal", status=receipt_status,
    )
    polled = []

    def fake_request(method, path, **kwargs):
        polled.append((method, path))
        return live_order

    monkeypatch.setattr(worker, "_request_json", fake_request)
    seen = []
    monkeypatch.setattr(worker, "process_paragraph", lambda row: seen.append(row) or True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tts_worker.py", "--source_file", str(source), "--project_dir", str(project),
            "--template_id", "template-full", "--speed", "0.9", "--full_text",
        ],
    )
    return polled, seen


def test_full_text_refunded_order_allows_a_new_paid_order(monkeypatch, tmp_path):
    """Lumean вернул оплату (compensated, списано 0) — озвучка проекта не должна быть заперта навсегда."""
    polled, seen = _terminal_receipt_run(
        monkeypatch, tmp_path, "compensated",
        {"status": "compensated", "total_tokens": 0, "tokens_spent": None},
    )

    worker.main()

    assert polled == [("GET", "/orders/order-closed")], "возврат подтверждается живым запросом"
    assert len(seen) == 1 and seen[0][1] == "Точный текст."


@pytest.mark.parametrize(
    "receipt_status, live_order",
    [
        ("compensated", {"status": "completed", "total_tokens": 53}),   # на диске возврат, у Lumean — оплаченный успех
        ("compensated", {"status": "compensated", "total_tokens": 120}),  # возврат частичный, токены списаны
        ("failed", {"status": "failed", "total_tokens": 0}),             # закрыт не возвратом — только ручной разбор
    ],
)
def test_full_text_other_closed_receipts_stay_fail_closed(monkeypatch, tmp_path, receipt_status, live_order):
    polled, seen = _terminal_receipt_run(monkeypatch, tmp_path, receipt_status, live_order)

    with pytest.raises(SystemExit) as exc:
        worker.main()

    assert exc.value.code == 1
    assert seen == []
    receipt = json.loads((tmp_path / "project" / worker.AUDIO_DIR_NAME / "1.mp3.lumean.json").read_text(encoding="utf-8"))
    assert receipt["order_id"] == "order-closed" and receipt["state"] == "terminal"
