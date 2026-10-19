#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отправить студии результат сверки профилей: кто каким каналом управляет (19.10.2026).

ЗАПУСКАЕТСЯ НА МИНСКЕ, рядом со сверкой `profile_accounts_audit.py`.

Зачем. Сверка кладёт результат в `~/accounts_audit/<профиль>_<время>/record.json`, и
дальше он живёт только на машине: в студии не видно, под каким аккаунтом канал виден и
туда ли смотрит привязка. 19.10.2026 из-за этого 16 каналов на пяти профилях стояли с
привязкой на аккаунт, который канала не видит, — а на экране всё выглядело нормально.

**Секретов не отправляем.** Наружу идут: хэш логина (`sha256`, он уже лежит в записи
сверки), маска для человека (`a01…@gmail.com`), номер аккаунта в переключателе Google и
четыре флага — видит ли канал, менеджер ли, есть ли аккаунт в реестре автовхода, есть ли
у него пароль и ключ 2FA. Ни адресов, ни паролей, ни кодов. Проверено тестом студии
(`tests/test_access.py`): в таблице под них нет даже столбцов.

Сопоставление «аккаунт ↔ канал» идёт по НАЗВАНИЮ канала из переключателя: `ucs` в записи
сверки — это идентификаторы разметки страницы, а не номера каналов, и совпадать с нашими
`UC…` они не обязаны. Название берём из того же списка, что видит человек.

    python3 access_report.py                       # все профили, последняя запись каждого
    python3 access_report.py --only 54,34          # только эти
    python3 access_report.py --dry-run             # показать, что отправится
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

AUDIT_DIR = os.environ.get("VS_AUDIT_DIR", "/home/vova/accounts_audit")
REGISTRY = os.environ.get(
    "VS_REGISTRY",
    "/home/vova/ww2_upload_stress_20260914/private/upload_accounts_recovery.json")
# Машинные ручки (`/api/...`) живут в кабинете на 8899 — там же, куда ходят почта
# и вердикты заливки. Пульт на 8800 их не знает: проверено, отвечает 404.
BASE = os.environ.get("VS_STUDIO_BASE", "http://127.0.0.1:8899")
TOKEN_FILES = ("/home/vova/.config/video-studio/upload_token",
               "/home/vova/.vsupload_token")


def sha(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode()).hexdigest()


def token() -> str:
    """Токен ноды заливки — тот же, которым ходит почта и вердикты заливки."""
    got = os.environ.get("VIDEO_UPLOAD_TOKEN") or os.environ.get("VS_UPLOAD_TOKEN")
    if got:
        return got.strip()
    for path in TOKEN_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    raise SystemExit("не нашёл токен ноды: ни в VIDEO_UPLOAD_TOKEN, ни в "
                     + ", ".join(TOKEN_FILES))


def registry_facts() -> dict:
    """Хэш логина → что о нём знает реестр автовхода: есть ли он там, есть ли секреты,
    и к каким парам «профиль+канал» он привязан."""
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            doc = json.load(f)
    except OSError as exc:
        raise SystemExit(f"реестр не прочитался: {exc}")
    out = {}
    for a in doc.get("accounts") or []:
        login = a.get("login") or ""
        out[sha(login)] = {
            "in_registry": True,
            "has_secrets": bool(a.get("password")
                                and (a.get("totp") or a.get("totp_secret"))),
            "bound": set(),
        }
    for b in doc.get("bindings") or []:
        key = sha(b.get("login") or "")
        if key in out:
            out[key]["bound"].add((str(b.get("profile")), b.get("channel_uc")))
    return out


def latest_records(only: set | None) -> dict:
    """Профиль → последняя запись сверки, у которой есть record.json."""
    best = {}
    for d in sorted(glob.glob(os.path.join(AUDIT_DIR, "*_*")), key=os.path.getmtime):
        name = os.path.basename(d)
        profile = name.split("_", 1)[0]
        if not profile.isdigit() or (only and profile not in only):
            continue
        path = os.path.join(d, "record.json")
        if os.path.exists(path):
            best[profile] = path
    return best


def channels_of(record: dict) -> list:
    """Каналы студии из записи сверки: `channels` — то, что сверка сопоставила сама."""
    out = []
    for ch in record.get("channels") or []:
        uc = str(ch.get("uc") or ch.get("channel_uc") or "").strip()
        name = str(ch.get("name") or ch.get("title") or "").strip()
        if uc:
            out.append({"uc": uc, "name": name})
    return out


def report_for(profile: str, path: str, reg: dict) -> dict:
    record = json.load(open(path, encoding="utf-8"))
    accounts = record.get("accounts") or []
    checked = float(record.get("finished") or record.get("started") or time.time())
    items = []
    for ch in channels_of(record):
        rows = []
        for a in accounts:
            key = str(a.get("login_sha256") or "")
            titles = " | ".join(a.get("titles") or [])
            sees = bool(ch["name"]) and ch["name"] in titles
            # «You're a manager» стоит в той же строке переключателя, что название.
            manager = sees and "manager" in titles.lower()
            facts = reg.get(key) or {}
            rows.append({
                "sha": key,
                "mask": a.get("login_masked") or "?",
                "authuser": a.get("authuser"),
                "sees": sees,
                "manager": manager,
                "in_registry": bool(facts.get("in_registry")),
                "has_secrets": bool(facts.get("has_secrets")),
                "bound": (profile, ch["uc"]) in (facts.get("bound") or set()),
            })
        if rows:
            items.append({"uc": ch["uc"], "accounts": rows})
    return {"profile": profile, "checked": checked, "channels": items}


def send(payload: dict, tok: str) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE.rstrip("/") + "/api/access/report", data=data,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Upload-Token", tok)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="профили через запятую")
    ap.add_argument("--dry-run", action="store_true", help="показать, не отправляя")
    args = ap.parse_args()

    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    reg = registry_facts()
    records = latest_records(only)
    if not records:
        print("записей сверки не нашёл — сначала прогнать profile_accounts_audit.py")
        return 1

    tok = "" if args.dry_run else token()
    sent = failed = 0
    for profile, path in sorted(records.items(), key=lambda kv: int(kv[0])):
        payload = report_for(profile, path, reg)
        n_ch = len(payload["channels"])
        n_acc = sum(len(x["accounts"]) for x in payload["channels"])
        if args.dry_run:
            print(f"профиль {profile}: каналов {n_ch}, записей {n_acc} "
                  f"({os.path.basename(os.path.dirname(path))})")
            for ch in payload["channels"]:
                seers = [a for a in ch["accounts"] if a["sees"]]
                who = ", ".join(f"#{a['authuser']} {a['mask']}"
                                + ("" if a["in_registry"] else " (нет в реестре)")
                                for a in seers) or "никто"
                print(f"    {ch['uc']}: видят — {who}")
            continue
        if not n_ch:
            print(f"профиль {profile}: каналов студии в записи нет — пропускаю")
            continue
        try:
            out = send(payload, tok)
        except urllib.error.HTTPError as exc:
            print(f"профиль {profile}: студия отказала — {exc.code} "
                  f"{exc.read()[:200].decode('utf-8', 'replace')}")
            failed += 1
            continue
        except OSError as exc:
            print(f"профиль {profile}: не отправилось — {exc}")
            failed += 1
            continue
        print(f"профиль {profile}: отправлено записей {out.get('accounts')}"
              + (f", каналов не из студии {out['unknown_channels']}"
                 if out.get("unknown_channels") else ""))
        sent += 1
    if not args.dry_run:
        print(f"итого: профилей отправлено {sent}, с отказом {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
