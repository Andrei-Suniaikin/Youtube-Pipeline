#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Лист «Очередь» книги ВВ2 — чужой журнал, из которого мы ТОЛЬКО читаем.

Начало конвейера владелец построил заново вне нашей платформы: Google-таблица ВВ2.
Лист «Очередь» — единственное место, где записано «что какому каналу взято»: A Дата,
B Канал, C Тема, D Донор, E Статус, F Кто делает, G Готовое видео, H Проверка,
I Заметка, J Название оригинала, K ID оригинала, L Язык канала.

**Почему в этом модуле нет и не будет ни одной записи в книгу.** Лист — единственный
журнал чужого процесса: у него нет нашей истории, откатить нашу правку нечем, а
статусы в колонке E ставит рука человека. Любая наша запись туда необратима. Это не
«пока не написали» — это условие задачи. Наружу модуль ходит ровно одним видом
запроса: `GET .../spreadsheets/{id}/values/{range}`. Ни `spreadsheets.get`, ни
перечисления вкладок, ни `open_by_key` из `gspread` (он тянет метаданные книги —
именно список листов, которого мы избегаем).

**Почему скоуп только readonly и ровно один диапазон.** В той же книге лежат листы
«Почты» и «Профили» и колонка «Почта» на листе «Каналы»: доступ к книге равен доступу
к почтам аккаунтов. Скоуп Google до одного листа сузить НЕ умеет, поэтому «только
`Очередь!A2:L`» — наша дисциплина, и она должна быть видна прямо в коде: единственный
скоуп в `SCOPES`, единственный сборщик ссылок `_url`, единственный диапазон из
настройки. Настоящая защита — вариант «зеркальная книга» (владелец отдаёт нам вторую
таблицу с одним `IMPORTRANGE`), и он поддержан тем же кодом: меняется только id.

**Статусы колонки E — не команда нам.** Они означают процесс владельца; запускать по
ним что-либо автоматически нельзя — это повторение ловушки «`done` ≠ опубликовано» на
чужих данных. Мы показываем рядом два столбца: «статус в таблице» и «что у нас».

**Настроек у нас ещё нет.** Идентификатор книги в переданном пакете санитизирован, а
сервисного аккаунта владелец не заводил. Поэтому «не настроено» — это отдельное
состояние с текстом «что именно положить», а не пустой список строк: пустой экран без
объяснения превращает незаконченную настройку в «у владельца ничего не запланировано».

**Что настраивается окружением.** `VS_WW2_TTL_SEC` (60) — как часто перечитываем лист;
`VS_WW2_SETTINGS_TTL_SEC` (600) — пороги отбора; `VS_WW2_TIMEOUT` (15) — один запрос;
`VS_WW2_SETTINGS_BUDGET_SEC` (= таймаут) — общий бюджет на все шесть порогов сразу;
`VS_WW2_FAIL_TTL_SEC` (30) и `VS_WW2_RATE_TTL_SEC` (120) — сколько НЕ переспрашивать
источник после отказа и после 429. Последние три появились из разбора: без них один
показ экрана при недоступной книге занимал поток uvicorn на 6 × 15 с, а после отказа
каждый следующий показ снова шёл наружу.

Сеть живёт в `read_rows`/`read_pool_settings`, разбор — в чистой `parse_values`,
транспорт подменяется параметром `fetch` (тот же приём, что у `pub_verify.check`),
поэтому тесты обходятся без сети, без ключа и без живой таблицы.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pub_ids import video_id as yt_id

# ---------------------------------------------------------------- доступ наружу

# Единственный скоуп. Readonly — не оптимизация, а единственное, что технически
# запрещает нам испортить чужой журнал, если в коде однажды появится ошибка.
SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
# Единственная точка API: values.get. Метода записи здесь нет намеренно.
API = "https://sheets.googleapis.com/v4/spreadsheets"

QUEUE_RANGE = "Очередь!A2:L"

# FORMATTED_VALUE — то, что владелец видит глазами. С UNFORMATTED дата пришла бы
# серийным числом Google, и нам пришлось бы заводить второй закон о датах.
RENDER_ROWS = "FORMATTED_VALUE"
# Для чисел наоборот: UNFORMATTED, иначе «300 000» приедет строкой с пробелами.
RENDER_NUMBERS = "UNFORMATTED_VALUE"

# ------------------------------------------------------------------ настройки

# Запасные имена — те же, что у владельца на VPS (`ww2_sheet.py`): копипаста из его
# `.env` должна работать без переименования.
ENV_ALIASES = {"VS_WW2_SHEET_ID": "WW2_SHEET_ID", "VS_WW2_SA_FILE": "WW2_SA"}
# Переживают рестарт: владелец вписывает id с экрана, а не идёт в systemd. Порядок
# как у `pub_verify.api_key()` — окружение старше базы.
STATE_KEYS = {"VS_WW2_SHEET_ID": "ww2_sheet_id", "VS_WW2_SA_FILE": "ww2_sa_file",
              "VS_WW2_SOURCE": "ww2_source", "VS_WW2_RANGE": "ww2_range",
              "VS_WW2_FILE": "ww2_file"}

SOURCE_SHEETS = "sheets"
SOURCE_FILE = "file"
SOURCE_OFF = "off"

# Куда владельцу класть ключ сервисного аккаунта. Путь, а не значение: секрет
# остаётся файлом вне репозитория (в проекте уже был случай ключа, уехавшего в git).
SA_PLACE = "/etc/studio/ww2_sa.json"

# Чего не хватает — текстом, который можно прочитать вслух и выполнить.
NEED = [
    "идентификатор книги ВВ2 (или её зеркала) — в VS_WW2_SHEET_ID или на экране; "
    "в переданном пакете он затёрт маркером, у нас его нет",
    "сервисный аккаунт Google: файл ключа положить в " + SA_PLACE +
    " (root:root, 0600, в репозиторий не кладём никогда) и указать путь в VS_WW2_SA_FILE",
    "поделиться книгой на почту сервисного аккаунта с правом «Просмотр» — "
    "читать мы будем только диапазон " + QUEUE_RANGE,
    "решение владельца: даём доступ ко всей книге или к зеркалу с одним "
    "IMPORTRANGE(\"…\";\"Очередь!A2:L\") — в книге лежат почты аккаунтов",
    "без Google вовсе: выгрузить лист в JSON и указать VS_WW2_SOURCE=file, VS_WW2_FILE=<путь>",
]


def _env(name: str, default: str = "") -> str:
    v = (os.environ.get(name) or "").strip()
    if not v:
        v = (os.environ.get(ENV_ALIASES.get(name, "")) or "").strip()
    return v or default


def _state(c, key: str) -> str:
    if c is None:
        return ""
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (key,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return ""                      # таблицы ещё нет — это не повод падать
    return (str(row[0] or "")).strip() if row else ""


def _num(name: str, default: float) -> float:
    try:
        v = float((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def settings(c=None) -> dict:
    """Откуда читаем и чего не хватает. Окружение старше базы, база старше умолчания."""
    sheet_id = _env("VS_WW2_SHEET_ID") or _state(c, STATE_KEYS["VS_WW2_SHEET_ID"])
    sa_file = _env("VS_WW2_SA_FILE") or _state(c, STATE_KEYS["VS_WW2_SA_FILE"])
    rng = (_env("VS_WW2_RANGE") or _state(c, STATE_KEYS["VS_WW2_RANGE"])
           or QUEUE_RANGE)
    snapshot = _env("VS_WW2_FILE") or _state(c, STATE_KEYS["VS_WW2_FILE"])
    asked = (_env("VS_WW2_SOURCE") or _state(c, STATE_KEYS["VS_WW2_SOURCE"])).lower()

    if asked in (SOURCE_SHEETS, SOURCE_FILE, SOURCE_OFF):
        source = asked
    elif sheet_id and sa_file:
        source = SOURCE_SHEETS
    elif snapshot:
        source = SOURCE_FILE
    else:
        source = SOURCE_OFF
    # Попросили книгу, а реквизитов нет — это «не настроено», а не «пусто».
    if source == SOURCE_SHEETS and not (sheet_id and sa_file):
        source = SOURCE_OFF
    if source == SOURCE_FILE and not snapshot:
        source = SOURCE_OFF

    timeout = _num("VS_WW2_TIMEOUT", 15.0)
    return {"source": source, "sheet_id": sheet_id, "sa_file": sa_file,
            "range": rng, "file": snapshot,
            "ttl": _num("VS_WW2_TTL_SEC", 60.0),
            "settings_ttl": _num("VS_WW2_SETTINGS_TTL_SEC", 600.0),
            "timeout": timeout,
            # Отказ сети тоже надо помнить, иначе каждый показ экрана после отказа
            # снова идёт наружу — мы сами печатаем «Google просит сбавить частоту» и
            # сами же частоту не сбавляем. 30 с — «кнопку обновить нажали дважды», а
            # не «подождите»; 429 — прямая просьба провайдера, поэтому пауза длиннее.
            "fail_ttl": _num("VS_WW2_FAIL_TTL_SEC", 30.0),
            "rate_ttl": _num("VS_WW2_RATE_TTL_SEC", 120.0),
            # Общий бюджет на ВСЕ шесть порогов: шесть таймаутов подряд — это
            # 6 × timeout секунд занятого потока uvicorn на один показ экрана.
            "settings_budget": _num("VS_WW2_SETTINGS_BUDGET_SEC", timeout),
            "scopes": list(SCOPES), "need": list(NEED)}


def hide_path(path: str) -> str:
    """Путь к ключу в логе — только именем файла.

    Целиком печатать нельзя даже путь: он подсказывает, где лежит секрет, а в проекте
    уже был ключ, уехавший в git открытым текстом.
    """
    p = str(path or "")
    return ("…/" + os.path.basename(p)) if p else "—"


def shown_settings(st: dict) -> dict:
    """Настройки в том виде, в котором их можно отдать на экран и в лог.

    Правило «целиком печатать нельзя даже путь» раньше исполнялось ровно наполовину:
    `hide_path` стоял только в тексте `no_key`, а `read_rows` клал в ответ весь `st`
    вместе с полным `sa_file` — то есть путь всё равно уезжал на экран и в любой лог
    ответа. Наружу теперь уходит только имя файла плюс отдельный признак «файл на
    месте»: владельцу нужно именно это («я положил ключ — он виден?»), а полный путь
    остаётся внутри модуля, где его читает только `_token`.
    """
    out = dict(st)
    out["sa_file"] = hide_path(st.get("sa_file") or "")
    out["sa_present"] = bool(st.get("sa_file")) and os.path.exists(st["sa_file"])
    return out


# ------------------------------------------------------------ разбор строки

COLUMNS = ["date", "channel_sheet", "topic", "donor", "status_sheet", "who",
           "result_url", "check", "note", "donor_title", "video_id", "lang_sheet"]
WIDTH = len(COLUMNS)

# Форматы дат ровно те, что разбирает сам владелец (`ww2_sheet.parse_date`).
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y")

_LINKISH = re.compile(r"https?://|youtu\.?be|/shorts/|[?&]v=", re.I)


def _date(raw: str):
    """(iso, ts) или ('', 0.0). Секунды — ТОЛЬКО для сортировки экрана.

    Часового пояса книги мы не знаем и придумывать его не будем: у владельца в коде
    живёт +07, но это пояс его машины, а не смысл колонки. Поэтому дата переводится в
    полночь UTC — сравнивать строки между собой этого хватает, а считать по ней сроки
    нельзя, и в колонке H владелец считает «просрочено» сам.
    """
    s = (raw or "").strip()
    for f in DATE_FORMATS:
        try:
            d = dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
        return d.isoformat(), dt.datetime(d.year, d.month, d.day,
                                          tzinfo=dt.timezone.utc).timestamp()
    return "", 0.0


def parse_values(values, now=None) -> list:
    """Ответ values.get → список строк очереди. Чистая функция, сети не касается."""
    now = float(now or time.time())
    out = []
    for i, raw in enumerate(values or []):
        cells = list(raw or [])
        # Хвостовые пустые ячейки Sheets просто не возвращает: строка приезжает длиной
        # 4 вместо 12. Без добивания справа первая же недозаполненная строка уронила бы
        # чтение по IndexError — и весь экран вместе с ней.
        cells = [str(x if x is not None else "").strip() for x in cells[:WIDTH]]
        cells += [""] * (WIDTH - len(cells))
        row = dict(zip(COLUMNS, cells))
        # Лист заведён на 1000 строк, а J1:L1 — ARRAYFORMULA на весь столбец, поэтому
        # пустых строк приезжает много. Отбрасываем ровно по тому же признаку, по
        # которому молчат формулы самой таблицы: пусты и «Канал», и «Донор».
        if not row["channel_sheet"] and not row["donor"]:
            continue
        # Номер строки листа — только для навигации («открыть в таблице»), НЕ
        # идентичность: владелец сортирует лист, и номера уезжают.
        row["sheet_row"] = i + 2
        row["date_iso"], row["date_ts"] = _date(row["date"])
        row["date_ok"] = bool(row["date_iso"]) or not row["date"]

        # Колонка K уже отдаёт канонические 11 символов — это формула в книге. Но тот
        # же закон у нас свой (`pub_ids.video_id`), и прогнать K через него дешевле,
        # чем однажды обнаружить, что два закона разошлись. Заодно это спасает зеркало
        # и выгрузку в JSON, где формулы колонки K может не быть вовсе.
        vid = yt_id(row["video_id"])
        row["video_id_from"] = "K" if vid else ""
        if not vid and _LINKISH.search(row["donor"] or ""):
            # Донор — ссылка: берём id из неё. Если в «Донор» вписано НАЗВАНИЕ, разбор
            # не зовём вовсе: хвост из 11 латинских символов в названии наш же закон
            # принял бы за идентификатор и приклеил бы строку к чужому ролику.
            vid = yt_id(row["donor"])
            row["video_id_from"] = "донор" if vid else ""
        row["video_id"] = vid
        row["read_at"] = now
        out.append(row)
    return out


# ------------------------------------------------------------------- транспорт

class SourceError(Exception):
    """Отказ источника: у каждого свой вид и свой текст для человека."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind = kind
        self.text = text


NOT_CONFIGURED = "not_configured"


def _url(sheet_id: str, rng: str, render: str) -> str:
    """Единственный сборщик ссылок на API. Всё, что уходит наружу, проходит здесь."""
    return (f"{API}/{urllib.parse.quote(sheet_id, safe='')}/values/"
            f"{urllib.parse.quote(rng, safe='')}?"
            + urllib.parse.urlencode({"valueRenderOption": render,
                                      "majorDimension": "ROWS"}))


_TOKEN = {"file": "", "value": "", "until": 0.0}


def _token(sa_file: str, now: float) -> str:
    """Bearer сервисного аккаунта. `google-auth` импортируется лениво.

    Лениво — потому что вариант «читаем выгруженный JSON» зависимостей не требует
    вовсе, и модуль обязан работать (и тестироваться) на машине, где google-auth не
    стоит. Ставить её в requirements имеет смысл только после решения владельца.

    Под своим замком: строки и пороги читаются под РАЗНЫМИ замками, и без него два
    одновременных показа экрана дали бы два обновления токена вместо одного.
    """
    with _TOKEN_LOCK:
        return _token_value(sa_file, now)


def _token_value(sa_file: str, now: float) -> str:
    if _TOKEN["file"] == sa_file and _TOKEN["value"] and now < _TOKEN["until"]:
        return _TOKEN["value"]
    if not os.path.exists(sa_file):
        raise SourceError("no_key", "файла ключа сервисного аккаунта нет: "
                                    + hide_path(sa_file))
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except Exception:                                              # noqa: BLE001
        raise SourceError("no_lib", "нет библиотеки google-auth "
                                    "(pip install google-auth==2.58.0) — или выберите "
                                    "источник «файл»")
    try:
        creds = service_account.Credentials.from_service_account_file(
            sa_file, scopes=list(SCOPES))
        creds.refresh(Request())
    except Exception as exc:                                       # noqa: BLE001
        # Текст исключения не печатаем целиком: в нём бывает содержимое ключа.
        raise SourceError("bad_key", "ключ сервисного аккаунта не принят "
                                     f"({type(exc).__name__})")
    exp = getattr(creds, "expiry", None)
    until = now + 300.0
    if exp is not None:
        try:
            until = exp.replace(tzinfo=dt.timezone.utc).timestamp() - 60.0
        except Exception:                                          # noqa: BLE001
            pass
    _TOKEN.update({"file": sa_file, "value": creds.token or "", "until": until})
    return _TOKEN["value"]


def _fetch(url: str, token: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _http_error(exc: urllib.error.HTTPError) -> SourceError:
    """403 и 404 у Google означают разное, и человеку надо сказать именно что."""
    raw = ""
    try:
        raw = exc.read().decode("utf-8", "replace")[:500]
    except Exception:                                              # noqa: BLE001
        pass
    code = int(getattr(exc, "code", 0) or 0)
    low = raw.lower()
    if code == 400 and ("unable to parse range" in low or "parse range" in low):
        return SourceError("bad_range",
                           "в книге нет листа «Очередь» — проверьте VS_WW2_RANGE")
    if code in (401, 403):
        return SourceError("forbidden",
                           "сервисный аккаунт не имеет доступа к книге: владелец не "
                           "поделился — или поделился другой книгой")
    if code == 404:
        return SourceError("not_found", "книги с таким id нет")
    if code == 429:
        return SourceError("rate", "Google просит сбавить частоту запросов")
    if code >= 500:
        return SourceError("server", f"Google ответил {code}")
    return SourceError("http", f"HTTP {code} {raw[:120]}")


def _ask(url: str, st: dict, fetch, now: float) -> dict:
    """Один запрос наружу. `fetch` подменяется в тестах — сети там нет вовсе."""
    if fetch is not None:
        body = fetch(url)
    else:
        body = _fetch(url, _token(st["sa_file"], now), st["timeout"])
    if not isinstance(body, dict):
        raise SourceError("bad_body", "ответ книги не разобрался")
    return body


def _read_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
    except FileNotFoundError:
        raise SourceError("no_file", f"снимка очереди нет: {path}")
    except (ValueError, OSError) as exc:
        raise SourceError("bad_file", f"снимок очереди не читается ({type(exc).__name__})")
    if isinstance(body, list):
        body = {"values": body}
    if not isinstance(body, dict):
        raise SourceError("bad_file", "в снимке ожидался список строк или {\"values\": …}")
    return body


# ----------------------------------------------------------------- чтение строк

_ROWS = {"key": "", "rows": [], "read_at": 0.0,
         "fail_kind": "", "fail_text": "", "retry_at": 0.0}

# Ручки кабинета синхронные, uvicorn исполняет их в пуле потоков: два одновременных
# показа экрана — это два обновления токена и два запроса в чужую книгу вместо
# обещанного «ровно одного». Замок отдельный на каждый кэш: чтение порогов не должно
# ждать чтения строк, а обновление токена нужно обоим.
_ROWS_LOCK = threading.RLock()
_POOL_LOCK = threading.RLock()
_TOKEN_LOCK = threading.RLock()

# Виды отказа, после которых повторять сразу бессмысленно и вредно: это не ответ
# книги, а «сейчас не получилось». Отказы-ответы (нет доступа, нет книги, нет листа)
# в паузу не попадают намеренно: владелец в этот момент чинит настройку и жмёт
# «обновить» — заставлять его ждать 30 с после каждой правки нельзя.
RETRY_KINDS = ("rate", "server", "timeout", "network")


def _cache_key(st: dict) -> str:
    # Путь к ключу входит в ключ кэша: сменили сервисный аккаунт — сменился и тот,
    # ЧЕЙ доступ мы показываем, и прочитанное прежним доступом больше не наше.
    return "|".join([st["source"], st["sheet_id"], st["range"], st["file"],
                     st["sa_file"]])


def _stale(now: float) -> float:
    return max(0.0, now - _ROWS["read_at"]) if _ROWS["read_at"] else 0.0


def read_rows(fetch=None, c=None, now=None, fresh: bool = False) -> dict:
    """Строки листа «Очередь».

    Возвращает `{rows, source, error, text, read_at, stale_sec, cached, need, url}`.
    `error` пустой — прочитали сейчас; иначе строки могут быть прошлыми, и `stale_sec`
    говорит, насколько. Молча подменять данные пустотой нельзя: «спросить не удалось» и
    «в очереди пусто» — разные состояния экрана, и путать их — значит однажды показать
    владельцу, что у него ничего не запланировано.

    Под замком целиком, вместе с запросом наружу: два одновременных показа экрана
    должны дать один запрос в чужую книгу, а не два. Ждать при этом второй поток
    будет не дольше `VS_WW2_TIMEOUT`.
    """
    with _ROWS_LOCK:
        return _read_rows(fetch, c, now, fresh)


def _read_rows(fetch, c, now, fresh: bool) -> dict:
    now = float(now or time.time())
    st = settings(c)
    key = _cache_key(st)
    if _ROWS["key"] != key:
        _ROWS.update({"key": key, "rows": [], "read_at": 0.0,
                      "fail_kind": "", "fail_text": "", "retry_at": 0.0})

    if st["source"] == SOURCE_OFF:
        # Здесь прошлые строки НЕ показываем: настройку сняли — значит смотреть на
        # чужой журнал мы больше не имеем права, и это состояние экрана отдельное.
        _ROWS.update({"rows": [], "read_at": 0.0,
                      "fail_kind": "", "fail_text": "", "retry_at": 0.0})
        return {"rows": [], "source": SOURCE_OFF, "error": NOT_CONFIGURED,
                "text": "Таблица ВВ2 не подключена: нет идентификатора книги и ключа "
                        "сервисного аккаунта", "need": list(NEED),
                "read_at": 0.0, "stale_sec": 0.0, "cached": False, "url": "",
                "retry_in": 0.0, "settings": shown_settings(st)}

    # Свежесть считаем по времени чтения, а не по наличию строк: пустая очередь —
    # это тоже ответ, и переспрашивать книгу на каждый показ экрана из-за неё незачем.
    if not fresh and _ROWS["read_at"] and now - _ROWS["read_at"] < st["ttl"]:
        return {"rows": list(_ROWS["rows"]), "source": st["source"], "error": "",
                "text": "", "need": [], "read_at": _ROWS["read_at"],
                "stale_sec": _stale(now), "cached": True, "url": "",
                "retry_in": 0.0, "settings": shown_settings(st)}

    # Отказ помним так же, как удачное чтение. Раньше не помнили вовсе: после 429
    # каждый показ экрана снова шёл наружу — код говорил владельцу «Google просит
    # сбавить частоту» и сам её не сбавлял. Пауза перекрывает и `fresh`: кнопка
    # «обновить» не должна быть способом продлить лимит или потерять доступ
    # сервисного аккаунта, выданный владельцем вручную.
    if _ROWS["retry_at"] and now < _ROWS["retry_at"]:
        return _failed(st, _ROWS["fail_kind"], _ROWS["fail_text"], now, "",
                       waiting=_ROWS["retry_at"] - now)

    url = ""
    try:
        if st["source"] == SOURCE_FILE:
            body = _read_file(st["file"])
        else:
            url = _url(st["sheet_id"], st["range"], RENDER_ROWS)
            body = _ask(url, st, fetch, now)
    except SourceError as exc:
        return _failed(st, exc.kind, exc.text, now, url)
    except (urllib.error.HTTPError,) as exc:
        e = _http_error(exc)
        return _failed(st, e.kind, e.text, now, url)
    except (TimeoutError, OSError, urllib.error.URLError) as exc:
        kind = "timeout" if _is_timeout(exc) else "network"
        text = (f"книга не ответила за {st['timeout']:g} с (VS_WW2_TIMEOUT)"
                if kind == "timeout" else f"до книги не достучались ({type(exc).__name__})")
        return _failed(st, kind, text, now, url)
    except Exception as exc:                                       # noqa: BLE001
        return _failed(st, "unknown", f"{type(exc).__name__}: {exc}", now, url)

    rows = parse_values(body.get("values") or [], now)
    _ROWS.update({"key": key, "rows": rows, "read_at": now,
                  "fail_kind": "", "fail_text": "", "retry_at": 0.0})
    return {"rows": list(rows), "source": st["source"], "error": "", "text": "",
            "need": [], "read_at": now, "stale_sec": 0.0, "cached": False,
            "url": url, "retry_in": 0.0, "settings": shown_settings(st)}


def _is_timeout(exc) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, TimeoutError) or "timed out" in str(reason or exc).lower()


def _pause_sec(st: dict, kind: str) -> float:
    """Сколько не переспрашивать источник после отказа этого вида."""
    # 429 — это прямая просьба провайдера сбавить частоту, поэтому пауза своя и
    # длиннее: повтор через полминуты лимит только продлит.
    return st["rate_ttl"] if kind == "rate" else st["fail_ttl"]


def _failed(st: dict, kind: str, text: str, now: float, url: str,
            waiting: float = 0.0) -> dict:
    """Отказ источника. Прошлые строки остаются на экране — с явным возрастом.

    `waiting` > 0 — мы наружу не ходили вовсе: отказ того же вида ещё в силе.
    """
    if kind in RETRY_KINDS and not waiting:
        _ROWS.update({"fail_kind": kind, "fail_text": text,
                      "retry_at": now + _pause_sec(st, kind)})
    rows = list(_ROWS["rows"])
    full = text
    if rows and _ROWS["read_at"]:
        when = time.strftime("%H:%M", time.localtime(_ROWS["read_at"]))
        full = f"{full}; показываю прочитанное в {when}"
    if waiting > 0:
        full = f"{full}; следующая попытка не раньше чем через {waiting:.0f} с"
    return {"rows": rows, "source": st["source"], "error": kind, "text": full,
            "need": list(NEED) if kind in (NOT_CONFIGURED, "no_key", "no_lib") else [],
            "read_at": _ROWS["read_at"], "stale_sec": _stale(now),
            "cached": bool(rows), "url": url,
            "retry_in": max(0.0, float(waiting)), "settings": shown_settings(st)}


def forget_cache() -> None:
    """Забыть прочитанное (смена настроек, тесты)."""
    _ROWS.update({"key": "", "rows": [], "read_at": 0.0,
                  "fail_kind": "", "fail_text": "", "retry_at": 0.0})
    _POOL.update({"key": "", "data": None, "read_at": 0.0, "good_until": 0.0,
                  "good": {}})
    _TOKEN.update({"file": "", "value": "", "until": 0.0})


# ------------------------------------------------- C5: правила отбора как данные

# Имя у владельца → (наш ключ, умолчание). Умолчания — ровно его (`ww2_pool.SETTINGS`),
# и они здесь ТОЛЬКО как значение первого запуска: правится это в книге, без релиза.
POOL_SETTINGS = (
    ("pool_min_len", "min_len", 20.0),        # минимальная длина оригинала, минут
    ("pool_mid", "mid", 0.5),                 # порог «средне», доля медианы
    ("pool_top", "top", 2.0),                 # порог «много», доля медианы
    ("pool_min_views", "min_views", 300000.0),
    ("pool_size", "size", 60.0),              # свободных в пуле канала
    ("pool_warmup", "warmup", 10.0),          # первых роликов ниже порога
)
FROM_SHEET = "таблица"
FROM_DEFAULT = "умолчание"
FROM_FILE = "снимок"
# Порог, который мы УЖЕ читали из книги, а сейчас прочитать не смогли. Это всё ещё
# правило владельца, просто постаревшее; подменять его захардкоженным умолчанием —
# значит от одной сетевой икоты тихо начать отбирать не по его правилам.
FROM_LAST = "прочитано раньше"

# Ни один порог прочитать не удалось, и сетевого отказа при этом не было (книга
# ответила, но именованных диапазонов в ней нет). Отдельное состояние: иначе
# вызывающий отличит «прочитали» от «не прочитали ничего» только разбором заметок.
NO_SETTINGS = "no_settings"

# `good` — последнее УДАЧНО прочитанное значение каждого порога: {ключ: (число, когда)}.
_POOL = {"key": "", "data": None, "read_at": 0.0, "good_until": 0.0, "good": {}}


def _pool_number(body, default: float):
    """Значение именованного диапазона. Закон отказа — дословно как у владельца."""
    values = (body or {}).get("values") or []
    v = values[0][0] if values and values[0] else ""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    try:
        # Из зеркала и из выгрузки то же число приезжает строкой — это не повод
        # потерять настройку владельца. Закон «положительное число» не меняется.
        f = float(str(v).replace(",", ".").replace(" ", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def read_pool_settings(fetch=None, c=None, now=None, fresh: bool = False,
                       clock=None) -> dict:
    """Шесть порогов отбора — из именованных диапазонов книги, а не числами в коде.

    У владельца `pool_min_len`, `pool_mid`, `pool_top`, `pool_min_views`, `pool_size`,
    `pool_warmup` живут именованными диапазонами и правятся без релиза. Мы читаем их
    так же и повторяем его поведение при отказе дословно: диапазона нет или не
    читается — берём умолчание и ГОВОРИМ об этом строкой, а не падаем и не молчим.
    Рядом с каждым значением — «таблица» или «умолчание»: человек должен видеть, по
    каким правилам сейчас отбирают, и откуда взялось число.

    Своего пула мы не строим: пять чисел из шести нам не нужны ни для одного решения,
    они показываются справочно. Копировать их в свои настройки нельзя — два места для
    одного числа разъедутся за неделю.

    Именованный диапазон читается тем же `values.get`, что и очередь: ни метаданных
    книги, ни списка вкладок это не требует (владелец берёт имена из
    `fetch_sheet_metadata` — нам туда нельзя, там же перечень листов с почтами).

    Отказ здесь НЕ подменяет правило владельца умолчанием: как и строки очереди,
    прочитанный раньше порог остаётся на экране с пометкой «прочитано в ЧЧ:ММ», и
    только если его никогда не читали — берётся число первого запуска. Неудача
    кэшируется коротко (`VS_WW2_FAIL_TTL_SEC`), а не на все десять минут: иначе одна
    сетевая икота десять минут держала бы нижнюю границу длины донора захардкоженной.

    `clock` — ход времени для общего бюджета (в тестах подменяется, сна нет).
    """
    with _POOL_LOCK:
        return _read_pool(fetch, c, now, fresh, clock)


def _read_pool(fetch, c, now, fresh: bool, clock=None) -> dict:
    now = float(now or time.time())
    clock = clock or time.monotonic
    st = settings(c)
    key = _cache_key(st)
    if _POOL["key"] != key:
        _POOL.update({"key": key, "data": None, "read_at": 0.0, "good_until": 0.0,
                      "good": {}})
    if not fresh and _POOL["data"] is not None and now < _POOL["good_until"]:
        out = dict(_POOL["data"])
        out["cached"] = True
        return out

    out = {k: d for _, k, d in POOL_SETTINGS}
    out["source"] = {k: FROM_DEFAULT for _, k, _d in POOL_SETTINGS}
    out["notes"] = []
    out["read_at"] = now
    out["cached"] = False

    if st["source"] == SOURCE_OFF:
        out["error"] = NOT_CONFIGURED
        out["notes"].append("таблица ВВ2 не подключена — показываю умолчания")
        return out
    out["error"] = ""

    if st["source"] == SOURCE_FILE:
        # Снимок — это дев-стенд без доступа к книге. Если в нём есть блок `pool`,
        # берём его, но помечаем «снимок»: это не живое значение из таблицы.
        try:
            body = _read_file(st["file"])
        except SourceError as exc:
            out["error"] = exc.kind
            out["notes"].append(exc.text + " — показываю умолчания")
            return out
        pool = body.get("pool") if isinstance(body.get("pool"), dict) else {}
        for name, k, default in POOL_SETTINGS:
            v = _pool_number({"values": [[pool.get(k, pool.get(name, ""))]]}, default)
            if v is None:
                out["notes"].append(f"пул: настройки {name} в снимке нет — беру {default:g}")
                continue
            out[k], out["source"][k] = v, FROM_FILE
            _POOL["good"][k] = (v, now)
        _POOL.update({"key": key, "data": dict(out), "read_at": now,
                      "good_until": now + st["settings_ttl"]})
        return out

    # Общий бюджет на все шесть имён. Раньше бюджета не было вовсе: недоступная книга
    # означала шесть таймаутов подряд, то есть один показ экрана держал поток uvicorn
    # 6 × VS_WW2_TIMEOUT секунд. Время меряем монотонными часами, а не `now`: `now` —
    # это отметка чтения (в тестах фиксированная), а не ход времени.
    started = clock()
    hard = ""          # вид «сейчас не получилось»: по нему длина паузы до повтора
    got = 0
    for name, k, default in POOL_SETTINGS:
        left = st["settings_budget"] - (clock() - started)
        if left <= 0:
            # Остаток имён не спрашиваем вовсе: книга уже не отвечает, а экран ждёт.
            _pool_fallback(out, name, k, default,
                           "общий бюджет времени на пороги исчерпан "
                           "(VS_WW2_SETTINGS_BUDGET_SEC)")
            hard = hard or "timeout"
            continue
        url = _url(st["sheet_id"], name, RENDER_NUMBERS)
        try:
            body = _ask(url, dict(st, timeout=min(st["timeout"], left)), fetch, now)
            v = _pool_number(body, default)
        except urllib.error.HTTPError as exc:
            e = _http_error(exc)
            # 400 «Unable to parse range» на ИМЕНИ означает не сломанный лист, а
            # отсутствие именованного диапазона — ровно случай зеркальной книги.
            v = None
            why = (f"именованного диапазона {name} в книге нет"
                   if e.kind == "bad_range" else e.text)
            hard = hard or (e.kind if e.kind in RETRY_KINDS else "")
        except SourceError as exc:
            v, why = None, exc.text
            hard = hard or (exc.kind if exc.kind in RETRY_KINDS else "")
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            v = None
            kind = "timeout" if _is_timeout(exc) else "network"
            hard = hard or kind
            why = (f"книга не ответила за {min(st['timeout'], left):g} с"
                   if kind == "timeout" else f"до книги не достучались ({type(exc).__name__})")
        except Exception as exc:                                   # noqa: BLE001
            v, why = None, f"{type(exc).__name__}"
        else:
            why = "нет такого именованного диапазона или он пуст"
        if v is None:
            _pool_fallback(out, name, k, default, why)
            continue
        out[k], out["source"][k] = v, FROM_SHEET
        _POOL["good"][k] = (v, now)
        got += 1

    if not got:
        # Ни одного порога. Умолчания на экране в этом случае — не «правила отбора»,
        # а заглушка, и вызывающий должен уметь отличить одно от другого по полю,
        # а не по разбору заметок.
        out["error"] = hard or NO_SETTINGS
        out["notes"].append("ни один порог из книги не прочитался")
    # Неудачу кэшируем коротко, удачу — на полный срок. «Диапазона нет» — это ответ
    # книги, а не неудача: у зеркала с одним IMPORTRANGE имён нет никогда, и
    # переспрашивать их каждые полминуты значит шесть запросов на каждый показ.
    pause = _pause_sec(st, hard) if hard else st["settings_ttl"]
    _POOL.update({"key": key, "data": dict(out), "read_at": now,
                  "good_until": now + pause})
    return out


def _pool_fallback(out: dict, name: str, k: str, default: float, why: str) -> None:
    """Порог не прочитался: сначала прошлое прочитанное, и только потом умолчание."""
    last = _POOL["good"].get(k)
    if last is not None:
        value, when_ts = last
        when = time.strftime("%H:%M", time.localtime(when_ts))
        out[k], out["source"][k] = value, FROM_LAST
        out["notes"].append(f"пул: настройка {name} не читается ({why}) — "
                            f"показываю прочитанное в {when}: {value:g}")
        return
    # Дословно поведение владельца: «настройка не читается — беру умолчание».
    out["notes"].append(f"пул: настройка {name} не читается ({why}) — беру {default:g}")


def min_len_minutes(c=None, fetch=None, saved: float = 0.0, default: float = 20.0):
    """Нижняя граница длины донора: таблица → наше сохранённое → константа.

    Единственное из шести чисел, которое у нас что-то решает. Порядок такой же, как у
    владельца: константа в коде — это умолчание первого запуска, а не правило.
    Возвращает `(значение, откуда)`; вызывающий показывает «откуда» рядом с числом.
    """
    pool = read_pool_settings(fetch=fetch, c=c)
    # FROM_LAST здесь наравне с «таблицей»: это то же число владельца, прочитанное
    # раньше. Иначе один таймаут опустил бы нижнюю границу длины донора с
    # владельческой до захардкоженной — и через проверку прошли бы доноры, за рерайт
    # и озвучку которых платят.
    if pool["source"].get("min_len") in (FROM_SHEET, FROM_FILE, FROM_LAST):
        return float(pool["min_len"]), pool["source"]["min_len"]
    if saved and saved > 0:
        return float(saved), "наша настройка"
    return float(default), FROM_DEFAULT


# --------------------------------------------- сопоставление с нашим выпуском

OURS = "ours"                  # строка очереди нашлась у нас в плане
MISSING = "missing"            # канал знаем, донора в плане нет
NO_CHANNEL = "no_channel"      # имя канала из листа не легло ни на один наш канал
NO_ID = "no_id"                # в «Донор» вписано название, ссылки нет
HAVE_TEXT = {OURS: "у нас есть", MISSING: "у нас нет",
             NO_CHANNEL: "канала нет у нас", NO_ID: "нет id донора"}

MAP_TABLE = "ww2_channel_map"
# Куда отложены связки, сделанные до того, как у таблицы появился арендатор.
OLD_MAP_TABLE = MAP_TABLE + "_bez_arendatora"

# Служебный просмотр владельца «вся база сразу». Пишется ЯВНО: сопоставление без
# арендатора не делается вовсе — см. `match`.
ALL_TENANTS = "*"


class MapError(Exception):
    """Ручная связка не принята (нет арендатора, нет канала, канал чужой)."""


def ensure_ww2_schema(c) -> None:
    """Ручные исправления связи «имя в листе → наш канал». Идемпотентно.

    Живёт отдельной ручкой, а не в общей схеме: таблица нужна только экрану ВВ2, и
    заводит её тот, кто первым до неё дошёл. Это ЕДИНСТВЕННАЯ запись во всём модуле —
    и она в НАШУ базу. В книгу мы не пишем ничего и никогда.

    Ключ — (арендатор, имя в листе), а не одно имя. Имя канала в чужом журнале
    глобально не уникально, и при едином ключе связка одного арендатора действовала
    бы на всех: строка его очереди приклеилась бы к каналу ДРУГОГО арендатора и
    показала бы рядом чужой план и чужой выпуск.
    """
    try:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({MAP_TABLE})").fetchall()]
    except sqlite3.Error:                                          # noqa: BLE001
        cols = []
    if cols and "tenant_id" not in cols:
        # Таблица со старым, глобальным ключом. Раздать её связки всем арендаторам
        # нельзя — это ровно та утечка, от которой мы уходим; приписать одному тоже
        # нельзя — кто их делал, в ней не записано. Поэтому старое откладываем под
        # другим именем: данные владельца целы, но не действуют, пока он не
        # переделает связки на экране (их единицы — это ручные исправления).
        c.execute(f"ALTER TABLE {MAP_TABLE} RENAME TO {OLD_MAP_TABLE}")
    c.execute(f"CREATE TABLE IF NOT EXISTS {MAP_TABLE}("
              "tenant_id TEXT NOT NULL, "
              "sheet_name TEXT NOT NULL, "        # имя канала из колонки B, как есть
              "channel_id INTEGER NOT NULL, "
              "updated REAL NOT NULL DEFAULT 0, "
              "PRIMARY KEY(tenant_id, sheet_name))")


def norm_name(name: str) -> str:
    """Имя канала для сравнения: регистр, двойные пробелы, `ё`/`е`.

    Связь по имени хрупка, и мы знаем это заранее: у владельца есть собственный
    механизм переименований, который правит колонку B задним числом. Пока он не даст
    стабильный ключ (код канала или UC), несопоставленные строки показываются громко,
    а не прячутся.
    """
    s = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    return s.replace("ё", "е")


def remember_channel(c, tenant_id: str, sheet_name: str, channel_id: int,
                     now=None) -> None:
    """Запомнить ручное исправление связи. Пишем в НАШУ базу, не в книгу.

    Арендатор обязателен и проверяется по самому каналу: связать строку чужого
    журнала можно только со СВОИМ каналом, иначе экран ВВ2 показал бы рядом с чужой
    строкой чужой план и чужой выпуск — и выглядело бы это как «наша строка».
    """
    tid = str(tenant_id or "").strip()
    if not tid or tid == ALL_TENANTS:
        raise MapError("связку делает арендатор, а не «вся база»")
    ensure_ww2_schema(c)
    own = c.execute("SELECT id FROM pub_channels WHERE id=? AND tenant_id=?",
                    (int(channel_id), tid)).fetchone()
    if not own:
        raise MapError("канала с таким id у этого арендатора нет")
    c.execute(f"INSERT INTO {MAP_TABLE}(tenant_id, sheet_name, channel_id, updated) "
              "VALUES(?,?,?,?) "
              "ON CONFLICT(tenant_id, sheet_name) DO UPDATE SET "
              "channel_id=excluded.channel_id, updated=excluded.updated",
              (tid, str(sheet_name or "").strip(), int(channel_id),
               float(now or time.time())))


def _channel_index(c, tenant_id):
    """{имя: id} + спорные имена + {id: имя} + ручные связки — в пределах арендатора.

    «Спорное имя» — это {имя: чем именно спорное}: текст уедет владельцу в строку, а
    не останется догадкой («два канала» и «две разные связки» — разные беды).

    Раньше фильтр по арендатору стоял ТОЛЬКО на `by_name`: имена каналов и ручные
    связки читались по всей базе. Имя из чужого журнала могло лечь на канал другого
    арендатора — вместе с его планом и выпуском.
    """
    where, args = ("", ())
    if tenant_id != ALL_TENANTS:
        where, args = (" WHERE tenant_id=?", (tenant_id,))
    by_name, dup, by_id = {}, {}, {}
    for cid, name in c.execute("SELECT id, name FROM pub_channels" + where,
                               args).fetchall():
        by_id[cid] = name
        k = norm_name(name)
        if not k:
            continue
        if k in by_name and by_name[k] != cid:
            # Два наших канала с одним именем: молча выбрать один из них — значит
            # однажды приклеить строку к чужому. Лечится ручной связкой.
            dup[k] = "у нас два канала с таким именем — свяжите вручную"
        by_name[k] = cid
    overrides, clash = {}, set()
    try:
        rows = c.execute(f"SELECT sheet_name, channel_id FROM {MAP_TABLE}" + where, args)
        for name, cid in rows:
            k = norm_name(name)
            if k in overrides and overrides[k] != int(cid):
                # Бывает только в служебном просмотре всей базы: одно имя листа
                # связано разными арендаторами с разными каналами. Выбрать молча —
                # та же утечка, поэтому связку не применяем вовсе.
                clash.add(k)
            overrides[k] = int(cid)
    except sqlite3.Error:
        pass                            # таблицы ещё нет — исправлений просто нет
    for k in clash:
        overrides.pop(k, None)
        dup[k] = "это имя связано вручную с каналами разных арендаторов"
    return by_name, dup, by_id, overrides


def match(c, rows, tenant_id=None) -> list:
    """Проставить каждой строке очереди, что о ней знает наш план.

    Ключ — (канал, `video_id`), и только он. Ссылка на один ролик приходит в разных
    написаниях, поэтому сравниваем не строки ссылок, а готовый `donor_video_id`
    из плана: он заполняется тем же `pub_ids.video_id`, что и колонка K таблицы.

    Дубли не схлопываем: две строки листа на один и тот же (канал, ролик) — это
    находка для владельца, а не мусор; таблица и сама пишет про них в колонке H.

    `tenant_id` обязателен: и каналы, и ручные связки, и план, и выпуск читаются
    только этого арендатора. Без него сопоставлять не с чем — строка чужого журнала
    приклеилась бы к каналу любого арендатора базы, показав рядом его план и выпуск.
    Служебный просмотр «вся база» существует, но пишется явно: `ALL_TENANTS`.
    """
    out = [dict(r) for r in rows or []]
    tid = str(tenant_id or "").strip()
    empty = {"channel_id": 0, "channel_name": "", "plan_item_id": 0, "plan_status": "",
             "release_id": "", "release_phase": "", "release_status": ""}
    if not tid:
        for r in out:
            r.update(empty)
            r.update({"have": "", "have_text": "",
                      "match_error": "арендатор не указан — сопоставлять чужой "
                                     "журнал не с чем"})
        return out
    try:
        by_name, dup, by_id, overrides = _channel_index(c, tid)
    except sqlite3.Error as exc:
        for r in out:
            r.update(empty)
            r.update({"have": "", "have_text": "",
                      "match_error": f"наш план не прочитался ({type(exc).__name__})"})
        return out

    seen = {}
    for r in out:
        r.update(empty)
        r["match_error"] = ""
        key = norm_name(r.get("channel_sheet"))
        cid = overrides.get(key) or (None if key in dup else by_name.get(key))
        if cid and cid not in by_id:
            # Ручная связка указывает на канал, которого у этого арендатора нет:
            # его удалили, или связка досталась от другого арендатора. Раньше такая
            # строка молча становилась «у нас нет» — с нашим id и пустым именем, то
            # есть выглядела сопоставленной. Это против правила модуля «несопо-
            # ставленные строки показываются громко».
            r["match_error"] = "связка указывает на канал, которого у нас больше нет"
            cid = None
        r["channel_id"] = cid or 0
        r["channel_name"] = by_id.get(cid, "") if cid else ""
        if not cid:
            r["have"] = NO_CHANNEL
            if key in dup:
                r["match_error"] = dup[key]
            r["have_text"] = HAVE_TEXT[NO_CHANNEL]
            continue
        vid = r.get("video_id") or ""
        if not vid:
            r["have"] = NO_ID
            r["have_text"] = HAVE_TEXT[NO_ID]
            continue
        pair = (cid, vid)
        if pair not in seen:
            seen[pair] = _look_up(c, tid, cid, vid)
        r.update(seen[pair])
    return out


def _look_up(c, tenant_id: str, channel_id: int, vid: str) -> dict:
    # Ключ — (канал, video_id) И арендатор: план и выпуск читаются только свои.
    where, args = "", [channel_id, vid]
    if tenant_id != ALL_TENANTS:
        where, args = " AND tenant_id=?", [channel_id, vid, tenant_id]
    row = c.execute(
        "SELECT id, status FROM pub_plan_items "
        "WHERE channel_id=? AND donor_video_id=?" + where +
        " AND status!='cancelled' ORDER BY id DESC LIMIT 1", args).fetchone()
    if not row:
        return {"have": MISSING, "have_text": HAVE_TEXT[MISSING]}
    found = {"have": OURS, "have_text": HAVE_TEXT[OURS],
             "plan_item_id": int(row[0]), "plan_status": str(row[1] or "")}
    try:
        rel_args = [row[0]] if tenant_id == ALL_TENANTS else [row[0], tenant_id]
        rel = c.execute(
            "SELECT release_id, phase, status FROM pub_releases "
            "WHERE plan_item_id=?" + where + " ORDER BY id DESC LIMIT 1",
            rel_args).fetchone()
    except sqlite3.Error:
        rel = None                     # выпусков ещё нет — строка плана и так найдена
    if rel:
        found.update({"release_id": str(rel[0] or ""), "release_phase": str(rel[1] or ""),
                      "release_status": str(rel[2] or "")})
    return found


def summary(rows) -> dict:
    """Четыре числа, которые отвечают на вопрос «где разошлись два журнала»."""
    out = {"rows": 0, OURS: 0, MISSING: 0, NO_CHANNEL: 0, NO_ID: 0, "unknown": 0}
    for r in rows or []:
        out["rows"] += 1
        k = r.get("have") or "unknown"
        out[k] = out.get(k, 0) + 1
    return out
