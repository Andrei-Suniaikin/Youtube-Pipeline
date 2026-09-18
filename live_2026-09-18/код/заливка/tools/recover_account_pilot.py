#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ручной пилот восстановления входа в УЖЕ открытом профиле AdsPower.

Зачем отдельный запуск: боевой путь — это воркер заливки, но профиль может быть
уже открыт оператором на другой машине, и закрывать/переоткрывать его нельзя.
Этот скрипт берёт существующую сессию под исключительное владение (лиза профиля
у общего координатора + проверка обеих машин), подключается к уже запущенному
браузеру и вызывает ровно тот же модуль, что и воркер.

Он НИКОГДА не закрывает профиль и не открывает вторую копию: подключение идёт
через debuggerAddress, driver.quit() не вызывается (он убил бы чужой браузер),
AdsPower browser/stop не дёргается.

    python3 tools/recover_account_pilot.py --profile 28 --uc UC... [--dry-run]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import upload_account_recovery as recovery          # noqa: E402
import upload_coordination                          # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = _load("pilot_collector", ROOT / "tools" / "collect_channel_emails.py")

# Опрашиваем КАЖДУЮ машину ровно один раз: localhost и tailscale-адрес своей же
# машины — это один и тот же AdsPower, а он отвечает "Too many request per
# second" на сдвоенный опрос. Полнота при этом сохраняется: своя машина + чужая.
def _hosts():
    local = "http://localhost:50325"
    peer = ("minsk", "http://100.75.222.76:50325") if sys.platform == "darwin" \
        else ("mac", "http://100.100.244.89:50325")
    return {"local": local, peer[0]: peer[1]}


HOSTS = _hosts()
PEER = next(name for name in HOSTS if name != "local")
LEASE_CONTEXT = {
    "schema_version": 1,
    "coordinator_command": ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--",
                            "video-ops-profile-leases-vps",
                            "/opt/video-ops/upload_profile_lease.py"],
    "holder": "mac",
    "ttl_seconds": 1800,
    "renew_interval_seconds": 300,
    "command_timeout_seconds": 40,
}


def active_here(api, profile):
    """(uid, debug_port) если профиль открыт на ЭТОЙ машине, иначе исключение."""
    index = collector.profile_index(api)
    uid = collector.uid_of(str(profile), index)
    if not uid:
        raise SystemExit(f"профиль {profile} не найден в AdsPower")
    response = collector._requests().get(api + "/api/v1/browser/local-active", timeout=15)
    entries = response.json().get("data", {}).get("list") or []
    mine = [e for e in entries if e.get("user_id") == uid]
    if len(mine) != 1 or not str(mine[0].get("debug_port", "")).isdigit():
        raise SystemExit(f"профиль {profile} не открыт на этой машине — пилот не применим")
    return uid, int(mine[0]["debug_port"])


def exclusive_or_die(profile, uid):
    """Профиль занят РОВНО на этой машине и на нём не идёт заливка."""
    busy, unreachable = collector.busy_everywhere(HOSTS, allow_unreachable=True)
    if unreachable:
        raise SystemExit(f"недоступны хосты {unreachable} — владение не доказано, стоп")
    where = busy.get(uid) or busy.get(str(profile)) or []
    foreign = sorted({h for h in where if h != "local"})
    if foreign:
        raise SystemExit(f"профиль открыт также на {foreign} — вторую сессию не создаём")
    procs = collector.local_upload_procs()
    if str(profile) in procs or uid in procs:
        raise SystemExit(f"по профилю {profile} уже идёт заливка — не вмешиваемся")
    return where


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--uc", required=True)
    parser.add_argument("--accounts-json", default=str(Path.home() / "ops/private/upload_accounts_profile28.json"))
    parser.add_argument("--auth-root", default=str(ROOT / "research/ww2_upload_stress_20260914"))
    parser.add_argument("--evidence", default="")
    parser.add_argument("--dry-run", action="store_true",
                        help="только доказать владение и состояние, ничего не вводить")
    parser.add_argument("--probe", action="store_true",
                        help="дойти до страницы выбора аккаунта и проверить, что нужная "
                             "строка находится; НИЧЕГО не нажимать и не вводить")
    args = parser.parse_args()

    os.environ["VIDEO_UPLOAD_ACCOUNT_RECOVERY"] = "1"
    os.environ.setdefault("ADSPOWER_RATE_LIMIT_DIR",
                          str(Path.home() / ".local/state/video-studio/adspower_rate_limit"))
    Path(os.environ["ADSPOWER_RATE_LIMIT_DIR"]).mkdir(parents=True, exist_ok=True)
    os.environ[recovery.ROOT_ENV] = args.auth_root
    os.environ[recovery.ACCOUNTS_ENV] = args.accounts_json
    api = HOSTS["local"]

    uid, port = active_here(api, args.profile)
    where = exclusive_or_die(args.profile, uid)
    print(json.dumps({"event": "ownership", "profile": args.profile, "uid": uid,
                      "debug_port": port, "open_on": where}, ensure_ascii=False), flush=True)

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    detail = collector._requests().get(api + "/api/v1/browser/active",
                                       params={"user_id": uid}, timeout=15).json().get("data", {})
    address = (detail.get("ws") or {}).get("selenium") or f"127.0.0.1:{port}"
    if address.rsplit(":", 1)[-1] != str(port):
        raise SystemExit("AdsPower отдал другой порт отладки — состояние изменилось, стоп")
    options = Options()
    options.add_experimental_option("debuggerAddress", address)
    service_path = detail.get("webdriver") or ""
    driver = (webdriver.Chrome(service=Service(service_path), options=options)
              if service_path else webdriver.Chrome(options=options))
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)

    lease = None
    try:
        with upload_coordination.profile_lease(str(args.profile), LEASE_CONTEXT) as lease:
            ownership_cache = {"at": 0.0}

            def ownership_check():
                """Владение подтверждается заново перед каждым вводом.

                AdsPower пропускает примерно один управляющий запрос в секунду и
                этот лимит общий со всеми заливками, поэтому переспрашиваем не
                чаще раза в 15 секунд: этого достаточно, чтобы ни один ввод не
                ушёл в профиль, открывшийся на второй машине, и при этом мы не
                занимаем чужую очередь опросами в цикле ожидания.
                """
                now = time.monotonic()
                if now - ownership_cache["at"] < 15.0:
                    return
                entries = collector._requests().get(
                    api + "/api/v1/browser/local-active", timeout=12
                ).json().get("data", {}).get("list") or []
                mine = [e for e in entries if e.get("user_id") == uid]
                if len(mine) != 1 or str(mine[0].get("debug_port")) != str(port):
                    raise RuntimeError("браузер профиля сменился — ввод прекращён")
                busy, unreachable = collector.busy_everywhere(HOSTS, allow_unreachable=True)
                if unreachable:
                    raise RuntimeError("вторая машина недоступна — владение не доказано")
                if any(h != "local" for h in (busy.get(uid) or [])):
                    raise RuntimeError("профиль открылся на второй машине — ввод прекращён")
                ownership_cache["at"] = now

            def lease_check():
                lease.assert_held()

            # Вкладку берём ту, что уже на странице входа Google: чужие вкладки
            # (в т.ч. сторонний 2FA-сайт) не трогаем.
            handles = driver.window_handles
            chosen = None
            for handle in handles:
                driver.switch_to.window(handle)
                host = (driver.current_url or "").split("/")[2] if "//" in (driver.current_url or "") else ""
                if host in ("accounts.google.com", "studio.youtube.com"):
                    chosen = handle
                    break
            if chosen is None:
                raise SystemExit("в профиле нет вкладки Google/Studio — не открываю новых, стоп")
            driver.switch_to.window(chosen)

            lease_check()
            ownership_check()
            # Воспроизводим то, что делает воркер: заходим прямо на канал.
            driver.get(f"https://studio.youtube.com/channel/{args.uc}/")
            time.sleep(3)
            secrets, reviewed = recovery._reviewed()
            cred = recovery._binding_login(secrets, str(args.profile), args.uc)
            state = recovery.classify(driver, args.uc, cred.login)
            print(json.dumps({"event": "state", **state}, ensure_ascii=False), flush=True)
            if args.dry_run:
                print(json.dumps({"event": "dry_run_done"}), flush=True)
                return 0

            if args.probe:
                # Ровно тот переход, который делает восстановление, и ровно то
                # выражение, которым оно ищет строку аккаунта. Клик НЕ делается.
                reached = recovery._open_account_chooser(driver, lease_check)
                after = recovery.classify(driver, args.uc, cred.login)
                point = driver.execute_script(
                    "return " + reviewed.account_choice_expression(cred.login))
                dom = driver.execute_script("return " + reviewed.READ_DOM)
                print(json.dumps({"event": "probe", "reached_chooser": reached,
                                  "state": after["state"], "snapshot": after["snapshot"],
                                  "account_row_found": bool(point),
                                  "page_language": driver.execute_script(
                                      "return document.documentElement.lang"),
                                  "headings": dom.get("headings"),
                                  "buttons": [b.get("text") for b in dom.get("buttons", [])][:6],
                                  }, ensure_ascii=False), flush=True)
                return 0

            statuses = []

            def on_status(value):
                statuses.append(value)
                print(json.dumps({"event": "status", "status": value}), flush=True)

            label = ""
            try:
                import sqlite3
                row = sqlite3.connect(str(Path.home() / "ops/registry.db")).execute(
                    "SELECT name FROM channels WHERE uc=?", (args.uc,)).fetchone()
                label = row[0] if row and row[0] else ""
            except Exception:
                label = ""
            result = recovery.maybe_recover(
                driver, str(args.profile), args.uc, lease_check,
                ownership_check=ownership_check, channel_label=label,
                evidence_dir=args.evidence or None,
                on_status=on_status, phase_before_upload=True,
            )
            print(json.dumps({"event": "recovery", "result": result,
                              "statuses": statuses}, ensure_ascii=False), flush=True)
            if not result:
                return 2
            # Независимое второе доказательство: список каналов этого аккаунта
            # читается отдельным механизмом (innertube), а не тем же DOM Studio.
            proven = str(result.get("session_index") or "")
            if proven.isdigit():
                channels = collector.read_channels(driver, int(proven))
                owns = any(c.get("uc") == args.uc for c in channels)
                print(json.dumps({"event": "independent_proof", "authuser": proven,
                                  "target_in_account_channels": owns,
                                  "channel_count": len(channels)}, ensure_ascii=False), flush=True)
            return 0
    finally:
        # НИКОГДА не driver.quit() и не browser/stop: сессия принадлежит оператору.
        try:
            driver.service.stop()
        except Exception:
            pass
        if lease is not None and lease.release_error:
            print(json.dumps({"event": "lease_release_error",
                              "error": lease.release_error}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
