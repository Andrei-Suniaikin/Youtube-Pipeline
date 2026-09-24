#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подписки для текста: добавить Codex или Claude без SSH — и без утечки секрета.

Что проверяется:
  • добавленная подписка сразу попадает в раздачу рерайта (без перезапуска);
  • секрет лежит 0600 в каталоге 0700 и не появляется ни в одном ответе;
  • вход протух дважды подряд — подписка вынута из раздачи; одиночный отказ — нет;
  • «Заменить» оставляет тот же каталог Codex (путь в пуле не меняется);
  • дубль, ключ API вместо подписки, чужой формат — отказ с понятной причиной;
  • менять серверные подписки может только администратор.

    .venv/bin/python tests/test_text_subscriptions.py
"""
from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="text-subs-")
os.environ["REWRITE_SUBSCRIPTIONS_DIR"] = os.path.join(TMP, "subs")
# Рерайт не должен увидеть ни ENV-подписок разработчика, ни его ~/.codex.
for k in ("REWRITE_CODEX_HOMES", "REWRITE_CODEX_HOME", "REWRITE_CLAUDE_OAUTH_TOKENS",
          "CLAUDE_CODE_OAUTH_TOKENS", "CLAUDE_CODE_OAUTH_TOKEN"):
    os.environ.pop(k, None)
os.environ["HOME"] = TMP
for p in ("rewrite", "shared", "server", "web"):
    sys.path.insert(0, os.path.join(BASE, p))

import subscriptions as S                                         # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, expect):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ: {expect}")
    except S.SubscriptionError as exc:
        check(label, expect in str(exc), True)


def jwt(claims):
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{part}.s"


def codex_auth(account="acc-1234", email="andrew@example.com", refresh="rt-SECRET-1"):
    return json.dumps({"auth_mode": "chatgpt", "tokens": {
        "access_token": "at-SECRET-" + account, "refresh_token": refresh,
        "id_token": jwt({"email": email}), "account_id": account}})


CLAUDE = "sk-ant-oat01-" + "Q" * 60 + "WXYZ"
CLAUDE2 = "sk-ant-oat01-" + "R" * 60 + "ABCD"


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def main() -> int:
    import rewrite_core as core

    print("Добавить и сразу раздавать")
    check("пусто — раздавать нечего", core.get_server_codex_homes(), [])
    a = S.add("codex", codex_auth(), "Основная", "boss")
    check("имя сохранилось", a["label"], "Основная")
    check("хвост — маска почты, не секрет", a["tail"], "an…@example.com")
    check("новая ещё не проверена", a["state"], "new")
    homes = core.get_server_codex_homes()
    check("рерайт видит её без перезапуска", len(homes), 1)
    auth = os.path.join(homes[0], "auth.json")
    check("auth.json закрыт: 0600", mode(auth), 0o600)
    check("каталог подписки закрыт: 0700", mode(homes[0]), 0o700)
    check("корень закрыт: 0700", mode(S.root()), 0o700)
    check("пул рерайта получил её",
          core.get_server_credential_pool("codex").stats()["credentials"], 1)

    b = S.add("claude", "Bearer " + CLAUDE)
    check("имя по умолчанию", b["label"], "Claude 1")
    check("хвост токена — четыре знака", b["tail"], "…WXYZ")
    check("Claude тоже в раздаче", core.get_server_claude_oauth_tokens(), [CLAUDE])
    check("токен закрыт: 0600",
          mode(os.path.join(S.root(), "claude", b["id"] + ".token")), 0o600)

    print("Секрет наружу не уходит")
    listing = json.dumps(S.listing())
    for secret in ("SECRET", CLAUDE[:30], "WXYZ" * 0 + CLAUDE, "andrew@", homes[0]):
        check(f"в списке нет «{secret[:14]}…»", secret in listing, False)
    index = open(os.path.join(S.root(), "index.json"), encoding="utf-8").read()
    check("в index.json нет токенов", "SECRET" in index or CLAUDE in index, False)

    print("Отказы с понятной причиной")
    refused("та же подписка второй раз",
            lambda: S.add("codex", codex_auth(refresh="rt-other")), "уже добавлена как «Основная»")
    refused("тот же токен Claude", lambda: S.add("claude", CLAUDE), "уже добавлена")
    refused("ключ API вместо подписки",
            lambda: S.add("claude", "sk-ant-api03-" + "x" * 50), "ключ API")
    refused("вход по ключу API у Codex",
            lambda: S.add("codex", json.dumps({"auth_mode": "apikey", "tokens": {}})),
            "auth_mode=chatgpt")
    refused("не JSON", lambda: S.add("codex", "codex login"), "это не JSON")
    refused("без refresh_token",
            lambda: S.add("codex", json.dumps({"auth_mode": "chatgpt",
                                               "tokens": {"access_token": "x"}})),
            "tokens.refresh_token")
    refused("непонятный провайдер", lambda: S.add("gemini", "x"), "Codex или Claude")

    print("Проверка — та же, что у рерайта")
    bad = [None, [], {"auth_mode": "apikey"}, {"auth_mode": "chatgpt"},
           {"auth_mode": "chatgpt", "tokens": {"access_token": "a", "refresh_token": " "}},
           {"auth_mode": "chatgpt", "tokens": {"access_token": "a", "refresh_token": "r"}}]
    for doc in bad:
        mine = theirs = "ok"
        try:
            S.validate_codex_auth(doc)
        except ValueError:
            mine = "refused"
        try:
            core.validate_codex_auth_document(doc)
        except ValueError:
            theirs = "refused"
        check(f"согласны про {str(doc)[:40]}", mine, theirs)

    print("Состояние по факту вызова")
    home = homes[0]
    core._note_codex_result(home, True, "текст")
    check("успех — работает", S.listing()[0]["state"], "ok")
    core._note_codex_result(home, False, "CODEX_LIMIT: usage limit reached")
    check("лимит Codex без выдуманного срока", (S.listing()[0]["state"],
                                                 S.listing()[0]["until"]), ("limit", None))
    core._note_codex_result(home, False, "CODEX_AUTH: 401 unauthorized")
    check("один отказ входа — ещё не протух", S.listing()[0]["state"], "limit")
    check("…и из раздачи не вынута", core.get_server_codex_homes(), [home])
    core._note_codex_result(home, False, "CODEX_AUTH: 401 unauthorized")
    check("второй подряд — вход протух", S.listing()[0]["state"], "dead")
    check("протухшая вынута из раздачи", core.get_server_codex_homes(), [])
    check("причина видна человеку", "401" in S.listing()[0]["note"], True)
    core._note_codex_result("/чужой/каталог", False, "CODEX_AUTH: x")
    check("чужой каталог не трогает наши", len(S.listing()), 2)

    print("Заменить вход")
    r = S.replace(a["id"], codex_auth(email="new@example.com", refresh="rt-new"))
    check("после замены — снова в раздаче", core.get_server_codex_homes(), [home])
    check("тот же каталог — путь в пуле не меняется", os.path.isfile(auth), True)
    check("новый вход на месте",
          json.load(open(auth))["tokens"]["refresh_token"], "rt-new")
    check("состояние заново", (r["state"], r["fails"], r["note"]), ("new", 0, ""))
    check("имя осталось", r["label"], "Основная")

    print("Claude: лимит со сроком, протух, вернуть")
    core._note_subscription("claude", CLAUDE, "limit", until=1.0)
    check("срок сброса прошёл — снова рабочая", S.listing()[1]["state"], "new")
    core._note_subscription("claude", CLAUDE, "auth", detail="dead " + CLAUDE)
    core._note_subscription("claude", CLAUDE, "auth", detail="dead " + CLAUDE)
    item = S.listing()[1]
    check("Claude протух", item["state"], "dead")
    check("в причине нет токена", CLAUDE in item["note"], False)
    check("вынут из раздачи", core.get_server_claude_oauth_tokens(), [])
    S.set_paused(b["id"], False)
    check("«вернуть в работу» снимает «протух»", S.listing()[1]["state"], "new")
    check("и возвращает в раздачу", core.get_server_claude_oauth_tokens(), [CLAUDE])
    S.set_paused(b["id"], True)
    check("на паузе — не раздаётся", core.get_server_claude_oauth_tokens(), [])
    check("пауза видна", S.listing()[1]["state"], "paused")

    print("Удалить")
    S.remove(a["id"])
    check("каталог входа стёрт", os.path.exists(home), False)
    check("из раздачи ушла", core.get_server_codex_homes(), [])
    S.remove(b["id"])
    check("файл токена стёрт",
          os.path.exists(os.path.join(S.root(), "claude", b["id"] + ".token")), False)
    try:
        S.remove(b["id"])
        check("повторное удаление", "прошло", "KeyError")
    except KeyError:
        check("повторное удаление — «нет такой»", True, True)

    print("Пульт: только администратор, секрет не возвращается")
    import app as W
    from fastapi import HTTPException

    class Req:
        def __init__(self, role):
            class U(dict):
                def keys(self):
                    return super().keys()
            self.state = type("St", (), {"user": U(id=1, name="boss", role=role)})()

    for fn, args in ((W.ui_subscriptions, ()),
                     (W.ui_subscription_add, (W.SubscriptionIn(provider="claude",
                                                                secret=CLAUDE2),)),
                     (W.ui_subscription_remove, ("abcdef123456",))):
        try:
            fn(*args, Req("user")) if args else fn(Req("user"))
            check(f"{fn.__name__} без прав", "прошло", 403)
        except HTTPException as exc:
            check(f"{fn.__name__} без прав — 403", exc.status_code, 403)
    out = W.ui_subscription_add(W.SubscriptionIn(provider="claude", secret=CLAUDE2,
                                                 label="Резерв"), Req("admin"))
    check("админ добавил", out["item"]["label"], "Резерв")
    check("в ответе нет токена", CLAUDE2 in json.dumps(out), False)
    got = W.ui_subscriptions(Req("admin"))
    check("в списке нет токена", CLAUDE2 in json.dumps(got, default=str), False)
    try:
        W.ui_subscription_add(W.SubscriptionIn(provider="claude", secret="abc"), Req("admin"))
        check("мусор вместо токена", "прошло", 400)
    except HTTPException as exc:
        check("мусор вместо токена — 400 с причиной",
              (exc.status_code, "setup-token" in exc.detail), (400, True))
    try:
        W.ui_subscription_edit("abcdef123456", W.SubscriptionEditIn(label="x"), Req("admin"))
        check("чужой id", "прошло", 404)
    except HTTPException as exc:
        check("чужой id — 404", exc.status_code, 404)
    sid = out["item"]["id"]
    ed = W.ui_subscription_edit(sid, W.SubscriptionEditIn(label="  Ночная  "), Req("admin"))
    check("переименование", ed["item"]["label"], "Ночная")
    W.ui_subscription_remove(sid, Req("admin"))
    check("админ удалил", S.listing(), [])

    print("Тревога: подписка разлогинилась")
    import pub_alerts as A
    check("пусто — повода нет", A.check_text_subscription_dead(None, 1.0), [])
    one = S.add("codex", codex_auth(account="acc-A", email="a@x.io"), "Первая")
    two = S.add("codex", codex_auth(account="acc-B", email="b@x.io"), "Вторая")
    home_one = next(h for h in core.get_server_codex_homes() if one["id"] in h)
    for _ in range(2):
        core._note_codex_result(home_one, False, "CODEX_AUTH: 401")
    got = A.check_text_subscription_dead(None, 1.0)
    check("один повод на протухшую", [a.key for a in got], [f"text_sub_dead:{one['id']}"])
    check("в заголовке имя подписки", "«Первая»" in got[0].title, True)
    check("сказано, что текст пишет вторая", "рабочих — 1" in got[0].text, True)
    check("сказано, что делать", "codex login" in got[0].text, True)
    check("секрета в тревоге нет", "SECRET" in got[0].text + got[0].title, False)
    check("повод входит в общий обход", any(
        f.__name__ == "check_text_subscription_dead" for f in A.CHECKS), True)
    home_two = next(h for h in core.get_server_codex_homes() if two["id"] in h)
    for _ in range(2):
        core._note_codex_result(home_two, False, "CODEX_AUTH: 401")
    got = A.check_text_subscription_dead(None, 1.0)
    check("обе протухли — два повода", len(got), 2)
    check("и сказано, что текст встанет", all("встанут" in a.text for a in got), True)
    S.replace(one["id"], codex_auth(account="acc-A", email="a@x.io", refresh="rt-2"))
    check("замена гасит повод", [a.key for a in A.check_text_subscription_dead(None, 1.0)],
          [f"text_sub_dead:{two['id']}"])
    S.remove(one["id"]); S.remove(two["id"])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("всё сходится")
    return 0


if __name__ == "__main__":
    sys.exit(main())
