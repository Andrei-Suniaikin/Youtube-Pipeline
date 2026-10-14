#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Личный отчёт в телеграм: каждому человеку — про его каналы и только про беды.

14.10.2026, владелец: «полезная фича была бы в настройках подключать свой тг, чтобы
тебе конкретно по твоим каналам и проектам приходил отчёт, просто краткая выжимка, что
1. у этих каналов проебан AdsPower, 2. этим каналам не хватает донора».

Что здесь важно и почему.

1. **Чьи каналы — выбирает сам человек.** Владельца канала в студии нет: все каналы
   принадлежат арендатору, у `pub_channels` нет колонки «кто ведёт», а «единый список
   каналов с владельцами» владелец пока оставил идеей (12.10.2026). Заводить его здесь
   тайком нельзя. Поэтому в настройке человек сам говорит, о каких каналах ему
   писать: обо всех каналах студии (умолчание) или по отбору — теги и/или каналы.

2. **Только беды, и молчание, когда бед нет.** Владелец 28.09.2026 жаловался, что
   «очень много раз долбит прилажка». Ежедневное «всё хорошо» быстро учит сообщение не
   читать, и в день, когда беда придёт, его пролистают. Поэтому без проблем не уходит
   ничего. И по той же причине рассылка ВЫКЛЮЧЕНА, пока человек сам её не включит, и
   уходит не чаще раза в сутки — в выбранный им час.

3. **Две группы, и обе — из того, что студия уже знает, без новых правил.**
   «AdsPower» — каналы, у которых заливка упирается в профиль или вход: профиль не
   привязан, профиль пропал с машины при описи (`pub_profiles.missing_since`),
   задание заливки держит чужой открытый профиль (`pub_upload_jobs.blocked_since`, тот
   же срок, что у тревоги §13), и выпуски в разборе, чью причину переводчик
   `block_reasons` относит к стороне «Вход и профиль». «Донор» — запас материала ниже
   порога тревоги §14 (`channel_supply`) и выпуски в разборе со стороной «Донор».
   Своих регулярных выражений здесь нет намеренно: перевод причины, живущий в двух
   местах, расходится первым же исправлением формулировки.

4. **«Не знаем» — не ноль.** У канала без расписания запас не ноль, его не от чего
   считать (`known=False`). Такие каналы в «не хватает донора» не попадают; они
   перечисляются отдельной строкой, и только в отчёте, который уходит и так, — сами по
   себе они повода писать не дают (иначе канал без расписания слал бы письмо каждый
   день навсегда).

5. **Привязка без chat_id.** Свой chat_id не знает никто. Человек получает в профиле
   одноразовый код (живёт 15 минут) и ссылку t.me/<бот>?start=<код>; нажимает Start —
   бот получает «/start <код>», и студия при очередном опросе getUpdates привязывает
   ЭТОТ чат к ЭТОМУ человеку. Код одноразовый: повторное «/start» с тем же кодом уже
   ни к кому не привяжет. Опрос идёт только пока у кого-то есть живой код — иначе
   студия не трогает очередь апдейтов бота вовсе (ею пользуется ещё и
   `dev/alerts.py --whoami`, который смотрит апдейты без подтверждения).

Сети в модуле нет: транспорт передаётся объектом с методами `send_to(chat, text)`,
`updates(offset)` и `me()` — это `pub_alerts_tg.Telegram`, а в тестах подставной.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import time

import pub_alerts
from block_reasons import ACCESS, DONOR, explain as explain_block
from channel_supply import channel_supply, supply_facts
from pub_digest import DIGEST_TZ, _zone                               # noqa: F401

LIMIT = 4096                     # предел одного сообщения Telegram
CODE_TTL = 15 * 60               # сколько живёт код привязки
DEFAULT_HOUR = 9
OFFSET_KEY = "tg_personal_offset"     # докуда прочитаны апдейты бота
POLL_KEY = "tg_personal_poll"         # чем кончился последний опрос (ошибка — словами)
BOT_KEY = "tg_bot_me"                 # имя бота: спрашиваем у Telegram один раз
BOT_TTL = 24 * 3600

SIDE_ADSPOWER = "adspower"
SIDE_DONOR = "donor"


# ───────────────────────────── хранение ─────────────────────────────

def ensure_schema(c) -> None:
    """Таблица привязок. Одна строка на человека: чат у человека один.

    Создаётся здесь, а не в общей схеме: проход тревог (`dev/alerts.py`) ходит в базу
    своим таймером и схему не создаёт — он запросто приходит раньше, чем служба
    кабинета применит миграцию, и падать на отсутствующей таблице не должен.
    `tenant_id` — арендатор, каналы которого показывал пульт в момент привязки: отчёт
    не должен вдруг начать рассказывать про чужие каналы.
    """
    c.execute("CREATE TABLE IF NOT EXISTS pub_user_tg("
              "user_id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, "
              "chat_id TEXT, chat_title TEXT, "
              "enabled INTEGER NOT NULL DEFAULT 0, hour INTEGER NOT NULL DEFAULT 9, "
              "filter TEXT NOT NULL DEFAULT '{}', "
              "link_code TEXT, link_expires REAL, linked_at REAL, "
              "last_day TEXT, last_sent REAL, updated REAL)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_user_tg_code "
              "ON pub_user_tg(link_code) WHERE link_code IS NOT NULL")


def _row(c, user_id: int):
    ensure_schema(c)
    return c.execute("SELECT * FROM pub_user_tg WHERE user_id=?", (int(user_id),)).fetchone()


def _ensure_row(c, user_id: int, tenant_id: str, now: float):
    if _row(c, user_id) is None:
        c.execute("INSERT INTO pub_user_tg(user_id,tenant_id,hour,updated) VALUES(?,?,?,?)",
                  (int(user_id), str(tenant_id), DEFAULT_HOUR, now))
    return _row(c, user_id)


def clean_filter(raw) -> dict:
    """Отбор каналов к единой форме: {"mode": "all"|"pick", "tags": [id], "channels": [id]}.

    Пустой отбор «по выбору» — не «ничего», а ошибка ввода: человек, снявший все галочки,
    иначе получил бы вечное молчание и думал бы, что у его каналов всё хорошо.
    """
    raw = raw if isinstance(raw, dict) else {}
    mode = "pick" if raw.get("mode") == "pick" else "all"

    def ids(xs):
        out = []
        for x in xs or []:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v not in out:
                out.append(v)
        return out
    tags, chans = ids(raw.get("tags")), ids(raw.get("channels"))
    if mode == "pick" and not (tags or chans):
        raise ValueError("выберите хотя бы один тег или канал — или «все каналы»")
    return {"mode": mode, "tags": tags if mode == "pick" else [],
            "channels": chans if mode == "pick" else []}


def _filter_of(row) -> dict:
    try:
        return clean_filter(json.loads(row["filter"] or "{}"))
    except (ValueError, TypeError):
        return {"mode": "all", "tags": [], "channels": []}


def settings(c, user_id: int, now: float = None) -> dict:
    """Что настроено у ЭТОГО человека. Чужие строки отсюда не видны по построению."""
    now = time.time() if now is None else float(now)
    row = _row(c, user_id)
    if row is None:
        return {"linked": False, "chat_title": "", "enabled": False, "hour": DEFAULT_HOUR,
                "tz": DIGEST_TZ, "filter": {"mode": "all", "tags": [], "channels": []},
                "code": "", "code_expires": None, "linked_at": None, "last_sent": None}
    live = bool(row["link_code"]) and float(row["link_expires"] or 0) > now
    return {"linked": bool(row["chat_id"]), "chat_title": row["chat_title"] or "",
            "enabled": bool(row["enabled"]) and bool(row["chat_id"]),
            "hour": int(row["hour"] if row["hour"] is not None else DEFAULT_HOUR),
            "tz": DIGEST_TZ, "filter": _filter_of(row),
            "code": row["link_code"] if live else "",
            "code_expires": row["link_expires"] if live else None,
            "linked_at": row["linked_at"], "last_sent": row["last_sent"]}


def set_settings(c, user_id: int, tenant_id: str, enabled=None, hour=None, flt=None,
                 now: float = None) -> dict:
    now = time.time() if now is None else float(now)
    row = _ensure_row(c, user_id, tenant_id, now)
    fields = {}
    if enabled is not None:
        if enabled and not row["chat_id"]:
            raise ValueError("сначала подключите телеграм — отчёту некуда приходить")
        fields["enabled"] = 1 if enabled else 0
    if hour is not None:
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            raise ValueError("час отправки — целое число от 0 до 23")
        if not 0 <= hour <= 23:
            raise ValueError("час отправки — целое число от 0 до 23")
        fields["hour"] = hour
    if flt is not None:
        flt = clean_filter(flt)
        _check_own(c, tenant_id, flt)
        fields["filter"] = json.dumps(flt)
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE pub_user_tg SET {sets}, updated=? WHERE user_id=?",
                  (*fields.values(), now, int(user_id)))
        c.commit()
    return settings(c, user_id, now)


def _check_own(c, tenant_id: str, flt: dict) -> None:
    """Теги и каналы отбора — только своего арендатора. Чужой id — отказ, а не тишина."""
    for tid in flt["tags"]:
        if not c.execute("SELECT 1 FROM pub_tags WHERE id=? AND tenant_id=?",
                         (tid, tenant_id)).fetchone():
            raise ValueError(f"тега {tid} нет среди тегов студии")
    for cid in flt["channels"]:
        if not c.execute("SELECT 1 FROM pub_channels WHERE id=? AND tenant_id=?",
                         (cid, tenant_id)).fetchone():
            raise ValueError(f"канала {cid} нет среди каналов студии")


def start_link(c, user_id: int, tenant_id: str, now: float = None) -> dict:
    """Новый одноразовый код привязки. Прежний живой код человека этим гасится."""
    now = time.time() if now is None else float(now)
    _ensure_row(c, user_id, tenant_id, now)
    # 12 символов из [A-Za-z0-9_-] — Telegram пропускает в start ровно этот алфавит.
    code = secrets.token_urlsafe(9)
    c.execute("UPDATE pub_user_tg SET link_code=?, link_expires=?, tenant_id=?, updated=? "
              "WHERE user_id=?", (code, now + CODE_TTL, str(tenant_id), now, int(user_id)))
    c.commit()
    return {"code": code, "expires": now + CODE_TTL}


def unlink(c, user_id: int, now: float = None) -> dict:
    """Отключить: чат забывается, рассылка гаснет. Отбор и час остаются — вдруг вернётся."""
    now = time.time() if now is None else float(now)
    if _row(c, user_id) is not None:
        c.execute("UPDATE pub_user_tg SET chat_id=NULL, chat_title=NULL, enabled=0, "
                  "link_code=NULL, link_expires=NULL, linked_at=NULL, updated=? "
                  "WHERE user_id=?", (now, int(user_id)))
        c.commit()
    return settings(c, user_id, now)


def bind_code(c, code: str, chat_id, chat_title: str = "", now: float = None) -> dict:
    """Привязать чат по коду. {"ok", "why", "user_id"}.

    Код ищется ровно как пришёл; просроченный не привязывает, но и не стирается молча —
    человек увидит в профиле, что код истёк, и возьмёт новый. Использованный код
    стирается в той же записи, что и привязка: второго «/start» с ним не бывает.
    """
    now = time.time() if now is None else float(now)
    ensure_schema(c)
    code = str(code or "").strip()
    if not code:
        return {"ok": False, "why": "unknown", "user_id": None}
    row = c.execute("SELECT * FROM pub_user_tg WHERE link_code=?", (code,)).fetchone()
    if row is None:
        return {"ok": False, "why": "unknown", "user_id": None}
    if float(row["link_expires"] or 0) <= now:
        return {"ok": False, "why": "expired", "user_id": row["user_id"]}
    c.execute("UPDATE pub_user_tg SET chat_id=?, chat_title=?, link_code=NULL, "
              "link_expires=NULL, linked_at=?, updated=? WHERE user_id=? AND link_code=?",
              (str(chat_id), str(chat_title or "")[:120], now, now, row["user_id"], code))
    c.commit()
    return {"ok": True, "why": "", "user_id": row["user_id"]}


def _pending(c, now: float) -> bool:
    ensure_schema(c)
    return c.execute("SELECT 1 FROM pub_user_tg WHERE link_code IS NOT NULL "
                     "AND link_expires>? LIMIT 1", (now,)).fetchone() is not None


def _chat_title(chat: dict) -> str:
    return (chat.get("title") or " ".join(x for x in (chat.get("first_name"),
                                                      chat.get("last_name")) if x)
            or (f"@{chat['username']}" if chat.get("username") else "") or "")


def _start_code(text: str) -> str:
    """«/start КОД» или «/start@имя_бота КОД» → КОД. Иначе пусто."""
    parts = str(text or "").strip().split()
    if len(parts) != 2 or not parts[0].split("@", 1)[0] == "/start":
        return ""
    return parts[1]


REPLY = {
    "ok": "Готово: этот чат привязан к студии ({name}). Отчёт о бедах ваших каналов "
          "включается в профиле студии — блок «Telegram-отчёт».",
    "expired": "Код устарел (живёт 15 минут). Возьмите новый в профиле студии: "
               "«Подключить Telegram».",
    "unknown": "Код не узнан или уже использован. Возьмите новый в профиле студии: "
               "«Подключить Telegram».",
}


def poll(c, tg, now: float = None, force: bool = False) -> dict:
    """Один короткий опрос апдейтов бота: привязать чаты, приславшие «/start <код>».

    Без живых кодов не опрашиваем вовсе (см. шапку модуля, п. 5) — если только не
    `force` (кнопка «проверить» в профиле). Ошибка опроса запоминается словами: самая
    вероятная — у бота задан webhook, и тогда getUpdates не работает в принципе.
    """
    now = time.time() if now is None else float(now)
    if not force and not _pending(c, now):
        return {"polled": False, "bound": [], "error": ""}
    offset = pub_alerts._state_row(c, OFFSET_KEY)                  # noqa: SLF001
    offset = int(offset) if isinstance(offset, (int, float)) else 0
    r = tg.updates(offset)
    if not r.get("ok"):
        err = r.get("error") or "Telegram не ответил"
        if r.get("code") == 409 or "webhook" in err.lower():
            err = ("у бота задан webhook — Telegram не отдаёт сообщения студии, "
                   "привязать чат нельзя. Снять webhook: deleteWebhook (делает владелец бота)")
        pub_alerts._state_save(c, POLL_KEY, {"error": err, "at": now}, now)   # noqa: SLF001
        return {"polled": True, "bound": [], "error": err}
    bound, top = [], offset
    for u in r.get("result") or []:
        uid = int(u.get("update_id") or 0)
        top = max(top, uid + 1)
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        code = _start_code(msg.get("text"))
        if not code or chat.get("id") is None:
            continue
        res = bind_code(c, code, chat["id"], _chat_title(chat), now=now)
        name = ""
        if res["ok"]:
            bound.append(res["user_id"])
            u_row = c.execute("SELECT name FROM users WHERE id=?",
                              (res["user_id"],)).fetchone() if _has_users(c) else None
            name = (u_row[0] if u_row else "") or f"учётка {res['user_id']}"
        try:
            tg.send_to(chat["id"], REPLY[res["why"] or "ok"].format(name=name))
        except Exception:                                          # noqa: BLE001
            pass            # ответ в чат — любезность; привязка уже записана
    if top != offset:
        pub_alerts._state_save(c, OFFSET_KEY, top, now)            # noqa: SLF001
    pub_alerts._state_save(c, POLL_KEY, {"error": "", "at": now}, now)    # noqa: SLF001
    return {"polled": True, "bound": bound, "error": ""}


def _has_users(c) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
                     ).fetchone() is not None


def poll_error(c) -> str:
    saved = pub_alerts._state_row(c, POLL_KEY)                     # noqa: SLF001
    return (saved or {}).get("error") or "" if isinstance(saved, dict) else ""


def bot_username(c, tg, now: float = None) -> tuple:
    """(имя бота, беда). Имя кэшируется на сутки: getMe на каждую кнопку не нужен."""
    now = time.time() if now is None else float(now)
    saved = pub_alerts._state_row(c, BOT_KEY)                      # noqa: SLF001
    if isinstance(saved, dict) and saved.get("username") \
            and now - float(saved.get("at") or 0) < BOT_TTL:
        return saved["username"], ""
    me = tg.me() or {}
    if not me.get("username"):
        return "", me.get("error") or "Telegram не назвал имя бота"
    pub_alerts._state_save(c, BOT_KEY, {"username": me["username"], "at": now}, now)  # noqa: SLF001
    return me["username"], ""


# ───────────────────────────── что в отчёте ─────────────────────────────

def _channels(c, tenant_id: str, flt: dict) -> list:
    """Каналы отчёта: живые каналы арендатора, отобранные настройкой человека."""
    rows = c.execute("SELECT * FROM pub_channels_live WHERE tenant_id=? "
                     "AND status!='archived' ORDER BY name COLLATE NOCASE",
                     (tenant_id,)).fetchall()
    if flt["mode"] != "pick":
        return rows
    keep = set(flt["channels"])
    if flt["tags"]:
        marks = ",".join("?" * len(flt["tags"]))
        keep |= {r[0] for r in c.execute(
            f"SELECT channel_id FROM pub_channel_tags WHERE tag_id IN ({marks})",
            flt["tags"])}
    return [r for r in rows if r["id"] in keep]


def _add(out: dict, cid: int, title: str, n: int = 0) -> None:
    """Причина канала: одинаковые складываются в счёт выпусков, а не повторяются."""
    rec = out.setdefault(cid, {})
    rec[title] = rec.get(title, 0) + n


def collect(c, tenant_id: str, flt: dict = None, now: float = None) -> dict:
    """Беды выбранных каналов. Ничего не пишет и никуда не ходит — только читает."""
    now = time.time() if now is None else float(now)
    flt = flt or {"mode": "all", "tags": [], "channels": []}
    pub_alerts.load_limits(c)                  # порог запаса — тот же, что у тревоги §14
    channels = _channels(c, tenant_id, flt)
    by_id = {r["id"]: r for r in channels}
    ads: dict = {}
    donor: dict = {}
    unknown: list = []
    if not channels:
        return {"adspower": [], "donor": [], "supply_unknown": [], "channels": 0,
                "min_days": float(pub_alerts.SUPPLY_MIN_DAYS)}
    active = [r for r in channels if (r["status"] or "active") == "active"]

    # ── AdsPower: профиль канала ──
    prof = {}
    if active:
        ids = [r["profile_id"] for r in active if r["profile_id"]]
        if ids:
            marks = ",".join("?" * len(ids))
            prof = {p["id"]: p for p in c.execute(
                f"SELECT id, serial, status, missing_since FROM pub_profiles "
                f"WHERE id IN ({marks})", ids)}
    for r in active:
        if not r["profile_id"]:
            _add(ads, r["id"], "профиль AdsPower не привязан к каналу")
            continue
        p = prof.get(r["profile_id"])
        if p is None:
            _add(ads, r["id"], "профиль канала удалён из студии")
        elif p["missing_since"] is not None:
            when = dt.datetime.fromtimestamp(float(p["missing_since"]),
                                             _zone(DIGEST_TZ)).strftime("%d.%m")
            _add(ads, r["id"], f"профиль {p['serial']} пропал из AdsPower (с {when})")

    # ── заливка ждёт чужой открытый профиль дольше срока тревоги §13 ──
    if any(x[1] == "blocked_since" for x in c.execute("PRAGMA table_info(pub_upload_jobs)")):
        for j in c.execute("SELECT channel_id, blocked_since FROM pub_upload_jobs "
                           "WHERE tenant_id=? AND status='queued' "
                           "AND blocked_since IS NOT NULL", (tenant_id,)):
            if j["channel_id"] in by_id and \
                    now - float(j["blocked_since"]) >= pub_alerts.PROFILE_BUSY_SEC:
                _add(ads, j["channel_id"], "заливка ждёт профиль, открытый посторонним", 1)

    # ── выпуски в разборе: сторона по переводчику причин ──
    for rel in c.execute("SELECT channel_id, blocked_reason FROM pub_releases "
                         "WHERE tenant_id=? AND status='blocked'", (tenant_id,)):
        if rel["channel_id"] not in by_id:
            continue
        b = explain_block(rel["blocked_reason"])
        if b.get("kind") == ACCESS:
            _add(ads, rel["channel_id"], b["title"], 1)
        elif b.get("kind") == DONOR:
            _add(donor, rel["channel_id"], b["title"], 1)

    # ── запас материала: ниже порога §14; «не знаем» — отдельно ──
    facts = supply_facts(c, tenant_id, now)
    for r in active:
        sup = channel_supply(r, facts.get(r["id"]), now)
        if not sup["known"]:
            unknown.append(r["name"])
        elif sup["days"] < pub_alerts.SUPPLY_MIN_DAYS:
            _add(donor, r["id"], sup["text"])

    def listing(side: dict) -> list:
        out = []
        for cid, reasons in side.items():
            parts = [f"{t} ({n} {_plural(n, 'выпуск', 'выпуска', 'выпусков')})" if n else t
                     for t, n in reasons.items()]
            out.append({"id": cid, "name": by_id[cid]["name"], "reasons": parts,
                        "stuck": sum(reasons.values())})
        return sorted(out, key=lambda x: (-x["stuck"], x["name"].lower()))
    return {"adspower": listing(ads), "donor": listing(donor),
            "supply_unknown": sorted(unknown, key=str.lower), "channels": len(channels),
            "min_days": float(pub_alerts.SUPPLY_MIN_DAYS)}


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def has_problems(data: dict) -> bool:
    """Повод писать — только две группы. «Запас неизвестен» сам повода не даёт (п. 4)."""
    return bool(data["adspower"] or data["donor"])


def render(data: dict, day: str, scope: str = "") -> str:
    """Отчёт текстом. Пустая строка — бед нет, отправлять нечего.

    Без разметки (как у тревог): в текст попадают имена каналов и причины, звёздочка в
    имени канала сломала бы отправку. Длина держится под пределом Telegram: что не
    влезло, сворачивается в «и ещё N» — лучше короче, чем не дошло совсем.
    """
    if not has_problems(data):
        return ""
    head = f"Ваши каналы · {day}" + (f" · {scope}" if scope else "")
    lines = [head]
    tail = []
    if data["supply_unknown"]:
        names = data["supply_unknown"]
        shown = ", ".join(names[:10]) + (f" и ещё {len(names) - 10}" if len(names) > 10 else "")
        tail = ["", f"Запас не посчитать (расписание не задано — это не ноль): {shown}"]
    budget = LIMIT - len("\n".join(tail)) - 80
    used = len(head)
    hidden = 0
    for key, title in ((SIDE_ADSPOWER, "AdsPower — заливка упирается в профиль или вход"),
                       (SIDE_DONOR, "Донор — не хватает материала")):
        items = data[key]
        if not items:
            continue
        sec = ["", f"{title} ({len(items)} "
                   f"{_plural(len(items), 'канал', 'канала', 'каналов')}):"]
        if used + len("\n".join(sec)) + 1 > budget:
            hidden += len(items)
            continue
        lines += sec
        used += len("\n".join(sec)) + 1
        for it in items:
            line = f"· {it['name']}: " + "; ".join(it["reasons"])
            if used + len(line) + 1 > budget:
                hidden += 1
                continue
            lines.append(line)
            used += len(line) + 1
    if hidden:
        lines.append(f"и ещё {hidden} — подробности в студии, экран «Выпуски» и плитки каналов")
    return "\n".join(lines + tail)[:LIMIT]


def scope_text(c, flt: dict) -> str:
    if flt["mode"] != "pick":
        return "все каналы студии"
    parts = []
    if flt["tags"]:
        marks = ",".join("?" * len(flt["tags"]))
        names = [r[0] for r in c.execute(
            f"SELECT name FROM pub_tags WHERE id IN ({marks}) ORDER BY name", flt["tags"])]
        parts.append("теги: " + ", ".join(names))
    if flt["channels"]:
        n = len(flt["channels"])
        parts.append(f"{n} {_plural(n, 'канал', 'канала', 'каналов')} по выбору")
    return "; ".join(parts)


def report_for(c, user_id: int, now: float = None) -> str:
    now = time.time() if now is None else float(now)
    row = _row(c, user_id)
    if row is None:
        return ""
    flt = _filter_of(row)
    data = collect(c, row["tenant_id"], flt, now)
    day = dt.datetime.fromtimestamp(now, _zone(DIGEST_TZ)).strftime("%d.%m.%Y")
    return render(data, day, scope_text(c, flt))


# ───────────────────────────── суточная отправка ─────────────────────────────

def daily_tick(c, tg, now: float = None) -> list:
    """Разослать личные отчёты, кому пора. Безопасно звать хоть каждую минуту.

    Кому пора: рассылка включена, чат привязан, местный час дошёл до выбранного, и за
    эти сутки отчёт ещё не собирался. Сутки помечаются собранными и тогда, когда бед нет
    и отправлять нечего: отчёт — утренний снимок, а не дежурство на весь день (дежурят
    тревоги). Непринятое Telegram сообщение помечено НЕ будет — попробуем в следующий
    проход, как у сводки и тревог.
    """
    now = time.time() if now is None else float(now)
    ensure_schema(c)
    local = dt.datetime.fromtimestamp(now, _zone(DIGEST_TZ))
    day = local.strftime("%Y-%m-%d")
    out = []
    for row in c.execute("SELECT * FROM pub_user_tg WHERE enabled=1 AND chat_id IS NOT NULL "
                         "AND (last_day IS NULL OR last_day!=?)", (day,)).fetchall():
        if local.hour < int(row["hour"] or 0):
            continue
        try:
            text = report_for(c, row["user_id"], now)
        except Exception as exc:                                   # noqa: BLE001
            out.append({"user_id": row["user_id"], "sent": False,
                        "why": f"отчёт не собрался: {exc}"})
            continue
        if text and not tg.send_to(row["chat_id"], text):
            out.append({"user_id": row["user_id"], "sent": False,
                        "why": "телеграм не принял сообщение — повторим позже"})
            continue
        c.execute("UPDATE pub_user_tg SET last_day=?, last_sent=COALESCE(?, last_sent) "
                  "WHERE user_id=?", (day, now if text else None, row["user_id"]))
        c.commit()
        out.append({"user_id": row["user_id"], "sent": bool(text),
                    "why": "" if text else "бед нет — промолчали"})
    return out


def tick(c, tg, now: float = None) -> dict:
    """Всё личное за один проход тревог: привязать новые чаты, разослать отчёты."""
    now = time.time() if now is None else float(now)
    if not getattr(tg, "token", "x"):
        # Бот не настроен: ни привязать, ни отправить нечем. Собирать отчёты каждую
        # минуту ради отказа транспорта незачем — профиль скажет об этом сам.
        return {"poll": {"polled": False, "bound": [], "error": ""}, "reports": []}
    linked = poll(c, tg, now)
    return {"poll": linked, "reports": daily_tick(c, tg, now)}


def send_now(c, tg, user_id: int, now: float = None) -> dict:
    """Кнопка «Прислать сейчас (проверка)»: отправить отчёт сразу, без учёта часа.

    В отличие от суточной рассылки, здесь молчать нельзя: человек нажал кнопку и ждёт
    сообщения. Нет бед — придёт одна строка об этом. Суточную отметку не трогает:
    проверка не должна отменять завтрашний (и сегодняшний) утренний отчёт.
    """
    now = time.time() if now is None else float(now)
    row = _row(c, user_id)
    if row is None or not row["chat_id"]:
        return {"sent": False, "why": "телеграм не подключён", "text": ""}
    text = report_for(c, user_id, now)
    empty = not text
    if empty:
        flt = _filter_of(row)
        text = (f"Проверка связи · {scope_text(c, flt)}: по AdsPower и донору бед нет. "
                f"Утренний отчёт в такие дни не приходит.")
    ok = bool(tg.send_to(row["chat_id"], text))
    return {"sent": ok, "empty": empty, "text": text,
            "why": "" if ok else "телеграм не принял сообщение"}
