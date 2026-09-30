# -*- coding: utf-8 -*-
"""Озвучка через FastGen (MANGA_TTS_ENGINE=fastgen): модель Gemini TTS в подписке FastGen.

Почему он. С 27.09 Lumean лежит. FastGen озвучивает на том же ключе, что и картинки
(keys.json), и без оплаты за знак: заказ стоит 10 «единиц запросов» из 600 в час — то есть
60 заказов в час на ключ, до 20 одновременно (поток speech; картинки его не трогают).
Замер 29.09.2026 (/root/fixlab4/fgtts): 125 слов — 37 с ожидания, 1300 слов — 4,6 мин
ожидания и 9,7 мин речи одним заказом; темп голоса charon: русский 124–135 сл/мин,
испанский 142, польский 136.

Как устроено — как у VoiceGen и Lumean (voice_voicegen.py, voice_lumean.py):
- блок ≈ 7 минут речи по темпу проекта (MANGA_FASTGEN_TTS_BLOCK_SECONDS) = один заказ,
  это ~830–1250 слов: меньше заказов в часовой лимит и меньше швов интонации;
- рядом с блоком квитанция block_NNNN.mp3.fastgen.json: отпечаток текста, модели, голоса и
  инструкции стиля плюс номер генерации. Повтор шага готовый блок не заказывает, а уже
  отправленный ждёт по его номеру, а не создаёт новый;
- FastGen отдаёт wav 24 кГц — перекодируем в mp3 44,1 кГц 128 кбит/с моно, ровно как блоки
  Lumean, чтобы склейка без пережатия и сборка шли прежним путём;
- меток слов нет — тайминги кадров по распознаванию речи (step_align → whisper), как у Lumean.

Чем отличается от Lumean. Там заказ стоит денег, и отправка без ответа ведёт к ручной сверке.
Здесь лишний заказ — это 10 единиц часа из 600, а списка генераций у FastGen нет (сверять не с
чем). Поэтому квитанция «отправка без ответа» после общего перерыва снимается сама и блок
заказывается заново: худший случай — один лишний заказ в подписке.

Лимиты:
- одновременных заказов на всю службу — MANGA_FASTGEN_TTS_PARALLEL (4): при ~4 мин на заказ
  это ~60 заказов в час, ровно часовой лимит ключа, и волна не упирается в отказы;
- 429 «час исчерпан» (проверяется бесплатным GET /usage) — ждём начала следующего часа UTC и
  повторяем. 429 — отказ до создания, повтор безопасен;
- 429 «все потоки заняты» — короткое ожидание и повтор;
- отказ сервиса (5xx, обрыв, отказ ключа 401/403) — новые заказы в этом проходе не шлём, и
  общий перерыв для всех роликов (файл fastgen_tts_pause_*.json рядом с projects_web).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import images as IM
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


DEFAULT_MODEL = "aistudio/gemini-3.8-flash-tts"
DEFAULT_VOICE = "charon"      # мужской, ровный; замерен на русском, испанском и польском 29.09
DEFAULT_SCENE = ("A calm storyteller records a manhwa recap for YouTube in a quiet studio. "
                 "Lively and engaged, natural conversational pacing, clear diction. "
                 "No theatrical acting, no shouting, no dramatic character voices.")
# Хост и ключ — те же, что у картинок (images.BASE, images.key: keys.json или FASTGEN_KEY).
BASE_URL = (os.environ.get("MANGA_FASTGEN_TTS_API_BASE") or IM.BASE).rstrip("/")
MODEL = (os.environ.get("MANGA_FASTGEN_TTS_MODEL") or DEFAULT_MODEL).strip()
_scene = (os.environ.get("MANGA_FASTGEN_TTS_SCENE") or "").strip()
# Пусто — спокойный живой рассказчик; off — без инструкции стиля.
SCENE = "" if _scene.lower() in ("off", "none", "-") else (_scene or DEFAULT_SCENE)
POLL_SECONDS = _int_env("MANGA_FASTGEN_TTS_POLL_SECONDS", 5, 1)
HEARTBEAT_SECONDS = _int_env("MANGA_FASTGEN_TTS_HEARTBEAT_SECONDS", 120, 1)
# Готовая генерация хранится у FastGen 40 минут (expires_at): ждём меньше, дальше — повтор шага.
MAX_WAIT_MINUTES = _int_env("MANGA_FASTGEN_TTS_MAX_WAIT_MINUTES", 30, 1)
PARALLEL = _int_env("MANGA_FASTGEN_TTS_PARALLEL", 4, 1)
# 7 минут по темпу проекта (pace.json): русский 119–135 сл/мин → 830–950 слов, у переводов темп
# записан старый языковой (румынский 179, испанский 167) → до ~1250 слов. FastGen читает медленнее
# этих темпов (чешский 130, румынский 150), и настоящий звук блока выходит 7–8,5 мин. Заказ в
# 1300 слов (9,7 мин речи) прошёл 29.09, дальше не пробовали — 8 минут по темпу проекта у чешского
# дали бы как раз 9,7 мин, поэтому 7 с запасом.
BLOCK_SECONDS = _int_env("MANGA_FASTGEN_TTS_BLOCK_SECONDS", 420, 30)
MAX_CHARS = _int_env("MANGA_FASTGEN_TTS_MAX_CHARS", 9000, 500)
PAUSE_MINUTES = _int_env("MANGA_FASTGEN_TTS_PAUSE_MINUTES", 15, 1)
# Сколько один блок готов ждать освобождения часового лимита ключа (час сбрасывается в :00 UTC).
QUOTA_WAIT_MINUTES = _int_env("MANGA_FASTGEN_TTS_QUOTA_WAIT_MINUTES", 90, 1)
ATTEMPTS = 2          # заказов на блок за проход: второй — только если первый закрылся неудачей
CREDITS = 10          # единиц часа за заказ (каталог /models, 29.09)
# Звук не похож на текст: быстрее 200 сл/мин — обрубок, медленнее 60 — тишина или повторы.
FASTEST_WPM, SLOWEST_WPM = 200.0, 60.0
SUCCESS = {"succeeded"}
TERMINAL_FAILURE = {"failed", "cancelled", "canceled", "expired"}
_VOICE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
KEY_HINT = "keys.json (поле fastgen.key) или FASTGEN_KEY — тот же ключ, что у картинок"


class FastGenTTSConfigError(RuntimeError):
    """Нет ключа, голоса или лимита речи — выясняется до заказа."""


class FastGenTTSAPIError(RuntimeError):
    def __init__(self, status: int, action: str, payload: dict, retry_after: Optional[float] = None):
        self.status = int(status)
        self.retry_after = retry_after
        self.payload = payload
        detail = payload.get("detail") if isinstance(payload.get("detail"), (str, dict, list)) else None
        message = str(payload.get("message") or payload.get("error") or detail or payload or "неизвестная ошибка")
        super().__init__(f"FastGen: {action}: HTTP {self.status}: {message[:300]}")


class FastGenTTSNetworkError(RuntimeError):
    pass


def voices_from_env() -> Dict[str, str]:
    """MANGA_FASTGEN_TTS_VOICE — голос: один на все языки ("charon") или JSON
    {"Russian": "charon", "Spanish": "algieba", "*": "charon"}. Пусто — charon."""
    raw = (os.environ.get("MANGA_FASTGEN_TTS_VOICE") or "").strip()
    if not raw:
        return {}
    value: object = raw
    if raw.startswith("{"):
        try:
            value = json.loads(raw)
        except ValueError:
            raise ValueError('MANGA_FASTGEN_TTS_VOICE: нужен голос или JSON вида {"Russian": "charon"}') from None
    if isinstance(value, str):
        value = {"*": value}
    if not isinstance(value, dict):
        raise ValueError('MANGA_FASTGEN_TTS_VOICE: нужен голос или JSON вида {"Russian": "charon"}')
    bad = [k for k, v in value.items() if not isinstance(v, str) or not _VOICE.match(v.strip().lower())]
    if bad:
        raise ValueError(f"MANGA_FASTGEN_TTS_VOICE: не похоже на голос у языков {', '.join(map(str, bad))}")
    return {str(k): v.strip().lower() for k, v in value.items()}


def voice_for(lang: str) -> str:
    voices = voices_from_env()
    return voices.get(lang) or voices.get("*") or DEFAULT_VOICE


voices_from_env()      # битая настройка останавливает запуск службы с понятной причиной


def api_key() -> str:
    try:
        return str(IM.key() or "").strip()
    except Exception:  # noqa: BLE001 — нет файла/поля: ниже понятная ошибка до заказа
        return ""


def preflight(lang: str) -> Tuple[str, str]:
    """(ключ, голос) для языка проекта или понятная ошибка до любого заказа (без сети)."""
    key = api_key()
    if not key:
        raise FastGenTTSConfigError(f"FastGen: нет ключа — нужен {KEY_HINT}; заказ не создан")
    return key, voice_for(lang)


def scene_mark(scene: str = None) -> str:
    scene = SCENE if scene is None else scene
    return hashlib.sha256(scene.encode("utf-8")).hexdigest()[:8] if scene else ""


def voice_id(voice: str) -> str:
    """Что стоит на месте голоса edge в отпечатке озвучки: модель, голос и инструкция стиля.
    Сменили любое — прежние блоки не годятся и заказываются заново, а не подмешиваются."""
    mark = scene_mark()
    return f"fastgen:{MODEL}:{voice}" + (f":{mark}" if mark else "")


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
        raise FastGenTTSNetworkError(f"FastGen: нет связи: {e}") from e


def _api(key: str, method: str, path: str, body: Optional[dict] = None, timeout: float = 60) -> dict:
    headers = {"X-API-Key": key, "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status, resp_headers, raw = _http(method, BASE_URL + path, headers, data, timeout)
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {"error": raw[:200].decode("utf-8", "replace")} if raw else {}
    payload = payload if isinstance(payload, dict) else {"data": payload}
    if status >= 400:
        retry_after = {str(k).lower(): v for k, v in (resp_headers or {}).items()}.get("retry-after")
        try:
            retry_after = max(0.0, float(retry_after)) if retry_after not in (None, "") else None
        except (TypeError, ValueError):
            retry_after = None
        raise FastGenTTSAPIError(status, f"{method} {path.split('?')[0]}", payload, retry_after)
    return payload


def _transient(e: Exception) -> bool:
    return isinstance(e, FastGenTTSNetworkError) or (
        isinstance(e, FastGenTTSAPIError) and (e.status in (408, 429) or e.status >= 500))


def _service_down(e: Exception) -> bool:
    """Отказ всего сервиса, а не одного текста: следующий заказ с тем же ключом получит то же."""
    if isinstance(e, FastGenTTSNetworkError):
        return True
    return isinstance(e, FastGenTTSAPIError) and (e.status >= 500 or e.status in (401, 403, 408))


def _api_retry(key, method, path, body=None, attempts=4, sleep=time.sleep, log=print):
    """Повторы только для бесплатных запросов (опрос, /usage, /models). Не для заказа."""
    for attempt in range(attempts):
        try:
            return _api(key, method, path, body)
        except Exception as e:
            if not _transient(e) or attempt >= attempts - 1:
                raise
            delay = getattr(e, "retry_after", None) or min(60, POLL_SECONDS * 2 ** attempt)
            log(f"  FastGen: временная ошибка ({e}) — повтор через {delay:g} с")
            sleep(delay)


def usage(key: str, sleep=time.sleep, log=print) -> dict:
    """Бесплатный GET /usage: лимит единиц часа, израсходовано, начало часа, потоки речи."""
    data = _api_retry(key, "GET", "/usage", sleep=sleep, log=log)
    limits = data.get("account_limits") or {}
    cur = data.get("current_usage") or {}
    hour = (cur.get("hourly_usage") or {}).get("aistudio_request_credit") or {}
    now = _wall_clock()
    return {"limit": int(limits.get("aistudio_request_credits_per_hour_limit") or 0),
            "threads": int(limits.get("speech_threads_allowed") or 0),
            "used": int(hour.get("current_usage") or 0),
            "window_start": float(hour.get("window_start") or (now - now % 3600)),
            "busy": int((cur.get("active_threads") or {}).get("speech_threads") or 0)}


def check_key(key: str, voice: str, sleep=time.sleep, log=print) -> str:
    """Бесплатные GET: ключ принят, у него есть лимит речи, модель знает этот голос."""
    try:
        u = usage(key, sleep=sleep, log=log)
    except FastGenTTSAPIError as e:
        if e.status in (401, 403):
            raise FastGenTTSConfigError(f"FastGen: ключ отвергнут (HTTP {e.status}); заказ не создан") from None
        raise
    if u["limit"] < CREDITS or u["threads"] < 1:
        raise FastGenTTSConfigError(f"FastGen: у ключа нет лимита речи (единиц в час {u['limit']}, "
                                    f"потоков {u['threads']}); заказ не создан")
    try:
        models = _api_retry(key, "GET", "/models", sleep=sleep, log=log)
        items = models.get("data") if isinstance(models.get("data"), list) else models.get("models") or []
        card = next((m for m in items if isinstance(m, dict) and m.get("id") == MODEL), None)
    except Exception as e:  # noqa: BLE001 — каталог не главное: без него голос проверит сам заказ
        log(f"  FastGen: каталог моделей не прочитан ({e}) — голос не сверяю")
        card = {}
    if card is None:
        raise FastGenTTSConfigError(f"FastGen: модели {MODEL} нет в каталоге — проверьте "
                                    "MANGA_FASTGEN_TTS_MODEL; заказ не создан")
    allowed = (((card or {}).get("options") or {}).get("voice") or {}).get("enum") or []
    if allowed and voice not in allowed:
        raise FastGenTTSConfigError(f"FastGen: модель {MODEL} не знает голос {voice} — проверьте "
                                    "MANGA_FASTGEN_TTS_VOICE; заказ не создан")
    left = max(0, u["limit"] - u["used"]) // CREDITS
    return (f"лимит {u['limit'] // CREDITS} заказов в час, в этом часе осталось {left}, "
            f"потоков речи занято {u['busy']} из {u['threads']}")


# ---------- общий перерыв после отказа сервиса (как у Lumean, voice_lumean.pause_until) ----------
_wall_clock = time.time    # настенные часы: общие для процессов; подменяется в тестах


def api_scope(key: str) -> str:
    return hashlib.sha256(f"{BASE_URL}\0{key}".encode("utf-8")).hexdigest()


def pause_path(key: str) -> Path:
    root = os.environ.get("MANGA_ROOT") or str(Path(__file__).resolve().parents[1] / "projects_web")
    return Path(root) / f"fastgen_tts_pause_{api_scope(key)[:16]}.json"


def _clock_text(ts: float) -> str:
    return time.strftime("%d.%m %H:%M:%S", time.localtime(ts))


def pause_until(key: str) -> float:
    """До какого момента новые заказы не создаются; 0 — перерыва нет. Дальше суток — не перерыв."""
    try:
        with open(pause_path(key), encoding="utf-8") as f:
            until = float(json.load(f).get("until") or 0)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return 0.0
    now = _wall_clock()
    if until != until or until > now + 24 * 3600:
        return 0.0
    return until if until > now else 0.0


def set_pause(key: str, e: Exception, log=print) -> float:
    seconds = PAUSE_MINUTES * 60
    retry_after = getattr(e, "retry_after", None)
    if retry_after:
        seconds = max(seconds, min(float(retry_after), 24 * 3600))
    until = max(_wall_clock() + seconds, pause_until(key))
    path = pause_path(key)
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
        log(f"  FastGen: общий перерыв не записан ({err}) — действует только рубильник этого прохода")
        return 0.0
    return until


# ---------- часовой лимит ключа (общий на службу) ----------
_SLOTS = threading.BoundedSemaphore(PARALLEL)
_QUOTA_LOCK = threading.Lock()
_QUOTA = {"until": 0.0}


class _Skipped(RuntimeError):
    """Блок не заказан: рубильник, общий перерыв или лимит часа. Квитанции нет, заказа нет."""


def _is_busy(e: FastGenTTSAPIError) -> bool:
    text = json.dumps(e.payload, ensure_ascii=False).lower()
    return "concurrency" in text or "thread" in text or "concurrent" in text


def _after_429(key: str, e: FastGenTTSAPIError, attempt: int, sleep, log) -> float:
    """Сколько ждать перед повтором после 429. Час исчерпан — только если /usage это подтвердил."""
    short = e.retry_after or min(60.0, float(POLL_SECONDS * 2 ** min(attempt, 4)))
    if _is_busy(e):
        log(f"  FastGen: все потоки речи заняты — повтор через {short:g} с")
        return short
    try:
        u = usage(key, sleep=sleep, log=log)
    except Exception as err:  # noqa: BLE001
        log(f"  FastGen: 429, а /usage не прочитан ({err}) — повтор через {short:g} с")
        return short
    if u["used"] + CREDITS > u["limit"]:
        until = u["window_start"] + 3600 + 5
        with _QUOTA_LOCK:
            _QUOTA["until"] = max(_QUOTA["until"], until)
        log(f"  FastGen: лимит часа исчерпан ({u['used']} из {u['limit']} единиц) — жду до "
            f"{_clock_text(until)}, заказ не создан")
        return max(1.0, until - _wall_clock())
    log(f"  FastGen: 429 ({e}), по /usage час не исчерпан ({u['used']} из {u['limit']}) — "
        f"повтор через {short:g} с")
    return short


def _wait_quota(deadline: float, block_idx: int, sleep, log) -> None:
    """Другой поток уже упёрся в лимит часа — не стучимся, ждём его конца."""
    until = _QUOTA["until"]
    now = _wall_clock()
    if until <= now:
        return
    if until > deadline:
        raise _Skipped(f"блок {block_idx}: не заказан — лимит часа FastGen до {_clock_text(until)}, "
                       f"дольше {QUOTA_WAIT_MINUTES} мин ожидания; повторите шаг")
    log(f"  FastGen: блок {block_idx}: жду лимит часа до {_clock_text(until)}")
    sleep(until - now)


# ---------- квитанции ----------
def receipt_path(mp3_path: str) -> str:
    return mp3_path + ".fastgen.json"


def identity(text: str, voice: str, model: str = None, scene: str = None) -> str:
    raw = json.dumps({"text": text, "model": model or MODEL, "voice": voice,
                      "scene": SCENE if scene is None else scene}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_receipt(mp3_path: str) -> dict:
    try:
        with open(receipt_path(mp3_path), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RuntimeError(f"FastGen: квитанция {receipt_path(mp3_path)} повреждена ({e}); "
                           "удалите её — новый заказ автоматически не создан") from None


def save_receipt(mp3_path: str, ident: str, key: str, state: str, generation_id: str = "",
                 status: str = "", **extra):
    atomic_json(receipt_path(mp3_path), {"provider": "fastgen", "model": MODEL, "identity": ident,
                                         "api_scope": api_scope(key), "state": state, "status": status,
                                         "generation_id": generation_id, **extra})


def _drop_receipt(mp3_path: str) -> None:
    try:
        os.remove(receipt_path(mp3_path))
    except FileNotFoundError:
        pass


def generation_to_resume(block_idx: int, mp3_path: str, ident: str, key: str, log=print) -> str:
    """Номер отправленной генерации, которую надо ждать; '' — можно заказывать; или _Skipped."""
    receipt = read_receipt(mp3_path)
    if not receipt:
        return ""
    gid = str(receipt.get("generation_id") or "")
    if receipt.get("state") == "creating" and not gid:
        # Отправка без ответа: создалась ли генерация, не узнать (списка у FastGen нет).
        sent = float(receipt.get("ended_at") or receipt.get("sent_at") or 0) or \
            os.path.getmtime(receipt_path(mp3_path))
        age = _wall_clock() - sent
        if age < PAUSE_MINUTES * 60:
            raise _Skipped(f"блок {block_idx}: прошлая отправка {age / 60:.0f} мин назад осталась без ответа — "
                           f"жду {PAUSE_MINUTES} мин, потом закажу заново")
        log(f"  FastGen: блок {block_idx}: отправка {age / 60:.0f} мин назад осталась без ответа — заказываю "
            f"заново (если та всё же создалась, лишний заказ — {CREDITS} единиц часа)")
        return ""
    if receipt.get("identity") != ident:
        return ""          # текст блока, модель, голос или стиль изменились — нужен другой звук
    if receipt.get("api_scope") != api_scope(key):
        log(f"  FastGen: блок {block_idx}: генерация {gid} заказана другим ключом — заказываю заново")
        return ""
    if receipt.get("state") in ("terminal", "done"):
        return ""          # закрыта неудачей или звук испорчен/удалён — заказ заново
    return gid


def create_generation(key: str, text: str, voice: str, mp3_path: str, ident: str, block_idx: int,
                      sleep=time.sleep, log=print) -> str:
    """POST /generations с ожиданием лимита часа и занятых потоков. Квитанция «creating»
    лежит ровно на время отправки: отказ 4xx (в том числе 429) — генерации нет, квитанция
    убирается; обрыв, 5xx и ответ без номера — «creating» остаётся с временем отправки."""
    body = {"model": MODEL, "prompt": text, "voice": voice}
    if SCENE:
        body["scene"] = SCENE
    deadline = _wall_clock() + QUOTA_WAIT_MINUTES * 60
    attempt = 0
    while True:
        _wait_quota(deadline, block_idx, sleep, log)
        sent_at = _wall_clock()
        save_receipt(mp3_path, ident, key, "creating", sent_at=sent_at)
        try:
            data = _api(key, "POST", "/generations", body, timeout=120)
        except FastGenTTSAPIError as e:
            if e.status == 429:
                _drop_receipt(mp3_path)
                wait = _after_429(key, e, attempt, sleep, log)
                if _wall_clock() + wait > deadline:
                    raise _Skipped(f"блок {block_idx}: не заказан — лимит FastGen держится дольше "
                                   f"{QUOTA_WAIT_MINUTES} мин; повторите шаг") from e
                sleep(wait)
                attempt += 1
                continue
            if e.status < 500 and e.status != 408:
                _drop_receipt(mp3_path)
            else:
                save_receipt(mp3_path, ident, key, "creating", sent_at=sent_at, ended_at=_wall_clock())
            raise
        except FastGenTTSNetworkError:
            save_receipt(mp3_path, ident, key, "creating", sent_at=sent_at, ended_at=_wall_clock())
            raise
        gid = str(data.get("id") or "").strip()
        if not gid:
            save_receipt(mp3_path, ident, key, "creating", sent_at=sent_at, ended_at=_wall_clock())
            raise FastGenTTSNetworkError("FastGen: POST /generations: ответ без номера генерации — "
                                         "создана ли она, неизвестно")
        save_receipt(mp3_path, ident, key, "created", gid, str(data.get("status") or ""),
                     sent_at=sent_at, expires_at=data.get("expires_at"))
        return gid


class _Terminal(RuntimeError):
    def __init__(self, gid, status, message=""):
        self.gid, self.status = gid, status
        super().__init__(f"генерация {gid} — {status}" + (f": {str(message)[:200]}" if message else ""))


def wait_generation(key: str, gid: str, block_idx: int, clock=time.monotonic, sleep=time.sleep,
                    log=print) -> dict:
    start = clock()
    last_beat, last_status = start, None
    while clock() - start < MAX_WAIT_MINUTES * 60:
        try:
            gen = _api(key, "GET", f"/generations/{urllib.parse.quote(gid, safe='')}")
        except FastGenTTSAPIError as e:
            if e.status in (404, 410):
                raise _Terminal(gid, "нет у FastGen (истекла или не найдена)") from None
            if not _transient(e):
                raise
            log(f"  FastGen: блок {block_idx}: опрос генерации {gid} не удался ({e}) — жду и повторяю")
            sleep(e.retry_after or POLL_SECONDS)
            continue
        except FastGenTTSNetworkError as e:
            log(f"  FastGen: блок {block_idx}: опрос генерации {gid} не удался ({e}) — жду и повторяю")
            sleep(POLL_SECONDS)
            continue
        status = str(gen.get("status") or "").strip().lower()
        if status in SUCCESS:
            return gen
        if status in TERMINAL_FAILURE:
            raise _Terminal(gid, status, gen.get("error") or gen.get("error_code") or "")
        now = clock()
        if status != last_status or now - last_beat >= HEARTBEAT_SECONDS:
            log(f"  FastGen: блок {block_idx}: генерация {gid} — {status or '?'}, ждём {(now - start) / 60:.1f} мин")
            last_status, last_beat = status, now
        sleep(POLL_SECONDS)
    raise TimeoutError(f"FastGen: блок {block_idx}: генерация {gid} не готова за {MAX_WAIT_MINUTES} мин; "
                       "квитанция сохранена — повтор шага продолжит ждать её, а не закажет заново")


def _result_bytes(key: str, gen: dict) -> bytes:
    res = gen.get("results") or []
    r0 = res[0] if res and isinstance(res[0], dict) else {}
    data = r0.get("data")
    if data:
        data = data.split(",", 1)[1] if str(data).startswith("data:") else data
        return base64.b64decode(data)
    url = str(r0.get("download_url") or "").strip()
    if not url:
        raise _Terminal(gen.get("id"), "готова без звука (нет ни data, ни download_url)")
    host = BASE_URL.split("/api/", 1)[0]
    if url.startswith("/"):
        url = host + url
    # Ключ — только своему хосту; подписанная ссылка хранилища в ключе не нуждается.
    headers = {"X-API-Key": key} if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(host).netloc else {}
    status, _h, raw = _http("GET", url, headers, None, 300)
    if status >= 400:
        raise FastGenTTSAPIError(status, "скачивание звука", {})
    return raw


def download(key: str, gen: dict, mp3_path: str, block_idx: int, sleep=time.sleep, log=print) -> None:
    """Звук готовой генерации → mp3 блока (44,1 кГц, 128 кбит/с, моно — как блоки Lumean)."""
    gid = gen.get("id")
    raw, last = None, None
    for attempt in range(3):
        try:
            raw = _result_bytes(key, gen)
            break
        except (FastGenTTSAPIError, FastGenTTSNetworkError) as e:
            last = e
            log(f"  FastGen: блок {block_idx}: звук генерации {gid} не скачан ({e}) — попытка {attempt + 2} из 3")
            sleep(POLL_SECONDS)
            try:   # подписанная ссылка живёт 15 мин — берём свежую карточку генерации
                gen = _api_retry(key, "GET", f"/generations/{urllib.parse.quote(str(gid), safe='')}",
                                 sleep=sleep, log=log)
            except FastGenTTSAPIError as err:
                if err.status in (404, 410):
                    raise _Terminal(gid, "звук не скачан, генерация уже удалена") from None
            except FastGenTTSNetworkError:
                pass
    if raw is None:
        raise _Terminal(gid, f"звук не скачан: {last}")
    if len(raw) <= 2048:
        raise _Terminal(gid, f"скачан пустой звук ({len(raw)} байт)")
    wav, part = mp3_path + ".part.wav", mp3_path + ".part.mp3"
    try:
        with open(wav, "wb") as f:
            f.write(raw)
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", wav, "-vn",
                            "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", part],
                           capture_output=True, text=True)
        if p.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) <= 2048:
            raise _Terminal(gid, f"звук не перекодирован в mp3: {(p.stderr or '').strip()[:200]}")
        os.replace(part, mp3_path)
    finally:
        for leftover in (wav, part):
            try:
                os.remove(leftover)
            except FileNotFoundError:
                pass


def audio_problem(text: str, seconds: float) -> Optional[str]:
    """Длина звука против числа слов: обрубок или тишина/повторы. None — годится."""
    words = len(text.split())
    if seconds < words / FASTEST_WPM * 60:
        return f"обрубок: {seconds:.1f} с на {words} слов (быстрее {FASTEST_WPM:g} сл/мин)"
    if seconds > words / SLOWEST_WPM * 60 + 10:
        return f"затянуто: {seconds:.1f} с на {words} слов (медленнее {SLOWEST_WPM:g} сл/мин)"
    return None


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
            and audio_problem(b.text, VO.duration(b.path)) is None)


def synth(blocks: List[VO.Block], out_dir: str, lang: str, log: Callable = print,
          sleep=time.sleep, clock=time.monotonic) -> List[VO.Block]:
    key, voice = preflight(lang)
    too_long = [b.idx for b in blocks if len(b.text) > MAX_CHARS]
    if too_long:
        raise FastGenTTSConfigError(f"FastGen: блоки {too_long[:10]} длиннее {MAX_CHARS} знаков; заказ не создан")
    os.makedirs(out_dir, exist_ok=True)
    for b in blocks:
        b.path = os.path.join(out_dir, f"block_{b.idx:04d}.mp3")
        b.words_path = ""          # меток слов нет — тайминги по распознаванию речи
    todo = [b for b in blocks if not block_ready(b, voice)]
    service_down = threading.Event()   # рубильник прохода: после отказа сервиса новые POST не шлём
    if todo:
        about = check_key(key, voice, sleep=sleep, log=log)
        log(f"озвучка FastGen: модель {MODEL}, голос {voice}, стиль "
            + ("свой (MANGA_FASTGEN_TTS_SCENE)" if SCENE and SCENE != DEFAULT_SCENE
               else "спокойный рассказчик" if SCENE else "без инструкции")
            + f" ({about}); к заказу/ожиданию блоков {len(todo)} из {len(blocks)}")

    def one(b: VO.Block):
        with _SLOTS:           # предел одновременных заказов на всю службу, а не на ролик
            return _one(b)

    def _one(b: VO.Block):
        ident = identity(b.text, voice)
        fp = VO.voice_fingerprint(b.text, voice_id(voice), "+0%")
        last: Optional[Exception] = None
        for attempt in range(ATTEMPTS):
            gid = generation_to_resume(b.idx, b.path, ident, key, log)
            if gid:
                log(f"  FastGen: блок {b.idx}: продолжаю ждать отправленную генерацию {gid}")
            else:
                if service_down.is_set():
                    raise _Skipped(f"блок {b.idx}: не заказан — сервис FastGen отказал в этом проходе")
                paused = pause_until(key)
                if paused:
                    raise _Skipped(f"блок {b.idx}: не заказан — общий перерыв FastGen до {_clock_text(paused)} "
                                   "после отказа сервиса; кончится сам, тогда повторите шаг")
                try:
                    gid = create_generation(key, b.text, voice, b.path, ident, b.idx, sleep=sleep, log=log)
                except _Skipped:
                    raise
                except Exception as e:
                    if _service_down(e) and not service_down.is_set():
                        service_down.set()
                        until = set_pause(key, e, log)
                        log(f"  FastGen: блок {b.idx}: сервис отказал ({e}) — новые заказы не отправляю"
                            + (f" до {_clock_text(until)} (общий перерыв всех роликов)" if until else "")
                            + "; уже отправленные жду")
                    raise RuntimeError(f"блок {b.idx}: заказ не создан: {e}") from e
                log(f"  FastGen: блок {b.idx}: генерация {gid} создана ({len(b.text.split())} слов)")
            atomic_json(b.path + ".voice.json", {"fingerprint": fp, "status": "synthesizing"})
            try:
                gen = wait_generation(key, gid, b.idx, clock=clock, sleep=sleep, log=log)
                download(key, gen, b.path, b.idx, sleep=sleep, log=log)
                problem = audio_problem(b.text, VO.duration(b.path))
                if problem:
                    raise _Terminal(gid, "звук не похож на текст", problem)
            except _Terminal as e:
                save_receipt(b.path, ident, key, "terminal", gid, e.status, error=str(e)[:300])
                last = e
                log(f"  FastGen: блок {b.idx}: {e}"
                    + (" — заказываю заново" if attempt + 1 < ATTEMPTS else ""))
                continue
            save_receipt(b.path, ident, key, "done", gid, "succeeded")
            atomic_json(b.path + ".voice.json", {"fingerprint": fp, "status": "complete", "generation_id": gid})
            return
        raise RuntimeError(f"блок {b.idx}: {last}")

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
            raise RuntimeError(f"озвучка FastGen: не готово блоков {len(errors)}"
                               + (f" (из них {skipped} не заказаны: отказ сервиса, общий перерыв или "
                                  "лимит часа)" if skipped else "")
                               + f": {first}") from first
    for b in blocks:
        b.seconds = VO.duration(b.path)
        log(f"  блок {b.idx:3d}: {b.seconds:6.1f}s  {len(b.text.split()):4d} слов")
    return blocks
