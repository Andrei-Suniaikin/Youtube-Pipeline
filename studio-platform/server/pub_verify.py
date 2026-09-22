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

**Квота — общий ресурс, а не наш.** Ключ один на всё: этой проверкой и ночным обходом
конкурентов ВВ2 тратится один суточный кошелёк. Поэтому 403 по квоте здесь означает
ровно «повторить позже»: выпуск не помечается сломанным, ответ не записывается как
результат проверки (про ролик мы ничего не узнали), проход останавливается целиком —
идти следующими роликами в тот же отказ бессмысленно. Учёт и ожидание сброса живут в
`pub_calls` рядом с журналом платных вызовов: квота исчерпаема так же, как деньги.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import pub_calls
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
QUOTA = "quota"            # единицы кончились — ПОВТОРИТЬ ПОЗЖЕ, ролик ни при чём

# Кто тратит — под этим именем расход видно в `pub_calls.quota_usage`.
CONSUMER = "verify"
# Что тратим: один `videos.list` = 1 единица. Берём из общей таблицы цен, чтобы
# цена вызова жила в одном месте с бюджетом, а не была тут переписана числом.
COST = pub_calls.QUOTA_COSTS["videos.list"]

# Ответы Google, означающие «единицы кончились» (суточный кошелёк) и «слишком часто»
# (короткий предел на частоту). Первое ждёт до полуночи по тихоокеанскому времени,
# второе — минуты, и путать их нельзя: из-за частоты терять сутки не за что.
QUOTA_REASONS = ("quotaexceeded", "dailylimitexceeded")
RATE_REASONS = ("ratelimitexceeded", "userratelimitexceeded")


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


def _named(names, low: str) -> bool:
    """Встречается ли имя причины ЦЕЛИКОМ — с границей слова, а не подстрокой.

    Подстрока здесь ошибается опасно: `dailyLimitExceededUnreg` содержит внутри себя
    `dailyLimitExceeded`, но означает совсем другое — «запрос ушёл без ключа». Приняв
    его за суточную квоту, система молча встала бы до полуночи вместо того, чтобы
    позвать человека чинить настройку.
    """
    return any(re.search(r"(?<![a-z0-9])" + n + r"(?![a-z0-9])", low) for n in names)


def quota_kind(code: int, raw: str) -> str:
    """Про что этот отказ: «daily» (единицы кончились), «rate» (частота) или пусто.

    Разбираем именно `reason` из тела ответа, а не код: 403 у Google означает и
    исчерпанную квоту, и выключенный API, и запрещённый ключ. Считать ключевую поломку
    исчерпанной квотой — значит молча ждать до полуночи вместо того, чтобы позвать
    человека; поэтому при неразборчивом теле квотой это НЕ считается.

    По той же причине ищем точные имена причин, а не слово «quota» в теле: слово это
    встречается и в домене ошибки (`youtube.quota`), и в тексте про выключенный API.

    Когда причины в теле нашлись — решение принимается ТОЛЬКО по ним: чужую причину
    (тот самый `dailyLimitExceededUnreg`) запасной текстовый разбор иначе всё равно
    перетолкует в квоту, и точный разбор окажется бесполезен. Запасной разбор включается
    лишь тогда, когда причин в json не нашлось вовсе — тело обрезано или это не json.
    """
    text = str(raw or "")
    reasons = []
    try:
        err = (json.loads(text) or {}).get("error") or {}
        reasons = [r for r in (str((e or {}).get("reason") or "").lower()
                               for e in (err.get("errors") or [])) if r]
    except (ValueError, TypeError, AttributeError):
        pass
    if reasons:
        if any(r in QUOTA_REASONS for r in reasons):
            return "daily"
        if any(r in RATE_REASONS for r in reasons) or code == 429:
            return "rate"
        return ""
    # Причин нет: тело не разобралось. Имя причины в тексте написано теми же буквами —
    # но ищем его с границей слова, см. `_named`.
    low = text.lower()
    if _named(QUOTA_REASONS, low):
        return "daily"
    if _named(RATE_REASONS, low) or code == 429:
        return "rate"
    return ""


def _charge(c, units: int, now, consumer: str) -> None:
    """Записать расход единиц. Учёт вспомогательный: его сбой не ломает проверку."""
    if c is None or units <= 0:
        return
    try:
        pub_calls.quota_spend(c, consumer, units, now=now)
    except Exception:                                              # noqa: BLE001
        pass


def check(video_id: str, key: str, fetch=None, c=None, now=None,
          consumer: str = CONSUMER) -> dict:
    """Один ролик. Возвращает {state, privacy, upload, published_at, error, cost}.

    Транспорт передаётся отдельно — чтобы решение разбиралось тестами без сети и без
    настоящего ключа.

    `c` необязателен и нужен только для учёта квоты: с ним вызов сначала смотрит, не
    ждём ли мы уже сброса (тогда в сеть не ходим вовсе), а после ответа записывает
    потраченные единицы. Без соединения проверка работает как раньше — такой её зовёт
    `pub_services.do_published`, и ломать ему вызов ради учёта не за что.

    Про `do_published` важно знать правду: сегодня движок в него не заходит НИ РАЗУ.
    `pub_engine.advance` на фазе `scheduled` закрывает выпуск (`status='done'`, около
    pub_engine.py:612) раньше, чем добирается до обработчика следующей фазы, а
    `live_releases` берёт только `pending`/`running`. Значит, весь учёт квоты живёт на
    пути `run()`, и без соединения (а с ним — и без учёта) проверка вызывается только
    из этой мёртвой ветки. Убирать её не за что, но считать её рабочим путём нельзя.
    """
    if not video_id:
        return {"state": UNKNOWN, "error": "нет video_id", "cost": 0}
    if not key:
        return {"state": UNKNOWN, "error": "нет ключа YouTube Data API", "cost": 0}
    if c is not None:
        waiting = pub_calls.quota_wait(c, now=now)
        if waiting:
            # Единицы кончились, сброса ещё не было. Ходить за вторым 403 незачем:
            # он не приблизит сброс, а лог засорит.
            return {"state": QUOTA, "kind": waiting.get("kind") or "daily",
                    "wait": waiting, "cost": 0,
                    "error": pub_calls.quota_why(waiting)}
    url = API + "?" + urllib.parse.urlencode(
        {"part": "status,snippet", "id": video_id, "key": key})
    try:
        body = (fetch or _fetch)(url)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            # Читаем с запасом: решение принимается по `reason` внутри json, а в
            # сообщение об ошибке всё равно уходит только начало.
            raw = exc.read().decode("utf-8", "replace")[:2000]
        except Exception:                                          # noqa: BLE001
            pass
        kind = quota_kind(exc.code, raw)
        if kind:
            wait = {}
            if c is not None:
                wait = pub_calls.quota_block(
                    c, f"HTTP {exc.code} от YouTube Data API: {raw[:120]}",
                    kind=kind, now=now)
            text = pub_calls.quota_why(wait) if wait else (
                "YouTube просит сбавить частоту запросов — повторим позже"
                if kind == "rate" else
                "квота YouTube Data API исчерпана: это не ошибка ролика, "
                "повторим после сброса (полночь по тихоокеанскому времени)")
            return {"state": QUOTA, "kind": kind, "wait": wait, "cost": 0,
                    "error": text}
        # Обычная ошибка: запрос до Google дошёл, значит единицы он списал.
        _charge(c, COST, now, consumer)
        return {"state": UNKNOWN, "error": f"HTTP {exc.code} {raw[:200]}", "cost": COST}
    except Exception as exc:                                       # noqa: BLE001
        # До API не дошли (сеть, таймаут) — единиц это не стоило.
        return {"state": UNKNOWN, "error": f"{type(exc).__name__}: {exc}", "cost": 0}

    _charge(c, COST, now, consumer)
    items = (body or {}).get("items") or []
    if not items:
        # Пусто — это ДВА разных случая, и ключ API их не различает. См. шапку модуля.
        return {"state": NOT_PUBLIC, "privacy": "", "upload": "", "cost": COST,
                "error": "ключ не видит ролик: он либо ещё приватный, либо удалён"}
    status = (items[0].get("status") or {})
    snippet = (items[0].get("snippet") or {})
    privacy = str(status.get("privacyStatus") or "")
    upload = str(status.get("uploadStatus") or "")
    state = PUBLIC if privacy == "public" else (
        UNLISTED if privacy == "unlisted" else NOT_PUBLIC)
    return {"state": state, "privacy": privacy, "upload": upload, "cost": COST,
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
    # `last_seen` — время ПОСЛЕДНЕЙ проверки: одинаковые подряд ответы с 19.09.2026
    # схлопываются в одну строку (pub_engine.log_phase), и `created` у неё означает
    # «когда этот ответ получили впервые». Спрашивать надо по последнему разу, иначе
    # ролик со стабильным ответом опрашивался бы на каждом проходе и жёг квоту API.
    row = c.execute(
        "SELECT payload, COALESCE(last_seen, created) FROM pub_release_events "
        "WHERE release_id=? AND phase='public_check' ORDER BY seq DESC LIMIT 1",
        (release_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row[0] or "{}")
    except (ValueError, TypeError):
        data = {}
    data["at"] = row[1]
    return data


def _note(c, release_id: str, payload: dict, now: float) -> None:
    """Запись через общий log_phase — чтобы и здесь работало схлопывание повторов.

    Своя вставка стояла тут исторически; при ней ролик, который висит приватным
    неделю, копил по строке на каждый проход, и все строки были одинаковые.
    """
    import pub_engine        # локально: модуль тянет requests, а поздней проверке он не нужен
    pub_engine.log_phase(c, release_id, "public_check", now,
                         video_id=payload.get("video_id") or "", payload=payload)


def run(c, tenant=None, now=None, fetch=None, key=None, log=None) -> dict:
    """Один поздний проход. Возвращает счётчики и не бросает наружу.

    Что делает с каждым исходом:
      public     → фаза становится `published`, в журнал выпуска пишется подтверждение;
      unlisted   → фазу НЕ повышаем: ролик вышел, но не для всех — это дефект;
      not_public → до истечения терпения молчим, после — помечаем к разбору;
      unknown    → не делаем ничего: отсутствие ответа не является ответом;
      quota      → проход ОСТАНАВЛИВАЕТСЯ целиком и ждёт сброса: ни один выпуск при
                   этом не помечен и не «проверен», потому что про ролики мы ничего
                   не узнали. Ровно этого не хватило ночью 18.09: прогон конкурентов
                   получил 403 на первом же и объявил необработанными всех остальных.
    """
    now = now or time.time()
    log = log or (lambda *_a: None)
    key = api_key(c) if key is None else key
    out = {"checked": 0, "published": 0, "unlisted": 0, "waiting": 0,
           "stale": 0, "unknown": 0, "skipped": 0, "quota_wait": 0,
           # Причина и состояние кошелька заводятся ВМЕСТЕ со счётчиками, а не в конце
           # прохода. Самые частые исходы — «нечего проверять» и «нет ключа» — уходят
           # ранним возвратом, и читатель отчёта (экран, dev/verify_public.py) падал бы
           # с KeyError ровно там, где всё в порядке.
           "why": "", "quota": pub_calls.quota_state(c, now=now)}
    rows = due(c, tenant, now)
    if not rows:
        return out
    if not key:
        out["unknown"] = len(rows)
        out["why"] = ("нет ключа YouTube Data API: положить в admin_state.youtube_api_key "
                      "или VS_YT_API_KEY")
        return out

    for idx, rel in enumerate(rows):
        rid = rel["release_id"]
        seen = _seen(c, rid)
        # Один и тот же ролик не дёргаем чаще, чем имеет смысл: квота API конечна.
        if seen and (now - float(seen.get("at") or 0)) < RECHECK_SEC:
            out["skipped"] += 1
            continue
        # Свой бюджет проверяем ПЕРЕД вызовом: когда единицы кончились по нашему же
        # счёту, идти за чужим 403 незачем — ответ известен заранее.
        allow = pub_calls.quota_allow(c, CONSUMER, COST, now=now)
        if not allow["ok"]:
            out["quota_wait"] += len(rows) - idx
            out["why"] = allow["why"]
            # Вслух — на КАЖДОМ проходе. Это консольный вывод самого прохода, а не
            # журнал выпуска: правило «в журнал один раз» он не нарушает. Без него
            # оператор, запустивший проверку руками во время ожидания, получает ровно
            # симптом ночи 18.09 — «ничего не произошло, а почему — неизвестно».
            log(f"  квота: {out['why']}")
            break
        res = check(rel["video_id"], key, fetch=fetch, c=c, now=now)
        if res["state"] == QUOTA:
            # 403 по квоте — это «повторить позже», а НЕ ошибка ролика: фазу не
            # трогаем, blocked_reason не ставим и ответ в журнал проверок не пишем.
            # Записать его результатом значило бы отложить настоящую проверку на
            # RECHECK_SEC и показать владельцу ошибку там, где ролик ни при чём.
            out["quota_wait"] += len(rows) - idx
            out["why"] = res.get("error") or ""
            log(f"  квота: {out['why']}")
            # В журнал ВЫПУСКА причина попадает один раз — в начале ожидания. Держит
            # это не признак `fresh`, а ветка выше: сюда проход доходит, только пока
            # ожидания ещё нет (его проверил `quota_allow`), а все следующие проходы
            # разворачиваются раньше, на бюджетной ветке. Условие по `fresh` стояло
            # тут и было ложью: ложным оно стать не может, проверить его нечем, а
            # выглядело защитой. За ночь одна и та же фраза раз в тридцать секунд не
            # объясняет ничего, зато вытесняет из журнала всё остальное.
            pub_log.add(c, rid, "публикация", out["why"], level="warn", now=now)
            break
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
    # Кошелёк кладём в отчёт прохода: «сколько единиц осталось и кто их съел» —
    # это то, чего ночью 18.09 не было видно вообще, а узнать пришлось по факту 403.
    out["quota"] = pub_calls.quota_state(c, now=now)
    return out
