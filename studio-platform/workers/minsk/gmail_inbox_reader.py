#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение списка писем Gmail внутри профиля AdsPower — только чтение (17.10.2026).

Общая часть для обхода почты (`mail_sweep.py`) и ручных прогонов. Берёт проверенные
куски пилота пересылки (`gmail_forward_pilot.py`, фаза inbox, живьём на профиле 20):
поиск аккаунта по почте через /u/N, запрос по письмам YouTube/Google/AdSense за 7 дней,
снятие строк списка без открытия писем.

Что делает за одно открытие профиля:
  1. бронь у координатора (та же, что у заливки и сборщика часов), проверка, что на
     профиле нет заливщика и браузер не открыт посторонним;
  2. запуск браузера, своя новая вкладка — исходные вкладки не трогаем;
  3. по /u/0…/u/N узнаёт, какая почта под каким номером; читает список для КАЖДОЙ
     почты профиля из реестра привязок (у одного профиля их бывает несколько);
  4. закрывает свою вкладку, останавливает браузер, отдаёт бронь.

Секреты: полная почта нужна только чтобы узнать аккаунт на странице; наружу (в записи,
в журнал, в студию) уходит маска `min…@gmail.com`. Ничего в аккаунтах не меняется.

Располагается на Минске в /home/vova/, рядом с пилотом; копия в репозитории —
studio-platform/workers/minsk/.
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/vova")
sys.path.insert(0, "/home/vova/VideoStudio/src")
import upload_puller as P                     # noqa: E402
import upload_coordination as coord           # noqa: E402
import gmail_forward_pilot as pilot           # noqa: E402  (JS, маска, реестр, фаза inbox)

mask = pilot.mask
login_for = pilot.login_for
uploader_on = pilot.uploader_on
REGISTRY = pilot.REGISTRY
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UC_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")


class ProfileBusy(RuntimeError):
    """Профиль занят (заливщик, открытый браузер, чужая бронь) — вернуться позже."""


def login_sha(e):
    return hashlib.sha256(str(e or "").strip().lower().encode("utf-8")).hexdigest()


def mask_text(text):
    """Снять адреса почты из свободного текста (сниппет «Security alert» повторяет
    почту аккаунта целиком)."""
    return EMAIL_RE.sub(lambda m: mask(m.group(0)), str(text or ""))


def ucs_for_login(login):
    """Все каналы этой почты по реестру — на любых профилях: письмо аккаунта относится
    ко всем его каналам, а не только к тем, что заведены на этом профиле."""
    d = json.load(open(REGISTRY))
    low = str(login or "").lower()
    out = []
    for b in d.get("bindings") or []:
        if str(b.get("login") or "").lower() == low:
            uc = str(b.get("channel_uc") or "").strip()
            if UC_RE.fullmatch(uc) and uc not in out:
                out.append(uc)
    return out


def profiles_with_bindings():
    d = json.load(open(REGISTRY))
    seen = {str(b.get("profile")) for b in (d.get("bindings") or []) if b.get("login")}
    return sorted(seen, key=lambda p: (0, int(p)) if p.isdigit() else (1, p))


def clean_rows(rows):
    """Строки списка → то, что едет в студию: без адресов в сниппете, с подсказками UC."""
    out = []
    for r in rows or []:
        subject = str(r.get("subject") or "")
        snippet = mask_text(r.get("snippet") or "")
        hits = []
        for uc in UC_RE.findall(subject + " " + str(r.get("snippet") or "")):
            if uc not in hits:
                hits.append(uc)
        out.append({"sender": str(r.get("sender") or "")[:200], "subject": subject[:500],
                    "snippet": snippet[:600], "date": str(r.get("date") or "")[:80],
                    "unread": bool(r.get("unread")), "uc_hints": hits})
    return out


def read_profile_inbox(profile, logins=None, evidence=None, max_authuser=None, settle=8):
    """Открыть профиль и снять списки писем для всех его почт. Возвращает запись:
    {profile, uid, results: [{login_masked, login_sha256, authuser, rows, error}],
     missing: [маски почт, которых в браузере не нашли], closed, error}.
    Бросает ProfileBusy, если профиль занят. Полных адресов в записи нет."""
    profile = str(profile)
    logins = [str(x).lower() for x in (logins if logins is not None else login_for(profile))]
    if not logins:
        raise ValueError(f"у профиля {profile} нет почт в реестре")
    max_authuser = max_authuser or max(4, len(logins) + 2)
    evidence = Path(evidence) if evidence else Path.home() / "mail_sweep" / "evidence"
    out = evidence / f"{profile}_{time.strftime('%Y%m%dT%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(evidence, 0o700)
    rec = {"profile": profile, "started": time.time(), "steps": [], "results": [],
           "missing": [], "evidence": str(out)}

    uid = P.resolve_user_id(profile)
    rec["uid"] = uid
    if uploader_on(profile):
        raise ProfileBusy("на профиле работает заливщик")
    if P.profile_browser_alive(P.ADSPOWER_API, uid):
        raise ProfileBusy("профиль уже открыт")

    driver, owned, original = None, False, []
    try:
        lease_cm = coord.profile_lease(profile, P.lease_context())
    except coord.ProfileLeaseError as exc:
        raise ProfileBusy(f"бронь не дана: {str(exc)[:120]}")
    try:
        with lease_cm as lease:
            def guard():
                if lease:
                    lease.assert_held()
            try:
                guard()
                resp = P._ads_get(P.ADSPOWER_API, "/api/v1/browser/start",
                                  {"user_id": uid, "open_tabs": 1, "ip_tab": 0, "headless": 0},
                                  timeout=180)
                if resp.get("code") != 0:
                    raise RuntimeError(f"browser/start: {str(resp.get('msg'))[:80]}")
                owned = True
                data = resp.get("data") or {}
                addr = (data.get("ws") or {}).get("selenium") or ""
                chromedriver = data.get("webdriver") or ""
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                opts = Options()
                opts.add_experimental_option("debuggerAddress", addr)
                driver = (webdriver.Chrome(service=Service(chromedriver), options=opts)
                          if chromedriver else webdriver.Chrome(options=opts))
                driver.set_page_load_timeout(90)
                driver.set_script_timeout(45)
                original = list(driver.window_handles)
                rec["original_tabs"] = len(original)
                guard()
                driver.switch_to.new_window("tab")
                # Какая почта под каким /u/N. Останавливаемся, когда нашли все свои или
                # Google увёл на вход (дальше номеров нет).
                found = {}
                for n in range(max_authuser):
                    guard()
                    driver.get(pilot.FWD_URL.format(n=n))
                    time.sleep(settle)
                    seen = (driver.execute_script(pilot.JS_EMAIL) or "").lower()
                    url = driver.current_url
                    rec["steps"].append({"authuser": n, "seen": mask(seen),
                                         "host": re.sub(r"^https?://([^/]+).*", r"\1", url)})
                    if "accounts.google.com" in url:
                        break
                    if seen in logins and seen not in found:
                        found[seen] = n
                    if len(found) == len(logins):
                        break
                rec["missing"] = [mask(e) for e in logins if e not in found]
                for email, n in found.items():
                    guard()
                    sub = out / f"u{n}"
                    sub.mkdir(exist_ok=True)
                    r = {"authuser": n, "steps": []}
                    try:
                        pilot.phase_inbox(driver, r, sub)
                        rows = json.loads((sub / "inbox_rows.json").read_text(encoding="utf-8"))
                    except Exception as exc:                       # noqa: BLE001
                        rows, r["error"] = [], f"{type(exc).__name__}: {str(exc)[:120]}"
                    rec["results"].append({
                        "login_masked": mask(email), "login_sha256": login_sha(email),
                        "authuser": n, "rows": clean_rows(rows),
                        "raw_rows": len(rows), "error": r.get("error") or r.get("inbox_error")})
                rec["result"] = "read" if rec["results"] else "account_not_found"
            except Exception as exc:                               # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            finally:
                try:
                    if driver is not None:
                        try:
                            pilot.accept_alert(driver, rec, "final_alert")
                            for h in list(driver.window_handles):
                                if h not in original:
                                    driver.switch_to.window(h)
                                    driver.close()
                        except Exception:                          # noqa: BLE001
                            pass
                        try:
                            driver.service.stop()
                        except Exception:                          # noqa: BLE001
                            pass
                    if owned:
                        P._ads_get(P.ADSPOWER_API, "/api/v1/browser/stop", {"user_id": uid}, timeout=60)
                        for _ in range(10):
                            time.sleep(2)
                            if not P.profile_browser_alive(P.ADSPOWER_API, uid):
                                rec["closed"] = True
                                break
                except Exception as exc:                           # noqa: BLE001
                    rec["close_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    except coord.ProfileLeaseError as exc:
        # Бронь не дали (чужой держатель) — это «занят», не ошибка чтения.
        raise ProfileBusy(f"бронь не дана: {str(exc)[:120]}")
    rec["finished"] = time.time()
    (out / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(out / "record.json", 0o600)
    return rec


if __name__ == "__main__":
    # Ручной прогон одного профиля: печатает запись без адресов почты.
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--email", default="")
    a = ap.parse_args()
    try:
        r = read_profile_inbox(a.profile, [a.email] if a.email else None)
    except ProfileBusy as exc:
        print(f"занят: {exc}")
        sys.exit(3)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0 if r.get("result") == "read" else 1)
