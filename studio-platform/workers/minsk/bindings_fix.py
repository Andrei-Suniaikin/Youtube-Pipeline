#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перевод привязок «профиль+канал → почта» на тот аккаунт, который канал видит (18.10.2026).

Зачем отдельно от auth_bindings.py. Тот умеет только ДОБАВЛЯТЬ: уже записанную привязку
он намеренно не трогает и пропускает как конфликт. Сверка 18.10 нашла другую беду: в
каждом профиле AdsPower сидит аккаунт-владелец и отдельный аккаунт-менеджер, каналы студии
видны почти всегда только под менеджером, а привязка указывает на владельца. Автовход берёт
почту из привязки, входит во владельца, Studio открывается без нужного канала — выпуск
встаёт на «вошли, но канал не подтвердился» (так по 15–20 раз падали «Maszyny Wojny»,
«Stal i Popiół», «Szturm i Obrona»). Починить это можно только заменой, поэтому замена
живёт отдельным файлом и включается явным ключом.

Паролей и ключей 2FA скрипт не читает, не печатает и не меняет: он правит единственное
поле `login` у записей `bindings`. Раздел `accounts` сверяется только на «есть ли такой
логин» и после записи проверяется на неизменность.

Что считается само, без входных файлов:
  • какие каналы у профиля — выгрузка студии ~/accounts_audit/studio_channels.csv;
  • под каким аккаунтом канал виден — записи сверки ~/accounts_audit/<профиль>_*/record.json
    (поле found_under: номер аккаунта в браузере);
  • годен ли аккаунт для автовхода — есть ли его логин в accounts реестра с паролем и кодом.

Трогаются только пары, где привязки нет или где привязанная почта канал НЕ видит.
Где кандидатов несколько или ни одного — пропуск с причиной, угадывать нельзя.

    python3 ~/bindings_fix.py            # сухой прогон
    python3 ~/bindings_fix.py --apply    # бэкап + атомарная запись
"""
import argparse
import csv
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
STUDIO_CSV = os.path.expanduser("~/accounts_audit/studio_channels.csv")
AUDIT_GLOB = os.path.expanduser("~/accounts_audit/*_2026*/record.json")
UC_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


def sha(email):
    return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()


def studio_channels():
    out = {}
    with open(STUDIO_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            uc = str(row["канал"]).strip()
            if UC_RE.match(uc):
                out.setdefault(str(row["профиль"]).strip(), []).append(
                    (uc, str(row.get("название") or "").strip()))
    return out


def audits():
    """Свежая запись сверки по каждому профилю, где известны и аккаунты, и каналы."""
    out = {}
    for path in sorted(glob.glob(AUDIT_GLOB)):
        rec = json.load(open(path, encoding="utf-8"))
        if rec.get("accounts") and rec.get("channels"):
            out[rec["profile"]] = rec
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if os.stat(REGISTRY).st_mode & 0o077:
        raise SystemExit(f"{REGISTRY}: права шире 0600 — остановился")
    doc = json.loads(open(REGISTRY, encoding="utf-8").read())
    accounts = doc.get("accounts") or []
    bindings = doc.setdefault("bindings", [])
    # Годный для автовхода логин: есть пароль и ключ кода. Значения не читаем, только факт.
    usable = {str(a.get("login") or "").strip().lower()
              for a in accounts
              if a.get("password") and (a.get("totp") or a.get("totp_secret"))}
    by_sha = {sha(login): login for login in usable}

    chans, auds = studio_channels(), audits()
    added = moved = fine = 0
    skips, moves = [], []

    for profile in sorted(chans, key=lambda p: (len(p), p)):
        audit = auds.get(profile)
        if audit is None:
            skips.append(f"профиль {profile}: сверки нет — пропускаю весь профиль")
            continue
        seen = {a["authuser"]: a["login_sha256"] for a in audit["accounts"]}
        for uc, name in chans[profile]:
            where = f"профиль {profile}, «{name}»"
            chan = next((c for c in audit["channels"] if c["uc"] == uc), None)
            if chan is None:
                continue
            rows = [b for b in bindings
                    if str(b.get("profile")) == profile and b.get("channel_uc") == uc]
            if len(rows) > 1:
                skips.append(f"{where}: в реестре {len(rows)} привязки — разобрать руками")
                continue
            current = str(rows[0].get("login") or "").strip().lower() if rows else ""
            under = [seen[n] for n in (chan.get("found_under") or []) if n in seen]
            if current and sha(current) in under:
                fine += 1
                continue
            good = [by_sha[s] for s in under if s in by_sha]
            if not good:
                why = ("канал не виден ни под одним аккаунтом профиля" if not under
                       else "аккаунт, который видит канал, не готов к автовходу "
                            "(нет в реестре либо без пароля или кода)")
                skips.append(f"{where}: {why}")
                continue
            if len(good) > 1:
                skips.append(f"{where}: канал виден под {len(good)} готовыми аккаунтами — "
                             f"выбрать нельзя, нужен человек")
                continue
            login = good[0]
            if rows:
                rows[0]["login"] = login
                moved += 1
                moves.append(f"~ {where}: {mask(current) or 'пустая почта'} → {mask(login)}")
            else:
                bindings.append({"profile": profile, "channel_uc": uc, "login": login})
                added += 1
                moves.append(f"+ {where}: привязка заведена на {mask(login)}")

    for line in moves:
        print(line)
    print(f"\nитог: завести {added}, перевести {moved}, уже верных {fine}, пропущено {len(skips)}")
    for s in skips:
        print("  пропуск:", s)
    if not args.apply:
        print("\nсухой прогон: реестр не тронут. Для записи — --apply")
        return 0
    if not (added or moved):
        print("менять нечего")
        return 0

    before = len(accounts)
    backup = f"{REGISTRY}.bak-{time.strftime('%Y%m%d-%H%M%S')}-bindingsfix"
    shutil.copy2(REGISTRY, backup)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(REGISTRY), prefix=".bindfix-")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
