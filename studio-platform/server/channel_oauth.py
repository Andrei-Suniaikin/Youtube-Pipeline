# -*- coding: utf-8 -*-
"""Доступ к статистике канала: хранение токена и выдача рабочего ключа на час.

Зачем это отдельно от `pub_stats`. Публичные числа (подписчики, просмотры) берутся
ключом API и одинаковы для всех. Подробная статистика — время просмотра, досмотренность,
приход и уход подписчиков, доход — не публична: её отдаёт только YouTube Analytics API и
только владельцу канала, по его личному согласию. Согласие живёт в виде долгого токена,
и это НАСТОЯЩИЙ СЕКРЕТ: он открывает статистику канала заказчика кому угодно, кто его
прочтёт. Поэтому он живёт здесь по тем же правилам, что подписки для текста
(`rewrite/subscriptions.py`), а не колонкой в базе:

    <каталог>/<id канала>.json    0600, каталог 0700

Каталог — рядом с базой (`.channel-tokens`), переопределяется только серверным
`VS_CHANNEL_TOKENS_DIR`. Наружу значение не отдаётся никогда: ни в ответе, ни в журнале,
ни в тревоге. Видно «подключено», хвост из четырёх знаков и состояние.

Почему у каждого канала свой токен. У заказчика каждый канал заведён на своём
гугл-аккаунте (владелец, 25.09.2026), поэтому «одно согласие на всё» здесь не работает —
согласий столько же, сколько каналов.

──────────────────────────────────────────────────────────────────────────────────────
ГЛАВНАЯ ЛОВУШКА, ради которой написан этот модуль.

Запрос статистики можно адресовать двумя способами: «канал этого токена» (`channel==MINE`)
или «вот этот канал» (`channel==UC…`). Первый короче и не требует знать номер канала —
и именно поэтому опасен. Токены вставляет человек, руками, по одному на канал, сто с
лишним раз подряд. Вставить токен не в ту строку он однажды обязан. С `MINE` Google
молча ответит статистикой ЧУЖОГО канала, и она ляжет в карточку этого — ошибка, которую
на экране не отличить от правды, и которая потом расходится по отчётам.

Поэтому спрашиваем только `channel==UC…`, всегда. Чужой токен на этот канал получает от
Google честный отказ, а не чужие числа. Проверка делается сразу при вставке — «этот
токен не от этого канала» человек видит в ту же секунду, а не месяцем позже.
──────────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 20

# Рабочий ключ живёт час. Обновляем чуть раньше: запрос, начатый за секунду до конца
# срока, дойдёт до Google уже просроченным.
EARLY_SEC = 120

# Отказ авторизации бывает и сбоем на той стороне. Одиночный — не повод объявлять
# согласие отозванным: объявленное отозванным само не вернётся, а человек в это время
# думает, что статистика собирается.
DEAD_AFTER = 2

_LOCK = threading.RLock()
_CACHE: Dict[int, Dict] = {}       # id канала → {"value": рабочий ключ, "until": ...}


class OAuthError(Exception):
    """Отказ, который можно показать человеку целиком: токена внутри нет."""


def tokens_dir() -> str:
    env = (os.environ.get("VS_CHANNEL_TOKENS_DIR") or "").strip()
    if env:
        return env
    db = (os.environ.get("VS_DEV_DB") or os.environ.get("VS_SERVER_DB") or "")
    base = os.path.dirname(os.path.abspath(db)) if db else os.getcwd()
    return os.path.join(base, ".channel-tokens")


def _path(channel_id: int) -> str:
    return os.path.join(tokens_dir(), f"{int(channel_id)}.json")


def _ensure_dir() -> str:
    path = tokens_dir()
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, stat.S_IRWXU)               # 0700: только владелец процесса
    except OSError:
        pass                                       # чужая файловая система — не повод падать
    return path


def _read(channel_id: int) -> dict:
    try:
        with open(_path(channel_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(channel_id: int, data: dict) -> None:
    """Атомарно и закрыто: оборванная запись не должна оставить половину токена."""
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=tokens_dir(), prefix=".tmp-")
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)         # 0600
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, _path(channel_id))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tail(value: str) -> str:
    value = (value or "").strip()
    return value[-4:] if len(value) >= 8 else ""


# ───────────────────────────── реквизиты приложения ─────────────────────────────

def client() -> tuple:
    """Наши client_id и client_secret. Окружение старше файла ключей — как везде.

    Своё приложение обязательно, и не «для красоты»: на служебной странице Google, где
    оператор добывает токен, с ЧУЖИМИ (её собственными) реквизитами выданный токен
    отзывается через сутки. Со своими — живёт, пока не отзовут.
    """
    cid = (os.environ.get("VS_YT_OAUTH_CLIENT_ID") or "").strip()
    sec = (os.environ.get("VS_YT_OAUTH_CLIENT_SECRET") or "").strip()
    if cid and sec:
        return cid, sec
    try:
        import server_keys
        data = server_keys._load()   # noqa: SLF001 — читаем тот же файл ключей
        cid = cid or str(data.get("yt_oauth_client_id") or "").strip()
        sec = sec or str(data.get("yt_oauth_client_secret") or "").strip()
    except Exception:                                      # noqa: BLE001
        pass
    return cid, sec


def client_ready() -> bool:
    cid, sec = client()
    return bool(cid and sec)


# ───────────────────────────── состояние и правка ─────────────────────────────

def state(channel_id: int) -> dict:
    """Что известно о подключении канала — БЕЗ значения токена."""
    data = _read(channel_id)
    if not data.get("refresh"):
        return {"connected": False, "tail": "", "state": "", "since": 0.0,
                "last_ok": 0.0, "error": "", "client": client_ready()}
    return {"connected": True, "tail": data.get("tail") or "",
            "state": data.get("state") or "new", "since": float(data.get("added") or 0),
            "last_ok": float(data.get("last_ok") or 0),
            "error": data.get("error") or "", "client": client_ready()}


def set_token(channel_id: int, refresh: str) -> dict:
    """Запомнить согласие. Значение проверяется на вид, но не на принадлежность.

    Принадлежность каналу проверяет вызывающий — живым запросом статистики (см. шапку
    модуля). Здесь только хранение.
    """
    refresh = (refresh or "").strip()
    if len(refresh) < 20:
        raise OAuthError("это не похоже на токен: слишком коротко")
    if any(ch.isspace() for ch in refresh):
        raise OAuthError("в токене не должно быть пробелов и переносов — "
                         "скопировалось что-то лишнее")
    with _LOCK:
        _write(int(channel_id), {"refresh": refresh, "tail": tail(refresh),
                                 "added": time.time(), "state": "new",
                                 "last_ok": 0.0, "error": "", "fails": 0})
        _CACHE.pop(int(channel_id), None)
    return state(channel_id)


def drop(channel_id: int) -> dict:
    with _LOCK:
        try:
            os.unlink(_path(channel_id))
        except OSError:
            pass
        _CACHE.pop(int(channel_id), None)
    return state(channel_id)


def note(channel_id: int, ok: bool, error: str = "", dead: bool = False) -> None:
    """Отметить итог обращения. Мёртвым согласие объявляется не с первого отказа."""
    with _LOCK:
        data = _read(channel_id)
        if not data.get("refresh"):
            return
        now = time.time()
        if ok:
            data.update(state="ok", last_ok=now, error="", fails=0)
        else:
            fails = int(data.get("fails") or 0) + 1
            data["fails"] = fails
            data["error"] = str(error or "")[:300]
            # `dead` — это ответ Google «согласие отозвано»; ему верим сразу, потому что
            # он про сам токен, а не про сеть. Прочие отказы должны повториться.
            data["state"] = "dead" if (dead or fails >= DEAD_AFTER) else "warn"
        _write(channel_id, data)


# ───────────────────────────── рабочий ключ ─────────────────────────────

def _post(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _revoked(raw: str) -> bool:
    """Google говорит «этого согласия больше нет» словом `invalid_grant`.

    Отличать это от сетевого сбоя обязательно: первое чинится только человеком (заново
    выдать токен), второе проходит само. Перепутав, мы либо молчим о сломанном канале,
    либо гоняем человека переподключать работающий.
    """
    return "invalid_grant" in (raw or "").lower()


def access_token(channel_id: int, post=None, now: float | None = None) -> str:
    """Рабочий ключ на час. Обновляется сам, в памяти живёт до истечения срока."""
    now = time.time() if now is None else now
    channel_id = int(channel_id)
    with _LOCK:
        cached = _CACHE.get(channel_id) or {}
        if cached.get("value") and now < float(cached.get("until") or 0):
            return cached["value"]
    data = _read(channel_id)
    refresh = data.get("refresh") or ""
    if not refresh:
        raise OAuthError("канал не подключён к статистике")
    cid, sec = client()
    if not (cid and sec):
        raise OAuthError("не заданы реквизиты приложения Google "
                         "(client_id и client_secret) — без них обновить доступ нечем")
    try:
        out = (post or _post)(TOKEN_URL, {
            "client_id": cid, "client_secret": sec,
            "refresh_token": refresh, "grant_type": "refresh_token"})
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:                                  # noqa: BLE001
            raw = str(exc)
        if _revoked(raw):
            note(channel_id, False, "согласие отозвано или устарело", dead=True)
            raise OAuthError("Google больше не принимает это согласие: его отозвали, "
                             "либо оно выдано другим приложением. Нужен новый токен")
        note(channel_id, False, f"Google не выдал доступ: {raw[:200]}")
        raise OAuthError(f"Google не выдал доступ: {raw[:200]}")
    except Exception as exc:                               # noqa: BLE001
        note(channel_id, False, f"связь с Google не удалась: {exc}")
        raise OAuthError(f"связь с Google не удалась: {exc}")

    value = str(out.get("access_token") or "")
    if not value:
        note(channel_id, False, "Google ответил без ключа доступа")
        raise OAuthError("Google ответил без ключа доступа")
    lives = float(out.get("expires_in") or 3600)
    with _LOCK:
        _CACHE[channel_id] = {"value": value,
                              "until": now + max(60.0, lives - EARLY_SEC)}
    return value


def forget_cache(channel_id: int | None = None) -> None:
    with _LOCK:
        if channel_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(int(channel_id), None)


# ───────────────────────────── подключение кнопкой ─────────────────────────────
#
# 09.10.2026. Раньше токен добывали на служебной странице Google (OAuth Playground) и
# вставляли руками. Для проверки приложения Google это не годится: адрес возврата
# должен быть на нашем подтверждённом домене, а демо-видео — показывать наш поток
# согласия. Теперь кнопка «Подключить через Google» уводит в окно согласия, Google
# возвращает код на наш адрес, мы меняем его на тот же долгий токен.

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# Ровно пара только для чтения — почему именно она, см. `analyticsHowTo` в пульте.
SCOPES = ("https://www.googleapis.com/auth/yt-analytics.readonly",
          "https://www.googleapis.com/auth/youtube.readonly")
# Адрес возврата обязан побайтно совпадать с внесённым в «Authorized redirect URIs».
DEFAULT_REDIRECT = "https://vovastudio.work/studio/oauth/callback"
PENDING_SEC = 15 * 60

_PENDING: Dict[str, Dict] = {}     # state → {"channel": id, "user": id, "until": ...}


def redirect_uri() -> str:
    return (os.environ.get("VS_YT_OAUTH_REDIRECT") or "").strip() or DEFAULT_REDIRECT


def start(channel_id: int, user_id, now: Optional[float] = None) -> str:
    """Адрес окна согласия Google для этого канала.

    `state` — одноразовый и привязан к человеку, который нажал кнопку: чужой ответ
    Google (или подделанная ссылка) не положит токен в наш канал.
    """
    import secrets
    cid, _sec = client()
    if not cid:
        raise OAuthError("не заданы реквизиты приложения Google — впишите их на "
                         "странице «Ключи»")
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(24)
    with _LOCK:
        for k in [k for k, v in _PENDING.items() if v["until"] < now]:
            _PENDING.pop(k, None)
        _PENDING[token] = {"channel": int(channel_id), "user": user_id,
                           "until": now + PENDING_SEC}
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent: без них Google не выдаёт долгий токен повторно.
        "access_type": "offline",
        "prompt": "select_account consent",
        "include_granted_scopes": "false",
        "state": token,
    })


def pending_channel(state_token: str, user_id, now: Optional[float] = None) -> int:
    """Канал, для которого начато подключение. Одноразово: второй раз — отказ."""
    now = time.time() if now is None else now
    with _LOCK:
        got = _PENDING.pop(str(state_token or ""), None)
    if got is None or got["until"] < now:
        raise OAuthError("ссылка подключения устарела или уже использована — нажмите "
                         "«Подключить через Google» ещё раз")
    if got["user"] != user_id:
        raise OAuthError("подключение начато другим пользователем студии")
    return got["channel"]


def exchange(code: str, post=None) -> str:
    """Код из ответа Google → долгий токен. Проверяет, что выданы ОБА права."""
    post = post or _post
    cid, sec = client()
    try:
        out = post(TOKEN_URL, {"code": code, "client_id": cid, "client_secret": sec,
                               "redirect_uri": redirect_uri(),
                               "grant_type": "authorization_code"})
    except urllib.error.HTTPError as exc:
        try:
            why = json.loads(exc.read().decode("utf-8") or "{}").get("error") or exc.code
        except Exception:                                  # noqa: BLE001
            why = exc.code
        raise OAuthError(f"Google не обменял код на токен ({why}) — попробуйте ещё раз")
    except Exception as exc:                               # noqa: BLE001
        raise OAuthError(f"Google не ответил: {type(exc).__name__}")
    granted = set(str(out.get("scope") or "").split())
    missing = [s.rsplit("/", 1)[-1] for s in SCOPES if s not in granted]
    if missing:
        # Согласие дробное: снятая галочка даёт рабочий с виду токен без отчётов.
        raise OAuthError("на экране Google сняли галочку: не выдано " + ", ".join(missing)
                         + " — подключите заново и оставьте обе галочки")
    refresh = str(out.get("refresh_token") or "")
    if not refresh:
        raise OAuthError("Google не выдал долгий токен — подключите ещё раз")
    return refresh


def revoke(channel_id: int, post=None) -> bool:
    """Отозвать согласие у Google (кнопка «Отключить»). Сбой — не повод не отключать."""
    data = _read(channel_id)
    refresh = data.get("refresh") or ""
    if not refresh:
        return False
    try:
        (post or _post)(REVOKE_URL, {"token": refresh})
        return True
    except Exception:                                      # noqa: BLE001
        return False
