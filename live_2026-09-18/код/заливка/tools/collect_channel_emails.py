#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Почты Google-аккаунтов, на которых стоят наши каналы → в реестр каналов.

Зачем: реестр знает «канал → профиль AdsPower», но не знает, на какой почте канал
заведён. Без почты нельзя восстановить доступ, понять, какие каналы падают вместе
(один аккаунт = одна блокировка) и куда придёт письмо о приглашении в коллаборацию.

Почта нигде не лежит офлайн: ни в метаданных AdsPower (поле username пустое у всех),
ни в Preferences/Login Data профиля. Единственный источник — живая сессия:
`youtube.com/account?authuser=N` печатает «Signed in as <почта>».

ГЛАВНОЕ ПРАВИЛО. Профиль AdsPower открывается ТОЛЬКО если он не открыт ни на этой
машине, ни на соседней. Профили общие (облако AdsPower), у Mac и минского бокса
одна учётка. Вторая сессия в том же профиле рвёт куки и приводит к «Verify it's
you» у Google, а если в профиле в этот момент идёт заливка — она ломается на
середине мастера. Поэтому перед КАЖДЫМ профилем опрашиваются все хосты из
--hosts; недоступный хост — это стоп, а не «наверное, там пусто».

    # кто где открыт (ничего не открывает)
    python3 collect_channel_emails.py check

    # сбор (запускать на машине с AdsPower; селениум цепляется к 127.0.0.1)
    python3 collect_channel_emails.py collect --profiles 28,29 --out /tmp/emails.json
    python3 collect_channel_emails.py collect --from-db --out /tmp/emails.json

    # запись в реестр (на машине с базой)
    python3 collect_channel_emails.py merge --in /tmp/emails.json --apply
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

DB = os.path.expanduser("~/ops/registry.db")          # боевой реестр контура
EMAILS_JSON = os.path.expanduser("~/ops/channel_emails.json")
BOOK = os.path.expanduser("~/video_studio_channels.json")   # историческая книга заливки
# все машины, где может быть открыт профиль. Порядок неважен, важна полнота.
HOSTS_DEFAULT = "local=http://localhost:50325,minsk=http://100.75.222.76:50325,mac=http://100.100.244.89:50325"
MAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UC_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


# ─────────────────────────── AdsPower ───────────────────────────

def _requests():
    # Lazy import: offline merge still needs neither requests nor any API call.
    src = str(Path(__file__).resolve().parents[1] / 'src')
    if src not in sys.path:
        sys.path.insert(0, src)
    import adspower_rate_limit
    return adspower_rate_limit


def _api_json(response, *, listing=False):
    value = response.json()
    if os.environ.get('ADSPOWER_RATE_LIMIT_DIR'):
        if (response.status_code != 200 or not isinstance(value, dict)
                or type(value.get('code')) is not int):
            raise RuntimeError('AdsPower response schema/status is invalid')
        if value['code'] == 0 and listing:
            data = value.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('list'), list):
                raise RuntimeError('AdsPower active/profile list is incomplete')
            if any(not isinstance(item, dict) or not isinstance(item.get('user_id'), str)
                   or not item['user_id'] for item in data['list']):
                raise RuntimeError('AdsPower active/profile list identity is invalid')
    return value


def hosts_from(arg: str) -> dict:
    out = {}
    for part in (arg or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, url = part.partition("=")
        out[name] = url or name
    return out


def ads_param(pid: str) -> dict:
    pid = str(pid)
    return {"serial_number": pid} if pid.isdigit() else {"user_id": pid}


def profile_index(api: str) -> dict:
    """serial_number → user_id и обратно."""
    r = _requests().get(f"{api}/api/v1/user/list", params={"page": 1, "page_size": 500}, timeout=40)
    d = _api_json(r, listing=True)
    if d.get("code") != 0:
        raise RuntimeError(f"user/list: {d.get('msg')}")
    by_serial, by_uid = {}, {}
    for p in (d.get("data") or {}).get("list") or []:
        by_serial[str(p.get("serial_number"))] = p.get("user_id")
        by_uid[p.get("user_id")] = str(p.get("serial_number"))
    return {"by_serial": by_serial, "by_uid": by_uid}


def uid_of(pid: str, idx: dict) -> str | None:
    pid = str(pid)
    return idx["by_serial"].get(pid) if pid.isdigit() else pid


def busy_everywhere(hosts: dict, allow_unreachable: bool = False) -> tuple[dict, list]:
    """{user_id: [хосты, где открыт]}. Недоступный хост — громкая ошибка."""
    busy: dict[str, list] = {}
    dead: list[str] = []
    for name, api in hosts.items():
        try:
            d = _api_json(_requests().get(f"{api}/api/v1/browser/local-active", timeout=12), listing=True)
            if d.get("code") != 0:
                raise RuntimeError(d.get("msg"))
            for x in (d.get("data") or {}).get("list") or []:
                busy.setdefault(x.get("user_id"), []).append(name)
        except Exception as e:
            dead.append(f"{name} ({api}): {e}")
    if dead and not allow_unreachable:
        raise SystemExit("[СТОП] не опрошены хосты — нельзя утверждать, что профиль свободен:\n  "
                         + "\n  ".join(dead))
    return busy, dead


def local_upload_procs() -> dict:
    """{profile: строка процесса} — заливки, идущие на этой машине."""
    out = {}
    try:
        result = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=20)
        if os.environ.get('ADSPOWER_RATE_LIMIT_DIR') and result.returncode != 0:
            raise RuntimeError('Local uploader process scan failed')
        ps = result.stdout
    except Exception:
        if os.environ.get('ADSPOWER_RATE_LIMIT_DIR'):
            raise RuntimeError('Local uploader process scan is incomplete') from None
        return out
    for line in ps.splitlines():
        if "upload_worker.py" not in line and "upload_manager" not in line:
            continue
        m = re.search(r"--profile\s+(\S+)", line)
        if m:
            out[m.group(1)] = line.strip()[:160]
    return out


# ─────────────────────────── сбор ───────────────────────────

JS_SESSION = r"""
var d = (window.ytcfg && ytcfg.data_) || {};
return JSON.stringify({
  url: location.href,
  name: d.USER_ACCOUNT_NAME || '',
  idx: String(d.SESSION_INDEX === undefined ? '' : d.SESSION_INDEX),
  sid: d.USER_SESSION_ID || '',
  logged: !!d.LOGGED_IN,
  text: document.body ? document.body.innerText.slice(0, 4000) : ''
});
"""

# Список каналов аккаунта (включая бренд-каналы) — innertube accounts_list.
# Проверено живьём 12.08.2026 на профиле 28: browseId в ответе НЕТ, id канала лежит
# в offlineCacheKeyToken.clientCacheKey БЕЗ префикса UC (`-pM2CMh…` → `UC-pM2CMh…`).
JS_ACCOUNTS_LIST = r"""
const cb = arguments[arguments.length-1];
const au = arguments[0];
(async()=>{
  try{
    function ck(re){ const m=document.cookie.match(re); return m?m[1]:''; }
    const SAPISID = ck(/SAPISID=([^;]+)/)||ck(/__Secure-3PAPISID=([^;]+)/)||ck(/__Secure-1PAPISID=([^;]+)/);
    const origin='https://www.youtube.com'; const ts=Math.floor(Date.now()/1000);
    const buf=await crypto.subtle.digest('SHA-1', new TextEncoder().encode(ts+' '+SAPISID+' '+origin));
    const hex=Array.from(new Uint8Array(buf)).map(b=>b.toString(16).padStart(2,'0')).join('');
    const key=ytcfg.get('INNERTUBE_API_KEY'); const ctx=ytcfg.get('INNERTUBE_CONTEXT');
    const r=await fetch('/youtubei/v1/account/accounts_list?key='+key+'&prettyPrint=false',{method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'SAPISIDHASH '+ts+'_'+hex,
               'X-Origin':origin,'X-Goog-AuthUser':String(au)},
      credentials:'include', body:JSON.stringify({context:ctx})});
    cb(JSON.stringify({status:r.status, body: await r.text()}));
  }catch(e){ cb('ERR:'+e.toString()); }
})();
"""


def open_profile(api: str, pid: str, tries: int = 3):
    rq = _requests()
    err = None
    for att in range(1, tries + 1):
        try:
            d = _api_json(rq.get(f"{api}/api/v1/browser/start",
                       params={**ads_param(pid), "open_tabs": 1, "ip_tab": 0, "headless": 0},
                       timeout=180))
            if d.get("code") == 0:
                return d["data"]["ws"]["selenium"], d["data"].get("webdriver", "")
            err = d.get("msg")
        except Exception as e:
            err = str(e)
        time.sleep(5 * att)
    raise RuntimeError(f"browser/start: {err}")


def close_profile(api: str, pid: str):
    try:
        _requests().get(f"{api}/api/v1/browser/stop", params=ads_param(pid), timeout=60)
    except Exception as e:
        print(f"    ! закрыть профиль не удалось: {e}", flush=True)


def read_session(driver, au: int, wait: int = 25) -> dict | None:
    """Сессия authuser=N: почта + имя аккаунта. None — сессии нет."""
    try:
        driver.get(f"https://www.youtube.com/account?authuser={au}")
    except Exception as e:
        print(f"    authuser={au}: страница не открылась ({str(e)[:60]})", flush=True)
        return None
    deadline = time.time() + wait
    last = {}
    while time.time() < deadline:
        time.sleep(2)
        try:
            last = json.loads(driver.execute_script(JS_SESSION))
        except Exception:
            continue
        if "accounts.google" in (last.get("url") or "") or "ServiceLogin" in (last.get("url") or ""):
            return None
        mails = MAIL_RE.findall(last.get("text") or "")
        if mails and last.get("name"):
            return {"authuser": au, "email": mails[0], "name": last.get("name"),
                    "session_index": last.get("idx"), "session_id": last.get("sid")}
    if not last.get("name"):
        return None
    # сессия есть (ytcfg отдал имя), а почту страница не показала — так и запишем
    return {"authuser": au, "email": None, "name": last.get("name"),
            "session_index": last.get("idx"), "session_id": last.get("sid"),
            "note": "страница /account не показала почту"}


def _items_of(node, out: list):
    """Рекурсивно собрать accountItem из ответа accounts_list."""
    if isinstance(node, dict):
        if "accountItem" in node and isinstance(node["accountItem"], dict):
            out.append(node["accountItem"])
        for v in node.values():
            _items_of(v, out)
    elif isinstance(node, list):
        for v in node:
            _items_of(v, out)


def _txt(node) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("simpleText"):
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []) if isinstance(r, dict))


def read_channels(driver, au: int) -> list:
    """Каналы (в т.ч. бренд-каналы) Google-аккаунта, под которым сейчас страница."""
    try:
        raw = driver.execute_async_script(JS_ACCOUNTS_LIST, au)
    except Exception as e:
        print(f"    accounts_list authuser={au}: {str(e)[:70]}", flush=True)
        return []
    if isinstance(raw, str) and raw.startswith("ERR:"):
        print(f"    accounts_list authuser={au}: {raw[:100]}", flush=True)
        return []
    try:
        m = json.loads(raw)
        j = json.loads(m.get("body") or "{}")
    except Exception as e:
        print(f"    accounts_list authuser={au}: ответ не разобран ({str(e)[:60]})", flush=True)
        return []
    items: list = []
    _items_of(j, items)
    out, seen = [], set()
    for it in items:
        key = ""
        for tok in (it.get("serviceEndpoint") or {}).get("selectActiveIdentityEndpoint", {}).get("supportedTokens", []):
            if isinstance(tok, dict) and (tok.get("offlineCacheKeyToken") or {}).get("clientCacheKey"):
                key = tok["offlineCacheKeyToken"]["clientCacheKey"]
                break
        uc = ("UC" + key) if key else ""
        if not UC_RE.match(uc) or uc in seen:
            continue
        seen.add(uc)
        out.append({"uc": uc,
                    "name": _txt(it.get("accountName")),
                    "handle": _txt(it.get("channelHandle")),
                    "byline": _txt(it.get("accountByline")),
                    "selected": bool(it.get("isSelected"))})
    return out


def collect(args):
    hosts = hosts_from(args.hosts)
    api = args.api
    idx = profile_index(api)
    profiles = [p.strip() for p in (args.profiles or "").split(",") if p.strip()]
    if args.from_db:
        con = sqlite3.connect(args.db)
        profiles = [r[0] for r in con.execute(
            "SELECT DISTINCT profile_id FROM channels WHERE profile_id IS NOT NULL ORDER BY 1")]
        con.close()
    if not profiles:
        raise SystemExit("нечего собирать: задай --profiles или --from-db")

    print(f"профилей к обходу: {len(profiles)} → {', '.join(map(str, profiles))}", flush=True)
    results, skipped = [], []
    for n, pid in enumerate(profiles, 1):
        uid = uid_of(pid, idx)
        if not uid:
            skipped.append((pid, "нет такого профиля в AdsPower"))
            print(f"[{n}/{len(profiles)}] {pid}: нет в AdsPower — пропуск", flush=True)
            continue
        # ── гейт: профиль не должен быть открыт нигде ──
        busy, _ = busy_everywhere(hosts, args.allow_unreachable)
        procs = local_upload_procs()
        where = busy.get(uid)
        if where:
            skipped.append((pid, f"открыт на: {', '.join(where)}"))
            print(f"[{n}/{len(profiles)}] {pid} ({uid}): ОТКРЫТ на {', '.join(where)} — не трогаю", flush=True)
            continue
        if str(pid) in procs or (idx["by_uid"].get(uid) or "") in procs:
            skipped.append((pid, "идёт заливка"))
            print(f"[{n}/{len(profiles)}] {pid}: идёт заливка — не трогаю", flush=True)
            continue

        print(f"[{n}/{len(profiles)}] профиль {pid} ({uid}) — открываю", flush=True)
        try:
            sel, drv = open_profile(api, pid)
        except Exception as e:
            skipped.append((pid, f"не открылся: {e}"))
            print(f"    ! {e}", flush=True)
            continue

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        opts = Options()
        opts.add_experimental_option("debuggerAddress", sel)
        driver = None
        rec = {"profile": str(pid), "user_id": uid, "ts": time.time(), "accounts": []}
        try:
            driver = webdriver.Chrome(service=Service(drv), options=opts) if drv else webdriver.Chrome(options=opts)
            driver.set_page_load_timeout(90)
            driver.set_script_timeout(60)
            seen_sessions = set()
            for au in range(0, args.max_authuser + 1):
                s = read_session(driver, au)
                if not s:
                    break
                # несуществующий authuser YouTube молча подменяет нулевым —
                # ловим по повтору сессии, иначе тот же аккаунт запишется N раз
                fp = s.get("session_id") or s.get("email") or s.get("name")
                if fp in seen_sessions:
                    break
                seen_sessions.add(fp)
                s["channels"] = read_channels(driver, au)
                rec["accounts"].append(s)
                print(f"    authuser={au}: {s.get('email') or '—'} ({s.get('name')}), "
                      f"каналов: {len(s['channels'])}", flush=True)
        except Exception as e:
            rec["error"] = str(e)[:200]
            print(f"    ! {str(e)[:160]}", flush=True)
        finally:
            # ⚠️ driver.quit() при debuggerAddress убивает профиль — закрываем только через API
            close_profile(api, pid)
        results.append(rec)
        if args.out:
            json.dump({"results": results, "skipped": skipped}, open(args.out, "w"),
                      ensure_ascii=False, indent=2)
        time.sleep(args.pause)

    print(f"\nсобрано профилей: {len(results)}, пропущено: {len(skipped)}", flush=True)
    for pid, why in skipped:
        print(f"  пропуск {pid}: {why}", flush=True)
    if args.out:
        print(f"результат: {args.out}", flush=True)


# ─────────────────────────── запись в реестр ───────────────────────────

def ensure_columns(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(channels)")}
    if "email" not in cols:
        con.execute("ALTER TABLE channels ADD COLUMN email TEXT")
    if "email_checked" not in cols:
        con.execute("ALTER TABLE channels ADD COLUMN email_checked REAL")


def merge(args):
    """Сырые снимки профилей → снимок каналов (channel_emails.json) + колонка в реестре."""
    accounts, seen_acc = [], set()
    for path in args.inp:
        data = json.load(open(path, encoding="utf-8"))
        for rec in data.get("results", []):
            prof = str(rec.get("profile"))
            for acc in rec.get("accounts", []):
                key = (prof, acc.get("session_id") or acc.get("email"))
                if key in seen_acc:
                    continue
                seen_acc.add(key)
                accounts.append({"profile_id": prof, "authuser": acc.get("authuser"),
                                 "email": acc.get("email"), "account": acc.get("name"),
                                 "channels": acc.get("channels") or [], "ts": rec.get("ts")})

    chans = {}
    for acc in accounts:
        if not acc.get("email"):
            continue
        for ch in acc["channels"]:
            chans[ch["uc"]] = {"email": acc["email"], "profile_id": acc["profile_id"],
                               "account": acc["account"], "authuser": acc["authuser"],
                               "name": ch.get("name"), "handle": ch.get("handle"),
                               "checked": acc.get("ts")}

    con = sqlite3.connect(args.db)
    ensure_columns(con)
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT uc,name,profile_id FROM channels")}
    scanned_profiles = {a["profile_id"] for a in accounts}

    matched = {uc: v for uc, v in chans.items() if uc in known}
    extra = {uc: v for uc, v in chans.items() if uc not in known}
    unmatched = [(uc, nm, p) for uc, (nm, p) in known.items()
                 if str(p) in scanned_profiles and uc not in matched]

    print(f"обойдено профилей: {len(scanned_profiles)}, аккаунтов: {len(accounts)}")
    print(f"каналов реестра с найденной почтой: {len(matched)} из {len(known)}")
    print(f"каналов реестра без почты на обойденных профилях: {len(unmatched)}")
    for uc, nm, prof in unmatched[:25]:
        print(f"   без почты: профиль {prof:>4} {uc} {nm}")
    if extra:
        print(f"каналов в аккаунтах, которых нет в реестре: {len(extra)} "
              f"(в основном личные каналы аккаунтов)")
        for uc, v in list(extra.items())[:8]:
            print(f"      профиль {v['profile_id']:>4} {uc} {v['name']} → {v['email']}")

    if not args.apply:
        print("\nсухой прогон. записать: добавь --apply")
        con.close()
        return

    if args.emails_out:
        payload = {"updated": time.time(),
                   "_": "снято с живых профилей: youtube.com/account?authuser=N + accounts_list. "
                        "Источник для build_registry.py (kind=live-scan), правится только пересъёмкой.",
                   "channels": chans, "accounts": accounts}
        tmp = args.emails_out + ".tmp"
        json.dump(payload, open(tmp, "w"), ensure_ascii=False, indent=2)
        os.replace(tmp, args.emails_out)
        print(f"✓ снимок аккаунтов: {args.emails_out} ({len(chans)} каналов)")

    bak = f"{args.db}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(args.db, bak)
    print(f"бэкап базы: {bak}")
    now = time.time()
    for uc, v in matched.items():
        con.execute("UPDATE channels SET email=?, email_checked=? WHERE uc=?", (v["email"], now, uc))
    con.commit()
    n = con.execute("SELECT count(*) FROM channels WHERE email IS NOT NULL").fetchone()[0]
    tot = con.execute("SELECT count(*) FROM channels").fetchone()[0]
    con.close()
    print(f"✓ почта проставлена у {n} каналов из {tot}")

    if args.book and os.path.isfile(BOOK):
        b = json.load(open(BOOK, encoding="utf-8"))
        touched = 0
        for _k, v in (b.get("channels") or {}).items():
            m = re.search(r"(UC[0-9A-Za-z_-]{22})", str(v.get("channel_url") or ""))
            if m and m.group(1) in matched:
                v["email"] = matched[m.group(1)]["email"]
                touched += 1
        if touched:
            shutil.copy2(BOOK, f"{BOOK}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
            tmp = BOOK + ".tmp"
            json.dump(b, open(tmp, "w"), ensure_ascii=False, indent=2)
            os.replace(tmp, BOOK)
            print(f"✓ адресная книга: почта проставлена у {touched} каналов")


def check(args):
    hosts = hosts_from(args.hosts)
    busy, dead = busy_everywhere(hosts, allow_unreachable=True)
    dead_names = {d.split(" ", 1)[0] for d in dead}
    for name, api in hosts.items():
        print(f"{name:6} {api:34} {'недоступен' if name in dead_names else 'опрошен'}")
    if dead:
        print("\n⚠ не опрошены (открывать профили НЕЛЬЗЯ, пока не ясно):")
        for d in dead:
            print("   " + d)
    if not busy:
        print("\nоткрытых профилей нет ни на одной машине")
    else:
        idx = None
        try:
            idx = profile_index(args.api)
        except Exception:
            pass
        print(f"\nоткрыты сейчас ({len(busy)}):")
        for uid, where in busy.items():
            serial = (idx or {}).get("by_uid", {}).get(uid, "?")
            print(f"   {uid} (профиль {serial}) — {', '.join(where)}")
    procs = local_upload_procs()
    if procs:
        print(f"\nзаливки на этой машине ({len(procs)}):")
        for p, line in procs.items():
            print(f"   профиль {p}: {line[:120]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="кто где открыт")
    c.add_argument("--hosts", default=os.getenv("VS_ADS_HOSTS", HOSTS_DEFAULT))
    c.add_argument("--api", default=os.getenv("ADSPOWER_API", "http://localhost:50325"))
    c.set_defaults(fn=check)

    g = sub.add_parser("collect", help="обойти профили и снять почты")
    g.add_argument("--profiles", default="")
    g.add_argument("--from-db", action="store_true")
    g.add_argument("--db", default=DB)
    g.add_argument("--out", default="")
    g.add_argument("--hosts", default=os.getenv("VS_ADS_HOSTS", HOSTS_DEFAULT))
    g.add_argument("--api", default=os.getenv("ADSPOWER_API", "http://localhost:50325"))
    g.add_argument("--max-authuser", type=int, default=4)
    g.add_argument("--pause", type=float, default=6.0)
    g.add_argument("--allow-unreachable", action="store_true",
                   help="не требовать ответа от всех машин (по умолчанию требуется)")
    g.set_defaults(fn=collect)

    m = sub.add_parser("merge", help="записать почты в реестр")
    m.add_argument("--in", dest="inp", action="append", required=True,
                   help="сырой снимок collect (можно несколько раз — по снимку с машины)")
    m.add_argument("--db", default=DB)
    m.add_argument("--emails-out", default=EMAILS_JSON,
                   help="снимок каналов для пересборки реестра (build_registry.py)")
    m.add_argument("--apply", action="store_true")
    m.add_argument("--book", action="store_true", help="продублировать в ~/video_studio_channels.json")
    m.set_defaults(fn=merge)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
