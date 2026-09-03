#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поздняя проверка: ролик действительно вышел (ТЗ §11).

Зачем отдельно. Выпуск закрывается по подтверждённому расписанию: Studio показала
SCHEDULED на нужном канале в нужное время. Это честное «сделано» для конвейера, но
это **не** «ролик вышел». Слово «опубликовано» в интерфейсе сегодня означает первое,
а читается как второе — известная ловушка проекта.

Поэтому проверка идёт отдельным поздним проходом, после наступления времени слота, и
только **повышает** запись: `scheduled` → `published`. Ничего не отменяет, ничего не
перезаливает, в необратимую фазу не лезет. Хуже выпуску от неё стать не может.

Доказательство — machine-readable ответ YouTube Data API (`videos.list`, `part=status`),
а не разбор страницы. Правило проекта: браузер результат не доказывает.

**Чего этой проверкой принципиально не видно.** Ключ API видит только публичные и
доступные по ссылке ролики. Запланированный (то есть ещё приватный) и удалённый
ролик выглядят одинаково — пустым ответом. Различить их можно только OAuth-токеном
канала; это решение владельца, и придумывать его за него нельзя. Поэтому пустой ответ
после срока — не приговор, а помеченный к разбору случай.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pub_log

API = "https://www.googleapis.com/youtube/v3/videos"
TIMEOUT = 15
KEY_STATE = "youtube_api_key"

# Через сколько после времени слота спрашивать. Ролик выходит по расписанию YouTube,
# а не по нашему: минуты расхождения нормальны, часы — нет.
GRACE_SEC = int(float(os.environ.get("VS_PUBLIC_GRACE_SEC") or 1800))
# Как часто перепроверять один и тот же ролик, пока он не вышел.
RECHECK_SEC = int(float(os.environ.get("VS_PUBLIC_RECHECK_SEC") or 1800))
# После какого времени молчания перестаём ждать и помечаем к разбору.
GIVE_UP_SEC = int(float(os.environ.get("VS_PUBLIC_GIVE_UP_SEC") or 6 * 3600))

PUBLIC = "public"          # подтверждено: ролик публичный
UNLISTED = "unlisted"      # вышел, но по ссылке — это дефект, а не успех
NOT_PUBLIC = "not_public"  # ключ его не видит: ещё приватный ЛИБО удалён
UNKNOWN = "unknown"        # нет ключа или API не ответил — вывода нет


def api_key(c) -> str:
    """Ключ YouTube Data API. Окружение старше базы: так же, как везде в проекте."""
    env = (os.environ.get("VS_YT_API_KEY") or "").strip()
    if env:
        return env
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (KEY_STATE,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return ""
    return (row[0] or "").strip() if row else ""


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def check(video_id: str, key: str, fetch=None) -> dict:
    """Один ролик. Возвращает {state, privacy, upload, published_at, error}.

    Транспорт передаётся отдельно — чтобы решение разбиралось тестами без сети и без
    настоящего ключа.
    """
    if not video_id:
        return {"state": UNKNOWN, "error": "нет video_id"}
    if not key:
        return {"state": UNKNOWN, "error": "нет ключа YouTube Data API"}
    url = API + "?" + urllib.parse.urlencode(
        {"part": "status,snippet", "id": video_id, "key": key})
    try:
        body = (fetch or _fetch)(url)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")[:200]
        except Exception:                                          # noqa: BLE001
            pass
        return {"state": UNKNOWN, "error": f"HTTP {exc.code} {raw}"}
    except Exception as exc:                                       # noqa: BLE001
        return {"state": UNKNOWN, "error": f"{type(exc).__name__}: {exc}"}

    items = (body or {}).get("items") or []
    if not items:
        # Пусто — это ДВА разных случая, и ключ API их не различает. См. шапку модуля.
        return {"state": NOT_PUBLIC, "privacy": "", "upload": "",
                "error": "ключ не видит ролик: он либо ещё приватный, либо удалён"}
    status = (items[0].get("status") or {})
    snippet = (items[0].get("snippet") or {})
    privacy = str(status.get("privacyStatus") or "")
    upload = str(status.get("uploadStatus") or "")
    state = PUBLIC if privacy == "public" else (
        UNLISTED if privacy == "unlisted" else NOT_PUBLIC)
    return {"state": state, "privacy": privacy, "upload": upload,
            "published_at": snippet.get("publishedAt") or "", "error": ""}


def due(c, tenant=None, now=None, grace=None) -> list:
    """Кого пора спросить: расписание подтверждено, время слота прошло, ролик известен.

    Порядок — по времени слота: первым проверяем то, что должно было выйти раньше.
    """
    now = now or time.time()
    grace = GRACE_SEC if grace is None else grace
    where, args = ((" AND tenant_id=?", (tenant,)) if tenant else ("", ()))
    return list(c.execute(
        "SELECT * FROM pub_releases WHERE phase='scheduled' AND video_id IS NOT NULL "
        "AND video_id!='' AND slot_at <= ?" + where + " ORDER BY slot_at",
        (now - grace,) + args))


def _seen(c, release_id: str) -> dict:
    """Когда этот ролик спрашивали в прошлый раз и что ответили."""
    row = c.execute(
        "SELECT payload, created FROM pub_release_events WHERE release_id=? "
        "AND phase='public_check' ORDER BY seq DESC LIMIT 1", (release_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row[0] or "{}")
    except (ValueError, TypeError):
        data = {}
    data["at"] = row[1]
    return data


def _note(c, release_id: str, payload: dict, now: float) -> None:
    last = c.execute("SELECT MAX(seq) FROM pub_release_events WHERE release_id=?",
                     (release_id,)).fetchone()[0] or 0
    c.execute("INSERT INTO pub_release_events(release_id,seq,phase,video_id,payload,created)"
              " VALUES(?,?,?,?,?,?)",
              (release_id, last + 1, "public_check", payload.get("video_id"),
               json.dumps(payload, ensure_ascii=False), now))


def run(c, tenant=None, now=None, fetch=None, key=None, log=None) -> dict:
    """Один поздний проход. Возвращает счётчики и не бросает наружу.

    Что делает с каждым исходом:
      public     → фаза становится `published`, в журнал выпуска пишется подтверждение;
      unlisted   → фазу НЕ повышаем: ролик вышел, но не для всех — это дефект;
      not_public → до истечения терпения молчим, после — помечаем к разбору;
      unknown    → не делаем ничего: отсутствие ответа не является ответом.
    """
    now = now or time.time()
    log = log or (lambda *_a: None)
    key = api_key(c) if key is None else key
    out = {"checked": 0, "published": 0, "unlisted": 0, "waiting": 0,
           "stale": 0, "unknown": 0, "skipped": 0}
    rows = due(c, tenant, now)
    if not rows:
        return out
    if not key:
        out["unknown"] = len(rows)
        out["why"] = ("нет ключа YouTube Data API: положить в admin_state.youtube_api_key "
                      "или VS_YT_API_KEY")
        return out

    for rel in rows:
        rid = rel["release_id"]
        seen = _seen(c, rid)
        # Один и тот же ролик не дёргаем чаще, чем имеет смысл: квота API конечна.
        if seen and (now - float(seen.get("at") or 0)) < RECHECK_SEC:
            out["skipped"] += 1
            continue
        res = check(rel["video_id"], key, fetch=fetch)
        out["checked"] += 1
        payload = dict(res, video_id=rel["video_id"])
        _note(c, rid, payload, now)

        if res["state"] == PUBLIC:
            c.execute("UPDATE pub_releases SET phase='published', updated=? WHERE id=?",
                      (now, rel["id"]))
            pub_log.add(c, rid, "публикация",
                        f"подтверждено: ролик {rel['video_id']} публичный"
                        + (f", вышел {res.get('published_at')}"
                           if res.get("published_at") else ""), now=now)
            out["published"] += 1
            log(f"  {rid}: вышел")
        elif res["state"] == UNLISTED:
            c.execute("UPDATE pub_releases SET blocked_reason=?, updated=? WHERE id=?",
                      ("ролик доступен только по ссылке, а должен быть публичным",
                       now, rel["id"]))
            pub_log.add(c, rid, "публикация",
                        f"ролик {rel['video_id']} доступен по ссылке, но не публичный — "
                        f"это дефект: фаза не повышена", level="error", now=now)
            out["unlisted"] += 1
            log(f"  {rid}: только по ссылке")
        elif res["state"] == NOT_PUBLIC:
            late = now - (float(rel["slot_at"] or now) + GIVE_UP_SEC)
            if late < 0:
                out["waiting"] += 1
                continue
            c.execute("UPDATE pub_releases SET blocked_reason=?, updated=? WHERE id=?",
                      ("выход не подтверждён: ключ API ролика не видит", now, rel["id"]))
            pub_log.add(c, rid, "публикация",
                        f"прошло {int((now - float(rel['slot_at'] or now)) / 3600)} ч "
                        f"после слота, а ролик {rel['video_id']} ключом API не виден. "
                        f"Он либо ещё приватный, либо удалён — ключ этого не различает, "
                        f"нужен OAuth-токен канала.", level="warn", now=now)
            out["stale"] += 1
            log(f"  {rid}: не подтверждён")
        else:
            out["unknown"] += 1
            log(f"  {rid}: ответа нет ({res.get('error')})")
    c.commit()
    return out
