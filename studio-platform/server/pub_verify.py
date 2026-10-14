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
# Как часто спрашивать про УЖЕ помеченный к разбору ролик. Фаза у него так и осталась
# `scheduled`, поэтому из `due` он не уходит никогда — и на прежнем получасовом шаге
# спрашивался бы вечно, по 48 единиц в сутки на штуку. Сотня таких (а они копятся:
# приватный или удалённый ролик сам себя не чинит) съедала бы половину суточного
# кошелька, из которого живут и статистика, и ночной обход. Совсем перестать спрашивать
# нельзя: YouTube иногда выпускает ролик позже срока, да и владелец может открыть его
# руками, — но сутки для этого достаточно частый шаг.
STALE_RECHECK_SEC = int(float(os.environ.get("VS_PUBLIC_STALE_RECHECK_SEC")
                              or 24 * 3600))
# Сколько роликов спрашиваем за ОДИН проход. Предел не про квоту (её сторожит
# `quota_allow`), а про длину прохода: без него проход длится столько, сколько роликов
# накопилось, а копятся они навсегда — неподтверждённый выпуск из фазы `scheduled` сам
# не уходит.
#
# Почему пятьдесят. Один вопрос — одна единица из 10 000 суточных, кошелёк общий с
# ночным обходом ВВ2 и статистикой каналов. Таймер поднимает проход раз в полчаса
# (`deploy/studio-verify.timer`), то есть 48 раз в сутки: потолок 50 × 48 = 2400 единиц,
# около четверти кошелька. Ожидаемая работа — от 150 до 1800 вопросов в сутки, это
# 3…38 за проход, и пятьдесят её накрывают с запасом. Сверху предел держит время:
# сетевой таймаут 15 с на вопрос, 50 × 15 = 750 с — меньше, чем TimeoutStartSec=900 у
# юнита, поэтому проход не убьют на полуслове.
#
# Остаток не теряется: следующий проход через полчаса спросит тех, до кого не дошли, —
# ровно в том же порядке (сначала самый старый слот).
# Не меньше одного вопроса за проход. Ноль в настройке (или отрицательное число,
# или опечатка вроде «0 50») остановил бы подтверждение выхода НАВСЕГДА, и при этом
# проход отчитывался бы как успешный: ни одной ошибки, просто «проверено 0». Такую
# поломку не замечают месяцами — поэтому предел снизу жёсткий.
ASK_LIMIT = max(1, int(float(os.environ.get("VS_PUBLIC_ASK_LIMIT") or 50)))

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
    """Ключ YouTube Data API. Одно место на всю студию — его зовут все потребители.

    Старшинство: окружение → файл ключей (его правит администратор на экране «Ключи»)
    → `admin_state`. Порядок не произвольный.

    Окружение первое, как везде в проекте: им машину чинят, когда экран недоступен.
    Файл ключей ВЫШЕ `admin_state` потому, что именно в него пишет форма: будь наоборот,
    владелец вставил бы новый ключ, увидел «сохранено» и не понял, почему ничего не
    изменилось, — старое значение из базы продолжало бы побеждать.
    `admin_state` остаётся последним и поддерживается ради тех установок, где ключ уже
    лежит там: молча перестать его видеть значило бы погасить проверку публикаций.
    """
    env = (os.environ.get("VS_YT_API_KEY") or "").strip()
    if env:
        return env
    try:
        import server_keys
        value = str(_load_keys(server_keys).get(KEY_STATE) or "").strip()
        if value:
            return value
    except Exception:                                              # noqa: BLE001
        pass          # файла ключей нет или он испорчен — спрашиваем базу, как раньше
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (KEY_STATE,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return ""
    return (row[0] or "").strip() if row else ""


def _load_keys(server_keys):
    """Отдельной функцией — чтобы тест мог подменить чтение файла, не трогая диск."""
    return server_keys._load()                                     # noqa: SLF001


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


# ── Проверка без ключа: публичная страница ролика ────────────────────────────────
#
# Решение владельца 04.10.2026: ключ YouTube Data API не нужен. Выход проверяем так же,
# как давно и успешно проверяет старый кабинет (/root/ops/ww2_sheet/published_check.py,
# 432 подтверждения из 434): публичный oEmbed отвечает 200 только на ролик, который
# сейчас может посмотреть любой. Не ответил — страница ролика своими словами говорит
# почему: приватный, удалён, «слишком длинный» (канал без подтверждённого телефона).
#
# Чего так не видно: oEmbed отвечает 200 и на ролик «доступ по ссылке». Наши ролики
# льются публичными, так что этот случай — редкий дефект, и ловит его только ключ.
PAGE_CHECK = (os.environ.get("VS_PUBLIC_PAGE_CHECK", "1") or "1") != "0"
OEMBED = "https://www.youtube.com/oembed"
WATCH = "https://www.youtube.com/watch?v="
PAGE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
VIA_PAGE = "page"
VIA_KEY = "key"
# Дословные причины YouTube → что делать человеку. Ключ — причина в нижнем регистре.
PAGE_HINTS = {
    "this video was removed because it was too long":
        "аккаунт канала не подтверждён телефоном: без этого YouTube не держит ролики "
        "длиннее 15 минут",
    "this video is private": "отложка не сработала, ролик остался приватным",
}
_PAGE_MARKS = (
    ("VIDEO_STATUS_REJECTED", "YouTube забраковал файл"),
    ("This video is private", "ролик приватный — отложка не сработала"),
    ("LOGIN_REQUIRED", "ролик приватный — отложка не сработала"),
    ("This video has been removed", "ролик удалён"),
    ("terminated", "канал отключён"),
    ("not available in your country", "заблокирован по стране"),
    ("copyright", "жалоба по правам"),
    ("Video unavailable", "ролик недоступен (брак файла или удалён)"),
)


def _get_page(url: str):
    """(код | None, тело). None — до YouTube не достучались: это не улика против ролика."""
    req = urllib.request.Request(url, headers={"User-Agent": PAGE_UA, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:                                       # noqa: BLE001 — сеть
        return None, type(exc).__name__


def can_check(c) -> bool:
    """Можно ли вообще подтверждать выход: есть ключ или включена проверка по странице."""
    return bool(api_key(c)) or PAGE_CHECK


def page_reason(video_id: str, get=None) -> str:
    """Почему ролик не публичный — словами самого YouTube со страницы ролика.

    Главное — блок playabilityStatus: там человеческий текст. Страница весит под
    мегабайт, и блок лежит далеко от начала, поэтому читаем её целиком.
    """
    code, body = (get or _get_page)(WATCH + urllib.parse.quote(video_id, safe=""))
    if code is None:
        return f"страница ролика не открылась ({body})"
    spot = re.search(r'"playabilityStatus":\{.{0,600}', body or "")
    if spot:
        said = re.search(r'"description":\{"content":"([^"]{3,200})"', spot.group(0))
        why = re.search(r'"reason":"([^"]{3,200})"', spot.group(0))
        text = (said.group(1) if said else "") or (why.group(1) if why else "")
        if text:
            hint = PAGE_HINTS.get(text.lower())
            return f"YouTube: «{text}»" + (f" → {hint}" if hint else "")
    low = (body or "").lower()
    for needle, human in _PAGE_MARKS:
        if needle.lower() in low:
            return human
    return f"страница ответила {code}, причина не опознана"


def check_page(video_id: str, get=None) -> dict:
    """Один ролик без ключа. Тот же вид ответа, что у `check`, плюс via='page'.

    Квоты у этого пути нет и единиц он не тратит (cost=0). Причину «почему не
    публичный» сюда не тянем: это вторая, тяжёлая страница, и нужна она только когда
    терпение кончилось (см. `run`).
    """
    if not video_id:
        return {"state": UNKNOWN, "error": "нет video_id", "cost": 0, "via": VIA_PAGE}
    code, body = (get or _get_page)(
        OEMBED + "?" + urllib.parse.urlencode(
            {"format": "json", "url": f"https://youtu.be/{video_id}"}))
    if code == 200:
        title = ""
        try:
            title = str(json.loads(body or "{}").get("title") or "")
        except Exception:                                          # noqa: BLE001
            pass
        return {"state": PUBLIC, "privacy": "public", "upload": "", "cost": 0,
                "published_at": "", "title": title, "error": "", "via": VIA_PAGE}
    if code is None:
        return {"state": UNKNOWN, "error": f"YouTube не ответил: {body}", "cost": 0,
                "via": VIA_PAGE}
    if 400 <= int(code) < 500:
        # Как у проверенного старого кабинета: любой отказ 4xx — «публично не видно»
        # (401 приватный, 404 удалён, 400 на недоступный ролик). Почему именно — скажет
        # страница ролика, когда кончится терпение. 5xx — сбой YouTube, вывода нет.
        return {"state": NOT_PUBLIC, "privacy": "", "upload": "", "cost": 0,
                "error": f"публично ролика не видно (oEmbed {code})", "via": VIA_PAGE}
    return {"state": UNKNOWN, "error": f"oEmbed ответил {code}", "cost": 0, "via": VIA_PAGE}


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
            "published_at": snippet.get("publishedAt") or "",
            "title": snippet.get("title") or "", "error": ""}


def due(c, tenant=None, now=None, grace=None) -> list:
    """Кого пора спросить: расписание подтверждено, время слота прошло, ролик известен.

    Порядок — по времени слота: первым проверяем то, что должно было выйти раньше.

    Здесь намеренно нет ни LIMIT, ни нижней границы по времени, и это не забывчивость.
    Список отвечает на вопрос «кто вообще под проверкой» — его же печатает
    `dev/verify_public.py` без `--run`, и урезанный ответ там был бы ложью.

    Обрезать его по тому же порядку было бы прямо опасно: первыми стоят самые старые
    слоты, а это как раз залежавшиеся выпуски — те, что уже помечены к разбору и
    спрашиваются раз в сутки (STALE_RECHECK_SEC). Стопка таких навсегда вытеснила бы из
    проверки сегодняшние выпуски, и подтверждение выхода тихо перестало бы работать.
    Поэтому предел стоит там, где идёт настоящая трата, — на числе ВОПРОСОВ за проход
    (ASK_LIMIT в `run`): пропуск недавно спрошенного вопроса не стоит и в предел не
    попадает.
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


def _plural_videos(n: int) -> str:
    """«1 ролик» / «2 ролика» / «5 роликов» — сообщение читает человек."""
    n = int(n)
    t = n % 100
    if 11 <= t <= 14:
        форма = "роликов"
    elif n % 10 == 1:
        форма = "ролик"
    elif 2 <= n % 10 <= 4:
        форма = "ролика"
    else:
        форма = "роликов"
    return f"{n} {форма}"


def хорошая_новость(c, вышли) -> str:
    """Сообщение «ролики вышли» — или пустая строка, если выходить было некому.

    Отдельной функцией, а не строкой внутри прохода, по двум причинам. Первая: это
    единственная ХОРОШАЯ новость, которую студия отправляет сама, и проверять её текст
    надо без сети. Вторая: до 02.10.2026 выход ролика не сообщался никак — проход
    поднимал запись `scheduled` → `published` в базе, и всё. Владелец 02.10: «да, я
    хочу знать, что видосы готовы». Знать из базы — это не знать.

    Канал называем по имени, а не по UC: человеку нужно имя. Нет имени в справочнике —
    пишем UC, но не молчим.
    """
    if not вышли:
        return ""
    строки = [f"✅ Вышли на YouTube: {_plural_videos(len(вышли))}", ""]
    for запись in вышли[:20]:
        имя = ""
        uc = str(запись.get("channel_id") or "")
        if uc:
            try:
                row = c.execute(
                    "SELECT name, lang FROM pub_channels WHERE uc=?", (uc,)).fetchone()
            except Exception:                                      # noqa: BLE001
                row = None
            if row:
                имя = str(row[0] or "")
                if row[1]:
                    имя += f" ({row[1]})"
        строки.append(f"• {имя or uc or 'канал неизвестен'}")
        строки.append(f"  youtu.be/{запись.get('video_id') or '—'}")
    остаток = len(вышли) - 20
    if остаток > 0:
        строки.append(f"…и ещё {_plural_videos(остаток)}")
    строки += ["",
               "Это ответ самой YouTube на вопрос «ролик публичный?», а не наше "
               "расписание: до этой строки «залито» и «вышло» были разными вещами."]
    return "\n".join(строки)


def run(c, tenant=None, now=None, fetch=None, key=None, log=None, page=None) -> dict:
    """Один поздний проход. Возвращает счётчики и не бросает наружу.

    Что делает с каждым исходом:
      public     → фаза становится `published`, в журнал выпуска пишется подтверждение;
      unlisted   → фазу НЕ повышаем: ролик вышел, но не для всех — это дефект;
      not_public → до истечения терпения молчим, после — помечаем к разбору и
                   спрашиваем дальше раз в сутки, а не раз в полчаса (STALE_RECHECK_SEC);
      unknown    → не делаем ничего: отсутствие ответа не является ответом;
      quota      → проход ОСТАНАВЛИВАЕТСЯ целиком и ждёт сброса: ни один выпуск при
                   этом не помечен и не «проверен», потому что про ролики мы ничего
                   не узнали. Ровно этого не хватило ночью 18.09: прогон конкурентов
                   получил 403 на первом же и объявил необработанными всех остальных.

    Записывается проход МЕЛКИМИ КУСКАМИ: один выпуск — одна транзакция. Раньше весь
    проход был одной транзакцией с единственным `commit` в конце, и замок записи SQLite
    держался от первого ролика до последнего — то есть и на время сетевых вопросов к
    googleapis. С получасовым таймером это означало, что кабинет и пульт получают
    «database is locked» на всю длину прохода: у пульта ожидание 5 с, у кабинета 30 с,
    а проход на сотне выпусков идёт дольше. Поэтому перед каждым сетевым вопросом
    незакрытых записей не остаётся.

    Куски выбраны по выпуску, потому что выпуски друг от друга не зависят: повышение
    фазы, ответ провайдера в журнал фаз и строка в журнал выпуска — про один ролик, и
    разорваться на середине они не должны.
    """
    now = now or time.time()
    log = log or (lambda *_a: None)
    key = api_key(c) if key is None else key
    # «вышли» — не счётчик, а список: по нему строится сообщение «ролики готовы», и
    # собрать его можно только во время прохода (см. место, где ставится phase).
    out = {"вышли": [], "checked": 0, "published": 0, "unlisted": 0, "waiting": 0,
           "stale": 0, "unknown": 0, "skipped": 0, "quota_wait": 0,
           # Сколько осталось следующему проходу из-за предела ASK_LIMIT. Отдельно от
           # `quota_wait`: там ждут сброса квоты, здесь — просто очередь, и путать эти
           # два «не спросили» нельзя.
           "deferred": 0,
           # Причина и состояние кошелька заводятся ВМЕСТЕ со счётчиками, а не в конце
           # прохода. Самые частые исходы — «нечего проверять» и «нет ключа» — уходят
           # ранним возвратом, и читатель отчёта (экран, dev/verify_public.py) падал бы
           # с KeyError ровно там, где всё в порядке.
           "why": "", "quota": pub_calls.quota_state(c, now=now)}
    rows = due(c, tenant, now)
    # Без ключа — по публичной странице (решение владельца 04.10.2026). `page` — транспорт
    # этого пути для тестов; его передача сама по себе включает путь.
    by_page = not key and (page is not None or PAGE_CHECK)
    out["via"] = VIA_PAGE if by_page else VIA_KEY
    if not rows:
        return out
    if not key and not by_page:
        out["unknown"] = len(rows)
        out["why"] = ("нет ключа YouTube Data API: вставить на экране «Ключи», "
                      "строка «YouTube Data API» — или задать переменной "
                      "VS_YT_API_KEY на сервере")
        return out

    asked = 0                  # сколько вопросов в сеть задали — по ним и предел
    for idx, rel in enumerate(rows):
        rid = rel["release_id"]
        seen = _seen(c, rid)
        # Один и тот же ролик не дёргаем чаще, чем имеет смысл: квота API конечна.
        # Шаг зависит от того, спрашивали ли его уже ПОСЛЕ того, как кончилось
        # терпение. Мерить по одному лишь `now >= give_up_at` нельзя: тогда ролик,
        # перешагнувший порог между проходами, получил бы суточную паузу вместо
        # пометки к разбору, и о нём узнали бы через сутки после срока.
        give_up_at = float(rel["slot_at"] or now) + GIVE_UP_SEC
        seen_at = float(seen.get("at") or 0) if seen else 0.0
        period = STALE_RECHECK_SEC if seen_at >= give_up_at else RECHECK_SEC
        if seen and (now - seen_at) < period:
            out["skipped"] += 1
            continue
        # Свой бюджет проверяем ПЕРЕД вызовом: когда единицы кончились по нашему же
        # счёту, идти за чужим 403 незачем — ответ известен заранее.
        allow = ({"ok": True} if by_page
                 else pub_calls.quota_allow(c, CONSUMER, COST, now=now))
        if not allow["ok"]:
            out["quota_wait"] += len(rows) - idx
            out["why"] = allow["why"]
            # Вслух — на КАЖДОМ проходе. Это консольный вывод самого прохода, а не
            # журнал выпуска: правило «в журнал один раз» он не нарушает. Без него
            # оператор, запустивший проверку руками во время ожидания, получает ровно
            # симптом ночи 18.09 — «ничего не произошло, а почему — неизвестно».
            log(f"  квота: {out['why']}")
            break
        if asked >= ASK_LIMIT:
            # Предел вопросов за проход. Это не потеря: остаток спросит следующий
            # проход через полчаса, порядок тот же — сначала самый старый слот.
            # В `deferred` — сколько выпусков проход НЕ посмотрел. Не «сколько ещё
            # спросим»: часть из них следующий проход пропустит как недавно спрошенные.
            # Обещать точное число вопросов вперёд мы не можем, а врать тут не за что.
            out["deferred"] = len(rows) - idx
            out["why"] = (f"за один проход спрашиваем не больше {ASK_LIMIT} роликов: "
                          f"до остальных {out['deferred']} проход не дошёл, их "
                          f"посмотрит следующий — ничего не потеряно")
            log(f"  предел прохода: {out['why']}")
            break
        # Сетевой вопрос задаётся ВНЕ открытой транзакции. Здесь коммитится предыдущий
        # выпуск: пока ждём ответ googleapis (до TIMEOUT=15 с), замок записи SQLite не
        # должен быть наш, иначе кабинет и пульт на это время теряют базу.
        c.commit()
        res = (check_page(rel["video_id"], get=page) if by_page
               else check(rel["video_id"], key, fetch=fetch, c=c, now=now))
        asked += 1
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
        if (by_page and res["state"] == NOT_PUBLIC
                and now >= float(rel["slot_at"] or now) + GIVE_UP_SEC):
            # Терпение кончилось — теперь стоит открыть страницу ролика: только она
            # говорит, приватный он, удалён или снят за длину. Раньше незачем: это
            # вторая, тяжёлая страница, а до срока «не виден» — обычное ожидание.
            res = dict(res, reason=page_reason(rel["video_id"], get=page))
        payload = dict(res, video_id=rel["video_id"])
        _note(c, rid, payload, now)

        if res["state"] == PUBLIC:
            # Причину блокировки снимаем вместе с подтверждением. Путь реален: ролик
            # сперва увидели «только по ссылке» (причина записана), владелец открыл его
            # публично, следующий проход подтвердил выход. Оставленная причина осталась
            # бы на экране рядом с зелёным «вышел публично» — экран противоречил бы сам
            # себе ровно той же монетой, какой врал до этой работы, только наоборот.
            c.execute("UPDATE pub_releases SET phase='published', blocked_reason=NULL, "
                      "updated=? WHERE id=?", (now, rel["id"]))
            pub_log.add(c, rid, "публикация",
                        f"подтверждено: ролик {rel['video_id']} публичный"
                        + (f", вышел {res.get('published_at')}"
                           if res.get("published_at") else ""), now=now)
            out["published"] += 1
            out["вышли"].append({
                "release_id": rid,
                "video_id": rel["video_id"],
                "channel_id": (rel["channel_id"] if "channel_id" in rel.keys() else ""),
            })
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
            hours = int((now - float(rel["slot_at"] or now)) / 3600)
            if by_page:
                reason = res.get("reason") or "причина не получена"
                c.execute("UPDATE pub_releases SET blocked_reason=?, updated=? WHERE id=?",
                          (f"выход не подтверждён: {reason}"[:500], now, rel["id"]))
                pub_log.add(c, rid, "публикация",
                            f"прошло {hours} ч после слота, а ролик {rel['video_id']} "
                            f"публично не виден. {reason}", level="warn", now=now)
            else:
                c.execute("UPDATE pub_releases SET blocked_reason=?, updated=? WHERE id=?",
                          ("выход не подтверждён: ключ API ролика не видит", now, rel["id"]))
                pub_log.add(c, rid, "публикация",
                            f"прошло {hours} ч "
                            f"после слота, а ролик {rel['video_id']} ключом API не виден. "
                            f"Он либо ещё приватный, либо удалён — ключ этого не различает, "
                            f"нужен OAuth-токен канала.", level="warn", now=now)
            out["stale"] += 1
            log(f"  {rid}: не подтверждён")
        else:
            out["unknown"] += 1
            log(f"  {rid}: ответа нет ({res.get('error')})")
    # Хвост: последний выпуск прохода и запись ожидания квоты, если проход упёрся в 403.
    c.commit()
    # Кошелёк кладём в отчёт прохода: «сколько единиц осталось и кто их съел» —
    # это то, чего ночью 18.09 не было видно вообще, а узнать пришлось по факту 403.
    out["quota"] = pub_calls.quota_state(c, now=now)
    return out
