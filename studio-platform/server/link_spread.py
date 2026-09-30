#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Раскладка пачки ссылок по каналам (владелец 30.09.2026).

«Сделать чекбокс, что завести много на много каналов — и тогда открывается попап, где
делаешь контрол-в, и он создаёт кучу карточек со вставленными ссылками и названиями
видео в скобочках в заголовке карточки; и тогда просто справа в дропдауне по названию
канал выбираешь».

Раньше ссылки ставились по одному каналу: зайти в карточку, вставить, нажать. На ста
пятидесяти каналах и общем списке ссылок это полтораста заходов.

Что этот модуль делает и чего НЕ делает. Он только раскладывает: разбирает пачку,
рассказывает про каждую ссылку (название, дубль, подсказка) и ставит то, чему человек
назначил канал. Разбор ссылок берётся у `pub_tags.parse_links`, «тот же ролик» — у
`pub_ids.video_id`, постановка в план — у `pub_tags.add_links`. Своей второй правды ни
об одном из трёх вопросов здесь нет: разойдясь, они однажды поставят один ролик дважды
или не увидят дубль вовсе.

Два закона этого экрана, оба дороже удобства:

1. **Подсказка канала — это подсказка, а не выбор.** Ролик, ушедший на чужой канал, —
   худшая ошибка системы и необратимая: он уже у YouTube. Поэтому `suggest()` НИКОГДА
   не возвращает «наугад похожий» канал: нет уверенности — пустой выбор и сказанная
   словами причина. И поэтому же в ответе подсказка лежит отдельным полем `hint`, а
   поля «выбранный канал» в раскладке нет вовсе: чтобы поставить, канал обязан приехать
   с экрана, то есть от человека.
2. **Дубль помечается, но не выбрасывается.** Один донор иногда нарочно идёт на два
   языка. Молчаливый отсев отнял бы у человека решение, которое принимает он.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from pub_ids import video_id
from pub_tags import LIVE_STATUSES, add_links

# Сколько ссылок принимаем за раз. Число наше, не владельца: оно про то, сколько
# названий разумно тянуть из oembed на один показ экрана, а не про устройство работы.
SPREAD_LIMIT = 200

# Название не добыли. Отдельная строка, а не пустота: «названия нет» и «названия не
# спрашивали» — разные вещи, и человек по заголовку карточки решает, тот ли это ролик.
TITLE_UNKNOWN = "название не добыли"

_LIVE_SQL = ",".join(f"'{s}'" for s in LIVE_STATUSES)


# ───────────────────────── дубли ─────────────────────────

def known_donors(c, tenant_id: str, urls: list) -> dict:
    """Ссылка → где этот ролик уже стоит: `{channel_id, channel, status, slot_at}`.

    Сравнение по идентификатору ролика, а не по строке ссылки: один и тот же ролик
    приходит в разных написаниях (`youtu.be/X`, `watch?v=X&t=30`, `/shorts/X`), и
    сравнение строк объявило бы их разными. Строка ссылки остаётся запасным признаком —
    у не-YouTube доноров идентификатора нет.

    Статусы — ровно те же `pub_tags.LIVE_STATUSES`, по которым дубль ловит сама
    постановка (`add_links`). Разойдясь, экран показывал бы дубль там, где постановка
    его не видит, — или, хуже, молчал бы там, где она пропустит ссылку.
    """
    out = {}
    urls = [u for u in (urls or []) if u]
    if not urls:
        return out
    vids = {}
    for u in urls:
        vid = video_id(u)
        if vid:
            vids.setdefault(vid, []).append(u)
    url_marks = ",".join("?" for _ in urls)
    vid_marks = ",".join("?" for _ in vids) or "NULL"
    rows = c.execute(
        "SELECT p.donor_url AS url, p.donor_video_id AS vid, p.status AS status, "
        "p.slot_at AS slot_at, ch.id AS channel_id, ch.name AS channel_name "
        "FROM pub_plan_items p JOIN pub_channels_live ch ON ch.id=p.channel_id "
        f"WHERE p.tenant_id=? AND p.status IN ({_LIVE_SQL}) "
        f"AND (p.donor_url IN ({url_marks}) OR (p.donor_video_id IS NOT NULL "
        f"AND p.donor_video_id!='' AND p.donor_video_id IN ({vid_marks}))) "
        "ORDER BY p.id",
        (tenant_id, *urls, *vids)).fetchall()
    for r in rows:
        hit = {"channel_id": r["channel_id"], "channel": r["channel_name"],
               "status": r["status"], "slot_at": r["slot_at"]}
        touched = list(vids.get(r["vid"] or "", ()))
        if r["url"] in urls:
            touched.append(r["url"])
        for u in touched:
            out.setdefault(u, hit)      # первая (самая ранняя) строка плана и есть ответ
    return out


# ───────────────────────── подсказка канала ─────────────────────────

# Буквы с диакритикой по языкам — полными наборами, а не «характерными». Полными
# нарочно: исключительность буквы вычисляется ниже ИЗ ЭТОЙ таблицы, и стоит выкинуть из
# испанского набора «ó», как она станет «исключительно польской» — и испанский ролик
# получит подсказку на польский канал. Ошибка канала необратима, поэтому таблица
# описывает языки честно, а не удобно.
LANG_LETTERS = {
    "ru": "ёъыэ",
    "uk": "єіїґ",
    "pl": "ąćęłńóśźż",
    "cs": "áčďéěíňóřšťúůýž",
    "es": "áéíñóúü¡¿",
    "de": "äöüß",
    "fr": "àâæçéèêëîïôœùûÿ",
    "tr": "çğıöşü",
    "it": "àèéìòù",
    "pt": "ãâáàçéêíõóôú",
    "ro": "ăâîșț",
    "hu": "áéíóöőúüű",
}

# Буква, которая есть ровно у одного языка. Только по таким и опознаём: «ó» встречается
# у пяти языков и не говорит ни о чём, «ł» — только у польского и говорит всё.
LANG_MARKS = {}
for _lang, _letters in LANG_LETTERS.items():
    for _ch in _letters:
        LANG_MARKS.setdefault(_ch, set()).add(_lang)
LANG_MARKS = {ch: next(iter(langs)) for ch, langs in LANG_MARKS.items() if len(langs) == 1}


def lang_of_title(title: str) -> str:
    """Язык названия — или пусто, если по названию его не видно.

    Никакой статистики и никаких словарей: только исключительные буквы (см. `LANG_MARKS`)
    и только при единственном ответе. Две разные пометки в одной строке — это не «две
    подсказки», это «мы не знаем», и молчание тут дешевле догадки.
    """
    hits = set()
    for ch in (title or "").lower():
        lang = LANG_MARKS.get(ch)
        if lang:
            hits.add(lang)
    return hits.pop() if len(hits) == 1 else ""


def _niche_words(ch: dict) -> list:
    """Слова ниши и тегов канала, по которым его вообще имеет смысл узнавать в названии.

    Короткие отбрасываем: тег «ww2» и ниша «сон» находятся внутри половины слов языка и
    дали бы совпадение там, где его нет.
    """
    raw = [ch.get("niche") or ""] + list(ch.get("tags") or ())
    out = []
    for piece in raw:
        for word in str(piece).lower().replace(",", " ").split():
            if len(word) >= 4 and word not in out:
                out.append(word)
    return out


def suggest(title: str, lang: str, channels: list) -> dict:
    """Предположение о канале для одной ссылки: `{channel_id, lang, why}`.

    `channel_id` — 0, когда уверенности нет, и это нормальный, а не исключительный
    исход: пустой выбор человек заполнит сам, а подставленный наугад канал он, скорее
    всего, не перепроверит — и ролик уедет не туда навсегда.

    `lang` — язык ролика, если он уже известен (его приносит полная проверка донора,
    `pub_tags.check_donor`); пусто — определяем по названию.

    Порядок сужения: язык → ниша. Канал ищется только среди работающих: ставить в
    остановленный или убранный в архив канал человек может и руками, но подсказывать
    такое нельзя.
    """
    lang = (lang or "").strip().lower()[:2] or lang_of_title(title)
    if not lang:
        return {"channel_id": 0, "lang": "",
                "why": "язык ролика по названию не виден — выберите канал сами"}
    live = [ch for ch in (channels or [])
            if (ch.get("status") or "") == "active"
            and (ch.get("lang") or "").strip().lower()[:2] == lang]
    if not live:
        return {"channel_id": 0, "lang": lang,
                "why": f"работающего канала на языке «{lang}» нет"}
    if len(live) == 1:
        return {"channel_id": live[0]["id"], "lang": lang,
                "why": f"единственный работающий канал на языке «{lang}»"}
    low = (title or "").lower()
    by_niche = [ch for ch in live if any(w in low for w in _niche_words(ch))]
    if len(by_niche) == 1:
        return {"channel_id": by_niche[0]["id"], "lang": lang,
                "why": f"язык «{lang}» и ниша «{by_niche[0].get('niche') or ''}»"}
    return {"channel_id": 0, "lang": lang,
            "why": f"каналов на языке «{lang}»: {len(live)} — выберите сами"}


# ───────────────────────── раскладка ─────────────────────────

def rows(c, tenant_id: str, urls: list, titles: dict, channels: list,
         langs: dict = None) -> list:
    """Строка-карточка на каждую ссылку: что это за ролик, не стоит ли он уже и куда
    его, скорее всего, надо.

    Ничего не записывает и в сеть не ходит: названия приходят готовыми (`titles`), их
    добывает вызывающий — ста ссылкам нужен один общий поход, а не сто подряд.
    """
    known = known_donors(c, tenant_id, urls)
    langs = langs or {}
    out = []
    for url in urls:
        title = (titles or {}).get(url) or ""
        dup = known.get(url)
        out.append({
            "url": url,
            "video_id": video_id(url),
            "title": title,
            # Пустое название — это «не добыли», и так оно и подписано. Пустая строка в
            # заголовке карточки читалась бы как «у ролика нет названия».
            "title_note": "" if title else TITLE_UNKNOWN,
            "duplicate": dup or None,
            "hint": suggest(title, langs.get(url, ""), channels),
        })
    return out


def place(c, tenant_id: str, pairs: list, now: float, titles: dict = None) -> list:
    """Поставить то, чему назначен канал. Отказ по одной строке не отменяет остальные.

    Каждая строка пишется и фиксируется сама — тем же правилом, что и массовые действия
    над каналами: общая транзакция откатила бы уже поставленное, и человек, увидев одну
    ошибку, не знал бы, что из сотни ссылок встало, а что нет.

    Строка без канала сюда не попадает вовсе (её отсеивает вызывающий) — и это не
    придирка к форме запроса: «поставить всё, что выбрано» обязано означать ровно
    выбранное, а не «выбранное плюс то, что мы предположили сами».
    """
    out = []
    for url, channel_id in pairs:
        ch = c.execute("SELECT * FROM pub_channels_live WHERE id=? AND tenant_id=?",
                       (channel_id, tenant_id)).fetchone()
        if ch is None:
            out.append({"url": url, "channel_id": channel_id, "channel": "",
                        "state": "failed", "slot": "",
                        "why": f"канала №{channel_id} нет — возможно, его уже убрали"})
            continue
        title = (titles or {}).get(url) or ""
        try:
            res = add_links(c, tenant_id, ch, [url], now, titles={url: title})
            c.commit()
        except (sqlite3.Error, ValueError) as exc:
            # `ValueError` — это в первую очередь `channel_slots.ScheduleError`: канал
            # с испорченным расписанием. Сотня ссылок не имеет права осыпаться из-за
            # одного такого канала — он обязан стать одной строкой отказа, а не
            # трассировкой на весь запрос.
            c.rollback()
            out.append({"url": url, "channel_id": ch["id"], "channel": ch["name"],
                        "state": "failed", "slot": "", "why": str(exc)})
            continue
        placed = res.get("placed") or []
        if res.get("skipped"):
            out.append({"url": url, "channel_id": ch["id"], "channel": ch["name"],
                        "state": "skipped", "slot": "",
                        "why": "этот ролик у канала уже стоит — второй раз не ставим"})
        elif placed:
            when = placed[0]
            out.append({"url": url, "channel_id": ch["id"], "channel": ch["name"],
                        "state": "placed",
                        "slot": when.isoformat() if isinstance(when, dt.datetime)
                                else str(when),
                        "why": ""})
        else:
            # Две РАЗНЫЕ причины остаться без даты, и путать их нельзя (разбор
            # 30.09.2026). «В горизонте нет свободного выхода» — это ожидание: выход
            # освободится, и ссылка встанет сама. «Расписания нет вовсе» — это тупик:
            # освобождаться нечему, ссылка пролежит в плане без даты столько, сколько
            # канал стоит ненастроенным. Обещать ей автоматическую постановку — прямая
            # неправда, и именно её студия уже перестала говорить на экране канала.
            #
            # Пустое расписание — состояние КАЖДОГО только что заведённого канала
            # (pub_schema: schedule NOT NULL DEFAULT '{}'), а не редкость.
            try:
                есть_расписание = bool(json.loads(ch["schedule"] or "{}"))
            except (TypeError, ValueError):
                есть_расписание = False
            out.append({"url": url, "channel_id": ch["id"], "channel": ch["name"],
                        "state": "queued", "slot": "",
                        "why": ("свободных выходов в горизонте нет — встанет, "
                                "как только выход освободится") if есть_расписание else
                               ("у канала не задано расписание: даты не будет, пока "
                                "его не зададут, и из очереди ссылка не выйдет")})
    return out
