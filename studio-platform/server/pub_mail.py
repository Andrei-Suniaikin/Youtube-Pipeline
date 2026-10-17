# -*- coding: utf-8 -*-
"""Почта каналов, прочитанная на Минске: приём строк Gmail, события, чтение для пульта.

Зачем (17.10.2026). Пересылку на приёмник `z09052581860@` Google этим аккаунтам
ставить не даёт («Invalid forwarding address» на живом профиле 30), и читалка
приёмника (`workers/mail/mail_monitor.py`) стоит пустой. Рабочая замена проверена на
профиле 20: открыть профиль AdsPower под бронью координатора, найти аккаунт по почте из
реестра привязок, прочитать в Gmail список писем YouTube/Google/AdSense за 7 дней —
только список, без открытия писем — и привезти строки сюда.

Что здесь:
  * `POST /api/mail/inbox` — машина присылает строки списка под тем же `X-Upload-Token`,
    что и заливка: отдельный секрет ради одной ручки — ещё одна вещь, которую
    владельцу надо выписать и положить на ноду.
  * Хранение — в базе читалки `mail_monitor.db` (НЕ в vsgen.db): таблица
    `inbox_messages` для строк и те же `events`, что у читалки, с `source='minsk_inbox'`.
    Так пульт показывает письма одним запросом независимо от того, какой дорогой они
    пришли.
  * Полных адресов почты здесь нет: машина присылает маску (`min…@gmail.com`), и мы
    маскируем ещё раз — на случай, если прислали полный. Сниппеты тоже чистятся:
    Google повторяет почту аккаунта в тексте «Security alert».
  * Дедуп по (профиль, почта, отправитель, тема, дата): у строки списка нет Message-ID,
    и повторный обход за те же 7 дней обязан ничего не удвоить.
  * Привязка к каналу — §8 ТЗ: явный UC в письме → почта аккаунта с единственным
    каналом → профиль с единственным каналом → иначе «на проверку». Совпадение по
    названию канала в сниппете привязкой не считается никогда.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
_MAIL_DIR = os.path.join(HERE, "..", "workers", "mail")
if _MAIL_DIR not in sys.path:
    sys.path.insert(0, _MAIL_DIR)
import mail_rules as R                                        # noqa: E402
import mail_monitor as _reader                                # noqa: E402  (схема базы)

SOURCE = "minsk_inbox"
ROWS_LIMIT = 500          # больше строк за раз — не список писем за неделю, а ошибка машины


def mail_db_path() -> str:
    """Та же база, что у читалки приёмника: `MAIL_MONITOR_DB`, иначе рядом с vsgen.db
    (на VPS это /srv/studio/data/mail_monitor.db — так и настроена читалка)."""
    explicit = os.environ.get("MAIL_MONITOR_DB")
    if explicit:
        return explicit
    base = os.environ.get("VS_SERVER_DB") or os.environ.get("VS_DEV_DB") or ""
    return os.path.join(os.path.dirname(base) or ".", "mail_monitor.db")


def ensure_inbox_schema(conn: sqlite3.Connection) -> None:
    """Таблицы читалки + таблица строк с Минска. Идемпотентно."""
    _reader.db_init(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inbox_messages (
            id INTEGER PRIMARY KEY,
            msg_key TEXT UNIQUE NOT NULL,     -- sha256(профиль|почта|отправитель|тема|дата)
            source TEXT NOT NULL,             -- minsk_inbox
            host TEXT,
            profile TEXT NOT NULL,            -- серийный номер AdsPower
            login_tag TEXT,                   -- маска почты: min…@gmail.com
            login_sha256 TEXT,                -- хэш полной почты, если машина прислала
            sender TEXT,
            subject TEXT,
            snippet TEXT,                     -- без адресов почты
            date_text TEXT,                   -- как показал Gmail
            event_time TEXT,                  -- разобранная дата Gmail, без пояса
            unread INTEGER NOT NULL DEFAULT 0,
            uc_hints TEXT,                    -- JSON: UC из темы/сниппета/подсказок
            login_ucs TEXT,                   -- JSON: каналы этой почты по реестру
            fetched_utc TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_inbox_profile ON inbox_messages(profile, fetched_utc);
        """
    )
    # Колонки событий, которых у читалки не было: ссылка на строку с Минска (у
    # `message_ref` внешний ключ на письма IMAP — туда строку списка не вставить) и
    # профиль, чтобы «на проверку» было видно, с какой машины и профиля письмо.
    for col, typ in (("inbox_ref", "INTEGER"), ("profile", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass   # колонка уже есть — проверяется живым SELECT в тестах
    conn.execute("CREATE INDEX IF NOT EXISTS ix_events_uc ON events(uc, event_time)")
    conn.commit()


def open_mail_db(path: str = None) -> sqlite3.Connection:
    path = path or mail_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_inbox_schema(conn)
    return conn


# ── разбор строки ──

_GMAIL_DATE_FORMATS = (
    "%a, %b %d, %Y, %I:%M %p",     # Sun, Oct 11, 2026, 10:01 AM  (title в списке)
    "%b %d, %Y, %I:%M %p",
    "%a, %d %b %Y, %H:%M",
    "%d %b %Y, %H:%M",
)


def parse_gmail_date(text: str):
    """Дата из подсказки Gmail → ISO без пояса. Пояс Gmail не сообщает, и выдумывать
    его (UTC? Минск? пояс канала?) нельзя — ТЗ §10. Не разобрали — None, не «сейчас»."""
    t = " ".join(str(text or "").split())
    for fmt in _GMAIL_DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return None


def _clean_ucs(values) -> list:
    out = []
    for v in values or []:
        v = str(v or "").strip()
        if R.UC_RE.fullmatch(v) and v not in out:
            out.append(v)
    return out


def profile_channel_ucs(vs: sqlite3.Connection, profile: str) -> list:
    """UC каналов студии, закреплённых за этим серийным номером AdsPower (все тенанты:
    машина тенанта не знает, а серийный номер на одной машине один)."""
    if vs is None or not str(profile or "").strip():
        return []
    try:
        rows = vs.execute(
            "SELECT DISTINCT ch.uc FROM pub_channels ch JOIN pub_profiles p ON p.id=ch.profile_id "
            "WHERE p.serial=? AND ch.uc IS NOT NULL AND ch.uc<>''", (str(profile).strip(),)
        ).fetchall()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows]


def bind_channel(hits: list, login_ucs: list, profile_ucs: list):
    """Приоритет §8: явный UC → единственный канал почты → единственный канал профиля
    → иначе на проверку. Почта или профиль с несколькими каналами канал НЕ выбирают:
    письмо уровня аккаунта (безопасность, AdSense) относится ко всем сразу."""
    if hits:
        return hits[0], "high"
    if len(set(login_ucs)) == 1:
        return login_ucs[0], "medium"
    if len(set(profile_ucs)) == 1:
        return profile_ucs[0], "low"
    return None, "review"


def ingest(mail: sqlite3.Connection, vs, payload: dict, now_utc: str = None) -> dict:
    """Положить строки одного профиля (и одной почты) в базу. Возвращает счёт для машины."""
    now_utc = now_utc or datetime.now(timezone.utc).isoformat()
    profile = str(payload.get("profile") or "").strip()
    if not profile:
        raise ValueError("нет номера профиля")
    rows = payload.get("rows") or []
    if not isinstance(rows, list):
        raise ValueError("rows — не список")
    if len(rows) > ROWS_LIMIT:
        raise ValueError(f"слишком много строк за раз: {len(rows)} > {ROWS_LIMIT}")
    host = str(payload.get("host") or "")[:40]
    login_tag = R.mask_email(payload.get("login_masked") or "")[:80]
    login_sha = str(payload.get("login_sha256") or "")[:64]
    login_ucs = _clean_ucs(payload.get("login_ucs"))
    profile_ucs = profile_channel_ucs(vs, profile)

    out = {"ok": True, "profile": profile, "received": len(rows), "new": 0, "dup": 0,
           "review": 0, "events": {}}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sender = str(row.get("sender") or "").strip()[:200]
        subject = " ".join(str(row.get("subject") or "").split())[:500]
        snippet = R.mask_emails_in(" ".join(str(row.get("snippet") or "").split()))[:600]
        date_text = " ".join(str(row.get("date") or "").split())[:80]
        unread = 1 if row.get("unread") else 0
        hits = R.uc_hits(subject, snippet) + [u for u in _clean_ucs(row.get("uc_hints"))
                                              if u not in R.uc_hits(subject, snippet)]
        key = R.inbox_message_key(profile, login_tag, sender, subject, date_text)
        cur = mail.execute(
            "INSERT OR IGNORE INTO inbox_messages(msg_key,source,host,profile,login_tag,"
            "login_sha256,sender,subject,snippet,date_text,event_time,unread,uc_hints,"
            "login_ucs,fetched_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, SOURCE, host, profile, login_tag, login_sha, sender, subject, snippet,
             date_text, parse_gmail_date(date_text), unread, json.dumps(hits),
             json.dumps(login_ucs), now_utc))
        if cur.rowcount == 0:
            out["dup"] += 1
            continue
        out["new"] += 1
        ref = mail.execute("SELECT id, event_time FROM inbox_messages WHERE msg_key=?",
                           (key,)).fetchone()
        etype = R.classify(sender, subject)
        uc, conf = bind_channel(hits, login_ucs, profile_ucs)
        mail.execute(
            "INSERT OR IGNORE INTO events(inbox_ref,profile,type,uc,account,source,event_time,"
            "binding_confidence,idem_key,created_utc) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ref[0], profile, etype, uc, login_tag or None, SOURCE,
             ref[1] or now_utc, conf, R.idem_key("inbox:" + key, etype, uc), now_utc))
        out["events"][etype] = out["events"].get(etype, 0) + 1
        if conf == "review":
            out["review"] += 1
    return out


# ── чтение для пульта (только чтение, своя база, своя связь) ──

def _ro(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "events" not in have or "inbox_messages" not in have:
        conn.close()
        return None
    return conn


def _event_dict(r) -> dict:
    return {
        "id": r["id"], "type": r["type"],
        "title": R.TYPE_TITLES.get(r["type"], r["type"]),
        "uc": r["uc"] or "",
        "account_level": not r["uc"],
        "confidence": r["binding_confidence"] or "",
        "confidence_title": R.CONFIDENCE_TITLES.get(r["binding_confidence"] or "", ""),
        "source": r["source"] or "",
        "profile": r["profile"] or "",
        "login": r["login_tag"] or r["account"] or "",
        "sender": r["sender"] or "",
        "subject": r["subject"] or "",
        "snippet": r["snippet"] or "",
        "date_text": r["date_text"] or "",
        "event_time": r["event_time"] or r["created_utc"] or "",
        "unread": bool(r["unread"]),
    }


_SELECT = ("SELECT e.id, e.type, e.uc, e.account, e.source, e.event_time, e.created_utc, "
           "e.binding_confidence, e.profile, m.login_tag, m.sender, m.snippet, m.date_text, "
           "m.unread, m.login_ucs, COALESCE(m.subject, mm.subject) subject "
           "FROM events e LEFT JOIN inbox_messages m ON m.id=e.inbox_ref "
           "LEFT JOIN messages mm ON mm.id=e.message_ref ")


def channel_events(uc: str, limit: int = 20, path: str = None) -> list:
    """Последние события канала: его собственные (uc совпал) и события уровня аккаунта
    тех почт, за которыми этот канал числится по реестру (uc не выбран, но канал в
    `login_ucs`). Второе помечено `account_level` — пульт говорит об этом словами."""
    uc = str(uc or "").strip()
    conn = _ro(path or mail_db_path())
    if conn is None or not uc:
        return []
    try:
        rows = conn.execute(
            _SELECT + "WHERE e.uc=? OR (e.uc IS NULL AND m.login_ucs LIKE ?) "
            "ORDER BY COALESCE(e.event_time, e.created_utc) DESC, e.id DESC LIMIT ?",
            (uc, f'%"{uc}"%', max(1, min(100, int(limit))))).fetchall()
        return [_event_dict(r) for r in rows]
    finally:
        conn.close()


def review_events(limit: int = 50, path: str = None) -> list:
    """События без канала и без почты-подсказки — то, что человеку надо разобрать руками."""
    conn = _ro(path or mail_db_path())
    if conn is None:
        return []
    try:
        rows = conn.execute(
            _SELECT + "WHERE e.binding_confidence='review' "
            "ORDER BY COALESCE(e.event_time, e.created_utc) DESC, e.id DESC LIMIT ?",
            (max(1, min(500, int(limit))),)).fetchall()
        return [_event_dict(r) for r in rows]
    finally:
        conn.close()


def mail_counts(ucs: list, path: str = None) -> dict:
    """uc → сколько событий у канала (своих + уровня аккаунта). Один запрос на список
    каналов, не по запросу на канал."""
    conn = _ro(path or mail_db_path())
    out = {u: 0 for u in ucs or []}
    if conn is None or not out:
        return out
    try:
        for r in conn.execute("SELECT uc, COUNT(*) n FROM events WHERE uc IS NOT NULL GROUP BY uc"):
            if r["uc"] in out:
                out[r["uc"]] += r["n"]
        for r in conn.execute(
                "SELECT m.login_ucs FROM events e JOIN inbox_messages m ON m.id=e.inbox_ref "
                "WHERE e.uc IS NULL AND m.login_ucs IS NOT NULL AND m.login_ucs<>'[]'"):
            try:
                for u in json.loads(r["login_ucs"] or "[]"):
                    if u in out:
                        out[u] += 1
            except ValueError:
                continue
        return out
    finally:
        conn.close()


# ── ручка машины ──

class MailRowIn(BaseModel):
    sender: str = ""
    subject: str = ""
    snippet: str = ""
    date: str = ""
    unread: bool = False
    uc_hints: list[str] = []


class MailInboxIn(BaseModel):
    host: str = ""
    profile: str = ""
    login_masked: str = ""
    login_sha256: str = ""
    login_ucs: list[str] = []
    rows: list[MailRowIn] = []


def register_mail_routes(app, ctx):
    db = ctx["db"]
    token_of = ctx["upload_token"]
    log = ctx.get("log") or (lambda *a: None)
    db_path = ctx.get("mail_db_path") or mail_db_path

    def _same_secret(got: str, want: str) -> bool:
        # Как в `pub_upload_api`: не-ASCII в заголовке — отказ, а не 500.
        if not want or not got:
            return False
        try:
            return secrets.compare_digest(got.encode("utf-8"), want.encode("utf-8"))
        except (UnicodeError, TypeError):                      # noqa: BLE001
            return False

    def _auth(request: Request) -> None:
        if not _same_secret(request.headers.get("X-Upload-Token", ""), token_of()):
            raise HTTPException(401, "нужен токен ноды заливки")

    @app.post("/api/mail/inbox")
    def api_mail_inbox(body: MailInboxIn, request: Request):
        """Строки списка Gmail одного профиля (и одной почты) с машины."""
        _auth(request)
        vs = db()
        mail = open_mail_db(db_path())
        try:
            try:
                out = ingest(mail, vs, body.model_dump())
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            mail.commit()
        finally:
            mail.close()
            vs.close()
        log(f"почта: профиль {out['profile']} — строк {out['received']}, новых {out['new']}, "
            f"на проверку {out['review']}")
        return out
