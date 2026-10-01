#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Теги каналов и очередь донорских ссылок.

Теги — единственный способ группировать каналы. Язык и ниша тоже показываются тегами,
но хранятся своими колонками: от языка зависит часовой пояс и флажок, от ниши — ничего,
кроме показа. Свои теги владелец добавляет сам, их список ничем не ограничен.

Тег НИКОГДА не определяет, какой ролик куда пойдёт: он только ищет и фильтрует.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

from channel_slots import next_free_slot, upcoming_slots
from pub_ids import video_id

OEMBED = "https://www.youtube.com/oembed"
YT_HOSTS = ("youtube.com", "youtu.be")


def donor_title(url: str) -> str:
    """Оригинальное название донорского ролика.

    Через открытый oembed YouTube — без ключей и квот. Не ответил или ролик закрыт —
    возвращаем пусто: показывать вместо названия ссылку хуже, но терять строку нельзя.

    Почему именно oembed, а не `videos.list`. Ключ YouTube Data API у нас ОДИН на всё:
    им живёт поздняя проверка выхода (`pub_verify`) и ночной обход конкурентов ВВ2,
    и суточный кошелёк у них общий — 18.09 он кончился в 01:00, и ночь встала.
    Вставка двухсот ссылок здесь не должна съедать единицы у проверки публикаций.
    Если когда-нибудь понадобится то, чего oembed не отдаёт (длина, дата, статус
    донора), это уже `videos.list` — и такой вызов обязан идти через учёт
    `pub_calls.quota_allow()` / `quota_spend()` под именем потребителя «donor»,
    иначе общий кошелёк снова будет опустошать тот, кто про него не знает.
    """
    if not any(h in (url or "") for h in YT_HOSTS):
        return ""          # не YouTube — спрашивать не у кого, в сеть не ходим
    try:
        import requests
        r = requests.get(OEMBED, params={"url": url, "format": "json"}, timeout=6)
        if r.status_code != 200:
            return ""
        return (r.json().get("title") or "").strip()[:300]
    except Exception:
        return ""

URL_RE = re.compile(r"https?://\S+")


# ───────────── проверка донора ДО постановки в план (19.09.2026) ─────────────
#
# Зачем. Ролик без субтитров сегодня узнаётся только на шаге «текст»: ссылка уже
# вставлена, слот канала уже занят, оператор уже отработал, а выпуск падает на первом
# же платном шаге и оставляет дыру в расписании. Спросить то же самое можно в момент
# вставки и бесплатно — субтитры всё равно придётся качать, и скачанное здесь ложится
# в тот же кэш `subs_cache`, откуда его возьмёт сам выпуск. То есть проверка не
# добавляет работы, а переносит её на день раньше.
#
# Два закона этого кода, оба дороже точности:
#
# 1. **Ничего платного.** Ни одной единицы квоты YouTube Data API: ключ один на всё,
#    и 18.09 общий суточный кошелёк кончился в час ночи. Длительность ролика берём из
#    последней реплики субтитров (`pub_remote._subs_seconds`), название — из открытого
#    oembed. Двести вставленных ссылок не имеют права отнять у проверки публикаций
#    двести единиц ради числа, которое и так приезжает с текстом.
# 2. **«Не смогли проверить» ≠ «донор плохой».** Мёртвый туннель и исчерпанная квота
#    выхода — это наша беда, а не свойство ролика, и они НИКОГДА не отклоняют ссылку;
#    в ответе они названы разными причинами (`exit_dead` и `exits_quota`), потому что
#    первую чинит владелец, а вторая проходит сама. Перепутав их с «нет субтитров»,
#    пульт начнёт браковать хорошие ссылки ровно в ту минуту, когда лёг туннель, —
#    именно этой ошибкой уже было похоронено сорок с лишним исправных выпусков.

# Пороги задаются только через .env или прямую запись в `admin_state` — экрана, который бы
# их правил, в системе нет (проверено: такого маршрута в server/main.py не существует).
DONOR_LIMITS_STATE = "donor_limits"      # ключ в admin_state

# Пороги. Владелец называл из них ОДИН — нижнюю границу длины ролика (у него это
# `pool_min_len` в таблице ВВ2, 20 минут). Остальные выбраны нами, и поэтому все до
# одного вынесены в настройку: число в коде здесь — умолчание первого запуска, а не
# правило. Подтверждение каждого — вопрос владельцу (см. отчёт).
DONOR_DEFAULTS = {
    # Короче — ролик берём, но помечаем: это граница владельца, а не запрет.
    "min_minutes": 20.0,
    # Потолка сверху НЕТ — решение владельца 30.09.2026 («убрать потолок совсем»).
    #
    # История такая. Число 90 выбрали МЫ, а не владелец: он называл только нижнюю
    # границу, 20 минут. Девяносто взяли от донорской полосы ВВ2 (20–60 минут) как базу
    # для предупреждения о цене — рерайт держит длину примерно 1:1, и трёхчасовой донор
    # тянет втрое больший счёт за озвучку, кадры и рендер.
    #
    # На практике двухчасовые доноры владельцу попадаются регулярно, и предупреждение
    # выскакивало почти на каждом. Предупреждение, которое видишь всегда, перестают
    # читать — и вместе с ним перестают замечать те, что про настоящую беду. Поэтому
    # ноль: про длину не предупреждаем вовсе.
    #
    # Ноль здесь означает «без потолка» (см. проверку `max_minutes > 0` ниже) — то же,
    # что у `subs_exits`. Вернуть предупреждение можно числом в настройке, код на месте.
    "max_minutes": 0.0,
    # Двадцать минут речи — это порядка 20 000 знаков. 2 000 — заведомо обрывок дорожки.
    # В `donor_subs` есть жёсткий пол в 200 знаков, но он про «пусто», а этот про «клок».
    "min_chars": 2000,
    # Оценка речи: ~150 слов в минуту, ~6,5 знака на слово. Нужна там, где длительность
    # неизвестна (старая строка кэша без `seconds`), и подписана на экране как оценка.
    "chars_per_minute": 1000.0,
    # Насколько текст может быть короче ожидаемого для своей длительности, прежде чем
    # мы скажем «дорожка неполная». Половина — это уже не оговорка диктора, а пропуск.
    "thin_ratio": 0.5,
    # Раньше решал, отклонять ли по ОЦЕНОЧНОЙ длине (когда секунд не знаем и считаем их
    # из знаков). С 22.09.2026 длиннее потолка — всегда предупреждение, а не отказ, и
    # эту ветку флаг больше не смотрит вовсе — ни для измеренной, ни для оценённой
    # длины. Ни на что другое флаг не влияет: `short_video`/`thin_text` от него не
    # зависят и никогда не зависели. Оставлен в настройках как неактивный на будущее,
    # если понадобится другая ветка, где «оценка» и «измерение» снова разойдутся.
    "estimate_rejects": 0,
    # Общий бюджет времени на пачку ссылок. Предпросмотр — синхронный запрос из
    # браузера, а один поход за субтитрами при мёртвом туннеле обходит ВСЕ выходы по
    # VS_SUBS_TIMEOUT=25 с каждый: на 21 выходе это почти девять минут на одну ссылку.
    # Ноль здесь означает «не ждать»: в сеть за этой пачкой не идём вовсе и отвечаем по
    # кэшу. Раньше ноль означал ровно обратное — проверку БЕЗ потолка по времени, то
    # есть тот, кто вписал 0, чтобы не ждать, получал самое долгое ожидание из
    # возможных. Снять потолок можно большим числом (например, 3600).
    "budget_sec": 20.0,
    # Сколько ссылок в пачке проверяем субтитрами. Остальные — по кэшу и названию.
    "limit": 20,
    # 0 — шаг субтитров выключен совсем: когда выходы выжжены, важнее их не добивать.
    "subs": 1,
    # Сколько выходов субтитров разрешено опросить на ОДНУ ссылку предпросмотра.
    # Выходы — общий дефицитный ресурс: их жжёт ночной шаг «текст», и отказ каждого
    # отправляет выход отдыхать для всей системы. Выпуск ночью ходит по всем выходам
    # (ему важно дойти), а человеку у экрана третий отказ подряд уже всё сказал.
    # 0 — потолка нет, предпросмотр ходит по всем выходам наравне с выпуском.
    "subs_exits": 3,
    # Сколько живёт запомненное НАЗВАНИЕ донора. Длительность ролика и наличие дорожки
    # не меняются никогда, а название владелец канала-донора переписать может — правда,
    # редко. Месяц выбран так, чтобы проверка не ходила в oembed по второму разу в
    # пределах одной работы над каналом, но и не показывала год назад увиденное имя.
    # Раньше этот порог жил в pub_remote отдельно от остальных; сюда его свели, чтобы он
    # жил по общему правилу окружение→база→умолчание — формы правки, как и у всех прочих
    # порогов этого блока, у него нет и не было.
    "title_ttl_sec": float(30 * 24 * 3600),
}

DONOR_ENV = {
    "min_minutes": "VS_DONOR_MIN_MINUTES", "max_minutes": "VS_DONOR_MAX_MINUTES",
    "min_chars": "VS_DONOR_MIN_CHARS", "chars_per_minute": "VS_DONOR_CHARS_PER_MINUTE",
    "thin_ratio": "VS_DONOR_THIN_RATIO", "estimate_rejects": "VS_DONOR_ESTIMATE_REJECTS",
    "budget_sec": "VS_DONOR_CHECK_BUDGET_SEC", "limit": "VS_DONOR_CHECK_LIMIT",
    "subs": "VS_DONOR_CHECK_SUBS", "subs_exits": "VS_DONOR_CHECK_EXITS",
    "title_ttl_sec": "VS_DONOR_TITLE_TTL",
}


def _donor_num(raw, default):
    """Число из настройки; мусор — это «настройки нет», а не ноль.

    Отрицательное отбрасываем всегда — такой настройки не бывает ни у одного из ключей.
    Ноль здесь НЕ отбрасываем: у `subs_exits`/`budget_sec` он осмысленно значит «без
    потолка»/«не ждать», так что единого правила «ноль — мусор» для всех ключей нет.
    Там, где ноль всё же опасен для конкретного ключа (`max_minutes=0` в знаменателе
    цены), беречься от него обязан вызывающий код (`donor_verdict`), а не эта функция.
    """
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError, AttributeError):
        return None
    if value < 0:
        return None
    return int(value) if isinstance(default, int) else value


def donor_limits(c=None) -> dict:
    """Пороги проверки донора: окружение старше базы, база старше умолчания.

    Порядок тот же, что у ключа YouTube (`pub_verify.api_key`) и у остального пульта:
    правка в systemd обязана пересиливать то, что записано в `admin_state` напрямую —
    экрана, который бы правил пороги за оператора, в системе нет (см. комментарий
    выше, у `DONOR_LIMITS_STATE`).

    В ответе есть `source` — просто чтобы было видно, откуда взято каждое число
    (окружение/база/умолчание), для диагностики и показа на экране, а не для формы
    правки — такой формы не существует.
    """
    out, source = dict(DONOR_DEFAULTS), {}
    saved = {}
    if c is not None:
        try:
            row = c.execute("SELECT v FROM admin_state WHERE k=?",
                            (DONOR_LIMITS_STATE,)).fetchone()
            if row and row[0]:
                saved = json.loads(row[0]) or {}
        except Exception:                                          # noqa: BLE001
            saved = {}          # нет таблицы или битый JSON — работаем на умолчаниях
    for key, default in DONOR_DEFAULTS.items():
        value = _donor_num(os.environ.get(DONOR_ENV[key]), default)
        if value is not None:
            out[key], source[key] = value, "окружение"
            continue
        value = _donor_num(saved.get(key), default)
        if value is not None:
            out[key], source[key] = value, "база"
            continue
        source[key] = "умолчание"
    out["source"] = source
    return out


# Причины, по которым проверить НЕ УДАЛОСЬ. Ни одна из них не отклоняет ссылку: это
# рассказ о нашей стороне. Текст пишем так, чтобы из него было понятно, надо ли
# владельцу что-то делать, — ради этого они и разведены.
DONOR_UNKNOWN_WHY = {
    "exits_quota": "выходы субтитров исчерпали квоту YouTube — проверим позже, "
                   "это проходит само",
    "exit_dead": "выход субтитров не отвечает (туннель) — проверить не смогли, "
                 "ждать бесполезно, нужен владелец",
    "no_lib": "на сервере нет youtube-transcript-api — проверить нечем",
    "ours": "сбой на нашей стороне — про ролик мы ничего не узнали",
    "budget": "не успели за отведённое время — ссылка не проверена",
    "over_limit": "в пачке слишком много ссылок, субтитры этой не качали",
    "off": "проверка субтитров выключена настройкой",
    "no_net": "в сети не спрашивали, а в кэше этого ролика нет",
    "not_checked": "субтитры не проверяли",
}


def donor_facts_blank(url: str = "", now: float = 0.0) -> dict:
    """Пустой лист фактов о доноре. Все поля существуют всегда — экрану так проще."""
    return {"url": url, "video_id": "", "title": "", "title_from": "",
            "has_subs": None, "chars": 0, "lang": "", "lang_ru": "",
            "seconds": 0.0, "minutes": 0.0, "length_from": "", "cached": False,
            "paid": False, "checked_at": float(now), "fail": "", "fail_text": ""}


def donor_verdict(facts: dict, limits: dict = None) -> dict:
    """Вердикт по фактам. Чистая функция: ни сети, ни базы, ни часов.

    Ровно поэтому её можно проверить таблицей «вход → вердикт» и поэтому причина отказа
    в пульте всегда совпадает с тем, что проверка на самом деле видела.

    Четыре исхода:
      `ok`      — субтитры есть, длина в границах;
      `reject`  — ставить нельзя: не ссылка, нет дорожки, обрывок;
      `warn`    — ставим, но показываем жёлтым: длиннее потолка (это вопрос цены,
                  а не запрет), короче границы владельца, дорожка выглядит неполной,
                  названия не добыли;
      `unknown` — проверить не удалось. СТАВИМ. Виновата наша сторона, а не ролик.
    """
    lim = dict(DONOR_DEFAULTS)
    lim.update(limits or {})
    facts = facts or {}

    def out(verdict, reason, why, warns=()):
        return {"verdict": verdict, "reason": reason, "why": why,
                "warns": list(warns), "can_plan": verdict != "reject",
                "checked": verdict in ("ok", "warn", "reject")}

    if not facts.get("video_id"):
        return out("reject", "not_youtube", "это не ссылка на ролик YouTube")
    fail = facts.get("fail") or ""
    if fail:
        return out("unknown", fail,
                   facts.get("fail_text") or DONOR_UNKNOWN_WHY.get(fail, "проверить не удалось"))
    if facts.get("has_subs") is None:
        return out("unknown", "not_checked", DONOR_UNKNOWN_WHY["not_checked"])
    if not facts.get("has_subs"):
        return out("reject", "no_subs",
                   "у ролика нет субтитров — текст выпуска брать не из чего")

    chars = int(facts.get("chars") or 0)
    if chars < lim["min_chars"]:
        return out("reject", "short_text",
                   f"расшифровка {chars} знаков при пороге {int(lim['min_chars'])} — "
                   "это обрывок дорожки, а не текст ролика")

    minutes = float(facts.get("minutes") or 0)
    measured = facts.get("length_from") == "субтитры"
    warns = []
    # Длиннее потолка — ВСЕГДА предупреждение, никогда не отказ (22.09.2026): владелец
    # сам решает, платить ли за длинного донора, а не мы за него. `estimate_rejects`
    # здесь больше не смотрим совсем — ни для измеренной, ни для оценённой длины: раньше
    # он превращал это же предупреждение в отказ, теперь у него для этой ветки роли нет.
    # Причина остаётся разной (`too_long` / `long_estimate`) — только чтобы отличать на
    # экране «мы знаем длину точно» от «мы её оценили по числу знаков».
    # `max_minutes <= 0` — тот же смысл «без потолка», что и у `subs_exits` выше (ноль у
    # `budget_sec` означает другое — «не ждать», это другой смысл нуля, не «без потолка»).
    # Владелец мог осознанно выставить `max_minutes=0`. Множитель цены при таком потолке
    # не посчитать честно (деление на ноль или на отрицательное), поэтому предупреждение
    # о цене в этом случае просто не показываем — это не отказ, а его пропуск.
    if minutes and lim["max_minutes"] > 0 and minutes > lim["max_minutes"]:
        mult = round(minutes / lim["max_minutes"], 1)
        mult_txt = ("%.1f" % mult).replace(".", ",")
        est_note = "" if measured else " (длина оценена по числу знаков, точной мы не знаем)"
        why = (f"длиннее обычного (до {int(lim['max_minutes'])} мин): "
               f"{int(round(minutes))} мин{est_note} — озвучка, кадры и рендер выйдут "
               f"примерно в {mult_txt}× дороже, чем у {int(lim['max_minutes'])}-минутного")
        warns.append(("too_long" if measured else "long_estimate", why))

    if measured and minutes and minutes < lim["min_minutes"]:
        warns.append(("short_video",
                      f"ролик идёт {int(round(minutes))} мин — короче границы "
                      f"{int(lim['min_minutes'])} мин"))
    if measured and minutes:
        expect = minutes * lim["chars_per_minute"]
        if expect and chars < expect * lim["thin_ratio"]:
            warns.append(("thin_text",
                          f"на {int(round(minutes))} мин всего {chars} знаков — "
                          "похоже, дорожка неполная"))
    if not facts.get("title"):
        warns.append(("no_title", "название ролика не получено — возможно, он закрыт"))

    if warns:
        return out("warn", warns[0][0], "; ".join(w[1] for w in warns),
                   [{"reason": r, "why": w} for r, w in warns])
    return out("ok", "ok", f"субтитры есть, {chars} знаков")


def _youtube_link(url: str) -> bool:
    """Ссылка ведёт на YouTube — то же требование, что у `donor_title`.

    Разбор идентификатора (`pub_ids.video_id`) хост не проверяет: у него есть запасное
    правило «последние 11 символов пути», и по нему `https://vimeo.com/12345678901` или
    `https://rutube.ru/video/abcdefghijk` выглядят ссылкой на ролик YouTube. Без этой
    проверки такая ссылка уходила в дефицитные выходы субтитров — настоящим запросом к
    YouTube через туннель, — а владелец получал на экране не «это не ссылка на ролик»,
    а «у ролика нет субтитров», то есть обвинение донора в том, чего он не делал.

    Голый идентификатор без ссылки (`dQw4w9WgXcQ`) остаётся допустимым: его и раньше
    принимали, и ни на какой чужой хост он не показывает.
    """
    text = (url or "").strip()
    if any(h in text for h in YT_HOSTS):
        return True
    return "/" not in text and "." not in text and ":" not in text


def check_donor(url: str, *, c=None, now: float = None, net: bool = True,
                limits: dict = None, deadline: float = None,
                subs=None, title_fetch=None, want_subs: bool = True) -> dict:
    """Проверить донорскую ссылку ДО постановки в план. Бесплатно и мягко.

    Возвращает один плоский словарь: факты о ролике плюс вердикт (`donor_verdict`).
    Поля перечислены в `donor_facts_blank` и в отчёте — пульт вешает эту функцию на
    предпросмотр ссылок.

    Порядок шагов — от бесплатного к дорогому, и это и есть суть: тащить субтитры
    трёхчасового ролика через дефицитный выход, чтобы потом отклонить его по длине,
    хуже, чем не проверять вовсе.

      1. разбор ссылки — без сети, и хост обязан быть youtube (см. `_youtube_link`:
         одиннадцать символов в конце пути бывают и у чужих сайтов);
      2. кэш `subs_cache` по video_id — без сети (второго кэша не заводим: один ролик
         идёт на несколько языковых каналов, и два ответа про него разойдутся);
      3. название — открытый oembed, без ключей и квот;
      4. субтитры — `pub_remote.donor_subs`, и скачанное остаётся в том же кэше:
         проверка не выбрасывается, а становится первым шагом будущего выпуска;
      5. длительность — из последней реплики дорожки, тоже бесплатно.

    `deadline` — момент, после которого в сеть не ходим (бюджет пачки). `net=False` —
    отвечать только по кэшу. `want_subs=False` — не качать субтитры этой ссылке (пачка
    длиннее `limit`). `subs` и `title_fetch` — подставные транспорты для тестов, как
    `fetch` у `pub_verify.check`: сети в тестах нет.

    Вызывать ПОСЛЕ отсева дублей: проверять донора, который никуда не встанет, значит
    зря дёргать выходы субтитров.
    """
    import pub_remote                # лениво: pub_tags читают и там, где requests лишний

    now = time.time() if now is None else float(now)
    lim = limits if limits is not None else donor_limits(c)
    # Срок и потолок выходов передаём ВНУТРЬ похода за субтитрами: бюджет, который
    # спрашивают только перед началом, на мёртвом туннеле не значит ничего — один
    # донор успевает обойти все выходы по таймауту каждый (см. `pub_remote.donor_subs`).
    subs = subs or (lambda link, net=True: pub_remote.donor_subs(
        link, net=net, strict=False, deadline=deadline,
        max_exits=int(lim.get("subs_exits") or 0) or None))
    title_fetch = title_fetch or donor_title

    facts = donor_facts_blank(url, now)
    facts["video_id"] = vid = video_id(url) if _youtube_link(url) else ""
    if not vid:
        facts.update(donor_verdict(facts, lim))
        return facts

    # Ноль в поле бюджета — это «не ждать»: ни oembed, ни субтитры в сеть не идут, ответ
    # собирается по кэшу. Без этого ноль означал бы ОТСУТСТВИЕ срока, то есть проверку
    # вообще без потолка — ровно противоположное тому, что имеет в виду человек,
    # вписывающий ноль в поле «сколько ждать».
    #
    # Правило работает, только когда срок не задан вызывающим. Тот, кто передал
    # `deadline` явно, уже решил, сколько готов ждать (пульт живёт по своему потолку
    # ожидания, `web/app.py:donor_net_deadline`), и отменять его решение мы не вправе.
    no_wait = deadline is None and not float(lim.get("budget_sec") or 0)

    def time_left() -> bool:
        # Бюджет считается по НАСТОЯЩИМ часам, а не по `now`: `now` задаётся вызывающим
        # (и в тестах вообще фиксирован), а ждать нас заставляет именно реальная сеть.
        if no_wait:
            return False
        return deadline is None or time.time() < deadline

    # ── название: сначала то, что уже знаем, и только потом сеть ──
    title, title_at = "", 0.0
    try:
        title, title_at = pub_remote.donor_cache_title(vid)
    except Exception:                                              # noqa: BLE001
        title, title_at = "", 0.0
    fresh = title and (now - title_at) < float(lim.get("title_ttl_sec") or 0)
    if fresh:
        facts["title"], facts["title_from"] = title, "кэш"
    elif net and time_left():
        fetched = ""
        try:
            fetched = title_fetch(url)
        except Exception:                                          # noqa: BLE001
            fetched = ""          # название — украшение, из-за него проверка не падает
        if fetched:
            facts["title"], facts["title_from"] = fetched, "сеть"
            try:
                pub_remote.donor_cache_put_title(vid, fetched, now)
            except Exception:                                      # noqa: BLE001
                pass
        elif title:
            # Свежего не добыли, но старое знаем. Показать вчерашнее название лучше,
            # чем голую ссылку, — но пометить, что оно вчерашнее, обязательно.
            facts["title"], facts["title_from"] = title, "кэш (устарело)"
    elif title:
        facts["title"], facts["title_from"] = title, "кэш (устарело)"

    # ── субтитры: кэш, потом, если разрешено и есть время, сеть ──
    got = {}
    try:
        got = subs(url, net=False) or {}
    except Exception:                                              # noqa: BLE001
        got = {}                  # кэш недоступен — это ускорение, а не условие работы
    if not got:
        if not want_subs:
            facts["fail"] = "over_limit"
            facts["fail_text"] = (DONOR_UNKNOWN_WHY["over_limit"]
                                  + f" (предел {int(lim['limit'])})")
        elif not lim["subs"]:
            facts["fail"] = "off"
        elif not net:
            facts["fail"] = "no_net"
        elif not time_left():
            facts["fail"] = "budget"
        else:
            try:
                got = subs(url, net=True) or {}
            except Exception as exc:                               # noqa: BLE001
                facts.update(_donor_subs_failure(exc, pub_remote))
    if got and not (got.get("text") or got.get("chars")):
        # Ответ есть, а текста в нём нет. «Нет субтитров» так НЕ выглядит — так
        # выглядит наша поломка, и объявлять по ней донор негодным нельзя: настоящее
        # отсутствие дорожки приходит исключением из закрытого списка `_no_subtitles`.
        facts["fail"], got = "ours", {}
        facts["fail_text"] = DONOR_UNKNOWN_WHY["ours"] + ": ответ без текста расшифровки"
    if got:
        text = got.get("text") or ""
        facts["chars"] = int(got.get("chars") or len(text))
        facts["has_subs"] = facts["chars"] > 0
        facts["lang"] = got.get("lang") or ""
        # Язык показываем только известный: `pub_remote.lang_ru` неизвестный код
        # называет русским (там это осознанное умолчание движка), а проверке ссылки
        # врать нельзя — по её ответу человек решает, тот ли это ролик.
        facts["lang_ru"] = (pub_remote.LANG_TO_RU.get(facts["lang"].lower(), "")
                            if facts["lang"] else "")
        facts["cached"] = bool(got.get("cached"))
        seconds = float(got.get("seconds") or 0)
        if seconds > 0:
            facts["seconds"], facts["minutes"] = seconds, seconds / 60.0
            facts["length_from"] = "субтитры"
        elif facts["chars"] and lim["chars_per_minute"]:
            # Старые строки кэша (до 19.09) длительности не помнят. Это честное «не
            # знаем», а не ноль минут: считаем оценку из знаков и так её и называем.
            facts["minutes"] = facts["chars"] / float(lim["chars_per_minute"])
            facts["seconds"] = facts["minutes"] * 60.0
            facts["length_from"] = "оценка по знакам"
    facts["checked_at"] = time.time()
    facts.update(donor_verdict(facts, lim))
    return facts


def _donor_subs_failure(exc, pub_remote) -> dict:
    """Отказ похода за субтитрами → причина в ответе проверки.

    Единственное место, где «донор плохой» вообще может появиться, — закрытый список
    `pub_remote._no_subtitles`, и приезжает он сюда машинным признаком `kind`, а не
    разбором русского текста сообщения. Всё остальное — наша сторона, и она обязана
    отвечать «не смогли проверить»: отклонить исправную ссылку из-за упавшего туннеля
    дороже, чем поставить сомнительную и увидеть это на шаге текста.
    """
    kind = getattr(exc, "kind", "") or ""
    if kind == pub_remote.FAIL_NO_SUBS:
        return {"has_subs": False, "fail": "", "fail_text": ""}
    # Два приговора РОЛИКУ, до которых проверка сегодня не доходит (она зовёт
    # `donor_subs(strict=False)`, а обе причины приходят только из строгого режима).
    # Размечены заранее и не своими словами, а фактами, по которым `donor_verdict` и
    # так умеет судить: иначе первая же правка `strict` превратила бы честное «обрывок
    # дорожки» в «сбой на нашей стороне» — и обрывок молча уехал бы в план.
    if kind == pub_remote.FAIL_THIN:
        # Дорожка есть, но это клок. Числа знаков строгий режим не отдаёт, поэтому ноль:
        # вердикт выйдет тот же самый (reject/short_text), а придумывать длину нельзя.
        return {"has_subs": True, "chars": 0, "fail": "", "fail_text": ""}
    if kind == pub_remote.FAIL_NOT_YT:
        return {"video_id": "", "fail": "", "fail_text": ""}
    known = {pub_remote.DOWN_QUOTA: "exits_quota", pub_remote.DOWN_TUNNEL: "exit_dead",
             pub_remote.DOWN_NO_LIB: "no_lib", pub_remote.DOWN_OURS: "ours",
             pub_remote.DOWN_BUDGET: "budget"}
    reason = known.get(kind, "ours")
    # Через `_hide_creds`, а не напрямую: текст ошибки уходит на экран пульта и в лог, а
    # прокси профилей AdsPower задаются с парой логин:пароль прямо в адресе выхода.
    text = pub_remote._hide_creds(str(exc))[:200]
    return {"fail": reason,
            "fail_text": f"{DONOR_UNKNOWN_WHY[reason]}: {text}" if text
                         else DONOR_UNKNOWN_WHY[reason]}


def check_donors(urls: list, *, c=None, now: float = None, net: bool = True,
                 limits: dict = None, subs=None, title_fetch=None) -> dict:
    """Проверить пачку ссылок под общим бюджетом времени.

    Предпросмотр — синхронный запрос из браузера, а один поход за субтитрами при
    мёртвом туннеле обходит все выходы по таймауту каждый. Поэтому бюджет общий и
    жёсткий: кончился — остальные ссылки честно возвращаются как «не успели», а не
    держат человека перед пустым экраном несколько минут.

    «Жёсткий» здесь значит именно это: срок передаётся ВНУТРЬ похода за субтитрами и
    обрывает перебор выходов (`pub_remote.donor_subs(deadline=…)`). Пока он только
    спрашивался перед началом, потолок был обещанием, а не правилом: одна ссылка при
    мёртвом туннеле съедала все выходы по 25 секунд каждый при бюджете в двадцать
    секунд на всю пачку. Оставшееся время не растягивается на ссылку: потолок общий.

    Возвращает {"items": [...], "limits": {...}, "summary": {...}, "spent_sec": N}.
    Порядок items совпадает с порядком urls: пульт показывает их строка к строке.
    """
    now = time.time() if now is None else float(now)
    lim = limits if limits is not None else donor_limits(c)
    started = time.time()
    deadline = started + float(lim["budget_sec"]) if lim["budget_sec"] else None
    items, summary = [], {"ok": 0, "warn": 0, "reject": 0, "unknown": 0}
    for number, url in enumerate(urls or ()):
        item = check_donor(url, c=c, now=now, net=net, limits=lim, deadline=deadline,
                           subs=subs, title_fetch=title_fetch,
                           want_subs=number < int(lim["limit"]))
        items.append(item)
        summary[item["verdict"]] = summary.get(item["verdict"], 0) + 1
    return {"items": items, "limits": lim, "summary": summary,
            "spent_sec": round(time.time() - started, 2)}



# ─────────────────────────── теги ───────────────────────────

def tag_names(c, channel_id: int) -> list:
    rows = c.execute(
        "SELECT t.name FROM pub_tags t JOIN pub_channel_tags ct ON ct.tag_id=t.id "
        "WHERE ct.channel_id=? ORDER BY t.name COLLATE NOCASE", (channel_id,)).fetchall()
    return [r[0] for r in rows]


def all_tags(c, tenant_id: str) -> list:
    """Все теги пользователя со счётчиком каналов — для полосы фильтров."""
    rows = c.execute(
        "SELECT t.name, COUNT(ct.channel_id) n FROM pub_tags t "
        "LEFT JOIN pub_channel_tags ct ON ct.tag_id=t.id "
        "LEFT JOIN pub_channels ch ON ch.id=ct.channel_id AND ch.status!='archived' "
        "WHERE t.tenant_id=? GROUP BY t.id HAVING n>0 ORDER BY n DESC, t.name COLLATE NOCASE",
        (tenant_id,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def clean_tag(raw) -> str:
    """Имя тега так, как оно лежит в базе: без лишних пробелов, строчными, до 40 знаков.

    Вынесено из `set_tags` (30.09.2026, массовая простановка тега): вторая копия этого
    правила рано или поздно разошлась бы с первой, и « WW2 » завёл бы второй тег рядом с
    «ww2» — отбор по тегу после этого находил бы половину каналов вместо всех.
    """
    return " ".join(str(raw or "").split()).strip().lower()[:40]


def add_tag(c, tenant_id: str, channel_id: int, name: str, now: float) -> bool:
    """Дописать каналу один тег. `False` — тег на нём уже стоял, менять было нечего.

    Не через `set_tags`: та переписывает НАБОР целиком, и массовое «проставить тег»
    через неё стёрло бы всё, что на канале уже висит.
    """
    tag = clean_tag(name)
    if not tag:
        raise ValueError("тег не задан")
    row = c.execute("SELECT id FROM pub_tags WHERE tenant_id=? AND name=?",
                    (tenant_id, tag)).fetchone()
    tag_id = row[0] if row else c.execute(
        "INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)",
        (tenant_id, tag, now)).lastrowid
    cur = c.execute("INSERT OR IGNORE INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)",
                    (channel_id, tag_id))
    return cur.rowcount > 0


def drop_tag(c, tenant_id: str, channel_id: int, name: str) -> bool:
    """Снять с канала один тег. `False` — тега на канале и не было.

    Сам тег из справочника не удаляется: его носят другие каналы, а `all_tags` и так
    не показывает теги, за которыми не осталось ни одного канала.
    """
    tag = clean_tag(name)
    if not tag:
        raise ValueError("тег не задан")
    cur = c.execute(
        "DELETE FROM pub_channel_tags WHERE channel_id=? AND tag_id IN "
        "(SELECT id FROM pub_tags WHERE tenant_id=? AND name=?)",
        (channel_id, tenant_id, tag))
    return cur.rowcount > 0


def set_tags(c, tenant_id: str, channel_id: int, names: list, now: float) -> None:
    """Переписать теги канала. Неизвестные заводятся на лету."""
    clean = []
    for raw in names:
        name = clean_tag(raw)
        if name and name not in clean:
            clean.append(name)
    c.execute("DELETE FROM pub_channel_tags WHERE channel_id=?", (channel_id,))
    for name in clean:
        row = c.execute("SELECT id FROM pub_tags WHERE tenant_id=? AND name=?",
                        (tenant_id, name)).fetchone()
        if row:
            tag_id = row[0]
        else:
            tag_id = c.execute("INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)",
                               (tenant_id, name, now)).lastrowid
        c.execute("INSERT OR IGNORE INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)",
                  (channel_id, tag_id))


def parse_tags(raw: str) -> list:
    """«ww2, сон, тест» → ['ww2','сон','тест']."""
    return [x for x in re.split(r"[,\n;]+", raw or "") if x.strip()]


# ─────────────────────── очередь ссылок ───────────────────────

# Статусы, при которых строка плана ЖИВАЯ: ролик считается взятым, а его выход —
# занятым. Снятое (`cancelled`) и упавшее (`error`) сюда не входят: снятую ссылку
# человек убрал сам, и не пустить её обратно значило бы запереть канал на ролике,
# от которого он отказался.
#
# Константа, а не литерал в каждом запросе: этот же набор спрашивает раскладка пачки
# (`link_spread.known_donors`), чтобы пометить дубль ДО постановки. Разойдись они —
# экран сказал бы «дубль» там, где `add_links` ссылку поставит, или промолчал бы там,
# где она будет пропущена.
LIVE_STATUSES = ("planned", "frozen", "running", "done")
_LIVE_SQL = ",".join(f"'{s}'" for s in LIVE_STATUSES)

# Статусы, при которых ссылку считаем УЖЕ ВЗЯТОЙ этим каналом. Шире живых ровно на один
# `error`, и это не придирка (разбор 01.10.2026, Д28).
#
# Сорвавшаяся строка плана занимает выход канала (`taken_slots` её учитывает), а дубль
# её не видел — та же донорская ссылка вставлялась второй раз, заводила вторую строку
# плана и второй выпуск. Если первая заливка успела отдать файл, на канале появлялся
# ВТОРОЙ ролик, причём каждая его стадия оплачена заново. Повторять сорвавшийся выпуск
# надо не новой ссылкой, а возвратом в работу — он сохраняет уже оплаченную работу и
# проверяет, что файл не уходил.
#
# `cancelled` сюда не входит и входить не должен: снятую ссылку человек убрал сам, и не
# пустить её обратно значило бы запереть канал на ролике, от которого он отказался.
DUP_STATUSES = LIVE_STATUSES + ("error",)
_DUP_SQL = ",".join(f"'{s}'" for s in DUP_STATUSES)


def parse_links(raw: str) -> list:
    """Текст из поля → список ссылок. По одной в строке либо просто вперемешку."""
    out, seen = [], set()
    for line in (raw or "").splitlines():
        for url in URL_RE.findall(line) or ([line.strip()] if line.strip() else []):
            url = url.strip().rstrip(",;")
            if url.startswith("http") and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def taken_slots(c, channel_id: int) -> list:
    """Уже занятые моменты этого канала — чтобы не поставить два ролика в один слот."""
    rows = c.execute(
        "SELECT slot_at FROM pub_plan_items WHERE channel_id=? AND slot_at IS NOT NULL "
        "AND status IN ('planned','frozen','running','done','error')", (channel_id,)).fetchall()
    return [dt.datetime.fromtimestamp(r[0], dt.timezone.utc) for r in rows if r[0]]


def add_links(c, tenant_id: str, channel, urls: list, now: float, slot=None,
              titles: dict = None) -> dict:
    """Разложить ссылки по свободным выходам канала.

    `slot` задан — ставим ИМЕННО в этот выход (так работает поле в пустой строке
    таблицы: куда вставил, туда и встало). Без него — по ближайшим свободным подряд.

    Ссылка, которой не хватило места в горизонте, всё равно сохраняется, просто без
    даты. Она займёт выход, как только он освободится: терять вставленное нельзя.
    """
    schedule = json.loads(channel["schedule"] or "{}")
    placed, queued, skipped = [], 0, []
    busy = taken_slots(c, channel["id"])
    horizon = int(channel["horizon_days"] or 3)
    # «Сейчас» берём из аргумента, а не из системных часов: иначе одна и та же вставка
    # даёт разный результат в зависимости от времени суток, и поведение нельзя проверить.
    moment = dt.datetime.fromtimestamp(float(now), dt.timezone.utc)
    # Названия доноров тянем ДО первой записи: сетевые вызовы под открытым write-замком
    # держали бы всю базу кабинета до шести секунд на ссылку.
    if titles is None:
        titles = {u: donor_title(u) for u in urls}

    for url in urls:
        # Один и тот же ролик приходит в разных написаниях: `youtu.be/X`, `watch?v=X`,
        # `watch?v=X&t=30`, ссылка с `si=…` из кнопки «Поделиться». Проверка по строке
        # ссылки все их считала разными роликами, и канал получал дубль, оплаченный
        # дважды — субтитры, рерайт, озвучка, картинки, рендер. Сравниваем по
        # идентификатору ролика, а строку ссылки оставляем запасным признаком: у не-
        # YouTube ссылок идентификатора нет.
        vid = video_id(url)
        dup = c.execute(
            "SELECT id FROM pub_plan_items WHERE channel_id=? "
            "AND (donor_url=? OR (?!='' AND donor_video_id=?)) "
            f"AND status IN ({_DUP_SQL})",
            (channel["id"], url, vid, vid)).fetchone()
        if dup:
            skipped.append(url)
            continue
        if slot is not None and not placed:
            stamp = float(slot)                     # точный выход, выбранный владельцем
            mode = "exact"
            placed.append(dt.datetime.fromtimestamp(stamp, dt.timezone.utc))
        else:
            point = (next_free_slot(schedule, channel["tz"], busy, horizon, moment)
                     if schedule else None)
            mode = "auto"
            if point is not None:
                busy.append(point)
                stamp = point.timestamp()
                placed.append(point)
            else:
                stamp = None
                queued += 1
        try:
            c.execute(
                "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,donor_video_id,"
                "donor_title,slot_at,slot_mode,position,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, channel["id"], url, vid, (titles or {}).get(url, ""), stamp,
                 mode, now, now, now))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                skipped.append(url)      # двойной сабмит формы: выход уже занят — не роняем пачку
                continue
            raise
    return {"placed": placed, "queued": queued, "skipped": skipped}


def plan_rows(c, channel_id: int) -> list:
    return c.execute(
        "SELECT * FROM pub_plan_items WHERE channel_id=? AND status!='cancelled' "
        "ORDER BY CASE WHEN slot_at IS NULL THEN 1 ELSE 0 END, slot_at, id",
        (channel_id,)).fetchall()


def coverage(c, channel, now=None) -> list:
    """Слоты канала на горизонт с привязанной ссылкой — строка для «Календаря».

    Возвращает [(момент, строка плана или None)] по возрастанию: пустая позиция и есть
    то место, куда нужно вставить ссылку.
    """
    schedule = json.loads(channel["schedule"] or "{}")
    if not schedule:
        return []
    slots = upcoming_slots(schedule, channel["tz"], int(channel["horizon_days"] or 3), now)
    rows = {}
    for r in plan_rows(c, channel["id"]):
        if r["slot_at"] and round(r["slot_at"]) not in rows:
            rows[round(r["slot_at"])] = r      # первая (живая) строка слота не затирается
    return [(s, rows.get(round(s.timestamp()))) for s in slots]


def releases_by_item(c, channel_id: int) -> dict:
    """Выпуски канала, разложенные по строкам плана: {plan_item_id: строка выпуска}."""
    rows = c.execute(
        "SELECT * FROM pub_releases WHERE channel_id=? ORDER BY id", (channel_id,)).fetchall()
    return {r["plan_item_id"]: r for r in rows}


def plan_by_channel(c, tenant_id: str, since: float, until: float) -> dict:
    """Строки плана всех каналов в окне — ОДНИМ запросом.

    Отдельный запрос на канал (70 каналов = 70 запросов) на пятистах превратит
    список в мёртвый экран, поэтому берём всё разом и раскладываем в память.
    """
    rows = c.execute(
        "SELECT channel_id, slot_at, status FROM pub_plan_items "
        "WHERE tenant_id=? AND status!='cancelled' AND slot_at IS NOT NULL "
        "AND slot_at BETWEEN ? AND ?", (tenant_id, since, until)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["channel_id"], set()).add(round(r["slot_at"]))
    return out


def troubles_by_channel(c, tenant_id: str) -> dict:
    """Каналы, где есть застрявший выпуск: {channel_id: причина}. Тоже одним запросом."""
    rows = c.execute(
        "SELECT r.channel_id, r.blocked_reason, MAX(r.updated) FROM pub_releases r "
        "JOIN pub_plan_items p ON p.id=r.plan_item_id AND p.status!='cancelled' "
        "WHERE r.tenant_id=? AND r.status='blocked' GROUP BY r.channel_id",
        (tenant_id,)).fetchall()
    return {r["channel_id"]: (r["blocked_reason"] or "нужен разбор") for r in rows}


def work_by_channel(c, tenant_id: str) -> dict:
    """Что реально происходит на каждом канале — одним запросом на весь список.

    Плитка должна отвечать на вопрос «как идут дела», а не «сколько мест в расписании
    занято»: владелец смотрит на список, чтобы увидеть работу, а заполненность выходов
    об этом не говорит ничего — она одинаковая и когда ролики делаются, и когда стоят.
    """
    rows = c.execute(
        "SELECT p.channel_id cid, "
        " COUNT(*) queued, "
        " SUM(CASE WHEN r.status IN ('pending','running') THEN 1 ELSE 0 END) working, "
        " SUM(CASE WHEN r.status='done' THEN 1 ELSE 0 END) ready, "
        " SUM(CASE WHEN r.status='blocked' THEN 1 ELSE 0 END) stuck "
        "FROM pub_plan_items p LEFT JOIN pub_releases r ON r.plan_item_id=p.id "
        "WHERE p.tenant_id=? AND p.status!='cancelled' GROUP BY p.channel_id",
        (tenant_id,)).fetchall()
    return {r["cid"]: {"queued": r["queued"] or 0, "working": r["working"] or 0,
                       "ready": r["ready"] or 0, "stuck": r["stuck"] or 0} for r in rows}


def channel_state(row, taken: set, trouble: str, now=None, work=None) -> dict:
    """Что происходит на канале — человеческой строкой, а не счётом слотов."""
    import json as _json
    from channel_slots import upcoming_slots
    try:
        schedule = _json.loads(row["schedule"] or "{}")
    except Exception:
        schedule = {}
    w = work or {}
    queued = int(w.get("queued") or 0)
    working = int(w.get("working") or 0)
    ready = int(w.get("ready") or 0)
    stuck = int(w.get("stuck") or 0)
    waiting = max(0, queued - working - ready - stuck)

    # Полоса заполнения показывает продвижение работы: сделано из взятого в план.
    filled, total = ready, queued

    if not schedule:
        return {"kind": "none", "text": "расписание не задано", "filled": 0, "total": 0}
    if not queued:
        slots = upcoming_slots(schedule, row["tz"], int(row["horizon_days"] or 3), now)
        return {"kind": "warn" if slots else "none",
                "text": "ссылок нет — вставьте доноров", "filled": 0, "total": 0}

    части = []
    if ready:
        части.append(f"{ready} готово")
    if working:
        части.append(f"{working} делается")
    if waiting:
        части.append(f"{waiting} ждёт")
    if stuck:
        части.append(f"{stuck} в разборе")
    text = " · ".join(части) or f"{queued} в очереди"

    if stuck:
        kind = "err"
    elif working:
        kind = "warn"
    elif ready >= queued:
        kind = "ok"
    else:
        kind = "warn"
    return {"kind": kind, "text": text, "filled": filled, "total": total}


def _links_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ссылки"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "ссылок"
    return "ссылок"


def move_item(c, item_id: int, target_slot: float, now: float) -> None:
    """Перетащить ссылку в другой выход. Если там уже стоит ссылка — меняются местами."""
    row = c.execute("SELECT * FROM pub_plan_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError("строка не найдена")
    if row["frozen"]:
        raise ValueError("ролик уже взят в работу — переставить нельзя")
    other = c.execute(
        "SELECT * FROM pub_plan_items WHERE channel_id=? AND slot_at=? AND id!=? "
        f"AND status IN ({_LIVE_SQL})",
        (row["channel_id"], target_slot, item_id)).fetchone()
    if other and other["frozen"]:
        raise ValueError("в том выходе ролик уже готовится — менять местами нельзя")
    # Освобождаем целевой слот через промежуточное состояние: иначе уникальный
    # индекс не даст двум строкам одновременно указывать на одно время.
    if other:
        moved = c.execute("UPDATE pub_plan_items SET slot_at=NULL, updated=? "
                          "WHERE id=? AND frozen=0", (now, other["id"]))
        if moved.rowcount != 1:
            raise ValueError("тот ролик уже взят в работу — менять местами поздно")
    moved = c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? "
                      "WHERE id=? AND frozen=0", (target_slot, now, item_id))
    if moved.rowcount != 1:
        # Пока владелец тянул строку, движок успел её заморозить: возвращаем всё как было.
        if other:
            c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? WHERE id=?",
                      (target_slot, now, other["id"]))
        raise ValueError("ролик уже взят в работу — переставить нельзя")
    if other:
        c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? WHERE id=?",
                  (row["slot_at"], now, other["id"]))


def cancel_item(c, item_id: int, now: float) -> str:
    """Убрать ролик из очереди — вместе с уже начатой работой над ним.

    Снять строку и оставить выпуск работать было бы худшим из вариантов: из таблицы
    ролик пропал, а система продолжает его делать и потом зальёт.
    """
    from pub_engine import reached_irreversible
    row = c.execute("SELECT * FROM pub_plan_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError("строка не найдена")
    rel = c.execute("SELECT * FROM pub_releases WHERE plan_item_id=? ORDER BY id DESC LIMIT 1",
                    (item_id,)).fetchone()
    if rel and reached_irreversible(c, rel["release_id"]):
        raise ValueError("ролик уже заливается на канал — из очереди его убирать поздно")
    if rel:
        job = c.execute("SELECT status FROM pub_upload_jobs WHERE release_id=?",
                        (rel["release_id"],)).fetchone()
        if job and job["status"] == "running":
            raise ValueError("машина уже взяла ролик в заливку — убирать поздно")
        # Ждущее задание гасим ОБЯЗАТЕЛЬНО: иначе нода позже заберёт его и зальёт
        # ролик, которого в таблице уже нет.
        c.execute("UPDATE pub_upload_jobs SET status='cancelled', error=?, updated=? "
                  "WHERE release_id=? AND status='queued'",
                  ("убран владельцем из очереди", now, rel["release_id"]))
    if rel and rel["status"] in ("pending", "running", "blocked"):
        c.execute("UPDATE pub_releases SET status='cancelled', updated=? WHERE release_id=?",
                  (now, rel["release_id"]))
    c.execute("UPDATE pub_plan_items SET status='cancelled', updated=? WHERE id=?", (now, item_id))
    return rel["release_id"] if rel else ""


def release_stages(c, channel_id: int) -> dict:
    """Состояние этапов по каждому выпуску канала — ОДНИМ запросом.

    Берём статус прямо из очереди кабинета, а не из своих отметок: там правда о том,
    идёт этап, стоит в очереди или упал.
    """
    rows = c.execute(
        "SELECT j.release_id, j.kind, q.status FROM pub_release_jobs j "
        "LEFT JOIN queue_tasks q ON CAST(q.job_id AS TEXT)=j.remote_job_id "
        "WHERE j.service='vsgen' AND j.release_id IN "
        "(SELECT release_id FROM pub_releases WHERE channel_id=?)", (channel_id,)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["release_id"], {})[r["kind"]] = r["status"] or "queued"
    return out


def upload_jobs_by_release(c, channel_id: int) -> dict:
    rows = c.execute(
        "SELECT * FROM pub_upload_jobs WHERE channel_id=?", (channel_id,)).fetchall()
    return {r["release_id"]: r for r in rows}
