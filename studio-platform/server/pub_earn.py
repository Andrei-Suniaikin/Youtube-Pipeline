"""Часы для монетизации: что сборщик Минска прочитал в разделе «Монетизация» Studio.

Добавлено 13.10.2026. Число, по которому YouTube пускает канал в партнёрку, — «подходящие
часы просмотра за 365 дней» — через API не отдаётся никому: Analytics считает обычные
часы, в которые входят Shorts, скрытые и удалённые ролики. Его видно только на странице
Earn в Studio. Читает её браузерный сборщик Володи (`collector.py` + `studio_reader.py`)
сессией менеджера на Минске; сюда приходит его выхлоп — файлы `reader_result.json`.

Чему верим. Считыватель сам помечает, доказал ли он, что читал именно этот канал:
`active_identity_verified` у всего прохода и `context_verified` у страницы Earn. Число без
этих двух подтверждений в карточку не попадает — чужие часы в карточке хуже пустой
клетки. Такое чтение всё равно записывается, но со статусом «не подтверждено» и
причиной: человеку видно, что сбор был и почему числа нет.

Пишем только добавлением: строка на «канал + момент чтения». Повторная загрузка того же
файла ничего не меняет (первичный ключ), а история нужна, чтобы видеть прирост.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import sqlite3
import time
import zipfile

UC_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# Порог партнёрки YouTube для длинных роликов: 4000 подходящих часов и 1000 подписчиков.
YPP_HOURS = 4000
YPP_SUBS = 1000
MAX_FILES = 5000
MAX_FILE_BYTES = 8 * 1024 * 1024

# Состояния чтения — словами для экрана.
STATE_TITLE = {
    "ok": "прочитано",
    "partner": "уже в партнёрке — Studio счётчик не показывает",
    "needs_country": "в канале не выбрана страна — Studio не показывает счётчик",
    "unverified": "не подтверждено, что читали этот канал — число не принято",
    "partial": "страница прочиталась не полностью",
    "error": "сбой чтения",
}


def ensure_earn_schema(c: sqlite3.Connection) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS pub_channel_earn("
        "channel_id INTEGER NOT NULL, "
        "read_at REAL NOT NULL, "            # когда считыватель закончил (epoch UTC)
        "uc TEXT NOT NULL, "
        "state TEXT NOT NULL, "              # ключ STATE_TITLE
        "hours_365 INTEGER, "                # подходящие часы за 365 дней
        "shorts_90 INTEGER, "                # подходящие просмотры Shorts за 90 дней
        "subs INTEGER, "
        "life_views INTEGER, "
        "life_hours REAL, "
        "as_of TEXT, "                       # «данные на …» — как написала Studio
        "email TEXT, "                       # чьей сессией читали
        "reason TEXT, "
        "loaded REAL NOT NULL, "
        "PRIMARY KEY(channel_id, read_at))")
    # 13.10.2026: из какого профиля AdsPower читали — сверка с закреплённым профилем.
    have = {r[1] for r in c.execute("PRAGMA table_info(pub_channel_earn)")}
    if "profile" not in have:
        c.execute("ALTER TABLE pub_channel_earn ADD COLUMN profile TEXT")


def _ts(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def _val(block, key):
    v = ((block or {}).get(key) or {}).get("value") if isinstance(block, dict) else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def reading(doc: dict) -> dict | None:
    """Один `reader_result.json` → чтение. None — это не результат считывателя."""
    if not isinstance(doc, dict) or not UC_RE.match(str(doc.get("uc") or "")):
        return None
    at = _ts(doc.get("finished_at")) or _ts(doc.get("started_at"))
    if at is None:
        return None
    earn = doc.get("earn") if isinstance(doc.get("earn"), dict) else {}
    life = doc.get("lifetime") if isinstance(doc.get("lifetime"), dict) else {}
    ctx = doc.get("context") if isinstance(doc.get("context"), dict) else {}
    verified = doc.get("active_identity_verified") is True and earn.get("context_verified") is True
    hours, subs = _val(earn, "qualified_watch_hours_365d"), _val(earn, "subscribers")
    if not earn:
        state, reason = "error", str(doc.get("reason") or "страница Earn не прочитана")[:300]
    elif earn.get("status") == "error":
        state, reason = "error", str(earn.get("reason") or "")[:300]
    elif not verified:
        state, reason = "unverified", "считыватель не подтвердил канал и сессию"
    elif earn.get("capture_status") == "observed_ypp_member":
        state, reason = "partner", ""
    elif earn.get("status") == "complete" and hours is not None:
        state, reason = "ok", ""
    elif earn.get("needs_country") or earn.get("status") == "needs_country":
        state, reason = "needs_country", ""
    else:
        state, reason = "partial", str(((earn.get("qualified_watch_hours_365d") or {})
                                        .get("reason")) or "")[:300]
    keep = state in ("ok", "partner")
    life_ok = verified and life.get("status") == "complete"
    return {
        "uc": doc["uc"], "read_at": round(at, 3), "state": state, "reason": reason,
        "hours_365": int(hours) if keep and hours is not None else None,
        "shorts_90": (int(_val(earn, "qualified_shorts_views_90d"))
                      if keep and _val(earn, "qualified_shorts_views_90d") is not None else None),
        "subs": int(subs) if keep and subs is not None else None,
        "life_views": int(_val(life, "views")) if life_ok and _val(life, "views") is not None else None,
        "life_hours": float(_val(life, "watch_hours")) if life_ok and _val(life, "watch_hours") is not None else None,
        "as_of": str(earn.get("as_of") or "")[:60] if keep else "",
        # Почта и профиль — только когда считыватель подтвердил, что сессия та самая:
        # иначе это может быть чужой аккаунт, и выводы по нему хуже, чем никаких.
        "email": str(ctx.get("email") or "")[:120] if verified else "",
        "profile": str(ctx.get("profile") or "")[:20] if verified else "",
    }


def parse_upload(name: str, data: bytes) -> tuple[list, list]:
    """Файл с Минска → (чтения, замечания). Принимает zip папки `--out` или один JSON."""
    docs, notes = [], []
    if data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("архив не открывается")
        names = [n for n in z.namelist() if n.endswith("reader_result.json")]
        if len(names) > MAX_FILES:
            raise ValueError(f"в архиве больше {MAX_FILES} результатов — разбейте на части")
        for n in names:
            info = z.getinfo(n)
            if info.file_size > MAX_FILE_BYTES:
                notes.append(f"{n}: файл слишком большой, пропущен")
                continue
            try:
                docs.append((n, json.loads(z.read(n).decode("utf-8"))))
            except (ValueError, UnicodeDecodeError):
                notes.append(f"{n}: не JSON, пропущен")
        if not names:
            raise ValueError("в архиве нет ни одного reader_result.json")
    else:
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("ждём zip папки сборщика или reader_result.json")
        for i, d in enumerate(doc if isinstance(doc, list) else [doc]):
            docs.append((f"{name}#{i}" if isinstance(doc, list) else name, d))
    out = []
    for n, d in docs:
        r = reading(d)
        if r is None:
            notes.append(f"{n}: не похоже на результат считывателя, пропущен")
        else:
            out.append(r)
    return out, notes


def plan(c: sqlite3.Connection, tenant, readings: list) -> dict:
    """Что запишется: по каналам студии, незнакомые UC — отдельно. Ничего не пишет."""
    ensure_earn_schema(c)
    by_uc = {}
    for row in c.execute("SELECT id, name, uc FROM pub_channels WHERE tenant_id=? "
                         "AND COALESCE(uc,'')<>''", (tenant,)):
        by_uc.setdefault(row["uc"], row)
    writes, unknown, seen = [], {}, set()
    for r in sorted(readings, key=lambda x: x["read_at"]):
        ch = by_uc.get(r["uc"])
        if ch is None:
            unknown[r["uc"]] = unknown.get(r["uc"], 0) + 1
            continue
        key = (ch["id"], r["read_at"])
        if key in seen:
            continue
        seen.add(key)
        old = c.execute("SELECT 1 FROM pub_channel_earn WHERE channel_id=? AND read_at=?",
                        key).fetchone()
        writes.append({**r, "channel_id": ch["id"], "channel": ch["name"], "known": bool(old)})
    latest = {}
    for w in writes:
        latest[w["channel_id"]] = w          # отсортировано по времени — последнее побеждает
    counts = {}
    for w in latest.values():
        counts[w["state"]] = counts.get(w["state"], 0) + 1
    return {"writes": writes,
            "report": {"readings": len(readings), "channels": len(latest),
                       "new": sum(1 for w in writes if not w["known"]),
                       "states": counts,
                       "unknown_uc": sorted(unknown),
                       "rows": [{"channel_id": w["channel_id"], "channel": w["channel"],
                                 "state": w["state"], "state_title": STATE_TITLE[w["state"]],
                                 "hours_365": w["hours_365"], "subs": w["subs"],
                                 "as_of": w["as_of"], "reason": w["reason"]}
                                for w in sorted(latest.values(), key=lambda x: x["channel"])]}}


def apply(c: sqlite3.Connection, planned: dict, now: float | None = None) -> int:
    now = time.time() if now is None else now
    ensure_earn_schema(c)
    n = 0
    for w in planned["writes"]:
        if w["known"]:
            continue
        cur = c.execute(
            "INSERT OR IGNORE INTO pub_channel_earn(channel_id,read_at,uc,state,hours_365,"
            "shorts_90,subs,life_views,life_hours,as_of,email,reason,loaded,profile) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (w["channel_id"], w["read_at"], w["uc"], w["state"], w["hours_365"], w["shorts_90"],
             w["subs"], w["life_views"], w["life_hours"], w["as_of"], w["email"], w["reason"],
             now, w.get("profile") or ""))
        n += cur.rowcount
    return n


def _row(r) -> dict:
    return {"read_at": r["read_at"], "state": r["state"], "state_title": STATE_TITLE.get(r["state"], r["state"]),
            "hours_365": r["hours_365"], "shorts_90": r["shorts_90"], "subs": r["subs"],
            "life_views": r["life_views"], "life_hours": r["life_hours"],
            "as_of": r["as_of"] or "", "reason": r["reason"] or ""}


def channel_state(c: sqlite3.Connection, channel_id: int) -> dict:
    """Последнее чтение канала и последнее принятое число — они могут различаться:
    свежий сбой не должен стирать вчерашние часы, но и прятать сбой нельзя."""
    ensure_earn_schema(c)
    last = c.execute("SELECT * FROM pub_channel_earn WHERE channel_id=? "
                     "ORDER BY read_at DESC LIMIT 1", (channel_id,)).fetchone()
    if last is None:
        return {}
    good = c.execute("SELECT * FROM pub_channel_earn WHERE channel_id=? AND state='ok' "
                     "ORDER BY read_at DESC LIMIT 2", (channel_id,)).fetchall()
    return {"last": _row(last),
            "good": _row(good[0]) if good else None,
            "prev": _row(good[1]) if len(good) > 1 else None,
            "goal_hours": YPP_HOURS, "goal_subs": YPP_SUBS}


def overview(c: sqlite3.Connection, tenant) -> dict:
    """Канал → последнее принятое чтение (для списка каналов)."""
    ensure_earn_schema(c)
    rows = c.execute(
        "SELECT e.* FROM pub_channel_earn e JOIN pub_channels ch ON ch.id=e.channel_id "
        "WHERE ch.tenant_id=? AND e.state IN ('ok','partner') AND e.read_at=("
        "  SELECT MAX(read_at) FROM pub_channel_earn x WHERE x.channel_id=e.channel_id "
        "  AND x.state IN ('ok','partner'))", (tenant,)).fetchall()
    return {r["channel_id"]: _row(r) for r in rows}


# ─────────────────────────── почты каналов (13.10.2026) ───────────────────────────
#
# Под какой почтой Google открывается канал. Отдельно почту никто не хранит: ни AdsPower,
# ни профиль браузера. Её пишет сам сборщик — «Signed in as …» живой сессии, — и только
# подтверждённое чтение (см. `reading`) несёт её сюда. Видят все, кто вошёл в студию.

def _email_rows(c: sqlite3.Connection, tenant, channel_id=None):
    ensure_earn_schema(c)
    sql = ("SELECT e.channel_id, e.email, e.profile, e.read_at FROM pub_channel_earn e "
           "JOIN pub_channels ch ON ch.id=e.channel_id WHERE ch.tenant_id=? "
           "AND COALESCE(e.email,'')<>''")
    args = [tenant]
    if channel_id is not None:
        sql += " AND e.channel_id=?"
        args.append(channel_id)
    return c.execute(sql + " ORDER BY e.read_at", args).fetchall()


def _assigned(c: sqlite3.Connection, channel_id: int) -> str:
    row = c.execute("SELECT p.serial FROM pub_channels ch LEFT JOIN pub_profiles p "
                    "ON p.id=ch.profile_id WHERE ch.id=?", (channel_id,)).fetchone()
    return str(row[0] or "") if row else ""


def channel_email(c: sqlite3.Connection, tenant, channel_id: int) -> dict:
    """Последняя подтверждённая почта канала; смена почты и чужой профиль — словами."""
    rows = _email_rows(c, tenant, channel_id)
    if not rows:
        return {}
    last = rows[-1]
    seen = []
    for r in rows:
        if r[1] not in seen:
            seen.append(r[1])
    assigned = _assigned(c, channel_id)
    return {"email": last[1], "read_at": last[3], "profile": last[2] or "",
            "assigned_profile": assigned,
            "other_profile": bool(last[2] and assigned and last[2] != assigned),
            "earlier": [e for e in seen if e != last[1]]}


def email_groups(c: sqlite3.Connection, tenant) -> dict:
    """Почта → каналы. Один аккаунт Google падает целиком — видно, какие каналы вместе."""
    last = {}
    for r in _email_rows(c, tenant):
        last[r[0]] = r                      # отсортировано по времени — последнее побеждает
    names = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT ch.id, ch.name, p.serial FROM pub_channels ch LEFT JOIN pub_profiles p "
        "ON p.id=ch.profile_id WHERE ch.tenant_id=?", (tenant,))}
    groups = {}
    for cid, r in last.items():
        if cid not in names:
            continue
        name, serial = names[cid]
        groups.setdefault(r[1], []).append({
            "channel_id": cid, "channel": name, "profile": r[2] or "",
            "assigned_profile": str(serial or ""), "read_at": r[3],
            "other_profile": bool(r[2] and serial and r[2] != str(serial))})
    out = [{"email": e, "channels": sorted(chs, key=lambda x: x["channel"])}
           for e, chs in groups.items()]
    out.sort(key=lambda g: (-len(g["channels"]), g["email"]))
    missing = sorted((names[cid][0] for cid in names if cid not in last))
    return {"groups": out, "without": missing,
            "channels": len(last), "emails": len(out)}
