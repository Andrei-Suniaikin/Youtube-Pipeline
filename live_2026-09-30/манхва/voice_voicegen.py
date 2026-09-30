# -*- coding: utf-8 -*-
"""Озвучка через VoiceGen (MANGA_TTS_ENGINE=voicegen).

Почему он. Замер 20.09.2026 на одном и том же тексте в 1000 знаков:
Lumean списал 519 токенов подписки за 40 с, VoiceGen сделал то же за 10 с, а его
пакет символов выходит вдвое дешевле на объёме ($1,25 против $2,76 за миллион знаков).
Голос тот же: VoiceGen принимает идентификатор голоса ElevenLabs напрямую.

Оплата и защита от двойной покупки — как у Lumean (voice_lumean.py):
- рядом с блоком лежит квитанция block_NNNN.mp3.voicegen.json. Повторный запуск шага
  с тем же текстом и голосом НЕ создаёт вторую задачу, а ждёт старую;
- отправка без ответа (обрыв, 5xx) оставляет квитанцию «creating»: дальше только
  ручной разбор в кабинете, потому что у POST /client/tasks нет защиты от дублей;
- ключ проверяется ДО любой задачи.

Две особенности провайдера, обе проверены живьём 20.09:
1. Cloudflare отбивает запросы без браузерного User-Agent (HTTP 403, error 1010),
   поэтому заголовок UA обязателен — без него не работает даже верный ключ.
2. На старых подписках (тариф astral) провайдер сам держит задачу до сброса
   суточного лимита знаков около 00:00 UTC. Это не зависание: статус остаётся
   queued с пометкой про дневной лимит, и после сброса задача идёт дальше.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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


def _float_env(name: str, default: float, low: float, high: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name}: нужно число, а не {raw!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{name}: от {low} до {high}")
    return value


BASE_URL = (os.environ.get("MANGA_VOICEGEN_API_BASE")
            or "https://api.qw1voicegencore.pro/api/v1").rstrip("/")
# Cloudflare сайта отбивает запросы без браузерного User-Agent — см. шапку модуля.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
POLL_SECONDS = _int_env("MANGA_VOICEGEN_POLL_SECONDS", 5, 1)
HEARTBEAT_SECONDS = _int_env("MANGA_VOICEGEN_HEARTBEAT_SECONDS", 120, 1)
# Задача может ждать сброса суточного лимита провайдера — ожидание до 8 часов.
MAX_WAIT_MINUTES = _int_env("MANGA_VOICEGEN_MAX_WAIT_MINUTES", 480, 1)
PARALLEL = _int_env("MANGA_VOICEGEN_PARALLEL", 5, 1)
# Блок ≈ 5 минут речи, как у Lumean: меньше швов интонации, чем у блоков edge по 55 с.
BLOCK_SECONDS = _int_env("MANGA_VOICEGEN_BLOCK_SECONDS", 300, 30)
MAX_CHARS = _int_env("MANGA_VOICEGEN_MAX_CHARS", 10000, 500)
# Сколько потоков провайдер даёт одной задаче (его же предел — 10 на аккаунт).
THREADS_PER_TASK = _int_env("MANGA_VOICEGEN_TASK_THREADS", 10, 1)
STABILITY = _float_env("MANGA_VOICEGEN_STABILITY", 0.5, 0.0, 1.0)
ENGINE = (os.environ.get("MANGA_VOICEGEN_ENGINE") or "elevenLabsV3").strip()
KEY_FILES = (Path.home() / "video_studio_api_keys.json", Path("/root/video_studio_api_keys.json"))
SUCCESS = {"done", "completed", "success"}
TERMINAL_FAILURE = {"failed", "error", "cancelled", "canceled"}
_HEX32 = re.compile(r"^[0-9a-f]{32}$", re.I)


class VoiceGenConfigError(RuntimeError):
    """Нет ключа или голоса — выясняется до задачи, символы не тронуты."""


class VoiceGenAPIError(RuntimeError):
    def __init__(self, status: int, action: str, payload: dict, retry_after: Optional[float] = None):
        self.status = int(status)
        self.retry_after = retry_after
        message = str(payload.get("detail") or payload.get("message") or payload.get("error")
                      or "неизвестная ошибка")[:300]
        super().__init__(f"VoiceGen: {action}: HTTP {self.status}: {message}")


class VoiceGenNetworkError(RuntimeError):
    pass


def voices_from_env() -> Dict[str, str]:
    """MANGA_VOICEGEN_VOICE — голос: один на все языки или JSON {"Russian": "<голос>"}.

    Значение — идентификатор голоса ElevenLabs (как A9ATT…, 20 знаков) либо номер
    голоса из каталога VoiceGen (32 шестнадцатеричных знака).
    """
    raw = (os.environ.get("MANGA_VOICEGEN_VOICE") or "").strip()
    if not raw:
        return {}
    value: object = raw
    if raw.startswith("{"):
        try:
            value = json.loads(raw)
        except ValueError:
            raise ValueError('MANGA_VOICEGEN_VOICE: нужен голос или JSON вида {"Russian": "<голос>"}') from None
    if isinstance(value, str):
        value = {"*": value.strip()}
    if not isinstance(value, dict):
        raise ValueError('MANGA_VOICEGEN_VOICE: нужен голос или JSON вида {"Russian": "<голос>"}')
    bad = [k for k, v in value.items()
           if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,64}", v.strip())]
    if bad:
        raise ValueError(f"MANGA_VOICEGEN_VOICE: не похоже на голос у языков {', '.join(map(str, bad))}")
    return {str(k): v.strip() for k, v in value.items()}


def voice_for(lang: str) -> str:
    voices = voices_from_env()
    return voices.get(lang) or voices.get("*") or ""


voices_from_env()      # битая настройка останавливает запуск службы с понятной причиной


def api_key() -> str:
    for name in ("VOICEGEN_API_KEY", "MANGA_VOICEGEN_KEY", "VGC_API_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    for path in KEY_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                value = str(json.load(f).get("voicegencore") or "").strip()
        except (OSError, ValueError, AttributeError):
            continue
        if value:
            return value
    return ""


def preflight(lang: str) -> Tuple[str, str]:
    """(ключ, голос) для языка проекта или понятная ошибка до любой задачи."""
    key = api_key()
    if not key:
        raise VoiceGenConfigError(
            "VoiceGen: нет ключа — задайте VOICEGEN_API_KEY в web.env или поле \"voicegencore\" в "
            + " / ".join(str(p) for p in KEY_FILES) + "; задача не создана")
    voice = voice_for(lang)
    if not voice:
        raise VoiceGenConfigError(
            f"VoiceGen: нет голоса для языка {lang} — задайте MANGA_VOICEGEN_VOICE; задача не создана")
    return key, voice


def voice_id(voice: str, engine: str = "") -> str:
    """Что стоит на месте голоса edge в отпечатке озвучки."""
    return "voicegen:" + (engine or ENGINE) + ":" + voice


def voice_field(voice: str) -> dict:
    """Номер каталога VoiceGen (32 знака) и голос ElevenLabs кладутся в РАЗНЫЕ поля."""
    return {"voice_id": voice} if _HEX32.match(voice) else {"elevenlabs_voice_id": voice}


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
        raise VoiceGenNetworkError(f"VoiceGen: нет связи: {e}") from e


def _headers(key: str) -> dict:
    return {"X-API-Key": key, "Accept": "application/json", "User-Agent": USER_AGENT}


def _api(key: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 180) -> dict:
    headers = _headers(key)
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status, resp_headers, raw = _http(method, BASE_URL + path, headers, data, timeout)
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if status >= 400:
        retry_after = resp_headers.get("Retry-After")
        try:
            retry_after = max(0.0, float(retry_after)) if retry_after not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise VoiceGenAPIError(status, f"{method} {path.split('?')[0]}", payload, retry_after)
    return payload


def _transient(e: Exception) -> bool:
    return isinstance(e, VoiceGenNetworkError) or (
        isinstance(e, VoiceGenAPIError) and (e.status == 429 or e.status >= 500))


def _api_retry(key, method, path, body=None, attempts=4, sleep=time.sleep, log=print):
    """Повторы только для бесплатных запросов (опрос, скачивание). Не для создания задачи."""
    for attempt in range(attempts):
        try:
            return _api(key, method, path, body)
        except Exception as e:
            if not _transient(e) or attempt >= attempts - 1:
                raise
            delay = getattr(e, "retry_after", None) or min(60, POLL_SECONDS * 2 ** attempt)
            log(f"  VoiceGen: временная ошибка ({e}) — повтор через {delay:g} с")
            sleep(delay)


def check_key(key: str) -> str:
    """Бесплатный GET: ключ принят, подписка жива. Возвращает строку про тариф для журнала."""
    try:
        data = _api_retry(key, "GET", "/client/me")
    except VoiceGenAPIError as e:
        if e.status in (401, 403):
            raise VoiceGenConfigError(f"VoiceGen: ключ отвергнут (HTTP {e.status}); задача не создана") from None
        raise
    if data.get("is_blocked"):
        raise VoiceGenConfigError("VoiceGen: аккаунт заблокирован провайдером; задача не создана")
    package = data.get("character_package") or {}
    if not data.get("has_active_subscription") and not package.get("active"):
        raise VoiceGenConfigError("VoiceGen: нет активной подписки и пакета символов; задача не создана")
    plan = str(data.get("current_plan") or "?")
    left = package.get("remaining") if package.get("active") else None
    return f"тариф {plan}" + (f", в пакете осталось {left} знаков" if isinstance(left, int) else "")


# ---------- квитанции ----------
def receipt_path(mp3_path: str) -> str:
    return mp3_path + ".voicegen.json"


def identity(text: str, voice: str, engine: str = "") -> str:
    raw = json.dumps({"text": text, "voice": voice, "engine": engine or ENGINE},
                     ensure_ascii=False, sort_keys=True)
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
        raise RuntimeError(f"VoiceGen: квитанция {receipt_path(mp3_path)} повреждена ({e}); "
                           "новая задача автоматически не создана") from None


def save_receipt(mp3_path: str, ident: str, key: str, state: str, task_id: str = "", status: str = ""):
    atomic_json(receipt_path(mp3_path), {"provider": "voicegen", "identity": ident,
                                         "api_scope": api_scope(key), "state": state,
                                         "status": status, "task_id": task_id})


def task_to_resume(block_idx: int, mp3_path: str, ident: str, key: str, log=print) -> str:
    """ID созданной задачи, которую надо ждать, '' — можно создавать, или ошибка."""
    receipt = read_receipt(mp3_path)
    where = f"блок {block_idx}: квитанция {receipt_path(mp3_path)}"
    if not receipt:
        return ""
    if receipt.get("state") == "creating" and not receipt.get("task_id"):
        raise RuntimeError(f"VoiceGen: {where} — прошлая отправка осталась без ответа, задача могла "
                           "создаться и списать символы. Проверьте кабинет VoiceGen и удалите квитанцию; "
                           "новая задача автоматически не создана")
    if receipt.get("identity") != ident:
        return ""          # текст блока или голос изменились — нужен другой звук
    task_id = str(receipt.get("task_id") or "")
    if receipt.get("api_scope") != api_scope(key) and task_id:
        raise RuntimeError(f"VoiceGen: {where} — ключ сменился при задаче {task_id}; новая задача "
                           "автоматически не создана")
    if receipt.get("state") == "terminal":
        log(f"  VoiceGen: блок {block_idx}: задача {task_id} закрыта ({receipt.get('status')}) — создаю заново")
        return ""
    return task_id


def create_task(key: str, text: str, voice: str, idx: int, sleep=time.sleep, log=print) -> str:
    body = {"text": text, **voice_field(voice), "voice_engine": ENGINE,
            "voice_engine_settings": {"stability": STABILITY},
            "filename": f"block_{idx:04d}.mp3", "thread_count": THREADS_PER_TASK, "auto_start": True}
    for attempt in range(4):
        try:
            data = _api(key, "POST", "/client/tasks", body)
            break
        except VoiceGenAPIError as e:
            # Явный 429 — отказ ДО создания задачи, повтор безопасен. Остальное — наверх.
            if e.status != 429 or attempt >= 3:
                raise
            delay = e.retry_after or min(60, POLL_SECONDS * 2 ** attempt)
            log(f"  VoiceGen: лимит создания задач — повтор через {delay:g} с")
            sleep(delay)
    task = data.get("task") if isinstance(data.get("task"), dict) else data
    task_id = str(task.get("task_id") or task.get("id") or "").strip()
    if not task_id:
        raise RuntimeError("VoiceGen: задача создана без номера")
    return task_id


def _task_of(payload: dict) -> dict:
    task = payload.get("task")
    return task if isinstance(task, dict) else payload


def _message(task: dict) -> str:
    return str(task.get("status_message") or task.get("message") or task.get("detail") or "")


def wait_task(key: str, task_id: str, block_idx: int, clock=time.monotonic, sleep=time.sleep,
              log=print) -> dict:
    start = clock()
    last_beat = start
    last_status = None
    while clock() - start < MAX_WAIT_MINUTES * 60:
        try:
            task = _task_of(_api(key, "GET", f"/client/tasks/{task_id}"))
        except Exception as e:
            if not _transient(e):
                raise
            log(f"  VoiceGen: блок {block_idx}: опрос задачи {task_id} не удался ({e}) — жду и повторяю")
            sleep(getattr(e, "retry_after", None) or POLL_SECONDS)
            continue
        status = str(task.get("status") or "").strip().lower()
        if status in SUCCESS:
            return task
        if status in TERMINAL_FAILURE:
            raise _Terminal(task_id, status, _message(task))
        now = clock()
        if status != last_status or now - last_beat >= HEARTBEAT_SECONDS:
            why = _message(task)
            # Ожидание суточного лимита — не зависание: провайдер сам вернётся к задаче.
            note = " (ждём сброса суточного лимита провайдера)" if "лимит" in why.lower() else ""
            log(f"  VoiceGen: блок {block_idx}: задача {task_id} — {status or '?'}, "
                f"{task.get('progress') or 0:.0f}%{note}, ждём {(now - start) / 60:.0f} мин")
            last_status, last_beat = status, now
        sleep(POLL_SECONDS)
    raise RuntimeError(f"VoiceGen: блок {block_idx}: задача {task_id} не завершилась за "
                       f"{MAX_WAIT_MINUTES} мин; символы могли списаться — проверьте кабинет")


class _Terminal(RuntimeError):
    def __init__(self, task_id, status, message=""):
        self.task_id, self.status = task_id, status
        super().__init__(f"задача {task_id} закрыта со статусом {status}"
                         + (f": {message[:200]}" if message else ""))


def download(key: str, task: dict, mp3_path: str, log=print) -> None:
    url = str(task.get("download_url") or "").strip()
    if not url:
        raise RuntimeError(f"VoiceGen: у готовой задачи {task.get('task_id')} нет ссылки на файл")
    if url.startswith("/api/v1"):
        url = BASE_URL.rsplit("/api/v1", 1)[0] + url
    elif url.startswith("/"):
        url = BASE_URL + url
    for attempt in range(4):
        status, _h, raw = _http("GET", url, _headers(key), None, 300)
        if status == 200 and len(raw) > 2048:
            tmp = mp3_path + ".part"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, mp3_path)
            return
        if attempt >= 3:
            raise RuntimeError(f"VoiceGen: файл задачи не скачался (HTTP {status}, {len(raw)} байт)")
        log(f"  VoiceGen: файл не отдался (HTTP {status}) — повтор")
        time.sleep(POLL_SECONDS)


# ---------- синтез ----------
def block_ready(b: VO.Block, voice: str) -> bool:
    try:
        with open(b.path + ".voice.json", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return False
    return (state.get("fingerprint") == VO.voice_fingerprint(b.text, voice_id(voice), "+0%")
            and state.get("status") == "complete"
            and os.path.exists(b.path) and os.path.getsize(b.path) > 2048
            and VO.duration(b.path) >= 0.4 * (len(b.text.split()) / 2.5))


def synth(blocks: List[VO.Block], out_dir: str, lang: str, log: Callable = print,
          sleep=time.sleep, clock=time.monotonic) -> List[VO.Block]:
    key, voice = preflight(lang)
    too_long = [b.idx for b in blocks if len(b.text) > MAX_CHARS]
    if too_long:
        raise VoiceGenConfigError(f"VoiceGen: блоки {too_long[:10]} длиннее {MAX_CHARS} знаков; задача не создана")
    os.makedirs(out_dir, exist_ok=True)
    for b in blocks:
        b.path = os.path.join(out_dir, f"block_{b.idx:04d}.mp3")
        b.words_path = ""          # меток слов провайдер не даёт — тайминги по распознаванию речи
    todo = [b for b in blocks if not block_ready(b, voice)]
    if todo:
        about = check_key(key)
        log(f"озвучка VoiceGen: голос {voice}, движок {ENGINE} ({about}); "
            f"к созданию/ожиданию блоков {len(todo)} из {len(blocks)}")

    def one(b: VO.Block):
        ident = identity(b.text, voice)
        task_id = task_to_resume(b.idx, b.path, ident, key, log)
        if task_id:
            log(f"  VoiceGen: блок {b.idx}: продолжаю ждать созданную задачу {task_id}")
        else:
            save_receipt(b.path, ident, key, "creating")
            try:
                task_id = create_task(key, b.text, voice, b.idx, sleep=sleep, log=log)
            except Exception as e:
                # Явный отказ 4xx — задачи нет, квитанцию убираем. Обрыв/5xx — неизвестно,
                # квитанция «creating» остаётся и не даст оплатить второй раз.
                if isinstance(e, VoiceGenAPIError) and e.status < 500 and e.status != 408:
                    os.remove(receipt_path(b.path))
                raise RuntimeError(f"блок {b.idx}: задача не создана: {e}") from e
            save_receipt(b.path, ident, key, "created", task_id)
            log(f"  VoiceGen: блок {b.idx}: задача {task_id} создана ({len(b.text)} знаков)")
        atomic_json(b.path + ".voice.json", {"fingerprint": VO.voice_fingerprint(b.text, voice_id(voice), "+0%"),
                                             "status": "synthesizing"})
        try:
            task = wait_task(key, task_id, b.idx, clock=clock, sleep=sleep, log=log)
            download(key, task, b.path, log=log)
        except _Terminal as e:
            save_receipt(b.path, ident, key, "terminal", task_id, e.status)
            raise RuntimeError(f"блок {b.idx}: {e}") from e
        if VO.duration(b.path) < 0.4 * (len(b.text.split()) / 2.5):
            raise RuntimeError(f"блок {b.idx}: звук задачи {task_id} слишком короткий: "
                               f"{VO.duration(b.path):.1f} с на {len(b.text.split())} слов")
        atomic_json(b.path + ".voice.json", {"fingerprint": VO.voice_fingerprint(b.text, voice_id(voice), "+0%"),
                                             "status": "complete", "task_id": task_id})

    if todo:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            errors = []
            for fut in [pool.submit(one, b) for b in todo]:
                try:
                    fut.result()
                except Exception as e:
                    errors.append(str(e))
        if errors:
            raise RuntimeError(f"озвучка VoiceGen: не готово блоков {len(errors)}: " + "; ".join(errors[:3]))
    return blocks
