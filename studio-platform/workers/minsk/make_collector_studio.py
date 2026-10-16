#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать `collector_studio.py` — нашу копию сборщика Володи (15.10.2026).

Его `collector.py` не трогаем: правим копию рядом, в том же каталоге
(`~/adspower_audit_20261004/`), чтобы она видела те же `src/`, `tools/` и
`studio_reader.py`. Скрипт накладывает две правки на текущий `collector.py` и
падает, если якорь не найден — значит, Володя его поменял, и правку надо пересмотреть.

1. **Каналы студии, которых нет в списках почт** (`--expect-ucs UC1,UC2`). Сборщик
   берёт каналы только из списка каналов каждой почты профиля. Канал, к которому у
   почты доступ менеджера, туда не попадает — у Frontnaplók и Ostromok (профиль 54) и
   украинских каналов (профиль 73) часов не было ни разу, хотя заливка в них идёт.
   Такой канал открываем напрямую `studio.youtube.com/channel/UC?authuser=N` в каждой
   почте профиля, как это делает заливщик, и читаем тем же считывателем.

2. **«Studio за 28 дней»**. Studio открывает канал на дашборде; до того как
   считыватель уйдёт на «Монетизацию», читаем карточку «Channel analytics»:
   подписчики, просмотры и часы за 28 дней, лучшие ролики за 48 часов. Пишется в
   `reader_result.json` ключом `dashboard`; студия берёт его только при подтверждённой
   сессии (`server/pub_earn.py:_dash`).

    python3 make_collector_studio.py ~/adspower_audit_20261004/collector.py \\
        ~/adspower_audit_20261004/collector_studio.py
"""
from __future__ import annotations

import sys

DASH_CODE = r'''

# ── 15.10.2026: «Studio за 28 дней» и каналы студии вне списков почт ─────────────
# Тот же снимок, что у считывателя (`studio_reader.JS_ID`): какой канал открыт в Studio.
JS_ID = """
const a=document.querySelector('ytcp-app');
return {url:location.href,ready:document.readyState,
 uc:a&&a.channel?a.channel.channelId:null,
 entity_id:a&&a.currentEntity?a.currentEntity.id:null,
 entity_type:a&&a.currentEntity?a.currentEntity.type:null};
"""
JS_DASHBOARD = r"""
const norm=t=>(t||'').replace(/ /g,' ').trim();
function deep(r){let a=[];for(const e of r.querySelectorAll('*')){a.push(e);if(e.shadowRoot)a.push(...deep(e.shadowRoot));}return a;}
const cards=deep(document).filter(e=>/^(ytcd-card|ytcd-channel-analytics-card|ytcd-.*card)$/i.test(e.tagName));
let best=null;
for(const c of cards){const t=c.innerText||'';if(/Channel analytics/.test(t)&&/Current subscribers/.test(t)){if(!best||t.length<best.length)best=t;}}
if(!best){const b=document.body?document.body.innerText:'';const i=b.indexOf('Channel analytics');
 if(i>=0){const j=b.indexOf('Go to channel analytics',i);best=b.slice(i,j>i?j:i+1500);}}
return best;
"""


def _dash_number(raw):
    s = (raw or "").replace(",", "").replace(" ", "").strip()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMB]?)", s)
    if not m:
        return None
    value = float(m[1]) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m[2]]
    return int(value) if value.is_integer() and "." not in m[1] else round(value, 1)


def parse_dashboard(text):
    lines = [" ".join(l.split()) for l in (text or "").splitlines() if l.strip()]
    out = {"subs": None, "views_28": None, "hours_28": None, "top": [], "period": None}

    def after(label):
        for i, line in enumerate(lines):
            if line == label and i + 1 < len(lines):
                return _dash_number(lines[i + 1].split(" ")[0])
            if line.startswith(label + " "):
                return _dash_number(line[len(label) + 1:].split(" ")[0])
        return None

    out["subs"] = after("Current subscribers")
    out["views_28"] = after("Views")
    out["hours_28"] = after("Watch time (hours)")
    for line in lines:
        if line.lower().startswith("last ") and "days" in line.lower():
            out["period"] = line
            break
    if "Top content" in lines:
        i = lines.index("Top content") + 1
        while i < len(lines) and not lines[i].startswith("Go to channel analytics"):
            line = lines[i]
            m = re.fullmatch(r"(.+?)\s+([0-9][0-9,.]*[KMB]?)", line)
            if m and not line.lower().startswith("last "):
                out["top"].append({"title": m[1], "views": _dash_number(m[2])})
            elif (i + 1 < len(lines) and _dash_number(lines[i + 1]) is not None
                  and not line.lower().startswith("last ")):
                out["top"].append({"title": line, "views": _dash_number(lines[i + 1])})
                i += 1
            i += 1
    return out


def read_dashboard(driver, uc, channel_dir, assert_held, timeout=25):
    """Карточка «Channel analytics» дашборда — только если Studio открыта на этом UC."""
    deadline = time.monotonic() + timeout
    text, ident = None, None
    while time.monotonic() < deadline:
        assert_held()
        try:
            ident = driver.execute_script(JS_ID)
            text = driver.execute_script(JS_DASHBOARD)
        except Exception:
            text = None
        if (isinstance(ident, dict) and ident.get("uc") == uc
                and ident.get("entity_id") == uc and text and "Current subscribers" in text):
            parsed = parse_dashboard(text)
            if parsed["subs"] is not None or parsed["views_28"] is not None:
                parsed.update(uc=uc, read_at=now())
                save(channel_dir / "dashboard.json", {**parsed, "raw": text[:4000]})
                return parsed
        time.sleep(1.5)
    save(channel_dir / "dashboard.json", {"uc": uc, "status": "not_read",
                                          "ident": ident, "raw": (text or "")[:2000]})
    return None


def _attach_dashboard(item, dash, channel_dir):
    result = item.get("result")
    if dash and isinstance(result, dict):
        result["dashboard"] = dash
        save(channel_dir / "reader_result.json", result)


def open_direct(driver, uc, au, assert_held, timeout=30):
    """Открыть канал в Studio под почтой N. ok / need_auth / no_access."""
    assert_held()
    driver.get(f"https://studio.youtube.com/channel/{uc}?authuser={au}")
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        assert_held()
        try:
            last = driver.execute_script(JS_ID)
        except Exception:
            last = None
        url = (last or {}).get("url") or ""
        if "accounts.google." in url:
            return "need_auth", last
        if (last and last.get("ready") == "complete" and last.get("uc") == uc
                and last.get("entity_id") == uc and last.get("entity_type") == "CHANNEL"):
            return "ok", last
        if last and last.get("uc") and last.get("uc") != uc and last.get("ready") == "complete":
            return "no_access", last
        time.sleep(1)
    return "no_access", last


def read_expected_channels(driver, reader, legacy, rec, directory, journal, assert_held,
                           options, expect_ucs):
    """Каналы студии, которых ни одна почта профиля не показала в своём списке."""
    listed = {c["uc"] for a in rec.get("accounts", []) for c in a.get("channels", []) or []}
    done = {c["uc"] for c in rec.get("channel_results", []) if reusable_metrics(c)}
    rec["expected_direct"] = []
    for uc in expect_ucs:
        if uc in listed or uc in done:
            continue
        tried = []
        for account in rec.get("accounts", []):
            if account.get("status") != "ok" or not account.get("email"):
                continue
            au = account["authuser"]
            channel_dir = directory / "channels" / f"authuser_{au:02d}" / uc
            try:
                state, ident = open_direct(driver, uc, au, assert_held)
            except Exception as exc:
                state, ident = "error", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            tried.append({"authuser": au, "email": account["email"], "state": state,
                          "seen_uc": (ident or {}).get("uc"), "url": (ident or {}).get("url")})
            # 16.10.2026: снимок экрана на каждую почту — доказательство для операторов,
            # что Studio под этой почтой канал не открывает (а не «скрипт сломался»).
            try:
                channel_dir.mkdir(parents=True, exist_ok=True)
                driver.save_screenshot(str(channel_dir / f"open_{state}.png"))
            except Exception:
                pass
            if state != "ok":
                continue
            context = {"profile": rec["profile"]["serial"], "user_id": rec["profile"]["user_id"],
                       "authuser": au, "email": account["email"],
                       "session_index": account.get("session_index"), "access": "direct"}
            save(channel_dir / "context.json", context)
            item = {"uc": uc, "context": context, "status": "reading", "direct": True}
            rec["channel_results"].append(item)
            dash = None
            try:
                dash = read_dashboard(driver, uc, channel_dir, assert_held)
                item["result"] = reader.read_channel(driver, uc, str(channel_dir),
                                                     {**options, "context": context,
                                                      "assert_held": assert_held})
                item["status"] = "reader_returned"
            except Exception as exc:
                item.update(status="error", error={"type": type(exc).__name__,
                                                   "message": str(exc)[:1000]})
            _attach_dashboard(item, dash, channel_dir)
            save(channel_dir / "collector_result.json", item)
            journal.checkpoint(directory, rec, "expected_channel_done")
            if reusable_metrics(item):
                break
        rec["expected_direct"].append({"uc": uc, "tried": tried})
    journal.checkpoint(directory, rec, "expected_channels_done")
'''


def main(src: str, dst: str) -> int:
    s = open(src, encoding="utf-8").read()

    def rep(a, b):
        nonlocal s
        if s.count(a) != 1:
            raise SystemExit(f"якорь не найден или не один: {a[:70]!r}")
        s = s.replace(a, b)

    # 1) код — перед completion()
    rep("\ndef completion(rec):", DASH_CODE + "\n\ndef completion(rec):")
    # 2) дашборд в обычном пути: между открытием канала и считывателем
    rep('''                    driver.get(f"https://studio.youtube.com/channel/{uc}?authuser={au}")
                    item["result"] = reader.read_channel(driver, uc, str(channel_dir),
                                                         {**options, "context": context, "assert_held": assert_held})
                    item["status"] = "reader_returned"''',
        '''                    driver.get(f"https://studio.youtube.com/channel/{uc}?authuser={au}")
                    _dash = read_dashboard(driver, uc, channel_dir, assert_held)
                    item["result"] = reader.read_channel(driver, uc, str(channel_dir),
                                                         {**options, "context": context, "assert_held": assert_held})
                    item["status"] = "reader_returned"
                    _attach_dashboard(item, _dash, channel_dir)''')
    # 3) после чтения списков — каналы студии напрямую
    rep('''                read_all_channels(driver, reader, legacy, rec, directory, journal, guard, options,
                                  metric_cache, getattr(args, "metrics_cache_root", None))''',
        '''                read_all_channels(driver, reader, legacy, rec, directory, journal, guard, options,
                                  metric_cache, getattr(args, "metrics_cache_root", None))
                if getattr(args, "expect_ucs", None):
                    read_expected_channels(driver, reader, legacy, rec, directory, journal, guard,
                                           options, args.expect_ucs)''')
    # 3а) 16.10.2026: второй хост AdsPower (macbook-pro-4) выключен — сборщик вставал целиком.
    # С --peer-offline-ok недоступный peer считается пустым; доступный — проверяется как раньше.
    rep('''                    raise StopRun(f"Host {name} active-list unavailable ({type(exc).__name__})") from None''',
        '''                    if name == "peer" and globals().get("PEER_OFFLINE_OK"):
                        print(f"peer offline ({type(exc).__name__}): считаем пустым (--peer-offline-ok)",
                              flush=True)
                        break
                    raise StopRun(f"Host {name} active-list unavailable ({type(exc).__name__})") from None''')
    # 4) ключ командной строки
    rep('''    parser.add_argument("--max-authuser", type=int, choices=range(20), default=19)''',
        '''    parser.add_argument("--max-authuser", type=int, choices=range(20), default=19)
    parser.add_argument("--expect-ucs", default="",
                        help="UC каналов студии на профиле через запятую: открыть напрямую тех, "
                             "кого нет в списках почт (доступ менеджера)")
    parser.add_argument("--peer-offline-ok", action="store_true",
                        help="второй хост AdsPower недоступен — не останавливаться (только если "
                             "владелец подтвердил, что профили на нём закрыты)")''')
    rep('''    args.api, args.peer_api = origin(args.api), origin(args.peer_api)''',
        '''    args.api, args.peer_api = origin(args.api), origin(args.peer_api)
    args.expect_ucs = [u.strip() for u in (args.expect_ucs or "").split(",")
                       if UC_RE.match(u.strip())]
    globals()["PEER_OFFLINE_OK"] = bool(args.peer_offline_ok)''')
    s = s.replace('"""Isolated, resumable AdsPower inventory; never modifies a registry or profile.',
                  '"""Isolated, resumable AdsPower inventory; never modifies a registry or profile.\n\n'
                  'STUDIO COPY (15.10.2026): built by studio-platform/workers/minsk/'
                  'make_collector_studio.py from collector.py — do not edit by hand.', 1)
    open(dst, "w", encoding="utf-8").write(s)
    print(f"собрано: {dst}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
