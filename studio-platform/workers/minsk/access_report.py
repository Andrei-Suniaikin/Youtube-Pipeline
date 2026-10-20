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

ЗАПУСКАТЬ С ПОЛНЫМ ОКРУЖЕНИЕМ ПУЛЛЕРА — иначе и токен, и пути к хранилищам будут
не те. У заливщика два env-файла, и второй ПЕРЕБИВАЕТ первый: в `~/.vsupload_env`
лежит устаревший `UPLOAD_TOKEN`, верный — в `~/.vsupload_env_studio`. Поэтому:

    set -a; . ~/.vsupload_env; . ~/.vsupload_env_studio; set +a
    python3 -I access_report.py                    # все профили, последняя запись каждого
    python3 -I access_report.py --only 54,34       # только эти
    python3 -I access_report.py --dry-run          # показать, что отправится
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
# Умолчания обоих хранилищ входов — те же пути, что стоят в env заливщика (см.
# registry_paths: почему их два и почему проверять надо оба).
AUTH_STORE = ("/home/vova/ww2_upload_stress_20260914/private/"
              "upload_accounts_recovery.json")
RECOVERY_STORE = ("/home/vova/manager_login_20261012/private/"
                  "studio_manager_accounts_20261013.json")
# Куда отправлять отчёт.
#
# ИСПРАВЛЕНО 20.10.2026: тут стояло `http://127.0.0.1:8899` — адрес кабинета НА VPS,
# а скрипт запускается на МИНСКЕ, где на этом порту нет ничего. Все 23 профиля отвечали
# «В соединении отказано», и экран доступов стоял пустой. Умолчание теперь публичный
# адрес студии — тот же, которым ходят минские пуллеры (`VSGEN_SERVER`). Если скрипт
# всё же запускают на VPS, можно задать `VS_STUDIO_BASE=http://127.0.0.1:8090`.
BASE = (os.environ.get("VS_STUDIO_BASE") or os.environ.get("VSGEN_SERVER")
        or "https://studio.13.140.129.66.sslip.io")
TOKEN_FILES = ("/home/vova/.config/video-studio/upload_token",
               "/home/vova/.vsupload_token")


def sha(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode()).hexdigest()


def token() -> str:
    """Токен ноды заливки — тот же, которым ходят вердикты заливки.

    ИСПРАВЛЕНО 20.10.2026. Боевые пуллеры берут его из `UPLOAD_TOKEN`
    (`upload_puller.py`: `TOKEN = os.environ.get("UPLOAD_TOKEN", "")`, задаётся в
    `~/.vsupload_env`), а здесь этого имени в списке не было — зато первым шёл файл
    `~/.vsupload_token`, в котором лежит УСТАРЕВШИЙ токен (хэш c6456236…, у студии
    41e50c5c…). Поэтому студия отвечала 401 на все 23 профиля. Боевое имя теперь первое,
    файлы остаются запасным вариантом.

    Порядок важен: сначала env, потом файлы — env задаёт systemd рядом с пуллером,
    а файл может быть забытым слепком с прошлой настройки.
    """
    for name in ("UPLOAD_TOKEN", "VIDEO_UPLOAD_TOKEN", "VS_UPLOAD_TOKEN"):
        got = os.environ.get(name)
        if got and got.strip():
            return got.strip()
    for path in TOKEN_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    raise SystemExit("не нашёл токен ноды: ни в VIDEO_UPLOAD_TOKEN, ни в "
                     + ", ".join(TOKEN_FILES))


def registry_paths() -> list:
    """Все хранилища входов, в которые смотрит заливщик.

    ИСПРАВЛЕНО 20.10.2026. Здесь читался ОДИН файл, и вердикт «вход настроен» выходил
    неправдой. У заливщика два этапа входа, и каждый читает СВОЙ файл своей переменной:

      * обычный вход и «Verify it's you» — `upload_auth.py`,
        env `VIDEO_UPLOAD_AUTH_ACCOUNTS`, сейчас `upload_accounts_recovery.json`;
      * восстановление аккаунта («account recovery needs the owner») —
        `upload_account_recovery.py`, env `VIDEO_UPLOAD_ACCOUNTS_JSON`, сейчас
        `manager_login_20261012/private/studio_manager_accounts_20261013.json`.

    Вторая переменная задана в ДВУХ EnvironmentFile, и в unit'е побеждает последний
    (`~/.vsupload_env_studio`) — то есть файл не тот, который кажется по первому.
    20.10 из-за этого новый канал был заведён в первое хранилище, а падал на втором:
    десять выпусков отвечали «no exact account binding for this profile+channel».

    Привязка должна быть в ОБОИХ. Поэтому проверяем оба и говорим, в каком её нет.
    `VS_REGISTRY` по-прежнему уважается: если задана — только она, как раньше.
    """
    one = os.environ.get("VS_REGISTRY")
    if one:
        return [one]
    # `dict.fromkeys` — чтобы не читать один файл дважды: когда env подключён не
    # полностью (только `~/.vsupload_env`, без `~/.vsupload_env_studio`), обе
    # переменные указывают на одно и то же, и «привязка есть во всех хранилищах»
    # вышло бы правдой по недоразумению.
    return list(dict.fromkeys(
        p for p in (os.environ.get("VIDEO_UPLOAD_AUTH_ACCOUNTS") or AUTH_STORE,
                    os.environ.get("VIDEO_UPLOAD_ACCOUNTS_JSON") or RECOVERY_STORE)
        if p))


def registry_facts() -> dict:
    """Хэш логина → что о нём знают хранилища входов: есть ли он там, есть ли секреты,
    к каким парам «профиль+канал» привязан и в каком файле привязки НЕ ХВАТАЕТ."""
    out = {}
    pairs_by_store = {}
    for path in registry_paths():
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except OSError:
            # Хранилища может не быть на этой машине — это не повод ронять сверку.
            print(f"[реестр] НЕ ПРОЧИТАЛОСЬ: {path}", file=sys.stderr)
            continue
        here = set()
        for a in doc.get("accounts") or []:
            rec = out.setdefault(sha(a.get("login") or ""),
                                 {"in_registry": True, "has_secrets": False,
                                  "bound": set()})
            if a.get("password") and (a.get("totp") or a.get("totp_secret")):
                rec["has_secrets"] = True
        for b in doc.get("bindings") or []:
            pair = (str(b.get("profile")), b.get("channel_uc"))
            here.add(pair)
            rec = out.get(sha(b.get("login") or ""))
            if rec is not None:
                rec["bound"].add(pair)
        pairs_by_store[path] = here
        print(f"[реестр] прочитано: {path} — привязок {len(here)}", file=sys.stderr)

    if not pairs_by_store:
        raise SystemExit("ни одно хранилище входов не прочиталось")

    # Привязка, которой нет хотя бы в одном хранилище, — мина: вход пройдёт один этап
    # и упрётся в другой. Именно так 20.10 встали десять выпусков Baśnie Leszego.
    sets = list(pairs_by_store.values())
    for rec in out.values():
        rec["bound_everywhere"] = {p for p in rec["bound"] if all(p in s for s in sets)}
        rec["bound_partially"] = rec["bound"] - rec["bound_everywhere"]
    if len(sets) > 1:
        only_one = set().union(*sets) - set.intersection(*sets)
        if only_one:
            print(f"[реестр] привязок есть не во всех хранилищах: {len(only_one)} — "
                  f"вход по ним пройдёт один этап и упрётся в другой", file=sys.stderr)
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
                # Привязка считается записанной только если она есть во ВСЕХ
                # хранилищах входов: вход идёт двумя этапами и каждый читает своё.
                # Было «хоть в одном» — и вердикт «вход настроен» выходил неправдой.
                "bound": (profile, ch["uc"]) in (facts.get("bound_everywhere") or set()),
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
