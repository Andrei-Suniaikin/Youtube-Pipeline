# -*- coding: utf-8 -*-
"""Подробная статистика канала: время просмотра, досмотренность, подписчики, доход.

Чем отличается от `pub_stats`. Там — публичные счётчики, один ключ на всю студию, и
только «сколько сейчас». Здесь — то, что YouTube отдаёт лишь владельцу канала по его
личному согласию (`channel_oauth`), и отдаёт **посуточно и задним числом**. Разница
принципиальная: подписчиков нельзя восстановить за прошлую неделю, а время просмотра —
можно, вместе со всей историей канала. Поэтому при подключении канала мы забираем
прошлое сразу, а не начинаем наблюдать с нуля.

Две вещи, из-за которых этот модуль устроен именно так.

**Канал называется по имени, а не «мой».** Запрос можно адресовать «каналу этого
токена» — короче и не требует знать номер. Но токены вставляет человек, руками, по
одному на канал, сто с лишним раз подряд; вставить не в ту строку он однажды обязан. На
«мой» Google молча ответит статистикой ЧУЖОГО канала, и она ляжет в карточку этого.
Поэтому всегда `channel==UC…`: чужой токен получает честный отказ, а не чужие числа.

**Свежие дни перезапрашиваются.** YouTube уточняет числа несколько суток после того, как
день закончился: отсеивает накрутки, досчитывает досмотры. Записав день один раз и
больше к нему не возвращаясь, мы навсегда сохранили бы черновик. Поэтому каждый проход
перезапрашивает последнюю неделю поверх записанного.

Показов и CTR превью здесь нет и быть не может: в этом API таких метрик не существует
вовсе (проверено по списку метрик — есть показы рекламы, карточек и аннотаций, но не
превью). Они живут в другом продукте, суточных выгрузках YouTube Reporting API, и
приезжают отдельным путём — колонки под них в таблице заведены сразу.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import channel_oauth

API = "https://youtubeanalytics.googleapis.com/v2/reports"
TIMEOUT = 30

# Что спрашиваем. Список — одно место на весь модуль: порядок колонок в ответе Google
# совпадает с порядком здесь, и разъехавшись они молча перепутают числа местами.
METRICS = ("views", "estimatedMinutesWatched", "averageViewDuration",
           "averageViewPercentage", "subscribersGained", "subscribersLost",
           "likes", "comments", "shares")
# Куда ляжет каждая метрика. Имена колонок наши, короткие и по-человечески.
COLUMNS = ("views", "minutes", "avg_sec", "avg_pct", "subs_gained", "subs_lost",
           "likes", "comments", "shares")

# Сколько прошлого забираем при первом подключении канала. Год — чтобы сразу было видно
# сезонность и эффект правок, и при этом один запрос: Google отдаёт посуточно.
BACKFILL_DAYS = 365
# Сколько последних дней перезапрашиваем каждый проход (YouTube их ещё уточняет).
REVISE_DAYS = 7
# Данные за вчера появляются не мгновенно. Спрашиваем по позавчера включительно, чтобы
# не записывать заведомо неполный день и не считать его потом «просевшим».
LAG_DAYS = 2


def ensure_analytics_schema(c: sqlite3.Connection) -> None:
    """Посуточная статистика канала. Идемпотентно.

    День — тот, который называет сам YouTube (`dimensions=day`), в часовом поясе канала
    по версии Google. Свой пояс не подставляем: сложить наши сутки с их сутками значит
    получить третьи, не совпадающие ни с чем, что владелец видит в Studio.
    """
    c.execute(
        "CREATE TABLE IF NOT EXISTS pub_channel_analytics("
        "channel_id INTEGER NOT NULL, "
        "day TEXT NOT NULL, "
        "views INTEGER, minutes REAL, avg_sec REAL, avg_pct REAL, "
        "subs_gained INTEGER, subs_lost INTEGER, "
        "likes INTEGER, comments INTEGER, shares INTEGER, "
        # Показы и CTR превью приезжают из другого продукта Google (суточные выгрузки
        # Reporting API) и ложатся в эти же строки. Пусто означает «ещё не приехало»,
        # а не «ноль показов».
        "impressions INTEGER, ctr REAL, "
        "fetched REAL NOT NULL, "
        "PRIMARY KEY(channel_id, day))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_channel_analytics_day "
              "ON pub_channel_analytics(day)")


def forget_channel(c: sqlite3.Connection, channel_id: int) -> None:
    """Удалить историю канала — вместе с самим каналом.

    Та же причина, что у публичных снимков: внешних ключей нет, номера каналов
    переиспользуются, и осиротевший ряд однажды прилипнет к чужому каналу.
    """
    ensure_analytics_schema(c)
    c.execute("DELETE FROM pub_channel_analytics WHERE channel_id=?", (int(channel_id),))


# ───────────────────────────── запрос ─────────────────────────────

class AnalyticsError(Exception):
    """Отказ, который показываем человеку целиком. Токена внутри нет."""


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _why(exc) -> str:
    """Причина отказа словами Google — без нашей догадки поверх неё."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
        err = body.get("error") or {}
        return str(err.get("message") or "")[:300]
    except Exception:                                              # noqa: BLE001
        return str(exc)[:300]


def ask(uc: str, token: str, start: str, end: str, get=None) -> list:
    """Посуточные строки за отрезок: [{day, views, minutes, …}]."""
    url = API + "?" + urllib.parse.urlencode({
        "ids": f"channel=={uc}", "startDate": start, "endDate": end,
        "metrics": ",".join(METRICS), "dimensions": "day", "sort": "day"})
    try:
        data = (get or _get)(url, token)
    except urllib.error.HTTPError as exc:
        why = _why(exc)
        if exc.code == 403:
            # Самый частый случай на сотне каналов: токен вставили не в ту строку.
            # Называем это прямо, иначе человек будет искать поломку в другом месте.
            raise AnalyticsError(
                f"Google не пускает этот токен к каналу {uc}: {why}. Чаще всего это "
                f"значит, что токен от другого канала — проверьте, что он взят под тем "
                f"же аккаунтом, которому принадлежит этот канал")
        raise AnalyticsError(f"YouTube не ответил ({exc.code}): {why}")
    except Exception as exc:                                       # noqa: BLE001
        raise AnalyticsError(f"связь с YouTube не удалась: {exc}")

    out = []
    for row in (data.get("rows") or []):
        if not row:
            continue
        item = {"day": str(row[0])}
        for pos, name in enumerate(COLUMNS, 1):
            item[name] = row[pos] if pos < len(row) else None
        out.append(item)
    return out


# ───────────────────────────── сбор ─────────────────────────────

def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def _shift(day: str, days: int) -> str:
    return (dt.date.fromisoformat(day) - dt.timedelta(days=days)).isoformat()


def _last_day(c: sqlite3.Connection, channel_id: int) -> str:
    row = c.execute("SELECT MAX(day) FROM pub_channel_analytics WHERE channel_id=?",
                    (int(channel_id),)).fetchone()
    return (row[0] or "") if row else ""


def _save(c: sqlite3.Connection, channel_id: int, rows: list, now: float) -> int:
    """Записать дни. Существующие перезаписываются: Google их уточняет.

    Показы и CTR при этом НЕ трогаются: они приезжают другим путём, и перезапись
    обычного дня не должна стирать уже привезённое.
    """
    sets = ", ".join(f"{name}=excluded.{name}" for name in COLUMNS)
    marks = ", ".join("?" * (len(COLUMNS) + 3))
    saved = 0
    for row in rows:
        values = [int(channel_id), row.get("day") or ""]
        values += [row.get(name) for name in COLUMNS]
        values.append(now)
        if not values[1]:
            continue
        c.execute(
            f"INSERT INTO pub_channel_analytics"
            f"(channel_id, day, {', '.join(COLUMNS)}, fetched) VALUES({marks}) "
            f"ON CONFLICT(channel_id, day) DO UPDATE SET {sets}, fetched=excluded.fetched",
            values)
        saved += 1
    return saved


def collect_channel(c: sqlite3.Connection, channel_id: int, uc: str, now=None,
                    get=None, token=None) -> dict:
    """Один канал: дозабрать новые дни и уточнить последнюю неделю."""
    now = time.time() if now is None else now
    ensure_analytics_schema(c)
    out = {"channel_id": int(channel_id), "days": 0, "from": "", "to": "", "why": ""}
    if not uc:
        out["why"] = "у канала не заполнен ID YouTube — спрашивать не о чем"
        return out
    try:
        token = channel_oauth.access_token(channel_id) if token is None else token
    except channel_oauth.OAuthError as exc:
        out["why"] = str(exc)
        return out

    end = _shift(_day(now), LAG_DAYS)
    last = _last_day(c, channel_id)
    # Первый заход — тянем год прошлого. Дальше — от последнего записанного дня, отступив
    # на неделю назад: её YouTube ещё уточняет.
    start = _shift(last, REVISE_DAYS) if last else _shift(end, BACKFILL_DAYS)
    if start > end:
        out["why"] = "новых суток ещё нет"
        return out
    out["from"], out["to"] = start, end
    try:
        rows = ask(uc, token, start, end, get=get)
    except AnalyticsError as exc:
        channel_oauth.note(channel_id, False, str(exc))
        out["why"] = str(exc)
        return out
    out["days"] = _save(c, channel_id, rows, now)
    # Фиксируем здесь, а не только в общем проходе: этот же вызов делает страница канала
    # сразу после вставки токена, со своей связью, — без записи человек увидел бы
    # «подключено», а назавтра пустую вкладку.
    c.commit()
    channel_oauth.note(channel_id, True)
    return out


def collect(c: sqlite3.Connection, tenant=None, now=None, get=None, log=None) -> dict:
    """Проход по всем подключённым каналам тенанта."""
    now = time.time() if now is None else now
    ensure_analytics_schema(c)
    sql = "SELECT id, name, uc FROM pub_channels WHERE uc IS NOT NULL AND uc<>''"
    args: list = []
    if tenant:
        sql += " AND tenant_id=?"
        args.append(tenant)
    out = {"channels": 0, "days": 0, "skipped": 0, "problems": []}
    for row in c.execute(sql + " ORDER BY id", args).fetchall():
        if not channel_oauth.state(row["id"]).get("connected"):
            out["skipped"] += 1
            continue
        got = collect_channel(c, row["id"], row["uc"], now=now, get=get)
        out["channels"] += 1
        out["days"] += got["days"]
        if got["why"]:
            out["problems"].append({"id": row["id"], "name": row["name"],
                                    "why": got["why"]})
            if log:
                log(f"[analytics] {row['name']}: {got['why']}")
    c.commit()
    return out


def verify(channel_id: int, uc: str, now=None, get=None, token=None) -> dict:
    """Проверить, что вставленный токен действительно от ЭТОГО канала.

    Делается сразу при вставке, а не при первом ночном сборе: ошибку «токен не от того
    канала» человек должен увидеть в ту же секунду, когда её сделал, — через месяц он не
    вспомнит, какую строку куда вставлял.
    """
    now = time.time() if now is None else now
    day = _shift(_day(now), LAG_DAYS + 5)
    try:
        key = channel_oauth.access_token(channel_id) if token is None else token
        rows = ask(uc, key, day, day, get=get)
    except (AnalyticsError, channel_oauth.OAuthError) as exc:
        channel_oauth.note(channel_id, False, str(exc))
        return {"ok": False, "why": str(exc)}
    channel_oauth.note(channel_id, True)
    return {"ok": True, "why": "", "rows": len(rows)}


# ───────────────────────────── чтение ─────────────────────────────

FIELDS = ("views", "minutes", "avg_sec", "avg_pct", "subs_gained", "subs_lost",
          "likes", "comments", "shares", "impressions", "ctr")


def series(c: sqlite3.Connection, channel_id: int, days: int = 90) -> list:
    ensure_analytics_schema(c)
    since = _shift(_day(time.time()), max(1, int(days)))
    rows = c.execute(
        f"SELECT day, {', '.join(FIELDS)} FROM pub_channel_analytics "
        f"WHERE channel_id=? AND day>=? ORDER BY day",
        (int(channel_id), since)).fetchall()
    return [dict(r) for r in rows]


def totals(c: sqlite3.Connection, channel_id: int, days: int = 28, now=None) -> dict:
    """Итоги за отрезок и за такой же предыдущий — чтобы было с чем сравнить.

    Голое «за 28 дней столько-то» ни о чём не говорит: хорошо это или плохо, видно
    только рядом с прошлым таким же отрезком.
    """
    now = time.time() if now is None else now
    ensure_analytics_schema(c)
    days = max(1, int(days))
    end = _shift(_day(now), LAG_DAYS)
    start = _shift(end, days - 1)
    prev_end = _shift(start, 1)
    prev_start = _shift(prev_end, days - 1)

    def box(a: str, b: str) -> dict:
        row = c.execute(
            "SELECT COUNT(*) n, SUM(views) views, SUM(minutes) minutes, "
            "SUM(subs_gained) gained, SUM(subs_lost) lost, SUM(likes) likes, "
            "SUM(comments) comments, SUM(impressions) impressions, "
            # Средние по дням взвешиваем просмотрами: день с тремя просмотрами не должен
            # весить столько же, сколько день с тремя тысячами.
            "SUM(avg_sec * COALESCE(views,0)) sec_w, SUM(avg_pct * COALESCE(views,0)) pct_w "
            "FROM pub_channel_analytics WHERE channel_id=? AND day BETWEEN ? AND ?",
            (int(channel_id), a, b)).fetchone()
        views = float(row["views"] or 0)
        return {"days": int(row["n"] or 0), "views": int(views),
                "minutes": float(row["minutes"] or 0),
                "subs_gained": int(row["gained"] or 0),
                "subs_lost": int(row["lost"] or 0),
                "subs_net": int(row["gained"] or 0) - int(row["lost"] or 0),
                "likes": int(row["likes"] or 0),
                "comments": int(row["comments"] or 0),
                "impressions": int(row["impressions"] or 0) or None,
                "avg_sec": (float(row["sec_w"] or 0) / views) if views else None,
                "avg_pct": (float(row["pct_w"] or 0) / views) if views else None,
                "from": a, "to": b}

    return {"now": box(start, end), "was": box(prev_start, prev_end), "span": days}


def channel_state(c: sqlite3.Connection, channel_id: int, now=None) -> dict:
    """Всё про подробную статистику канала одним куском — для экрана."""
    now = time.time() if now is None else now
    ensure_analytics_schema(c)
    access = channel_oauth.state(channel_id)
    out = {"access": access, "days": 0, "first": "", "last": "",
           "totals": {}, "series": []}
    if not access.get("connected"):
        return out
    row = c.execute("SELECT COUNT(*) n, MIN(day) a, MAX(day) b "
                    "FROM pub_channel_analytics WHERE channel_id=?",
                    (int(channel_id),)).fetchone()
    out["days"] = int(row["n"] or 0)
    out["first"], out["last"] = row["a"] or "", row["b"] or ""
    if out["days"]:
        out["totals"] = totals(c, channel_id, now=now)
        out["series"] = series(c, channel_id)
    return out
