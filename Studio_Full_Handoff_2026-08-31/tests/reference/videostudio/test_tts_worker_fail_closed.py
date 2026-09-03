"""Focused offline regression tests for VoiceGenCore and Voicer workers."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import voicegencore_worker as vgc  # noqa: E402
import voicer_worker as voicer  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


class _Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise voicer.requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self
            )


def _argv(monkeypatch, worker_name, source, project, *extra):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            worker_name,
            "--source_file",
            str(source),
            "--project_dir",
            str(project),
            *extra,
        ],
    )


def test_vgc_one_absolute_deadline_cancels_instead_of_waiting_eight_times(monkeypatch, tmp_path):
    clock = _Clock()
    creates = []
    cancels = []
    messages = []

    monkeypatch.setattr(vgc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(vgc.time, "sleep", clock.sleep)
    monkeypatch.setattr(vgc, "MAX_WAIT_TIME_MINUTES", 0.1)  # six seconds total
    monkeypatch.setattr(vgc, "POLL_INTERVAL", 1)
    monkeypatch.setattr(vgc, "create_task", lambda *_a, **_k: creates.append("task-1") or "task-1")
    monkeypatch.setattr(vgc, "check_task_status", lambda *_a: ("queued", 0, {"status": "queued"}))
    monkeypatch.setattr(vgc, "cancel_task", lambda _token, task_id: cancels.append(task_id) or True)
    monkeypatch.setattr(vgc, "print_user", lambda message, *_a, **_k: messages.append(message))
    monkeypatch.setattr(vgc, "print_error_user_friendly", lambda message, *_a, **_k: messages.append(message))

    result = vgc.process_text(
        "token", "text", "voice", "elevenLabsV3", 0.5,
        str(tmp_path), "1.mp3", 1, idx=1,
    )

    assert result is False
    assert creates == ["task-1"]
    assert cancels == ["task-1"]
    assert clock.now == pytest.approx(6.0)
    assert sum("Озвучка: queued" in message for message in messages) == 1
    assert any("не завершилась за 0.1 мин" in message for message in messages)


def test_vgc_heartbeat_emits_only_on_state_change_before_30_seconds(monkeypatch, tmp_path):
    clock = _Clock()
    states = iter([
        ("queued", 0, {}),
        ("queued", 0, {}),
        ("processing", 0, {}),
        ("processing", 20, {}),
        ("done", 100, {}),
    ])
    messages = []

    monkeypatch.setattr(vgc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(vgc.time, "sleep", clock.sleep)
    monkeypatch.setattr(vgc, "POLL_INTERVAL", 1)
    monkeypatch.setattr(vgc, "create_task", lambda *_a, **_k: "task-state")
    monkeypatch.setattr(vgc, "check_task_status", lambda *_a: next(states))
    monkeypatch.setattr(vgc, "download_audio", lambda *_a, **_k: None)
    monkeypatch.setattr(vgc, "print_user", lambda message, *_a, **_k: messages.append(message))

    assert vgc.process_text(
        "token", "text", "voice", "elevenLabsV3", 0.5,
        str(tmp_path), "1.mp3", 1, idx=1,
    ) is True
    heartbeats = [message for message in messages if message.startswith("Озвучка:")]
    assert heartbeats == [
        "Озвучка: queued (0%) (1с)",
        "Озвучка: processing (0%) (3с)",
        "Озвучка: processing (20%) (4с)",
    ]


def test_vgc_transient_poll_retry_reuses_task_id_and_deadline(monkeypatch, tmp_path):
    clock = _Clock()
    creates = []
    checks = []
    downloads = []

    monkeypatch.setattr(vgc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(vgc.time, "sleep", clock.sleep)
    monkeypatch.setattr(vgc, "POLL_INTERVAL", 1)
    monkeypatch.setattr(vgc, "RETRY_DELAY_SECONDS", 1)
    monkeypatch.setattr(vgc, "create_task", lambda *_a, **_k: creates.append("same-task") or "same-task")

    def fake_status(_token, task_id):
        checks.append(task_id)
        if len(checks) == 1:
            raise vgc.requests.exceptions.ConnectionError("temporary")
        return "done", 100, {}

    monkeypatch.setattr(vgc, "check_task_status", fake_status)
    monkeypatch.setattr(vgc, "download_audio", lambda _token, task_id, _path: downloads.append(task_id))

    assert vgc.process_text(
        "token", "text", "voice", "elevenLabsV3", 0.5,
        str(tmp_path), "1.mp3", 1, idx=1,
    ) is True
    assert creates == ["same-task"]
    assert checks == ["same-task", "same-task"]
    assert downloads == ["same-task"]


def test_vgc_free_plan_and_thread_limit_are_explicit():
    free = {"has_active_subscription": False, "free_remaining": 100, "max_threads": 1}
    assert vgc._vgc_access_error(free, 99) is None
    assert "недостаточно" in vgc._vgc_access_error(free, 101)
    assert vgc._clamp_thread_count(10, free) == (1, 1)
    assert vgc._clamp_thread_count(10, {"data": free}) == (1, 1)
    assert "ни одного потока" in vgc._vgc_access_error(
        {"has_active_subscription": True, "max_threads": 0}, 1
    )
    assert vgc._vgc_access_error(
        {"has_active_subscription": True, "free_remaining": 0, "max_threads": 10},
        1_000_000,
    ) is None


def test_vgc_main_clamps_threads_and_requires_real_output(monkeypatch, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("test paragraph", encoding="utf-8")
    project = tmp_path / "project"
    seen = {}
    messages = []
    _argv(
        monkeypatch,
        "voicegencore_worker.py",
        source,
        project,
        "--voice_id",
        "voice-id",
        "--thread_count",
        "10",
    )
    monkeypatch.setenv("VGC_API_TOKEN", "token")
    monkeypatch.setattr(
        vgc,
        "check_auth",
        lambda _token: {"has_active_subscription": True, "max_threads": 1},
    )

    def fake_process(_token, _text, _voice, _engine, _stability, output_dir, filename, threads, idx=0):
        del idx
        seen["threads"] = threads
        Path(output_dir, filename).write_bytes(b"ID3")
        return True

    monkeypatch.setattr(vgc, "process_text", fake_process)
    monkeypatch.setattr(vgc, "print_user", lambda message, *_a, **_k: messages.append(message))

    assert vgc.main() == 0
    assert seen["threads"] == 1
    assert any("запрошено потоков 10, доступно 1" in message for message in messages)

    Path(project, vgc.AUDIO_DIR_NAME, "1.mp3").unlink()
    monkeypatch.setattr(vgc, "process_text", lambda *_a, **_k: True)
    assert vgc.main() == 1


@pytest.mark.parametrize(
    ("script", "token_name"),
    [
        ("voicegencore_worker.py", "VGC_API_TOKEN"),
        ("voicer_worker.py", "VOICER_API_TOKEN"),
    ],
)
def test_worker_process_exits_nonzero_without_required_token(tmp_path, script, token_name):
    source = tmp_path / "source.txt"
    source.write_text("text", encoding="utf-8")
    env = os.environ.copy()
    env.pop(token_name, None)
    result = subprocess.run(
        [
            sys.executable,
            str(SRC / script),
            "--source_file",
            str(source),
            "--project_dir",
            str(tmp_path / "project"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1, result.stdout


def test_voicer_status_network_retry_and_download_retry_keep_one_task_id(monkeypatch, tmp_path):
    creates = []
    status_calls = []
    downloads = []
    monkeypatch.setenv("VOICER_API_TOKEN", "token")

    monkeypatch.setattr(
        voicer,
        "create_task",
        lambda *_a, **_k: creates.append("task-v") or ("task-v", 1),
    )

    def fake_status_get(_url, **_kwargs):
        status_calls.append(1)
        if len(status_calls) == 1:
            raise voicer.requests.exceptions.ConnectionError("temporary status outage")
        return _Response(payload={"status": "completed"})

    def fake_download(task_id, output_path):
        downloads.append(task_id)
        if len(downloads) == 1:
            raise voicer.requests.exceptions.ConnectionError("temporary download outage")
        Path(output_path).write_bytes(b"ID3")

    monkeypatch.setattr(voicer.requests, "get", fake_status_get)
    monkeypatch.setattr(voicer, "download_audio", fake_download)
    monkeypatch.setattr(voicer.time, "sleep", lambda _seconds: None)

    output = tmp_path / "1.mp3"
    data = (1, "text", str(output), None, None, None, False, 0.5, 1)
    assert voicer.process_paragraph(data) is True
    assert creates == ["task-v"]
    assert downloads == ["task-v", "task-v"]
    assert output.read_bytes() == b"ID3"


def test_voicer_status_404_is_immediate_and_does_not_recreate(monkeypatch, tmp_path):
    creates = []
    status_calls = []
    monkeypatch.setenv("VOICER_API_TOKEN", "token")
    monkeypatch.setattr(
        voicer,
        "create_task",
        lambda *_a, **_k: creates.append("missing-task") or ("missing-task", 1),
    )

    def missing_status(_url, **_kwargs):
        status_calls.append(1)
        return _Response(status=404, text="not found")

    monkeypatch.setattr(voicer.requests, "get", missing_status)
    monkeypatch.setattr(voicer.time, "sleep", lambda _seconds: None)
    data = (1, "text", str(tmp_path / "1.mp3"), None, None, None, False, 0.5, 1)

    assert voicer.process_paragraph(data) is False
    assert creates == ["missing-task"]
    assert len(status_calls) == 1


def test_voicer_main_cache_success_and_missing_output_fail_closed(monkeypatch, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("paragraph", encoding="utf-8")
    project = tmp_path / "project"
    output = project / voicer.AUDIO_DIR_NAME / "1.mp3"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"ID3")
    monkeypatch.setenv("VOICER_API_TOKEN", "token")
    _argv(monkeypatch, "voicer_worker.py", source, project, "--use_cache")

    assert voicer.main() == 0

    output.unlink()
    monkeypatch.setattr(voicer, "process_paragraph", lambda _data: True)
    _argv(monkeypatch, "voicer_worker.py", source, project)
    assert voicer.main() == 1
