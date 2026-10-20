#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал платных вызовов провайдеров — уровнем ниже журнала выпусков.

Зачем отдельная таблица. Существующий журнал (`pub_release_jobs`) хранит строку на ЭТАП:
`UNIQUE(release_id, service, kind)`. Внутри одного этапа «картинки» при медиане 155 кадров
происходит 155 отдельных платных вызовов, про которые этот журнал не знает ничего.

Что защищает их сегодня — пять независимых файловых кэшей внутри движков (по подсчёту
разбора: картинки 54 места, рендер 18, Lumean 7, Voicer 23, VGC 2) плюс правило «повтор
только на явный 429» в озвучке. Файловый кэш закрывает случай «файл уже скачан» и не
закрывает окно между «провайдер принял заказ» и «файл скачан»: рестарт в этот момент
платит второй раз. Симптом виден в живом коде — 03.09 все таймауты картинок подняты ×10
с комментарием, что оплаченные кадры снимались собственным таймером.

Правило №4 ТЗ: платный вызов повторяется ТОЛЬКО когда доказано, что он не состоялся.
Доказательство — строка в этой таблице.

Порядок работы (важен именно такой):

    1. reserve()          — ДО обращения к провайдеру. Строка появляется раньше денег.
    2. mark_ordered()     — как только известен идентификатор заказа.
    3. mark_delivered()   — когда результат лежит на диске и открывается.

Если между 1 и 2 процесс умер, при следующем reserve() вернётся состояние `reserved`:
это НЕ разрешение повторить, а требование сходить к провайдеру и выяснить судьбу заказа.
Единственное состояние, разрешающее новую попытку, — `failed`.

Второе правило, младше первого: **начатый заказ продолжается только тем ключом, которым
начат**. В строке закрепляется отпечаток ключа (`credential_id` — sha256 вендора и
значения, сам ключ сюда не попадает никогда). Для чужого ключа заказа просто «нет», а
«нет» неотличимо от «не создавался» — и следующим шагом стал бы второй платёж за тот же
кадр. Проверка: `credential_conflict()` / `assert_same_credential()`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import re
import sqlite3
import time

# Состояния строки. Порядок отражает жизненный цикл.
NEW = "new"                 # строки не было, вызов можно делать
RESERVED = "reserved"       # заявка записана, судьба заказа неизвестна
ORDERED = "ordered"         # заказ у провайдера создан — деньги, возможно, потрачены
DELIVERED = "delivered"     # результат получен и лежит на диске
FAILED = "failed"           # провайдер отказал ДО создания заказа — повтор безопасен

# Что делать вызывающему, получив существующую строку.
SKIP = "skip"               # ничего не делать, результат уже есть
VERIFY = "verify"           # спросить провайдера про существующий заказ, НЕ создавать новый
RETRY = "retry"             # прошлая попытка не дошла до заказа — можно пробовать снова

_ACTION = {DELIVERED: SKIP, ORDERED: VERIFY, RESERVED: VERIFY, FAILED: RETRY}


# ── Запись под замком SQLite ─────────────────────────────────────────────────
# 19.10.2026. За сутки в журнал не легли 23 отметки «результат получен» и 3 «заказ
# создан»: SQLite ответил `database is locked` несмотря на `busy_timeout=30000`.
# Это не зависание базы, а конкуренция на запись — две пачки, 10:44 и 14:55–15:19,
# как раз когда планировщик раздавал задачи, а движки закрывали вызовы.
#
# Для ЭТОГО журнала цена отказа особая. Деньги за вызов уже потрачены, а строка
# осталась в `ordered`: следующий проход увидит её и пойдёт ВЫЯСНЯТЬ судьбу заказа
# (`VERIFY`) вместо того, чтобы взять готовый файл с диска. В худшем случае — платный
# заказ заново. Поэтому отметку о потраченных деньгах повторяем, пока база не примет.
#
# Повтор здесь безопасен, и это надо сказать прямо: `database is locked` означает, что
# транзакция НЕ состоялась — ни одной строки не записано. Мы повторяем не платный вызов,
# а отметку о нём; правило «повтор оплачиваемого create запрещён» этим не нарушается.
_LOCKED_WORDS = ("database is locked", "database table is locked", "database is busy")


def is_locked(exc: BaseException) -> bool:
    """База была занята другим писателем (а не сломана и не испорчена)."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return any(word in text for word in _LOCKED_WORDS)


def _pause(attempt: int, base: float) -> float:
    """Пауза растёт с попытками, но со случайной добавкой.

    Без неё два процесса, получившие отказ одновременно, вернутся тоже одновременно и
    снова столкнутся — расходясь по времени, они расходятся и по замку.
    """
    return base * attempt + random.random() * base


def write_retry(open_conn, work, *, tries: int = 5, base: float = 0.4,
                label: str = "", log=None):
    """Выполнить запись целиком, повторив её, если база была занята.

    Повторяем ВМЕСТЕ с открытием соединения: брошенная транзакция держит свой же замок,
    и попытка номер два на том же соединении спорила бы сама с собой.

    Что НЕ повторяем: всё, что не `database is locked`. Отказ прав, чужой проект,
    испорченная строка — это ответы, а не помехи, и повторять их бессмысленно.
    """
    last = None
    for attempt in range(1, max(1, tries) + 1):
        c = open_conn()
        try:
            out = work(c)
            c.commit()
            if attempt > 1 and log:
                log(f"{label or 'запись'}: прошла с {attempt}-й попытки")
            return out
        except sqlite3.OperationalError as exc:
            try:
                c.rollback()
            except Exception:                               # noqa: BLE001
                pass
            if not is_locked(exc) or attempt >= tries:
                raise
            last = exc
            if log:
                log(f"{label or 'запись'}: база занята, попытка {attempt} из {tries}")
            time.sleep(_pause(attempt, base))
        finally:
            try:
                c.close()
            except Exception:                               # noqa: BLE001
                pass
    raise last                                              # pragma: no cover


def retry_here(c: sqlite3.Connection, work, *, tries: int = 5, base: float = 0.4,
               label: str = "", log=None):
    """То же, но на ЧУЖОМ соединении: повторяем работу и commit, не переоткрывая связь.

    Нужна там, где соединение принадлежит вызывающему (`pub_services._close_text_call`):
    переоткрыть его нельзя, а терять отметку о потраченных деньгах нельзя тем более.

    Почему повтор на том же соединении безопасен. `database is locked` означает, что НИ
    ОДНА наша запись не легла: кто первым записал строку, тот и держит замок записи, и
    второй writer не получает его вовсе. Значит, повторять нечего бояться — повторяется
    работа, которой в базе нет. Отката здесь поэтому нет: он выбросил бы чужую работу,
    сделанную в той же транзакции до нас.
    """
    last = None
    for attempt in range(1, max(1, tries) + 1):
        try:
            out = work(c)
            c.commit()
            if attempt > 1 and log:
                log(f"{label or 'запись'}: прошла с {attempt}-й попытки")
            return out
        except sqlite3.OperationalError as exc:
            if not is_locked(exc) or attempt >= tries:
                raise
            last = exc
            if log:
                log(f"{label or 'запись'}: база занята, попытка {attempt} из {tries}")
            time.sleep(_pause(attempt, base))
    raise last                                              # pragma: no cover


def ensure_calls_schema(c: sqlite3.Connection) -> None:
    """Создать таблицу журнала. Идемпотентно."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS provider_calls("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id TEXT NOT NULL, "
        # Проект, а не выпуск: выпуск есть только у автоконвейера, проект — всегда.
        "project_id TEXT NOT NULL, "
        "release_id TEXT NOT NULL DEFAULT '', "
        "kind TEXT NOT NULL, "              # images | clip | tts | cover
        "unit TEXT NOT NULL, "              # номер сцены, кусок текста, язык обложки
        "fingerprint TEXT NOT NULL, "       # хэш входа: модель + промпт + параметры
        "provider TEXT NOT NULL DEFAULT '', "
        "model TEXT NOT NULL DEFAULT '', "
        "remote_order_id TEXT NOT NULL DEFAULT '', "
        # Отпечаток КЛЮЧА, которым начат вызов (sha256 вендора и значения, см.
        # shared/credential_budget.credential_identity). Не ключ и не его кусок:
        # по отпечатку нельзя восстановить секрет, но можно ответить на два вопроса,
        # на которые журнал до сих пор ответить не мог, — «сколько этот ключ уже
        # съел» и «тем ли ключом мы продолжаем начатый заказ».
        "credential_id TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL, "
        "result_path TEXT NOT NULL DEFAULT '', "
        "cost REAL, "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "error TEXT NOT NULL DEFAULT '', "
        "created REAL NOT NULL, updated REAL NOT NULL, "
        # Ядро защиты: тот же вход в том же проекте нельзя заказать дважды.
        "UNIQUE(project_id, kind, unit, fingerprint))"
    )
    # Экран выпуска и подсчёт стоимости ходят по проекту; журнал — по времени.
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_project "
              "ON provider_calls(project_id, kind, updated)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_release "
              "ON provider_calls(release_id, status)")
    # Поиск незавершённых при восстановлении после рестарта.
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_open "
              "ON provider_calls(status, updated)")
    # Миграция баз, заведённых до отпечатка ключа. Пустая строка на старых строках —
    # честный признак «ключ неизвестен»: правило «продолжать тем же ключом» на них не
    # применяется, потому что применять его не к чему (см. credential_conflict).
    for _col, _typ in [("credential_id", "TEXT NOT NULL DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE provider_calls ADD COLUMN {_col} {_typ}")
        except sqlite3.OperationalError:
            pass                        # колонка уже есть — это и есть «идемпотентно»
    ensure_quota_schema(c)


def fingerprint(**parts) -> str:
    """Отпечаток входа вызова.

    Всё, что влияет на РЕЗУЛЬТАТ и на ЦЕНУ, должно попасть сюда: модель, промпт,
    seed, соотношение сторон, референсы. Что не влияет — не должно, иначе один и тот же
    кадр будет считаться разным и оплачиваться заново. Пустые значения отбрасываются,
    чтобы добавление необязательного параметра не обесценивало старые строки.
    """
    clean = {k: v for k, v in parts.items() if v not in (None, "", [], {})}
    blob = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── Отпечаток ключа задачи ───────────────────────────────────────────────────
# Строку журнала для движка заводит HTTP-ручка (`POST /api/calls/reserve`), а движок
# про отпечаток своего ключа не знает и знать не должен: ключ ему выдал кабинет, и
# спрашивать у движка, чем он работает, — значит верить на слово тому, кого мы как раз
# и страхуем. Поэтому отпечаток отдаёт кабинет по НОМЕРУ ЗАДАЧИ: он один решает, какой
# ключ ушёл воркеру, и он же держит это в памяти, пока задача жива.
_JOB_CREDENTIAL_RESOLVER = None


def set_job_credential_resolver(fn) -> None:
    """Подключить поиск отпечатка ключа по номеру задачи. None — снять."""
    global _JOB_CREDENTIAL_RESOLVER
    _JOB_CREDENTIAL_RESOLVER = fn


def job_credential(job_id) -> str:
    """Отпечаток ключа, выданного этой задаче. Пусто — неизвестен, и это не ошибка."""
    fn = _JOB_CREDENTIAL_RESOLVER
    if fn is None:
        return ""
    try:
        return str(fn(job_id) or "")
    except Exception:                                              # noqa: BLE001
        return ""


class CredentialMismatch(RuntimeError):
    """Начатый заказ пытаются продолжить ДРУГИМ ключом."""


def _row_value(row, column, default=""):
    """Значение колонки, которой в этой строке может и не быть.

    Строку могли прочитать из базы, ещё не прошедшей миграцию, — падать на этом
    нельзя: журнал решает, можно ли повторять платный вызов, и он обязан отвечать
    даже на неполных данных.
    """
    try:
        return row[column]
    except (IndexError, KeyError):
        return default


def credential_conflict(row, credential_id: str) -> bool:
    """True — этим ключом продолжать нельзя.

    Правило одно: заказ, начатый ключом A, продолжается только ключом A. Причина не
    в аккуратности, а в деньгах. Идентификатор заказа выдан аккаунтом ключа A; ключ B
    его не видит, и вопрос «что с моим заказом?» вернёт «нет такого» — то есть выглядеть
    это будет как «заказа не было», а следующим шагом станет ВТОРОЙ оплаченный заказ
    за тот же кадр. Пустой отпечаток с любой стороны — не конфликт: на строках, заведённых
    до этой колонки, ключ неизвестен, и придумывать за них нельзя.
    """
    was = str(_row_value(row, "credential_id") or "")
    now_id = str(credential_id or "")
    return bool(was and now_id and was != now_id)


def assert_same_credential(row, credential_id: str) -> None:
    """Жёсткая форма того же правила для вызывающего, который идёт ДОПЛАЧИВАТЬ."""
    if credential_conflict(row, credential_id):
        raise CredentialMismatch(
            "заказ начат другим ключом (…%s), продолжать этим (…%s) нельзя — "
            "нужен ключ с тем же отпечатком либо разбор руками"
            % (str(_row_value(row, "credential_id"))[:12], str(credential_id)[:12]))


def reserve(c: sqlite3.Connection, *, tenant_id: str, project_id: str, kind: str,
            unit: str, fp: str, provider: str = "", model: str = "",
            release_id: str = "", credential_id: str = "",
            now: float | None = None) -> dict:
    """Заявить вызов ДО обращения к провайдеру.

    Возвращает {"action": …, "status": …, "call_id": …, "row": …}:

      action=new     строки не было — вызов делать можно
      action=skip    результат уже получен — вызов делать НЕЛЬЗЯ, взять result_path
      action=verify  заказ уже создавался (или судьба неизвестна) — спросить провайдера
                     про remote_order_id, НЕ создавать второй
      action=retry   прошлая попытка отвалилась до создания заказа — можно пробовать

    Гонка двух воркеров разрешается уникальным индексом: проигравший получит `verify`,
    а не вторую заявку.

    `credential_id` — отпечаток ключа, которым вызов делается СЕЙЧАС (не сам ключ).
    В ответе к нему добавляются два поля:

      credential_id         отпечаток, закреплённый за строкой
      credential_mismatch   True — строка начата ДРУГИМ ключом

    При `credential_mismatch` действие остаётся `verify` (никогда не `new`): второй
    оплаченный заказ за тот же кадр хуже любой другой ошибки, поэтому выбор между
    «спросить» и «заплатить» всегда решается в пользу «спросить». Продолжать заказ
    этим ключом при этом НЕЛЬЗЯ — см. assert_same_credential.
    """
    now = time.time() if now is None else now
    credential_id = str(credential_id or "")
    cur = c.execute(
        "INSERT OR IGNORE INTO provider_calls(tenant_id,project_id,release_id,kind,unit,"
        "fingerprint,provider,model,credential_id,status,attempts,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (tenant_id, project_id, release_id, kind, unit, fp, provider, model,
         credential_id, RESERVED, now, now))
    row = c.execute(
        "SELECT * FROM provider_calls WHERE project_id=? AND kind=? AND unit=? "
        "AND fingerprint=?", (project_id, kind, unit, fp)).fetchone()
    if cur.rowcount == 1:
        return {"action": "new", "status": RESERVED, "call_id": row["id"], "row": row,
                "credential_id": credential_id, "credential_mismatch": False}

    status = row["status"]
    action = _ACTION.get(status, VERIFY)
    mismatch = credential_conflict(row, credential_id)
    if action == RETRY:
        # Прошлая попытка не дошла до заказа — открываем строку заново, считая попытку.
        # Заказа не было, денег не было, значит и ключ не закреплён: новая попытка
        # вправе пойти другим ключом, и отпечаток перезаписывается. Пустой новый
        # отпечаток старый НЕ стирает: «не сказали, каким ключом» — не то же самое,
        # что «ключ сменился».
        c.execute("UPDATE provider_calls SET status=?, error='', attempts=attempts+1, "
                  "credential_id=COALESCE(NULLIF(?,''),credential_id), "
                  "updated=? WHERE id=?", (RESERVED, credential_id, now, row["id"]))
        row = c.execute("SELECT * FROM provider_calls WHERE id=?", (row["id"],)).fetchone()
        return {"action": "new", "status": RESERVED, "call_id": row["id"], "row": row,
                "credential_id": str(_row_value(row, "credential_id") or ""),
                "credential_mismatch": False}
    return {"action": action, "status": status, "call_id": row["id"], "row": row,
            "credential_id": str(_row_value(row, "credential_id") or ""),
            "credential_mismatch": mismatch}


def mark_ordered(c: sqlite3.Connection, call_id: int, remote_order_id: str,
                 credential_id: str = "", now: float | None = None) -> None:
    """Заказ создан. С этого момента повтор запрещён — только проверка существующего.

    Здесь же за строкой закрепляется ключ. Закрепление проверяется, а не просто
    записывается: попытка привязать к начатому заказу ВТОРОЙ отпечаток означает, что
    выше по стеку потеряли связь «этот заказ — этого ключа», и дальше такая строка
    разрешила бы «продолжить» заказ в чужом аккаунте. Это ошибка программиста, и она
    должна быть слышна сразу, а не превратиться в лишний платёж через неделю.

    ВНИМАНИЕ ТОМУ, КТО ПОДКЛЮЧИТ `credential_id` К ВЫЗЫВАЮЩИМ. Сегодня ни один из них
    отпечаток не передаёт (pub_calls_api.py:91, pub_services.py:309), поэтому проверка
    ниже не срабатывает никогда. Как только отпечаток появится, ручку
    `POST /api/calls/{id}/ordered` придётся чинить вместе с ним: она зовёт mark_ordered
    СРАЗУ ПОСЛЕ создания ПЛАТНОГО заказа, и необработанное исключение уедет движку как
    500 уже после списания денег. Ручка обязана поймать CredentialMismatch и ответить
    словами «заказ начат другим ключом», оставив строку в `reserved`: `failed` —
    единственное состояние, разрешающее ВТОРОЙ платный заказ за тот же кадр.
    """
    now = time.time() if now is None else now
    credential_id = str(credential_id or "")
    if credential_id:
        row = c.execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
        if row is not None:
            assert_same_credential(row, credential_id)
    c.execute("UPDATE provider_calls SET status=?, remote_order_id=?, "
              "credential_id=COALESCE(NULLIF(?,''),credential_id), updated=? "
              "WHERE id=?", (ORDERED, str(remote_order_id or ""), credential_id,
                             now, call_id))


def mark_delivered(c: sqlite3.Connection, call_id: int, result_path: str,
                   cost: float | None = None, now: float | None = None) -> None:
    """Результат получен. `result_path` обязателен: «готово» без файла — не готово."""
    if not result_path:
        raise ValueError("mark_delivered без result_path: «готово» доказывается файлом")
    now = time.time() if now is None else now
    c.execute("UPDATE provider_calls SET status=?, result_path=?, cost=?, error='', "
              "updated=? WHERE id=?", (DELIVERED, result_path, cost, now, call_id))


# ── Редактура секретов на записи в колонку error ─────────────────────────────
# Текст отказа провайдера приезжает сюда ДВУМЯ путями: из кабинета (pub_services) и по
# HTTP от движка, который может работать на другой машине (POST /api/calls/{id}/failed).
# В ответе провайдера бывает наш же ключ или подписанный URL, а редактор кабинета
# (_redact_worker_output) стоит только на стриме логов — второй путь писал в SQLite как
# есть. Ставим редактуру в ЕДИНСТВЕННОЕ место записи: так её нельзя обойти, заведя
# третий путь. Сам журнал про секреты кабинета не знает и знать не должен — редактор
# подкладывает тот, кто их знает (server/main.py).
#
# НО опираться на то, что кабинет импортирован, нельзя. `set_error_redactor` вызывается
# побочным эффектом импорта server/main.py; при голом `import pub_calls` (отдельный
# процесс обслуживания, будущий сервис, тест) редактора не было бы вовсе, и сырой ответ
# провайдера лёг бы в SQLite — ровно тот сценарий, который эта правка закрывает. Поэтому
# ниже стоит СОБСТВЕННЫЙ консервативный редактор по образцу строки, а подключаемый —
# уточнение поверх него (он знает конкретные значения ключей и вырезает их точно).
_ERROR_REDACTOR = None

# Что режем без всякого знания о ключах: `sk-…`-подобные токены, значение после слов
# вроде api key/token/authorization и подпись в query подписанного URL. Список нарочно
# узкий: редактор по образцу обязан вырезать секрет, но не имеет права съесть причину
# отказа — по ней решают, можно ли повторять платный вызов.
_SECRET_LIKE_RE = re.compile(
    r"(?:sk|rk|pk|api)-[A-Za-z0-9_\-]{12,}"
    r"|(?:api[-_ ]?key|apikey|access[-_ ]?token|refresh[-_ ]?token|authorization|"
    r"bearer|x-api-key)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{12,}"
    r"|[?&](?:x-amz-signature|signature|sig|token|apikey|api_key|key)=[^&\s\"']{8,}",
    re.I)


def set_error_redactor(fn) -> None:
    """Подключить редактуру секретов для колонки error. None — снять."""
    global _ERROR_REDACTOR
    _ERROR_REDACTOR = fn


def default_redactor(text: str) -> str:
    """Редактура по образцу: работает всегда, даже когда кабинет не импортирован."""
    return _SECRET_LIKE_RE.sub("[REDACTED]", str(text or ""))


def _redacted(error: str) -> str:
    text = str(error or "")
    if not text:
        return text
    fn = _ERROR_REDACTOR
    if fn is None:
        return default_redactor(text)
    try:
        # Сначала точная редактура (знает значения ключей), потом сетка по образцу:
        # ключ ПАРТНЁРА, заведённый после последнего обновления кэша секретов, точной
        # редактуре ещё неизвестен, а на образец он похож.
        return default_redactor(str(fn(text)))
    except Exception:                                          # noqa: BLE001
        # Редактор сломался — сырой текст в базу НЕ кладём. Строка журнала ценна сама
        # по себе (она решает, можно ли повторять вызов), а нередактированный текст
        # может нести ключ, и отсюда он уйдёт на экран и в выгрузки.
        return "текст отказа скрыт: редактура секретов не отработала"


def mark_failed(c: sqlite3.Connection, call_id: int, error: str,
                ordered: bool = False, now: float | None = None,
                lost: bool = False) -> None:
    """Вызов не удался. Три случая, и они различаются судьбой денег.

    `ordered=False` — провайдер отказал ДО создания заказа (документированный 429,
    отказ валидации): денег нет, повтор безопасен, строка уходит в `failed`.

    `ordered=True` — заказ создан, но результат не получен: обрыв, таймаут, 5xx.
    Это НЕОДНОЗНАЧНО, и строка остаётся в `ordered`, чтобы следующая попытка пошла
    проверять заказ, а не создавать второй. Ровно та разница, из-за которой в озвучке
    повтор разрешён только на явный 429.

    `lost=True` — заказ создан, ДЕНЬГИ ПОТРАЧЕНЫ, и провайдер больше не знает про этот
    заказ: на вопрос о нём отвечает 404. Строка уходит в `failed`, то есть повтор
    разрешается — и это осознанный выбор, а не упрощение.

    Почему так, на примере 03.10.2026. Два кадра из 609 висели в `ordered` 14 и 17
    часов; провайдер чистит старые операции, и на оба заказа отвечал 404. Прежняя
    развилка считала 404 неоднозначностью, поэтому каждый прогон шёл спрашивать и
    получал тот же 404 — вечный круг. А рядом лежали 607 ОПЛАЧЕННЫХ кадров, которые
    из-за одного недостающего не проходили контракт квитанций и не ехали в рендер.
    То есть прежнее правило берегло один потерянный заказ ценой шестисот полученных.

    Отличие от `ordered=False` именно в деньгах, и его нельзя стирать: там повтор
    бесплатен, здесь первый заказ уже оплачен и невозвратен. Поэтому и слово отдельное:
    кто читает журнал, должен видеть разницу между «не платили» и «заплатили впустую».
    """
    now = time.time() if now is None else now
    # Режем ПОСЛЕ редактуры: «[REDACTED]» длиннее ключа, и обрезка до неё оставила бы
    # в базе хвост секрета. Своей обрезкой мы, впрочем, распоряжаемся только здесь:
    # по дороге сюда текст режут ещё четверо (воркер — detail[:300], call_journal —
    # [:500], pub_calls_api — [:500], pub_services — [:400]), и ключ, попавший на
    # границу ЛЮБОЙ из них, приезжает огрызком, которого точная редактура по значению
    # уже не найдёт. Поэтому редактор кабинета вырезает и длинные НАЧАЛА ключа
    # (main._cut_secret_prefixes), а здешняя сетка по образцу ловит то, чего он не знал.
    text = _redacted(error)[:500]
    if ordered and not lost:
        c.execute("UPDATE provider_calls SET error=?, updated=? WHERE id=?",
                  (text, now, call_id))
        return
    c.execute("UPDATE provider_calls SET status=?, error=?, updated=? WHERE id=?",
              (FAILED, text, now, call_id))


def open_calls(c: sqlite3.Connection, project_id: str) -> list:
    """Вызовы проекта, судьба которых не выяснена. Это и есть список «сходить спросить»."""
    return c.execute(
        "SELECT * FROM provider_calls WHERE project_id=? AND status IN (?,?) "
        "ORDER BY id", (project_id, RESERVED, ORDERED)).fetchall()


def project_summary(c: sqlite3.Connection, project_id: str) -> dict:
    """Сводка по проекту: сколько вызовов в каком состоянии и сколько потрачено.

    Отдаёт то, чего просит ТЗ §12 — «до запуска видно примерную стоимость»: колонка
    `cost` заполняется по факту, и сумма по проекту становится наблюдаемой величиной,
    а не оценкой.
    """
    out = {"by_status": {}, "calls": 0, "cost": 0.0}
    for r in c.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(cost),0) c FROM provider_calls "
            "WHERE project_id=? GROUP BY status", (project_id,)):
        out["by_status"][r["status"]] = r["n"]
        out["calls"] += r["n"]
        out["cost"] += float(r["c"] or 0)
    return out


# ──────────────────── квота YouTube Data API как общий ресурс ────────────────────
#
# Деньги — не единственный исчерпаемый ресурс вызова. Ключ YouTube Data API у нас ОДИН
# на всё: поздняя проверка выхода (`pub_verify`) и ночной обход конкурентов таблицы ВВ2
# берут единицы из одного суточного кошелька, и никто из них не знает про другого.
#
# 18.09 в 01:00 это стоило ночи: прогон пула упал на первом же 403 по квоте, весь
# остаток конкурентов получил «не обработан этой ночью» (`ww2_pool.py:739-743`),
# догоняли руками в 10:09. У нас тот же 403 был неотличим от любой другой ошибки —
# то есть выглядел как проблема ролика, хотя ролик тут ни при чём.
#
# Поэтому квота учитывается здесь, рядом с платными вызовами, по тем же правилам:
# строка расхода на потребителя, суточный бюджет и честное ожидание сброса.
#
# Таблица заводится в этом модуле, а не в `pub_schema`, по той же причине, что и
# `provider_calls`: это учёт ОДНОГО вызова, а не выпуска, и живёт он вместе со своей
# логикой. `ensure_calls_schema` зовут при старте (`main.py:2128`), так что таблица
# появляется сама, без отдельной миграции.

QUOTA_YT = "youtube"                 # имя кошелька: один ключ — один кошелёк
QUOTA_WAIT_KEY = "api_quota_wait"    # admin_state: активное ожидание сброса

# Google считает не запросы, а «единицы», и цены очень разные. По коду мы сегодня
# делаем ровно один вид вызова — `videos.list` (`pub_verify.API`, part=status,snippet),
# это 1 единица. Ночной обход ВВ2 тем же ключом делает `channels.list` (1),
# `playlistItems.list` (1) и `videos.list` (1) — поиска там нет.
# `search.list` внесён заранее и стоит 100: один необдуманный поиск съедает сто
# проверок выхода, и увидеть это надо в тот же день, а не в конце суток.
QUOTA_COSTS = {
    "videos.list": 1,
    "channels.list": 1,
    "playlistItems.list": 1,
    "captions.list": 50,
    "search.list": 100,
}

# Суточный бюджет. 10 000 единиц — это то, что Google даёт проекту по умолчанию,
# без заявки на расширение. Настройкой, а не константой, потому что бюджет — свойство
# ЧУЖОГО проекта в Google Cloud: владелец может попросить больше (или получить меньше
# после аудита), и тогда цифру меняют в окружении, а не в релизе.
YT_DAILY_UNITS = int(float(os.environ.get("VS_YT_QUOTA_UNITS") or 10000))

# Сколько ждать после «слишком часто» (rateLimitExceeded). Это НЕ суточная квота:
# короткий предел на частоту, он отпускает сам через минуты. Ждать до полуночи из-за
# него — потерять день зря, поэтому пауза своя и короткая.
YT_RATE_WAIT_SEC = int(float(os.environ.get("VS_YT_RATE_WAIT_SEC") or 300))

# Google сбрасывает суточную квоту в ПОЛНОЧЬ ПО ТИХООКЕАНСКОМУ ВРЕМЕНИ (Pacific Time),
# а не в полночь UTC и не в нашу. Зона переходит на летнее время (PST=UTC−8, PDT=UTC−7),
# поэтому считаем через zoneinfo, а не фиксированным сдвигом: фиксированный сдвиг дважды
# в год ошибался бы на час, и мы бы либо ждали лишний час впустую, либо начинали тратить
# до сброса и снова получали 403.
QUOTA_TZ = "America/Los_Angeles"


def _quota_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(QUOTA_TZ)
    except Exception:                                              # noqa: BLE001
        # Без базы зон (урезанный контейнер) берём зимний сдвиг. Хуже, чем zoneinfo:
        # летом сутки съедут на час. Но это лучше, чем считать сброс по UTC.
        return dt.timezone(dt.timedelta(hours=-8))


def ensure_quota_schema(c: sqlite3.Connection) -> None:
    """Расход квоты: строка на «кошелёк + сутки + потребитель». Идемпотентно.

    Потребитель — не проект и не выпуск, а ИМЯ РАБОТЫ («verify», «donor», «ww2»):
    вопрос, на который таблица отвечает, звучит «кто выел общий ключ сегодня».
    Растёт она медленно: несколько строк в сутки.
    """
    c.execute(
        "CREATE TABLE IF NOT EXISTS api_quota_usage("
        "api TEXT NOT NULL, "               # youtube
        "day TEXT NOT NULL, "               # сутки квоты по тихоокеанскому времени
        "consumer TEXT NOT NULL, "          # verify | donor | ww2 | …
        "units INTEGER NOT NULL DEFAULT 0, "
        "calls INTEGER NOT NULL DEFAULT 0, "
        "updated REAL NOT NULL, "
        "PRIMARY KEY(api, day, consumer))")


def quota_day(now: float | None = None) -> str:
    """Какие сейчас «квотные сутки» — дата по тихоокеанскому времени, YYYY-MM-DD."""
    now = time.time() if now is None else now
    return dt.datetime.fromtimestamp(now, _quota_zone()).strftime("%Y-%m-%d")


def quota_reset_at(now: float | None = None) -> float:
    """Момент ближайшего сброса: ближайшая полночь по тихоокеанскому времени."""
    now = time.time() if now is None else now
    zone = _quota_zone()
    local = dt.datetime.fromtimestamp(now, zone)
    nxt = (local + dt.timedelta(days=1)).date()
    return dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone).timestamp()


def human_left(seconds: float) -> str:
    """«6 ч 40 мин» — ожидание словами. Сухие секунды человек не читает."""
    sec = max(0, int(seconds))
    if sec < 60:
        return "меньше минуты"
    h, m = divmod(sec // 60, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def quota_spend(c: sqlite3.Connection, consumer: str, units: int = 1,
                api: str = QUOTA_YT, now: float | None = None) -> int:
    """Записать расход. Возвращает, сколько всего потрачено за эти сутки.

    Зовётся ПОСЛЕ ответа провайдера: Google списывает единицы за состоявшийся запрос,
    а не за намерение. Локальный отказ (нет ключа, нет video_id) квоты не стоит, и
    приписывать его сюда — врать самим себе о том, сколько осталось.
    """
    now = time.time() if now is None else now
    ensure_quota_schema(c)
    day = quota_day(now)
    c.execute(
        "INSERT INTO api_quota_usage(api,day,consumer,units,calls,updated) "
        "VALUES(?,?,?,?,1,?) ON CONFLICT(api,day,consumer) DO UPDATE SET "
        "units=units+excluded.units, calls=calls+1, updated=excluded.updated",
        (api, day, str(consumer or "?")[:40], int(units), now))
    return quota_used(c, api, now)


def quota_used(c: sqlite3.Connection, api: str = QUOTA_YT,
               now: float | None = None) -> int:
    """Сколько единиц этого ключа мы насчитали за текущие квотные сутки."""
    now = time.time() if now is None else now
    try:
        row = c.execute(
            "SELECT COALESCE(SUM(units),0) FROM api_quota_usage WHERE api=? AND day=?",
            (api, quota_day(now))).fetchone()
    except Exception:                                              # noqa: BLE001
        return 0          # таблицы ещё нет — считать нечего, но и падать не за что
    return int(row[0] or 0)


def quota_usage(c: sqlite3.Connection, api: str = QUOTA_YT,
                now: float | None = None) -> list:
    """Кто сколько потратил за сутки: [{consumer, units, calls}], самый жадный первым."""
    now = time.time() if now is None else now
    try:
        rows = c.execute(
            "SELECT consumer, units, calls FROM api_quota_usage WHERE api=? AND day=? "
            "ORDER BY units DESC, consumer", (api, quota_day(now))).fetchall()
    except Exception:                                              # noqa: BLE001
        return []
    return [{"consumer": r[0], "units": int(r[1] or 0), "calls": int(r[2] or 0)}
            for r in rows]


def quota_wait(c: sqlite3.Connection, api: str = QUOTA_YT,
               now: float | None = None) -> dict:
    """Активное ожидание сброса или {} — единственный вопрос перед вызовом.

    Запись сама протухает: по времени `until` и по смене квотных суток. Второе важнее
    первого — если часы машины уедут, сутки всё равно сменятся.
    """
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, QUOTA_WAIT_KEY) or "{}")
        rec = (data or {}).get(api) or {}
    except (ValueError, TypeError, AttributeError):
        return {}
    if not rec:
        return {}
    if now >= float(rec.get("until") or 0) or rec.get("day") != quota_day(now):
        return {}
    rec = dict(rec)
    rec["left_sec"] = max(0.0, float(rec["until"]) - now)
    return rec


def quota_block(c: sqlite3.Connection, reason: str, api: str = QUOTA_YT,
                kind: str = "daily", now: float | None = None,
                until: float | None = None) -> dict:
    """Отметить «единицы кончились, ждём сброса».

    Возвращает запись с признаком `fresh`: True только когда ожидание НАЧАЛОСЬ сейчас.
    По нему вызывающий и решает, писать ли строку в журнал — причина ожидания нужна
    один раз, а не на каждом тридцатисекундном проходе; иначе журнал выпуска за ночь
    состоит из одной и той же фразы и перестаёт отвечать на вопрос «что было до того».
    """
    now = time.time() if now is None else now
    was = quota_wait(c, api, now)
    if until is None:
        until = (now + YT_RATE_WAIT_SEC) if kind == "rate" else quota_reset_at(now)
    if was:
        # Уже ждём — срок не продлеваем и не укорачиваем: сброс назначает Google,
        # а не частота наших попыток.
        return dict(was, fresh=False)
    try:
        data = json.loads(_state_raw(c, QUOTA_WAIT_KEY) or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    rec = {"since": now, "until": float(until), "kind": kind,
           "day": quota_day(now), "reason": str(reason or "")[:300],
           "used_here": quota_used(c, api, now), "budget": YT_DAILY_UNITS}
    data[api] = rec
    try:
        c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                  (QUOTA_WAIT_KEY, json.dumps(data, ensure_ascii=False), now))
    except Exception:                                              # noqa: BLE001
        # Запомнить ожидание не вышло — причину всё равно возвращаем: вызывающий
        # должен остановить проход сейчас. Хуже, чем с записью (следующий проход
        # снова сходит за 403), но лучше, чем исключение из проверки публикаций.
        pass
    return dict(rec, fresh=True, left_sec=max(0.0, float(until) - now))


def quota_allow(c: sqlite3.Connection, consumer: str, units: int = 1,
                api: str = QUOTA_YT, now: float | None = None,
                budget: int | None = None) -> dict:
    """Можно ли сейчас тратить: {"ok", "why", "wait", "used", "left"}.

    Два разных «нельзя», и оба означают ПОДОЖДАТЬ, а не «сломалось»:
      * активное ожидание после 403 — провайдер уже сказал, что единиц нет;
      * наш собственный счётчик упёрся в бюджет — идти за 403 незачем.

    Второй случай не отменяет первого: ключ общий, и чужой расход наш счётчик не видит,
    поэтому 403 может прийти задолго до того, как наша сумма дойдёт до бюджета.
    """
    now = time.time() if now is None else now
    budget = YT_DAILY_UNITS if budget is None else budget
    wait = quota_wait(c, api, now)
    used = quota_used(c, api, now)
    if wait:
        return {"ok": False, "why": quota_why(wait), "wait": wait,
                "used": used, "left": max(0, budget - used)}
    if used + int(units) > budget:
        return {"ok": False, "used": used, "left": max(0, budget - used), "wait": {},
                "why": (f"свой суточный бюджет YouTube Data API исчерпан: "
                        f"{used} из {budget} единиц. Ждём сброса через "
                        f"{human_left(quota_reset_at(now) - now)} "
                        f"(полночь по тихоокеанскому времени)")}
    return {"ok": True, "why": "", "wait": {}, "used": used, "left": budget - used}


def quota_why(wait: dict) -> str:
    """Причина ожидания словами — то, что увидит владелец вместо кода 403."""
    if not wait:
        return ""
    left = human_left(wait.get("left_sec") or 0)
    if wait.get("kind") == "rate":
        return f"YouTube просит сбавить частоту запросов. Ждём {left}"
    used, budget = int(wait.get("used_here") or 0), int(wait.get("budget") or 0)
    tail = ""
    if budget and used < budget:
        # Наш счётчик далеко от бюджета, а единиц уже нет. Два числа — это улика, но не
        # приговор, и называть виноватым «другого потребителя» как факт нельзя: `budget`
        # — ЭТО НАША СОБСТВЕННАЯ НАСТРОЙКА (VS_YT_QUOTA_UNITS, умолчание 10000), а не
        # то, что Google выдал проекту. Если реальная квота проекта ниже умолчания, наш
        # расход честно меньше бюджета и при этом виноваты мы сами. Поэтому владельцу
        # называются обе возможности — обе проверяемые, и обе ведут к делу.
        tail = (f". Наш расход за сутки — {used} из {budget} единиц: либо единицы "
                f"выел другой потребитель того же ключа, либо бюджет "
                f"VS_YT_QUOTA_UNITS не совпадает с реальной квотой проекта")
    return (f"квота YouTube Data API исчерпана: ждём сброса через {left} "
            f"(полночь по тихоокеанскому времени){tail}")


def quota_state(c: sqlite3.Connection, api: str = QUOTA_YT,
                now: float | None = None) -> dict:
    """Всё про кошелёк одним куском — для экрана и для отчёта прохода."""
    now = time.time() if now is None else now
    wait = quota_wait(c, api, now)
    used = quota_used(c, api, now)
    return {"day": quota_day(now), "budget": YT_DAILY_UNITS, "used": used,
            "left": max(0, YT_DAILY_UNITS - used), "by_consumer": quota_usage(c, api, now),
            "reset_at": quota_reset_at(now), "waiting": bool(wait), "wait": wait,
            "why": quota_why(wait)}


# ─────────────────────────── свободное место на нодах ───────────────────────────

# Тревога про диск смотрела только на сервер. Рендер и заливка идут на другой машине,
# и про её место сервер не знал ничего — а «мало места» без указания машины посылает
# человека проверять не тот компьютер.
NODE_DISK_KEY = "node_disk"
# Через сколько молчания сведения о машине считаются протухшими. Ноды опрашивают
# очередь раз в двадцать секунд, так что четверть часа тишины — это уже тишина.
NODE_DISK_STALE = 900


def note_node_disk(c: sqlite3.Connection, host: str, role: str, free_gb, total_gb,
                   now: float | None = None) -> None:
    """Запомнить, сколько места на машине. Молча игнорирует неполные сведения:
    старый пуллер их не шлёт, и это не повод падать."""
    if not host or free_gb is None:
        return
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, NODE_DISK_KEY) or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    data[str(host)[:40]] = {"role": str(role or "")[:20], "at": now,
                            "free_gb": float(free_gb),
                            "total_gb": float(total_gb or 0)}
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (NODE_DISK_KEY, json.dumps(data, ensure_ascii=False), now))


def node_disk(c: sqlite3.Connection, now: float | None = None) -> list:
    """Что известно про машины: [{host, role, free_gb, total_gb, age_sec, stale}]."""
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, NODE_DISK_KEY) or "{}")
    except ValueError:
        return []
    out = []
    for host, rec in sorted((data or {}).items()):
        if not isinstance(rec, dict):
            continue
        age = now - float(rec.get("at") or 0)
        out.append({"host": host, "role": rec.get("role") or "",
                    "free_gb": rec.get("free_gb"), "total_gb": rec.get("total_gb"),
                    "age_sec": int(age), "stale": age > NODE_DISK_STALE})
    return out


def _state_raw(c: sqlite3.Connection, key: str):
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (key,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return None
    return row[0] if row else None
