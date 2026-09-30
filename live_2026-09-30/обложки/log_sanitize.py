"""Упрощение пользовательского лога задачи.

`job.log` отдаётся клиенту (`GET /jobs/{id}`) и рисуется в UI. Подробные
сообщения пайплайна раскрывают внутреннюю кухню: имена провайдеров, шаги
(анализ превью → style-transfer → адаптация текста → проверка надписи),
кусок промпта, ретраи, абсолютные пути, сырые ответы API, traceback.

Здесь этот поток превращается в короткий прогресс:

    ▶ Старт: 12 обложек
    ✓ 1/12 · pl_koperta.jpg
    ⚠ повтор попытки 2/3
    ✓ 2/12 · ro_coperta.jpg (без текста)
    ■ Завершено: 12 из 12

Принцип — WHITELIST: распознаём только известные события, всё остальное
молча выбрасываем. Новая (ещё не классифицированная) строка в cover_core
не утечёт сама собой — в отличие от blacklist-подхода.

ENV:
    COVER_LOG_VERBOSE=1  — вернуть в UI подробный лог как было (для отладки).
    COVER_LOG_JOURNAL=0  — не дублировать подробный лог в stderr/journalctl.
"""

import os
import re
import threading


def log_verbose() -> bool:
    """Подробный лог в UI (отладка владельца). Читается динамически."""
    return str(os.environ.get("COVER_LOG_VERBOSE", "0")).strip().lower() in ("1", "true", "yes", "on")


def log_journal() -> bool:
    """Дублировать полный лог в stderr → journalctl (диагностика владельца)."""
    return str(os.environ.get("COVER_LOG_JOURNAL", "1")).strip().lower() in ("1", "true", "yes", "on")


# --- классификация сообщений cover_core -------------------------------------
# Сообщения приходят с префиксами обёрток: "[pl] …", "[ro v2] …", "[3/12] …",
# поэтому ищем маркеры внутри строки (re.search), а не только в начале.

_RE_SAVED = re.compile(r"💾\s*Сохранено\s*(?P<notext>\(без текста\))?\s*:\s*(?P<path>.+?)\s*$")
_RE_SKIPPED = re.compile(r"⏭.*?Уже есть\s*:\s*(?P<path>.+?)\s*$")
_RE_CANCELLED = re.compile(r"⏹\s*Задание отменено")
_RE_RETRY = re.compile(r"⚠\s*Попытка\s*(?P<k>\d+)\s*/\s*(?P<n>\d+)")
_RE_REF_MISSING = re.compile(r"Реф-файл\s.*пропущен|Референс не найден")
_RE_ERROR = re.compile(r"❌")

# «Ошибки», которые на деле — промежуточные шаги пайплайна: их показ раскрыл бы
# наличие GPT-проверки надписи и fallback-ветки «без текста».
_ERROR_DROP_MARKERS = (
    "Проблема:",
    "Не удалось сгенерировать даже без текста",
)


# --- человеческие формулировки ошибок ---------------------------------------
# Возвращаем ТОЛЬКО фиксированные строки из этой таблицы: исходный текст
# исключения наружу не подставляется никогда (иначе утечка через ошибку).
_ERROR_RULES = (
    (
        ("отменен", "отменён", "cancel", "aborted"),
        "Задача отменена",
    ),
    (
        ("moderation", "safety", "policy", "blocked", "prohibited", "content filter",
         "violat", "rejected", "цензур", "фильтр контента", "запрещ"),
        "Запрос отклонён фильтром контента — измените текст или референс",
    ),
    (
        ("401", "403", "unauthorized", "forbidden", "invalid key", "invalid api key",
         "api key", "apikey", "ключ", "лиценз", "auth", "credential", "permission denied"),
        "Нет доступа к сервису генерации — сообщите администратору",
    ),
    (
        ("429", "rate limit", "too many requests", "quota", "no accounts available",
         "insufficient", "billing", "credit", "лимит", "исчерпан"),
        "Сервис перегружен или лимит исчерпан — повторите позже",
    ),
    (
        ("timeout", "timed out", "таймаут", "истекло время"),
        "Сервис не ответил вовремя — повторите задачу",
    ),
    (
        ("не удалось скачать", "download", "скачивани", "youtube", "превью недоступ"),
        "Не удалось получить исходное превью по ссылке",
    ),
    (
        ("не отдал картинку", "пустой ответ", "пустой результат", "empty", "no image",
         "не вернул", "нет изображени"),
        "Сервис не вернул изображение — повторите задачу",
    ),
    (
        ("no space", "disk full", "read-only", "oserror", "ioerror", "errno"),
        "Ошибка сохранения файла на сервере",
    ),
    (
        ("connection", "network", "dns", "ssl", "proxy", "unreachable", "reset by peer",
         "500", "502", "503", "504", "bad gateway"),
        "Сервис генерации временно недоступен — повторите позже",
    ),
)

_ERROR_DEFAULT = "Не удалось выполнить задачу — попробуйте повторить"


def humanize_error(err) -> str:
    """Техническая ошибка → понятная пользователю причина без внутренних деталей."""
    text = str(err or "").strip().lower()
    if not text:
        return _ERROR_DEFAULT
    for needles, message in _ERROR_RULES:
        for needle in needles:
            if needle in text:
                return message
    return _ERROR_DEFAULT


def _plural_covers(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "обложка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "обложки"
    return "обложек"


class JobLogSanitizer:
    """Поток подробных сообщений → строки прогресса. Потокобезопасен.

    `feed()` зовётся из воркер-потоков пула генерации, поэтому счётчики под
    локом. При COVER_LOG_VERBOSE=1 работает как passthrough.
    """

    def __init__(self, total: int = 0, verbose=None):
        self._lock = threading.Lock()
        self._total = max(0, int(total or 0))
        self._done = 0
        self._reasons = {}          # причина → сколько раз встретилась
        self._verbose = log_verbose() if verbose is None else bool(verbose)
        self._cancelled = False

    # --- состояние ---------------------------------------------------------
    @property
    def verbose(self) -> bool:
        return self._verbose

    @property
    def failed(self) -> int:
        with self._lock:
            return sum(self._reasons.values())

    def set_total(self, total: int):
        """Уточнить общее число обложек (авторитетное значение из ядра)."""
        try:
            value = int(total or 0)
        except (TypeError, ValueError):
            return
        if value > 0:
            with self._lock:
                self._total = value

    def _counter(self) -> str:
        return f"{self._done}/{self._total}" if self._total else f"{self._done}"

    # --- вывод -------------------------------------------------------------
    def start(self) -> list:
        if self._verbose:
            return []
        with self._lock:
            total = self._total
        if total:
            return [f"▶ Старт: {total} {_plural_covers(total)}"]
        return ["▶ Старт"]

    def feed(self, msg: str) -> list:
        """Одно сообщение ядра → 0..1 строк пользовательского лога."""
        if self._verbose:
            return [str(msg)]
        text = str(msg or "")
        if not text.strip():
            return []

        if _RE_CANCELLED.search(text):
            with self._lock:
                if self._cancelled:
                    return []          # ядро пишет отмену из каждого потока
                self._cancelled = True
            return ["⏹ Отменено"]

        m = _RE_SAVED.search(text)
        if m:
            name = os.path.basename(m.group("path").strip()) or "обложка"
            suffix = " (без текста)" if m.group("notext") else ""
            with self._lock:
                self._done += 1
                counter = self._counter()
            return [f"✓ {counter} · {name}{suffix}"]

        if _RE_SKIPPED.search(text):
            with self._lock:
                self._done += 1
                counter = self._counter()
            return [f"⏭ {counter} · уже готово"]

        m = _RE_RETRY.search(text)
        if m:
            return [f"⚠ повтор попытки {m.group('k')}/{m.group('n')}"]

        if _RE_REF_MISSING.search(text):
            return ["⚠ референс не приложен"]

        if _RE_ERROR.search(text):
            if any(marker in text for marker in _ERROR_DROP_MARKERS):
                return []              # промежуточный шаг, не результат задачи
            reason = humanize_error(text)
            with self._lock:
                seen = reason in self._reasons
                self._reasons[reason] = self._reasons.get(reason, 0) + 1
            return [] if seen else [f"✗ {reason}"]   # дубли — сводкой в финале

        return []                      # whitelist: неизвестное не показываем

    def note_error(self, err) -> list:
        """Фатальная ошибка задачи (исключение воркера)."""
        if self._verbose:
            return []
        reason = humanize_error(err)
        with self._lock:
            self._reasons[reason] = self._reasons.get(reason, 0) + 1
        return [f"✗ {reason}"]

    def finish(self, ok: int = None, failed: int = None) -> list:
        """Итоговая строка + сводка по причинам неудач."""
        if self._verbose:
            return []
        with self._lock:
            done = self._done if ok is None else max(0, int(ok or 0))
            total = self._total or done
            reasons = dict(self._reasons)
        lines = [f"■ Завершено: {done} из {total}"]
        missing = failed if failed is not None else max(0, total - done)
        if missing and reasons:
            for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"✗ Не удалось: {count} — {reason}")
        elif missing:
            lines.append(f"✗ Не удалось: {missing} — {_ERROR_DEFAULT}")
        return lines


def expected_cover_count(mode: str, urls, languages, titles_per_lang,
                         seeds_per_lang, n_variants: int = 1) -> int:
    """Сколько обложек ожидается — для строки старта (ядро потом уточняет)."""
    langs = len(languages or []) or 1
    try:
        variants = max(1, int(n_variants or 1))
    except (TypeError, ValueError):
        variants = 1
    if mode == "seed":
        seeds = len(seeds_per_lang or {}) or langs
        return seeds * variants
    if mode == "title":
        return len(titles_per_lang or {}) or langs
    return (len(urls or []) or 1) * langs
