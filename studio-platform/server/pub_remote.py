#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Клиенты соседних сервисов: рерайт текста и генератор обложек.

Оба живут на той же машине, поэтому ходим на localhost — мимо внешнего домена и Caddy.
Ключи доступа хранятся в таблице `admin_state` кабинета: там же, где переживает рестарт
пауза очереди, и не в файле с вендорскими секретами.

Разделение отказов — главное, ради чего здесь свои обёртки:

* **отказ этой строки** — битый донор, нет субтитров, отказ модерации. Строка в карантин;
* **отказ общего ресурса** — сосед не отвечает, кончились креды канала (у рерайта это 503),
  превышен лимит. Ждёт вся очередь, ни одна строка не ломается.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import threading
import time

import requests

from pub_engine import ItemFailed, ResourceDown
# Разбор донорской ссылки живёт в pub_ids: им пользуется и миграция схемы, куда requests
# тянуть нельзя. Имена оставлены здесь же — на них ссылаются существующие вызовы.
from pub_ids import YT_ID_RE, video_id            # noqa: F401

# Адреса текста и обложек. Решение владельца 30.09.2026: «конечно ходим к своим, старых
# студий для новой как будто нет, она живёт свою жизнь».
#
# Раньше здесь по умолчанию стояли СТАРЫЕ сервисы (8899 и 8900) — так было верно, пока
# шёл переезд и оба варианта существовали одновременно. Сейчас это мина: запуск мимо
# `deploy/start-studio.sh` (а он единственный, кто эти адреса выставляет) молча отправил
# бы новую студию работать в чужие базы — чужие пресеты, чужая очередь, чужие файлы. Беда
# такого рода не падает с ошибкой, она тихо делает работу не там.
#
# Теперь по умолчанию — СВОИ, те же, что платформа монтирует под префиксами. Порт берём
# из `VS_PORT`, как его считает и скрипт запуска: одно число в одном месте. Старые
# сервисы по-прежнему доступны, но только если их назвать явно.
_PORT = (os.environ.get("VS_PORT") or "8090").strip() or "8090"
REWRITE_BASE = (os.environ.get("VS_REWRITE_BASE")
                or f"http://127.0.0.1:{_PORT}/rewrite")
COVER_BASE = os.environ.get("VS_COVER_BASE") or f"http://127.0.0.1:{_PORT}/cover"

TIMEOUT = 30
# YouTube не отдаёт субтитры адресам облачных провайдеров, а каждый домашний адрес
# держит лишь несколько десятков скачиваний в час и потом отвечает 429. Поэтому выходов
# несколько (туннели с наших машин), и на отказ мы отвечаем сменой выхода, а не ожиданием.
# «direct» (или «-») — выход без туннеля, напрямую с этой машины. Нужен не для красоты:
# на VPS без туннеля YouTube субтитры не отдаёт, а на домашнем адресе отдаёт прекрасно,
# и до появления этого значения сказать «иди напрямую» было нечем — пустая переменная
# означала «выходов нет вообще».
DIRECT = ("direct", "-", "none", "нет")
SUBS_PROXIES = ["" if p.strip().lower() in DIRECT else p.strip()
                for p in os.environ.get("VS_SUBS_PROXIES",
                                        "socks5h://127.0.0.1:1080").split(",")
                if p.strip()]
# ВНИМАНИЕ (решение владельца, см. отчёт). В VS_SUBS_PROXIES сегодня перемешаны выходы
# ДВУХ разных назначений: туннели для ЧТЕНИЯ субтитров с домашних машин и прокси
# профилей AdsPower, через которые идёт ЗАЛИВКА на каналы. Сторож проверяет весь
# список как выходы чтения — иначе он бы молча решал за владельца, что есть что.
# Разводить списки должен владелец; предложение по именам — в отчёте.

# Cookies владельца: с ними YouTube отвечает как обычному зрителю, а не как анониму.
SUBS_COOKIES = os.environ.get("VS_SUBS_COOKIES", "/root/.vs_yt_cookies.txt")
# Выход, ответивший отказом, отдыхает — но очередь идёт дальше через остальные.
SUBS_REST = float(os.environ.get("VS_SUBS_REST", "1800"))
# Таймаут на запрос к YouTube (18.09.2026). `youtube-transcript-api` зовёт сессию БЕЗ
# таймаута, а выход бывает полумёртвым: порт слушает, туннель не отвечает — так на бою
# 18.09 и было (срез live_2026-09-18, глава «субтитры», раздел 5). Без таймаута первый
# же выход вешает поток, а движок выпусков крутится одним циклом: встаёт всё, включая
# снятие зависших заливок. Ставим умолчание на уровне сессии — библиотеку не трогаем.
SUBS_TIMEOUT = float(os.environ.get("VS_SUBS_TIMEOUT", "25"))
_subs_rest_until = {}          # выход → до какого времени отдыхает
_subs_gate = threading.Lock()

# ── сторож живости выходов (19.09.2026) ───────────────────────────────────────
# Таймаут спас поток, но не ответил на вопрос «а этот выход вообще жив». Полумёртвый
# туннель (порт слушает, запросы не проходят) выглядит для ротации как обычный выход:
# заказ уходит в него, ждёт SUBS_TIMEOUT, получает отказ — и так на каждой строке,
# пока владелец не проверит туннель руками. Поэтому каждый выход периодически
# опрашивается отдельно от работы.
#
# Адрес проверки — НЕ YouTube, и это принципиально. Проверка у YouTube жгла бы ту же
# квоту адреса, которую бережёт ротация, и на двух десятках выходов сама выглядела бы
# как перебор адресов — то есть готовила бы бан ровно тем выходам, живость которых
# проверяет. Берём нейтральную страницу; она заодно возвращает внешний адрес, каким
# выход виден снаружи, — по нему сразу видно туннель, который «работает», но выпускает
# нас с адреса самого VPS.
PROBE_URL = os.environ.get("VS_SUBS_PROBE_URL", "https://www.cloudflare.com/cdn-cgi/trace")
# Как часто проверять каждый выход. Реже — дольше живёт неверное представление о
# выходе; чаще — лишний стук в сеть без всякой пользы: выход не умирает каждую минуту.
# Ноль полностью выключает сторожа: сам он больше не ходит никуда, сведения за час
# протухают, и ротация возвращается ровно к тому, чем была до него.
PROBE_PERIOD = float(os.environ.get("VS_SUBS_PROBE_PERIOD", "600"))
# Проверка обязана быть заметно короче рабочего таймаута: это ответ «жив ли», а не
# попытка дождаться результата.
PROBE_TIMEOUT = float(os.environ.get("VS_SUBS_PROBE_TIMEOUT", "10"))
# Сколько проверок подряд должны провалиться, прежде чем выход считается мёртвым.
# Одиночный обрыв бывает у любого канала связи, и убирать из-за него рабочий выход
# дороже, чем один раз сходить в него зря.
PROBE_FAILS = int(os.environ.get("VS_SUBS_PROBE_FAILS", "2"))
# После какого молчания сведения о выходе перестают влиять на ротацию. Сторож мог не
# работать (рестарт, выключенная проверка), и тогда вчерашнее «мёртв» — не знание, а
# воспоминание. Протухло — работаем как до сторожа, через все выходы.
PROBE_STALE = float(os.environ.get("VS_SUBS_PROBE_STALE", "3600"))

_probe_gate = threading.Lock()
_probe_running = False         # проверка уже идёт своим потоком — второй не нужен
_health = {}                   # выход → сведения сторожа (память процесса)
_health_loaded = False         # сведения из базы подняты после рестарта

# Русские названия языков — их ждут оба соседа.
LANG_TO_RU = {
    "pl": "Польский", "cs": "Чешский", "sk": "Словацкий", "ro": "Румынский",
    "hr": "Хорватский", "el": "Греческий", "hu": "Венгерский", "ru": "Русский",
    "de": "Немецкий", "fr": "Французский", "it": "Итальянский", "es": "Испанский",
    "pt": "Португальский", "po": "Португальский", "fi": "Финский", "bg": "Болгарский",
    "uk": "Украинский", "en": "Английский", "tr": "Турецкий", "nl": "Нидерландский",
}


def lang_ru(code: str) -> str:
    return LANG_TO_RU.get((code or "").lower(), "Русский")


# ─────────────────────────── ключи ───────────────────────────

def get_key(c, name: str) -> str:
    """Ключ доступа к соседу. Нет таблицы или записи — пустая строка, а не падение:
    отсутствие ключа должно читаться как «ждём настройки», а не как сбой строки."""
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (f"pub_key_{name}",)).fetchone()
    except Exception:
        return ""
    return (row[0] if row else "") or ""


def set_key(c, name: str, value: str, now: float) -> None:
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (f"pub_key_{name}", value, now))


# ─────────────────────────── общее ───────────────────────────

class RemoteRejected(ItemFailed):
    """Сосед разобрал запрос и отказал по смыслу (422): не та модель, не тот язык.

    Отдельный класс нужен, чтобы вызывающий мог объяснить отказ человеку своими
    словами («не принял модель X»), а не пересказывать чужой HTTP-код. Наследник
    ItemFailed: для движка это по-прежнему отказ ЭТОЙ строки, очередь не встаёт.
    """


def _call(method: str, url: str, key: str, service: str, **kw):
    """Запрос к соседу с переводом его ответа в наши два класса отказа."""
    # `missing_job=True` ставят только опросы уже созданной задачи: там 404/410 значит
    # «задача у соседа исчезла», а не «мы ошиблись адресом». Отличать это важно — см.
    # RemoteJobMissing ниже.
    missing_job = kw.pop("missing_job", False)
    headers = kw.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {key}"
    try:
        resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
    except requests.RequestException as exc:
        raise ResourceDown(service, f"{service} не отвечает: {type(exc).__name__}")

    if resp.status_code in (401, 403):
        raise ResourceDown(service, f"{service}: ключ доступа не принят")
    if resp.status_code == 429:
        raise ResourceDown(service, f"{service}: превышен лимит запросов")
    if resp.status_code == 503:
        # У рерайта 503 означает «нет кредов канала», а не «сервис лёг»: ретраить строкой
        # бессмысленно, но и в карантин её вести нельзя — ждёт вся очередь.
        raise ResourceDown(service, f"{service}: нет доступных ключей провайдера")
    if resp.status_code >= 500:
        raise ResourceDown(service, f"{service}: внутренняя ошибка {resp.status_code}")
    if resp.status_code in (404, 410) and missing_job:
        raise RemoteJobMissing(
            f"{service}: сохранённая задача отсутствует — она удалена у соседа или "
            f"заказана с другого стенда. Автоматическое создание дубля остановлено: "
            f"новый заказ это новые деньги, решение за человеком")
    if resp.status_code == 422:
        raise RemoteRejected(f"{service} не принял запрос: {_detail(resp)}")
    if resp.status_code >= 400:
        raise ItemFailed(f"{service} ответил {resp.status_code}: {_detail(resp)}")
    return resp


def _detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:200]
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            if body.get(key):
                return str(body[key])[:200]
    return json.dumps(body, ensure_ascii=False)[:200]


# ─────────────────────────── субтитры донора ───────────────────────────


def _subs_live_exits() -> list:
    """Выходы, годные прямо сейчас: не отдыхают и не признаны мёртвыми.

    Два признака негодности РАЗНЫЕ и складывать их в один нельзя. «Отдыхает» — это
    исчерпанная квота адреса (429), выход при этом жив и через полчаса снова нужен.
    «Мёртв» — туннель не отвечает вообще; его не лечит ожидание, лечит владелец.

    Три правила, ради которых это написано именно так:

    * проверка не задерживает заказ — сторож будится в фоне и результата мы не ждём;
    * пока свежих сведений нет, работаем ровно как до сторожа — через все выходы;
    * если мёртвыми оказались ВСЕ, мы им не верим и идём по полному списку. Так
      выглядит не «сеть кончилась», а упавший адрес проверки или отсутствие SOCKS:
      остановить из-за этого выпуски значило бы, что сторож вредит сильнее поломки,
      которую стережёт.
    """
    now = time.time()
    subs_probe_async()         # разбудить сторожа, если пора; поток свой, ответа не ждём
    with _subs_gate:
        rested = [p for p in SUBS_PROXIES if _subs_rest_until.get(p, 0) <= now]
    dead = _subs_dead_exits(now)
    if not dead:
        return rested
    return [p for p in rested if p not in dead] or rested


def _subs_rest(proxy: str) -> None:
    """Отправить выход отдыхать: его квота на этот час исчерпана."""
    with _subs_gate:
        _subs_rest_until[proxy] = time.time() + SUBS_REST


def _subs_wait_left() -> int:
    """Сколько минут до пробуждения ближайшего выхода — для понятного сообщения."""
    with _subs_gate:
        if not _subs_rest_until:
            return 0
        return max(0, int((min(_subs_rest_until.values()) - time.time()) / 60) + 1)


class _SubsSession(requests.Session):
    """Сессия со своим таймаутом по умолчанию.

    `youtube-transcript-api` вызывает `session.get(...)` без `timeout`, и переопределить
    это снаружи нечем: единственное место, где можно подставить умолчание, — сама
    сессия. Явно переданный вызывающим таймаут остаётся сильнее.
    """

    def request(self, method, url, **kw):                          # noqa: D102
        kw.setdefault("timeout", SUBS_TIMEOUT)
        return super().request(method, url, **kw)


def _subs_session(proxy: str):
    """Сессия к YouTube: заданный выход наружу и cookies владельца внутри.

    Файл cookies читаем на каждый запрос — обновили его, и новые подхватятся сами,
    перезапуск сервиса для этого не нужен.
    """
    session = _SubsSession()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    if SUBS_COOKIES and os.path.exists(SUBS_COOKIES):
        try:
            jar = http.cookiejar.MozillaCookieJar(SUBS_COOKIES)
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                session.cookies.set_cookie(cookie)
        except Exception:
            pass          # битый или чужой формат — идём анонимом, это не повод падать
    return session


def _transcript_api(proxy: str):
    from youtube_transcript_api import YouTubeTranscriptApi
    return YouTubeTranscriptApi(http_client=_subs_session(proxy))


def _proxy_unusable(exc) -> str:
    """Туннель задан, но пойти в него нечем — это настройка, а не квота.

    `requests` отвечает на socks5-адрес без установленного PySocks исключением
    `InvalidSchema: Missing dependencies for SOCKS support` — по нему невозможно
    догадаться, что дело в туннеле. Отправить такой выход «отдыхать» было бы хуже
    вдвойне: через полчаса система сказала бы «все выходы исчерпали квоту», хотя
    квота ни при чём.
    """
    if not isinstance(exc, requests.exceptions.InvalidSchema):
        return ""
    return ("выход субтитров задан туннелем, но SOCKS-поддержка не установлена "
            "(pip install 'requests[socks]'), либо туннель не поднят. "
            "Ходить напрямую: VS_SUBS_PROXIES=direct")


# Имена, которыми библиотека сообщает «видео недоступно», НЕ означают «у ролика нет
# субтитров». 21.09.2026: стенд получил VideoUnavailable на ролике, с которого в ту же
# минуту, с того же адреса и тем же выходом (`direct`) вручную снялось 5429 знаков
# расшифровки. То есть так YouTube отвечает и когда просто не обслуживает нас сейчас.
# Разница между «ролика больше нет» и «нам сейчас не отдали» решает судьбу строки, и
# угадывать её по имени исключения нельзя — надо спросить.
_AMBIGUOUS_UNAVAILABLE = ("VideoUnavailable", "VideoUnplayable")


def _video_really_gone(vid: str) -> bool:
    """Ролика нет ни для кого — или он есть, и «недоступен» относится к НАМ.

    Улика бесплатная: открытый oembed YouTube отвечает 200 с названием на живой ролик и
    400 на удалённый. Ключей и квоты не тратит — тот же путь, которым проверка донора уже
    узнаёт название (`pub_tags.donor_title`).

    Не смогли спросить — возвращаем False. Молчание сети не повод хоронить строку: цена
    ошибок несимметрична. Лишнее ожидание стоит задержки, ложный приговор — исправного
    донора, а этой ошибкой здесь уже дважды выкашивало выпуски десятками.
    """
    if not vid:
        return False
    try:
        from pub_tags import donor_title                           # noqa: PLC0415
        return not donor_title(f"https://www.youtube.com/watch?v={vid}")
    except Exception:                                              # noqa: BLE001
        return False


def _no_subtitles(exc) -> bool:
    """У ролика действительно нет субтитров — это приговор строке, а не нашей стороне.

    Список закрытый: только то, чем библиотека сообщает об отсутствии дорожек. Всё
    остальное (наши опечатки, обрывы, неожиданные ответы) строку убивать не вправе.
    """
    name = type(exc).__name__
    return name in ("NoTranscriptFound", "TranscriptsDisabled", "NoTranscriptAvailable",
                    "VideoUnavailable", "VideoUnplayable", "AgeRestricted")


# Имена исключений, которыми библиотека сообщает «YouTube не обслуживает ЭТОТ адрес».
# Список один на две функции нарочно: `_is_exit_fault` решает, менять ли выход, а
# `_exit_fault_kind` — что сказать человеку про причину. Разойдясь, они дают худший из
# возможных ответов, и уже разошлись: `YouTubeRequestFailed` (обёртка над «ответ не
# 200» — самая частая форма блокировки адреса) считался отказом выхода в ротации, а в
# объяснении причины попадал в «мёртвый туннель», то есть звал владельца чинить
# исправный туннель вместо «подождите, пройдёт само».
_EXIT_QUOTA_NAMES = ("TooManyRequests", "IpBlocked", "RequestBlocked",
                     "YouTubeRequestFailed")


def _exit_quota_fault(exc) -> bool:
    """Отказ выхода по квоте/блокировке адреса — по имени класса исключения."""
    name = type(exc).__name__
    return any(part in name for part in _EXIT_QUOTA_NAMES)


def _is_exit_fault(exc) -> bool:
    """Виноват выход, а не ролик: исчерпанная квота либо неработающий туннель.

    Оба случая лечатся сменой выхода. Записать их в «у ролика нет субтитров» значило бы
    похоронить исправный донор из-за упавшего туннеля — этой ошибкой уже было убито
    сорок с лишним выпусков, прежде чем её нашли.
    """
    if _exit_quota_fault(exc):
        return True
    return isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.ProxyError,
                            requests.exceptions.Timeout))


# Машинные признаки отказа субтитров. Нужны не движку выпусков (ему довольно того, что
# это ResourceDown и строка ждёт), а проверке донора перед постановкой в план: ей эти
# случаи надо РАЗЛИЧАТЬ на экране. «Квота» — адрес жив, но YouTube его сейчас не
# обслуживает, через полчаса всё получится само; «туннель» — выход не отвечает вообще,
# и ждать бесполезно, нужен владелец. Сложив их в одно «не смогли», пульт показал бы
# человеку одну и ту же строку в двух разных ситуациях, из которых чинить надо только
# вторую. Признак кладём АТРИБУТОМ на исключение, а не новым типом: `ResourceDown`
# живёт в pub_engine, разбирать его причину по русскому тексту сообщения — способ
# сломать проверку первой же правкой формулировки.
DOWN_QUOTA = "quota"       # выходы исчерпали квоту YouTube (429), лечится временем
DOWN_TUNNEL = "tunnel"     # выход не отвечает / SOCKS не установлен, лечится владельцем
DOWN_NO_LIB = "no_lib"     # на сервере нет youtube-transcript-api
DOWN_OURS = "ours"         # наша поломка: ни ролик, ни выход не виноваты
DOWN_BUDGET = "budget"     # не успели за отведённое время — про ролик ничего не узнали
FAIL_NO_SUBS = "no_subs"   # у ролика действительно нет дорожки — приговор ролику
FAIL_NOT_YT = "not_youtube"
FAIL_THIN = "thin"         # дорожка есть, но это обрывок


def _exit_fault_kind(exc) -> str:
    """Чем именно негоден выход: исчерпанной квотой или мёртвым туннелем.

    Разделение то же, что в ротации (`_subs_live_exits`): «отдыхает» и «мёртв» — разные
    состояния. 429, блокировка адреса и «YouTube ответил не 200» (`_EXIT_QUOTA_NAMES`) —
    это квота; обрыв, отказ прокси и таймаут — это туннель. Неизвестное относим к
    туннелю: «сходите посмотрите на выход» — совет менее вредный, чем «подождите
    полчаса», когда ждать нечего.
    """
    if _exit_quota_fault(exc):
        return DOWN_QUOTA
    return DOWN_TUNNEL


def _subs_down(kind: str, message: str) -> ResourceDown:
    """Отказ ресурса «субтитры» с машинным признаком причины."""
    down = ResourceDown("субтитры", message)
    down.kind = kind
    return down


def _subs_failed(kind: str, message: str) -> ItemFailed:
    """Отказ самой строки (ролика) с машинным признаком причины."""
    bad = ItemFailed(message)
    bad.kind = kind
    return bad


def _cabinet_db():
    """Своё соединение с базой кабинета.

    Путь берём из окружения на КАЖДЫЙ вызов, а не при импорте: стенд поднимается на
    копии базы, и запомненный путь увёл бы запись в боевую.

    Порядок тот же, что у кабинета (`main.py:21`: VS_SERVER_DB либо `vsgen.db` рядом),
    плюс VS_DB впереди — им пользуются тесты и разбор инцидента. Раньше здесь стоял
    только VS_DB с зашитым боевым путём, а стенд задаёт VS_SERVER_DB: на стенде кэш
    субтитров молча писал в несуществующий `/root/...` и не работал вовсе. Сторожу
    такое не подходит принципиально — пульт читает его таблицу соединением кабинета,
    и лежать она обязана в той же базе.
    """
    import sqlite3
    path = (os.environ.get("VS_DB") or os.environ.get("VS_SERVER_DB")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "vsgen.db"))
    return sqlite3.connect(path, timeout=20)


# Срок жизни запомненного НАЗВАНИЯ донора здесь больше не задаётся: он такой же порог
# проверки ссылки, как остальные девять, и живёт вместе с ними в
# `pub_tags.DONOR_DEFAULTS["title_ttl_sec"]` (окружение то же — VS_DONOR_TITLE_TTL).
# Пока он стоял здесь, владелец видел на экране настроек девять порогов из десяти, а
# десятый правился только через systemd. Свежесть решает тот, кто спрашивает: кэш
# отдаёт название вместе со временем, когда его запомнили (`donor_cache_title`).

# Колонки, дописанные к кэшу 19.09.2026 поверх исходных трёх (video_id/text/created).
_SUBS_EXTRA_COLS = (("lang", "TEXT NOT NULL DEFAULT ''"),
                    ("seconds", "REAL NOT NULL DEFAULT 0"),
                    ("title", "TEXT NOT NULL DEFAULT ''"),
                    ("title_at", "REAL NOT NULL DEFAULT 0"))
_SUBS_BASE_COLS = ("video_id", "text", "created")


def _subs_cache_cols(conn) -> set:
    """Какие колонки у кэша есть НА САМОМ ДЕЛЕ, а не какие мы хотели дописать.

    Досыпка колонок (`ALTER TABLE ADD COLUMN`) может не пройти: база занята чужой
    длинной транзакцией (SQLITE_BUSY), файл открыт только на чтение, диск полон.
    Раньше после такого провала кэш умирал ЦЕЛИКОМ и молча: чтение просило
    несуществующие колонки и возвращало «не знаем», запись падала в тот же `except` —
    то есть каждый выпуск снова шёл за субтитрами в сеть. Это ровно та нагрузка, из-за
    которой YouTube закрывает адрес (330 лишних скачиваний из 574 на одной волне), и
    появлялась она без единой строки в журнале.

    Поэтому набор колонок спрашивается у базы перед каждым чтением и записью, а
    недостающие просто не упоминаются в SQL: кэш работает по-старому, в объёме трёх
    исходных колонок, вместо того чтобы не работать вовсе.
    """
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(subs_cache)")}
    except Exception:                                              # noqa: BLE001
        return set(_SUBS_BASE_COLS)


def _subs_cache_db():
    """Соединение с базой кабинета. Кэш субтитров лежит там же, где остальная работа.

    Колонки `lang`, `seconds`, `title`, `title_at` дописываются на месте (19.09.2026).
    Это НЕ второй кэш рядом: проверка донора перед постановкой в план
    (`pub_tags.check_donor`) спрашивает про тот же самый video_id то же самое — есть ли
    расшифровка, на каком она языке, сколько ролик идёт, как называется. Отдельная
    таблица дала бы два ответа про один ролик, и вопрос «какой из них правда» встал бы
    ровно в тот день, когда они разойдутся.

    Старые строки остаются годными: у них просто пустой язык и нулевая длительность —
    это честное «не знаем», а не «ноль минут».

    Досыпка — попытка, а не условие: не прошла (занятая база, файл на чтение) — кэш
    продолжает работать на трёх исходных колонках, см. `_subs_cache_cols`.
    """
    conn = _cabinet_db()
    conn.execute("CREATE TABLE IF NOT EXISTS subs_cache("
                 "video_id TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL)")
    have = _subs_cache_cols(conn)
    for name, decl in _SUBS_EXTRA_COLS:
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE subs_cache ADD COLUMN {name} {decl}")
        except Exception:                                          # noqa: BLE001
            pass      # колонку добавил соседний процесс между PRAGMA и ALTER — не беда
    return conn


# ──────────────── сторож живости выходов субтитров (19.09.2026) ────────────────
#
# Что было. Ротация знала про выход ровно одно: ответил он на рабочий запрос или нет.
# Мёртвый туннель (`127.0.0.1:1080` у владельца прямо сейчас: порт слушает, запросы не
# проходят) от живого этим не отличается — он отличается только тем, что каждый заказ
# сначала ждёт его таймаут. Узнать это можно было руками и никак иначе.
#
# Что стало. Каждый выход отдельно от работы стучится в нейтральный адрес, итог ложится
# в таблицу `subs_exit_health` (выход → жив/мёртв, когда последний раз отвечал, чем
# ответил в последний раз). Мёртвый временно уходит из ротации и возвращается сам, как
# только снова ответит: сторож не «выключает» выход навсегда, он сообщает факт.

def ensure_subs_health_schema(c) -> None:
    """Создать таблицу сторожа. Идемпотентно.

    Живёт отдельной ручкой, потому что таблицу заводит тот, кто первым до неё дошёл:
    сторож крутится в процессе кабинета, а общий `ensure_pub_schema` про субтитры
    ничего не знает. Пульту достаточно позвать это на своём соединении.
    """
    c.execute(
        "CREATE TABLE IF NOT EXISTS subs_exit_health("
        # Строка выхода ровно как она задана в VS_SUBS_PROXIES; '' — «напрямую».
        "exit_url TEXT PRIMARY KEY, "
        "alive INTEGER NOT NULL DEFAULT 1, "
        "fails INTEGER NOT NULL DEFAULT 0, "     # провалов подряд
        "checked REAL NOT NULL DEFAULT 0, "      # когда последний раз проверяли
        "ok_at REAL NOT NULL DEFAULT 0, "        # когда последний раз ОТВЕТИЛ
        "ms INTEGER NOT NULL DEFAULT 0, "        # сколько занял последний удачный ответ
        "ip TEXT NOT NULL DEFAULT '', "          # каким адресом выход виден снаружи
        "error TEXT NOT NULL DEFAULT '')")


_CREDS_RE = re.compile(r"://[^/@\s]+@")


def _hide_creds(text: str) -> str:
    """Убрать логин и пароль из строки выхода.

    Прокси профилей AdsPower задаются с парой логин:пароль прямо в адресе, а текст
    ошибки `requests` этот адрес цитирует целиком. Без чистки пароль уехал бы в
    таблицу, в лог и на экран пульта.
    """
    return _CREDS_RE.sub("://***@", str(text or ""))


def exit_label(proxy: str) -> str:
    """Имя выхода для человека: без пароля и без лишней длины."""
    if not proxy:
        return "напрямую"          # выход без туннеля — так его называет и настройка
    return _hide_creds(proxy)[:80]


def _probe_session(proxy: str):
    """Сессия для проверки: только выход, без cookies владельца.

    Cookies здесь не просто не нужны — их нельзя брать принципиально: проверка ходит
    на ЧУЖОЙ адрес, а в файле лежит живая сессия владельца на YouTube.
    """
    session = _SubsSession()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _trace_ip(body: str) -> str:
    """Внешний адрес из ответа `cdn-cgi/trace` (строка `ip=...`).

    Другой адрес проверки такой строки не отдаст — тогда просто пусто: адрес полезен,
    но проверка не про него.
    """
    for line in (body or "")[:2000].splitlines():
        if line.startswith("ip="):
            return line[3:].strip()[:45]
    return ""


def _probe_exit(proxy: str) -> dict:
    """Один стук в нейтральный адрес через заданный выход."""
    started = time.time()
    try:
        # Сессия закрывается сразу: проверка — разовый стук, держать под неё открытое
        # соединение на каждый из двух десятков выходов незачем.
        with _probe_session(proxy) as session:
            resp = session.get(PROBE_URL, timeout=PROBE_TIMEOUT)
    except Exception as exc:                                       # noqa: BLE001
        # Отдельный разбор «туннель задан, а ходить в него нечем»: это настройка
        # машины, а не смерть выхода, и человеку нужно сказать именно это.
        why = _proxy_unusable(exc) or f"{type(exc).__name__}: {exc}"
        return {"ok": False, "ms": int((time.time() - started) * 1000), "ip": "",
                "error": _hide_creds(why)[:200]}
    ms = int((time.time() - started) * 1000)
    if resp.status_code >= 400:
        # Через прокси так выглядит отказ авторизации (407) или страница-заглушка:
        # выход отвечает, но наружу не выпускает.
        return {"ok": False, "ms": ms, "ip": "",
                "error": f"адрес проверки ответил {resp.status_code}"}
    return {"ok": True, "ms": ms, "ip": _trace_ip(resp.text), "error": ""}


def _health_row(alive=True, fails=0, checked=0.0, ok_at=0.0, ms=0, ip="", error="") -> dict:
    return {"alive": bool(alive), "fails": int(fails), "checked": float(checked),
            "ok_at": float(ok_at), "ms": int(ms), "ip": str(ip or ""),
            "error": str(error or "")}


def _health_load() -> None:
    """Поднять сведения сторожа из базы — один раз за жизнь процесса.

    После рестарта память пуста, а знание о мёртвом выходе никуда не делось. Без этого
    первые минуты после каждого рестарта система снова ходила бы в мёртвый туннель.
    Протухшие строки отсекает не загрузка, а PROBE_STALE при решении о ротации.
    """
    global _health_loaded
    if _health_loaded:
        return
    _health_loaded = True          # ставим до чтения: не вышло — второй раз не пробуем
    try:
        conn = _cabinet_db()
        try:
            ensure_subs_health_schema(conn)
            rows = conn.execute(
                "SELECT exit_url,alive,fails,checked,ok_at,ms,ip,error "
                "FROM subs_exit_health").fetchall()
        finally:
            conn.close()
    except Exception:
        return                     # база недоступна — сторож просто ничего не знает
    for r in rows:
        _health[r[0]] = _health_row(r[1], r[2], r[3], r[4], r[5], r[6], r[7])


def _health_save(proxy: str, row: dict) -> None:
    """Запомнить состояние выхода в базе.

    Не получилось — молчим: сторож остаётся жить в памяти процесса, а работа выпусков
    не должна зависеть от того, доступна ли табличка со сведениями о туннелях.
    """
    try:
        conn = _cabinet_db()
        try:
            ensure_subs_health_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO subs_exit_health"
                "(exit_url,alive,fails,checked,ok_at,ms,ip,error) VALUES(?,?,?,?,?,?,?,?)",
                (proxy, 1 if row["alive"] else 0, row["fails"], row["checked"],
                 row["ok_at"], row["ms"], row["ip"], row["error"]))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _subs_note_ok(proxy: str, now: float | None = None) -> None:
    """Выход только что отдал субтитры — он жив, и это надёжнее любой проверки.

    Обратное неверно и поэтому здесь не делается: отказ РАБОЧЕГО запроса значит
    «YouTube не дал субтитры этому адресу» (квота, бан, капча) — выход при этом
    совершенно жив, и хоронить его нельзя. Смерть объявляет только сторож.
    """
    now = time.time() if now is None else now
    _health_load()
    row = _health.get(proxy) or _health_row()
    row.update({"alive": True, "fails": 0, "checked": now, "ok_at": now, "error": ""})
    _health[proxy] = row
    _health_save(proxy, row)


def _subs_dead_exits(now: float | None = None) -> set:
    """Выходы, которые сторож СЕЙЧАС считает мёртвыми.

    Незнакомый и протухший выход мёртвым не считается: молчание сторожа — это не
    приговор выходу, а отсутствие сведений, и вести себя надо как до сторожа.
    """
    now = time.time() if now is None else now
    _health_load()
    dead = set()
    for proxy in SUBS_PROXIES:
        row = _health.get(proxy)
        if not row or not row.get("checked"):
            continue
        if now - row["checked"] > PROBE_STALE:
            continue
        if not row["alive"]:
            dead.add(proxy)
    return dead


def subs_probe_due(now: float | None = None) -> bool:
    """Пора ли проверять: хоть один выход не проверялся дольше PROBE_PERIOD."""
    if not SUBS_PROXIES or PROBE_PERIOD <= 0:
        return False
    now = time.time() if now is None else now
    _health_load()
    for proxy in SUBS_PROXIES:
        row = _health.get(proxy)
        if not row or now - row["checked"] >= PROBE_PERIOD:
            return True
    return False


def subs_probe_all(force: bool = False, now: float | None = None) -> dict:
    """Обойти выходы и записать, кто из них жив. Возвращает короткий итог.

    Идём по очереди, а не всеми потоками сразу: два десятка одновременных соединений
    с одной машины — это ровно тот всплеск, за который адрес и получает бан. Обход
    живёт в фоновом потоке, и его длительность никого не задерживает.
    """
    now = time.time() if now is None else now
    _health_load()
    checked = alive = dead = 0
    for proxy in list(SUBS_PROXIES):
        row = _health.get(proxy)
        if not force and row and now - row["checked"] < PROBE_PERIOD:
            continue               # проверен недавно — второй стук ничего не добавит
        res = _probe_exit(proxy)
        row = row or _health_row()
        checked += 1
        stamp = time.time()
        if res["ok"]:
            row.update({"alive": True, "fails": 0, "checked": stamp, "ok_at": stamp,
                        "ms": res["ms"], "ip": res["ip"], "error": ""})
            alive += 1
        else:
            row["fails"] = int(row.get("fails") or 0) + 1
            row["checked"] = stamp
            row["error"] = res["error"]
            # Мёртвым объявляем не с первого промаха: одиночный обрыв бывает у любой
            # связи, а убранный зря выход — это потерянная пропускная способность.
            row["alive"] = row["fails"] < PROBE_FAILS
            if not row["alive"]:
                dead += 1
        _health[proxy] = row
        _health_save(proxy, row)
    return {"checked": checked, "alive": alive, "dead": dead,
            "total": len(SUBS_PROXIES)}


def _probe_worker() -> None:
    global _probe_running
    try:
        subs_probe_all()
    except Exception:
        pass                       # сторож не имеет права уронить процесс кабинета
    finally:
        with _probe_gate:
            _probe_running = False


def subs_probe_async() -> bool:
    """Разбудить сторожа в фоне, если пора. Возвращает, запустился ли обход.

    Главное свойство — не задерживать заказ: вызывающий не ждёт ни ответа сети, ни
    даже базы. Второй поток не поднимается, пока не закончил первый.
    """
    global _probe_running
    with _probe_gate:
        if _probe_running or not subs_probe_due():
            return False
        _probe_running = True
    threading.Thread(target=_probe_worker, name="subs-probe", daemon=True).start()
    return True


def subs_exits_state(c=None, now: float | None = None) -> dict:
    """Состояние всех выходов субтитров одним куском — для плитки пульта.

    `c` — соединение пульта: пульт может жить другим процессом, и тогда память
    сторожа ему не видна, а таблица видна. Без него отвечаем тем, что знает процесс.

    Возвращает готовые к показу поля, а не сырые метки времени: плитке нужно
    «работает / мёртв / отдыхает», а не арифметика по эпохе.
    """
    now = time.time() if now is None else now
    rows = {}
    if c is not None:
        try:
            for r in c.execute("SELECT exit_url,alive,fails,checked,ok_at,ms,ip,error "
                               "FROM subs_exit_health").fetchall():
                rows[r[0]] = _health_row(r[1], r[2], r[3], r[4], r[5], r[6], r[7])
        except Exception:
            rows = {}              # таблицы ещё нет — сторож ни разу не отработал
    else:
        _health_load()
        # Берём по списку выходов, а не копией словаря: словарь в этот момент может
        # дописывать фоновый обход, а список выходов — неподвижная настройка.
        rows = {p: _health[p] for p in SUBS_PROXIES if p in _health}

    with _subs_gate:
        rest = dict(_subs_rest_until)

    exits, alive, dead, unknown, resting = [], 0, 0, 0, 0
    last_check = 0.0
    for proxy in SUBS_PROXIES:
        row = rows.get(proxy)
        fresh = bool(row and row["checked"] and now - row["checked"] <= PROBE_STALE)
        is_dead = bool(fresh and not row["alive"])
        rest_left = max(0.0, float(rest.get(proxy, 0)) - now)
        if not row or not row["checked"]:
            unknown += 1
            state = "не проверялся"
        elif is_dead:
            dead += 1
            state = "мёртв"
        elif not fresh:
            unknown += 1
            state = "сведения устарели"
        else:
            alive += 1
            state = "работает"
        if rest_left > 0:
            resting += 1
            state = "отдыхает" if not is_dead else "мёртв"
        last_check = max(last_check, (row or {}).get("checked") or 0.0)
        exits.append({
            "name": exit_label(proxy),
            "state": state,
            "alive": (None if not row or not row["checked"] else bool(row["alive"])),
            "fresh": fresh,
            "dead": is_dead,
            "resting": rest_left > 0,
            "rest_min": int(rest_left / 60) + 1 if rest_left > 0 else 0,
            "ok_ago": int(now - row["ok_at"]) if row and row["ok_at"] else None,
            "checked_ago": int(now - row["checked"]) if row and row["checked"] else None,
            "ms": (row or {}).get("ms") or 0,
            "ip": (row or {}).get("ip") or "",
            "error": (row or {}).get("error") or "",
        })

    # Два выхода, которые видны снаружи ОДНИМ адресом, — это не два выхода: они делят
    # и квоту YouTube, и репутацию, и ротация между ними ничего не даёт. Показываем
    # факт; что с ним делать — решает владелец, списки мы не переставляем.
    by_ip = {}
    for e in exits:
        if e["ip"]:
            by_ip.setdefault(e["ip"], []).append(e["name"])
    shared_ip = {ip: names for ip, names in by_ip.items() if len(names) > 1}
    for e in exits:
        e["ip_shared"] = e["ip"] in shared_ip

    total = len(SUBS_PROXIES)
    # Если мёртвыми оказались все, ротация им не верит (см. `_subs_live_exits`), и
    # плитка обязана говорить то же самое: иначе экран пугает «всё мертво» там, где
    # система продолжает работать, и владелец идёт чинить не то.
    trusted = not (total and dead == total)
    if not total:
        kind, text = "warn", "выходы не заданы"
    elif unknown == total:
        kind, text = "none", "проверки ещё не было"
    elif not trusted:
        kind, text = "err", ("выход не отвечает на проверку" if total == 1 else
                             f"ни один из {total} выходов не отвечает на проверку")
    elif dead:
        kind, text = "warn", f"{alive} из {total} работают, {dead} не отвечают"
    else:
        kind, text = "ok", "выход работает" if total == 1 else f"все {total} работают"
    if resting:
        text += f", {resting} на отдыхе"
    return {"kind": kind, "text": text, "total": total, "alive": alive, "dead": dead,
            "unknown": unknown, "resting": resting, "trusted": trusted,
            "probe_url": PROBE_URL, "period": PROBE_PERIOD,
            "checked_ago": int(now - last_check) if last_check else None,
            "shared_ip": shared_ip, "running": _probe_running, "exits": exits}


def _subs_cache_row(vid: str) -> dict:
    """Всё, что кэш помнит про ролик: текст, язык, длительность, название.

    Один сюжет идёт на несколько языковых каналов, и без кэша каждый канал качал бы
    один и тот же ролик заново — на нашей волне это 330 лишних скачиваний из 574,
    то есть ровно та нагрузка, из-за которой YouTube и закрывает адрес. Проверка
    ссылки перед постановкой в план добавляет к этому ещё по обращению на ссылку,
    поэтому она читает и пишет тот же самый кэш.

    Пустой ответ означает «не знаем» — и когда ролика в кэше нет, и когда сам кэш
    недоступен: он ускорение, а не условие работы.

    Читаем только те колонки, которые в таблице действительно есть: если досыпка
    новых не прошла, текст расшифровки всё равно обязан находиться — иначе один
    неудавшийся ALTER отправляет в сеть заново ВСЮ волну.
    """
    if not vid:
        return {}
    try:
        conn = _subs_cache_db()
        try:
            have = _subs_cache_cols(conn)
            want = [name for name in ("text", "created", "lang", "seconds",
                                      "title", "title_at") if name in have]
            if "text" not in want:          # такого кэша не бывает — это не наша таблица
                return {}
            # Имена колонок — наши собственные литералы выше, из запроса в SQL
            # подставляется только их подмножество; данные по-прежнему параметром.
            row = conn.execute(f"SELECT {', '.join(want)} FROM subs_cache "
                               "WHERE video_id=?", (vid,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    got = dict(zip(want, row))
    return {"text": got.get("text") or "", "created": float(got.get("created") or 0),
            "lang": got.get("lang") or "", "seconds": float(got.get("seconds") or 0),
            "title": got.get("title") or "", "title_at": float(got.get("title_at") or 0)}


def _subs_cache_get(vid: str) -> str:
    """Ранее скачанные субтитры этого ролика — или пусто."""
    return _subs_cache_row(vid).get("text") or ""


def _subs_cache_put(vid: str, text: str, lang: str = "", seconds: float = 0.0) -> None:
    """Запомнить расшифровку вместе с языком дорожки и длиной ролика.

    Название НЕ трогаем: оно лежит в той же строке, но приходит другим путём (oembed) и
    переживает пересказ субтитров. Поэтому здесь не `INSERT OR REPLACE`, который затёр
    бы его пустым, а точечное обновление своих колонок.

    Пишем по тем колонкам, которые есть: без `lang`/`seconds` (досыпка не прошла)
    расшифровка всё равно обязана лечь в кэш. Потерять её нельзя — она добыта
    дефицитным выходом, и второй раз за ней пойдут в сеть.
    """
    try:
        conn = _subs_cache_db()
        try:
            have = _subs_cache_cols(conn)
            cols = ["video_id", "text", "created"]
            values = [vid, text, time.time()]
            if "lang" in have:
                cols.append("lang")
                values.append(str(lang or ""))
            if "seconds" in have:
                cols.append("seconds")
                values.append(float(seconds or 0))
            sets = ", ".join(f"{name}=excluded.{name}" for name in cols[1:])
            conn.execute(
                f"INSERT INTO subs_cache({', '.join(cols)}) "
                f"VALUES({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(video_id) DO UPDATE SET {sets}", values)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                                       # noqa: BLE001
        # Своего журнала у модуля нет, а молчать здесь дороже всего: расшифровку добыл
        # дефицитный выход, и раз она не легла в кэш — за ней пойдут в сеть ещё раз.
        # stdout ловит systemd, и в journalctl это видно наравне с остальным кабинетом.
        print(f"[subs] кэш не принял расшифровку {vid}: {type(exc).__name__}: "
              f"{_hide_creds(exc)[:200]}", flush=True)


def donor_cache_title(vid: str) -> tuple:
    """Запомненное название донора и время, когда его запомнили. ("", 0) — не знаем."""
    row = _subs_cache_row(vid)
    return (row.get("title") or "", float(row.get("title_at") or 0))


def donor_cache_put_title(vid: str, title: str, now: float = None) -> None:
    """Запомнить название донора, не тронув расшифровку.

    Строка кэша может появиться раньше субтитров: проверка ссылки узнаёт название
    бесплатно (oembed), а до субтитров дело может не дойти вовсе — выключены настройкой
    или кончился бюджет времени. Пустой текст в такой строке читается как «расшифровки
    не знаем», и `donor_text` спокойно идёт за ней в сеть.
    """
    if not vid:
        return
    try:
        conn = _subs_cache_db()
        try:
            if not {"title", "title_at"} <= _subs_cache_cols(conn):
                return    # колонок названия нет (досыпка не прошла) — спросим oembed ещё
            conn.execute(
                "INSERT INTO subs_cache(video_id,text,created,title,title_at) "
                "VALUES(?,'',?,?,?) ON CONFLICT(video_id) DO UPDATE SET "
                "title=excluded.title, title_at=excluded.title_at",
                (vid, time.time() if now is None else float(now), str(title or "")[:300],
                 time.time() if now is None else float(now)))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _subs_seconds(parts) -> float:
    """Длина ролика по последней реплике дорожки: начало + длительность.

    Бесплатный способ узнать длительность. Точный ответ даёт `videos.list`
    (`part=contentDetails`), но он стоит единицу квоты, а ключ YouTube Data API у нас
    ОДИН на всё: им живёт проверка «ролик действительно вышел» и ночной обход ВВ2, и
    18.09 общий суточный кошелёк кончился в час ночи. Двести вставленных ссылок — это
    двести единиц, отнятых у проверки публикаций, ради числа, которое субтитры и так
    приносят с собой. Оценка снизу (после последней реплики бывает тишина), для
    потолка длительности этого достаточно.
    """
    last = 0.0
    for p in parts or ():
        try:
            if isinstance(p, dict):
                start, dur = float(p.get("start") or 0), float(p.get("duration") or 0)
            else:
                start = float(getattr(p, "start", 0) or 0)
                dur = float(getattr(p, "duration", 0) or 0)
        except (TypeError, ValueError):
            continue
        last = max(last, start + dur)
    return last


def _track_code(track) -> str:
    return str(getattr(track, "language_code", "") or "")


def _pick_track(tracks: list):
    """Дорожка субтитров донора: язык оригинала, ручная версия предпочтительнее.

    Авто-дорожку YouTube делает только для языка оригинала — по ней и опознаём язык.
    Ручные дорожки чаще переводы и приходят по алфавиту, поэтому «первая» ничего не
    значит. Текст берём ручной того же языка: в нём есть пунктуация.
    """
    for track in tracks:
        if getattr(track, "is_generated", False):
            code = _track_code(track)
            for want_generated in (False, True):
                for other in tracks:
                    if (_track_code(other) == code
                            and bool(getattr(other, "is_generated", False)) == want_generated):
                        return other
            return track
    return tracks[0]


def donor_text(url: str) -> str:
    """Текст донорского ролика из субтитров YouTube.

    Тонкая обёртка над `donor_subs`: движку выпусков нужен только текст, и менять ему
    вызов ради полей, которые он не читает, не за что.
    """
    return donor_subs(url)["text"]


def donor_subs(url: str, *, net: bool = True, strict: bool = True,
               deadline: float = None, max_exits: int = None) -> dict:
    """Расшифровка донора и всё, что известно про неё саму.

    Возвращает {"text", "chars", "lang", "seconds", "cached"}. `lang` — код дорожки,
    по которой узнаётся язык оригинала; `seconds` — длина ролика, посчитанная по
    последней реплике (см. `_subs_seconds`), 0 означает «не знаем».

    Пробуем выходы по очереди: отказ по адресу — это исчерпанная квота ЭТОГО выхода,
    а не беда ролика. Кончились все выходы — ждёт вся очередь, строка остаётся живой.

    `net=False` — только кэш: пустой словарь вместо похода в сеть. Так проверка ссылок
    отвечает бесплатно, когда бюджет времени кончился или выходы выжжены.

    `strict=False` — вернуть короткую расшифровку, а не отказать по ней. Движку нужен
    отказ: обрывок дорожки нельзя отдавать в оплачиваемый рерайт. Проверке ссылки нужно
    ЧИСЛО — она показывает человеку, сколько знаков пришло, и решает по своим порогам.

    `deadline` — момент, после которого перебор выходов прекращается. Без него один
    заказ при мёртвом туннеле обходит ВСЕ выходы по `SUBS_TIMEOUT` каждый: на 21 выходе
    это почти девять минут на одну ссылку, а бюджет пачки в предпросмотре обещает
    двадцать секунд — и спрашивался он только ПЕРЕД походом, уже начатый не обрывая.
    Ночному выпуску срок не нужен: он никого не держит перед экраном, и ему важнее
    дойти до последнего выхода.

    `max_exits` — сколько выходов вообще разрешено опросить за один вызов. Выходы —
    общий дефицитный ресурс: их жжёт ночной шаг «текст», и отказ каждого отправляет
    выход отдыхать для ВСЕЙ системы. Предпросмотр листает ссылки пачками, и без потолка
    два десятка проверок в плохой день уводят на отдых все выходы перед ночным
    прогоном. None — без потолка, так ходит сам выпуск.
    """
    vid = video_id(url)
    if not vid:
        raise _subs_failed(FAIL_NOT_YT, f"не разобрал ссылку на ролик: {url[:80]}")
    row = _subs_cache_row(vid)
    if row.get("text"):
        # этот ролик уже качали для соседнего канала
        return {"text": row["text"], "chars": len(row["text"]), "lang": row.get("lang") or "",
                "seconds": float(row.get("seconds") or 0), "cached": True}
    if not net:
        return {}

    try:
        from youtube_transcript_api import YouTubeTranscriptApi   # noqa: F401
    except ImportError:
        raise _subs_down(DOWN_NO_LIB, "на сервере нет youtube-transcript-api")

    exits = _subs_live_exits()
    if not exits:
        # Пустой список означает именно отдых: мёртвым выходам ротация не верит, когда
        # мёртвыми вышли все, и возвращает полный список (см. `_subs_live_exits`).
        raise _subs_down(DOWN_QUOTA,
                         f"все выходы исчерпали квоту, ближайший через {_subs_wait_left()} мин")

    if max_exits is not None and int(max_exits) > 0:
        exits = exits[:int(max_exits)]

    last_block = None
    tried = 0
    for proxy in exits:
        if deadline is not None and time.time() >= deadline:
            # Срок вышел посреди перебора. Возвращаем именно «не успели», а не «квота»
            # и не «туннель»: про выходы мы в этот момент ничего нового не знаем, а
            # спутать «мы не дождались» с «адрес выжжен» значит соврать в обе стороны.
            raise _subs_down(DOWN_BUDGET,
                             f"не успели за отведённое время: опрошено выходов {tried} "
                             f"из {len(exits)}")
        tried += 1
        try:
            api = _transcript_api(proxy)
            tracks = list(api.list(vid))
            if not tracks:
                raise _subs_failed(FAIL_NO_SUBS, "у ролика нет субтитров")
            track = _pick_track(tracks)
            lang = _track_code(track)      # язык оригинала — по дорожке, а не по догадке
            fetched = track.fetch()
            parts = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
        except (ItemFailed, ResourceDown):
            raise
        except Exception as exc:                       # noqa: BLE001
            why = _proxy_unusable(exc)
            if why:
                raise _subs_down(DOWN_TUNNEL, why)
            if _is_exit_fault(exc):
                _subs_rest(proxy)      # этот выход исчерпан — пробуем следующий
                last_block = exc
                continue
            if _no_subtitles(exc):
                # «Недоступно» — приговор только если ролика действительно нет. Живой
                # ролик, который нам сейчас не отдали, обязан подождать, а не попасть
                # в разбор как битый донор.
                if (type(exc).__name__ in _AMBIGUOUS_UNAVAILABLE
                        and not _video_really_gone(vid)):
                    raise _subs_down(
                        DOWN_OURS,
                        f"YouTube не отдал субтитры ({type(exc).__name__}), но ролик на "
                        f"месте — это отказ нам, а не свойство ролика; строка подождёт")
                raise _subs_failed(FAIL_NO_SUBS,
                                   f"у ролика нет субтитров ({type(exc).__name__})")
            # Всё прочее — наша поломка, а не свойство ролика. Одна опечатка в коде
            # успела похоронить 48 исправных выпусков как «без субтитров»: строка живёт,
            # ждёт и пойдёт снова, когда поломку починят.
            raise _subs_down(DOWN_OURS, f"сбой на нашей стороне: {type(exc).__name__}")

        # Выход только что сходил в YouTube и вернулся — это и есть лучшая проверка
        # живости, какая у нас бывает. Заодно отодвигает отдельный стук сторожа:
        # проверять то, что и так работает, незачем.
        _subs_note_ok(proxy)
        text = " ".join((p.get("text") if isinstance(p, dict) else getattr(p, "text", "")) or ""
                        for p in parts).strip()
        if len(text) < 200 and strict:
            raise _subs_failed(FAIL_THIN, "субтитры донора слишком короткие или пустые")
        seconds = _subs_seconds(parts)
        if len(text) >= 200:
            # Обрывок в кэш не кладём: там он стал бы ответом и для движка, который
            # такой текст обязан отклонить, — и отклонял бы его уже без похода в сеть,
            # то есть навсегда.
            _subs_cache_put(vid, text, lang, seconds)
        return {"text": text, "chars": len(text), "lang": lang, "seconds": seconds,
                "cached": False}

    # Сюда приходим, перебрав все выходы. Чем они негодны — знает последний отказ:
    # 429 это квота, обрыв туннеля — не она, и проверка донора скажет про них разное.
    # Текст тоже разный: «подождите полчаса» при мёртвом туннеле — ложный совет, ждать
    # там нечего, и владелец из-за него не пойдёт чинить то единственное, что сломано.
    kind = _exit_fault_kind(last_block) if last_block is not None else DOWN_QUOTA
    if kind == DOWN_QUOTA:
        raise _subs_down(kind,
                         f"все выходы исчерпали квоту, ближайший через {_subs_wait_left()} мин")
    raise _subs_down(kind, "ни один выход субтитров не ответил: "
                           f"{type(last_block).__name__} — похоже, туннель не поднят")


# ─────────────────────────── рерайт ───────────────────────────

class RemoteJobMissing(ItemFailed):
    """Задача, номер которой мы храним, у соседа не существует (404/410).

    Отдельный класс, а не общий отказ: на это нельзя отвечать повтором. Повтор здесь
    означал бы «закажу заново», то есть вторую оплату того же текста. Строка встаёт и
    ждёт человека — он либо найдёт задачу, либо разрешит платить снова.
    """


# Сколько времени живёт запомненный список моделей соседа. Список меняется редко —
# правкой переменной окружения у рерайта и его рестартом, — а заказы идут пачками:
# двадцать выпусков подряд спросили бы одно и то же двадцать раз. Пять минут снимают
# почти все повторные запросы и при этом не требуют нашего рестарта после правки
# у соседа. Ноль отключает память полностью (для разбора инцидента).
MODELS_TTL = float(os.environ.get("VS_REWRITE_MODELS_TTL", "300"))
# отпечаток (адрес + ключ) → (до какого времени годен, список моделей)
_models_cache = {}
_models_gate = threading.Lock()

# Что сказать человеку, когда модель выбрать не из чего. Молча подставить литерал
# нельзя: именно так у нас и появился зашитый gpt-4.1-nano, которого с 17.09 нет в
# списке серверных ключей рерайта.
CHOOSE_MODEL = "выберите модель в пресете рерайта этого канала"


def _parse_models(body) -> list:
    """Ответ соседа на /models → [{"value": имя, "default": помечена ли}].

    Формат разбираем терпимо нарочно. Боевой рерайт отдаёт сессии список словарей
    `{value,label,default}` (live_2026-09-18/код/рерайт/server.py:2283); ручку по
    ключу `GET /api/v1/models` мы дописали сами 19.09 (studio-platform/rewrite/api.py)
    и держим ровно тот же формат. Но ребейз форка 31.08 → 18.09 (B7) накладывает наши
    правки заново, и ответ может приехать обёрнутым (`{"models": [...]}`) или списком
    строк. Упасть из-за обёртки, когда нужное имя в ответе есть, значило бы остановить
    очередь на ровном месте.
    """
    if isinstance(body, dict):
        for field in ("models", "items", "data"):
            if isinstance(body.get(field), list):
                body = body[field]
                break
    if not isinstance(body, list):
        return []
    out = []
    for item in body:
        if isinstance(item, str):
            value, marked = item.strip(), False
        elif isinstance(item, dict):
            value = str(item.get("value") or item.get("id") or item.get("model")
                        or item.get("name") or "").strip()
            marked = bool(item.get("default") or item.get("is_default"))
        else:
            continue
        if value:
            out.append({"value": value, "default": marked})
    return out


def _about_model(detail: str, model: str) -> bool:
    """Отказ соседа именно про модель, а не про язык или размер куска.

    Судим по тексту ответа: имя модели в нём или слово «модель». Текст отказа у
    рерайта свой (`server_key_model_error`), кода ошибки для этого случая нет —
    422 у него один на всё. Ошибиться безопасно: не узнали — покажем отказ как есть.
    """
    low = (detail or "").lower()
    if "модел" in low or "model" in low:
        return True
    return bool(model) and model.lower() in low


# ─── Пределы, которыми сосед зажимает тело заказа ПЕРЕД своим отпечатком ───
# Числа не из головы: это `ApiJobCreate` в studio-platform/rewrite/api.py —
# `_clamp_chunk_size` (500..20000), `_clamp_max_threads` (1..API_MAX_THREADS_PER_JOB)
# и `model_config(str_strip_whitespace=True)`, которая обрезает пробелы всем строкам,
# включая `languages[].code`. Переменная и её умолчание тоже соседские (api.py:71).
#
# ЭТА СВЯЗЬ ОБЯЗАНА ОБНОВЛЯТЬСЯ ВМЕСТЕ С СОСЕДОМ. Смысл нормализации один: «ключ ⇔
# тело» должно быть взаимно однозначным. Если у соседа границы разъедутся с нашими,
# вернётся ровно то, от чего мы уходим, — в одну сторону 409 навсегда (наш ключ
# совпал, тело у соседа разное), в другую ВТОРАЯ ОПЛАЧЕННАЯ задача (наш ключ разный,
# тело у соседа одно и то же). Связь держит tests/test_rewrite_order_key.py: он
# сверяет эти числа с самим `rewrite/api.py`, а не с их пересказом.
ORDER_CHUNK_MIN, ORDER_CHUNK_MAX = 500, 20000
try:
    ORDER_THREADS_MAX = max(
        1, min(16, int(os.environ.get("REWRITE_MAX_THREADS_PER_JOB", "3")))
    )
except (TypeError, ValueError):
    ORDER_THREADS_MAX = 3


def _normalized_order(body: dict) -> dict:
    """Тело заказа в том виде, в каком его увидит сосед ПОСЛЕ разбора.

    Зачем вообще. Сосед считает свой отпечаток не по присланному телу, а по телу,
    прошедшему через `ApiJobCreate`: там пробелы у строк обрезаны, размер куска зажат
    в 500..20000, число потоков — в 1..ORDER_THREADS_MAX (по умолчанию 3, а форма
    пульта при этом разрешает 1..8).
    Значит поля, которые он схлопывает, наш ключ различать не имеет права: правка
    «потоков» с 4 на 8 в том же пресете давала бы НАШ новый ключ при ТОМ ЖЕ теле у
    соседа, и после потерянного ответа на POST сосед создал бы вторую оплаченную
    задачу вместо возврата первой.

    Нормализованное тело и отправляется, и хэшируется — одно и то же, чтобы «что
    послали» и «под каким ключом послали» нельзя было развести правкой в одном месте.
    """
    def trimmed(value):
        """Пробелы по краям строк — их сосед срезает у ВСЕХ полей сразу
        (`str_strip_whitespace=True`), в том числе у вложенных `languages[].code`
        и у `files[]`. Остальное отдаём как есть: числа и флаги он не трогает."""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return {key: trimmed(inner) for key, inner in value.items()}
        if isinstance(value, list):
            return [trimmed(item) for item in value]
        return value

    out = {name: trimmed(value) for name, value in (body or {}).items()}
    if "chunk_size" in out:
        out["chunk_size"] = max(ORDER_CHUNK_MIN,
                                min(ORDER_CHUNK_MAX, int(out["chunk_size"])))
    if "max_threads" in out:
        out["max_threads"] = max(1, min(ORDER_THREADS_MAX, int(out["max_threads"])))
    return out


def _order_digest(body: dict, source_sha: str) -> str:
    """Отпечаток заказа рерайта — ровно то, что сосед прячет под ключом повтора.

    `files` подменяется хэшем СОДЕРЖИМОГО донора, потому что так делает и сосед
    (`api.create_job`: `fingerprint['files'] = [file_hash(...)]`). Иначе отпечаток
    менялся бы на каждом тике: `upload_text` выдаёт новый `file_id` каждый раз, и
    защита от второй оплаты не сработала бы ни разу.

    Тело приводится к соседскому виду (`_normalized_order`) — иначе отпечаток видел
    бы разницу там, где сосед её уже стёр, и различал бы два одинаковых для него
    заказа. Цена такой разницы — вторая оплата.
    """
    if not source_sha:
        # Без хэша донора отпечаток не покрывает текст: перекачанные субтитры дали бы
        # тот же ключ с другим телом, то есть ровно тот 409-навсегда, от которого мы
        # здесь и уходим. Это может случиться только от правки кода, поэтому падаем
        # громко и до обращения к соседу — денег такой отказ не стоит.
        raise ValueError("ключ повтора заказа рерайта требует отпечатка исходника")
    shape = dict(_normalized_order(body), files=[source_sha])
    blob = json.dumps(shape, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class RewriteClient:
    def __init__(self, key: str, base: str = REWRITE_BASE):
        self.key = key
        self.base = base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1{path}"

    def upload_text(self, text: str, filename: str = "donor.txt") -> str:
        """Имя файла становится именем результата и режется до 50 символов: одинаковые
        имена у двух задач затирают друг друга, поэтому имя обязано быть уникальным."""
        resp = _call("POST", self._url("/files"), self.key, "рерайт",
                     files={"file": (filename, text.encode("utf-8"), "text/plain")})
        return resp.json()["file_id"]

    def preset_language(self, preset: dict, lang_code: str) -> dict:
        """Языковой набор пресета для нашего языка: там лежат имя ведущего и стиль."""
        want = (lang_code or "").lower()
        ru = lang_ru(lang_code).lower()
        for entry in (preset or {}).get("languages") or []:
            if not isinstance(entry, dict):
                continue
            for field in ("code", "display"):
                if str(entry.get(field) or "").lower() in (want, ru):
                    return entry
        return {}

    def models(self) -> list:
        """Модели, которые сосед готов принять ОТ ЭТОГО КЛЮЧА.

        Список зависит от ключа: кабинету на серверном ключе рерайт с 17.09 разрешает
        только свой короткий перечень, а кабинету со своим ключом провайдера — прежний
        общий. Поэтому и память по ключу, а не общая на процесс. Сам ключ в памяти не
        лежит: ключом служит его отпечаток, чтобы секрет не всплыл в дампе состояния.
        """
        slot = hashlib.sha256(f"{self.base}\n{self.key}".encode("utf-8")).hexdigest()[:16]
        now = time.time()
        with _models_gate:
            cached = _models_cache.get(slot)
            if cached and cached[0] > now:
                return list(cached[1])
        resp = _call("GET", self._url("/models"), self.key, "рерайт")
        try:
            body = resp.json()
        except ValueError as exc:
            # Код 200, а тело не JSON — так выглядит чужой прокси, страница входа или
            # обрезанный ответ. Переводим здесь, чтобы наружу из клиента уходил ровно
            # один класс отказа: иначе ValueError улетает мимо `except ItemFailed`
            # в общий обработчик движка, и человек видит загадочную поломку вместо
            # понятного «список моделей получить не удалось».
            raise ItemFailed(f"рерайт ответил на список моделей не-JSON: {exc}") from exc
        items = _parse_models(body)
        # Запоминаем только НЕПУСТОЙ разбор. Отказ должен спрашиваться заново, иначе
        # починка у соседа не видна нам ещё пять минут. Пустой разбор при коде 200 —
        # это тот же отказ, только замаскированный: у соседа поменялась форма ответа
        # или пропали кредиты провайдера. Запомнить его значит увести в карантин всю
        # идущую следом пачку выпусков и не заметить, что сосед уже починился.
        if items:
            with _models_gate:
                _models_cache[slot] = (now + MODELS_TTL, items)
        return list(items)

    def resolve_model(self, preset: dict) -> str:
        """Какой моделью заказывать рерайт.

        Первое слово за пресетом: это выбор владельца канала. Пусто — спрашиваем
        соседа и берём ту, которую он пометил как «по умолчанию». До 19.09.2026 здесь
        стоял литерал `gpt-4.1-nano`; с 17.09 рерайт на серверном ключе отвечает на
        него 422 (`rewrite_core.server_key_model_error`), то есть в момент снятия паузы
        все наши заказы встали бы разом.

        Не спросили список — честный отказ, а не другой литерал. Подстановка наугад
        стоит денег: модели в списке различаются ценой в разы, и выбирать за владельца
        мы не вправе.
        """
        chosen = str((preset or {}).get("model") or "").strip()
        if chosen:
            return chosen
        try:
            items = self.models()
        except ItemFailed as exc:
            # Сюда приходит всё, чем сосед может не отдать список: 404 (сервис старше
            # ручки `/api/v1/models`), 400/422, а также не-JSON в теле — `models()`
            # переводит его в ItemFailed сам, чтобы здесь не было второго класса.
            # Это настройка канала, а не авария ресурса: ждать нечего.
            raise ItemFailed(f"в пресете не выбрана модель, а список моделей рерайта "
                             f"получить не удалось ({exc}) — {CHOOSE_MODEL}") from exc
        marked = [item["value"] for item in items if item["default"]]
        if marked:
            return marked[0]
        if len(items) == 1:
            return items[0]["value"]      # выбора нет ни у нас, ни у владельца
        if not items:
            raise ItemFailed(f"в пресете не выбрана модель, а сосед вернул пустой "
                             f"список моделей — {CHOOSE_MODEL}")
        names = ", ".join(item["value"] for item in items[:10])
        raise ItemFailed(f"в пресете не выбрана модель, а сосед не пометил ни одну из "
                         f"{len(items)} своих как «по умолчанию» ({names}) — {CHOOSE_MODEL}")

    def create_job(self, file_id: str, lang_code: str, preset: dict,
                   channel_name: str = "", idempotency_key: str = "",
                   model: str = "", source_sha: str = "") -> str:
        preset = preset or {}
        lang_entry = self.preset_language(preset, lang_code)
        # Модель выясняется здесь, до тела запроса. Явный аргумент сильнее пресета:
        # он нужен тому, кто пересоздаёт заказ после отказа «модель не принята» и уже
        # знает, чем заменить. Пусто — обычный порядок: пресет, потом подсказка соседа.
        model = str(model or "").strip() or self.resolve_model(preset)
        # Тело сразу приводится к соседскому виду (`_normalized_order`): пробелы
        # обрезаны, размер куска и число потоков зажаты его же границами. Сосед всё
        # это делает и сам, но тогда ОН считал бы отпечаток по зажатому телу, а МЫ
        # ключ — по сырому: одинаковый для него заказ уехал бы под двумя ключами, и
        # после потерянного ответа на POST был бы оплачен дважды. Отправляем и хэшируем
        # ровно одно тело.
        body = _normalized_order({
            "files": [file_id],
            # В languages допустим ТОЛЬКО code: сервис запрещает лишние поля,
            # название канала передаётся отдельным полем channel_name.
            "languages": [{"code": lang_ru(lang_code)}],
            "model": model,
            "chunk_size": int(preset.get("chunk") or 3500),
            "preserve_paragraphs": bool(preset.get("preserve_paragraphs", True)),
            # Длину режем ДО нормализации: сосед сначала срезает пробелы и только
            # потом меряет длину, так что обрезанный нами хвост ему не мешает.
            "address_style": (preset.get("address") or "")[:8],
            "channel_name": (channel_name or lang_entry.get("channel") or "")[:80],
            # Имя ведущего живёт в языковом наборе пресета; без него рерайт подставит
            # своё умолчание, и в intro/outro поедет чужое имя.
            "host_name": str(lang_entry.get("host") or "")[:80],
            "safe_text": bool(preset.get("safe_text", False)),
            "max_threads": int(preset.get("threads") or 1),
        })
        # Ключ повтора (перенесено из production 14.09): если ответ на POST потерялся
        # по дороге, второй такой же запрос вернёт ТУ ЖЕ задачу, а не создаст вторую.
        # Сосед узнаёт заказ по отпечатку тела, где вместо file_id стоит хэш содержимого
        # текста; тот же ключ с ДРУГИМ телом он обязан отвергнуть — навсегда, 409.
        #
        # 19.09.2026, ребейз B7: с этого дня сосед ключ действительно ПРОВЕРЯЕТ, и
        # `release:<id>` в одиночку стал опасен. Выпуск живёт неделями, а тело заказа
        # за это время меняется от вещей, которых никто не считает «новым заказом»:
        # поправили размер куска или число потоков в том же пресете, переименовали
        # канал, перекачали субтитры донора, сосед подсказал другую модель по пустому
        # `model` в пресете. Каждый такой случай — тот же ключ с другим телом, то есть
        # 409 НАВСЕГДА: выпуск краснеет и сам уже не починится.
        #
        # Поэтому ключ = выпуск + отпечаток РОВНО ТОГО, что сосед сравнивает. Тогда
        # «тот же заказ» (потерянный ответ, повторный тик) ловится и второй раз не
        # оплачивается, а «другой заказ» честно создаётся заново вместо вечного 409.
        # Брать сюда pub_calls.fingerprint нельзя: он намеренно считает по ИМЕНИ пресета
        # и модели ИЗ пресета (см. pub_services.do_source_ready) — он отвечает на вопрос
        # «за это уже платили?», а не «это тот же запрос?». Правка пресета на месте его
        # не меняет, а тело заказа меняет — и 409-навсегда осталось бы.
        if idempotency_key:
            idempotency_key = f"{idempotency_key}:{_order_digest(body, source_sha)}"
        try:
            resp = _call("POST", self._url("/jobs"), self.key, "рерайт", json=body,
                         headers={"Idempotency-Key": idempotency_key}
                         if idempotency_key else {})
        except RemoteRejected as exc:
            # 422 про модель — это не загадочная поломка, а решение, которое человек
            # может принять за минуту. Показываем его именно так.
            if _about_model(str(exc), model):
                raise ItemFailed(f"сосед не принял модель «{model}»: {exc}. "
                                 f"Выберите другую модель в пресете рерайта "
                                 f"этого канала") from exc
            raise
        return resp.json()["job_id"]

    def job(self, job_id: str) -> dict:
        return _call("GET", self._url(f"/jobs/{job_id}"), self.key, "рерайт",
                     missing_job=True).json()

    def retry(self, job_id: str) -> dict:
        """Возобновить ТУ ЖЕ задачу соседа, не заказывая новую.

        03.10.2026. У соседа прогресс хранится по кускам и привязан к номеру задачи
        (PROGRESS_MANAGER): `/retry` дописывает только незавершённые куски, готовые не
        переделываются и второй раз не оплачиваются. Новый заказ того же текста начал
        бы с первого куска — для ролика на час это 14 оплаченных кусков из 18 в мусор.
        Номер задачи остаётся прежним, поэтому журнал платных вызовов тут не трогаем:
        денег это событие не стоит.
        """
        return _call("POST", self._url(f"/jobs/{job_id}/retry"), self.key, "рерайт",
                     missing_job=True).json()

    def result_code(self, info: dict, lang_code: str) -> str:
        """Под каким кодом сервис положил результат.

        Короткие коды у него свои и местами неожиданные (португальский — `po`,
        хорватский — `cr`), поэтому код берём из ответа задачи, а не строим сами.
        """
        files = (info or {}).get("output_files") or {}
        if not files:
            raise ItemFailed("рерайт отметил задачу готовой, но файла результата нет")
        want = (lang_code or "").lower()
        for code in files:
            if code.lower() == want:
                return code
        return list(files)[0]

    def result_text(self, job_id: str, code: str) -> str:
        resp = _call("GET", self._url(f"/jobs/{job_id}/files/{code}"), self.key, "рерайт")
        return resp.text


# ─────────────────────────── обложки ───────────────────────────

class CoverClient:
    def __init__(self, key: str, base: str = COVER_BASE):
        self.key = key
        self.base = base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1{path}"

    def create_job(self, preset_name: str, lang_code: str, donor_url: str) -> str:
        """Обложка по донорской ссылке.

        Свой заголовок не придумываем: сервис сам скачивает превью донора, разбирает
        текст на нём и переводит на язык канала. Режим `title` потребовал бы готового
        заголовка, которого на этой стадии ещё нет.
        """
        body = {"mode": "url", "preset_name": preset_name,
                "languages": [lang_ru(lang_code)], "urls": [donor_url]}
        resp = _call("POST", self._url("/jobs"), self.key, "обложки", json=body)
        return resp.json()["job_id"]

    def job(self, job_id: str) -> dict:
        return _call("GET", self._url(f"/jobs/{job_id}"), self.key, "обложки").json()

    def download(self, job_id: str, lang_code: str) -> bytes:
        resp = _call("GET", self._url(f"/jobs/{job_id}/files/{lang_ru(lang_code)}"),
                     self.key, "обложки")
        return resp.content


def cover_degraded(job: dict) -> bool:
    """Обложка вышла бракованной: текста на ней нет вовсе либо он нарисован с ошибками.

    Сервис делает несколько попыток нарисовать текст и проверяет результат; не вышло —
    помечает файл как fallback. Смотреть только на статус задачи значит принять такой
    брак за нормальную обложку.

    Оговорка: если у пресета выключена проверка текста, брак не помечается вообще —
    тогда этот признак ничего не поймает, и качество остаётся на совести пресета.
    """
    if not isinstance(job, dict):
        return False
    if job.get("fallback_count"):
        return True
    return bool(job.get("fallback_files"))
