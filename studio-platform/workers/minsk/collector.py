#!/usr/bin/env python3
"""Isolated, resumable AdsPower inventory; never modifies a registry or profile.

Runtime copies required beside this file: tools/collect_channel_emails.py,
src/adspower_rate_limit.py, src/upload_coordination.py. No production imports.
Run on the AdsPower host; do not run two collectors in the same output directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

HERE = Path(__file__).resolve().parent
UC_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class StopRun(RuntimeError):
    """Unknown safety state: preserve evidence and do not start another profile."""


class AdsPowerAPIError(StopRun):
    def __init__(self, path, code, message):
        self.path, self.code, self.message = path, code, message
        super().__init__(f"AdsPower {path} returned code={code}; msg={message!r}")


class _StartBlockedProxy(Exception):
    """Internal exit after proven inactivity; run normal finalization and lease release."""


def now():
    return datetime.now(timezone.utc).isoformat()


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def load_module(name, path):
    if not path.is_file():
        raise RuntimeError(f"Required isolated module missing: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def save(path, value):
    """Backup before replacement, atomic JSON and fsync, including directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Journal:
    def __init__(self, output):
        self.output = output
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.stage = "starting"
        self.profile = None
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()

    def append(self, filename, value):
        with self.lock, (self.output / filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def event(self, kind, **fields):
        value = {"at": now(), "event": kind, "profile": self.profile, **fields}
        self.append("events.jsonl", value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def checkpoint(self, directory, record, stage):
        self.stage = stage
        record["updated_at"] = now()
        record["stage"] = stage
        save(directory / "checkpoints" / (stamp() + "_" + stage + ".json"), record)
        save(directory / "record.json", record)
        self.event("checkpoint", stage=stage)

    def heartbeat(self):
        while not self.stop.is_set():
            self.append("heartbeat.jsonl", {"at": now(), "pid": os.getpid(),
                                         "profile": self.profile, "stage": self.stage})
            self.stop.wait(15)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)


def origin(value):
    value = value if "://" in value else "http://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("API must be an HTTP(S) origin without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("API must be an origin, not a URL path")
    return value.rstrip("/")


def safe_api_message(value):
    """Keep a bounded diagnostic, not echoed request objects or credentials."""
    if not isinstance(value, str):
        return "[non-text message omitted]" if value is not None else "[message missing]"
    message = value[:4096]
    # Upstream sometimes appends its full JSON request; never retain it.
    message = re.split(r"[\{\[]", message, maxsplit=1)[0]
    message = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+", "[url]", message)
    message = re.sub(r"[^\s,;]+@[^\s,;]+", "[account/address]", message)
    message = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|token|access_token|refresh_token|cookie|authorization|"
        r"username|proxy_user|proxy_username|proxy_password|proxy_pass|api_key|api-key)\b"
        r"[\"'\s]*[:=]?[\s]*(?:is\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1=[redacted]", message)
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"(?<!\w)(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", "[host]", message)
    message = re.sub(r"\b[A-Za-z0-9_+/=-]{28,}\b", "[opaque]", message)
    message = " ".join(message.split())
    return message[:300] or "[message omitted]"


def api_json(legacy, api, path, params=None, timeout=40, listing=False, check=None):
    transport = legacy._requests()
    # Pacing waits must keep testing the lease, including immediately before
    # dispatch. Bound queueing as well as the actual HTTP request.
    with transport.deadline_scope(time.monotonic() + timeout + 30, check=check):
        response = transport.get(api + path, params=params, timeout=timeout)
    value = response.json()
    if response.status_code != 200 or not isinstance(value, dict) or type(value.get("code")) is not int:
        raise StopRun(f"Invalid AdsPower status/schema at {path}")
    if value["code"] != 0:
        message = safe_api_message(value.get("msg"))
        raise AdsPowerAPIError(path, value["code"], message)
    if listing:
        data = value.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise StopRun(f"AdsPower {path} list missing")
        if any(not isinstance(p, dict) or not isinstance(p.get("user_id"), str)
               or not p["user_id"] for p in data["list"]):
            raise StopRun(f"AdsPower {path} identity missing")
    return value


def all_profiles(legacy, api, journal):
    """Request pages until an empty page; repeated/nonprogressing pages are errors."""
    result, seen = [], set()
    for page in range(1, 1001):
        value = api_json(legacy, api, "/api/v1/user/list",
                         {"page": page, "page_size": 100}, listing=True)
        rows = value["data"]["list"]
        if not rows:
            journal.event("profile_listing_complete", profiles=len(result), empty_page=page)
            return result
        for row in rows:
            uid, serial = row["user_id"], str(row.get("serial_number") or "")
            if not PROFILE_RE.fullmatch(uid) or not serial.isdigit():
                raise StopRun("Invalid profile UID/serial in user/list")
            if uid in seen:
                raise StopRun("Repeated profile across pages: inventory is not stable")
            seen.add(uid)
            result.append({"serial": serial, "user_id": uid, "name": row.get("name"),
                           "group": {"id": row.get("group_id"), "name": row.get("group_name")},
                           "remark": row.get("remark")})
        journal.event("profile_listing_page", page=page, rows=len(rows))
    raise StopRun("Profile listing exceeded page bound without a proven end")


def upload_processes():
    """Fail closed; support --profile X and --profile=X; never save full argv."""
    proc = subprocess.run(["ps", "-eo", "pid,args"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=20, check=False)
    if proc.returncode:
        raise StopRun("Local uploader process scan failed")
    output = []
    for line in proc.stdout.splitlines():
        if "upload_worker.py" not in line and "upload_manager" not in line:
            continue
        # Exclude only this process; other unidentified uploaders block the gate.
        pid_text, _, argv = line.strip().partition(" ")
        if pid_text == str(os.getpid()):
            continue
        matches = re.findall(r"(?:^|\s)--profile(?:=|\s+)([^\s]+)", argv)
        output.append({"pid": int(pid_text) if pid_text.isdigit() else None,
                       "profiles": [p.strip("\"'") for p in matches],
                       "kind": "upload_worker" if "upload_worker.py" in argv else "upload_manager"})
    return output


def active_everywhere(legacy, hosts):
    # Reuse the established retry policy, but enforce schema even without env flag.
    busy = {}
    for name, api in hosts.items():
        for attempt in range(legacy.HOST_POLL_ATTEMPTS):
            try:
                value = api_json(legacy, api, "/api/v1/browser/local-active", timeout=12, listing=True)
                for row in value["data"]["list"]:
                    busy.setdefault(row["user_id"], []).append(name)
                break
            except Exception as exc:
                if attempt + 1 == legacy.HOST_POLL_ATTEMPTS:
                    raise StopRun(f"Host {name} active-list unavailable ({type(exc).__name__})") from None
                time.sleep(legacy.HOST_POLL_PAUSE_SECONDS)
    return busy


def busy_uploaders(profile):
    ids = {profile["serial"], profile["user_id"]}
    return [p for p in upload_processes() if not p["profiles"] or ids.intersection(p["profiles"])]


# Generic read-only DOM inspection; wizard tag is backed by SELECTOR_MAP.md §2.
JS_TABS = """
function deep(root){let out=[];for(const n of root.querySelectorAll('*')){
 out.push(n);if(n.shadowRoot)out.push(...deep(n.shadowRoot));}return out;}
function visible(n){const r=n.getBoundingClientRect(),s=getComputedStyle(n);
 return !n.hidden&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';}
const all=deep(document),nodes=all.filter(n=>n.tagName==='YTCP-UPLOADS-DIALOG');
const app=all.find(n=>n.tagName==='YTCP-APP');
return {url:location.href,title:document.title,ready:document.readyState,
 studio:location.hostname==='studio.youtube.com',
 studio_app_ready:!!(app&&(app.channel||app.currentEntity)),
 wizard:nodes.map(n=>({visible:visible(n),opened:n.opened===true,
 inner_opened:deep(n).some(x=>x.tagName==='TP-YT-PAPER-DIALOG'&&x.opened===true),
 workflowStep:n.workflowStep||null,uploadMode:n.uploadMode||null,
 isVideoUploaded:n.isVideoUploaded===undefined?null:n.isVideoUploaded,
 entries:Object.entries(n.uploadDetailsByFrontendId||{}).map(([key,x])=>({
  key,local:x.local===undefined?null:x.local,frontendId:x.frontendId||null,
  videoId:x.videoId||null,status:x.status||null,filename:x.filename||null,
  fileSelectedTimeMs:x.fileSelectedTimeMs===undefined?null:x.fileSelectedTimeMs,
  transferStartTimeMs:x.transferStartTimeMs===undefined?null:x.transferStartTimeMs,
  transferProgress:x.transferProgress?{fraction:x.transferProgress.fraction,
    abandoned:x.transferProgress.abandoned}:null}))}))};
"""
JS_AUTHUSERS = """
return [...document.querySelectorAll('a[href],[data-authuser]')].map(n=>({
 href:n.getAttribute('href'),authuser:n.getAttribute('data-authuser'),
 text:(n.innerText||'').slice(0,200),visible:!!n.getClientRects().length
})).filter(x=>x.visible&&(x.authuser!==null||/[?&]authuser=\\d+/.test(x.href||'')));
"""


def tab_safety(tab):
    reasons = []
    if tab.get("ready") != "complete":
        reasons.append("document_not_complete")
    if tab.get("studio") and not tab.get("studio_app_ready"):
        reasons.append("studio_app_not_ready")
    if not isinstance(tab.get("wizard"), list):
        reasons.append("wizard_inspection_missing")
    for wizard in tab.get("wizard", []):
        if wizard.get("visible") or wizard.get("opened") or wizard.get("inner_opened"):
            reasons.append("open_upload_wizard")
        if not isinstance(wizard.get("entries"), list):
            reasons.append("upload_entries_unreadable")
        for entry in wizard.get("entries", []):
            if entry.get("local") is not False:
                reasons.append("local_or_unknown_upload_entry")
    return {"clear": not reasons, "reasons": sorted(set(reasons))}


def original_tabs(driver, assert_held, directory):
    """Observe restored tabs without navigation; two stable polls, >=5 seconds."""
    snapshots, stable, previous_handles = [], 0, None
    start = time.monotonic()
    tabs = []
    try:
        while time.monotonic() - start < 45:
            assert_held()
            handles = driver.window_handles
            if not handles:
                raise StopRun("No original browser tabs could be inspected")
            tabs = []
            for handle in handles:
                driver.switch_to.window(handle)
                tab = driver.execute_script(JS_TABS)
                tab["handle"] = handle
                tab["safety"] = tab_safety(tab)
                tabs.append(tab)
            snapshots.append({"at": now(), "tabs": tabs})
            # A seen local upload cannot be cleared by a later disappearing DOM.
            if any(any(r in {"open_upload_wizard", "local_or_unknown_upload_entry", "upload_entries_unreadable"}
                           for r in tab["safety"]["reasons"]) for tab in tabs):
                return tabs
            clear = all(t["safety"]["clear"] for t in tabs)
            stable = stable + 1 if clear and handles == previous_handles else 0
            previous_handles = handles
            if stable >= 2 and time.monotonic() - start >= 5:
                return tabs
            time.sleep(1)
        # Incomplete/unknown means manual; caller preserves the owned profile.
        for tab in tabs:
            tab["safety"] = {"clear": False, "reasons": ["initial_tabs_readiness_timeout"]}
        return tabs
    finally:
        save(directory / "raw" / "original_tabs_observations.json", snapshots)


def read_session(driver, au, legacy, directory, assert_held):
    snapshots, error = [], None
    try:
        assert_held()
        driver.get(f"https://www.youtube.com/account?authuser={au}")
        deadline = time.monotonic() + 24
        while time.monotonic() < deadline:
            assert_held()
            raw = driver.execute_script(legacy.JS_SESSION)
            state = json.loads(raw)
            snapshots.append({"at": now(), "raw": raw, "parsed": state})
            if "accounts.google." in state.get("url", "") or "ServiceLogin" in state.get("url", ""):
                break
            if state.get("logged") and state.get("name") and legacy.MAIL_RE.search(state.get("text") or ""):
                break
            time.sleep(2)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    save(directory / f"session_{au:02d}.json", {"requested_authuser": au, "snapshots": snapshots, "error": error})
    if error:
        return {"authuser": au, "status": "session_error", "error": error}
    last = snapshots[-1]["parsed"] if snapshots else {}
    if "accounts.google." in last.get("url", "") or "ServiceLogin" in last.get("url", ""):
        return {"authuser": au, "status": "need_auth", "url": last.get("url"),
                "saved_email_candidates": list(dict.fromkeys(legacy.MAIL_RE.findall(last.get("text") or ""))),
                "attribution": "signed_out_account_chooser", "email": None}
    mails = list(dict.fromkeys(legacy.MAIL_RE.findall(last.get("text") or "")))
    # Ambiguous email must never silently become the owner of all channels.
    email = mails[0] if len(mails) == 1 else None
    return {"authuser": au, "status": "ok" if last.get("logged") and email else "unknown_owner",
            "email": email, "email_candidates": mails, "name": last.get("name"),
            "session_index": last.get("idx"), "session_id": last.get("sid"),
            "logged": last.get("logged"), "url": last.get("url")}


def parse_channel_groups(body, au, expected_email, legacy):
    """Live schema 2026-10-04 canary19b; never flatten across Google headers."""
    groups, channels, unresolved, no_channel = [], [], [], []
    for action in body.get("actions", []):
        menu = action.get("getMultiPageMenuAction", {}).get("menu", {}).get("multiPageMenuRenderer", {})
        for section_index, wrapper in enumerate(menu.get("sections", [])):
            section = wrapper.get("accountSectionListRenderer")
            if not isinstance(section, dict):
                continue
            header = section.get("header", {}).get("googleAccountHeaderRenderer", {})
            email = legacy._txt(header.get("email"))
            group = {"section_index": section_index, "header": header, "email": email,
                     "expected_email": expected_email, "authuser": au,
                     "header_matches": bool(email and expected_email and email.casefold() == expected_email.casefold()),
                     "items": 0}
            groups.append(group)
            for contents in section.get("contents", []):
                for wrapper_item in contents.get("accountItemSectionRenderer", {}).get("contents", []):
                    item = wrapper_item.get("accountItem")
                    if not isinstance(item, dict):
                        continue  # menu actions such as Add account are not identities.
                    group["items"] += 1
                    tokens = item.get("serviceEndpoint", {}).get("selectActiveIdentityEndpoint", {}).get("supportedTokens", [])
                    signin_urls = [t.get("accountSigninToken", {}).get("signinUrl") for t in tokens if isinstance(t, dict)]
                    indices = [v for url in signin_urls if isinstance(url, str)
                               for v in parse_qs(urlsplit(url).query).get("authuser", [])]
                    scope = {"section_index": section_index, "header_email": email,
                             "signin_authusers": indices, "signin_urls": [u for u in signin_urls if u]}
                    if not group["header_matches"] or not indices or set(indices) != {str(au)}:
                        unresolved.append({"reason": "unknown_crossowner", "scope": scope, "source_item": item})
                        continue
                    if item.get("hasChannel") is False:
                        no_channel.append({"scope": scope, "source_item": item})
                        continue
                    keys = [t.get("offlineCacheKeyToken", {}).get("clientCacheKey") for t in tokens if isinstance(t, dict)]
                    ucs = ["UC" + key for key in keys if isinstance(key, str) and UC_RE.fullmatch("UC" + key)]
                    if not ucs:
                        unresolved.append({"reason": "channel_uc_missing", "scope": scope, "source_item": item})
                    for uc in ucs:
                        channels.append({"uc": uc, "name": legacy._txt(item.get("accountName")),
                                         "handle": legacy._txt(item.get("channelHandle")),
                                         "byline": legacy._txt(item.get("accountByline")),
                                         "selected": bool(item.get("isSelected")), "scope": scope,
                                         "ownership_verified": True, "source_item": item})
    status = "ok" if (channels or no_channel) and not unresolved else "partial" if channels else "unknown"
    return {"status": status, "channels": channels, "unresolved_items": unresolved,
            "groups": groups, "no_channel_accounts": no_channel, "http_status": 200}


def read_channels(driver, au, expected_email, legacy, directory, assert_held):
    assert_held()
    raw = driver.execute_async_script(legacy.JS_ACCOUNTS_LIST, au)
    save(directory / f"accounts_list_{au:02d}.json", {"at": now(), "authuser": au, "raw": raw})
    if not isinstance(raw, str) or raw.startswith("ERR:"):
        raise RuntimeError("accounts_list returned an error; channel count is unknown")
    envelope = json.loads(raw)
    if envelope.get("status") != 200:
        raise RuntimeError(f"accounts_list HTTP {envelope.get('status')}; channel count is unknown")
    body = json.loads(envelope.get("body") or "null")
    if not isinstance(body, dict) or body.get("error"):
        raise RuntimeError("accounts_list body is invalid; channel count is unknown")
    return parse_channel_groups(body, au, expected_email, legacy)


def discover(driver, legacy, rec, directory, journal, assert_held, max_authuser):
    seen, hinted, ended = {}, set(), False
    raw_dir = directory / "raw"
    for au in range(max_authuser + 1):
        assert_held()
        account = read_session(driver, au, legacy, raw_dir, assert_held)
        rec["accounts"].append(account)
        journal.checkpoint(directory, rec, f"session_{au:02d}")
        if account["status"] != "ok":
            if au == 0 and account["status"] == "need_auth":
                rec["discovery"] = {"complete": False, "reason": "primary_account_requires_auth"}
                return
            continue
        ui = driver.execute_script(JS_AUTHUSERS)
        save(raw_dir / f"authuser_ui_{au:02d}.json", ui)
        for link in ui:
            values = [link.get("authuser")] + re.findall(r"[?&]authuser=(\d+)", link.get("href") or "")
            hinted.update(int(v) for v in values if str(v).isdigit())
        rec["observed_authusers"] = sorted(hinted)
        fp = (account.get("session_id"), account.get("email"), account.get("session_index"))
        previous = seen.get(fp)
        if previous is not None and str(account.get("session_index")) != str(au):
            account["status"] = "repeated_session_alias"
            account["alias_of_authuser"] = previous
            if not any(v >= au for v in hinted):
                rec["discovery"] = {"complete": all(a["status"] in {"ok", "repeated_session_alias"}
                                                       and a.get("channels_status", "ok") == "ok" for a in rec["accounts"]),
                                    "reason": "requested_authuser_resolved_to_previously_observed_session",
                                    "boundary_authuser": au, "resolved_index": account.get("session_index")}
                ended = True
                break
            continue
        seen[fp] = au
        try:
            result = read_channels(driver, au, account["email"], legacy, raw_dir, assert_held)
            account["channels_status"] = result["status"]
            account["channels"] = result["channels"]
            account["unresolved_items"] = result["unresolved_items"]
            account["groups"] = result["groups"]
            account["no_channel_accounts"] = result["no_channel_accounts"]
            for ch in result["channels"]:
                rec["relationships"].append({"profile": rec["profile"]["serial"],
                                              "user_id": rec["profile"]["user_id"], "authuser": au,
                                              "email": account["email"], "uc": ch["uc"],
                                              "name": ch["name"], "handle": ch["handle"],
                                              "ownership_verified": ch["ownership_verified"], "scope": ch["scope"]})
        except Exception as exc:
            account["channels_status"] = "error"
            account["channels_error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        journal.checkpoint(directory, rec, f"identities_{au:02d}")
    if not ended:
        rec["discovery"] = {"complete": False, "reason": "bounded_authuser_scan_exhausted_without_proven_end",
                            "max_authuser": max_authuser}
    journal.checkpoint(directory, rec, "identity_discovery_done")


def verified_runtime(proof, state):
    return (isinstance(proof, dict) and isinstance(state, dict)
            and proof.get("context_verified") is True
            and state.get("uc") == proof.get("uc") and state.get("entity_id") == proof.get("uc")
            and state.get("entity_type") == "CHANNEL"
            and str(state.get("session_index")) == str(proof.get("authuser"))
            and str(state.get("delegated_session_id") or "") == str(proof.get("page_id") or "")
            and bool(proof.get("datasync_id")) and state.get("datasync_id") == proof.get("datasync_id"))


def reusable_metrics(item):
    result, uc = item.get("result"), item.get("uc")
    return (item.get("status") == "reader_returned" and isinstance(result, dict)
            and result.get("status") == "complete" and result.get("uc") == uc
            and result.get("active_identity_verified") is True
            and isinstance(result.get("active_identity_proof"), dict)
            and result["active_identity_proof"].get("uc") == uc
            and result["active_identity_proof"].get("selected") is True
            and verified_runtime(result["active_identity_proof"], result["active_identity_proof"].get("runtime"))
            and verified_runtime(result["active_identity_proof"], result["active_identity_proof"].get("after_navigation_runtime"))
            and verified_runtime(result["active_identity_proof"], result.get("identity"))
            and result.get("identity", {}).get("uc") == uc
            and result.get("identity", {}).get("entity_id") == uc
            and result.get("identity", {}).get("entity_type") == "CHANNEL"
            and all(isinstance(result.get(k), dict) and result[k].get("status") == "complete"
                    and result[k].get("context_verified") is True
                    and verified_runtime(result["active_identity_proof"], result[k].get("runtime_identity"))
                    for k in ("features", "lifetime", "earn")))


def load_metric_cache(output):
    cache = {}
    for path in sorted(output.glob("profiles/*/attempt_*/channels/authuser_*/*/collector_result.json")):
        try:
            item = json.loads(path.read_text())
            if reusable_metrics(item) and not item.get("reused_metrics_from"):
                cache.setdefault(item["uc"], {"result": item["result"], "source": str(path)})
        except (OSError, ValueError):
            continue  # A broken cached file never replaces a fresh live read.
    return cache


def load_shared_metric_cache(cache_root, only_uc=None):
    """Read today's final atomic reader results, with all three evidence scopes."""
    cache_root = Path(cache_root).resolve()
    today = datetime.now(timezone.utc).date().isoformat()
    channel_part = only_uc if only_uc and UC_RE.fullmatch(only_uc) else "*"
    pattern = f"*/profiles/*/attempt_*/channels/authuser_*/{channel_part}/reader_result.json"
    cache = {}
    for path in sorted(cache_root.glob(pattern)):
        if any("probe" in part.casefold() for part in path.relative_to(cache_root).parts):
            continue
        try:
            if not path.resolve().is_relative_to(cache_root):
                continue
            result = json.loads(path.read_text())
            uc = result.get("uc")
            item = {"status": "reader_returned", "uc": uc, "result": result}
            if (not isinstance(uc, str) or not UC_RE.fullmatch(uc)
                    or not str(result.get("finished_at", "")).startswith(today)
                    or not reusable_metrics(item)):
                continue
            verified = True
            for stage in ("features", "lifetime", "earn"):
                evidence_path = Path(result[stage].get("evidence") or "").resolve()
                if not evidence_path.is_relative_to(cache_root):
                    verified = False
                    break
                evidence = json.loads(evidence_path.read_text())
                identity = evidence.get("identity") or {}
                if (identity.get("uc") != uc or identity.get("entity_id") != uc
                        or identity.get("entity_type") != "CHANNEL"
                        or not verified_runtime(result["active_identity_proof"], identity)
                        or evidence.get("data", {}).get("status") != "complete"
                        or not str(evidence.get("at", "")).startswith(today)):
                    verified = False
                    break
            if verified:
                cache.setdefault(uc, {"result": result, "source": str(path)})
        except (OSError, ValueError, TypeError, AttributeError):
            continue  # Partial writes, old schemas and broken evidence mean cache miss.
    return cache


def read_all_channels(driver, reader, legacy, rec, directory, journal, assert_held, options,
                      metric_cache=None, metrics_cache_root=None):
    for account in rec["accounts"]:
        if account.get("status") != "ok" or not account.get("email"):
            continue
        au, seen = account["authuser"], set()
        for channel in account.get("channels", []):
            uc = channel["uc"]
            if uc in seen:
                continue
            seen.add(uc)
            assert_held()
            channel_dir = directory / "channels" / f"authuser_{au:02d}" / uc
            context = {"profile": rec["profile"]["serial"], "user_id": rec["profile"]["user_id"],
                       "authuser": au, "email": account["email"], "session_index": account.get("session_index")}
            save(channel_dir / "context.json", context)
            item = {"uc": uc, "context": context, "status": "reading"}
            rec["channel_results"].append(item)
            journal.checkpoint(directory, rec, "channel_begin")
            if channel.get("ownership_verified") is not True:
                item.update(status="unknown_owner", reason="account_group_scope_not_verified")
                save(channel_dir / "collector_result.json", item)
                journal.checkpoint(directory, rec, "channel_owner_unverified")
                continue
            cached = metric_cache.get(uc) if metric_cache is not None else None
            if not cached and metrics_cache_root:
                # Other collectors may have completed this UC since we started.
                # This reads results only; it never touches their profiles.
                shared = load_shared_metric_cache(metrics_cache_root, only_uc=uc)
                if metric_cache is not None:
                    metric_cache.update(shared)
                cached = shared.get(uc)
            if cached:
                item.update(status="reader_returned", result=cached["result"],
                            reused_metrics_from={"path": cached["source"],
                                                 "observed_at": cached["result"].get("finished_at"),
                                                 "original_context": cached["result"].get("context")})
                save(channel_dir / "collector_result.json", item)
                journal.checkpoint(directory, rec, "channel_metrics_reused")
                continue
            try:
                session = read_session(driver, au, legacy, channel_dir, assert_held)
                if session.get("status") != "ok" or session.get("email") != account["email"]:
                    item.update(status="need_auth_or_unknown_owner", actual_session=session)
                else:
                    assert_held()
                    driver.get(f"https://studio.youtube.com/channel/{uc}?authuser={au}")
                    item["result"] = reader.read_channel(driver, uc, str(channel_dir),
                                                         {**options, "context": context, "assert_held": assert_held})
                    item["status"] = "reader_returned"
            except Exception as exc:
                item.update(status="error", error={"type": type(exc).__name__, "message": str(exc)[:1000]})
            save(channel_dir / "collector_result.json", item)
            if metric_cache is not None and reusable_metrics(item):
                metric_cache.setdefault(uc, {"result": item["result"], "source": str(channel_dir / "collector_result.json")})
            journal.checkpoint(directory, rec, "channel_done")


def completion(rec):
    identity_complete = rec.get("discovery", {}).get("complete") is True
    expected = {(a["authuser"], a.get("email"), c["uc"])
                for a in rec.get("accounts", []) if a.get("status") == "ok"
                for c in a.get("channels", [])}
    completed = {(c["context"]["authuser"], c["context"].get("email"), c["uc"])
                 for c in rec.get("channel_results", [])
                 if reusable_metrics(c)}
    metrics_complete = (not rec.get("identity_only") and identity_complete
                        and completed == expected)
    rec.update(identity_complete=identity_complete, metrics_complete=metrics_complete,
               expected_channel_reads=len(expected), complete_channel_reads=len(completed))
    rec["status"] = "collected" if identity_complete and (rec.get("identity_only") or metrics_complete) else "partial"


def confirm_proxy_start_inactive(legacy, hosts, profile, assert_held):
    """Read-only proof after the exact Check Proxy Fail response; never stop/retry."""
    assert_held()
    busy = active_everywhere(legacy, hosts)
    assert_held()
    if profile["user_id"] in busy:
        raise StopRun("Proxy-blocked profile appears in an active-list")
    specific = api_json(legacy, hosts["local"], "/api/v1/browser/active",
                        {"user_id": profile["user_id"]}, timeout=15, check=assert_held)
    data = specific.get("data")
    if (not isinstance(data, dict) or data.get("status") != "Inactive"
            or data.get("user_id", profile["user_id"]) != profile["user_id"]):
        raise StopRun("Proxy-blocked profile-specific Inactive not verified")
    assert_held()
    uploaders = busy_uploaders(profile)
    if uploaders:
        raise StopRun("Uploader present after proxy-blocked start")
    assert_held()
    return {"at": now(), "hosts_checked": list(hosts), "active_on": [],
            "queried_user_id": profile["user_id"], "specific_status": "Inactive",
            "uploaders": [], "lease_held": True}


def configure_headless_viewport(driver, directory, guard, *, enabled=False):
    """Match headed PNG dimensions using native window geometry on our safe tab."""
    if not enabled:
        return
    target = {"width": 1590, "height": 903}
    evidence = {"before": None, "after": None, "target": target,
                "attempts": [], "confirmed": False}

    def checked(name, action):
        guard()
        try:
            value = action()
        except Exception as exc:
            # A lease error must take precedence over a browser failure.
            guard()
            raise StopRun(f"Headless viewport {name} failed: {type(exc).__name__}: "
                          f"{safe_api_message(str(exc))}") from exc
        guard()
        return value

    def inner_size():
        value = checked("read", lambda: driver.execute_script(
            "return {width:innerWidth,height:innerHeight}"))
        if (not isinstance(value, dict) or set(value) != {"width", "height"}
                or any(type(value[key]) is not int or value[key] <= 0 for key in target)):
            raise StopRun("Headless viewport dimensions are invalid")
        return value

    try:
        inner = inner_size()
        evidence["before"] = evidence["after"] = inner
        for _ in range(2):
            if inner == target:
                break
            rect = checked("get_window_rect", driver.get_window_rect)
            if (not isinstance(rect, dict) or any(type(rect.get(key)) is not int
                    or rect[key] <= 0 for key in target)):
                raise StopRun("Headless native window dimensions are invalid")
            requested = {key: rect[key] + target[key] - inner[key] for key in target}
            attempt = {"before": inner, "window_rect": rect, "requested": requested}
            evidence["attempts"].append(attempt)
            checked("set_window_rect", lambda: driver.set_window_rect(**requested))
            inner = inner_size()
            attempt["after"] = evidence["after"] = inner
        evidence["confirmed"] = inner == target
        if not evidence["confirmed"]:
            raise StopRun("Headless viewport could not be matched to evidence baseline")
    except Exception as exc:
        evidence["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        raise
    finally:
        save(Path(directory) / "headless_viewport.json", evidence)


def scan_profile(args, profile, legacy, coordination, lease_context, reader, options, journal, metric_cache=None):
    directory = args.out / "profiles" / f"{profile['serial']}_{profile['user_id']}" / ("attempt_" + stamp())
    rec = {"schema_version": 1, "profile": profile, "started_at": now(), "status": "starting",
           "identity_only": reader is None, "identity_complete": False, "metrics_complete": False,
           "accounts": [], "relationships": [], "channel_results": []}
    journal.profile = profile["serial"]
    journal.checkpoint(directory, rec, "before_lease")
    hosts = {"local": args.api, "peer": args.peer_api}
    owned, preserve, driver, close_ok = False, False, None, False
    lease = None
    with coordination.profile_lease(profile["serial"], lease_context) as lease:
        def guard():
            if lease:
                lease.assert_held()
        try:
            guard()
            # User's required standard diagnostic, followed by our strict gate.
            # legacy.check itself may report unreachable without raising; that
            # is never taken as permission to start by active_everywhere below.
            legacy.check(SimpleNamespace(hosts=f"local={args.api},peer={args.peer_api}", api=args.api))
            guard()
            busy = active_everywhere(legacy, hosts)
            uploaders = busy_uploaders(profile)
            rec["gate"] = {"checked_at": now(), "active_on": busy.get(profile["user_id"], []), "uploaders": uploaders}
            if rec["gate"]["active_on"] or uploaders:
                rec["status"] = "skipped_busy"
                journal.checkpoint(directory, rec, "skipped_busy")
                return rec
            journal.checkpoint(directory, rec, "start_pending")
            guard()
            try:
                # Deliberately one start call. Never retry or attach to a prior session.
                response = api_json(legacy, args.api, "/api/v1/browser/start",
                                    {"user_id": profile["user_id"], "open_tabs": 1, "ip_tab": 0,
                                     "headless": int(args.headless)},
                                    timeout=180, check=guard)
                data = response.get("data") or {}
                debugger = (data.get("ws") or {}).get("selenium")
                if not debugger:
                    raise StopRun("Successful start response has no Selenium endpoint")
                owned = True
                # Until ALL restored tabs are inspected, a preexisting upload
                # could be present. Attach/snapshot failure must leave it open.
                preserve = True
                rec["ownership"] = {"start_acknowledged": True, "at": now()}
                journal.checkpoint(directory, rec, "owned_profile_started")
            except Exception as exc:
                rec["status"] = "manual_start_ambiguous"
                rec["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
                preserve = True
                journal.checkpoint(directory, rec, "start_error")
                if (isinstance(exc, AdsPowerAPIError) and exc.path == "/api/v1/browser/start"
                        and exc.code == -1 and exc.message == "Check Proxy Fail"):
                    try:
                        if lease is None:
                            raise StopRun("No held lease for proxy-block recovery")
                        rec["proxy_start_inactivity"] = confirm_proxy_start_inactive(legacy, hosts, profile, guard)
                    except Exception as recovery_error:
                        rec["proxy_recovery_error"] = {"type": type(recovery_error).__name__,
                                                       "message": safe_api_message(str(recovery_error))}
                    else:
                        preserve, close_ok = False, True
                        rec["status"] = "start_blocked_proxy"
                        raise _StartBlockedProxy from None
                raise StopRun("Start outcome is ambiguous; no retry, no stop, no next profile") from None
            busy = active_everywhere(legacy, hosts)
            if busy.get(profile["user_id"]) != ["local"]:
                preserve = True
                raise StopRun("Started profile not exclusively active on local host")
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            opts = Options()
            opts.add_experimental_option("debuggerAddress", debugger)
            driver = webdriver.Chrome(service=Service(data["webdriver"]), options=opts) if data.get("webdriver") else webdriver.Chrome(options=opts)
            driver.set_page_load_timeout(60)
            driver.set_script_timeout(45)
            try:
                rec["original_tabs"] = original_tabs(driver, guard, directory)
                if not all(t["safety"]["clear"] for t in rec["original_tabs"]):
                    preserve = True
                    rec["status"] = "manual_original_upload_wizard"
                    journal.checkpoint(directory, rec, "manual_upload_wizard")
                    raise StopRun("Original tabs not proven free of uploads; leave profile and tabs untouched")
            except Exception:
                preserve = True
                raise
            journal.checkpoint(directory, rec, "original_tabs_recorded")
            preserve = False
            # Navigation happens in our own new tab, preserving original tabs/URLs.
            guard()
            driver.switch_to.new_window("tab")
            guard()
            configure_headless_viewport(driver, directory, guard, enabled=args.headless)
            discover(driver, legacy, rec, directory, journal, guard, args.max_authuser)
            if reader:
                read_all_channels(driver, reader, legacy, rec, directory, journal, guard, options,
                                  metric_cache, getattr(args, "metrics_cache_root", None))
            completion(rec)
        except _StartBlockedProxy:
            pass  # Keep the original error and proceed through finally + lease release.
        except BaseException as exc:
            if rec["status"] not in {"manual_start_ambiguous", "manual_original_upload_wizard"}:
                rec["status"] = "stopped" if isinstance(exc, (StopRun, KeyboardInterrupt)) else "error"
                rec["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            raise
        finally:
            if owned and not preserve:
                try:
                    if lease:
                        try:
                            lease.assert_held()
                        except coordination.ProfileLeaseError:
                            preserve = True
                            raise StopRun("Lease lost; do not close a profile another holder could now own") from None
                    if busy_uploaders(profile):
                        preserve = True
                        raise StopRun("Uploader appeared during audit; leave profile open for manual review")
                    before_stop = active_everywhere(legacy, hosts)
                    if "peer" in before_stop.get(profile["user_id"], []):
                        preserve = True
                        raise StopRun("Profile became active on peer; leave it for manual review")
                    # Active-list retries may outlast a lease renewal failure.
                    try:
                        guard()
                    except coordination.ProfileLeaseError:
                        preserve = True
                        raise StopRun("Lease lost during close gate; preserve profile") from None
                    # Stop only a profile whose successful start belongs to this attempt.
                    api_json(legacy, args.api, "/api/v1/browser/stop", {"user_id": profile["user_id"]},
                             timeout=60, check=guard)
                    for attempt in range(5):
                        busy = active_everywhere(legacy, hosts)
                        if profile["user_id"] not in busy:
                            close_ok = True
                            break
                        time.sleep(2)
                    if not close_ok:
                        raise StopRun("Owned profile closure was not confirmed on both hosts")
                except Exception as exc:
                    rec["close_error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            rec["closure"] = {"owned": owned, "preserved_for_manual_review": preserve, "confirmed_both_hosts": close_ok}
            if lease and (preserve or (owned and not close_ok)):
                lease.retain_until_expiry("audit left profile open or closure uncertain; manual review")
            # driver.quit() is forbidden for attached AdsPower sessions. Release only
            # the chromedriver service process after confirmed browser shutdown.
            if driver is not None and close_ok:
                try:
                    driver.service.stop()
                except Exception:
                    pass
            rec["finished_at"] = now()
            journal.checkpoint(directory, rec, "profile_final")
            if preserve and rec.get("close_error"):
                raise StopRun("Profile preserved because ownership/active uploader state changed")
            if owned and not close_ok and not preserve:
                raise StopRun("Closing profile could not be verified; stopping run")
    if lease and lease.release_error:
        raise StopRun("Lease release was not confirmed; stopping run")
    return rec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:50325")
    parser.add_argument("--peer-api", default="http://100.100.244.89:50325")
    parser.add_argument("--out", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--profiles")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--identity-only", action="store_true")
    parser.add_argument("--headless", action="store_true",
                        help="Run this automatic scan without a browser window; preserve full-size PNGs")
    parser.add_argument("--lease-context", type=Path, required=True)
    parser.add_argument("--reader-options", type=Path)
    parser.add_argument("--max-authuser", type=int, choices=range(20), default=19)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reuse-channel-metrics", action="store_true",
                        help="Reuse only verified complete metrics for the same UC within this output run")
    parser.add_argument("--metrics-cache-root", type=Path,
                        help="Reuse today's complete, scope-verified metrics in sibling run directories")
    args = parser.parse_args()
    args.api, args.peer_api = origin(args.api), origin(args.peer_api)
    if urlsplit(args.api).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("--api must address local AdsPower; execute on Minsk")
    if args.api == args.peer_api or urlsplit(args.peer_api).hostname in {"localhost", "127.0.0.1", "::1"}:
        parser.error("--peer-api must be the other machine")
    args.out = args.out.expanduser().resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = (args.out / ".collector.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Another collector owns this output directory")
    sys.path.insert(0, str(HERE / "src"))
    # Preload by exact path so installed/production copies cannot win resolution.
    load_module("adspower_rate_limit", HERE / "src" / "adspower_rate_limit.py")
    coordination = load_module("upload_coordination", HERE / "src" / "upload_coordination.py")
    legacy = load_module("audit_collect_channel_emails", HERE / "tools" / "collect_channel_emails.py")
    lease_context = None
    if args.lease_context:
        lease_context = coordination.normalize_lease_context(json.loads(args.lease_context.read_text()))
        if lease_context is None:
            parser.error("--lease-context was supplied but has no valid lease context")
    options = json.loads(args.reader_options.read_text()) if args.reader_options else {}
    if not isinstance(options, dict):
        parser.error("--reader-options must be a JSON object")
    reader_path = HERE / "studio_reader.py"
    reader = load_module("audit_studio_reader", reader_path) if reader_path.exists() and not args.identity_only else None
    metric_cache = {} if args.metrics_cache_root else load_metric_cache(args.out) if args.reuse_channel_metrics else None
    if args.metrics_cache_root:
        args.metrics_cache_root = args.metrics_cache_root.expanduser().resolve()
        metric_cache.update(load_shared_metric_cache(args.metrics_cache_root))
    journal = Journal(args.out)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        journal.event("run_started", identity_only=reader is None, lease_required=lease_context is not None)
        # Connectivity required even before listing; missing peer never means empty.
        active_everywhere(legacy, {"local": args.api, "peer": args.peer_api})
        profiles = all_profiles(legacy, args.api, journal)
        save(args.out / "profile_snapshots" / (stamp() + ".json"), profiles)
        save(args.out / "profiles.json", profiles)
        if not args.all:
            requested = {p.strip() for p in args.profiles.split(",") if p.strip()}
            selected = [p for p in profiles if p["serial"] in requested or p["user_id"] in requested]
            found = {p[k] for p in selected for k in ("serial", "user_id")}
            if requested - found:
                raise StopRun("Requested profiles absent from complete AdsPower listing: " + ",".join(sorted(requested - found)))
            profiles = selected
        for profile in sorted(profiles, key=lambda p: int(p["serial"])):
            folder = args.out / "profiles" / f"{profile['serial']}_{profile['user_id']}"
            if args.resume:
                prior = sorted(folder.glob("attempt_*/record.json"))
                if prior:
                    record = json.loads(prior[-1].read_text())
                    if (record.get("status") == "collected" and record.get("identity_only") == (reader is None)
                            and record.get("identity_complete") is True
                            and (reader is None or record.get("metrics_complete") is True)
                            and (reader is None or all(reusable_metrics(c) for c in record.get("channel_results", [])))
                            and record.get("closure", {}).get("confirmed_both_hosts")):
                        journal.event("resume_skip_complete", serial=profile["serial"])
                        continue
            try:
                scan_profile(args, profile, legacy, coordination, lease_context, reader, options, journal, metric_cache)
            except coordination.ProfileLeaseError as exc:
                journal.event("lease_blocked", serial=profile["serial"], reason=str(exc))
                raise StopRun("Could not hold required profile lease") from None
        journal.event("run_completed", profiles_considered=len(profiles))
        return 0
    except BaseException as exc:
        journal.event("run_stopped", error_type=type(exc).__name__, reason=str(exc)[:1000])
        return 130 if isinstance(exc, KeyboardInterrupt) else 2
    finally:
        journal.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
