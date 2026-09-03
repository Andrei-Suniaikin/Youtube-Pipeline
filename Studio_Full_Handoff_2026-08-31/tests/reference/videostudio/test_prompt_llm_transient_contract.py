import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import prompt_llm as pll


def _reset_channel_state():
    pll._dead.clear()
    pll._dead_reason.clear()
    pll._strikes.clear()
    pll._announced.clear()


def test_locked_codex_timeout_never_becomes_fake_401(monkeypatch):
    _reset_channel_state()
    calls = []
    monkeypatch.setenv("VS_PROMPT_PROVIDER", "codex")
    monkeypatch.setenv("VS_PROMPT_FALLBACK", "0")
    monkeypatch.setattr(pll, "_channel_ready", lambda *_args, **_kwargs: True)

    def timeout(*_args, **_kwargs):
        calls.append(1)
        return False, "CODEX_TIMEOUT: нет данных 60с"

    monkeypatch.setattr(pll, "call_codex_subscription", timeout)
    payload = {"messages": [{"role": "user", "content": "scene"}]}

    statuses = [pll.post(payload).status_code for _ in range(3)]

    assert statuses == [502, 502, 502]
    assert len(calls) == 3


def test_locked_auth_failure_stays_terminal(monkeypatch):
    _reset_channel_state()
    monkeypatch.setenv("VS_PROMPT_PROVIDER", "codex")
    monkeypatch.setenv("VS_PROMPT_FALLBACK", "0")
    monkeypatch.setattr(pll, "_channel_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        pll, "call_codex_subscription",
        lambda *_args, **_kwargs: (False, "CODEX_AUTH: unauthorized"),
    )

    response = pll.post({"messages": [{"role": "user", "content": "scene"}]})

    assert response.status_code == 401
