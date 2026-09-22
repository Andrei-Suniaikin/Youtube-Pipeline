"""Публичный лог задачи: только прогресс, без внутренней кухни пайплайна.

Клиент видит `job.log` в UI и хвост в `/api/v1` — раньше туда утекала вся
механика: Pass A/B/C/D, воронка, judge/editor/cleaner, имена моделей, хэши и
длины промптов, cache-статистика, номера ретраев, stderr CLI, пути дампов.

Здесь один фильтр на выходе (`server.py: _execute_job_common → drain_log`).
Правило — **default-deny**: строка проходит, только если распознана как прогресс,
этап или ошибка; всё остальное отбрасывается. Новый лог из будущего кода по
умолчанию наружу не попадёт.

Из сырой строки наружу выносятся только: номера частей и название языка (его
задал сам клиент в задаче, и только если этот язык уже встречался как начатый).
Текст исключений, пути, модели, токены не выносятся никогда — вместо них код
ошибки (`E-LIMIT`, `E-TIMEOUT`, `E-NET`, `E-PART`, `E-SAVE`, `E-INTERNAL`),
по которому нужное место находится в полном логе на сервере.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, List, Optional

__all__ = ["PublicLogFilter", "public_log_line"]

# ============== публичные формулировки ==============

P_ACCEPTED = "Задача принята"
P_PREPARING = "Подготовка"
P_QUALITY = "Проверка качества"
P_WORKING = "Обработка…"
P_RETRY = "Повторная попытка"
P_WAIT_LIMIT = "Ожидание — лимит провайдера (E-LIMIT)"
P_WAIT_RETRY = "Ожидание — повторная попытка"
P_STOPPED = "Остановлено"
P_DONE = "Готово — файл сохранён"
P_ERR_TIMEOUT = "Ошибка: истекло время ожидания (E-TIMEOUT)"
P_ERR_NET = "Ошибка: сеть недоступна (E-NET)"
P_ERR_SAVE = "Ошибка: результат не сохранён (E-SAVE)"
P_ERR_INTERNAL = "Ошибка: сбой обработки (E-INTERNAL)"
P_ERR_FAILED = "Ошибка: задача не завершена (E-FAIL)"
P_ERR_MODEL = (
    "Ошибка: модель недоступна у провайдера — выберите другую модель "
    "и запустите заново (E-MODEL)"
)

HEARTBEAT_SEC = 120

# ============== нормализация входа ==============

_TS_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*")
_WS = re.compile(r"\s+")
# lang_display задаёт клиент в задаче; наружу пускаем только «похожее на имя».
_LANG_BAD = re.compile(r"[/\\=<>{}\[\]|]|https?:|\.\w{2,4}$")


def _normalize(raw: str) -> str:
    """Снять таймштамп log_msg и отступы — паттерны матчим по сути строки."""
    return _TS_PREFIX.sub("", str(raw)).strip()


def _num(value: str) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 999999 else None


def _safe_lang(value: str) -> str:
    """Название языка из задачи клиента. Пустая строка = наружу не выносим."""
    lang = _WS.sub(" ", str(value or "")).strip()
    if not lang or len(lang) > 40 or _LANG_BAD.search(lang):
        return ""
    return lang


# ============== паттерны прогресса ==============

# Генерация: «📐 {preset}: {n} chunk(s) × ~{words} words, …»
RE_GEN_PLAN = re.compile(r"^📐 .+?: (\d+) chunk\(s\)")
# Рерайт: «🌍 {lang}: начинаю ({n} частей)»
RE_LANG_START = re.compile(r"^🌍 (?P<lang>.+?): начинаю \((?P<n>\d+) частей\)")
# «🔄 {lang}: продолжаю с части {k}/{n}»
RE_LANG_RESUME = re.compile(
    r"^🔄 (?P<lang>.+?): продолжаю с части (?P<k>\d+)/(?P<n>\d+)"
)
# «   ✅ {lang}: часть {i}/{n}»
RE_LANG_CHUNK = re.compile(r"^✅ (?P<lang>.+?): часть (?P<i>\d+)/(?P<n>\d+)")
# Генерация: «✅ Chunk 3/12: …», «✅ Conv turn 3/12: …», «✅ Codex turn 3/12: …»
RE_GEN_CHUNK = re.compile(
    r"^✅ (?:Chunk|Conv turn|Codex turn|Turn) (?P<i>\d+)/(?P<n>\d+)\b"
)
# «✅ {lang}: готово → file.txt» — якорь строгий, чтобы не поймать
# «✅ {lang}: Pass C (nativeness) готов» и «✅ {lang}: брендинг применён».
RE_LANG_DONE = re.compile(r"^✅ (?P<lang>.+?): готово(?: →.*)?$")
RE_LANG_DONE_ALT = re.compile(
    r"^✅ (?P<lang>.+?): (?:все части готовы|уже завершено ранее)$"
)
RE_LANG_PARTIAL = re.compile(r"^⚠️ (?P<lang>.+?): готово с \d+ ошибками")
# «⚠️ {lang}: N чанков провалились — файл НЕ сохранён …»
RE_LANG_FAILED = re.compile(r"^⚠️ (?P<lang>.+?): \d+ чанков провалились")
# FIX-2026-09-13-length: жёлтые предупреждения о длине — задача успешна, текст короче.
# Наружу — язык (по тем же правилам) и два числа слов; имя файла не выносим.
# «⚠️ {lang}: текст короче исходника: N из M слов → file.txt»
RE_LANG_SHORT = re.compile(
    r"^⚠️ (?P<lang>.+?): текст короче исходника: (?P<n>\d+) из (?P<m>\d+) слов(?: →.*)?$"
)
# «⚠️ Текст короче цели: N из ~M слов» — генерация
RE_GEN_SHORT = re.compile(r"^⚠️ Текст короче цели: (?P<n>\d+) из ~(?P<m>\d+) слов$")
RE_GEN_DONE = re.compile(r"^🎬 Done:")
RE_STOPPED = re.compile(r"^(?:⏹️|⛔)")
RE_TASK_RETRY = re.compile(r"^🔁")

# Этапы: наружу идёт только имя фазы, никаких параметров.
RE_PHASE_PREPARE = re.compile(
    r"^(?:📊 .+?: Analyz|📊 .+?: анализ из кэша|📐 Проектирую воронку|✅ Воронка:"
    r"|ℹ️ план \d+ частей)"
)
RE_PHASE_QUALITY = re.compile(
    r"^(?:🔎|🧹|✂️|🗃️|📐 .+?: постпроцесс|📐 Postprocess|⚖️|🧑‍⚖️)"
)
# judge-строки приходят готовым текстом из rewrite_core (переменная _jl).
RE_PHASE_JUDGE = re.compile(r"(?i)\bjudge\b|\bретеншн\b|\bсудья\b")

# ============== ошибки и ожидания ==============

RE_ERR_MARKER = re.compile(r"^(?:❌|⚠️|⏳|⏱️|🔄|↻)")
RE_FAIL_MARKER = re.compile(r"^❌")
RE_WAIT_MARKER = re.compile(r"^(?:⏳|⏱️)")
RE_SAVE_FAIL = re.compile(r"^❌ Не сохранён финальный скрипт")

KW_LIMIT = ("rate limit", "usage limit", "на лимите", "лимит", "429", "quota")
KW_TIMEOUT = ("timeout", "таймаут", "stall", "истекло")
KW_NET = ("disconnect", "connection", "сетевая ошибка", "network", "сеть")
# FIX-2026-09-13-models: метка rewrite_core — провайдер не даёт саму модель.
RE_MODEL_UNAVAILABLE = re.compile(r"\bCODEX_MODEL_UNAVAILABLE\b")


def _classify_error(text: str) -> Optional[str]:
    """Строка с маркером ошибки → публичный код. None = наружу не выносим.

    Уровень маркера важнее ключевых слов:
      ❌ — задача реально провалилась, клиенту нужен код;
      ⏳/⏱️ — идёт ожидание, задача жива;
      ⚠️/🔄 — внутреннее предупреждение пайплайна (откат, фолбэк, ретрай
      парсера). Наружу пускаем только лимит провайдера: он объясняет долгую
      паузу. Остальное молчит — иначе клиент по «⚠️» пересчитает все ретраи и
      увидит, где именно пайплайн спотыкается.
    """
    low = text.lower()
    # Раньше ключевых слов: в хвосте строки бывает кусок промпта со словом «лимит».
    if RE_FAIL_MARKER.match(text) and RE_MODEL_UNAVAILABLE.search(text):
        return P_ERR_MODEL
    if any(k in low for k in KW_LIMIT):
        return P_WAIT_LIMIT
    if RE_WAIT_MARKER.match(text):
        return P_WAIT_RETRY
    if not RE_FAIL_MARKER.match(text):
        return None
    if any(k in low for k in KW_TIMEOUT):
        return P_ERR_TIMEOUT
    if any(k in low for k in KW_NET):
        return P_ERR_NET
    return P_ERR_INTERNAL


class PublicLogFilter:
    """Сырой лог задачи → публичный прогресс. Состояние живёт одну задачу."""

    def __init__(self, heartbeat_sec: int = HEARTBEAT_SEC):
        self._heartbeat_sec = heartbeat_sec
        self._known_langs: set = set()
        self._last_public: str = ""
        self._last_emit: Optional[datetime] = None

    # ---- вывод ----

    def _stamp(self, text: str, now: datetime) -> str:
        return f"[{now.strftime('%H:%M:%S')}] {text}"

    def _emit(
        self, text: str, now: datetime, out: List[str], allow_repeat: bool = False
    ) -> None:
        # Схлопываем подряд идущие повторы — кроме heartbeat, который повторами
        # и работает: подавленный дубль не обновлял бы таймер и убил бы признак
        # жизни насовсем.
        if text == self._last_public and not allow_repeat:
            return
        out.append(self._stamp(text, now))
        self._last_public = text
        self._last_emit = now

    # ---- перевод одной строки ----

    def _translate(self, line: str) -> Optional[str]:
        text = _normalize(line)
        if not text:
            return None

        m = RE_LANG_CHUNK.match(text)
        if m:
            i, n = _num(m.group("i")), _num(m.group("n"))
            if i is None or n is None:
                return None
            lang = self._lang_or_empty(m.group("lang"))
            return f"{lang}: {i}/{n}" if lang else f"Часть {i}/{n}"

        m = RE_GEN_CHUNK.match(text)
        if m:
            i, n = _num(m.group("i")), _num(m.group("n"))
            return f"Часть {i}/{n}" if i is not None and n is not None else None

        m = RE_LANG_START.match(text)
        if m:
            n = _num(m.group("n"))
            lang = _safe_lang(m.group("lang"))
            if not lang or n is None:
                return P_PREPARING
            self._known_langs.add(lang)
            return f"{lang}: старт — {n} частей"

        m = RE_LANG_RESUME.match(text)
        if m:
            k, n = _num(m.group("k")), _num(m.group("n"))
            lang = _safe_lang(m.group("lang"))
            if k is None or n is None:
                return None
            if lang:
                self._known_langs.add(lang)
                return f"{lang}: продолжение — {k}/{n}"
            return f"Продолжение — {k}/{n}"

        m = RE_GEN_PLAN.match(text)
        if m:
            n = _num(m.group(1))
            return f"{P_PREPARING} — {n} частей" if n is not None else P_PREPARING

        for pattern in (RE_LANG_DONE, RE_LANG_DONE_ALT):
            m = pattern.match(text)
            if m:
                lang = self._lang_or_empty(m.group("lang"))
                return f"{lang}: готово" if lang else "Часть работы готова"

        m = RE_LANG_PARTIAL.match(text)
        if m:
            lang = self._lang_or_empty(m.group("lang"))
            return f"{lang}: готово частично" if lang else "Готово частично"

        m = RE_LANG_FAILED.match(text)
        if m:
            lang = self._lang_or_empty(m.group("lang"))
            tail = "не удалось завершить (E-PART)"
            return f"{lang}: {tail}" if lang else f"Ошибка: {tail}"

        m = RE_LANG_SHORT.match(text)
        if m:
            n, total = _num(m.group("n")), _num(m.group("m"))
            if n is None or total is None:
                return None
            lang = self._lang_or_empty(m.group("lang"))
            tail = f"текст короче исходника: {n} из {total} слов"
            return f"{lang}: {tail}" if lang else f"Т{tail[1:]}"

        m = RE_GEN_SHORT.match(text)
        if m:
            n, total = _num(m.group("n")), _num(m.group("m"))
            if n is None or total is None:
                return None
            return f"Текст короче цели: {n} из ~{total} слов"

        if RE_GEN_DONE.match(text):
            return P_DONE
        if RE_STOPPED.match(text):
            return P_STOPPED
        if RE_TASK_RETRY.match(text):
            return P_RETRY
        if RE_SAVE_FAIL.match(text):
            return P_ERR_SAVE
        if RE_PHASE_PREPARE.match(text):
            return P_PREPARING
        if RE_PHASE_QUALITY.match(text) or RE_PHASE_JUDGE.search(text):
            return P_QUALITY
        if RE_ERR_MARKER.match(text):
            return _classify_error(text)
        return None

    def _lang_or_empty(self, value: str) -> str:
        """Язык выносим наружу, только если он уже засветился как начатый.

        Так в позицию языка не попадёт техническая строка вроде «Chunk 3»,
        если формат лога в пайплайне однажды поменяется.
        """
        lang = _safe_lang(value)
        return lang if lang in self._known_langs else ""

    # ---- API ----

    def start(self, now: Optional[datetime] = None) -> List[str]:
        now = now or datetime.now()
        out: List[str] = []
        self._emit(P_ACCEPTED, now, out)
        return out

    def feed(
        self, raw_lines: Iterable[str], now: Optional[datetime] = None
    ) -> List[str]:
        now = now or datetime.now()
        out: List[str] = []
        raw_lines = list(raw_lines or [])
        for line in raw_lines:
            public = self._translate(line)
            if public:
                self._emit(public, now, out)
        if not out and self._heartbeat_due(now):
            # Долгие этапы (воронка, судья, длинный API-вызов) молчат минутами —
            # без этого клиент видит зависшую задачу.
            self._emit(P_WORKING, now, out, allow_repeat=True)
        return out

    def _heartbeat_due(self, now: datetime) -> bool:
        if self._last_emit is None:
            return False
        return (now - self._last_emit).total_seconds() >= self._heartbeat_sec

    def close(
        self, status: str = "", now: Optional[datetime] = None
    ) -> List[str]:
        """Финальная строка по статусу задачи — чтобы конец был явным."""
        now = now or datetime.now()
        out: List[str] = []
        status = (status or "").lower()
        if status == "completed":
            self._emit(P_DONE, now, out)
        elif status == "cancelled":
            self._emit(P_STOPPED, now, out)
        elif status == "failed":
            self._emit(P_ERR_FAILED, now, out)
        return out


def public_log_line(raw: str) -> Optional[str]:
    """Разовый перевод одной строки — для тестов и отладки правил."""
    return PublicLogFilter()._translate(raw)
