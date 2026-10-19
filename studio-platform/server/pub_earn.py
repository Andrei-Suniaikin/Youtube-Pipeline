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
import os
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
    # 15.10.2026: «Studio за 28 дней» с дашборда канала (JSON, см. `_dash`).
    if "dash" not in have:
        c.execute("ALTER TABLE pub_channel_earn ADD COLUMN dash TEXT")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _dash(doc: dict, verified: bool) -> str:
    """Карточка «Channel analytics» дашборда Studio → JSON для колонки `dash`.

    Читает её наша копия сборщика (`workers/minsk/collector_studio.py`) на той же
    странице, куда Studio открывает канал. Берём, только если считыватель подтвердил
    канал и сессию, и карточка про этот же UC — иначе это чужие цифры.
    """
    d = doc.get("dashboard")
    if not verified or not isinstance(d, dict) or d.get("uc") != doc.get("uc"):
        return ""
    top = [{"title": str(t.get("title") or "")[:160], "views": _num(t.get("views"))}
           for t in (d.get("top") or [])[:5] if isinstance(t, dict) and t.get("title")]
    out = {"subs": _num(d.get("subs")), "views_28": _num(d.get("views_28")),
           "hours_28": _num(d.get("hours_28")), "top": top,
           "period": str(d.get("period") or "")[:60]}
    if all(out[k] is None for k in ("subs", "views_28", "hours_28")) and not top:
        return ""
    return json.dumps(out, ensure_ascii=False)


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
        "dash": _dash(doc, verified),
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
            "shorts_90,subs,life_views,life_hours,as_of,email,reason,loaded,profile,dash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (w["channel_id"], w["read_at"], w["uc"], w["state"], w["hours_365"], w["shorts_90"],
             w["subs"], w["life_views"], w["life_hours"], w["as_of"], w["email"], w["reason"],
             now, w.get("profile") or "", w.get("dash") or ""))
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
    dash = c.execute("SELECT read_at, dash FROM pub_channel_earn WHERE channel_id=? "
                     "AND COALESCE(dash,'')<>'' ORDER BY read_at DESC LIMIT 1",
                     (channel_id,)).fetchone()
    try:
        dash_doc = {**json.loads(dash[1]), "read_at": dash[0]} if dash else None
    except ValueError:
        dash_doc = None
    return {"last": _row(last),
            "good": _row(good[0]) if good else None,
            "prev": _row(good[1]) if len(good) > 1 else None,
            "dash": dash_doc,
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


# ─────────────────── светофор монетизации на карточке (19.10.2026) ───────────────────
#
# Владелец: «монета и светофор на карточке канала — красный нет монеты, жёлтый подались
# и ждём, зелёный зарабатывает». Три цвета мало: «не знаем» — это не «нет монеты».
# Чтение Earn падает у половины каналов (на 19.10 — 42 канала из 68 с попытками), и
# закрасить их красным значило бы соврать. Поэтому четвёртый, серый: сбор не дошёл.
#
# Откуда что берётся:
#   • зелёный — Studio сама сказала «канал в партнёрке» (`state='partner'`, таких 5) или
#     владелец поставил монетку руками (`pub_channels.coin`);
#   • жёлтый — порог YPP пройден (4000 подходящих часов и 1000 подписчиков), а партнёрки
#     ещё нет: значит заявка либо подана и ждёт, либо её пора подать. Различить это
#     машинно нечем — Studio показывает только счётчики, — поэтому так и написано;
#   • красный — числа есть, до порога не дотягивает: показываем, сколько набрано;
#   • серый — принятого чтения нет, причина словами из `STATE_TITLE`.

LIGHTS = {"green": "зарабатывает", "yellow": "ждёт решения YouTube",
          "red": "монеты нет", "gray": "не знаем"}


def light_of(good: dict | None, last: dict | None, coin: bool = False) -> dict:
    """Светофор одного канала: `{"light", "text", "why"}`. Чистая функция — тестируется
    без базы и одинаково считается в списке каналов, в карточке и в сводке."""
    good = good or None
    state = (good or {}).get("state") or (last or {}).get("state") or ""
    hours = (good or {}).get("hours_365")
    subs = (good or {}).get("subs")
    as_of = (good or {}).get("as_of") or ""
    if state == "partner":
        return {"light": "green", "text": LIGHTS["green"],
                "why": "Studio: канал уже в партнёрской программе"}
    if coin:
        return {"light": "green", "text": LIGHTS["green"],
                "why": "отмечено руками — монетный канал"}
    if hours is not None and subs is not None and hours >= YPP_HOURS and subs >= YPP_SUBS:
        return {"light": "yellow", "text": LIGHTS["yellow"],
                "why": f"порог пройден: {hours} ч из {YPP_HOURS} и {subs} подписчиков из "
                       f"{YPP_SUBS}{' на ' + as_of if as_of else ''} — заявка подана и ждёт "
                       f"ответа либо её пора подать"}
    if hours is not None:
        need = []
        if hours < YPP_HOURS:
            need.append(f"{hours} ч из {YPP_HOURS}")
        if subs is not None and subs < YPP_SUBS:
            need.append(f"{subs} подписчиков из {YPP_SUBS}")
        return {"light": "red", "text": LIGHTS["red"],
                "why": "до порога партнёрки: " + ", ".join(need) +
                       (f" (на {as_of})" if as_of else "")}
    why = STATE_TITLE.get(state, state) if state else "часы ни разу не собирались"
    return {"light": "gray", "text": LIGHTS["gray"], "why": why}


def lights(c: sqlite3.Connection, tenant) -> dict:
    """Канал → светофор монетизации. Один проход на весь список: поканальный счёт
    превратил бы открытие списка каналов в сотню запросов."""
    ensure_earn_schema(c)
    good = overview(c, tenant)
    last = {r["channel_id"]: _row(r) for r in c.execute(
        "SELECT e.* FROM pub_channel_earn e JOIN pub_channels ch ON ch.id=e.channel_id "
        "WHERE ch.tenant_id=? AND e.read_at=("
        "  SELECT MAX(read_at) FROM pub_channel_earn x WHERE x.channel_id=e.channel_id)",
        (tenant,)).fetchall()}
    out = {}
    for row in c.execute("SELECT id, coin FROM pub_channels WHERE tenant_id=?", (tenant,)):
        out[row[0]] = light_of(good.get(row[0]), last.get(row[0]), bool(row[1]))
    return out


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


# ─────────────────────── сбор по кнопке (13.10.2026) ───────────────────────
#
# Человек в студии жмёт «Обновить часы» — студия ставит задание на профиль AdsPower,
# к которому привязан канал. Минский пуллер сбора (`earn_puller.py`) забирает задание,
# запускает сборщик Володи на этот профиль и присылает `reader_result.json` — их
# принимает тот же `parse_upload`/`plan`/`apply`, что и ручная загрузка. Сборщик только
# читает Studio и берёт ту же бронь профиля, что заливка, поэтому повтор безопасен:
# зависшее задание просто возвращается в очередь.

REQ_STALE_SEC = 20 * 60          # пуллер молчит дольше — задание снова в очереди
REQ_EXPIRE_SEC = 12 * 3600       # никто не взял за полсуток — снимаем со словами
REQ_TITLE = {"queued": "поставлен в очередь", "running": "идёт сбор",
             "done": "готово", "failed": "не вышло",
             # 14.10.2026: ежедневный сбор не ждёт занятый профиль — оставляет его на
             # следующий проход. Это не сбой: число просто не обновилось сегодня.
             "skipped": "пропущен — профиль был занят"}


def ensure_request_schema(c: sqlite3.Connection) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS pub_earn_requests("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
        "profile TEXT NOT NULL, "            # серийник профиля AdsPower
        "status TEXT NOT NULL, "             # queued | running | done | failed
        "created REAL NOT NULL, created_by TEXT, "
        "claim_token TEXT, host TEXT, claimed REAL, heartbeat REAL, finished REAL, "
        "attempts INTEGER NOT NULL DEFAULT 0, note TEXT)")
    # 14.10.2026: задание ежедневного сбора отличается от кнопки. Человек, нажавший
    # «Обновить часы», ждёт ответа сейчас — его задание идёт первым и ждёт занятый
    # профиль; ночной обход по всем профилям занятый пропускает до следующих суток.
    have = {r[1] for r in c.execute("PRAGMA table_info(pub_earn_requests)")}
    if "auto" not in have:
        c.execute("ALTER TABLE pub_earn_requests ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")


def request_collect(c: sqlite3.Connection, tenant, channel_ids=None, by: str = "",
                    now: float | None = None, auto: bool = False) -> dict:
    """Поставить сбор по профилям этих каналов (None — по всем каналам с профилем)."""
    now = time.time() if now is None else now
    ensure_request_schema(c)
    sql = ("SELECT ch.id, ch.name, p.serial FROM pub_channels ch LEFT JOIN pub_profiles p "
           "ON p.id=ch.profile_id WHERE ch.tenant_id=? AND COALESCE(ch.uc,'')<>''")
    rows = c.execute(sql, (tenant,)).fetchall()
    if channel_ids is not None:
        want = {int(x) for x in channel_ids}
        rows = [r for r in rows if r[0] in want]
    no_profile = sorted(r[1] for r in rows if not r[2])
    serials = sorted({str(r[2]) for r in rows if r[2]}, key=lambda s: (len(s), s))
    active = {r[0] for r in c.execute(
        "SELECT profile FROM pub_earn_requests WHERE tenant_id=? AND status IN "
        "('queued','running')", (tenant,))}
    queued = []
    for s in serials:
        if s in active:
            continue
        c.execute("INSERT INTO pub_earn_requests(tenant_id,profile,status,created,created_by,"
                  "auto) VALUES(?,?,'queued',?,?,?)", (tenant, s, now, by[:60], 1 if auto else 0))
        queued.append(s)
    return {"queued": queued, "already": sorted(set(serials) & active,
                                                  key=lambda s: (len(s), s)),
            "no_profile": no_profile}


def _sweep_requests(c: sqlite3.Connection, now: float) -> None:
    c.execute("UPDATE pub_earn_requests SET status='queued', claim_token=NULL, "
              "note='пуллер замолчал — задание снова в очереди' "
              "WHERE status='running' AND COALESCE(heartbeat, claimed, 0) < ?",
              (now - REQ_STALE_SEC,))
    # Пуллер жив, но Мак не отвечал — тогда «пуллер не работает?» было бы неправдой.
    host = host_state(c)
    why = ("за 12 часов Мак Володи так и не ответил — без него сбор не запускается"
           if host.get("peer_ok") is False and now - float(host.get("at") or 0) < 3600
           else "за 12 часов сбор никто не взял — пуллер Минска не работает?")
    c.execute("UPDATE pub_earn_requests SET status='failed', finished=?, note=? "
              "WHERE status='queued' AND created < ?", (now, why, now - REQ_EXPIRE_SEC))


# Что пуллер в последний раз сказал о второй машине (Маке Володи). Сборщик без её ответа
# не открывает ни одного профиля (collector.py, `active_everywhere` перед списком
# профилей), и на нашей стороне это не лечится — но человек в студии должен видеть,
# почему задание стоит в очереди, а не гадать.
HOST_KEY = "earn_host"
PEER_DOWN_NOTE = ("Мак Володи не отвечает — без него сбор не запускается, "
                  "чтобы не открыть профиль на двух машинах сразу")


def _state_get(c: sqlite3.Connection, key: str):
    c.execute("CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    row = c.execute("SELECT v FROM admin_state WHERE k=?", (key,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else None
    except ValueError:
        return None


def _state_put(c: sqlite3.Connection, key: str, value, now: float) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (key, json.dumps(value, ensure_ascii=False), now))


def host_state(c: sqlite3.Connection) -> dict:
    v = _state_get(c, HOST_KEY)
    return v if isinstance(v, dict) else {}


def note_host(c: sqlite3.Connection, host: str, peer_ok, now: float) -> None:
    """Запомнить, отвечает ли Мак. Пишем смену состояния или раз в 10 минут: пуллер
    спрашивает задание каждые полминуты, и базе незачем столько одинаковых записей."""
    if peer_ok is None:
        return
    peer_ok = bool(peer_ok)
    old = host_state(c)
    same = old.get("peer_ok") is peer_ok
    if same and now - float(old.get("at") or 0) < 600:
        return
    _state_put(c, HOST_KEY, {"host": host[:40], "peer_ok": peer_ok,
                             "since": (old.get("since") or now) if same else now,
                             "at": now}, now)


def claim_request(c: sqlite3.Connection, host: str, now: float | None = None,
                  manual_only: bool = False, peer_ok=None) -> dict | None:
    """Пуллеру — следующее задание. Один и тот же профиль второй раз не выдаётся.

    Задание по кнопке идёт раньше ночного обхода: обход по всем профилям длится часы,
    и человек не должен ждать его конца ради одного канала. `manual_only` — пуллер
    знает, что Мак не отвечает: ночные задания пусть ждут Мака в очереди (их срок —
    полсуток), а кнопочное он возьмёт и сразу ответит словами, почему не вышло.
    """
    import secrets
    now = time.time() if now is None else now
    ensure_request_schema(c)
    note_host(c, host, peer_ok, now)
    _sweep_requests(c, now)
    row = c.execute("SELECT id, tenant_id, profile, auto FROM pub_earn_requests "
                    "WHERE status='queued' " + ("AND auto=0 " if manual_only else "")
                    + "AND profile NOT IN (SELECT profile FROM pub_earn_requests "
                    "WHERE status='running') ORDER BY auto, created, id LIMIT 1").fetchone()
    if row is None:
        return None
    token = "earn_" + secrets.token_urlsafe(18)
    cur = c.execute("UPDATE pub_earn_requests SET status='running', claim_token=?, host=?, "
                    "claimed=?, heartbeat=?, attempts=attempts+1, note='' "
                    "WHERE id=? AND status='queued'", (token, host[:40], now, now, row[0]))
    if cur.rowcount != 1:
        return None
    return {"id": row[0], "profile": row[2], "token": token, "auto": bool(row[3]),
            "ucs": profile_ucs(c, row[1], row[2])}


def _own_request(c: sqlite3.Connection, req_id: int, token: str):
    row = c.execute("SELECT id, tenant_id, status, claim_token FROM pub_earn_requests "
                    "WHERE id=?", (req_id,)).fetchone()
    if row is None or row[2] != "running" or not token or row[3] != token:
        return None
    return row


def beat_request(c: sqlite3.Connection, req_id: int, token: str, note: str = "",
                 now: float | None = None) -> bool:
    now = time.time() if now is None else now
    ensure_request_schema(c)
    if _own_request(c, req_id, token) is None:
        return False
    c.execute("UPDATE pub_earn_requests SET heartbeat=?, note=? WHERE id=?",
              (now, note[:300], req_id))
    return True


def finish_request(c: sqlite3.Connection, req_id: int, token: str, ok: bool, note: str = "",
                   now: float | None = None, skipped: bool = False):
    """Закрыть задание. Возвращает tenant_id или None, если задание не этого пуллера.
    `skipped` — профиль был занят и сборщик его не открывал (ночной обход)."""
    now = time.time() if now is None else now
    ensure_request_schema(c)
    row = _own_request(c, req_id, token)
    if row is None:
        return None
    status = "done" if ok else ("skipped" if skipped else "failed")
    c.execute("UPDATE pub_earn_requests SET status=?, finished=?, heartbeat=?, note=?, "
              "claim_token=NULL WHERE id=?", (status, now, now, note[:500], req_id))
    return row[1]


def channel_request(c: sqlite3.Connection, tenant, channel_id: int) -> dict:
    """Последнее задание сбора по профилю канала — для карточки."""
    ensure_request_schema(c)
    serial = _assigned(c, channel_id)
    if not serial:
        return {"profile": "", "can": False,
                "why": "канал не привязан к профилю AdsPower — собирать не из чего"}
    row = c.execute("SELECT id, status, created, claimed, finished, note FROM pub_earn_requests "
                    "WHERE tenant_id=? AND profile=? ORDER BY id DESC LIMIT 1",
                    (tenant, serial)).fetchone()
    out = {"profile": serial, "can": True, "why": ""}
    host = host_state(c)
    if host.get("peer_ok") is False:
        out["peer_down_since"] = host.get("since")
    if row is not None:
        out["last"] = {"id": row[0], "status": row[1], "title": REQ_TITLE.get(row[1], row[1]),
                       "created": row[2], "claimed": row[3], "finished": row[4],
                       "note": row[5] or ""}
        # 15.10.2026: профиль обойдён, а этого канала в выхлопе нет — у профиля
        # «готово», у канала «сбора не было», и что это значит, было не понять.
        if row[1] == "done" and row[3]:
            ensure_earn_schema(c)
            got = c.execute("SELECT 1 FROM pub_channel_earn WHERE channel_id=? AND loaded>=?",
                            (channel_id, float(row[3]))).fetchone()
            out["last"]["missed"] = got is None
    return out


def profile_ucs(c: sqlite3.Connection, tenant, serial: str) -> list:
    """UC каналов студии на профиле: сборщик откроет их напрямую, если ни одна почта
    профиля не показала канал в своём списке (так бывает с доступом менеджера)."""
    rows = c.execute("SELECT ch.uc FROM pub_channels ch JOIN pub_profiles p ON p.id=ch.profile_id "
                     "WHERE ch.tenant_id=? AND p.serial=? AND ch.status='active' "
                     "AND COALESCE(ch.uc,'')<>'' ORDER BY ch.id", (tenant, str(serial))).fetchall()
    return [r[0] for r in rows if UC_RE.match(r[0] or "")]


# ─────────────────────── ежедневный сбор (14.10.2026) ───────────────────────
#
# Владелец 14.10: «стата почему-то должна ждать, когда я тыкаю заказать стату… хотя бы
# раз в день фетчить всю стату». Раз в сутки, в назначенный час, студия сама ставит
# сбор по всем каналам с профилем — тем же `request_collect`, что и кнопка «Собрать по
# всем каналам», только с пометкой «ночной». Пуллер Минска такие задания не держит ради
# занятого профиля (пропускает до следующих суток), а пока Мак Володи не отвечает —
# оставляет их в очереди ждать его. Включено по умолчанию; выключается на экране
# «Часы монетизации» или VS_EARN_AUTO=0.
#
# Живёт на том же минутном таймере, что тревоги и сводка (`dev/alerts.py`): своего
# процесса не заводим — см. там же, почему.

AUTO_KEY = "earn_auto"             # настройка: включено ли и в котором часу
AUTO_DONE_KEY = "earn_auto_done"   # за какие сутки обход уже ставился (+ «:тенант»)
AUTO_BY = "ежедневный сбор"
AUTO_TZ = (os.environ.get("VS_EARN_TZ") or os.environ.get("VS_DIGEST_TZ")
           or "Europe/Warsaw").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _zone(tz_name: str):
    """Опечатка в поясе не должна ронять минутный таймер тревог — тогда UTC."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:                    # noqa: BLE001
        return dt.timezone.utc


def auto_settings(c: sqlite3.Connection) -> dict:
    """Что действует. Нет записи — умолчание из окружения: включено, 04:00 по Варшаве.

    Четыре утра — заливка к этому часу обычно стоит, и к утру цифры уже в карточках.
    Мак Володи в это время может спать — тогда ночные задания дождутся его в очереди.
    """
    saved = _state_get(c, AUTO_KEY)
    saved = saved if isinstance(saved, dict) else {}
    on = saved.get("on")
    if not isinstance(on, bool):
        on = os.environ.get("VS_EARN_AUTO", "1").strip() != "0"
    hour = saved.get("hour")
    if isinstance(hour, bool) or not isinstance(hour, int):
        hour = _env_int("VS_EARN_HOUR", 4)
    return {"on": on, "hour": min(23, max(0, hour)), "tz": AUTO_TZ}


def set_auto_settings(c: sqlite3.Connection, on=None, hour=None, now=None) -> dict:
    """Поправить настройку. Возвращает то, что стало действовать."""
    now = time.time() if now is None else now
    cur = auto_settings(c)
    value = {"on": cur["on"], "hour": cur["hour"]}
    if on is not None:
        value["on"] = bool(on)
    if hour is not None:
        try:
            if isinstance(hour, bool):
                raise ValueError
            hour = int(hour)
        except (TypeError, ValueError):
            raise ValueError("час сбора — целое число от 0 до 23")
        if not 0 <= hour <= 23:
            raise ValueError("час сбора — целое число от 0 до 23")
        value["hour"] = hour
    _state_put(c, AUTO_KEY, value, now)
    return auto_settings(c)


def daily_tick(c: sqlite3.Connection, tenant_id, now=None, tz_name: str = None) -> dict:
    """Поставить ночной обход, если пора. Безопасно звать хоть каждую минуту.

    Ставится не чаще раза в календарные сутки и только после назначенного часа.
    Профиль, по которому сбор уже стоит или идёт (кнопка, вчерашний обход, который
    ждёт Мака), второй раз не ставится — это делает сам `request_collect`.
    """
    now = time.time() if now is None else float(now)
    if not tenant_id:
        return {"queued": [], "why": "не задан тенант — ставить сбор не для кого"}
    conf = auto_settings(c)
    if not conf["on"]:
        return {"queued": [], "why": "ежедневный сбор выключен настройкой"}
    local = dt.datetime.fromtimestamp(now, _zone(tz_name or conf["tz"]))
    day = local.strftime("%Y-%m-%d")
    if local.hour < conf["hour"]:
        return {"queued": [], "why": f"ещё рано: сбор в {conf['hour']:02d}:00"}
    key = f"{AUTO_DONE_KEY}:{tenant_id}"
    done = _state_get(c, key)
    if isinstance(done, dict) and done.get("day") == day:
        return {"queued": [], "why": "за эти сутки обход уже ставился"}
    out = request_collect(c, tenant_id, None, by=AUTO_BY, now=now, auto=True)
    _state_put(c, key, {"day": day, "at": now, "queued": len(out["queued"]),
                        "already": len(out["already"]),
                        "no_profile": len(out["no_profile"])}, now)
    c.commit()
    return {**out, "why": ""}


def auto_status(c: sqlite3.Connection, tenant) -> dict:
    """Для экрана: настройка, последний обход и чем кончились его задания."""
    ensure_request_schema(c)
    out = {"settings": auto_settings(c), "last": None, "host": host_state(c)}
    done = _state_get(c, f"{AUTO_DONE_KEY}:{tenant}")
    if not isinstance(done, dict) or not done.get("at"):
        return out
    rows = c.execute("SELECT status, note, profile FROM pub_earn_requests WHERE tenant_id=? "
                     "AND auto=1 AND created>=? ORDER BY id", (tenant, done["at"])).fetchall()
    counts, failed = {}, {}
    for st, note, _p in rows:
        counts[st] = counts.get(st, 0) + 1
        if st == "failed":
            failed[note or "без причины"] = failed.get(note or "без причины", 0) + 1
    out["last"] = {"day": done.get("day"), "at": done["at"], "queued": done.get("queued", 0),
                   "counts": counts,
                   "skipped_profiles": sorted({str(p) for st, _n, p in rows if st == "skipped"},
                                              key=lambda s: (len(s), s)),
                   "failed_reasons": [{"note": k, "n": v} for k, v in
                                      sorted(failed.items(), key=lambda x: -x[1])][:5]}
    return out
