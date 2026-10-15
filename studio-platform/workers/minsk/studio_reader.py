"""Read-only Studio audit, selectors/props observed on Minsk 2026-10-04.

Evidence: evidence/probe2/cmd_103,109,112,122.result.json and country_control.
No verification/SMS or profile lifecycle. The only optional mutation is
setting an explicitly unset country to Argentina, under exact guards.
"""
from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import re
import shutil
import time
from urllib.parse import parse_qs, urlsplit
import uuid


class IdentityError(RuntimeError):
    pass


class PermissionEntryError(IdentityError):
    pass


class CountrySaveUnconfirmed(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, str(path) + ".bak")
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


JS_ID = """
const a=document.querySelector('ytcp-app');
const cfg=window.ytcfg&&window.ytcfg.data_||{};
return {url:location.href,ready:document.readyState,lang:document.documentElement.lang,
 uc:a&&a.channel?a.channel.channelId:null,
 entity_id:a&&a.currentEntity?a.currentEntity.id:null,
 entity_type:a&&a.currentEntity?a.currentEntity.type:null,
 session_index:cfg.SESSION_INDEX===undefined?null:String(cfg.SESSION_INDEX),
 delegated_session_id:cfg.DELEGATED_SESSION_ID===undefined?null:cfg.DELEGATED_SESSION_ID,
 datasync_id:cfg.DATASYNC_ID===undefined?null:cfg.DATASYNC_ID};
"""
JS_MENU_ITEMS = """
function deep(r){let a=[];for(const e of r.querySelectorAll('*')){a.push(e);if(e.shadowRoot)a.push(...deep(e.shadowRoot));}return a;}
return deep(document).filter(e=>e.tagName==='YTD-ACCOUNT-ITEM-RENDERER').filter(e=>{
const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';
}).map(e=>({element:e,data:e.data||null}));
"""
JS_ANALYTICS = """
return [...document.querySelectorAll('yta-key-metric-block')].map(e=>{
 const d=e.polymerController&&e.polymerController.__data;
 const p=d&&d.keyMetricTab&&d.keyMetricTab.primaryContent;
 return p?{label:d.label,metric:p.metric,total:p.total,totalTooltip:p.totalTooltip,
 scope:p.exploreConfig&&p.exploreConfig.restrictAndTimePeriodConfig}:null;
}).filter(Boolean);
"""
JS_HUB_METRICS = """
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
const lines=e=>(e?.innerText||'').split('\\n').map(x=>x.trim()).filter(Boolean);
const sections=[...document.querySelectorAll('ytcp-item-section-renderer')].filter(e=>visible(e)&&lines(e)[0]==='Key metrics');
return {url:location.href,document_ready:document.readyState,sections:sections.map(section=>({
 heading:lines(section)[0],text:section.innerText,
 periods:[...section.querySelectorAll('button[role=tab],button[role=combobox]')].filter(visible).map(e=>({text:e.innerText.trim(),role:e.getAttribute('role'),aria_selected:e.getAttribute('aria-selected')})),
 metrics:[...section.querySelectorAll('ytcp-simple-metric-view-model')].filter(visible).map(e=>{
 const card=e.closest('yt-card-view-model');return {label:card&&section.contains(card)?lines(card)[0]:null,
 raw_value:e.innerText.trim(),card_text:card&&section.contains(card)?card.innerText:null,
 card_metric_count:card?card.querySelectorAll('ytcp-simple-metric-view-model').length:null};})
}))};
"""
JS_HUB_ADVANCED = """
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
const pages=[...document.querySelectorAll('yta-explore-page')].filter(visible),p=pages.length===1?pages[0]:null;
return {url:location.href,document_ready:document.readyState,page_count:pages.length,
 current_view_config:p?.currentViewConfig||null,entity_config:p?.entityConfig||null,
 visible_text:p?.innerText||'',
 table_headers:p?[...p.querySelectorAll('yta-explore-table-header')].filter(visible).map(e=>e.innerText):[],
 raw_total_rows:p?[...p.querySelectorAll('yta-explore-table-row.total-row')].filter(visible).map(r=>({
 model:r.row||null,title:r.querySelector('#entity-title-value')?.innerText.trim()||null,text:r.innerText,
 visible_metric_values:[...r.querySelectorAll('.debug-metric-value')].filter(visible).map(e=>e.innerText.trim())})):[]};
"""
WELCOME_TEXT = ("Welcome to YouTube Studio YouTube Studio has personalized insights, the latest news & "
                "insider tips, analytics, and more Continue")
PERMISSION_TEXT = ("Oops, you don't have permission to view this page "
                   "You need to be signed into an account that has access. Try switching or signing into "
                   "an account that has permission. Switch account Return to Studio")
COUNTRY_WARNING = "You have not set a Country of Residence yet"
JS_COUNTRY = """
const app=document.querySelector('ytcp-app');
const e=document.querySelector('#country-of-residence-select');
const b=document.querySelector('ytcp-button#submit-button');
return {coreSettings:app&&app.channel&&app.channel.settings?app.channel.settings.coreSettings:null,
 country_text:e?e.innerText:null,
 save:b?{text:b.innerText,disabled:b.disabled,aria_disabled:b.getAttribute('aria-disabled')}:null};
"""


def normalized(text):
    return " ".join((text or "").split())


def studio_route_uc(state):
    url = urlsplit((state or {}).get("url") or "")
    if url.scheme != "https" or url.netloc != "studio.youtube.com":
        return None
    match = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)", url.path)
    return match[1] if match else None


def exact_permission_entry(state, visible_text, expected_uc):
    # Live p39_control cmd713: this page is a bootstrap opportunity, never
    # evidence of channel access and never an identity verification success.
    return (studio_route_uc(state) == expected_uc and state.get("ready") == "complete"
            and not state.get("uc") and not state.get("entity_id")
            and normalized(visible_text) == PERMISSION_TEXT)


def bootstrap_context(state):
    """Pin an already signed-in Studio context solely to open its account menu."""
    uc = studio_route_uc(state)
    au = str((state or {}).get("session_index"))
    page = str((state or {}).get("delegated_session_id") or "")
    sync = (state or {}).get("datasync_id")
    if (not uc or state.get("ready") != "complete" or not au.isdigit()
            or not isinstance(sync, str) or not re.fullmatch(r"\d+\|\|\d*", sync)):
        return None
    first, second = sync.split("||")
    if (page and (not page.isdigit() or first != page or not second)) or (not page and second):
        return None
    expected = {"uc": uc, "authuser": int(au), "page_id": page or None, "datasync_id": sync}
    return expected if runtime_context_matches(expected, state) else None


def primary_retry_context(expected, state):
    """Observed C11: crossing Google accounts can first open its remembered brand."""
    if expected.get("page_id") is not None:
        return None
    primary_sync = expected.get("datasync_id")
    if not isinstance(primary_sync, str) or not re.fullmatch(r"\d+\|\|", primary_sync):
        return None
    current = bootstrap_context(state)
    if (not current or not current["page_id"] or current["uc"] == expected["uc"]
            or current["authuser"] != expected["authuser"]
            or current["datasync_id"].split("||")[1] != primary_sync[:-2]):
        return None
    return current


def parse_menu_identity(data):
    """Decode the observed account-item tokens; no name/selected-only fallback."""
    tokens = (data or {}).get("serviceEndpoint", {}).get("selectActiveIdentityEndpoint", {}).get("supportedTokens", [])
    values = {"uc": set(), "authuser": set(), "page_id": set(), "datasync_id": set()}
    for token in tokens:
        key = token.get("offlineCacheKeyToken", {}).get("clientCacheKey")
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{22}", key):
            values["uc"].add("UC" + key)
        signin = token.get("accountSigninToken", {}).get("signinUrl")
        if isinstance(signin, str):
            values["authuser"].update(parse_qs(urlsplit(signin).query).get("authuser", []))
        page = token.get("pageIdToken", {}).get("pageId")
        if page is not None:
            values["page_id"].add(str(page))
        sync = token.get("datasyncIdToken", {}).get("datasyncIdToken")
        if isinstance(sync, str) and sync:
            values["datasync_id"].add(sync)
    if any(len(values[k]) != 1 for k in ("uc", "authuser", "datasync_id")) or len(values["page_id"]) > 1:
        return None
    out = {k: next(iter(v)) if v else None for k, v in values.items()}
    if not out["authuser"].isdigit():
        return None
    out.update(authuser=int(out["authuser"]), selected=(data or {}).get("isSelected") is True)
    return out


def runtime_context_matches(expected, state):
    return (isinstance(expected, dict) and isinstance(state, dict)
            and state.get("uc") == expected.get("uc")
            and state.get("entity_id") == expected.get("uc") and state.get("entity_type") == "CHANNEL"
            and str(state.get("session_index")) == str(expected.get("authuser"))
            and str(state.get("delegated_session_id") or "") == str(expected.get("page_id") or "")
            and bool(expected.get("datasync_id")) and state.get("datasync_id") == expected.get("datasync_id"))


def numeric(raw, integer=False):
    """Only English exact numeric values; abbreviations/unknown locale stay null."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().replace("\u00a0", " ").replace("\u202f", " ")
    if not re.fullmatch(r"[+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
        return None
    number = float(value.replace(",", ""))
    return int(number) if integer and number.is_integer() else number if not integer else None


def capture_screenshot(driver, filename=None):
    """Capture the page surface without activating its browser window."""
    png = base64.b64decode(driver.execute_cdp_cmd(
        "Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"])
    if filename is None:
        return png
    try:
        with open(filename, "wb") as output:
            output.write(png)
    except OSError:
        return False
    return True


def wake_render(driver, check, observations, reason):
    """Minsk 2026-10-04: one screenshot wakes paused background UI rendering.

    This is a read-only rendering nudge, never evidence that a control is ready.
    Failures are recorded; normal scope/readiness checks still decide success.
    """
    check()
    started = time.monotonic()
    observation = {"at": now(), "reason": reason}
    try:
        png = capture_screenshot(driver)
        observation["status"] = "ok" if isinstance(png, bytes) and png else "empty_screenshot"
    except Exception as exc:
        observation.update(status="error", error=type(exc).__name__ + ": " + str(exc)[:300])
    observation["elapsed_seconds"] = round(time.monotonic() - started, 3)
    observations.append(observation)
    check()  # Lease failures must propagate, including during a screenshot.


def parse_features(text):
    fields = {}
    names = {1: "Стандартные функции", 2: "Расширенные функции", 3: "Продвинутые функции"}
    states = {"Enabled": "enabled", "Not enabled": "not_enabled", "Disabled": "disabled",
              "Eligible": "eligible", "Not eligible": "not_eligible",
              "Включены": "enabled", "Можно включить": "eligible"}  # P50 verified RU cards, 2026-10-04.
    for match in re.finditer(r"(?ms)^([123])\.\s*([^\n]+)\n(.*?)(?=^[123]\.\s|\Z)", text):
        level = int(match[1])
        raw = [line.strip() for line in match[3].splitlines() if line.strip() in states]
        state = states[raw[0]] if len(raw) == 1 else None
        fields[str(level)] = {"label": names[level], "source_label": match[2].strip(),
                              "status": state, "enabled": state == "enabled" if state else None,
                              "raw_status": raw[0] if len(raw) == 1 else None,
                              "raw_section": match[0], "reason": None if state else "status_not_uniquely_read"}
    return {"status": "complete" if all(fields.get(str(k), {}).get("status") for k in (2, 3)) else "partial",
            "levels": fields, "raw_text": text}


def parse_lifetime(rows, expected_uc, visible_text=""):
    fields = {}
    for code, key, label, integer in [("EXTERNAL_VIEWS", "views", "Views", True),
                                      ("EXTERNAL_WATCH_TIME", "watch_hours", "Watch time (hours)", False)]:
        candidates = [r for r in rows if r.get("metric") == code]
        verified = [r for r in candidates if r.get("scope", {}).get("entity", {}).get("id") == expected_uc
                    and r.get("scope", {}).get("entity", {}).get("type") == "CHANNEL"
                    and r.get("scope", {}).get("timePeriod", {}).get("type") == "lifetime"
                    and r.get("label") in {label, "Просмотры" if integer else "Время просмотра (часы)"}]
        row = verified[0] if len(verified) == 1 else {}
        value = numeric(row.get("totalTooltip"), integer=integer)
        # A non-abbreviated total is exact enough when the tooltip is absent.
        if value is None and row and not row.get("totalTooltip"):
            value = numeric(row.get("total"), integer=integer)
        fields[key] = {"value": value, "unit": "views" if integer else "hours", "source": row or None,
                       "availability": "reported" if value is not None else "unknown",
                       "reason": None if value is not None else "exact_numeric_value_or_lifetime_scope_not_verified"}
    explicit_no_activity = (bool(re.search(r"Your channel hasn['’]t gotten any views yet", visible_text, re.I))
                            or "Ваши видео пока никто не смотрел" in visible_text.splitlines())
    no_activity = explicit_no_activity and all(f["source"] and f["source"].get("total") == "—"
                                              and not f["source"].get("totalTooltip") for f in fields.values())
    if no_activity:
        for field in fields.values():
            field.update(value=None, availability="not_reported_no_activity",
                         capture_status="observed_no_activity", reason="Studio displays dash; no views yet")
    numeric_complete = all(f["value"] is not None for f in fields.values())
    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    dates = [lines[i - 1] for i, line in enumerate(lines) if line in {"Lifetime", "Все время"} and i > 0
             and re.search(r"\d", lines[i - 1]) and re.search(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|20\d{2})", lines[i - 1])]
    dates = list(dict.fromkeys(dates))
    return {"status": "complete" if numeric_complete or no_activity else "partial",
            "numeric_complete": numeric_complete,
            "capture_status": "observed_no_activity" if no_activity else "reported" if numeric_complete else "unknown",
            **fields, "period": "lifetime", "raw_metric_blocks": rows,
            "visible_date_range": dates[0] if len(dates) == 1 else None,
            "visible_date_range_source": "visible line immediately before observed Lifetime/Все время",
            "visible_date_range_reason": None if len(dates) == 1 else "date_range_not_uniquely_observed"}


def parse_hub_lifetime(snapshot, expected_uc):
    """UI evidence only: cmd_507..514, never infer backend scope or chart values."""
    sections = snapshot.get("sections") or []
    section = sections[0] if len(sections) == 1 and sections[0].get("heading") == "Key metrics" else {}
    periods = section.get("periods") or []
    selected = [p for p in periods if p.get("aria_selected") == "true"]
    period_ok = len(selected) == 1 and selected[0].get("text") == "Lifetime"
    url = urlsplit(snapshot.get("url") or "")
    route_ok = url.hostname == "studio.youtube.com" and url.path == f"/channel/{expected_uc}/analytics/hub_overview"
    rows = section.get("metrics") or []
    fields = {}
    for key, label, integer in (("views", "Views", True), ("watch_hours", "Watch time (hours)", False)):
        matches = [r for r in rows if r.get("label") == label and r.get("card_metric_count") == 1]
        row = matches[0] if len(matches) == 1 else None
        source = ({"source_type": "hub_ui", "label": label, "raw_value": row.get("raw_value"),
                   "section": "Key metrics", "card_text": row.get("card_text")} if row else None)
        value = numeric(row.get("raw_value"), integer=integer) if row else None
        if not (period_ok and route_ok):
            value = None
        fields[key] = {"value": value, "unit": "views" if integer else "hours", "source": source,
                       "availability": "reported" if value is not None else "unknown",
                       "reason": None if value is not None else "exact_numeric_value_or_hub_lifetime_selection_not_verified"}
    views_text = (fields["views"]["source"] or {}).get("card_text") or ""
    dates = list(dict.fromkeys(re.findall(
        r"(?m)^\s*((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, \d{4})\s*$", views_text)))
    numeric_complete = all(f["value"] is not None for f in fields.values())
    complete = numeric_complete and snapshot.get("document_ready") == "complete" and len(dates) == 2
    return {"status": "complete" if complete else "partial", "source_type": "hub_ui",
            "period": "lifetime", "observed_period": selected[0].get("text") if len(selected) == 1 else None,
            "ui_period_verified": period_ok and route_ok, "key_metrics_section": "Key metrics" if section else None,
            "ui_period_evidence": periods, "raw_metric_blocks": rows, "numeric_complete": numeric_complete,
            "capture_status": "reported" if complete else "unknown", **fields,
            "observed_dates": dates, "visible_date_range": " — ".join(dates) if len(dates) == 2 else None,
            "visible_date_range_source": "Views card inside Key metrics",
            "visible_date_range_reason": None if len(dates) == 2 else "date_range_not_uniquely_observed"}


def parse_hub_advanced_lifetime(snapshot, expected_uc):
    """Observed hub_control cmd918/922/923: exact UI Total, at UI precision."""
    config, entity = snapshot.get("current_view_config") or {}, snapshot.get("entity_config") or {}
    url = urlsplit(snapshot.get("url") or "")
    query = parse_qs(url.query)
    observed_query_keys = {"entity_id", "entity_type", "time_period", "explore_type", "metrics_computation_type",
                           "metric", "granularity", "t_metrics", "v_metrics", "dimension", "o_column", "o_direction"}
    route_ok = (url.scheme == "https" and url.netloc == "studio.youtube.com"
                and url.path == f"/channel/{expected_uc}/analytics/hub_overview/explore"
                and query.get("entity_id") == [expected_uc] and query.get("entity_type") == ["CHANNEL"]
                and query.get("time_period") == ["lifetime"] and set(query) <= observed_query_keys
                and query.get("metrics_computation_type") == ["DELTA"])
    model_ok = (snapshot.get("page_count") == 1 and config.get("entity") == {"id": expected_uc, "type": "CHANNEL"}
                and config.get("timePeriod") == {"type": "lifetime"} and config.get("viewType") == "MAIN"
                and set(config) <= {"entity", "timePeriod", "userHasRevenueAccess", "viewType"}
                and entity.get("id") == expected_uc and entity.get("type") == "CHANNEL")
    lines = [s.strip() for s in snapshot.get("visible_text", "").splitlines() if s.strip()]
    period_ok = lines.count("Lifetime") == 1
    dates = [lines[i - 1] for i, line in enumerate(lines) if line == "Lifetime" and i > 0
             and re.search(r"20\d{2}", lines[i - 1])]
    rows = snapshot.get("raw_total_rows") or []
    row = rows[0] if len(rows) == 1 else {}
    model = row.get("model") or {}
    cells, visible = model.get("metricCells") or [], row.get("visible_metric_values") or []
    total_ok = (model.get("dimensionCell", {}).get("id") == "ALL"
                and model.get("dimensionCell", {}).get("title") == "Total" and row.get("title") == "Total"
                and bool(cells) and len(cells) == len(visible)
                and [c.get("absoluteValue") for c in cells] == visible)
    headers = snapshot.get("table_headers") or []
    header_lines = [s.strip() for s in headers[0].splitlines()] if len(headers) == 1 else []
    scope_ok = route_ok and model_ok and period_ok and total_ok
    fields = {}
    for key, code, label, integer in (("views", "EXTERNAL_VIEWS", "Views", True),
                                      ("watch_hours", "EXTERNAL_WATCH_TIME", "Watch time (hours)", False)):
        found = [(i, c) for i, c in enumerate(cells) if c.get("metric") == code]
        cell = found[0][1] if len(found) == 1 else {}
        shown = visible[found[0][0]] if len(found) == 1 and found[0][0] < len(visible) else None
        value = numeric(cell.get("absoluteValue"), integer=integer)
        if not scope_ok or header_lines.count(label) != 1:
            value = None
        fields[key] = {"value": value, "unit": "views" if integer else "hours",
            "availability": "reported" if value is not None else "unknown",
            "reason": None if value is not None else "exact_advanced_lifetime_total_not_verified",
            "source": {"source_type": "hub_advanced_lifetime_total", "metric": code, "label": label,
                       "raw_value": cell.get("absoluteValue"), "visible_value": shown,
                       "total_row_id": model.get("dimensionCell", {}).get("id"), "total_row_title": row.get("title")}}
    numeric_complete = all(f["value"] is not None for f in fields.values())
    complete = numeric_complete and snapshot.get("document_ready") == "complete"
    return {"status": "complete" if complete else "partial", "source_type": "hub_advanced_lifetime_total",
            "period": "lifetime", "observed_period": "Lifetime" if period_ok else None,
            "ui_period_verified": route_ok and period_ok, "advanced_scope_verified": scope_ok,
            "ui_period_evidence": {"visible_text": snapshot.get("visible_text"), "lifetime_line_count": lines.count("Lifetime"),
                                   "visible_periods": [line for line in lines if line == "Lifetime"]},
            "numeric_complete": numeric_complete, "capture_status": "reported" if complete else "unknown",
            "precision": "as displayed in the Advanced mode Total row", **fields,
            "visible_date_range": dates[0] if len(dates) == 1 else None,
            "visible_date_range_source": "Advanced mode visible line immediately before Lifetime",
            **{k: snapshot.get(k) for k in ("url", "document_ready", "page_count", "current_view_config",
                                           "entity_config", "raw_total_rows", "table_headers")}}


def parse_earn(text):
    values = {}
    patterns = {"qualified_watch_hours_365d": r"([\d][\d,.]*)\s+qualified watch hours?\s+last 365 days",
                "qualified_shorts_views_90d": r"([\d][\d,.]*)\s+qualified Shorts views\s+last 90 days",
                "subscribers": r"([\d][\d,.]*)\s+subscribers?\b"}
    for key, pattern in patterns.items():
        sources = re.findall(pattern, text, re.I)
        ru_patterns = {"qualified_watch_hours_365d": r"(?m)^(\d+) подходящих часов просмотра\s+за последние 365 дней",
                       "qualified_shorts_views_90d": r"(?m)^(\d+) подходящих просмотров Shorts\s+за последние 90 дней"}
        if key in ru_patterns:
            sources += re.findall(ru_patterns[key], text)
        elif key == "subscribers":
            sources += ["0" for line in text.splitlines() if line.strip() == "Нет подписчиков"]
        parsed = [numeric(x, integer=True) for x in sources]
        unique = set(parsed)
        value = parsed[0] if parsed and len(unique) == 1 and None not in unique else None
        values[key] = {"value": value, "raw_values": sources,
                       "availability": "reported" if value is not None else "unknown",
                       "reason": None if value is not None else "metric_missing_or_conflicting"}
    dates = list(dict.fromkeys(x.strip() for x in re.findall(r"Showing data as of:\s*([^\n]+)", text)))
    dates = list(dict.fromkeys(dates + [x.strip() for x in re.findall(r"(?m)^Данные на ([^\n]+)", text)]))
    exact_country_warning = COUNTRY_WARNING in normalized(text)
    needs_country = exact_country_warning or bool(re.search(r"(?:choose|select|set)\s+(?:your\s+|a\s+)?(?:country|country/region)|country.{0,50}(?:not selected|not set)", text, re.I))
    partner_observed = any(line.strip() in {"You're a YouTube Partner", "You’re a YouTube Partner"}
                           for line in text.splitlines())
    numeric_complete = values["qualified_watch_hours_365d"]["value"] is not None
    partner_without_counter = (partner_observed and not needs_country
                               and not re.search(r"qualified watch hours?", text, re.I))
    if partner_without_counter:
        values["qualified_watch_hours_365d"].update(
            value=None, availability="not_displayed_ypp_member",
            reason="Studio does not display qualified watch hours on this YPP member page")
    complete = (numeric_complete and len(dates) == 1) or partner_without_counter
    return {"status": "complete" if complete else "needs_country" if needs_country else "partial",
            "capture_complete": complete, "numeric_complete": numeric_complete,
            "capture_status": "observed_ypp_member" if partner_without_counter else "reported" if complete else "unknown",
            "partner_status": "observed_ypp_member" if partner_observed else None,
            **values, "as_of": dates[0] if len(dates) == 1 else None,
            "as_of_sources": dates, "period_days": 365, "needs_country": needs_country,
            "exact_unset_country_warning": exact_country_warning,
            "country_changed": False, "raw_text": text}


def read_channel(driver, expected_uc, output_dir, options):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    check = options.get("assert_held", lambda: None)
    context = options.get("context") or {}
    au = context.get("authuser")
    country_option = options.get("set_unset_country")
    if country_option not in (None, "AR"):
        raise ValueError("set_unset_country supports only explicitly authorized AR")
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", expected_uc) or type(au) is not int or not context.get("email"):
        raise ValueError("Reader requires valid UC and confirmed authuser/email context")
    base = f"https://studio.youtube.com/channel/{expected_uc}"
    result = {"schema_version": 1, "uc": expected_uc, "context": context, "started_at": now(),
              "status": "partial", "features": None, "lifetime": None, "earn": None,
              "render_wake_attempts": []}
    active_expectation = None

    def wake(reason):
        wake_render(driver, check, result["render_wake_attempts"], reason)

    def navigate(url):
        check()
        if "authuser" in parse_qs(urlsplit(url).query):
            raise IdentityError("authuser navigation would reset the selected brand runtime")
        driver.get(url)
        wake("after_navigation")

    def identity(timeout=35):
        deadline = time.monotonic() + timeout
        next_wake = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            check()
            last = driver.execute_script(JS_ID)
            url = urlsplit(last["url"])
            match = re.search(r"^/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)", url.path)
            if "accounts.google." in (url.hostname or ""):
                raise IdentityError("need_auth")
            if url.hostname != "studio.youtube.com" or (match and match[1] != expected_uc):
                raise IdentityError("wrong_origin_or_channel")
            if (last.get("ready") == "complete" and not last.get("uc")
                    and exact_permission_entry(last, text(), expected_uc)):
                raise PermissionEntryError("exact Studio permission entry page")
            if (match and last.get("uc") == expected_uc and last.get("entity_id") == expected_uc
                    and last.get("entity_type") == "CHANNEL" and last.get("ready") == "complete"):
                if active_expectation is None or runtime_context_matches(active_expectation, last):
                    return last
            if time.monotonic() >= next_wake:
                wake("identity_not_ready")
                next_wake = time.monotonic() + 5
            time.sleep(1)
        raise IdentityError("exact_channel_identity_unverified: " + json.dumps(last, ensure_ascii=False))

    def wait_value(read, ready, timeout=35):
        deadline, last = time.monotonic() + timeout, None
        next_wake = time.monotonic() + 5
        while time.monotonic() < deadline:
            check()
            last = read()
            if ready(last):
                return last
            # Background Chrome may pause render after a late network response.
            # Wake only while pending, at most once per five seconds.
            if time.monotonic() >= next_wake:
                wake("wait_value_not_ready")
                next_wake = time.monotonic() + 5
            time.sleep(1)
        return last

    def text():
        return driver.execute_script("return document.body ? document.body.innerText : '';")

    def click(selector, label=None):
        def find():
            check()
            elements = driver.find_elements("css selector", selector)
            return [e for e in elements if e.is_displayed() and e.is_enabled()
                    and (label is None or e.text.strip() == label)]
        found = wait_value(find, lambda es: len(es) == 1)
        if not found or len(found) != 1:
            raise RuntimeError("unique_visible_control_missing: " + selector + ":" + str(label))
        identity(timeout=3)
        check()
        try:
            found[0].click()
        except Exception as exc:
            observed_controls = {"button#avatar-btn": "avatar",
                                 "tp-yt-paper-icon-item#settings-item": "settings"}
            control = observed_controls.get(selector)
            if selector == 'button[role="combobox"]' and label == "More":
                control = "hub_more"  # P51 exact Welcome intercepted this native period control.
            if control is None or type(exc).__name__ != "ElementClickInterceptedException":
                raise
            wake(control + "_intercepted_before_welcome_check")
            if not dismiss_observed_welcome("welcome_after_" + control + "_intercept"):
                raise  # Unknown overlay: keep the original intercepted-click failure.
            retry = wait_value(find, lambda es: len(es) == 1)
            if not retry or len(retry) != 1:
                raise RuntimeError("unique control missing after exact welcome dismissal: " + selector)
            identity(timeout=3)
            check()
            retry[0].click()  # Exactly one native retry; a second failure propagates.
        wake("after_click:" + selector)

    def evidence(stage, value):
        record = {"at": now(), "identity": driver.execute_script(JS_ID), "visible_text": text(), "data": value}
        screenshot = root / f"{stage}.png"
        if screenshot.exists():
            shutil.copy2(screenshot, str(screenshot) + ".bak")
        try:
            if not capture_screenshot(driver, str(screenshot)):
                raise RuntimeError("screenshot_returned_false")
            record["screenshot"] = screenshot.name
        except Exception as exc:
            record["screenshot_error"] = type(exc).__name__ + ": " + str(exc)[:300]
        save(root / f"{stage}.json", record)
        value["evidence"] = str(root / f"{stage}.json")
        return value

    def dismiss_observed_welcome(evidence_prefix="welcome", pinned_guard=None):
        dialogs = [e for e in driver.find_elements("css selector", "ytcp-dialog#welcome-dialog") if e.is_displayed()]
        if not dialogs:
            return False
        if len(dialogs) != 1 or normalized(dialogs[0].text) != WELCOME_TEXT:
            raise RuntimeError("unknown welcome dialog; no dismissal authorized")
        guard = pinned_guard if pinned_guard is not None else lambda: identity(timeout=5)
        guard()
        evidence(evidence_prefix + "_before", {"dialog_text": dialogs[0].text, "status": "observed_exact_welcome"})
        if pinned_guard is None:
            click("ytcp-dialog#welcome-dialog ytcp-button#dismiss-button", "Continue")
        else:
            # Only this exact, previously observed welcome may be dismissed
            # in the pinned working context before target activation.
            buttons = wait_value(lambda: [e for e in driver.find_elements(
                "css selector", "ytcp-dialog#welcome-dialog ytcp-button#dismiss-button")
                if e.is_displayed() and e.is_enabled() and normalized(e.text) == "Continue"], lambda es: len(es) == 1)
            if not buttons or len(buttons) != 1:
                raise IdentityError("unique pinned-context welcome Continue missing")
            guard()
            fresh = [e for e in driver.find_elements("css selector", "ytcp-dialog#welcome-dialog") if e.is_displayed()]
            if len(fresh) != 1 or normalized(fresh[0].text) != WELCOME_TEXT:
                raise IdentityError("pinned-context welcome changed before Continue")
            check()
            buttons[0].click()
            wake("after_pinned_welcome_continue")
        remaining = wait_value(lambda: [e for e in driver.find_elements("css selector", "ytcp-dialog#welcome-dialog")
                                        if e.is_displayed()], lambda es: not es)
        guard()
        evidence(evidence_prefix + "_after", {"status": "dismissed" if not remaining else "still_visible"})
        if remaining:
            raise RuntimeError("welcome dismissal not confirmed; no repeat click")
        return True

    def return_from_permission_entry():
        # Observed native control: p39_control cmd713..715. One Return only;
        # no sign-in, verification, channel creation or arbitrary redirect.
        check()
        if not exact_permission_entry(driver.execute_script(JS_ID), text(), expected_uc):
            raise IdentityError("permission bootstrap trigger changed")
        result["entry_bootstrap"] = evidence("permission_before_bootstrap", {
            "status": "observed_permission_page", "return_clicks": 0,
            "source": "p39_control cmd713..715"})
        links = [e for e in driver.find_elements("css selector", 'a.button.tonal[href="/"]')
                 if e.is_displayed() and e.is_enabled() and normalized(e.text) == "Return to Studio"]
        if len(links) != 1:
            raise IdentityError("unique observed Return to Studio control missing")
        check()
        if not exact_permission_entry(driver.execute_script(JS_ID), text(), expected_uc):
            raise IdentityError("permission bootstrap trigger changed before Return")
        result["entry_bootstrap"].update(status="return_requested", return_clicks=1)
        save(root / "reader_result.json", result)
        links[0].click()
        wake("after_permission_return_to_studio")
        def working_state():
            state = driver.execute_script(JS_ID)
            parsed = urlsplit(state.get("url") or "")
            if parsed.scheme != "https" or parsed.netloc != "studio.youtube.com":
                raise IdentityError("permission bootstrap left Studio; no authentication attempted")
            return state if bootstrap_context(state) else None
        state = wait_value(working_state, lambda value: value is not None)
        if state is None:
            raise IdentityError("Return to Studio did not establish a verified working context")
        result["entry_bootstrap"].update(status="working_context_observed", working_runtime=state)
        evidence("permission_after_return", result["entry_bootstrap"])
        return bootstrap_context(state)

    def read_hub_lifetime():
        identity()
        before = wait_value(lambda: driver.execute_script(JS_HUB_METRICS),
                            lambda s: len(s.get("sections") or []) == 1
                            and bool(s["sections"][0].get("periods")) and bool(s["sections"][0].get("metrics")))
        before_value = parse_hub_lifetime(before or {}, expected_uc)
        evidence("hub_lifetime_before", {"snapshot": before, "parsed": before_value})
        already_lifetime = before_value["ui_period_verified"]
        if not already_lifetime:
            click('button[role="combobox"]', "More")
            click('button[role="menuitem"]', "Lifetime")
        previous_signature, stable_count = None, 0
        def read_stable():
            nonlocal previous_signature, stable_count
            identity(timeout=5)
            snapshot = driver.execute_script(JS_HUB_METRICS)
            value = parse_hub_lifetime(snapshot, expected_uc)
            dates_before, dates_after = before_value["observed_dates"], value["observed_dates"]
            date_changed = bool(len(dates_before) == 2 and len(dates_after) == 2 and dates_before != dates_after)
            transition_ok = already_lifetime or date_changed
            signature = json.dumps({"periods": value["ui_period_evidence"], "dates": dates_after,
                                    "metrics": value["raw_metric_blocks"]}, sort_keys=True)
            ready = (value["ui_period_verified"] and len(dates_after) == 2
                     and snapshot.get("document_ready") == "complete" and transition_ok)
            stable_count = stable_count + 1 if ready and signature == previous_signature else 1 if ready else 0
            previous_signature = signature
            value["transition_evidence"] = {"already_lifetime": already_lifetime,
                "date_range_before": dates_before, "date_range_after": dates_after, "date_changed": date_changed,
                "stable_snapshots": stable_count, "document_ready": snapshot.get("document_ready"),
                "readiness_source": "selected UI period, date transition and repeated stable Key metrics"}
            value["hub_ui_ready_for_advanced"] = ready and stable_count >= 2
            if not value["hub_ui_ready_for_advanced"]:
                value.update(status="partial", capture_status="unknown",
                             reason="hub_lifetime_transition_or_stability_not_verified")
            return value
        value = wait_value(read_stable, lambda value: value["status"] == "complete"
                           or value.get("hub_ui_ready_for_advanced") is True)
        if value["status"] == "complete" or not value.get("hub_ui_ready_for_advanced"):
            return value
        evidence("hub_lifetime_before_advanced", value)
        selector = 'yt-card-view-model.ytcpAdaptiveCardGridViewModelKeyMetricsCard > div[role="button"]'
        def view_buttons():
            return [e for e in driver.find_elements("css selector", selector) if e.is_displayed() and e.is_enabled()
                    and driver.execute_script("""
const card=arguments[0].closest('yt-card-view-model'),section=card?.closest('ytcp-item-section-renderer');
const first=e=>(e?.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean)[0];
return first(card)==='Views'&&first(section)==='Key metrics';
""", e)]
        buttons = wait_value(view_buttons, lambda es: len(es) == 1)
        if not buttons or len(buttons) != 1:
            raise IdentityError("unique observed Lifetime Views detail button missing")
        identity(timeout=5)
        fresh_hub = parse_hub_lifetime(driver.execute_script(JS_HUB_METRICS), expected_uc)
        if not fresh_hub["ui_period_verified"]:
            raise IdentityError("Lifetime selection changed before Advanced mode click")
        check()
        buttons[0].click()
        wake("after_native_lifetime_views_detail_click")
        previous_advanced, stable_advanced = None, 0
        def advanced_stable():
            nonlocal previous_advanced, stable_advanced
            identity(timeout=5)
            snapshot = driver.execute_script(JS_HUB_ADVANCED)
            advanced = parse_hub_advanced_lifetime(snapshot, expected_uc)
            signature = json.dumps(snapshot, sort_keys=True)
            ready = advanced["status"] == "complete"
            stable_advanced = stable_advanced + 1 if ready and signature == previous_advanced else 1 if ready else 0
            previous_advanced = signature
            advanced["transition_evidence"] = {"stable_snapshots": stable_advanced,
                "document_ready": snapshot.get("document_ready"),
                "source": "native Lifetime Views click; exact model and visible Total stable twice",
                "hub_before": value["transition_evidence"]}
            if not ready or stable_advanced < 2:
                advanced.update(status="partial", capture_status="unknown",
                                reason="advanced_lifetime_scope_total_or_stability_not_verified")
            return advanced
        advanced = wait_value(advanced_stable, lambda v: v["status"] == "complete")
        return evidence("hub_advanced_lifetime_total", advanced)

    def activate_exact_identity(entry_context=None):
        nonlocal active_expectation
        def menu_guard(menu_context=None):
            pinned = menu_context if menu_context is not None else entry_context if active_expectation is None else None
            if pinned is None:
                return identity(timeout=5)
            check()
            state = driver.execute_script(JS_ID)
            if bootstrap_context(state) != pinned:
                raise IdentityError("working Studio context changed before target activation")
            return state
        def open_menu(menu_context=None):
            if menu_context is not None or (entry_context is not None and active_expectation is None):
                # This restricted guard is used only to activate the target.
                # Unknown overlays fail; the global identity guard is unchanged.
                avatars = wait_value(lambda: [e for e in driver.find_elements("css selector", "button#avatar-btn")
                                              if e.is_displayed() and e.is_enabled()], lambda es: len(es) == 1)
                if not avatars or len(avatars) != 1:
                    raise IdentityError("unique bootstrap avatar missing")
                menu_guard(menu_context)
                check()
                try:
                    avatars[0].click()
                except Exception as exc:
                    if type(exc).__name__ != "ElementClickInterceptedException":
                        raise
                    wake("pinned_avatar_intercepted_before_welcome_check")
                    if not dismiss_observed_welcome("welcome_after_pinned_avatar_intercept",
                                                    pinned_guard=lambda: menu_guard(menu_context)):
                        raise
                    retry = wait_value(lambda: [e for e in driver.find_elements("css selector", "button#avatar-btn")
                                                if e.is_displayed() and e.is_enabled()], lambda es: len(es) == 1)
                    if not retry or len(retry) != 1:
                        raise IdentityError("unique pinned avatar missing after exact welcome dismissal")
                    menu_guard(menu_context)
                    check()
                    retry[0].click()  # One retry only, with the same pinned runtime.
                wake("after_bootstrap_avatar_click")
            else:
                click("button#avatar-btn")
            def switch_items():
                elements = driver.find_elements("css selector", "ytd-compact-link-renderer")
                return [e for e in elements if e.is_displayed() and driver.execute_script(
                    "return arguments[0].data?.icon?.iconType==='SWITCH_ACCOUNTS';", e)]
            switches = wait_value(switch_items, lambda es: len(es) == 1)
            if not switches or len(switches) != 1:
                raise IdentityError("unique account switch menu action not found")
            menu_guard(menu_context)
            check()
            switches[0].click()
            wake("after_account_switch_menu_click")
            return wait_value(lambda: driver.execute_script(JS_MENU_ITEMS), lambda rows: bool(rows)) or []
        def exact(rows):
            matches = []
            for row in rows:
                parsed = parse_menu_identity(row.get("data"))
                if parsed and parsed["uc"] == expected_uc and parsed["authuser"] == au:
                    matches.append((row, parsed))
            return matches
        rows = open_menu()
        save(root / "active_menu_before.json", {"at": now(), "items": [r.get("data") for r in rows]})
        matches = exact(rows)
        if len(matches) != 1:
            raise IdentityError("menu target UC/authuser/tokens not uniquely verified")
        row, expected = matches[0]
        # ALWAYS click: isSelected can remain true while ?authuser reset ytcfg.
        menu_guard()
        check()
        row["element"].click()
        wake("after_exact_channel_native_click")
        active_expectation = expected
        try:
            runtime = identity()
        except IdentityError as first_error:
            # Strict identity stays unchanged. Retry only the observed
            # cross-Google primary->remembered-brand outcome, once.
            first_state = driver.execute_script(JS_ID)
            retry_context = primary_retry_context(expected, first_state)
            if str(first_error) != "wrong_origin_or_channel" or retry_context is None:
                raise
            retry = {"status": "observed_same_owner_brand", "repeat_clicks": 0,
                     "expected_primary": expected, "first_runtime": first_state,
                     "source": "C11 10:52:39 and redirect_control cmd809"}
            result["primary_selection_retry"] = evidence("primary_first_selection", retry)
            retry_rows = open_menu(retry_context)
            save(root / "primary_retry_menu.json", {"at": now(), "items": [r.get("data") for r in retry_rows]})
            selected_contexts = [parse_menu_identity(r.get("data")) for r in retry_rows
                                 if (r.get("data") or {}).get("isSelected") is True]
            current_state = menu_guard(retry_context)
            if len(selected_contexts) != 1 or not runtime_context_matches(selected_contexts[0], current_state):
                raise IdentityError("primary retry selected brand menu does not prove current runtime")
            repeat = exact(retry_rows)
            keys = ("uc", "authuser", "page_id", "datasync_id")
            if len(repeat) != 1 or any(repeat[0][1][k] != expected[k] for k in keys):
                raise IdentityError("primary retry exact UC/authuser/datasync tokens changed")
            retry.update(status="repeat_requested", repeat_clicks=1, selected_brand=selected_contexts[0],
                         verified_brand_runtime=current_state)
            evidence("primary_before_repeat", retry)
            save(root / "reader_result.json", result)
            menu_guard(retry_context)
            check()
            repeat[0][0]["element"].click()
            wake("after_primary_repeat_native_click")
            runtime = identity()  # No second retry, fallback or weakened guard.
            retry.update(status="primary_runtime_verified", final_runtime=runtime)
            evidence("primary_after_repeat", retry)
        fresh_rows = open_menu()
        save(root / "active_menu_after.json", {"at": now(), "items": [r.get("data") for r in fresh_rows]})
        selected = exact(fresh_rows)
        if (len(selected) != 1 or selected[0][1].get("selected") is not True
                or any(selected[0][1][k] != expected[k] for k in ("uc", "authuser", "page_id", "datasync_id"))):
            raise IdentityError("fresh selected menu item does not match the activated runtime")
        # This closes the menu and proves query-free Studio navigation preserves
        # the selected runtime before any Settings read or permitted mutation.
        navigate(base)
        after_navigation = identity()
        proof = {**expected, "selected": True, "context_verified": True,
                 "runtime": runtime, "after_navigation_runtime": after_navigation,
                 "source": "native_menu_click_then_exact_delegated_and_datasync_runtime",
                 "at": now()}
        result["active_identity_proof"] = evidence("active_identity", proof)
        result["active_identity_verified"] = True
        result["identity"] = after_navigation
        if entry_context is not None:
            result["entry_bootstrap"].update(status="target_context_verified", target_runtime=after_navigation)
        save(root / "reader_result.json", result)

    def set_unset_argentina(before):
        """Live exact sequence in country_control cmd_202..236, 2026-10-04."""
        # Broad country wording alone never authorizes a settings mutation.
        if country_option != "AR" or COUNTRY_WARNING not in normalized(before.get("raw_text")):
            return before
        identity()
        before = evidence("earn_before_country", before)
        change = {"requested_country": "AR", "country_changed": False, "status": "checking_unset",
                  "trigger": COUNTRY_WARNING, "started_at": now(), "context": context}
        result["country_change"] = change
        initial = driver.execute_script(JS_COUNTRY)
        change["initial"] = initial
        # Even a contradicting warning must never overwrite an existing country.
        core = initial.get("coreSettings") or {}
        if core.get("country") or core.get("featureCountry"):
            change.update(status="not_changed_existing_country", reason="coreSettings country already populated")
            save(root / "reader_result.json", result)
            return before
        navigate(base)
        identity()
        click("tp-yt-paper-icon-item#settings-item")
        click("li#channel")
        state = wait_value(lambda: driver.execute_script(JS_COUNTRY),
                           lambda x: bool(normalized(x.get("country_text"))))
        if not state or normalized(state.get("country_text")) != "Country of residence Select":
            change.update(status="not_changed_unset_unverified", observed=state)
            save(root / "reader_result.json", result)
            return before
        identity(timeout=5)
        evidence("country_before_selection", state)
        click("#country-of-residence-select ytcp-dropdown-trigger")
        options_ar = wait_value(
            lambda: [e for e in driver.find_elements("css selector", "tp-yt-paper-item[test-id='AR']")
                     if e.is_displayed() and e.is_enabled() and normalized(e.text) == "Argentina"
                     and e.get_property("value") == "AR"], lambda es: len(es) == 1)
        if not options_ar or len(options_ar) != 1:
            raise RuntimeError("Argentina option is not uniquely visible with value AR")
        identity(timeout=5)
        check()
        options_ar[0].click()
        wake("after_country_option_click")
        def save_ready(x):
            b = x.get("save") or {}
            return (normalized(x.get("country_text")) == "Country of residence Argentina"
                    and normalized(b.get("text")) == "Save" and b.get("disabled") is False
                    and b.get("aria_disabled") == "false")
        selected = wait_value(lambda: driver.execute_script(JS_COUNTRY), save_ready)
        if not selected or not save_ready(selected):
            raise RuntimeError("Argentina selection or enabled Save not verified")
        identity(timeout=5)
        change.update(status="save_pending", selection=selected)
        evidence("country_before_save", selected)
        save(root / "reader_result.json", result)
        check()
        # Atomic identity+country+button verification immediately before Save.
        driver.execute_script("""
const uc=arguments[0],a=document.querySelector('ytcp-app'),e=document.querySelector('#country-of-residence-select');
const b=document.querySelector('ytcp-button#submit-button');
const cfg=window.ytcfg&&window.ytcfg.data_||{};
const norm=s=>(s||'').split(/\\s+/).filter(Boolean).join(' ');
if(!a||a.channel?.channelId!==uc||a.currentEntity?.id!==uc||a.currentEntity?.type!=='CHANNEL')throw Error('country save identity mismatch');
if(String(cfg.SESSION_INDEX)!==String(arguments[1])||String(cfg.DELEGATED_SESSION_ID||'')!==String(arguments[2]||'')||cfg.DATASYNC_ID!==arguments[3])throw Error('country save active runtime mismatch');
if(location.origin!=='https://studio.youtube.com'||location.pathname!=='/channel/'+uc&& !location.pathname.startsWith('/channel/'+uc+'/'))throw Error('country save route mismatch');
if(!e||norm(e.innerText)!=='Country of residence Argentina')throw Error('country selection changed');
if(!b||b.disabled!==false||b.getAttribute('aria-disabled')!=='false'||norm(b.innerText)!=='Save'||!b.getClientRects().length)throw Error('Save not enabled');
b.click();
""", expected_uc, au, active_expectation["page_id"], active_expectation["datasync_id"])
        change["status"] = "save_dispatched_verification_pending"
        save(root / "reader_result.json", result)
        wake("after_country_save")
        # A hidden Save/dialog is not a persistence ACK: navigating at that
        # point can cancel the in-flight request. Observe the current page's
        # channel model first, before performing ANY navigation.
        def read_save_ack():
            identity(timeout=3)
            return driver.execute_script(JS_COUNTRY)
        def save_acknowledged(state):
            core = (state or {}).get("coreSettings") or {}
            return core.get("country") == "AR" and core.get("featureCountry") == "AR"
        ack = wait_value(read_save_ack, save_acknowledged, timeout=35)
        change["save_ack"] = ack
        evidence("country_save_ack", {"status": "confirmed" if save_acknowledged(ack) else "unknown",
                                      "state": ack})
        if not save_acknowledged(ack):
            change.update(status="save_outcome_unverified", reason="current-page country model did not acknowledge AR")
            save(root / "reader_result.json", result)
            raise CountrySaveUnconfirmed("Country Save model ACK timeout; no repeat Save")
        change["status"] = "save_model_acknowledged"
        save(root / "reader_result.json", result)
        navigate(base + "/monetization/overview")
        identity()
        def read_after():
            return {"state": driver.execute_script(JS_COUNTRY), "earn": parse_earn(text())}
        # Earn can expose a partial channel projection with coreSettings={}
        # even after a successful Save. Its metrics/warning and the freshly
        # reopened Settings UI are independent evidence of the final state.
        after = wait_value(read_after, lambda x: COUNTRY_WARNING not in normalized(x["earn"].get("raw_text"))
                           and x["earn"]["status"] == "complete")
        if not after:
            after = {"state": None, "earn": parse_earn("")}
        after_identity = identity(timeout=5)
        after["earn"]["context_verified"] = runtime_context_matches(active_expectation, after_identity)
        after["earn"]["runtime_identity"] = after_identity
        change["after_earn_reload"] = after
        evidence("country_earn_after_reload", after)
        # Preserve exact fresh metrics even if the separate country UI check times out.
        if after["earn"].get("status") == "complete" and after["earn"]["context_verified"]:
            result["earn"] = evidence("earn", dict(after["earn"]))
            save(root / "reader_result.json", result)
        # Earn exposes a sparse channel model; reopen Settings from Dashboard.
        navigate(base)
        identity()
        click("tp-yt-paper-icon-item#settings-item")
        click("li#channel")
        reopened = wait_value(lambda: driver.execute_script(JS_COUNTRY),
                              lambda x: normalized(x.get("country_text")) == "Country of residence Argentina")
        wake("country_reopened_final_check")
        identity(timeout=5)
        reopened = driver.execute_script(JS_COUNTRY)
        if not reopened or normalized(reopened.get("country_text")) != "Country of residence Argentina":
            change.update(status="save_outcome_unverified", after=after, reopened_settings=reopened)
            save(root / "reader_result.json", result)
            raise CountrySaveUnconfirmed("Fresh Settings UI did not confirm Argentina")
        identity(timeout=5)
        evidence("country_reopened", reopened)
        click("ytcp-button#cancel-button", "Close")
        change.update(status="confirmed", country_changed=True, after=after,
                      reopened_settings=reopened, verification="model_ack_then_reload_then_settings_ui", finished_at=now())
        after["earn"].update(country_changed=True, country="AR", country_change_evidence=str(root / "country_reopened.json"))
        evidence("country_after_save", after)
        save(root / "reader_result.json", result)
        return after["earn"]

    try:
        wake("reader_entry")
        entry_context = None
        try:
            result["identity"] = identity()
        except PermissionEntryError:
            entry_context = return_from_permission_entry()
        if entry_context is None:
            dismiss_observed_welcome()
        activate_exact_identity(entry_context)
    except Exception as exc:
        result["reason"] = str(exc)
        try:
            result["entry_failure"] = evidence("entry_failure", {"status": "error", "reason": str(exc)[:1200]})
        except Exception as evidence_error:
            result["entry_failure_evidence_error"] = type(evidence_error).__name__ + ": " + str(evidence_error)[:300]
        save(root / "reader_result.json", result)
        return result
    for stage in ("lifetime", "earn", "features"):
        try:
            check()
            if stage == "lifetime":
                navigate(base + "/analytics/tab-overview/period-lifetime")
                identity()
                value = wait_value(lambda: parse_lifetime(driver.execute_script(JS_ANALYTICS), expected_uc, text()),
                                   lambda r: r["status"] == "complete" or urlsplit(driver.current_url).path
                                   == f"/channel/{expected_uc}/analytics/hub_overview")
                if urlsplit(driver.current_url).path == f"/channel/{expected_uc}/analytics/hub_overview":
                    value = read_hub_lifetime()
            elif stage == "earn":
                navigate(base + "/monetization/overview")
                identity()
                value = wait_value(lambda: parse_earn(text()), lambda r: r["status"] in {"complete", "needs_country"})
                value = set_unset_argentina(value)
            else:
                navigate(base)
                feature_identity = identity()
                click("tp-yt-paper-icon-item#settings-item")
                click("li#channel")
                ru_features = feature_identity.get("lang") == "ru-RU"
                click("tp-yt-paper-tab", "Доступность функций" if ru_features else "Feature eligibility")
                value = wait_value(lambda: parse_features(driver.execute_script(
                    "const e=document.querySelector('ytcp-channel-settings-dialog');return e?e.innerText:'';")),
                    lambda r: r["status"] == "complete")
            stage_identity = identity(timeout=5)
            value["context_verified"] = runtime_context_matches(active_expectation, stage_identity)
            value["runtime_identity"] = stage_identity
            result[stage] = evidence(stage, value)
            if stage == "features":
                click("ytcp-button#cancel-button", "Закрыть" if ru_features else "Close")
        except CountrySaveUnconfirmed as exc:
            change = result.setdefault("country_change", {})
            change.update(status="save_outcome_unverified", country_changed=False,
                          reason=str(exc), verification_pending=True)
            observed = result.get(stage)
            if (isinstance(observed, dict) and observed.get("status") == "complete"
                    and observed.get("context_verified") is True
                    and runtime_context_matches(active_expectation, observed.get("runtime_identity"))):
                observed["country_save_unconfirmed_reason"] = str(exc)
            else:
                result[stage] = {"status": "error", "reason": str(exc)}
            save(root / "reader_result.json", result)
            try:
                identity(timeout=5)
                closes = [e for e in driver.find_elements("css selector", "ytcp-button#cancel-button")
                          if e.is_displayed() and e.is_enabled() and normalized(e.text) == "Close"]
                if len(closes) > 1:
                    raise RuntimeError("ambiguous Close after unverified country Save")
                if closes:
                    check()
                    closes[0].click()  # Dismiss only; never repeat Save.
                    wake("after_unverified_country_close")
                identity(timeout=5)
            except Exception as recovery_error:
                change["read_only_recovery_error"] = type(recovery_error).__name__ + ": " + str(recovery_error)
                break  # No navigation if ownership or safe dismissal cannot be proved.
        except IdentityError as exc:
            result[stage] = {"status": "error", "reason": str(exc)}
            break  # wrong/unknown owner stops this channel, no further navigation.
        except Exception as exc:
            failure = {"status": "error", "reason": type(exc).__name__ + ": " + str(exc)[:1200]}
            try:
                result[stage] = evidence(stage, failure)
            except Exception:
                result[stage] = failure
        save(root / "reader_result.json", result)
    result["status"] = "complete" if all(isinstance(result[s], dict) and result[s].get("status") == "complete"
                                         for s in ("features", "lifetime", "earn")) else "partial"
    result["finished_at"] = now()
    save(root / "reader_result.json", result)
    return result
