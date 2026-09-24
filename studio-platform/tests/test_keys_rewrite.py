#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Своя подписка Codex/Claude в «Моих ключах» — контракт пульта (задача A, 22.09.2026).

Рерайт (`rewrite/server.py`) давно умеет хранить и использовать личную подписку Codex
(`codex_auth`) и личную подписку Claude (`oauth_token`), но пульт их не пробрасывал —
пользователь физически не мог ими воспользоваться иначе как SSH-доступом к серверу
рерайта. Здесь проверяется только серверная половина (A1): `GET /ui/keys` (через
`_rewrite_keys`) и `POST /ui/keys/rewrite`. Ни сети, ни стенда — `_rw` подменён.

  • `_rewrite_keys` зеркалит `own_codex_auth_set`/`codex_key_source` рерайта;
  • `POST /ui/keys/rewrite` — partial update: отсутствующее поле не трогает сохранённое
    значение, присутствующее (даже пустой строкой) — сохраняет/снимает;
  • `oauth_token` нормализуется и проверяется `normalize_claude_token` ДО обращения к
    рерайту — ключ API Anthropic и мусор отклоняются без единого сетевого вызова;
  • секрет никогда не всплывает в ответе пульта.

    .venv/bin/python tests/test_keys_rewrite.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
for p in ("web", "server"):
    sys.path.insert(0, os.path.join(BASE, p))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeReq:
    """Заменяет `Request`: обоим ручкам нужен только `request.state.sid`."""

    def __init__(self, sid="sid-1"):
        self.state = type("St", (), {"sid": sid})()


class FakeRW:
    """Подменяет `W._rw` — эмулирует `/settings/keys` рерайта без сети.

    `calls` копит каждый вызов (путь, sid, тело) — тесты проверяют, что и сколько раз
    ушло. `fail` — одноразовая подмена ответа на отказ (код >= 400).
    """

    def __init__(self):
        self.calls = []
        self.state = {
            "own_openai_key_set": False,
            "own_claude_token_set": False,
            "own_codex_auth_set": False,
            "openai_configured": True,
            "claude_configured": False,
            "openai_key_source": "server",
            "claude_key_source": "none",
            "codex_key_source": "none",
        }
        self.fail = None

    def __call__(self, path, sid, payload=None, **kw):
        self.calls.append((path, sid, payload))
        if self.fail is not None:
            code, body = self.fail
            self.fail = None
            return code, body
        if payload is not None:
            if "openai_api_key" in payload:
                self.state["own_openai_key_set"] = bool(payload["openai_api_key"].strip())
                self.state["openai_key_source"] = (
                    "user" if self.state["own_openai_key_set"] else "server")
            if "codex_auth" in payload:
                self.state["own_codex_auth_set"] = bool(payload["codex_auth"].strip())
                self.state["codex_key_source"] = (
                    "user" if self.state["own_codex_auth_set"] else "none")
            if "oauth_token" in payload:
                self.state["own_claude_token_set"] = bool(payload["oauth_token"].strip())
                self.state["claude_key_source"] = (
                    "user" if self.state["own_claude_token_set"] else "none")
            return 200, {"ok": True}
        return 200, dict(self.state)


def main() -> int:
    import app as W                                                # noqa: E402
    from fastapi import HTTPException

    SECRET_CODEX = '{"auth_mode":"chatgpt","tokens":{"access_token":"a","refresh_token":"r"}}'
    SECRET_CLAUDE_API = "sk-ant-api03-" + "x" * 40
    SECRET_CLAUDE_OAT = "sk-ant-oat01-" + "y" * 40
    ALL_SECRETS = (SECRET_CODEX, SECRET_CLAUDE_API, SECRET_CLAUDE_OAT, "sk-x")

    print("_rewrite_keys: зеркалит own_codex/codex_source")
    fake = FakeRW()
    W._rw = fake
    fake.state["own_codex_auth_set"] = True
    fake.state["codex_key_source"] = "user"
    got = W._rewrite_keys("sid-1")
    check("own_codex прочитан", got["own_codex"], True)
    check("codex_source прочитан", got["codex_source"], "user")
    check("own_openai/own_claude не задеты", (got["own_openai"], got["own_claude"]),
          (False, False))
    check("один вызов на чтение", len(fake.calls), 1)
    check("чтение без тела", fake.calls[0][2], None)

    print("ui_keys_rewrite: только codex_auth — тело содержит ровно одно поле")
    fake = FakeRW()
    W._rw = fake
    out = W.ui_keys_rewrite(W.RewriteKeyIn(codex_auth=SECRET_CODEX), FakeReq())
    check("ответ ok", out["ok"], True)
    check("set только codex", out["set"], {"codex": True})
    check("один вызов _rw", len(fake.calls), 1)
    check("тело содержит только codex_auth", fake.calls[0][2], {"codex_auth": SECRET_CODEX})

    print("ui_keys_rewrite: oauth_token = ключ API Anthropic — отклонён без обращения к _rw")
    fake = FakeRW()
    W._rw = fake
    try:
        W.ui_keys_rewrite(W.RewriteKeyIn(oauth_token=SECRET_CLAUDE_API), FakeReq())
        check("ключ API Anthropic отклонён", "прошло без отказа", "HTTPException 422")
    except HTTPException as exc:
        check("код 422", exc.status_code, 422)
        check("текст про ключ API Anthropic", "ключ API Anthropic" in str(exc.detail), True)
        check("поданное значение не утекло в текст ошибки (422 Anthropic)",
              SECRET_CLAUDE_API in str(exc.detail), False)
    check("_rw не вызван вовсе", fake.calls, [])

    print("ui_keys_rewrite: мусор без sk-ant-oat — тоже отклонён")
    fake = FakeRW()
    W._rw = fake
    try:
        W.ui_keys_rewrite(W.RewriteKeyIn(oauth_token="просто мусор"), FakeReq())
        check("мусор отклонён", "прошло без отказа", "HTTPException 422")
    except HTTPException as exc:
        check("код 422 (мусор)", exc.status_code, 422)
        check("текст «не похоже на токен подписки»",
              "не похоже на токен подписки" in str(exc.detail), True)
        check("поданное значение не утекло в текст ошибки (мусор)",
              "просто мусор" in str(exc.detail), False)
    check("_rw не вызван вовсе (мусор)", fake.calls, [])

    print("ui_keys_rewrite: правильный префикс, но короче 40 симв. — тоже отклонён (§7)")
    fake = FakeRW()
    W._rw = fake
    short_token = "sk-ant-oat01-abc"  # похож на настоящий, но короче 40 символов
    try:
        W.ui_keys_rewrite(W.RewriteKeyIn(oauth_token=short_token), FakeReq())
        check("короткий токен отклонён", "прошло без отказа", "HTTPException 422")
    except HTTPException as exc:
        check("код 422 (короткий токен)", exc.status_code, 422)
        check("текст «не похоже на токен подписки» (короткий)",
              "не похоже на токен подписки" in str(exc.detail), True)
        check("поданное значение не утекло в текст ошибки (короткий токен)",
              short_token in str(exc.detail), False)
    check("_rw не вызван вовсе (короткий токен)", fake.calls, [])

    print("ui_keys_rewrite: codex_auth пересылается как есть — пульт не валидирует JSON/схему (п.4)")
    fake = FakeRW()
    W._rw = fake
    garbage_codex = "это не json и не auth_mode вообще"
    out = W.ui_keys_rewrite(W.RewriteKeyIn(codex_auth=garbage_codex), FakeReq())
    check("мусорный codex_auth дошёл до _rw как есть (пульт не режет его сам)",
          fake.calls[0][2], {"codex_auth": garbage_codex})
    check("set по мусорному codex_auth — set=true (пульт не судит содержимое, только пустоту)",
          out["set"], {"codex": True})

    print("ui_keys_rewrite: валидный oauth_token — нормализуется (Bearer/пробелы/переносы срезаны)")
    fake = FakeRW()
    W._rw = fake
    raw = f"  Bearer {SECRET_CLAUDE_OAT}\n"
    out = W.ui_keys_rewrite(W.RewriteKeyIn(oauth_token=raw), FakeReq())
    check("set claude=true", out["set"], {"claude": True})
    check("тело нормализовано", fake.calls[0][2], {"oauth_token": SECRET_CLAUDE_OAT})

    print("ui_keys_rewrite: пустой oauth_token — снимает свою подписку Claude, set=false")
    fake = FakeRW()
    W._rw = fake
    out = W.ui_keys_rewrite(W.RewriteKeyIn(oauth_token=""), FakeReq())
    check("пустая строка не идёт в normalize_claude_token (проходит как сигнал «снять»)",
          out["set"], {"claude": False})
    check("тело содержит пустой oauth_token как есть", fake.calls[0][2], {"oauth_token": ""})

    print("ui_keys_rewrite: два поля в одном запросе — один вызов, ответ по обоим")
    fake = FakeRW()
    W._rw = fake
    out = W.ui_keys_rewrite(
        W.RewriteKeyIn(codex_auth="", openai_api_key="sk-x"), FakeReq())
    check("один вызов _rw (два поля)", len(fake.calls), 1)
    check("тело содержит оба поля, не третье",
          fake.calls[0][2], {"codex_auth": "", "openai_api_key": "sk-x"})
    check("set по обоим полям", out["set"], {"codex": False, "openai": True})

    print("ui_keys_rewrite: пустое тело — ничего не меняется, _rw не вызван")
    fake = FakeRW()
    W._rw = fake
    out = W.ui_keys_rewrite(W.RewriteKeyIn(), FakeReq())
    check("ok, пустой set", out, {"ok": True, "set": {}})
    check("_rw не вызван (пустое тело)", fake.calls, [])

    print("ui_keys_rewrite: отказ _rw пробрасывается тем же кодом и текстом")
    fake = FakeRW()
    W._rw = fake
    fake.fail = (422, "codex_auth must use ChatGPT subscription login")
    try:
        W.ui_keys_rewrite(W.RewriteKeyIn(codex_auth=SECRET_CODEX), FakeReq())
        check("отказ рерайта пробрасывается", "прошло без отказа", "HTTPException 422")
    except HTTPException as exc:
        check("код 422 от рерайта", exc.status_code, 422)
        check("текст от рерайта как есть", exc.detail,
              "codex_auth must use ChatGPT subscription login")
        check("поданное значение не утекло в текст ошибки (422 от рерайта)",
              SECRET_CODEX in str(exc.detail), False)

    fake = FakeRW()
    W._rw = fake
    fake.fail = (502, "рерайт не отвечает: TimeoutError")
    try:
        W.ui_keys_rewrite(W.RewriteKeyIn(oauth_token=SECRET_CLAUDE_OAT), FakeReq())
        check("недоступность рерайта пробрасывается", "прошло без отказа", "HTTPException 502")
    except HTTPException as exc:
        check("код 502 от рерайта", exc.status_code, 502)
        check("текст 502 как есть", exc.detail, "рерайт не отвечает: TimeoutError")
        check("поданное значение не утекло в текст ошибки (502 от рерайта)",
              SECRET_CLAUDE_OAT in str(exc.detail), False)

    print("Секрет не встречается ни в одном ответе пульта")
    fake = FakeRW()
    W._rw = fake
    fake.state["own_codex_auth_set"] = True
    fake.state["own_claude_token_set"] = True
    responses = [
        W._rewrite_keys("sid-1"),
        W.ui_keys_rewrite(W.RewriteKeyIn(codex_auth=SECRET_CODEX), FakeReq()),
        W.ui_keys_rewrite(W.RewriteKeyIn(oauth_token=SECRET_CLAUDE_OAT), FakeReq()),
        W.ui_keys_rewrite(W.RewriteKeyIn(), FakeReq()),
    ]
    dump = json.dumps(responses, ensure_ascii=False, default=str)
    for secret in ALL_SECRETS:
        check(f"секрет не в ответах: {secret[:16]}…", secret in dump, False)

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} проверок не прошли: {FAILS}")
        return 1
    print("OK: все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
