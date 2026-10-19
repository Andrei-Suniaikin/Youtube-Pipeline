#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Привязка «профиль+канал → почта» по слову владельца (19.10.2026).

Зачем отдельно от bindings_fix.py. Тот выбирает аккаунт сам, по записям сверки: берёт
тот, под которым канал виден в Studio. Там, где канал виден под двумя аккаунтами профиля
(аккаунт-владелец и приглашённый менеджер) либо где права менеджера выданы только что и
сверка о них ещё не знает, выбрать машинa не может — и честно пропускает пару. Решение
приносит человек: 19.10 ребята назвали, какой из двух аккаунтов владелец, какому выдан
менеджер, и через какой работать. Этот скрипт записывает именно названное, не угадывая.

Чем отличается от auth_bindings.py: тот умеет только добавлять и пропускает уже
записанную привязку как конфликт. Здесь замена — смысл работы, поэтому она разрешена,
но только для пар, названных в командной строке поимённо.

Паролей и ключей 2FA скрипт не читает, не печатает и не меняет: правится единственное
поле `login` у записей `bindings`. Раздел `accounts` сверяется на «есть ли такой логин
и готов ли он к автовходу» и после записи проверяется на неизменность.

Сверка (если запись по профилю есть) используется только для предупреждения: если она
говорит, что канал под этим аккаунтом не виден, скрипт скажет об этом и всё равно
запишет — права могли выдать после сверки, и слово владельца сильнее старой записи.

    python3 ~/bindings_set.py --set 40:UCN9rwIpHw-UxhRBYBFdOfOw:a01…@gmail.com
    python3 ~/bindings_set.py --set … --set … --apply
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "/home/vova")
import gmail_forward_pilot as pilot                      # noqa: E402  (REGISTRY, mask)

REGISTRY = pilot.REGISTRY
mask = pilot.mask
AUDIT_GLOB = os.path.expanduser("~/accounts_audit/*_2026*/record.json")
UC_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
MAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def sha(email: str) -> str:
    return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()


def parse_task(spec: str) -> tuple:
    """«профиль:канал:почта» → (профиль, канал, почта). Разбор строгий: тихо принятая
    опечатка в номере канала означает привязку, которой никто не ищет."""
    parts = spec.split(":")
    if len(parts) != 3:
        raise SystemExit(f"--set {spec}: нужно «профиль:канал:почта»")
    profile, uc, login = (p.strip() for p in parts)
    if not profile.isdigit():
        raise SystemExit(f"--set {spec}: профиль «{profile}» — не номер")
    if not UC_RE.match(uc):
        raise SystemExit(f"--set {spec}: «{uc}» не похож на номер канала UC…")
    if not MAIL_RE.match(login):
        raise SystemExit(f"--set {spec}: «{mask(login)}» не похож на почту")
    return profile, uc, login.lower()


def audit_seen(profile: str) -> set:
    """Хэши аккаунтов, под которыми сверка видела хоть один канал профиля.
    Пусто — значит сверки нет, и предупреждать не о чем."""
    out = set()
    for path in sorted(glob.glob(AUDIT_GLOB)):
        try:
            rec = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(rec.get("profile")) != profile or not rec.get("accounts"):
            continue
        by_no = {a["authuser"]: a["login_sha256"] for a in rec["accounts"]}
        for chan in rec.get("channels") or []:
            for no in chan.get("found_under") or []:
                if no in by_no:
                    out.add(by_no[no])
    return out


def audit_channel_under(profile: str, uc: str) -> set:
    """Хэши аккаунтов, под которыми сверка видела именно этот канал."""
    out = set()
    for path in sorted(glob.glob(AUDIT_GLOB)):
        try:
            rec = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(rec.get("profile")) != profile or not rec.get("accounts"):
            continue
        by_no = {a["authuser"]: a["login_sha256"] for a in rec["accounts"]}
        for chan in rec.get("channels") or []:
            if chan.get("uc") != uc:
                continue
            for no in chan.get("found_under") or []:
                if no in by_no:
                    out.add(by_no[no])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="tasks", action="append", default=[],
                    metavar="профиль:канал:почта",
                    help="что записать; ключ можно повторять")
    ap.add_argument("--apply", action="store_true", help="бэкап и запись")
    args = ap.parse_args()
    if not args.tasks:
        raise SystemExit("нечего делать: ни одного --set")
    tasks = [parse_task(t) for t in args.tasks]

    if os.stat(REGISTRY).st_mode & 0o077:
        raise SystemExit(f"{REGISTRY}: права шире 0600 — остановился")
    doc = json.loads(open(REGISTRY, encoding="utf-8").read())
    accounts = doc.get("accounts") or []
    bindings = doc.setdefault("bindings", [])
    usable = {str(a.get("login") or "").strip().lower()
              for a in accounts
              if a.get("password") and (a.get("totp") or a.get("totp_secret"))}
    known = {str(a.get("login") or "").strip().lower() for a in accounts}

    added = moved = fine = 0
    stop = []
    for profile, uc, login in tasks:
        where = f"профиль {profile}, канал {uc}"
        if login not in known:
            stop.append(f"{where}: {mask(login)} в реестре нет — сначала auth_import.py")
            continue
        if login not in usable:
            stop.append(f"{where}: у {mask(login)} нет пароля или ключа кода — "
                        f"автовход этим аккаунтом не сможет войти")
            continue
        rows = [b for b in bindings
                if str(b.get("profile")) == profile and b.get("channel_uc") == uc]
        if len(rows) > 1:
            stop.append(f"{where}: в реестре {len(rows)} привязки — разобрать руками")
            continue
        under = audit_channel_under(profile, uc)
        if under and sha(login) not in under:
            print(f"! {where}: сверка видела канал под другим аккаунтом профиля. "
                  f"Записываю по слову владельца — права могли выдать после сверки")
        elif not under and audit_seen(profile):
            print(f"! {where}: в сверке этого канала нет вовсе — проверить после "
                  f"первой же заливки")
        if rows:
            current = str(rows[0].get("login") or "").strip().lower()
            if current == login:
                fine += 1
                print(f"= {where}: уже {mask(login)} — не трогаю")
                continue
            rows[0]["login"] = login
            moved += 1
            print(f"~ {where}: {mask(current) or 'пустая почта'} → {mask(login)}")
        else:
            bindings.append({"profile": profile, "channel_uc": uc, "login": login})
            added += 1
            print(f"+ {where}: привязка заведена на {mask(login)}")

    print(f"\nитог: завести {added}, перевести {moved}, уже верных {fine}, "
          f"отказов {len(stop)}")
    for s in stop:
        print("  отказ:", s)
    if not args.apply:
        print("\nсухой прогон: реестр не тронут. Для записи — --apply")
        return 1 if stop else 0
    if not (added or moved):
        print("менять нечего")
        return 1 if stop else 0

    before = len(accounts)
    backup = f"{REGISTRY}.bak-{time.strftime('%Y%m%d-%H%M%S')}-bindingsset"
    shutil.copy2(REGISTRY, backup)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(REGISTRY), prefix=".bindset-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    check = json.loads(open(tmp, encoding="utf-8").read())
    if len(check.get("accounts") or []) != before:
        os.unlink(tmp)
        raise SystemExit("раздел accounts изменился — запись отменена")
    os.replace(tmp, REGISTRY)
    print(f"записано, бэкап {backup}")
    print("перезапуск пуллеров не нужен: привязка читается на каждую попытку входа")
    return 1 if stop else 0


if __name__ == "__main__":
    sys.exit(main())
