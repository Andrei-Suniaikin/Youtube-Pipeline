import json
import stat
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import prompt_llm as pll


def _response_format(marker="schema-only-marker"):
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "scene_result",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "scene": {
                        "type": "string",
                        "description": marker,
                    }
                },
                "required": ["scene"],
                "additionalProperties": False,
            },
        },
    }


def _prepare_fake_local_codex(monkeypatch):
    monkeypatch.setenv("VS_CODEX_BIN", "/usr/bin/true")
    monkeypatch.delenv("VS_CODEX_HOME", raising=False)
    monkeypatch.delenv("REWRITE_CODEX_HOME", raising=False)
    monkeypatch.delenv("VS_SERVER_CREDENTIAL_ISOLATION", raising=False)


def test_codex_command_adds_schema_only_when_requested(tmp_path):
    output_path = str(tmp_path / "final.json")
    schema_path = str(tmp_path / "schema.json")

    structured = pll._codex_text_only_command(
        "/usr/bin/codex",
        str(tmp_path),
        "gpt-test",
        output_path,
        schema_path,
    )
    plain = pll._codex_text_only_command(
        "/usr/bin/codex", str(tmp_path), "gpt-test", output_path
    )

    assert structured[structured.index("--output-schema") + 1] == schema_path
    assert structured[structured.index("-o") + 1] == output_path
    assert structured[-1] == "-"
    assert "--output-schema" not in plain


def test_codex_native_schema_file_is_private_exact_and_ephemeral(
    monkeypatch, tmp_path
):
    _prepare_fake_local_codex(monkeypatch)
    response_format = _response_format()
    expected_schema = response_format["json_schema"]["schema"]
    captured = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            captured["command"] = list(command)
            captured["stderr"] = kwargs["stderr"]

        def communicate(self, prompt, timeout=None):
            captured["prompt"] = prompt
            schema_path = Path(
                captured["command"][
                    captured["command"].index("--output-schema") + 1
                ]
            )
            captured["schema_path"] = schema_path
            captured["schema"] = json.loads(
                schema_path.read_text(encoding="utf-8")
            )
            captured["schema_mode"] = stat.S_IMODE(schema_path.stat().st_mode)
            output_path = Path(
                captured["command"][captured["command"].index("-o") + 1]
            )
            output_path.write_text('{"scene":"ok"}', encoding="utf-8")
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(pll.subprocess, "Popen", FakeProcess)
    ok, output = pll.call_codex_subscription(
        [{"role": "user", "content": "make one scene"}],
        model="gpt-test",
        timeout=30,
        response_format=response_format,
    )

    assert (ok, output) == (True, '{"scene":"ok"}')
    assert captured["schema"] == expected_schema
    assert captured["schema_mode"] == 0o600
    assert "schema-only-marker" not in captured["prompt"]
    assert not captured["schema_path"].exists()
    assert not captured["schema_path"].parent.exists()


def test_codex_schema_path_and_contents_do_not_escape_on_cli_error(
    monkeypatch, tmp_path
):
    _prepare_fake_local_codex(monkeypatch)
    response_format = _response_format("never-echo-this-schema-marker")
    captured = {}

    class FakeProcess:
        returncode = 1

        def __init__(self, command, **kwargs):
            captured["command"] = list(command)
            captured["stderr"] = kwargs["stderr"]

        def communicate(self, prompt, timeout=None):
            schema_path = Path(
                captured["command"][
                    captured["command"].index("--output-schema") + 1
                ]
            )
            captured["schema_path"] = schema_path
            assert "never-echo-this-schema-marker" not in prompt
            captured["stderr"].write(
                "provider rejected schema file " + str(schema_path)
            )
            captured["stderr"].flush()
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(pll.subprocess, "Popen", FakeProcess)
    ok, error = pll.call_codex_subscription(
        [{"role": "user", "content": "make one scene"}],
        model="gpt-test",
        timeout=30,
        response_format=response_format,
    )

    assert ok is False
    assert error.startswith("CODEX_ERROR:")
    assert "never-echo-this-schema-marker" not in error
    assert str(captured["schema_path"]) not in error
    assert "[REDACTED]" in error
    assert not captured["schema_path"].exists()


@pytest.mark.parametrize(
    "stderr_text",
    [
        "user script mentions 401 BCE\nERROR: output schema rejected by CLI",
        "ERROR: output schema not supported for this model",
    ],
)
def test_codex_schema_error_is_not_misclassified_as_auth(
    monkeypatch, stderr_text
):
    _prepare_fake_local_codex(monkeypatch)

    class FakeProcess:
        returncode = 1

        def __init__(self, _command, **kwargs):
            self.stderr = kwargs["stderr"]

        def communicate(self, _prompt, timeout=None):
            self.stderr.write(stderr_text)
            self.stderr.flush()
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(pll.subprocess, "Popen", FakeProcess)
    ok, error = pll.call_codex_subscription(
        [{"role": "user", "content": "historical scene"}],
        model="gpt-test",
        timeout=30,
        response_format=_response_format(),
    )

    assert ok is False
    assert error.startswith("CODEX_ERROR:")
    assert "CODEX_AUTH" not in error

    response = pll._call_channel(
        pll.PROVIDER_CODEX,
        [{"role": "user", "content": "historical scene"}],
        _response_format(),
        30,
        None,
    )
    assert response.status_code == 502


def test_codex_schema_write_failure_removes_partial_file(tmp_path):
    response_format = _response_format()
    response_format["json_schema"]["schema"]["bad"] = object()

    with pytest.raises(TypeError, match="not JSON serializable"):
        pll._write_codex_output_schema(str(tmp_path), response_format)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "response_format",
    [
        {"type": "json_schema"},
        {"type": "json_schema", "json_schema": {}},
        {"type": "json_schema", "json_schema": {"schema": "not-an-object"}},
    ],
)
def test_codex_malformed_schema_fails_before_starting_cli(
    monkeypatch, response_format
):
    _prepare_fake_local_codex(monkeypatch)
    monkeypatch.setattr(
        pll.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Codex CLI must not start"),
    )

    ok, error = pll.call_codex_subscription(
        [{"role": "user", "content": "make one scene"}],
        model="gpt-test",
        timeout=30,
        response_format=response_format,
    )

    assert ok is False
    assert error.startswith("CODEX_ERROR: ValueError:")
    assert "not-an-object" not in error


def test_channel_routing_uses_native_schema_only_for_codex(monkeypatch):
    response_format = _response_format()
    original_messages = [{"role": "user", "content": "make one scene"}]
    captured = {}

    def fake_codex(messages, model, timeout=None, response_format=None):
        captured["codex_messages"] = messages
        captured["codex_format"] = response_format
        return True, '{"scene":"codex"}'

    def fake_claude(messages, model, timeout=None):
        captured["claude_messages"] = messages
        return True, '{"scene":"claude"}'

    monkeypatch.setattr(pll, "call_codex_subscription", fake_codex)
    monkeypatch.setattr(pll, "call_claude_subscription", fake_claude)

    codex_response = pll._call_channel(
        pll.PROVIDER_CODEX,
        original_messages,
        response_format,
        30,
        None,
    )
    claude_response = pll._call_channel(
        pll.PROVIDER_CLAUDE,
        original_messages,
        response_format,
        30,
        None,
    )

    assert codex_response.status_code == 200
    assert claude_response.status_code == 200
    assert captured["codex_format"] is response_format
    assert captured["codex_messages"] == original_messages
    assert "schema-only-marker" not in json.dumps(
        captured["codex_messages"]
    )
    assert "schema-only-marker" in json.dumps(
        captured["claude_messages"]
    )
    assert original_messages == [
        {"role": "user", "content": "make one scene"}
    ]


def test_codex_json_object_keeps_legacy_text_directive(monkeypatch):
    captured = {}

    def fake_codex(messages, model, timeout=None, response_format=None):
        captured["messages"] = messages
        captured["format"] = response_format
        return True, '{"ok":true}'

    monkeypatch.setattr(pll, "call_codex_subscription", fake_codex)
    response_format = {"type": "json_object"}

    response = pll._call_channel(
        pll.PROVIDER_CODEX,
        [{"role": "user", "content": "return status"}],
        response_format,
        30,
        None,
    )

    assert response.status_code == 200
    assert captured["format"] is response_format
    assert "OUTPUT FORMAT (strict)" in json.dumps(captured["messages"])
