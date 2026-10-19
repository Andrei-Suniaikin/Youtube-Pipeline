#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Импорт аккаунтов автовхода из файла поставщика данных (18.10.2026).

Зачем. Сверка 18.10 показала: каналы студии видны под аккаунтом-менеджером профиля, а в
реестре автовхода таких аккаунтов нет — «Verify it's you» на их каналах проходит только
человек. Владелец присылает почту, пароль и постоянный ключ 2FA одним файлом; этот скрипт
раскладывает их по местам и ничего секретного не показывает наружу.

Формат файла (блоки по профилям):

    ПРОФИЛЬ 27
    Каналы:
    - Dawne Noce
      https://www.youtube.com/channel/UCSPvk14eRJfNiHjSjaIAKNw
    Логин: ...@gmail.com
    Пароль: ...
    2FA — постоянный секрет для аутентификатора: ...

Что проверяется до записи:
  • ключ 2FA — настоящий base32 (иначе коды не сойдутся, а ошибка всплывёт только на
    живом входе, уже после ввода пароля);
  • почта РЕАЛЬНО вошла в браузер этого профиля — сравнивается sha256 адреса с записями
    сверки (~/accounts_audit/<профиль>_*/record.json). Не сошлось — пропуск с причиной:
    добавить аккаунт, которого в профиле нет, значит гарантированно войти не туда;
  • номера каналов из файла сверяются с тем, что видно под этим аккаунтом, и расхождение
    печатается — но на запись аккаунта не влияет.

Привязки каналов этот скрипт НЕ трогает: ими занимается bindings_fix.py, который считает
их по сверке. Порядок всегда такой: сначала аккаунты, потом привязки.

    python3 ~/auth_import.py ~/acc.txt                  # сухой прогон
    python3 ~/auth_import.py ~/acc.txt --apply          # бэкап + запись
    python3 ~/auth_import.py ~/acc.txt --apply --shred  # и затереть исходник
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/home/vova")
import gmail_forward_pilot as pilot                       # noqa: E402  (REGISTRY, mask)

REGISTRY = pilot.REGISTRY
mask = pilot.mask
AUDIT_GLOB = os.path.expanduser("~/accounts_audit/*_2026*/record.json")
B32 = re.compile(r"^[A-Z2-7]{16,64}=*$")
EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
UC = re.compile(r"UC[0-9A-Za-z_-]{22}")
HEAD = re.compile(r"^\s*ПРОФИЛЬ\s+(\d+)\s*$", re.I)
LOGIN = re.compile(r"^\s*Логин\s*:\s*(\S+)\s*$", re.I)
PWD = re.compile(r"^\s*Пароль\s*:\s*(.+?)\s*$", re.I)
TOTP = re.compile(r"^\s*2FA[^:]*:\s*(\S+)\s*$", re.I)
BACKUP_MAIL = re.compile(r"^\s*Резервная почта\s*:", re.I)


def sha(email):
    return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()


def parse(path):
    """Файл → [{profile, login, password, totp, ucs}]. Строки резервной почты пропускаем:
    её пароль поставщик не дал, и в реестр автовхода она всё равно не идёт."""
    blocks, cur = [], None
    for raw in open(path, encoding="utf-8"):
        head = HEAD.match(raw)
        if head:
            if cur:
                blocks.append(cur)
            cur = {"profile": head.group(1), "login": "", "password": "", "totp": "", "ucs": []}
            continue
        if cur is None:
            continue
        if BACKUP_MAIL.match(raw):
            cur["skip_next_password"] = True
            continue
        m = LOGIN.match(raw)
        if m:
            cur["login"] = m.group(1).strip().lower()
            continue
        m = TOTP.match(raw)
        if m:
            cur["totp"] = m.group(1).replace(" ", "").upper()
            continue
        m = PWD.match(raw)
        if m:
            if cur.pop("skip_next_password", False):
                continue                      # это пароль резервной почты, он не наш
            cur["password"] = m.group(1)
            continue
        cur["ucs"].extend(u for u in UC.findall(raw) if u not in cur["ucs"])
    if cur:
        blocks.append(cur)
    return blocks


def audits():
    out = {}
    for path in sorted(glob.glob(AUDIT_GLOB)):
        rec = json.load(open(path, encoding="utf-8"))
        if rec.get("accounts"):
            out[rec["profile"]] = rec
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--shred", action="store_true")
    args = ap.parse_args()

    if os.stat(REGISTRY).st_mode & 0o077:
        raise SystemExit(f"{REGISTRY}: права шире 0600 — остановился")
    doc = json.loads(open(REGISTRY, encoding="utf-8").read())
    accounts = doc.setdefault("accounts", [])
    by_login = {str(a.get("login") or "").strip().lower(): a for a in accounts}
    auds = audits()

    added = updated = same = 0
    skips, notes = [], []
    for b in parse(args.source):
        where = f"профиль {b['profile']}"
        if not EMAIL.match(b["login"] or ""):
            skips.append(f"{where}: почта не разобрана")
            continue
        who = f"{where}, {mask(b['login'])}"
        if not b["password"]:
            skips.append(f"{who}: нет пароля")
            continue
        if not B32.match(b["totp"] or ""):
            skips.append(f"{who}: ключ 2FA не base32 — проверьте строку 2FA")
            continue
        audit = auds.get(b["profile"])
        if audit is None:
            skips.append(f"{who}: профиль не сверялся — не могу подтвердить, что эта почта в нём")
            continue
        acc_rec = next((a for a in audit["accounts"] if a["login_sha256"] == sha(b["login"])), None)
        if acc_rec is None:
            skips.append(f"{who}: этой почты нет среди вошедших в профиль — не добавляю")
            continue
        if audit.get("channels"):
            under = {c["uc"] for c in audit["channels"]
                     if acc_rec["authuser"] in (c.get("found_under") or [])}
            missing = [u for u in b["ucs"] if u not in under]
            if missing:
                notes.append(f"{who}: в файле есть каналы, которых под этим аккаунтом не видно: "
                             f"{', '.join(missing)}")
        cur = by_login.get(b["login"])
        if cur is None:
            accounts.append({"login": b["login"], "password": b["password"], "totp": b["totp"]})
            by_login[b["login"]] = accounts[-1]
            added += 1
            print(f"+ {who}: аккаунт добавлен (вошёл в профиль под номером {acc_rec['authuser']})")
        elif cur.get("password") != b["password"] or (cur.get("totp") or "") != b["totp"]:
            cur["password"], cur["totp"] = b["password"], b["totp"]
            updated += 1
            print(f"~ {who}: пароль и ключ обновлены")
        else:
            same += 1
            print(f"= {who}: уже был с теми же данными")

    print(f"\nитог: добавить {added}, обновить {updated}, без изменений {same}, "
          f"пропущено {len(skips)}")
    for n in notes:
        print("  ·", n)
    for s in skips:
        print("  пропуск:", s)
    if not args.apply:
        print("\nсухой прогон: реестр не тронут. Для записи — --apply")
        return 0
    if not (added or updated):
        print("менять нечего")
        return 0

    before = len(doc.get("bindings") or [])
    backup = f"{REGISTRY}.bak-{time.strftime('%Y%m%d-%H%M%S')}-authimport"
    shutil.copy2(REGISTRY, backup)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(REGISTRY), prefix=".authimp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    check = json.loads(open(tmp, encoding="utf-8").read())
    if len(check.get("bindings") or []) != before:
        os.unlink(tmp)
        raise SystemExit("раздел bindings изменился — запись отменена")
    os.replace(tmp, REGISTRY)
    print(f"записано, бэкап {backup}")
    print("дальше: python3 ~/bindings_fix.py --apply — привязки каналов на эти аккаунты")
    if args.shred:
        try:
            subprocess.run(["shred", "-u", args.source], check=True)
            print(f"исходник {args.source} затёрт")
        except Exception as exc:                          # noqa: BLE001
            print(f"исходник НЕ затёрт ({str(exc)[:60]}) — удалите сами: shred -u {args.source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
