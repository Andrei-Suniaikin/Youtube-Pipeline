#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Публичная статистика каналов: подписчики, просмотры, число роликов.

Зачем отдельный модуль и почему именно снимками.

YouTube отдаёт только «сколько сейчас»: подписчиков столько-то, просмотров столько-то.
ПРИРОСТА в ответе нет вовсе, и задним числом он не берётся ниоткуда — ни из архива, ни
из самого YouTube. «+120 подписчиков за неделю» существует ровно постольку, поскольку
неделю назад мы записали своё число. Поэтому здесь не «запрос статистики», а ежедневный
снимок в свою таблицу: каждый день без снимка — дыра в графике, которую уже не закрыть.

Чего тут нет и не будет. Время просмотра, CTR обложки, удержание и доход публичными не
бывают: их отдаёт только YouTube Analytics API и только владельцу канала, по отдельному
согласию гугл-аккаунта. Это другой ключ, другое согласие и другая работа; молча
подсовывать вместо них публичные просмотры — врать экраном.

Цена вопроса. `channels.list` стоит ОДНУ единицу за пачку до пятидесяти каналов, а не за
канал: сто двадцать шесть каналов — три единицы в сутки при бюджете в десять тысяч.
Кошелёк общий с проверкой публикаций (`pub_verify`) и ночным обходом, поэтому расход
идёт через тот же учёт `pub_calls.quota_allow`/`quota_spend` под своим именем «stats» —
чтобы на вопрос «кто выел ключ» отвечала таблица, а не догадка.

Ключ — тот же, что у проверки выхода (`pub_verify.api_key`): окружение старше базы.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
import urllib.parse
import urllib.request

import pub_calls
import pub_verify

API = "https://www.googleapis.com/youtube/v3/channels"
TIMEOUT = 15

# Сколько каналов влезает в один запрос. Пятьдесят — предел самого API, и он же делает
# расход смешным: пачка стоит столько же, сколько один канал.
BATCH = 50

# Кто тратит — под этим именем расход виден в `pub_calls.quota_usage`.
CONSUMER = "stats"
COST = pub_calls.QUOTA_COSTS["channels.list"]

# Не ходить в сеть чаще, чем раз в столько, по кнопке «обновить». Снимок суточный, и
# десять нажатий подряд не сделают числа свежее — только выедят общий кошелёк.
FRESH_SEC = 600


def ensure_stats_schema(c: sqlite3.Connection) -> None:
    """Снимки статистики: строка на «канал + сутки». Идемпотентно.

    День — по UTC, а не по часовому поясу канала. Пояс у каналов разный, а ряд должен
    складываться в один график: иначе соседние точки одного портфеля оказываются из
    разных суток и прирост считается по разным отрезкам времени.
    """
    c.execute(
        "CREATE TABLE IF NOT EXISTS pub_channel_stats("
        "channel_id INTEGER NOT NULL, "
        "day TEXT NOT NULL, "               # YYYY-MM-DD по UTC
        "subs INTEGER, "                    # NULL — владелец канала скрыл подписчиков
        "views INTEGER, "
        "videos INTEGER, "
        "hidden INTEGER NOT NULL DEFAULT 0, "
        "taken REAL NOT NULL, "
        "PRIMARY KEY(channel_id, day))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_channel_stats_day "
              "ON pub_channel_stats(day)")


def day_of(now: float | None = None) -> str:
    now = time.time() if now is None else now
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")


def _shift(day: str, days: int) -> str:
    return (dt.date.fromisoformat(day) - dt.timedelta(days=days)).isoformat()


def forget_channel(c: sqlite3.Connection, channel_id: int) -> None:
    """Убрать снимки удалённого канала.

    Внешних ключей в базе нет, а id каналов переиспользуются (см. CLAUDE.md, «Ловушки»).
    Оставленный ряд рано или поздно прилипнет к новому каналу с тем же номером, и тот
    покажет чужой прирост как свой — ошибка, которую на экране не распознать.
    """
    ensure_stats_schema(c)
    c.execute("DELETE FROM pub_channel_stats WHERE channel_id=?", (int(channel_id),))


# ───────────────────────────── сбор ─────────────────────────────

def _fetch(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _targets(c: sqlite3.Connection, tenant=None) -> list:
    """Каналы, про которые есть что спрашивать, — то есть все с известным UC.

    Архивные и приостановленные снимаются наравне с рабочими, и это не недосмотр:
    пачка стоит одну единицу независимо от того, сколько в ней каналов, а пропуск
    оставил бы в ряду дыру ровно на то время, пока канал лежал в архиве. Вернуть канал
    в работу — дело одного нажатия, а восстановить неснятые дни нельзя ничем.
    """
    sql = ("SELECT id, name, uc FROM pub_channels "
           "WHERE uc IS NOT NULL AND uc<>''")
    args: list = []
    if tenant:
        sql += " AND tenant_id=?"
        args.append(tenant)
    return list(c.execute(sql + " ORDER BY id", args).fetchall())


def _without_uc(c: sqlite3.Connection, tenant=None) -> int:
    sql = "SELECT COUNT(*) FROM pub_channels WHERE uc IS NULL OR uc=''"
    args: list = []
    if tenant:
        sql += " AND tenant_id=?"
        args.append(tenant)
    return int(c.execute(sql, args).fetchone()[0] or 0)


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _save(c: sqlite3.Connection, channel_id: int, stat: dict, now: float) -> dict:
    """Записать снимок за сегодняшние сутки. Второй заход за день переписывает первый.

    Переписывает, а не добавляет: точность ряда — сутки, и два снимка в один день
    означали бы два значения у одной точки графика.
    """
    hidden = bool(stat.get("hiddenSubscriberCount"))
    subs = None if hidden else _int(stat.get("subscriberCount"))
    views = _int(stat.get("viewCount"))
    videos = _int(stat.get("videoCount"))
    c.execute(
        "INSERT INTO pub_channel_stats(channel_id,day,subs,views,videos,hidden,taken) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(channel_id,day) DO UPDATE SET "
        "subs=excluded.subs, views=excluded.views, videos=excluded.videos, "
        "hidden=excluded.hidden, taken=excluded.taken",
        (int(channel_id), day_of(now), subs, views, videos, int(hidden), now))
    return {"subs": subs, "views": views, "videos": videos, "hidden": hidden}


def collect(c: sqlite3.Connection, tenant=None, key=None, fetch=None,
            now=None, log=None) -> dict:
    """Один проход: снять статистику по всем каналам тенанта.

    Отвечает отчётом, а не числом: сколько каналов спросили, сколько YouTube не нашёл,
    сколько осталось без UC, во что обошлось и почему остановились. Пустой ответ без
    причины — то, из-за чего потом невозможно понять, сломалось или просто нечего было
    делать.
    """
    now = time.time() if now is None else now
    fetch = fetch or _fetch
    ensure_stats_schema(c)
    out = {"asked": 0, "saved": 0, "missing": [], "no_uc": _without_uc(c, tenant),
           "units": 0, "calls": 0, "why": "", "quota": {}}

    key = pub_verify.api_key(c) if key is None else key
    rows = _targets(c, tenant)
    if not rows:
        out["why"] = ("каналов с известным UC нет: статистику спрашивают по "
                      "идентификатору канала, и без него YouTube не отвечает")
        out["quota"] = pub_calls.quota_state(c, now=now)
        return out
    if not key:
        # Не ошибка сбора, а отсутствие настройки: ключ бесплатный, заводится в Google
        # Cloud. Говорим это словами, а не пустой таблицей с нулями.
        out["why"] = ("нет ключа YouTube Data API: положить в admin_state.youtube_api_key "
                      "или в VS_YT_API_KEY")
        out["quota"] = pub_calls.quota_state(c, now=now)
        return out

    by_uc = {}
    for row in rows:
        by_uc.setdefault((row["uc"] or "").strip(), []).append(row)

    ids = [x for x in by_uc if x]
    for start in range(0, len(ids), BATCH):
        batch = ids[start:start + BATCH]
        allow = pub_calls.quota_allow(c, CONSUMER, COST, now=now)
        if not allow.get("ok"):
            out["why"] = allow.get("why") or "квота исчерпана"
            break
        url = API + "?" + urllib.parse.urlencode(
            {"part": "statistics", "id": ",".join(batch), "maxResults": BATCH,
             "key": key})
        try:
            data = fetch(url)
        except Exception as exc:                                   # noqa: BLE001
            raw = ""
            code = getattr(exc, "code", 0)
            try:
                raw = exc.read().decode("utf-8", "replace")        # type: ignore[attr-defined]
            except Exception:                                      # noqa: BLE001
                raw = str(exc)
            kind = pub_verify.quota_kind(code, raw)
            if kind:
                wait = pub_calls.quota_block(c, raw[:300], kind=kind, now=now)
                out["why"] = pub_calls.quota_why(wait)
            else:
                # Не про квоту: сеть, выключенный API, запрещённый ключ. Останавливаемся
                # и называем причину — молча пропущенная пачка выглядела бы как «у этих
                # каналов нет статистики».
                out["why"] = f"YouTube не ответил: {str(raw)[:200]}"
            break
        # Единицы Google списал за состоявшийся запрос — записываем расход по факту.
        pub_calls.quota_spend(c, CONSUMER, COST, now=now)
        out["units"] += COST
        out["calls"] += 1
        out["asked"] += len(batch)

        seen = set()
        for item in (data.get("items") or []):
            uc = (item.get("id") or "").strip()
            seen.add(uc)
            for row in by_uc.get(uc, []):
                _save(c, row["id"], item.get("statistics") or {}, now)
                out["saved"] += 1
        for uc in batch:
            if uc not in seen:
                # YouTube не знает такого канала: опечатка в UC, канал удалён или
                # закрыт. Это свойство настройки, а не сбоя, и владельцу его надо
                # показать поимённо.
                for row in by_uc.get(uc, []):
                    out["missing"].append({"id": row["id"], "name": row["name"],
                                           "uc": uc})
    c.commit()
    out["quota"] = pub_calls.quota_state(c, now=now)
    if log and out["why"]:
        log(f"[stats] {out['why']}")
    return out


def collect_if_stale(c: sqlite3.Connection, tenant=None, now=None, **kw) -> dict:
    """То же, но не чаще раза в `FRESH_SEC`: кнопка «обновить» не должна жечь кошелёк."""
    now = time.time() if now is None else now
    ensure_stats_schema(c)
    sql = ("SELECT MAX(s.taken) FROM pub_channel_stats s "
           "JOIN pub_channels ch ON ch.id = s.channel_id")
    args: list = []
    if tenant:
        sql += " WHERE ch.tenant_id=?"
        args.append(tenant)
    row = c.execute(sql, args).fetchone()
    last = float(row[0] or 0) if row else 0.0
    if last and now - last < FRESH_SEC:
        return {"asked": 0, "saved": 0, "missing": [], "no_uc": _without_uc(c, tenant),
                "units": 0, "calls": 0, "fresh": True,
                "why": (f"снимок сделан {int((now - last) // 60)} мин назад — "
                        f"новый ничего не изменит, числа обновляются раз в сутки"),
                "quota": pub_calls.quota_state(c, now=now)}
    return collect(c, tenant=tenant, now=now, **kw)


# ───────────────────────────── чтение ─────────────────────────────

def _rows_at(c: sqlite3.Connection, ids: list, day: str) -> dict:
    """Последний снимок каждого канала НЕ ПОЗЖЕ указанного дня."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = c.execute(
        f"SELECT channel_id, day, subs, views, videos, hidden, taken "
        f"FROM pub_channel_stats WHERE channel_id IN ({marks}) AND day<=? "
        f"ORDER BY channel_id, day", list(ids) + [day]).fetchall()
    out = {}
    for r in rows:
        out[r["channel_id"]] = dict(r)      # последний по возрастанию дня и побеждает
    return out


def _diff(now_row: dict, was_row: dict, field: str):
    """Прирост или None. None — «не знаем», и это не ноль.

    Ноль означал бы «не изменилось», а у канала, за которым мы ещё не наблюдали неделю,
    прироста нет не потому, что его не было.
    """
    if not now_row or not was_row:
        return None
    a, b = now_row.get(field), was_row.get(field)
    if a is None or b is None:
        return None
    return int(a) - int(b)


def overview(c: sqlite3.Connection, tenant=None, now=None, spans=(7, 28)) -> dict:
    """Статистика по всем каналам тенанта: последние числа и прирост за отрезки."""
    now = time.time() if now is None else now
    ensure_stats_schema(c)
    today = day_of(now)
    rows = _targets(c, tenant)
    ids = [r["id"] for r in rows]
    last = _rows_at(c, ids, today)
    past = {n: _rows_at(c, ids, _shift(today, n)) for n in spans}

    items = []
    for row in rows:
        cur = last.get(row["id"]) or {}
        item = {"id": row["id"], "name": row["name"], "uc": row["uc"],
                "subs": cur.get("subs"), "views": cur.get("views"),
                "videos": cur.get("videos"), "hidden": bool(cur.get("hidden")),
                "day": cur.get("day") or "", "taken": cur.get("taken") or 0.0,
                "delta": {}}
        for n in spans:
            was = past[n].get(row["id"]) or {}
            # Снимок «за 7 дней» может оказаться сегодняшним же, если ряд короче недели:
            # сравнивать точку с самой собой бессмысленно, честнее сказать «не знаем».
            if was.get("day") == item["day"]:
                was = {}
            item["delta"][str(n)] = {
                "subs": _diff(cur, was, "subs"), "views": _diff(cur, was, "views"),
                "videos": _diff(cur, was, "videos"), "since": was.get("day") or ""}
        items.append(item)

    have = [x for x in items if x["day"]]
    return {"channels": items, "no_uc": _without_uc(c, tenant),
            "have": len(have), "total": len(items),
            "key": bool(pub_verify.api_key(c)),
            "subs_total": sum(x["subs"] or 0 for x in have),
            "views_total": sum(x["views"] or 0 for x in have),
            "quota": pub_calls.quota_state(c, now=now)}


def series(c: sqlite3.Connection, channel_id: int, days: int = 90) -> list:
    """Ряд снимков канала от старых к новым — для графика и для выгрузки."""
    ensure_stats_schema(c)
    since = _shift(day_of(), max(1, int(days)))
    rows = c.execute(
        "SELECT day, subs, views, videos, hidden, taken FROM pub_channel_stats "
        "WHERE channel_id=? AND day>=? ORDER BY day", (int(channel_id), since)).fetchall()
    return [dict(r) for r in rows]


def channel_state(c: sqlite3.Connection, channel_id: int, now=None,
                  spans=(7, 28)) -> dict:
    """То же, что `overview`, но про один канал — плюс ряд для графика."""
    now = time.time() if now is None else now
    ensure_stats_schema(c)
    today = day_of(now)
    cur = (_rows_at(c, [int(channel_id)], today) or {}).get(int(channel_id)) or {}
    out = {"subs": cur.get("subs"), "views": cur.get("views"),
           "videos": cur.get("videos"), "hidden": bool(cur.get("hidden")),
           "day": cur.get("day") or "", "taken": cur.get("taken") or 0.0,
           "key": bool(pub_verify.api_key(c)), "delta": {},
           "series": series(c, channel_id)}
    for n in spans:
        was = (_rows_at(c, [int(channel_id)], _shift(today, n)) or {}).get(int(channel_id)) or {}
        if was.get("day") == out["day"]:
            was = {}
        out["delta"][str(n)] = {
            "subs": _diff(cur, was, "subs"), "views": _diff(cur, was, "views"),
            "videos": _diff(cur, was, "videos"), "since": was.get("day") or ""}
    return out
