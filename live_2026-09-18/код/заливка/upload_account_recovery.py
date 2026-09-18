#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded, opt-in recovery of a signed-out Google account during YouTube upload.

The upload worker routes to the exact target channel *before* it hands any file
to Studio.  When that routing fails because the bound Google account is signed
out, this module signs it back in — exact account only, one password + one TOTP,
under the same profile lease the worker already runs under — and then re-proves
live access to the exact target channel UC.  It never signs out other accounts,
never clears cookies, never recreates a profile, and never re-sends an MP4:
recovery only runs at routing time, which the durable phase contract keeps below
``upload_started`` (:mod:`upload_contract`), so a fresh file transfer is
impossible on this path.

Why a separate module and not code inside the worker: the sign-in touches
Google credentials and reviewed live-DOM predicates.  Those already live in the
reviewed auth bundle (``auth_browser``/``google_auth_secrets`` next to
``upload_auth``'s ROOT).  This module reuses them and adds only the state
classifier and the exact-channel proof, so the credential-handling surface stays
in one reviewed place.

Enable with ``VIDEO_UPLOAD_ACCOUNT_RECOVERY=1``.  With the flag unset the worker
behaviour is byte-for-byte unchanged (``maybe_recover`` returns ``None`` before
touching anything).

Operator-facing statuses (never carry secrets or field contents):
    account_signed_out, recovering_login, awaiting_2fa, channel_confirmed,
    owner_help_needed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ENABLE_ENV = "VIDEO_UPLOAD_ACCOUNT_RECOVERY"
# The reviewed auth bundle (credentials loader, TOTP, live-DOM predicates).
# Same location upload_auth loads from; overridable for the Mac pilot / tests.
ROOT_ENV = "VIDEO_UPLOAD_AUTH_ROOT"
DEFAULT_ROOT = Path("/home/vova/ww2_upload_stress_20260914")
# Exact per-(profile, channel_uc) -> login binding + accounts store.
ACCOUNTS_ENV = "VIDEO_UPLOAD_ACCOUNTS_JSON"

UC_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# One submission per stage, one overall wall-clock ceiling, no login retries.
RECOVERY_DEADLINE_SECONDS = 210.0
# Per account slot when proving the exact channel after sign-in, and how long a
# page must have settled before its refusal is believed.
SLOT_PROOF_SECONDS = 20.0
SLOT_SETTLE_SECONDS = 4.0
# Собственный бюджет доказательства канала. Раньше оно получало ОСТАТОК от
# бюджета входа и на профиле с несколькими аккаунтами не успевало дойти до
# нужного слота: живьём 16.09.2026 канал был доступен на authuser=1, а проверка
# умирала после слота 0 и объявляла «вошли, но канал не подтверждён».
CHANNEL_PROOF_SECONDS = 120.0
# How many identical, fruitless looks at one sign-in step before giving up on it.
MAX_STALLED_PROBES = 20
# Необязательные предложения ПОСЛЕ успешного входа: их можно просто покинуть,
# ничего не нажимая. Всё остальное на /v3/signin/speedbump/ — к владельцу.
SKIPPABLE_SPEEDBUMPS = ("passkeyenrollment",)

# Where the outcome of a recovery is remembered between worker processes.  Each
# upload is its own process, so without this the same hopeless sign-in would be
# retried for every queued video and hammer Google with it.
STATE_DIR_ENV = "VIDEO_UPLOAD_RECOVERY_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".local/state/video-studio/account-recovery"
OWNER_HELP_COOLDOWN_SECONDS = 6 * 3600.0
MAX_ATTEMPTS_PER_WINDOW = 3
ATTEMPT_WINDOW_SECONDS = 24 * 3600.0

# Statuses surfaced to the operator log.  Constants only; never interpolate
# secrets, emails, passwords, codes or raw field values into these.
STATUS_SIGNED_OUT = "account_signed_out"
STATUS_RECOVERING = "recovering_login"
STATUS_AWAITING_2FA = "awaiting_2fa"
STATUS_CHANNEL_CONFIRMED = "channel_confirmed"
STATUS_OWNER_HELP = "owner_help_needed"


class RecoveryBlocked(RuntimeError):
    """Value-free diagnostic safe for the operator log.

    Any exception carrying a URL, argv, credential or field value must be
    converted into this before it leaves the module.
    """


class OwnerHelpNeeded(RecoveryBlocked):
    """A human must act (wrong password/code, CAPTCHA, SMS, phone, passkey,
    an unknown challenge, or a binding that does not own the channel).  The
    caller must stop automatic attempts, not retry."""


# --- Live state classification -------------------------------------------------

# One atomic snapshot of the active tab.  Distinguishes the six states the caller
# must tell apart.  Reads only structural facts (origin/path, Studio app entity,
# whether the *target* login appears signed-out in the chooser, and the reviewed
# Google challenge signals).  Returns no field values.
_CHALLENGE_SIGNALS_JS = r"""(()=>{
const body=(document.body&&document.body.innerText)||'';
// Видимость обязательна. Google держит невидимую reCAPTCHA почти на КАЖДОЙ
// странице входа (оценка риска), поэтому наличие iframe[src*=recaptcha] само по
// себе не значит ничего: по нему детектор срабатывал бы всегда и вход не
// проходил бы никогда. Живая проверка 16.09.2026 на профиле 28: обычная
// страница пароля, невидимая рекапча в разметке, ложный стоп.
// Слово "reCAPTCHA" из текстовой проверки тоже убрано — оно бывает в видимой
// мелкой подписи "This site is protected by reCAPTCHA".
function vis(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);
 return r.width>20&&r.height>20&&s.display!=='none'&&s.visibility!=='hidden'&&e.offsetParent!==null;}
const widget=[...document.querySelectorAll('iframe[src*="recaptcha"],div.g-recaptcha,#captchaimg,img#captchaimg')].some(vis);
const onChallenge=location.pathname.indexOf('/challenge/recaptcha')>=0
                ||location.pathname.indexOf('/challenge/ipp')>=0;
return {lang:String(document.documentElement.lang||''),host:location.hostname,path:location.pathname,
 captcha:onChallenge||widget||/(i'm not a robot|confirm you.?re not a robot|unusual traffic|type the characters you see|type the text you hear or see)/i.test(body),
 phone:/(phone number|verify.*phone|confirm.*phone|by text message|get a verification code)/i.test(body),
 passkey:/(passkey|security key)/i.test(body),
 wrong_password:/(wrong password|incorrect password)/i.test(body),
 wrong_code:/(wrong code|incorrect code)/i.test(body),
 rate_limit:/(too many failed attempts|too many attempts|try again later)/i.test(body)};})()"""


_CLASSIFY_JS = r"""(()=>{
function deep(root,out){for(const e of root.querySelectorAll('*')){out.push(e);if(e.shadowRoot)deep(e.shadowRoot,out)}return out}
function vis(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}
const all=deep(document,[]);
const app=all.find(e=>e.tagName==='YTCP-APP');
const body=(document.body&&document.body.innerText)||'';
const host=location.hostname, path=location.pathname;
// Chooser entries for the exact target login, with their signed-out marker.
// Live dump research/dom/accountchooser_signed_out_20260916.json (profile 28,
// 2026-09-16, zh-HK UI): a signed-out entry is
//   div[role="link"][data-identifier][data-authuser="-1"]
//     [data-button-type="multipleChoiceIdentifier"]
// A signed-in entry carries its real session index there instead.  The number
// is the machine fact; the "Signed out" caption next to it is localized
// ("已登出" in that dump) and is never matched.  Elements without an explicit
// data-authuser are inner duplicates and are ignored.
const expected=EXPECTED_LOGIN;
let chooser_has_target=false, chooser_target_signed_out=false, chooser_present=false;
let chooser_target_authuser=null, chooser_entry_count=0, chooser_other_mode=false;
if(host==='accounts.google.com'&&path==='/v3/signin/accountchooser'){
  chooser_present=true;
  for(const e of all){
    if(!e.getAttribute||e.getAttribute('role')!=='link')continue;
    const id=e.getAttribute('data-identifier'), au=e.getAttribute('data-authuser');
    const kind=e.getAttribute('data-button-type');
    if(id===null||au===null||!vis(e))continue;
    // The same chooser also renders rows for "Remove account": identical role,
    // identifier and authuser, different data-button-type. Clicking one of those
    // would delete the account from the profile, so rows are counted and matched
    // only in the picker mode from the live dump.
    if(kind!=='multipleChoiceIdentifier'){chooser_other_mode=true;continue}
    chooser_entry_count++;
    if(id===expected){
      chooser_has_target=true;
      chooser_target_authuser=au;
      if(au==='-1') chooser_target_signed_out=true;
    }
  }
}
const denied=/(don't have permission|do not have permission|permission denied|you need access|request access)/i.test(body);
return {
  origin: location.origin, host, path,
  ready: document.readyState,
  app_uc: (app&&app.channel&&app.channel.channelId)||'',
  entity: (app&&app.currentEntity)?{type:app.currentEntity.type,id:app.currentEntity.id}:null,
  session_index: (window.ytcfg&&window.ytcfg.data_&&window.ytcfg.data_.SESSION_INDEX!==undefined
    &&window.ytcfg.data_.SESSION_INDEX!==null)?String(window.ytcfg.data_.SESSION_INDEX):'',
  chooser_present, chooser_has_target, chooser_target_signed_out,
  chooser_target_authuser, chooser_entry_count, chooser_other_mode,
  denied,
  signals: {
    verify: body.includes("Verify it's you"),
    phone: /(phone number|verify.*phone|confirm.*phone|by text message|get a verification code)/i.test(body),
    passkey: /(passkey|security key)/i.test(body),
    captcha: location.pathname.indexOf('/challenge/recaptcha')>=0
      ||[...document.querySelectorAll('iframe[src*="recaptcha"],div.g-recaptcha,#captchaimg')].some(vis)
      ||/(i'm not a robot|confirm you.?re not a robot|unusual traffic)/i.test(body),
    wrong_password: /(wrong password|incorrect password)/i.test(body),
    wrong_code: /(wrong code|incorrect code)/i.test(body),
    rate_limit: /(too many failed attempts|too many attempts|try again later)/i.test(body)
  }
};})()"""


def _eval(driver, expression, *, attempts=3, pause=time.sleep):
    """Read the page, tolerating the transient failures a navigating tab throws.

    A read that races a redirect raises once and succeeds immediately after, so a
    single throw must not end a recovery that is otherwise on track.  The callers
    are all inside deadline-bounded loops, which keeps this from hiding a page
    that is genuinely unreadable.
    """
    last = None
    for attempt in range(max(1, attempts)):
        try:
            return driver.execute_script("return " + expression)
        except Exception as exc:
            last = exc
            if attempt + 1 < max(1, attempts):
                pause(0.5)
    raise RecoveryBlocked("live page unreadable") from None


def classify(driver, channel_uc, login):
    """Return a structured, secret-free classification of the current tab.

    Categories (mutually exclusive ``state`` field):
      * ``channel_ready``          — the exact target channel is live.
      * ``verify``                 — in-Studio "Verify it's you" -> the existing
                                     upload_auth rendezvous owns this, not us.
      * ``phone``                  — phone/SMS/passkey/CAPTCHA -> owner help.
      * ``other_account_selected`` — Studio shows a *different* channel; the
                                     caller's own switching should run, not a
                                     sign-in.
      * ``studio_access_denied``   — Studio refuses the exact channel with no
                                     identity at all.  Live evidence (profile 22,
                                     14.09; profile 28, 16.09) shows this single
                                     page covers BOTH "the bound account is
                                     signed out" and "this account simply has no
                                     access": ``app.channel`` and
                                     ``currentEntity`` are null in both.  It is
                                     therefore ambiguous by construction and
                                     must be discriminated on the Google account
                                     chooser, never guessed from Studio.
      * ``google_signin``          — a Google sign-in page/chooser is showing.
      * ``signin_transition``      — youtube.com/signin or /signin_prompt:
                                     proves a login transition only, never
                                     channel access.
      * ``consent_required``       — the cookie consent wall; the owner answers
                                     it once, this module never clicks it.
      * ``unknown``                — anything else -> caller keeps its own path.
    """
    if UC_RE.fullmatch(channel_uc or "") is None:
        raise RecoveryBlocked("invalid target channel id")
    js = _CLASSIFY_JS.replace("EXPECTED_LOGIN", json.dumps(login))
    s = _eval(driver, js)
    if not isinstance(s, dict):
        raise RecoveryBlocked("live page unreadable")
    sig = s.get("signals") or {}
    host, path = s.get("host"), s.get("path", "")
    on_google = host == "accounts.google.com"
    on_studio = host == "studio.youtube.com"

    if sig.get("phone") or sig.get("passkey") or sig.get("captcha"):
        state = "phone"
    elif sig.get("verify") and on_studio:
        state = "verify"
    elif on_studio and s.get("app_uc") == channel_uc and s.get("entity") == {
        "type": "CHANNEL", "id": channel_uc
    } and not s.get("denied"):
        state = "channel_ready"
    elif on_studio and s.get("app_uc") and s.get("app_uc") != channel_uc:
        state = "other_account_selected"
    elif on_studio and (s.get("denied") or not s.get("app_uc")):
        state = "studio_access_denied"
    elif on_google and (path.startswith("/v3/signin/") or path == "/AddSession"
                        or s.get("chooser_present")):
        state = "google_signin"
    elif host == "consent.youtube.com":
        # The cookie wall blocks the sign-in route and is the owner's choice to
        # answer, not something this module clicks through on their behalf.
        state = "consent_required"
    elif host == "www.youtube.com" and path in ("/signin", "/signin_prompt"):
        state = "signin_transition"
    else:
        state = "unknown"
    return {"state": state, "snapshot": _safe_snapshot(s)}


def _safe_snapshot(s):
    """A copy safe to persist/log: structural flags only, no free text/values."""
    return {
        "host": s.get("host"), "path": s.get("path"), "ready": s.get("ready"),
        "app_uc": s.get("app_uc"), "entity": s.get("entity"),
        "session_index": s.get("session_index"),
        "chooser_present": s.get("chooser_present"),
        "chooser_has_target": s.get("chooser_has_target"),
        "chooser_target_signed_out": s.get("chooser_target_signed_out"),
        "chooser_target_authuser": s.get("chooser_target_authuser"),
        "chooser_entry_count": s.get("chooser_entry_count"),
        "chooser_other_mode": s.get("chooser_other_mode"),
        "denied": s.get("denied"), "signals": s.get("signals"),
    }


def _state_path(profile, channel_uc):
    directory = Path(os.getenv(STATE_DIR_ENV) or DEFAULT_STATE_DIR)
    return directory / f"{profile}_{channel_uc}.json"


def _read_state(profile, channel_uc):
    try:
        value = json.loads(_state_path(profile, channel_uc).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(profile, channel_uc, value):
    """Atomic, secret-free note of what happened, for the next process."""
    path = _state_path(profile, channel_uc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _blocked_by_recent_verdict(profile, channel_uc):
    now = time.time()
    state = _read_state(profile, channel_uc)
    when = state.get("owner_help_at")
    if isinstance(when, (int, float)) and 0 < now - when < OWNER_HELP_COOLDOWN_SECONDS:
        return True, "owner help already requested for this account recently"
    attempts = [t for t in (state.get("attempts") or [])
                if isinstance(t, (int, float)) and 0 < now - t < ATTEMPT_WINDOW_SECONDS]
    if len(attempts) >= MAX_ATTEMPTS_PER_WINDOW:
        return True, "too many recovery attempts for this account today"
    return False, ""


def _note_attempt(profile, channel_uc):
    now = time.time()
    state = _read_state(profile, channel_uc)
    attempts = [t for t in (state.get("attempts") or [])
                if isinstance(t, (int, float)) and 0 < now - t < ATTEMPT_WINDOW_SECONDS]
    attempts.append(now)
    state.update({"schema_version": 1, "profile": str(profile),
                  "channel_uc": channel_uc, "attempts": attempts[-10:]})
    _write_state(profile, channel_uc, state)


def _note_outcome(profile, channel_uc, outcome):
    now = time.time()
    state = _read_state(profile, channel_uc)
    state.update({"schema_version": 1, "profile": str(profile),
                  "channel_uc": channel_uc, "last_outcome": outcome,
                  "last_outcome_at": now})
    if outcome == STATUS_OWNER_HELP:
        state["owner_help_at"] = now
    elif outcome == STATUS_CHANNEL_CONFIRMED:
        state.pop("owner_help_at", None)
        state["attempts"] = []
    _write_state(profile, channel_uc, state)


# --- Reviewed auth bundle + exact credential binding ---------------------------

def _root():
    return Path(os.getenv(ROOT_ENV) or DEFAULT_ROOT)


def _accounts_path():
    override = os.getenv(ACCOUNTS_ENV)
    return Path(override) if override else (_root() / "private" / "google_accounts.json")


def _load_module(name, path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise RecoveryBlocked("reviewed auth bundle missing")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reviewed():
    root = _root()
    secrets = _load_module("recovery_secrets", root / "google_auth_secrets.py")
    reviewed = _load_module("recovery_auth_browser", root / "auth_browser.py")
    return secrets, reviewed


def _binding_login(secrets, profile, channel_uc):
    """Exact, fail-closed (profile, channel_uc) -> login from the private store.

    The file must be private (owner-only, no group/other bits).  Exactly one
    binding may match; anything else stops recovery without guessing.
    """
    path = _accounts_path()
    try:
        st = path.stat()
    except OSError:
        raise OwnerHelpNeeded("account binding store unavailable") from None
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise OwnerHelpNeeded("account binding store is not private")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise OwnerHelpNeeded("account binding store unreadable") from None
    if not isinstance(document, dict) or not isinstance(document.get("bindings"), list):
        raise OwnerHelpNeeded("account binding store malformed")
    matches = [
        b for b in document["bindings"]
        if isinstance(b, dict) and str(b.get("profile")) == str(profile)
        and b.get("channel_uc") == channel_uc and isinstance(b.get("login"), str)
    ]
    if len(matches) != 1:
        # No binding, or an ambiguous one, is exactly the "wrong binding /
        # needs owner" case — never sign into a guessed account.
        raise OwnerHelpNeeded("no exact account binding for this profile+channel")
    return secrets.load_credentials(path, matches[0]["login"])


# --- Bounded sign-in -----------------------------------------------------------

# What is actually under the point at the instant of the click. Between reading
# a row's rectangle and dispatching the press, the ownership re-proof can take
# seconds and the chooser can re-render; in the live dump "Remove an account" is
# the row immediately after "Use another account", so a one-row shift would click
# a destructive control. The press only happens if the element under the cursor
# is still the intended one.
def _hit_test_js(point, login):
    return r"""(()=>{
const e=document.elementFromPoint(POINT_X,POINT_Y);
if(!e)return null;
const row=e.closest('[data-button-type]');
const ctl=e.closest('[role="link"],[role="button"],button')||e;
return {btype:row?row.getAttribute('data-button-type'):'',
        identifier:row?(row.getAttribute('data-identifier')===EXPECTED):false,
        text:(ctl.innerText||'').replace(/\s+/g,' ').trim().toLowerCase().slice(0,40)};})()"""\
        .replace('POINT_X', json.dumps(float(point['x'])))\
        .replace('POINT_Y', json.dumps(float(point['y'])))\
        .replace('EXPECTED', json.dumps(login))


def _chooser_hit_is_safe(driver, point, login):
    """True only for the bound account's picker row or the exact 'Use another
    account' control; anything else (notably a removal row) means: do not click."""
    hit = _eval(driver, _hit_test_js(point, login))
    if not isinstance(hit, dict):
        return False
    if hit.get("btype") == "multipleChoiceIdentifier" and hit.get("identifier") is True:
        return True
    return (not hit.get("btype")) and hit.get("text") == "use another account"


def _click(driver, point, lease_check, *, verify=None):
    lease_check()
    driver.execute_cdp_cmd("Page.bringToFront", {})
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": point["x"], "y": point["y"]},
    )
    if callable(verify) and not verify():
        raise RecoveryBlocked("the control under the cursor changed; not clicking")
    for kind in ("mousePressed", "mouseReleased"):
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": point["x"], "y": point["y"],
             "button": "left", "clickCount": 1},
        )


MAX_CLOCK_OFFSET_SECONDS = 3.0
CLOCK_CACHE_SECONDS = 120.0
_CLOCK_CACHE = {}


def _clock_synced():
    """True only if this host's clock is demonstrably right for a 30s TOTP window.

    A 2FA code is a function of the clock, so an unchecked clock turns into a
    rejected code and a locked account.  Linux answers through timedatectl; the
    Mac (where an already-open profile may have to be recovered) has no such
    command, so the offset is measured directly — NTP first, then Google's own
    ``Date`` header.  A host that can prove nothing is refused, never assumed.
    """
    import subprocess
    # Only a positive verdict is cached, and only briefly: a "no" from a flaky
    # probe must not condemn every later attempt in this process.
    cached = _CLOCK_CACHE.get("value_at")
    if cached and time.monotonic() - cached < CLOCK_CACHE_SECONDS:
        return True
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip() == "yes":
            _CLOCK_CACHE["value_at"] = time.monotonic()
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        # Cheapest first: one HEAD against the host we are about to authenticate
        # to.  Its Date header has one-second granularity, which is far finer
        # than the tolerance that matters for a 30-second code.
        import email.utils
        import urllib.request
        before = time.time()
        with urllib.request.urlopen(
            urllib.request.Request("https://accounts.google.com/generate_204",
                                   method="HEAD"), timeout=6) as response:
            header = response.headers.get("Date")
        after = time.time()
        server = email.utils.parsedate_to_datetime(header).timestamp()
        if abs((before + after) / 2 - server) <= MAX_CLOCK_OFFSET_SECONDS:
            _CLOCK_CACHE["value_at"] = time.monotonic()
            return True
    except Exception:
        pass
    try:
        out = subprocess.run(["sntp", "-t", "2", "time.apple.com"],
                             capture_output=True, text=True, timeout=5)
        offsets = re.findall(r"([+-]\d+\.\d+)\s+\+/-", out.stdout or "")
        if offsets and abs(float(offsets[-1])) <= MAX_CLOCK_OFFSET_SECONDS:
            _CLOCK_CACHE["value_at"] = time.monotonic()
            return True
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return False


def _sign_in(driver, credentials, secrets, reviewed, lease_check, on_status, deadline,
             *, pause=time.sleep, wall_clock=time.time, monotonic=time.monotonic):
    """One bounded sign-in of the exact account.  Selenium-driven, one submission
    per stage, stops on any unexpected challenge.  Returns when Google leaves the
    accounts flow (Studio or /signin_prompt); the caller re-proves the exact UC.
    """
    submitted = set()
    announced_2fa = False
    stalled = 0
    last_stage = None
    while monotonic() < deadline:
        lease_check()
        state = _eval(driver, reviewed.READ_DOM)
        if not isinstance(state, dict):
            raise RecoveryBlocked("sign-in page unreadable")
        path = state.get("path") or ""
        # Живьём 16.09.2026: после принятого пароля Google показывает
        # /v3/signin/speedbump/passkeyenrollment — «Sign in faster», предложение
        # завести passkey. Это НЕ проверка личности, а необязательное
        # предложение уже вошедшему пользователю. Ничего на нём не нажимаем:
        # просто уходим, вердикт вынесет проверка канала. Иначе сигнал passkey
        # объявлял бы успешный вход «нужна помощь владельца».
        if state.get("origin") == "https://accounts.google.com" and path.startswith(
                "/v3/signin/speedbump/"):
            if path.rsplit("/", 1)[-1] not in SKIPPABLE_SPEEDBUMPS:
                raise OwnerHelpNeeded("Google asks for an extra step after sign-in")
            # Просто уйти со страницы НЕДОСТАТОЧНО: живьём 16.09.2026 после
            # ухода все аккаунты снова оказались вышедшими. Вход завершает
            # именно отказ от предложения — жмём «Not now», и только его.
            if "speedbump" not in submitted:
                point = _eval(driver, _SPEEDBUMP_SKIP_JS)
                if not point:
                    pause(0.5)
                    continue
                _click(driver, point, lease_check)
                submitted.add("speedbump")
                pause(2)
                continue
            pause(min(2.0, max(0.0, deadline - monotonic())))
            continue
        sig = dict(state.get("signals", {}) or {})
        extra = _eval(driver, _CHALLENGE_SIGNALS_JS)
        if isinstance(extra, dict):
            # Union of both readers: the reviewed one carries no CAPTCHA signal.
            for key, value in extra.items():
                sig[key] = bool(sig.get(key)) or bool(value) if key != "lang" else sig.get(key)
            if (extra.get("host") == "accounts.google.com"
                    and not str(extra.get("lang") or "").lower().startswith("en")):
                # The stop signals below are English-only; a localized page would
                # turn a rejected password into a silent timeout.
                raise OwnerHelpNeeded("Google sign-in page is not in English; refusing to type")
        if sig.get("wrong_password") or sig.get("wrong_code"):
            raise OwnerHelpNeeded("credential rejected; no automatic retry")
        if sig.get("phone") or sig.get("passkey") or sig.get("captcha") or sig.get("rate_limit"):
            raise OwnerHelpNeeded("interactive challenge requires the owner")
        origin = state.get("origin")
        if origin in ("https://studio.youtube.com", "https://www.youtube.com",
                      "https://m.youtube.com"):
            # Google finished with us and handed the browser back to YouTube.
            # Which page it chose proves nothing about channel access, so the
            # verdict is left to verify_exact_channel — not guessed here.
            return
        if origin != "https://accounts.google.com":
            raise OwnerHelpNeeded("unexpected sign-in destination")
        stage = path.rsplit("/", 1)[-1]
        # A page that never moves must be named, not waited out: burning the
        # whole budget hides which step is stuck and holds the profile lease
        # for nothing.
        stalled = stalled + 1 if stage == last_stage else 0
        last_stage = stage
        if stalled > MAX_STALLED_PROBES:
            raise RecoveryBlocked(f"sign-in stopped making progress at stage: {stage}")
        if stage in submitted:
            pause(min(2.0, max(0.0, deadline - monotonic())))
            continue
        if stage == "accountchooser":
            expr = _account_row_expression(credentials.login)
            point = _eval(driver, expr)
            if not point:
                # Bound account not offered here: fall to "Use another account".
                point = _eval(driver, _USE_ANOTHER_JS)
                if not point:
                    pause(0.5)
                    continue
            on_status(STATUS_RECOVERING)
            _click(driver, point, lease_check,
                   verify=lambda: _chooser_hit_is_safe(driver, point, credentials.login))
            submitted.add(stage)
        elif stage == "identifier":
            if _submit_identifier(driver, credentials, lease_check, deadline, pause=pause):
                submitted.add(stage)
        elif stage in ("pwd", "totp"):
            if stage == "totp" and not announced_2fa:
                on_status(STATUS_AWAITING_2FA)
                announced_2fa = True
            if _submit_secret(driver, credentials, secrets, reviewed, stage,
                              lease_check, deadline, pause=pause, wall_clock=wall_clock):
                submitted.add(stage)
        else:
            raise OwnerHelpNeeded("Google requires an additional sign-in step")
        pause(2)
    raise RecoveryBlocked("sign-in did not complete within the recovery deadline")


# The row that SELECTS the bound account. Deliberately a local copy of the
# reviewed predicate plus the one attribute the live dump shows distinguishes a
# picker row from a "Remove account" row — clicking the latter would delete the
# account from the profile. Values are never returned.
def _account_row_expression(login):
    return r"""(()=>{
const expected=EXPECTED;
if(location.hostname!=='accounts.google.com'||location.pathname!=='/v3/signin/accountchooser')return null;
const m=Array.from(document.querySelectorAll('div[role="link"][data-identifier]')).filter(e=>{
 const r=e.getBoundingClientRect(),s=getComputedStyle(e);
 return e.getAttribute('data-identifier')===expected
  && e.getAttribute('data-button-type')==='multipleChoiceIdentifier'
  && r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'
  && e.getAttribute('aria-disabled')!=='true';});
if(m.length!==1)return null;
const r=m[0].getBoundingClientRect();
return {x:r.x+r.width/2,y:r.y+r.height/2,origin:location.origin,path:location.pathname,account_matches:true};
})()""".replace('EXPECTED', json.dumps(login))


# "Use another account". Live dump research/dom/chooser_controls_p28_20260916.json
# (profile 28, 2026-09-16): this control and "Remove an account" are the SAME
# shape — div[role=link], same classes, no data-button-type, no id — and differ
# only by their caption and an obfuscated jsname (rwl3qc vs o7vT9b). There is no
# safe attribute to match on, so this runs only after the page is pinned to
# English, compares the caption exactly, refuses anything that looks like a
# removal, and demands a single candidate. Getting this wrong deletes an account
# from the profile, so ambiguity means "do nothing".
_USE_ANOTHER_JS = r"""(()=>{
if(location.hostname!=='accounts.google.com'||location.pathname!=='/v3/signin/accountchooser')return null;
if(!String(document.documentElement.lang||'').toLowerCase().startsWith('en'))return null;
const all=Array.from(document.querySelectorAll('div[role="link"],[role="button"],button')).filter(e=>{
 const r=e.getBoundingClientRect(),s=getComputedStyle(e);
 return !e.hasAttribute('data-identifier')&&!e.hasAttribute('data-button-type')
  &&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';});
const text=e=>(e.innerText||'').replace(/\s+/g,' ').trim().toLowerCase();
if(all.some(e=>/remove/.test(text(e))&&text(e)==='use another account'))return null;
const c=all.filter(e=>text(e)==='use another account'&&!/remove/.test(text(e)));
if(c.length!==1)return null;const r=c[0].getBoundingClientRect();
return {x:r.x+r.width/2,y:r.y+r.height/2,jsname:c[0].getAttribute('jsname')||''};
})()"""


# Отказ от необязательного предложения passkey. Жмём ТОЛЬКО «Not now»:
# кнопка создания passkey рядом, и промах завёл бы на профиле новый способ входа.
# Требуется английский язык (его модуль пиннит до любого действия).
_SPEEDBUMP_SKIP_JS = r"""(()=>{
if(location.hostname!=='accounts.google.com')return null;
if(location.pathname.indexOf('/v3/signin/speedbump/')!==0)return null;
if(!String(document.documentElement.lang||'').toLowerCase().startsWith('en'))return null;
function vis(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);
 return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&!e.disabled
        &&e.getAttribute('aria-disabled')!=='true';}
const all=[...document.querySelectorAll('button,[role="button"]')].filter(vis);
const txt=e=>(e.innerText||'').replace(/\s+/g,' ').trim().toLowerCase();
const skip=all.filter(e=>txt(e)==='not now');
if(skip.length!==1)return null;
const r=skip[0].getBoundingClientRect();
return {x:r.x+r.width/2,y:r.y+r.height/2,label:txt(skip[0])};})()"""


_IDENTIFIER_POINT_JS = r"""(()=>{
const e=document.querySelector('input#identifierId[name="identifier"][type="text"]');
if(!e)return null;const r=e.getBoundingClientRect(),s=getComputedStyle(e);
if(!(r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'))return null;
return {x:r.x+r.width/2,y:r.y+r.height/2};})()"""


def _submit_identifier(driver, credentials, lease_check, deadline, *, pause=time.sleep):
    from selenium.webdriver.common.by import By
    selector = 'input#identifierId[name="identifier"][type="text"]'

    def fresh(expected):
        return _eval(
            driver,
            "(function(sel,val){const e=document.querySelector(sel);"
            "return location.origin==='https://accounts.google.com'"
            "&&location.pathname==='/v3/signin/identifier'&&e"
            "&&document.activeElement===e&&e.value===val;})"
            "(%s,%s)" % (json.dumps(selector), json.dumps(expected)),
        ) is True

    fields = [e for e in driver.find_elements(By.CSS_SELECTOR, selector)
              if e.is_displayed() and e.is_enabled()]
    if len(fields) != 1 or fields[0].get_attribute("value") != "":
        raise RecoveryBlocked("one empty identifier field required")
    lease_check()
    driver.execute_cdp_cmd("Page.bringToFront", {})
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'})", fields[0])
    except Exception:
        pass
    try:
        fields[0].click()
    except Exception:
        point = _eval(driver, _IDENTIFIER_POINT_JS)
        if not point:
            return False
        _click(driver, point, lease_check)
    if not fresh(""):
        raise RecoveryBlocked("identifier field focus mismatch")
    driver.execute_cdp_cmd("Input.insertText", {"text": credentials.login})
    if not fresh(credentials.login):
        raise RecoveryBlocked("identifier native input not acknowledged")
    buttons = [e for e in driver.find_elements(By.CSS_SELECTOR, "button")
               if e.is_displayed() and e.is_enabled() and (e.text or "").strip() == "Next"]
    if len(buttons) != 1:
        raise RecoveryBlocked("one Next button required")
    lease_check()
    if not fresh(credentials.login):
        raise RecoveryBlocked("identifier changed before submit")
    buttons[0].click()
    pause(3)
    return True


def _submit_secret(driver, credentials, secrets, reviewed, stage, lease_check, deadline,
                   *, pause=time.sleep, wall_clock=time.time):
    from selenium.webdriver.common.by import By
    expr = (reviewed.password_expression if stage == "pwd"
            else reviewed.totp_expression)(credentials.login)
    form = _eval(driver, expr)
    if not form:
        pause(1)
        return False
    if not form.get("empty"):
        raise RecoveryBlocked("refuse to resubmit a populated credential form")
    if stage == "totp":
        if not _clock_synced():
            raise OwnerHelpNeeded("synchronized clock required for 2FA code")
        remaining = 30 - wall_clock() % 30
        if remaining < 12:
            pause(remaining + 0.2)
    selector = ('input[type="password"][name="Passwd"]' if stage == "pwd"
                else 'input[type="tel"][name="totpPin"][id="totpPin"]')
    inputs = [e for e in driver.find_elements(By.CSS_SELECTOR, selector)
              if e.is_displayed() and e.is_enabled()]
    if len(inputs) != 1:
        pause(0.5)
        return False
    lease_check()
    # Сначала прокрутить поле в середину экрана: живьём на Mac 16.09.2026 поле
    # пароля оказалось под шапкой, и родной клик Selenium отказался
    # (ElementClickInterceptedException). Прокрутка — не ввод, она ничего не
    # отправляет; после неё координаты перечитываются заново.
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'})", inputs[0])
    except Exception:
        pass
    try:
        inputs[0].click()
    except Exception:
        # Перекрыт другим элементом — кликаем нативно по свежим координатам
        # того же самого поля (тот же путь, что и в проверенном сценарии).
        fresh_point = _eval(driver, expr)
        if not (fresh_point and fresh_point.get("empty")):
            return False
        _click(driver, fresh_point["input"], lease_check)
    form = _eval(driver, expr)
    if form and form.get("empty") and not form.get("focused"):
        pause(0.3)
        _click(driver, form["input"], lease_check)
        form = _eval(driver, expr)
    if not (form and form.get("empty") and form.get("focused")):
        raise RecoveryBlocked("credential input focus mismatch")
    issued = wall_clock()
    if stage == "totp":
        secret, remaining = secrets.totp_code(credentials.totp, timestamp=issued)
        if remaining < 10:
            raise RecoveryBlocked("insufficient 2FA code lifetime")
    else:
        secret = credentials.password
    lease_check()
    fresh = _eval(driver, expr)
    if not (fresh and fresh.get("empty") and fresh.get("focused")):
        raise RecoveryBlocked("credential form changed before input")
    driver.execute_cdp_cmd("Input.insertText", {"text": secret})
    secret = None
    form = _eval(driver, expr)
    if not (form and not form.get("empty") and form.get("focused")):
        raise RecoveryBlocked("credential input not acknowledged")
    if stage == "totp":
        now = wall_clock()
        if int(now // 30) != int(issued // 30) or 30 - now % 30 < 5:
            raise RecoveryBlocked("2FA window expired before submission")
    _click(driver, form["next"], lease_check)
    pause(3)
    return True


# --- Exact-channel proof -------------------------------------------------------

def _current_session_index(driver):
    """The account slot the browser itself reports for the page it is on."""
    try:
        value = driver.execute_script(
            "return (window.ytcfg&&window.ytcfg.data_&&"
            "window.ytcfg.data_.SESSION_INDEX!==undefined&&"
            "window.ytcfg.data_.SESSION_INDEX!==null)?"
            "String(window.ytcfg.data_.SESSION_INDEX):''")
    except Exception:
        return ""
    return value if isinstance(value, str) and value.isdigit() else ""


def verify_exact_channel(driver, channel_uc, lease_check, *, timeout=30, pause=time.sleep,
                         account_slots=0, monotonic=time.monotonic):
    """Prove live access to the exact target UC, by three independent facts:
    the Studio URL is that channel, ``ytcp-app.channel.channelId`` is that UC,
    and ``currentEntity`` is that CHANNEL — with no permission-denied text.

    The YouTube home page and ``/signin_prompt`` only prove a login transition,
    never channel access, so they never satisfy this.  Routing hints
    (``authuser``) are updated by the caller only from this live proof.

    A freshly restored account is usually NOT slot 0 of the profile, so a bare
    channel URL would open somebody else's session and look like a refusal.  The
    slots are therefore tried in order of evidence: first the one the browser
    reports right now (the session that just signed in), then the default, then
    the remaining slots the account chooser actually showed — a bounded list, not
    a blind sweep.  Studio's router needs ``/channel/UC?authuser=N``; the form
    with a slash before the query breaks it.
    """
    if UC_RE.fullmatch(channel_uc or "") is None:
        raise RecoveryBlocked("invalid target channel id")
    deadline = monotonic() + timeout
    base = f"https://studio.youtube.com/channel/{channel_uc}"
    attempts, seen = [], set()
    current = _current_session_index(driver)
    if current:
        attempts.append(current)
        seen.add(current)
    attempts.append(None)                      # profile default
    try:
        slots = int(account_slots)
    except (TypeError, ValueError):
        slots = 0
    for index in range(max(0, min(slots, 8))):
        if str(index) not in seen:
            seen.add(str(index))
            attempts.append(str(index))
    snapshot = None
    for slot in attempts:
        if monotonic() >= deadline:
            break
        lease_check()
        driver.get(base + ("/" if slot is None else f"?authuser={slot}"))
        started = monotonic()
        slot_deadline = min(deadline, started + SLOT_PROOF_SECONDS)
        while monotonic() < slot_deadline:
            lease_check()
            c = classify(driver, channel_uc, login="")
            snapshot = c["snapshot"]
            if c["state"] == "channel_ready":
                return {"confirmed": True, "channel_uc": channel_uc,
                        "session_index": snapshot.get("session_index") or (slot or ""),
                        "snapshot": snapshot}
            # A refusal counts only from a page that finished loading and had a
            # moment to render: a slow Studio looks identical to a refusal, and
            # calling it "no access" too early is exactly the old false abort.
            if (c["state"] in ("studio_access_denied", "other_account_selected")
                    and snapshot.get("ready") == "complete"
                    and monotonic() - started >= SLOT_SETTLE_SECONDS):
                break                          # this slot is answered: try the next
            pause(1)
    return {"confirmed": False, "channel_uc": channel_uc, "snapshot": snapshot}


# The "Add account" link, identified by where it GOES, not by what it says.
# Live shape: a#endpoint with href https://accounts.google.com/AddSession...
# (research/ww2_finish_first_20260914/profile21_login/add_account_step.py, 14.09).
_ADD_SESSION_LINK_JS = r"""(()=>{
const out=[];
for(const a of document.querySelectorAll('a[href]')){
 let u; try{u=new URL(a.href)}catch(e){continue}
 if(u.protocol!=='https:'||u.hostname!=='accounts.google.com'||u.pathname!=='/AddSession')continue;
 const r=a.getBoundingClientRect(),s=getComputedStyle(a);
 if(r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden')out.push(a.href);
}
return out.length===1?out[0]:null;})()"""


def _pin_english(driver, lease_check):
    """Re-open the current accounts.google.com URL with hl=en and prove it took.

    Everything that reads the sign-in pages — the reviewed Next/password/TOTP
    predicates and every STOP signal (wrong password, wrong code, phone, rate
    limit) — matches English text.  Profile 28's chooser was live-captured in
    zh-HK, so a page that merely *looks* like a sign-in is not enough: unless the
    document really reports English, this module must not type at all, otherwise
    a rejected password degrades into a silent timeout instead of a clear stop.
    """
    lease_check()
    try:
        route = urlsplit(driver.current_url or "")
    except Exception:
        raise RecoveryBlocked("live page unreadable") from None
    if route.hostname != "accounts.google.com":
        return False
    existing = dict(parse_qsl(route.query, keep_blank_values=True)).get("hl", "")
    if not existing.lower().startswith("en"):
        query = [(k, v) for k, v in parse_qsl(route.query, keep_blank_values=True)
                 if k != "hl"] + [("hl", "en")]
        lease_check()
        driver.get(urlunsplit((route.scheme, route.netloc, route.path,
                               urlencode(query), "")))
    lang = _eval(driver, "String(document.documentElement.lang||'')")
    return isinstance(lang, str) and lang.lower().startswith("en")


def _open_account_chooser(driver, lease_check, *, pause=time.sleep, timeout=25,
                          monotonic=time.monotonic):
    """Navigate the existing tab to Google's account chooser, in English.

    Evidence for the route: on 2026-09-16 the signed-out profile 28 held
    ``accounts.google.com/v3/signin/accountchooser?continue=https://www.youtube.com/signin``
    — i.e. YouTube's own sign-in route is what produces the chooser.  The UI
    language is then pinned to English the same way the proven
    ``login_account.sign_in`` does it, because the reviewed password/TOTP
    predicates match an English form; the account rows themselves are matched by
    ``data-identifier``, never by language.

    Two routes, because one is not enough.  When the profile has no session at
    all, YouTube's sign-in route lands on the chooser by itself.  But a profile
    can hold several accounts: if another one is still signed in, that same route
    simply signs YouTube in as *them* and never shows a chooser.  The second
    route is then the observed "Add account" link on youtube.com/account, whose
    href goes to ``accounts.google.com/AddSession`` (proven live 14.09.2026 in
    research/ww2_finish_first_20260914/profile21_login/add_account_step.py).  The
    link is matched by its href host+path, never by its caption.

    This only navigates.  It opens no new window, closes nothing, signs nobody
    out, and touches no other account in the profile.
    """
    deadline = monotonic() + timeout

    def settle():
        """Return the chooser/accounts URL once Google shows one, else ''."""
        while monotonic() < deadline:
            lease_check()
            try:
                route = urlsplit(driver.current_url or "")
            except Exception:
                raise RecoveryBlocked("live page unreadable") from None
            if route.hostname == "accounts.google.com":
                _pin_english(driver, lease_check)
                return "chooser"
            if route.hostname == "consent.youtube.com":
                return "consent"
            if route.hostname == "www.youtube.com" and route.path not in ("/signin",):
                return "youtube"          # already signed in as somebody
            pause(1)
        return ""

    lease_check()
    driver.get("https://www.youtube.com/signin?hl=en")
    landed = settle()
    if landed in ("chooser", "consent"):
        return landed == "chooser"

    # Чужая сессия уже активна: обычный вход просто входит как она и чузер не
    # показывает. Тогда идём тем же адресом, что стоит за пунктом «Add account»
    # в меню аккаунта (его путь /AddSession снят с живой ссылки 14.09.2026,
    # research/ww2_finish_first_20260914/profile21_login/add_account_step.py).
    # Живая проверка 16.09.2026: этот адрес отдаёт страницу выбора на английском
    # со всеми аккаунтами профиля. Страница настроек youtube.com/account ссылок
    # на accounts.google.com не содержит — пункт живёт только в меню под аватаром.
    lease_check()
    driver.get("https://accounts.google.com/AddSession?hl=en"
               "&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin")
    return settle() == "chooser"


# --- Public entry point --------------------------------------------------------

def enabled():
    return os.getenv(ENABLE_ENV) == "1"


# Внутренние причины -> то, что человек прочитает в телефоне.
_HUMAN_REASON = {
    "interactive challenge requires the owner": "капча или другая проверка «это точно ты»",
    "credential rejected; no automatic retry": "Google отклонил пароль или код — данные устарели",
    "channel needs owner action (phone verification)": "нужно подтверждение телефона",
    "cookie consent page must be answered by the owner once": "висит окно про cookies, его надо закрыть один раз",
    "Google sign-in page is not in English; refusing to type": "страница входа не на английском — не ввожу вслепую",
    "Google requires an additional sign-in step": "Google просит лишний шаг входа",
    "Google asks for an extra step after sign-in": "Google просит лишний шаг после входа",
    "account chooser is not in account-picking mode": "список аккаунтов в режиме удаления — не трогаю",
    "bound account is signed in but has no access to this channel": "аккаунт вошёл, но доступа к каналу нет",
    "account is signed in; the exact channel is not accessible": "аккаунт вошёл, но канал недоступен",
    "signed in but exact channel not confirmed": "вход прошёл, но канал не подтвердился",
    "no exact account binding for this profile+channel": "нет точной привязки аккаунта к каналу",
    "owner help already requested for this account recently": "повтор: помощь уже запрошена недавно",
    "too many recovery attempts for this account today": "слишком много попыток за сутки — пауза",
    "unexpected sign-in destination": "вход увёл на неизвестную страницу",
    "synchronized clock required for 2FA code": "часы машины не синхронизированы — код 2FA не отправляю",
    "account binding store unavailable": "не найден файл с данными входа",
    "account binding store unreadable": "файл с данными входа не читается",
    "account binding store malformed": "файл с данными входа испорчен",
    "account binding store is not private": "у файла с данными входа слишком открытые права",
}


def _notify_owner(profile, channel_uc, reason, label=""):
    """Пинг оператору в Telegram. Ровно там, где записан НОВЫЙ вердикт
    «нужен человек», — повторные отказы по остыванию молчат, иначе на каждый
    ролик очереди приходило бы одно и то же сообщение."""
    try:
        import upload_notify
    except ImportError:
        return False
    human = _HUMAN_REASON.get(str(reason).strip(), str(reason))
    hint = ("Открой профиль и пройди проверку руками, потом заливка повторится."
            if "challenge" in reason or "captcha" in reason.lower()
            or "owner" in reason.lower() else "")
    try:
        return upload_notify.owner_help(profile, channel_uc, human,
                                        host=socket.gethostname(), label=label,
                                        hint=hint)
    except Exception:
        return False


def maybe_recover(driver, profile, channel_uc, lease_check, *,
                  ownership_check=None, evidence_dir=None, channel_label="",
                  notify=_notify_owner,
                  on_status=None, phase_before_upload=True, pause=time.sleep,
                  deadline_seconds=RECOVERY_DEADLINE_SECONDS, wall_clock=time.time,
                  monotonic=time.monotonic):
    """Try to recover a signed-out bound account and re-prove the exact channel.

    Returns:
      * ``None``  — recovery is disabled or not applicable (caller proceeds as if
                    this module did not exist);
      * dict ``{"recovered": True, ...}`` — signed back in and the exact UC is
                    live; the caller should continue the upload;
      * raises :class:`OwnerHelpNeeded` — a human must act; the caller must abort
                    this attempt (never resetting the durable phase to ready).

    ``lease_check`` must raise if the profile lease / manager supervision was
    lost.  ``phase_before_upload`` must be True: recovery only runs before the
    file hand-off, so it can never cause a re-upload.
    """
    if not enabled():
        return None
    if not callable(lease_check):
        # No lease, no recovery.  A sign-in without the same exclusive hold the
        # upload runs under could collide with another worker on this profile.
        raise RecoveryBlocked("account recovery requires the profile lease guard")
    if not phase_before_upload:
        # Belt-and-braces: a signed-out state after upload_started is the
        # existing in-upload Verify rendezvous' concern, not a fresh sign-in.
        raise RecoveryBlocked("account recovery is only valid before file hand-off")
    if PROFILE_RE.fullmatch(str(profile) or "") is None or UC_RE.fullmatch(channel_uc or "") is None:
        raise RecoveryBlocked("invalid profile or channel for recovery")

    def status(value, *, remember=True, reason=""):
        if remember and value in (STATUS_OWNER_HELP, STATUS_CHANNEL_CONFIRMED):
            _note_outcome(profile, channel_uc, value)
            if value == STATUS_OWNER_HELP and callable(notify):
                notify(profile, channel_uc, reason or "вход требует человека",
                       channel_label)
        if callable(on_status):
            try:
                on_status(value)
            except Exception:
                pass

    def guard():
        # Both halves of "this profile is mine": the cross-host lease, and the
        # browser this driver is actually attached to.
        lease_check()
        if callable(ownership_check):
            ownership_check()

    def intent(stage):
        """Durable, secret-free note that a step is about to happen."""
        if not evidence_dir:
            return
        try:
            directory = Path(evidence_dir)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"recovery_{stage}_{int(time.time())}.intent.json"
            with path.open("x", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "profile": str(profile),
                           "channel_uc": channel_uc, "stage": stage,
                           "monotonic": monotonic()}, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass

    blocked, reason = _blocked_by_recent_verdict(profile, channel_uc)
    if blocked:
        # Cheapest possible refusal: no navigation, no credentials, no Google
        # traffic.  The operator already knows; repeating it per queued video
        # only wastes minutes and risks a rate limit.
        raise OwnerHelpNeeded(reason)

    guard()
    secrets, reviewed = _reviewed()
    # Resolve the exact binding first: no binding == owner help, before any click.
    credentials = _binding_login(secrets, profile, channel_uc)

    initial = classify(driver, channel_uc, credentials.login)
    state = initial["state"]
    if state in ("channel_ready", "verify", "other_account_selected", "unknown"):
        # Nothing to recover, or the caller's own switching / the existing
        # in-upload Verify rendezvous owns it.
        return None
    if state == "phone":
        status(STATUS_OWNER_HELP, reason="channel needs owner action (phone verification)")
        raise OwnerHelpNeeded("channel needs owner action (phone verification)")
    if state == "consent_required":
        status(STATUS_OWNER_HELP, reason="cookie consent page must be answered by the owner once")
        raise OwnerHelpNeeded("cookie consent page must be answered by the owner once")
    if state not in ("studio_access_denied", "google_signin", "signin_transition"):
        return None

    # Prove the clock before anything is typed, not between two credential
    # steps: the probe can need a network round trip, and spending it mid-flow
    # would eat the recovery budget and risk a stale code.
    if not _clock_synced():
        status(STATUS_OWNER_HELP, reason="synchronized clock required for 2FA code")
        raise OwnerHelpNeeded("synchronized clock required for 2FA code")

    deadline = monotonic() + float(deadline_seconds)
    # Studio's refusal page cannot tell "signed out" from "no access": both show
    # a null app.channel.  Ask the account chooser, which states it per account.
    if state != "google_signin":
        intent("open_chooser")
        _open_account_chooser(driver, guard, pause=pause, monotonic=monotonic)
    chooser = classify(driver, channel_uc, credentials.login)
    snap = chooser["snapshot"]
    if chooser["state"] == "consent_required":
        status(STATUS_OWNER_HELP, reason="cookie consent page must be answered by the owner once")
        raise OwnerHelpNeeded("cookie consent page must be answered by the owner once")
    if chooser["state"] != "google_signin":
        # Visiting the sign-in route landed somewhere signed-in: the account was
        # never signed out, so this is an access problem, not a login problem.
        status(STATUS_OWNER_HELP, reason="account is signed in; the exact channel is not accessible")
        raise OwnerHelpNeeded("account is signed in; the exact channel is not accessible")
    if snap.get("chooser_other_mode"):
        # The chooser is showing rows that are not account pickers — the live
        # dump's other mode is "Remove an account". Clicking there would delete
        # an account, so this stops immediately instead of hunting for a row.
        status(STATUS_OWNER_HELP, reason="account chooser is not in account-picking mode")
        raise OwnerHelpNeeded("account chooser is not in account-picking mode")
    if snap.get("chooser_present") and snap.get("chooser_has_target") \
            and not snap.get("chooser_target_signed_out"):
        status(STATUS_OWNER_HELP, reason="bound account is signed in but has no access to this channel")
        raise OwnerHelpNeeded("bound account is signed in but has no access to this channel")

    if not _pin_english(driver, guard):
        status(STATUS_OWNER_HELP, reason="Google sign-in page is not in English; refusing to type")
        raise OwnerHelpNeeded("Google sign-in page is not in English; refusing to type")

    status(STATUS_SIGNED_OUT)
    _note_attempt(profile, channel_uc)
    intent("sign_in")
    try:
        _sign_in(driver, credentials, secrets, reviewed, guard, status, deadline,
                 pause=pause, wall_clock=wall_clock, monotonic=monotonic)
    except OwnerHelpNeeded as exc:
        status(STATUS_OWNER_HELP, reason=str(exc))
        raise
    finally:
        # Drop the secret-bearing object as soon as sign-in ends, success or not.
        credentials = None

    intent("verify_channel")
    slots = snap.get("chooser_entry_count")
    proof = verify_exact_channel(driver, channel_uc, guard,
                                 timeout=CHANNEL_PROOF_SECONDS,
                                 pause=pause, monotonic=monotonic,
                                 account_slots=slots if isinstance(slots, int) else 0)
    if not proof.get("confirmed"):
        status(STATUS_OWNER_HELP, reason="signed in but exact channel not confirmed")
        raise OwnerHelpNeeded("signed in but exact channel not confirmed")
    status(STATUS_CHANNEL_CONFIRMED)
    return {"recovered": True, "channel_uc": channel_uc,
            "session_index": proof.get("session_index"),
            "snapshot": proof.get("snapshot")}
