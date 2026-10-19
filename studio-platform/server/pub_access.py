#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единый список каналов: кто каким каналом управляет и чего не хватает для заливки.

Владелец 12.10.2026 попросил единый список каналов с владельцами, менеджерами, профилями
и отметкой «права приняты»; 19.10.2026 — «давай сделаем, только для ребят нужен понятный
флоу». Этот модуль — машинная половина: он принимает с Минска факты сверки профилей и
превращает их в один вердикт на канал, одинаковый для экрана, отчёта и письма ребятам.

Почему это вообще нужно. Канал в YouTube Studio виден не владельцу аккаунта, а
приглашённому менеджеру. Заливка входит в Google сама, значит ей нужны пароль и ключ 2FA
ИМЕННО того аккаунта, под которым канал виден. Если привязка «профиль+канал → почта»
указывает на другой аккаунт того же профиля, заливка идёт, пока жива открытая руками
сессия браузера, а первый самостоятельный вход ломается с «активный аккаунт не управляет
каналом». 19.10.2026 так стояли 16 каналов на пяти профилях — и увидеть это в студии было
нельзя, только сверкой на машине.

Что здесь есть и чего нет:

  * **Секретов здесь нет и не будет.** Приходят только хэш логина (`sha256`), маска для
    человека (`a01…1234@gmail.com`) и флаги «есть в реестре» / «есть пароль и код».
    Пароли и ключи 2FA живут на Минске в файле 0600 и в студию не ездят. Поэтому этот
    список можно показывать и выгружать.
  * **Отметка «права приняты» не ручная, а машинная.** Сверка видит в шапке Studio
    «You're a manager» — это и есть доказательство, что приглашение принято. Ручная
    галочка «мы выдали права» говорит только о намерении: 19.10 по профилю 40 права были
    «выданы», а Studio отвечала «нет доступа» — ручная отметка соврала бы.
  * **Вердикт считается в одном месте.** `verdicts()` зовут и список каналов, и карточка,
    и выгрузка для ребят: разойдясь, они начали бы спорить, чего не хватает каналу.
"""
from __future__ import annotations

import json
import sqlite3
import time

# Что может быть не так с доступом к каналу. Порядок = порядок важности для человека.
STATE_TEXT = {
    "ok": "вход настроен",
    "wrong_account": "привязка на аккаунт, который канала не видит",
    "no_secrets": "нет пароля и ключа 2FA того аккаунта, который видит канал",
    "no_binding": "канал виден, но привязка не записана",
    "not_in_profile": "канала нет ни под одним аккаунтом профиля",
    "unchecked": "профиль ещё не сверяли",
}

# Что делать человеку. Это же идёт в выгрузку для ребят.
STATE_FIX = {
    "ok": "",
    "wrong_account": "перевести привязку на аккаунт, который видит канал "
                     "(bindings_fix.py делает это сам, если аккаунт есть в реестре)",
    "no_secrets": "запросить у ребят адрес, пароль и ключ 2FA этого аккаунта",
    "no_binding": "записать привязку профиль+канал → почта",
    "not_in_profile": "проверить, выданы ли права менеджера и тот ли это профиль; "
                      "если канал удалён — снять его с плана",
    "unchecked": "прогнать сверку профиля (profile_accounts_audit.py), "
                 "профиль не должен быть занят заливкой",
}

MAX_ACCOUNTS = 40            # аккаунтов в одном профиле столько не бывает; защита от мусора


def ensure_schema(c: sqlite3.Connection) -> None:
    """Таблица фактов сверки. Ключ — канал плюс хэш аккаунта: один канал бывает виден
    под несколькими аккаунтами профиля, и это само по себе важный факт."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS pub_channel_access("
        " channel_id INTEGER NOT NULL,"
        " account_sha TEXT NOT NULL,"
        " profile TEXT NOT NULL,"
        " account_mask TEXT NOT NULL,"
        " authuser INTEGER,"
        " sees_channel INTEGER NOT NULL DEFAULT 0,"
        " is_manager INTEGER NOT NULL DEFAULT 0,"
        " in_registry INTEGER NOT NULL DEFAULT 0,"
        " has_secrets INTEGER NOT NULL DEFAULT 0,"
        " bound INTEGER NOT NULL DEFAULT 0,"
        " checked REAL NOT NULL,"
        " PRIMARY KEY(channel_id, account_sha))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_access_profile "
              "ON pub_channel_access(profile)")


def _flag(v) -> int:
    return 1 if v else 0


def ingest(c: sqlite3.Connection, payload: dict, now: float = None) -> dict:
    """Факты сверки одного профиля. Отчёт заменяет прежние факты этого профиля целиком:
    аккаунт могли убрать из браузера, и старая строка врала бы, что он всё ещё видит канал.

    Ждём: `{"profile": "54", "checked": <unix>, "channels": [
              {"uc": "UC…", "accounts": [
                 {"sha": "<64 hex>", "mask": "a01…1234@gmail.com", "authuser": 2,
                  "sees": true, "manager": true, "in_registry": false,
                  "has_secrets": false, "bound": false}]}]}`
    Хэш — чтобы сравнивать аккаунты, не зная их адресов; маска — чтобы человек узнал.
    """
    now = time.time() if now is None else now
    ensure_schema(c)
    profile = str(payload.get("profile") or "").strip()
    if not profile:
        raise ValueError("не сказано, какой это профиль")
    checked = float(payload.get("checked") or now)
    items = payload.get("channels")
    if not isinstance(items, list):
        raise ValueError("нет списка каналов")

    uc_to_id = {r["uc"]: r["id"] for r in c.execute(
        "SELECT id, uc FROM pub_channels WHERE COALESCE(uc,'')<>''")}
    # Прежние факты профиля — их заменяем, но только по каналам, которые есть в отчёте,
    # и по каналам этого профиля: отчёт о профиле 54 не должен стирать факты профиля 26.
    c.execute("DELETE FROM pub_channel_access WHERE profile=?", (profile,))

    written = unknown = 0
    for item in items:
        uc = str((item or {}).get("uc") or "").strip()
        cid = uc_to_id.get(uc)
        if cid is None:
            unknown += 1
            continue
        accounts = (item or {}).get("accounts") or []
        if len(accounts) > MAX_ACCOUNTS:
            raise ValueError(f"в отчёте {len(accounts)} аккаунтов на канал — это мусор")
        for a in accounts:
            sha = str((a or {}).get("sha") or "").strip().lower()
            if len(sha) != 64:
                continue
            c.execute(
                "INSERT OR REPLACE INTO pub_channel_access(channel_id,account_sha,profile,"
                "account_mask,authuser,sees_channel,is_manager,in_registry,has_secrets,"
                "bound,checked) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cid, sha, profile, str(a.get("mask") or "?")[:120],
                 a.get("authuser"), _flag(a.get("sees")), _flag(a.get("manager")),
                 _flag(a.get("in_registry")), _flag(a.get("has_secrets")),
                 _flag(a.get("bound")), checked))
            written += 1
    return {"profile": profile, "accounts": written, "unknown_channels": unknown}


def verdict_of(rows: list) -> dict:
    """Один вердикт по строкам доступа одного канала. Пустой список — «не сверяли».

    Разбор именно в таком порядке, потому что так его читает человек: сначала «всё
    хорошо», потом «есть кому входить, но входим не тем», потом «входить некем».
    """
    if not rows:
        return {"state": "unchecked", "text": STATE_TEXT["unchecked"],
                "fix": STATE_FIX["unchecked"], "profile": "", "accounts": []}
    profile = str(rows[0].get("profile") or "")
    seers = [r for r in rows if r.get("sees_channel")]
    bound = [r for r in rows if r.get("bound")]
    accounts = [{"mask": r.get("account_mask"), "authuser": r.get("authuser"),
                 "sees": bool(r.get("sees_channel")), "manager": bool(r.get("is_manager")),
                 "in_registry": bool(r.get("in_registry")),
                 "has_secrets": bool(r.get("has_secrets")),
                 "bound": bool(r.get("bound"))} for r in rows]

    def out(state, extra=""):
        return {"state": state, "text": STATE_TEXT[state],
                "fix": (STATE_FIX[state] + (f" ({extra})" if extra else "")).strip(),
                "profile": profile, "accounts": accounts}

    if not seers:
        return out("not_in_profile")
    # Кто-то канал видит. Хватает ли у него секретов и стоит ли на него привязка?
    with_secrets = [r for r in seers if r.get("has_secrets")]
    if not with_secrets:
        who = ", ".join(f"#{r.get('authuser')} {r.get('account_mask')}" for r in seers)
        return out("no_secrets", f"аккаунт {who} в профиле {profile}")
    if not bound:
        return out("no_binding")
    if any(r.get("bound") and r.get("sees_channel") and r.get("has_secrets") for r in rows):
        return out("ok")
    # Привязка есть, но она на аккаунт, который канала не видит, — главная мина.
    right = ", ".join(f"#{r.get('authuser')} {r.get('account_mask')}" for r in with_secrets)
    return out("wrong_account", f"канал видит {right}")


def verdicts(c: sqlite3.Connection, tenant) -> dict:
    """Канал → вердикт. Один проход на весь список каналов."""
    ensure_schema(c)
    by_channel = {}
    for r in c.execute(
            "SELECT a.* FROM pub_channel_access a JOIN pub_channels ch ON ch.id=a.channel_id "
            "WHERE ch.tenant_id=? ORDER BY a.authuser", (tenant,)):
        by_channel.setdefault(r["channel_id"], []).append(dict(r))
    out = {}
    for r in c.execute("SELECT id FROM pub_channels WHERE tenant_id=?", (tenant,)):
        out[r[0]] = verdict_of(by_channel.get(r[0], []))
    return out


def summary(c: sqlite3.Connection, tenant) -> dict:
    """Сколько каналов в каком состоянии — для шапки экрана и для сводки."""
    counts = {}
    for v in verdicts(c, tenant).values():
        counts[v["state"]] = counts.get(v["state"], 0) + 1
    return counts


# ─────────────────────────── ручка приёма с Минска ───────────────────────────

def register_access_routes(app, ctx):
    """`POST /api/access/report` — отчёт сверки одного профиля под тем же токеном ноды
    заливки, что и почта: это та же машина, второй секрет заводить незачем."""
    import secrets as _secrets
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    db = ctx["db"]
    token_of = ctx["upload_token"]
    log = ctx.get("log") or (lambda *a: None)

    class AccessReportIn(BaseModel):
        profile: str
        checked: float | None = None
        channels: list = []

    def _same_secret(got: str, want: str) -> bool:
        if not want or not got:
            return False
        try:
            return _secrets.compare_digest(got.encode("utf-8"), want.encode("utf-8"))
        except (UnicodeError, TypeError):                      # noqa: BLE001
            return False

    @app.post("/api/access/report")
    def api_access_report(body: AccessReportIn, request: Request):
        if not _same_secret(request.headers.get("X-Upload-Token", ""), token_of()):
            raise HTTPException(401, "нужен токен ноды заливки")
        c = db()
        try:
            try:
                out = ingest(c, body.model_dump())
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            c.commit()
        finally:
            c.close()
        log(f"сверка профиля {out['profile']}: записей {out['accounts']}"
            + (f", каналов не из студии {out['unknown_channels']}"
               if out["unknown_channels"] else ""))
        return out
