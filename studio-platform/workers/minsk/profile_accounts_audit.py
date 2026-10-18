#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка «реестр привязок ↔ что на самом деле вошло в профиль» — только чтение (18.10.2026).

Зачем. Ночной обход почты 18.10 не нашёл в браузере почту из реестра у 16 профилей; у
профиля 78 оба канала открываются, а привязка их аккаунта записана на профиль 25. Автовход
(«Verify it's you») ищет пару «профиль + канал → почта» в реестре; где реестр расходится с
фактом, заливщик встаёт на exact_binding_unavailable / AUTH_GATE. Владелец: «по входу делаем
обход по профилям».

Что делает за одно открытие профиля (бронь координатора, как у заливки и читалки почты):
  1. accounts.google.com/ListAccounts — какие аккаунты Google вошли в браузер профиля и в
     каком порядке (номер = authuser);
  2. для каждого аккаунта youtube.com/channel_switcher?authuser=N — какие каналы ему
     доступны (номера UC из страницы + названия), снимок страницы с замаскированными почтами;
  3. сверка: каналы студии этого профиля → под каким аккаунтом найдены; почты реестра
     этого профиля → есть ли в браузере; аккаунт браузера → записан ли в реестре, на каком
     профиле, есть ли у него пароль и секрет кода (только да/нет);
  4. предложение привязок: канал студии найден ровно под одним аккаунтом, привязки
     (профиль, канал) в реестре нет, а у аккаунта в реестре есть пароль и секрет →
     строка в ~/accounts_audit/proposed_bindings.csv (0600, на Минске; формат
     auth_bindings.py «профиль,канал,почта»). Сам реестр НЕ меняется.

Ничего не вводится, ничего в аккаунтах не меняется. Наружу (record.json, журнал) уходят
только маски `min…@gmail.com` и sha256 почт.

    python3 profile_accounts_audit.py 78                  # один профиль, печать записи
    python3 profile_accounts_audit.py --all [--loop-hours 6]   # все профили студии, занятые — позже
"""
import argparse
import csv
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
import gmail_forward_pilot as pilot           # noqa: E402

mask = pilot.mask
REGISTRY = pilot.REGISTRY
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UC_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
HOME = Path.home() / "accounts_audit"
STUDIO_CSV = HOME / "studio_channels.csv"          # профиль,канал,название — выгрузка из студии
PROPOSED = HOME / "proposed_bindings.csv"
LIST_URL = "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard"
SWITCHER_URL = "https://www.youtube.com/channel_switcher?authuser={n}"

# Почты на странице → маска до снимка (в снимке и тексте полных адресов не остаётся).
JS_MASK_EMAILS = r"""
var re = /([\w.+-]{3})[\w.+-]*@([\w-]+(?:\.[\w-]+)+)/g;
var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
var n; while ((n = w.nextNode())) { if (re.test(n.nodeValue)) { n.nodeValue = n.nodeValue.replace(re, '$1…@$2'); } re.lastIndex = 0; }
document.querySelectorAll('[aria-label*="@"],[title*="@"]').forEach(function(e){
  ['aria-label','title'].forEach(function(a){ var v=e.getAttribute(a); if (v) e.setAttribute(a, v.replace(re, '$1…@$2')); });
});
return true;
"""
JS_SWITCHER = r"""
var html = document.documentElement ? document.documentElement.innerHTML : '';
var ucs = []; (html.match(/UC[0-9A-Za-z_-]{22}/g) || []).forEach(function(u){ if (ucs.indexOf(u) < 0) ucs.push(u); });
var titles = [];
document.querySelectorAll('#channel-title, ytd-account-item-renderer #channel-title, ytd-account-item-renderer yt-formatted-string, .channel-switcher-item-title').forEach(function(e){
  var t = (e.innerText || '').trim(); if (t && titles.indexOf(t) < 0) titles.push(t);
});
var text = document.body ? document.body.innerText : '';
var email = ''; var m = text.match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/); if (m) email = m[0];
document.querySelectorAll('[aria-label*="@"]').forEach(function(e){ if (!email) { var mm=(e.getAttribute('aria-label')||'').match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/); if (mm) email=mm[0]; } });
return JSON.stringify({ucs: ucs, titles: titles, text: text.slice(0, 4000), email: email, url: location.href});
"""


def sha(e):
    return hashlib.sha256(str(e or "").strip().lower().encode("utf-8")).hexdigest()


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def studio_channels():
    """{профиль: [(uc, название)]} из выгрузки студии."""
    out = {}
    with open(STUDIO_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(str(row["профиль"]).strip(), []).append(
                (str(row["канал"]).strip(), str(row.get("название") or "").strip()))
    return out


def registry_view():
    d = json.load(open(REGISTRY, encoding="utf-8"))
    accounts = {}
    for a in d.get("accounts") or []:
        login = str(a.get("login") or "").strip().lower()
        if login:
            accounts[login] = bool(a.get("password")) and bool(a.get("totp") or a.get("totp_secret"))
    by_login, pairs = {}, set()
    for b in d.get("bindings") or []:
        login = str(b.get("login") or "").strip().lower()
        prof, uc = str(b.get("profile")), str(b.get("channel_uc") or "")
        by_login.setdefault(login, set()).add(prof)
        pairs.add((prof, uc))
    return accounts, by_login, pairs


def parse_list_accounts(text):
    """Тело ListAccounts → почты по порядку (порядок = authuser). Вышедшие из аккаунта
    тоже бывают в списке — их отличает флаг, но номер они всё равно занимают."""
    emails = []
    try:
        data = json.loads(text)
        for item in (data[1] if isinstance(data, list) and len(data) > 1 else []):
            e = next((x for x in item if isinstance(x, str) and EMAIL_RE.fullmatch(x)), "")
            signed_out = bool(item[9] == 0) if len(item) > 9 and isinstance(item[9], int) else False
            emails.append((e.lower(), signed_out))
    except (ValueError, TypeError, IndexError):
        emails = []
    if not emails:
        seen = []
        for e in EMAIL_RE.findall(text or ""):
            if e.lower() not in seen:
                seen.append(e.lower())
        emails = [(e, False) for e in seen]
    return emails


def audit_profile(profile, channels, reg, evidence_root=HOME, settle=7):
    profile = str(profile)
    accounts_reg, by_login, pairs = reg
    out_dir = Path(evidence_root) / f"{profile}_{time.strftime('%Y%m%dT%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(evidence_root, 0o700)
    rec = {"profile": profile, "started": time.time(), "accounts": [], "channels": [],
           "registry_missing": [], "proposed": 0, "evidence": str(out_dir)}
    uid = P.resolve_user_id(profile)
    rec["uid"] = uid
    if pilot.uploader_on(profile):
        raise RuntimeError("busy: на профиле работает заливщик")
    if P.profile_browser_alive(P.ADSPOWER_API, uid):
        raise RuntimeError("busy: профиль уже открыт")
    try:
        lease_cm = coord.profile_lease(profile, P.lease_context())
    except coord.ProfileLeaseError as exc:
        raise RuntimeError(f"busy: бронь не дана: {str(exc)[:120]}")
    driver, owned, original, full = None, False, [], []
    try:
        with lease_cm as lease:
            def guard():
                if lease:
                    lease.assert_held()
            try:
                guard()
                resp = P._ads_get(P.ADSPOWER_API, "/api/v1/browser/start",
                                  {"user_id": uid, "open_tabs": 1, "ip_tab": 0, "headless": 0}, timeout=180)
                if resp.get("code") != 0:
                    raise RuntimeError(f"browser/start: {str(resp.get('msg'))[:80]}")
                owned = True
                data = resp.get("data") or {}
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                opts = Options()
                opts.add_experimental_option("debuggerAddress", (data.get("ws") or {}).get("selenium") or "")
                chromedriver = data.get("webdriver") or ""
                driver = (webdriver.Chrome(service=Service(chromedriver), options=opts)
                          if chromedriver else webdriver.Chrome(options=opts))
                driver.set_page_load_timeout(90)
                driver.set_script_timeout(45)
                original = list(driver.window_handles)
                driver.switch_to.new_window("tab")
                guard()
                driver.get(LIST_URL)
                time.sleep(3)
                body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
                (out_dir / "list_accounts.txt").write_text(
                    EMAIL_RE.sub(lambda m: mask(m.group(0)), body)[:4000], encoding="utf-8")
                full = parse_list_accounts(body)
                rec["accounts_source"] = "ListAccounts" if full else ""
                if not full:
                    # Запасной путь читалки почты (проверен 18.10 на 23 профилях): Gmail
                    # /u/0…/u/N, почта аккаунта со страницы; увёл на вход — номеров больше нет.
                    for n in range(8):
                        guard()
                        driver.get(pilot.FWD_URL.format(n=n))
                        time.sleep(settle)
                        if "accounts.google.com" in driver.current_url:
                            break
                        seen = (driver.execute_script(pilot.JS_EMAIL) or "").lower()
                        if not seen or any(seen == e for e, _ in full):
                            break
                        full.append((seen, False))
                    rec["accounts_source"] = "gmail_u" if full else "none"
                for n, (email, signed_out) in enumerate(full):
                    guard()
                    acc = {"authuser": n, "login_masked": mask(email), "login_sha256": sha(email),
                           "signed_out": signed_out,
                           "in_registry": email in accounts_reg,
                           "registry_secrets": bool(accounts_reg.get(email)),
                           "registry_profiles": sorted(by_login.get(email, set()), key=lambda p: (len(p), p)),
                           "ucs": [], "titles": []}
                    if not signed_out:
                        try:
                            driver.get(SWITCHER_URL.format(n=n))
                            time.sleep(settle)
                            info = json.loads(driver.execute_script(JS_SWITCHER) or "{}")
                            acc["ucs"] = [u for u in info.get("ucs") or [] if UC_RE.fullmatch(u)]
                            acc["titles"] = [EMAIL_RE.sub(lambda m: mask(m.group(0)), t)[:120]
                                             for t in info.get("titles") or []][:30]
                            seen = str(info.get("email") or "").lower()
                            acc["page_login_matches"] = (seen == email) if seen else None
                            acc["page_host"] = re.sub(r"^https?://([^/]+).*", r"\1", str(info.get("url") or ""))
                            acc["_text"] = norm(info.get("text"))
                            driver.execute_script(JS_MASK_EMAILS)
                            driver.save_screenshot(str(out_dir / f"acc{n}.png"))
                        except Exception as exc:                       # noqa: BLE001
                            acc["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    rec["accounts"].append(acc)
                rec["result"] = "done" if full else "no_accounts"
            except Exception as exc:                                   # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            finally:
                try:
                    if driver is not None:
                        try:
                            for h in list(driver.window_handles):
                                if h not in original:
                                    driver.switch_to.window(h)
                                    driver.close()
                        except Exception:                              # noqa: BLE001
                            pass
                        try:
                            driver.service.stop()
                        except Exception:                              # noqa: BLE001
                            pass
                    if owned:
                        P._ads_get(P.ADSPOWER_API, "/api/v1/browser/stop", {"user_id": uid}, timeout=60)
                        for _ in range(10):
                            time.sleep(2)
                            if not P.profile_browser_alive(P.ADSPOWER_API, uid):
                                rec["closed"] = True
                                break
                except Exception as exc:                               # noqa: BLE001
                    rec["close_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    except coord.ProfileLeaseError as exc:
        raise RuntimeError(f"busy: бронь не дана: {str(exc)[:120]}")

    # ── сверка ──
    present = {e for e, _ in full}
    for login in sorted(l for l, profs in by_login.items() if profile in profs):
        if login and login not in present:
            rec["registry_missing"].append(mask(login))
    proposed = []
    for uc, name in channels:
        under = [a for a in rec["accounts"] if uc in a["ucs"]]
        by_name = [a for a in rec["accounts"] if name and norm(name) in a.get("_text", "")] if not under else []
        hits = under or by_name
        ch = {"uc": uc, "name": name, "bound": (profile, uc) in pairs,
              "found_under": [a["authuser"] for a in hits],
              "found_by": "uc" if under else ("name" if by_name else ""),
              "accounts": [{"authuser": a["authuser"], "login_masked": a["login_masked"],
                            "in_registry": a["in_registry"], "registry_secrets": a["registry_secrets"],
                            "registry_profiles": a["registry_profiles"]} for a in hits]}
        if not hits:
            ch["verdict"] = "канала нет ни под одним аккаунтом профиля"
        elif ch["bound"]:
            ch["verdict"] = "привязка есть"
        elif len(hits) > 1:
            ch["verdict"] = "канал под несколькими аккаунтами — нужен человек"
        elif not hits[0]["in_registry"]:
            ch["verdict"] = "аккаунта нет в реестре — нужны почта, пароль и секрет"
        elif not hits[0]["registry_secrets"]:
            ch["verdict"] = "аккаунт в реестре без пароля или секрета"
        else:
            ch["verdict"] = "можно привязать (предложено)"
            email = full[hits[0]["authuser"]][0]
            proposed.append((profile, uc, email))
        rec["channels"].append(ch)
    for a in rec["accounts"]:
        a.pop("_text", None)
    rec["proposed"] = len(proposed)
    if proposed:
        new = not PROPOSED.exists()
        with open(PROPOSED, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["профиль", "канал", "почта"])
            w.writerows(proposed)
        os.chmod(PROPOSED, 0o600)
    rec["finished"] = time.time()
    (out_dir / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(out_dir / "record.json", 0o600)
    return rec


def summary_line(rec):
    chs = rec.get("channels") or []
    bad = [c for c in chs if c.get("verdict") != "привязка есть"]
    return (f"профиль {rec['profile']}: аккаунтов {len(rec.get('accounts') or [])}, каналов студии {len(chs)}, "
            f"с привязкой {len(chs) - len(bad)}, предложено {rec.get('proposed', 0)}, "
            f"почт реестра нет в браузере {len(rec.get('registry_missing') or [])}"
            + (f", ошибка {rec['error']}" if rec.get("error") else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--loop-hours", type=float, default=0)
    ap.add_argument("--retry-min", type=float, default=10)
    a = ap.parse_args()
    HOME.mkdir(exist_ok=True)
    os.chmod(HOME, 0o700)
    chans = studio_channels()
    reg = registry_view()
    if a.profile and not a.all:
        try:
            rec = audit_profile(a.profile, chans.get(str(a.profile), []), reg)
        except RuntimeError as exc:
            print(f"занят: {exc}")
            return 3
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        print(summary_line(rec))
        return 0
    todo = sorted(chans, key=lambda p: (len(p), p))
    deadline = time.time() + a.loop_hours * 3600
    log = open(HOME / f"run_{time.strftime('%Y%m%dT%H%M%S')}.log", "a", encoding="utf-8", buffering=1)
    while todo:
        left = []
        for p in todo:
            try:
                rec = audit_profile(p, chans[p], reg)
                log.write(time.strftime("%H:%M ") + summary_line(rec) + "\n")
            except RuntimeError as exc:
                left.append(p)
                log.write(time.strftime("%H:%M ") + f"профиль {p}: {str(exc)[:120]}\n")
            except Exception as exc:                                   # noqa: BLE001
                log.write(time.strftime("%H:%M ") + f"профиль {p}: сбой {type(exc).__name__}: {str(exc)[:120]}\n")
        todo = left
        if not todo or time.time() > deadline:
            break
        log.write(time.strftime("%H:%M ") + f"заняты, жду {a.retry_min:g} мин: {' '.join(todo)}\n")
        time.sleep(a.retry_min * 60)
    log.write(time.strftime("%H:%M ") + "ГОТОВО" + (f"; не сняты: {' '.join(todo)}" if todo else "") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
