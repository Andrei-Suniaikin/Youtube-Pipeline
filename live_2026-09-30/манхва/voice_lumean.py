# -*- coding: utf-8 -*-
"""Озвучка через Lumean (платно) — запасной движок при MANGA_TTS_ENGINE=lumean.

Один блок озвучки = один заказ Lumean. Логика оплаты перенесена из VideoStudio
(src/tts_worker.py) в минимальном виде:

- рядом с блоком лежит квитанция block_NNNN.mp3.lumean.json. Повторный запуск шага
  с тем же текстом и шаблоном НЕ создаёт второй заказ, а ждёт старый;
- отправка заказа без ответа (обрыв, 5xx) оставляет квитанцию «creating» — дальше
  только ручная проверка в кабинете: у POST /orders нет защиты от двойной оплаты;
- закрытый заказ (failed/compensated/cancelled) заказывается заново только после живой
  проверки, что за него ничего не списано (cancelled 30.08.2026 списал 15 122 токена);
- ключ и шаблон проверяются ДО любого заказа.

Меток слов Lumean не отдаёт, поэтому тайминги кадров идут через распознавание речи
(pipeline.step_align → align.whisper_words на языке проекта).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import voice as VO
from generation_receipts import atomic_json


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name}: нужно целое число, а не {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name}: не меньше {minimum}")
    return value


BASE_URL = (os.environ.get("MANGA_LUMEAN_API_BASE") or "https://api.lumean.app/api/public").rstrip("/")
POLL_SECONDS = _int_env("MANGA_LUMEAN_POLL_SECONDS", 10, 1)
HEARTBEAT_SECONDS = _int_env("MANGA_LUMEAN_HEARTBEAT_SECONDS", 120, 1)
# 03.09.2026 в VideoStudio ожидание очереди подняли до 300 мин по просьбе владельца.
MAX_WAIT_MINUTES = _int_env("MANGA_LUMEAN_MAX_WAIT_MINUTES", 300, 1)
PARALLEL = _int_env("MANGA_LUMEAN_PARALLEL", 5, 1)
# Один заказ ≈ 5 минут речи: меньше швов интонации, чем у блоков edge-tts по 55 с.
BLOCK_SECONDS = _int_env("MANGA_LUMEAN_BLOCK_SECONDS", 300, 30)
# Жёсткого предела в API не найдено: 04.09.2026 прошли заказы до 216 562 знаков, Lumean
# сам режет текст на куски до 2000 знаков. Предел здесь — ограничение цены ошибки одного заказа.
MAX_CHARS = _int_env("MANGA_LUMEAN_MAX_CHARS", 10000, 500)
# 27.09.2026, аудит A2-TTS01: сколько длится общий перерыв после отказа сервиса. 15 мин —
# ролик за это время всё равно не дойдёт до озвучки заново, а волна не наплодит «creating».
PAUSE_MINUTES = _int_env("MANGA_LUMEAN_PAUSE_MINUTES", 15, 1)
KEY_FILES = (Path.home() / "video_studio_api_keys.json", Path("/root/video_studio_api_keys.json"))
SUCCESS = {"completed", "result_delivered"}
TERMINAL_FAILURE = {"failed", "compensated", "cancelled"}
TTS_SERVICES = {"heygen", "elevenlabs"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class LumeanConfigError(RuntimeError):
    """Нет ключа или шаблона — выясняется до заказа, деньги не тронуты."""


class LumeanAPIError(RuntimeError):
    def __init__(self, status: int, action: str, payload: dict, retry_after: Optional[float] = None):
        self.status = int(status)
        self.retry_after = retry_after
        message = str(payload.get("message") or "неизвестная ошибка")[:300]
        # Само «Ошибка валидации» не говорит, что не так: детали лежат в errors, и без них
        # 20.09 пришлось выяснять причину отказа вручную. Печатаем поле и первую причину.
        errors = payload.get("errors")
        if isinstance(errors, dict) and errors:
            details = "; ".join(f"{field}: {(value[0] if isinstance(value, list) and value else value)}"
                                for field, value in list(errors.items())[:3])
            message = f"{message} ({details[:300]})"
        super().__init__(f"Lumean: {action}: HTTP {self.status}: {message}")


class LumeanNetworkError(RuntimeError):
    pass


def templates_from_env() -> Dict[str, str]:
    raw = (os.environ.get("MANGA_LUMEAN_TEMPLATES") or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError('MANGA_LUMEAN_TEMPLATES: нужен JSON вида {"Russian": "<uuid шаблона>"}') from None
    if not isinstance(value, dict):
        raise ValueError('MANGA_LUMEAN_TEMPLATES: нужен JSON вида {"Russian": "<uuid шаблона>"}')
    bad = [k for k, v in value.items() if not isinstance(v, str) or not _UUID.match(v.strip())]
    if bad:
        raise ValueError(f"MANGA_LUMEAN_TEMPLATES: не UUID шаблона у языков {', '.join(map(str, bad))}")
    return {str(k): v.strip() for k, v in value.items()}


templates_from_env()   # битая настройка останавливает запуск службы с понятной причиной


def voices_from_env() -> Dict[str, str]:
    """MANGA_LUMEAN_VOICE — подмена голоса в заказе.

    Шаблон в кабинете Lumean задаёт и сервис, и голос. Один голос на семь языков держать
    семью шаблонами неудобно: заводим один шаблон на сервис и подменяем голос в заказе.
    Значение — либо один голос на все языки ("A9ATT..."), либо JSON {"Russian": "..."}.
    Пусто — голос берётся из шаблона, как раньше.
    """
    raw = (os.environ.get("MANGA_LUMEAN_VOICE") or "").strip()
    if not raw:
        return {}
    value: object = raw
    if raw.startswith("{"):
        try:
            value = json.loads(raw)
        except ValueError:
            raise ValueError('MANGA_LUMEAN_VOICE: нужен голос или JSON вида {"Russian": "<голос>"}') from None
    if isinstance(value, str):
        value = {"*": value.strip()}
    if not isinstance(value, dict):
        raise ValueError('MANGA_LUMEAN_VOICE: нужен голос или JSON вида {"Russian": "<голос>"}')
    bad = [k for k, v in value.items()
           if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,64}", v.strip())]
    if bad:
        raise ValueError(f"MANGA_LUMEAN_VOICE: не похоже на голос у языков {', '.join(map(str, bad))}")
    return {str(k): v.strip() for k, v in value.items()}


def voice_for(lang: str) -> str:
    """Голос для языка: точное совпадение, иначе общий '*', иначе пусто (голос шаблона)."""
    voices = voices_from_env()
    return voices.get(lang) or voices.get("*") or ""


voices_from_env()      # битая настройка останавливает запуск службы с понятной причиной


def api_key() -> str:
    for name in ("LUMEAN_API_KEY", "MANGA_LUMEAN_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    for path in KEY_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                value = str(json.load(f).get("lumean") or "").strip()
        except (OSError, ValueError, AttributeError):
            continue
        if value:
            return value
    return ""


def preflight(lang: str) -> Tuple[str, str]:
    """(ключ, шаблон) для языка проекта или понятная ошибка до любого заказа."""
    key = api_key()
    if not key:
        raise LumeanConfigError(
            "Lumean: нет ключа — задайте LUMEAN_API_KEY в web.env или поле \"lumean\" в "
            + " / ".join(str(p) for p in KEY_FILES) + "; заказ не создан")
    template = templates_from_env().get(lang)
    if not template:
        raise LumeanConfigError(
            f"Lumean: нет шаблона голоса для языка {lang} — добавьте его в MANGA_LUMEAN_TEMPLATES; "
            "заказ не создан")
    return key, template


def voice_id(template: str, voice: str = "") -> str:
    """Что стоит на месте голоса edge в отпечатке озвучки.

    Голос входит в отпечаток: сменили голос — прежние блоки перестают годиться и
    заказываются заново, а не подмешиваются к новым.
    """
    return "lumean:" + template + (":" + voice if voice else "")


# ---------- HTTP ----------
def _http(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float):
    """(код, заголовки, тело). Единственная точка выхода в сеть — подменяется в тестах."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, OSError) as e:
        raise LumeanNetworkError(f"Lumean: нет связи: {e}") from e


def _api(key: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 60,
         hdr: Optional[dict] = None) -> dict:
    headers = {"X-API-KEY": key, "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status, resp_headers, raw = _http(method, BASE_URL + path, headers, data, timeout)
    if hdr is not None:
        hdr.update(resp_headers or {})
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if status >= 400:
        retry_after = payload.get("retry_after") or resp_headers.get("Retry-After")
        try:
            retry_after = max(0.0, float(retry_after)) if retry_after not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise LumeanAPIError(status, f"{method} {path.split('?')[0]}", payload, retry_after)
    if not payload.get("success") or not isinstance(payload.get("data"), dict):
        raise LumeanNetworkError(f"Lumean: {method} {path}: неожиданный формат ответа")
    return payload["data"]


def _transient(e: Exception) -> bool:
    return isinstance(e, LumeanNetworkError) or (
        isinstance(e, LumeanAPIError) and (e.status == 429 or e.status >= 500))


def _service_down(e: Exception) -> bool:
    """Отказ всего сервиса, а не одного текста — после него новые заказы прохода не шлём.

    27.09.2026, аудит F04: POST /orders отвечал 503 «технические работы», а остальные блоки
    продолжали заказывать — 256 квитанций «creating» без номера заказа в 8 проектах.
    Сюда же обрыв связи, 408, 429 после повторов и отказ ключа/баланса (401/402/403):
    следующий заказ с тем же ключом получит то же самое.
    """
    if isinstance(e, LumeanNetworkError):
        return True
    return isinstance(e, LumeanAPIError) and (e.status >= 500 or e.status in (401, 402, 403, 408, 429))


class _Skipped(RuntimeError):
    """Блок не заказан: рубильник прохода уже выключен. Квитанции нет, денег не тронуто."""


# ---------- общий перерыв (27.09.2026, аудит A2-TTS01) ----------
# Рубильник выше живёт в памяти одного прохода. Волна идёт 4 роликами параллельно, и каждый
# следующий ролик снова слал заказы в лежащий сервис: заглушка, 20 роликов × 20 блоков при
# постоянном 503 — 100 отправок и 100 «creating» на ручной разбор. Перерыв лежит в файле рядом
# с projects_web: его видят все потоки, процессы и проходы, и он кончается сам.
_wall_clock = time.time    # настенные часы: общие для процессов; подменяется в тестах


def pause_path(key: str) -> Path:
    root = os.environ.get("MANGA_ROOT") or str(Path(__file__).resolve().parents[1] / "projects_web")
    return Path(root) / f"lumean_pause_{api_scope(key)[:16]}.json"


def _clock_text(ts: float) -> str:
    return time.strftime("%d.%m %H:%M:%S", time.localtime(ts))


def pause_until(key: str) -> float:
    """До какого момента (unix-время) новые заказы Lumean не создаются; 0 — перерыва нет."""
    try:
        with open(pause_path(key), encoding="utf-8") as f:
            until = float(json.load(f).get("until") or 0)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return 0.0
    сейчас = _wall_clock()
    # дальше суток — не перерыв: сама студия больше суток не пишет, а бесконечность или
    # время через сто лет (ручная правка, скачок часов) остановили бы озвучку навсегда
    # (сверка 27.09: Infinity в файле ронял озвучку с OverflowError)
    if until != until or until > сейчас + 24 * 3600:
        return 0.0
    return until if until > сейчас else 0.0


def set_pause(key: str, e: Exception, log=print) -> float:
    """Ставит или продлевает перерыв. Сбой записи не валит озвучку: остаётся рубильник прохода."""
    seconds = PAUSE_MINUTES * 60
    retry_after = getattr(e, "retry_after", None)
    if retry_after:
        seconds = max(seconds, min(float(retry_after), 24 * 3600))
    until = max(_wall_clock() + seconds, pause_until(key))
    path = pause_path(key)
    # Не atomic_json: он называет временный файл по номеру процесса, а отказ ловят сразу
    # несколько потоков одного процесса — им нужен свой временный файл на поток.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"until": until, "until_text": _clock_text(until), "set_at": _clock_text(_wall_clock()),
                       "reason": str(e)[:300]}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as err:
        log(f"  Lumean: общий перерыв не записан ({err}) — действует только рубильник этого прохода")
        return 0.0
    return until


def _api_retry(key, method, path, body=None, attempts=4, sleep=time.sleep, log=print):
    """Повторы только для бесплатных запросов (опрос, ссылка на файл). Не для POST /orders."""
    for attempt in range(attempts):
        try:
            return _api(key, method, path, body)
        except Exception as e:
            if not _transient(e) or attempt >= attempts - 1:
                raise
            delay = getattr(e, "retry_after", None) or min(60, POLL_SECONDS * 2 ** attempt)
            log(f"  Lumean: временная ошибка ({e}) — повтор через {delay:g} с")
            sleep(delay)


def check_template(key: str, template: str) -> str:
    """Бесплатный GET: ключ принят и шаблон — озвучка, а не звуковые эффекты."""
    try:
        data = _api_retry(key, "GET", f"/templates/{template}")
    except LumeanAPIError as e:
        if e.status == 401:
            raise LumeanConfigError("Lumean: ключ отвергнут (HTTP 401); заказ не создан") from None
        if e.status == 404:
            raise LumeanConfigError(f"Lumean: шаблон {template} не найден в кабинете; заказ не создан") from None
        raise
    service = str(data.get("service_key") or (data.get("service") or {}).get("code") or "").lower()
    if service not in TTS_SERVICES:
        raise LumeanConfigError(f"Lumean: шаблон {template} ({data.get('name')}) — сервис {service or '?'}, "
                                "а не озвучка; заказ не создан")
    return str(data.get("name") or template)


# ---------- квитанции ----------
def receipt_path(mp3_path: str) -> str:
    return mp3_path + ".lumean.json"


def identity(text: str, template: str, voice: str = "") -> str:
    # Голос попадает в отпечаток, только если он задан: без подмены отпечатки прежних
    # квитанций остаются прежними и уже оплаченные блоки не заказываются второй раз.
    body = {"text": text, "template_id": template}
    if voice:
        body["voice_id"] = voice
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def api_scope(key: str) -> str:
    return hashlib.sha256(f"{BASE_URL}\0{key}".encode("utf-8")).hexdigest()


def read_receipt(mp3_path: str) -> dict:
    try:
        with open(receipt_path(mp3_path), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Lumean: квитанция {receipt_path(mp3_path)} повреждена ({e}); "
                           "новый заказ автоматически не создан") from None


def save_receipt(mp3_path: str, ident: str, key: str, state: str, order_id: str = "", status: str = "", **extra):
    # 27.09.2026: extra — sent_at/ended_at у «creating»: от них окно сверки «мог создаться»
    atomic_json(receipt_path(mp3_path), {"provider": "lumean", "identity": ident, "api_scope": api_scope(key),
                                         "state": state, "status": status, "order_id": order_id, **extra})


def _refunded(key: str, order_id: str) -> bool:
    """Живая проверка: заказ закрыт и за него ничего не списано.

    27.09.2026, аудит A2-TTS03: пустая сумма считалась нулём — ответ {"status": "failed"} без
    полей списания разрешал новый платный заказ. Возврат — только явный ноль: так Lumean отдаёт
    возвращённый заказ (живой GET 27.09: 29 compensated — total_tokens 0, tokens_spent null),
    а у оплаченных закрытых сумма есть (compensated 1575, cancelled 15 122). Иначе — «неясно».
    """
    order = _api_retry(key, "GET", f"/orders/{order_id}")
    status = str(order.get("status") or "").lower()
    суммы = [order.get(k) for k in ("tokens_spent", "total_tokens", "tokens_from_quota", "tokens_from_payg")]
    ноль = [type(v) in (int, float) and v == 0 for v in суммы]
    return status in TERMINAL_FAILURE and any(ноль[:2]) and all(v is None or z for v, z in zip(суммы, ноль))


def order_to_resume(block_idx: int, mp3_path: str, ident: str, key: str, log=print) -> str:
    """ID оплаченного заказа, который надо ждать, '' — можно заказывать, или ошибка."""
    receipt = read_receipt(mp3_path)
    where = f"блок {block_idx}: квитанция {receipt_path(mp3_path)}"
    if not receipt:
        return ""
    if receipt.get("state") == "creating" and not receipt.get("order_id"):
        raise RuntimeError(f"Lumean: {where} — прошлая отправка заказа осталась без ответа, заказ мог "
                           "создаться и оплатиться. Проверьте кабинет Lumean и удалите квитанцию; "
                           "новый заказ автоматически не создан")
    if receipt.get("state") == "manual_recovery":
        raise RuntimeError(f"Lumean: {where} — заказ {receipt.get('order_id')} требует ручного разбора в "
                           "кабинете Lumean; новый заказ автоматически не создан")
    if receipt.get("identity") != ident:
        return ""          # текст блока или шаблон изменились — нужен другой звук
    order_id = str(receipt.get("order_id") or "")
    if receipt.get("api_scope") != api_scope(key) and order_id:
        raise RuntimeError(f"Lumean: {where} — ключ сменился при заказе {order_id}; новый заказ "
                           "автоматически не создан")
    if receipt.get("state") == "terminal":
        if order_id and _refunded(key, order_id):
            log(f"  Lumean: блок {block_idx}: заказ {order_id} закрыт ({receipt.get('status')}), "
                "списано 0 — заказываю заново")
            return ""
        raise RuntimeError(f"Lumean: {where} — заказ {order_id} закрыт со статусом {receipt.get('status')} "
                           "и возврат не подтверждён (списаны токены или Lumean не назвал сумму — сверьте "
                           "в кабинете); новый заказ автоматически не создан. Если нужен "
                           "новый — удалите квитанцию")
    return order_id


# ---------- сверка «мог создаться» (27.09.2026) ----------
# Отправка без ответа (обрыв, 5xx) оставляет квитанцию «creating», и дальше была только ручная
# сверка в кабинете. 27.09 Lumean весь день отвечал на POST 503: каждая попытка — «creating»,
# а заказов за день на аккаунте 0. Сверяем, как человек, по списку заказов аккаунта.
# Полного текста Lumean не отдаёт ни в списке, ни в /orders/<id>, поэтому свой заказ узнаём по
# шаблону, name (начало текста), task_data.text_length (знаки) и files[0].size (байты UTF-8):
# живой замер 27.09 — 300 из 300 заказов студии совпали по всем четырём. Сверка — только GET,
# поэтому идёт и в общий перерыв: он про POST, а «не создан» снимает блок с ручного разбора.
SVERKA_WINDOW = _int_env("MANGA_LUMEAN_SVERKA_MINUTES", 10, 1) * 60   # сколько после отправки ждём заказ
SVERKA_SKEW = 120      # запас на расхождение часов студии и Lumean (created_at в UTC), с
SVERKA_PAGES = 2       # страниц списка по 100 заказов за проход; не хватило — «не знаю»
SVERKA_CLOCK = 60      # 27.09.2026: часы студии и Lumean (заголовок Date) расходятся больше — «не знаю»


def _clock_ok(headers: dict) -> bool:
    """Часы студии идут вровень с Lumean. Нет заголовка Date или расхождение больше SVERKA_CLOCK —
    False: окно сверки могло уйти мимо своего заказа, а «не создан» по ошибке = двойная оплата."""
    from email.utils import parsedate_to_datetime
    date = {str(k).lower(): v for k, v in (headers or {}).items()}.get("date")
    try:
        return abs(parsedate_to_datetime(date).timestamp() - _wall_clock()) <= SVERKA_CLOCK
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False


def _order_ts(o: dict) -> float:
    from datetime import datetime
    dt = datetime.fromisoformat(str(o.get("created_at") or "").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"created_at без часового пояса: {o.get('created_at')!r}")
    return dt.timestamp()


def _sverka_orders(key: str, lo: float, fresh_after: float, cache: dict) -> Optional[list]:
    """[(время, заказ)] аккаунта с created_at ≥ lo, новые первыми, или None — «не знаю»."""
    with cache["lock"]:
        if cache.get("failed"):
            return None                       # список в этом проходе уже не дался — не долбим
        if cache.get("at", 0) < fresh_after:  # взят до закрытия окна — мог не видеть заказ
            cache.update(at=_wall_clock(), items=[], page=0, last=1)
        try:
            while cache["page"] < min(cache["last"], SVERKA_PAGES) and not (
                    cache["items"] and cache["items"][-1][0] < lo):
                hdr: dict = {}
                data = _api(key, "GET", f"/orders?per_page=100&page={cache['page'] + 1}", hdr=hdr)
                if cache["page"] == 0 and not _clock_ok(hdr):
                    cache["failed"] = True        # часы разошлись с Lumean — окну не верим
                    return None
                cache["items"] += [(_order_ts(o), o) for o in data["items"]]
                cache["page"], cache["last"] = cache["page"] + 1, int(data["last_page"])
        except Exception:
            cache["failed"] = True
            raise
        times = [t for t, _ in cache["items"]]
        if times != sorted(times, reverse=True):
            return None                       # не «новые первыми» — окно по страницам не отрезать
        if cache["page"] < cache["last"] and not (times and times[-1] < lo):
            return None                       # до начала окна за SVERKA_PAGES страниц не дошли
        return [(t, o) for t, o in cache["items"] if t >= lo]


def _is_mine(o: dict, text: str, template: str) -> bool:
    td = o.get("task_data") if isinstance(o.get("task_data"), dict) else {}
    f = (td.get("files") or [None])[0]
    name = str(o.get("name") or "")
    return (bool(o.get("id")) and o.get("template_id") == template and td.get("text_length") == len(text)
            and isinstance(f, dict) and f.get("size") == len(text.encode("utf-8"))
            and bool(name) and text.startswith(name))


def reconcile_creating(block_idx: int, mp3_path: str, ident: str, key: str, text: str, template: str,
                       cache: dict, log=print) -> None:
    """Квитанция «creating» без номера заказа — сверка по списку заказов аккаунта.

    Окно не закрыто, ошибка GET, в окне чужие или два своих — ничего не меняем (дальше ручная
    сверка, как было). В окне ни одного заказа — отправка его не создала: квитанцию в сторону,
    блок закажется обычным порядком. Ровно один свой — берём его номер и ждём, нового не заказываем.
    """
    rp = receipt_path(mp3_path)
    try:
        r = read_receipt(mp3_path)
        if r.get("state") != "creating" or r.get("order_id") or r.get("api_scope") != api_scope(key):
            return
        t0 = float(r.get("sent_at") or os.path.getmtime(rp))   # старые квитанции — по времени файла
        lo = t0 - SVERKA_SKEW
        hi = float(r.get("ended_at") or t0) + SVERKA_WINDOW + SVERKA_SKEW
        if _wall_clock() < hi:
            log(f"  Lumean: блок {block_idx}: отправка без ответа — сверю список заказов после {_clock_text(hi)}")
            return
        orders = _sverka_orders(key, lo, hi, cache)
        if orders is None:
            log(f"  Lumean: блок {block_idx}: сверка «мог создаться» — список заказов не получен или не дочитан до окна, оставляю ручную")
            return
        window = {o.get("id"): o for t, o in orders if t <= hi}
        if not window:
            gone = f"{rp}.не_создан_{time.strftime('%Y%m%d_%H%M%S', time.localtime(_wall_clock()))}"
            os.rename(rp, gone)
            log(f"  Lumean: блок {block_idx}: сверка — с {_clock_text(lo)} по {_clock_text(hi)} на аккаунте ни "
                f"одного заказа, отправка его не создала; квитанция убрана в {os.path.basename(gone)}")
            return
        mine = [o for o in window.values() if r.get("identity") == ident and _is_mine(o, text, template)]
        if len(mine) == 1:
            save_receipt(mp3_path, ident, key, "created", str(mine[0]["id"]), sent_at=t0)
            log(f"  Lumean: блок {block_idx}: сверка — отправка без ответа всё же создала заказ {mine[0]['id']} "
                f"({mine[0].get('status')}); жду его, новый не заказываю")
            return
        log(f"  Lumean: блок {block_idx}: сверка — в окне отправки заказов {len(window)}, своих {len(mine)}: "
            "однозначно не узнать, оставляю ручную сверку")
    except Exception as e:
        log(f"  Lumean: блок {block_idx}: сверка «мог создаться» не удалась ({e}) — оставляю ручную")


def create_order(key: str, template: str, text: str, voice: str = "", sleep=time.sleep, log=print) -> str:
    # Тело как у VideoStudio (372 выполненных заказа HeyGen): формат mp3 фиксируется в заказе.
    body = {"template_id": template, "input_text": text,
            "config_override": {"output_settings": {"audio_format": "mp3"}}}
    if voice:
        body["config_override"]["tts_settings"] = {"voice_id": voice}
    for attempt in range(4):
        try:
            order = _api(key, "POST", "/orders", body)
            break
        except LumeanAPIError as e:
            # Явный 429 — отказ ДО создания заказа, повтор безопасен. Остальное — наверх.
            if e.status != 429 or attempt >= 3:
                raise
            # 27.09.2026: не дольше минуты — иначе последняя отправка уходит за окно сверки «мог создаться»
            delay = min(60, e.retry_after or POLL_SECONDS * 2 ** attempt)
            log(f"  Lumean: лимит создания заказов — повтор через {delay:g} с")
            sleep(delay)
    order_id = str(order.get("id") or "").strip()
    if not order_id:
        # 27.09.2026, аудит A2-TTS02: общий RuntimeError не включал рубильник — заглушка с таким
        # ответом получила 20 отправок из 20 блоков. Создан ли заказ, неизвестно — это обрыв.
        raise LumeanNetworkError("Lumean: POST /orders: ответ «успех» без номера заказа — создан ли "
                                 "заказ, неизвестно")
    return order_id


def wait_order(key: str, order_id: str, block_idx: int, clock=time.monotonic, sleep=time.sleep, log=print) -> dict:
    start = clock()
    last_beat = start
    last_status = None
    while clock() - start < MAX_WAIT_MINUTES * 60:
        try:
            order = _api(key, "GET", f"/orders/{order_id}")
        except Exception as e:
            if not _transient(e):
                raise
            log(f"  Lumean: блок {block_idx}: опрос заказа {order_id} не удался ({e}) — жду и повторяю")
            sleep(getattr(e, "retry_after", None) or POLL_SECONDS)
            continue
        status = str(order.get("status") or "").strip().lower()
        if status in SUCCESS:
            return order
        if status in TERMINAL_FAILURE:
            raise _Terminal(order_id, status)
        if status == "partially_completed" and any(
                isinstance(i, dict) and str(i.get("status") or "").lower() in ("failed", "policy_flagged")
                for i in (order.get("items") or [])):
            raise _Manual(f"Lumean: блок {block_idx}: заказ {order_id} выполнен частично (сбойные или "
                          "отклонённые куски) — повторите их в кабинете Lumean; новый заказ не создан")
        now = clock()
        if status != last_status or now - last_beat >= HEARTBEAT_SECONDS:
            log(f"  Lumean: блок {block_idx}: заказ {order_id} — {status or 'неизвестно'}, "
                f"{order.get('progress_percent') or 0}%, ждём {int((now - start) // 60)} мин")
            last_status, last_beat = status, now
        sleep(POLL_SECONDS)
    raise TimeoutError(f"Lumean: блок {block_idx}: заказ {order_id} не готов за {MAX_WAIT_MINUTES} мин; "
                       "квитанция сохранена — повтор шага продолжит ждать его, а не закажет заново")


class _Terminal(RuntimeError):
    def __init__(self, order_id, status):
        self.status = status
        super().__init__(f"Lumean: заказ {order_id} закрыт со статусом {status}")


class _Manual(RuntimeError):
    pass


def download(key: str, order: dict, mp3_path: str, sleep=time.sleep, log=print) -> None:
    order_id = order.get("id")
    if order.get("files_expired") is True:
        raise _Terminal(order_id, "files_expired")
    files = (order.get("result") or {}).get("files") or []
    if not files or not isinstance(files[0], str):
        raise RuntimeError(f"Lumean: заказ {order_id} готов, но без аудиофайла")
    link = _api_retry(key, "POST", "/storage/url", {"path": files[0], "download": True},
                      sleep=sleep, log=log).get("url")
    if not link:
        raise RuntimeError(f"Lumean: не получена ссылка на звук заказа {order_id}")
    part = mp3_path + ".part.mp3"
    last = None
    for attempt in range(3):
        try:
            status, _h, raw = _http("GET", link, {}, None, 300)
            if status >= 400:
                raise LumeanNetworkError(f"скачивание: HTTP {status}")
            with open(part, "wb") as f:
                f.write(raw)
            break
        except LumeanNetworkError as e:
            last = e
            log(f"  Lumean: скачивание звука заказа {order_id} не удалось ({e}) — попытка {attempt + 2} из 3")
            sleep(POLL_SECONDS)
    else:
        raise RuntimeError(f"Lumean: звук заказа {order_id} не скачан: {last}")
    if os.path.getsize(part) <= 2048:
        os.remove(part)
        raise RuntimeError(f"Lumean: скачан пустой звук заказа {order_id}")
    os.replace(part, mp3_path)


# ---------- блоки ----------
def block_ready(b: VO.Block, template: str, voice: str = "") -> bool:
    try:
        with open(b.path + ".voice.json", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return False
    return (state.get("fingerprint") == VO.voice_fingerprint(b.text, voice_id(template, voice), "+0%")
            and state.get("status") == "complete"
            and os.path.exists(b.path) and os.path.getsize(b.path) > 2048
            and VO.duration(b.path) >= 0.4 * (len(b.text.split()) / 2.5))


def synth(blocks: List[VO.Block], out_dir: str, lang: str, log: Callable = print,
          sleep=time.sleep, clock=time.monotonic) -> List[VO.Block]:
    key, template = preflight(lang)
    voice = voice_for(lang)
    too_long = [b.idx for b in blocks if len(b.text) > MAX_CHARS]
    if too_long:
        raise LumeanConfigError(f"Lumean: блоки {too_long[:10]} длиннее {MAX_CHARS} знаков; заказ не создан")
    os.makedirs(out_dir, exist_ok=True)
    for b in blocks:
        b.path = os.path.join(out_dir, f"block_{b.idx:04d}.mp3")
        b.words_path = ""          # меток слов у Lumean нет — тайминги по распознаванию речи
    todo = [b for b in blocks if not block_ready(b, template, voice)]
    # Рубильник прохода (27.09.2026, F04): после отказа сервиса новые POST не шлём.
    service_down = threading.Event()
    sverka = {"lock": threading.Lock()}   # 27.09.2026: список заказов для сверки «creating» — один на проход
    if todo:
        name = check_template(key, template)
        log(f"озвучка Lumean: шаблон «{name}»"
            + (f", голос {voice}" if voice else ", голос шаблона")
            + f", к заказу/ожиданию блоков {len(todo)} из {len(blocks)}")

    def one(b: VO.Block):
        ident = identity(b.text, template, voice)
        # 27.09.2026: «creating» без номера — сперва сверка по списку заказов, потом ручная
        reconcile_creating(b.idx, b.path, ident, key, b.text, template, sverka, log)
        order_id = order_to_resume(b.idx, b.path, ident, key, log)
        if order_id:
            log(f"  Lumean: блок {b.idx}: продолжаю ждать оплаченный заказ {order_id}")
        else:
            # Проверка стоит ДО квитанции: не начатый блок не оставляет «creating» и потом
            # закажется обычным порядком. Ожидание уже оплаченных заказов выше не трогаем.
            if service_down.is_set():
                raise _Skipped(f"блок {b.idx}: не заказан — сервис Lumean отказал в этом проходе")
            paused = pause_until(key)
            if paused:
                raise _Skipped(f"блок {b.idx}: не заказан — общий перерыв Lumean до {_clock_text(paused)} "
                               "после отказа сервиса; кончится сам, тогда повторите шаг")
            sent_at = _wall_clock()   # 27.09.2026: от времени отправки считается окно сверки
            save_receipt(b.path, ident, key, "creating", sent_at=sent_at)
            try:
                order_id = create_order(key, template, b.text, voice, sleep=sleep, log=log)
            except Exception as e:
                if _service_down(e) and not service_down.is_set():
                    service_down.set()
                    until = set_pause(key, e, log)
                    log(f"  Lumean: блок {b.idx}: сервис отказал ({e}) — новые заказы не отправляю"
                        + (f" до {_clock_text(until)} (общий перерыв всех роликов)" if until else "")
                        + "; уже отправленные жду")
                # Явный отказ 4xx — заказа нет, квитанцию убираем. Обрыв/5xx — неизвестно,
                # квитанция «creating» остаётся и не даст оплатить второй раз.
                if isinstance(e, LumeanAPIError) and e.status < 500 and e.status != 408:
                    os.remove(receipt_path(b.path))
                else:   # 27.09.2026: конец последней попытки — повторы после 429 сдвигают отправку
                    save_receipt(b.path, ident, key, "creating", sent_at=sent_at, ended_at=_wall_clock())
                raise RuntimeError(f"блок {b.idx}: заказ не создан: {e}") from e
            save_receipt(b.path, ident, key, "created", order_id)
            log(f"  Lumean: блок {b.idx}: заказ {order_id} создан ({len(b.text)} знаков)")
        atomic_json(b.path + ".voice.json", {"fingerprint": VO.voice_fingerprint(b.text, voice_id(template, voice), "+0%"),
                                             "status": "synthesizing"})
        try:
            order = wait_order(key, order_id, b.idx, clock=clock, sleep=sleep, log=log)
            download(key, order, b.path, sleep=sleep, log=log)
        except _Terminal as e:
            save_receipt(b.path, ident, key, "terminal", order_id, e.status)
            raise RuntimeError(f"блок {b.idx}: {e}") from e
        except _Manual as e:
            save_receipt(b.path, ident, key, "manual_recovery", order_id, "manual_recovery")
            raise
        if VO.duration(b.path) < 0.4 * (len(b.text.split()) / 2.5):
            raise RuntimeError(f"блок {b.idx}: звук заказа {order_id} слишком короткий: "
                               f"{VO.duration(b.path):.1f} с на {len(b.text.split())} слов")
        atomic_json(b.path + ".voice.json", {"fingerprint": VO.voice_fingerprint(b.text, voice_id(template, voice), "+0%"),
                                             "status": "complete", "order_id": order_id})

    if todo:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            errors = []
            for fut in [pool.submit(one, b) for b in todo]:
                try:
                    fut.result()
                except Exception as e:
                    errors.append(e)
        if errors:
            skipped = sum(isinstance(e, _Skipped) for e in errors)
            first = next((e for e in errors if not isinstance(e, _Skipped)), errors[0])
            raise RuntimeError(f"озвучка Lumean: не готово блоков {len(errors)}"
                               + (f" (из них {skipped} не заказаны: отказ сервиса или общий перерыв Lumean)"
                                  if skipped else "")
                               + f": {first}") from first
    for b in blocks:
        b.seconds = VO.duration(b.path)
        log(f"  блок {b.idx:3d}: {b.seconds:6.1f}s  {len(b.text.split()):4d} слов")
    return blocks
