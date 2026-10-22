#!/usr/bin/env python3
"""
mail_monitor.py — приёмник служебной почты для мониторинга часов/монетизации.

Реализация §8/§10 ТЗ "Автоматический сбор часов YouTube и уведомлений Gmail"
через IMAP + пароль приложения (вместо Gmail API OAuth — проще, без 7-дневного
токена и верификации restricted-scope; владелец выбрал этот путь).

Принципы (жёстко):
  * Читает ТОЛЬКО read-only (SELECT readonly, никаких STORE/EXPUNGE/удалений).
  * Пишет в СВОЮ базу /srv/studio/data/mail_monitor.db — прод-базу vsgen.db НЕ трогает.
  * Текст письма — ДАННЫЕ. Никогда не исполняется и не инициирует действий с аккаунтом.
  * Идемпотентно: дедуп по (uidvalidity, uid) и Message-ID; события по idem_key.
  * Переживает перезапуск: checkpoint (uidvalidity, last_uid) в своей БД.
  * "Нет новых писем" != "пересылка работает". Статус маршрута храним отдельно.

CLI:
  --init     создать БД и поставить checkpoint на текущий максимум UID
             (пилот стартует «с этого момента», старые 51 письма не трогаем)
  --once     один проход опроса (это делает служба/таймер, не оператор вручную)
  --status   напечатать счётчики и состояние (без чтения содержимого писем)
"""
import argparse
import email
import email.utils
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

ENV_PATH = os.environ.get("MAIL_MONITOR_ENV", "/srv/studio/mail_monitor.env")
DB_PATH = os.environ.get("MAIL_MONITOR_DB", "/srv/studio/data/mail_monitor.db")
RAW_DIR = os.environ.get("MAIL_MONITOR_RAW", "/srv/studio/data/mail_raw")
IMAP_SOCKET_TIMEOUT = float(os.environ.get("MAIL_IMAP_TIMEOUT_SEC", "60") or 60)

# Правила классификации общие с приёмом строк Gmail с Минска (`server/pub_mail.py`):
# одно письмо не должно получать две категории в зависимости от дороги. На VPS
# читалка лежит копией в /srv/studio рядом с базой, поэтому ищем модуль и рядом с
# собой, и в каталоге приложения.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from mail_rules import RULES, UC_RE, classify          # noqa: E402,F401
except ImportError:                                        # копия вне репозитория
    sys.path.insert(0, "/srv/studio/app/workers/mail")
    from mail_rules import RULES, UC_RE, classify          # noqa: E402,F401

VIDEO_RE = re.compile(r"(?:watch\?v=|youtu\.be/|/video/|/shorts/)([0-9A-Za-z_-]{11})")
LINK_RE = re.compile(r"https?://[^\s<>\"')]+")


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def load_env(path=ENV_PATH):
    cfg = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    for k in ("MAIL_IMAP_HOST", "MAIL_IMAP_USER", "MAIL_IMAP_PASS"):
        if k not in cfg:
            raise SystemExit(f"в {path} нет ключа {k}")
    return cfg


def db_connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            mailbox TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            message_id TEXT,
            received_utc TEXT,
            from_hdr TEXT,
            to_hdr TEXT,
            delivered_to TEXT,
            route_tag TEXT,
            subject TEXT,
            links TEXT,
            raw_path TEXT,
            fetched_utc TEXT,
            UNIQUE(mailbox, uidvalidity, uid)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_msgid
            ON messages(message_id) WHERE message_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            message_ref INTEGER REFERENCES messages(id),
            type TEXT NOT NULL,
            uc TEXT,
            account TEXT,
            source TEXT,
            event_time TEXT,
            binding_confidence TEXT,   -- high | medium | low | review
            idem_key TEXT UNIQUE NOT NULL,
            created_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS mail_route (
            route_tag TEXT PRIMARY KEY,   -- метка из z09052581860+<tag>@
            source TEXT,                  -- исходная почта канала
            uc TEXT,                      -- канал, к которому привязан этот маршрут
            status TEXT,                  -- configured | pending | unknown
            last_delivery_utc TEXT
        );
        """
    )
    conn.commit()


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key, value):
    conn.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def imap_connect(cfg):
    # 22.10.2026: без таймаута сокета один проход завис на IMAP с 18.10 17:20 на трое
    # суток — служба оставалась «activating», таймер раз в 5 минут не срабатывал, письма
    # не читались. Таймаут ловит зависший сокет; срок всего прохода режет TimeoutStartSec
    # в mail-monitor.service.
    M = imaplib.IMAP4_SSL(cfg["MAIL_IMAP_HOST"], 993, timeout=IMAP_SOCKET_TIMEOUT)
    M.login(cfg["MAIL_IMAP_USER"], cfg["MAIL_IMAP_PASS"])
    return M


def select_readonly(M, box="INBOX"):
    typ, data = M.select(box, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT {box}: {typ} {data}")
    uidvalidity = int(M.response("UIDVALIDITY")[1][0])
    return uidvalidity


def max_uid(M):
    typ, data = M.uid("SEARCH", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return 0
    uids = [int(x) for x in data[0].split()]
    return max(uids) if uids else 0


def extract_route_tag(delivered_to, to_hdr, user):
    """Метка маршрута из плюс-алиаса z09052581860+<tag>@gmail.com (Delivered-To / To)."""
    base = user.split("@", 1)[0]
    for hdr in (delivered_to or "", to_hdr or ""):
        for addr in email.utils.getaddresses([hdr]):
            local = addr[1].split("@", 1)[0]
            if local.startswith(base + "+"):
                return local[len(base) + 1:]
    return None


def bind_channel(conn, uc_hits, route_tag):
    """Приоритет §8: явный UC в письме → маршрутная метка → иначе на проверку."""
    if uc_hits:
        return uc_hits[0], "high"
    if route_tag:
        row = conn.execute(
            "SELECT uc FROM mail_route WHERE route_tag=? AND uc IS NOT NULL", (route_tag,)
        ).fetchone()
        if row and row[0]:
            return row[0], "medium"
    return None, "review"


def process_message(conn, mailbox, uidvalidity, uid, raw_bytes, user):
    msg = email.message_from_bytes(raw_bytes)
    from_hdr = str(msg.get("From", ""))
    to_hdr = str(msg.get("To", ""))
    delivered_to = str(msg.get("Delivered-To", ""))
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    message_id = (msg.get("Message-ID") or "").strip() or None
    date_hdr = msg.get("Date")
    try:
        received = email.utils.parsedate_to_datetime(date_hdr).astimezone(timezone.utc).isoformat()
    except Exception:
        received = None

    # тело -> только извлечение ссылок; содержимое не исполняется
    body_text = []
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            try:
                body_text.append(part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    body = "\n".join(body_text)
    links = LINK_RE.findall(body)[:50]
    uc_hits = UC_RE.findall(body) + UC_RE.findall(subject)
    route_tag = extract_route_tag(delivered_to, to_hdr, user)

    # сырой .eml рядом, отдельно от логов (§10: хранить раздельно)
    os.makedirs(RAW_DIR, exist_ok=True)
    raw_path = os.path.join(RAW_DIR, f"{uidvalidity}_{uid}.eml")
    try:
        with open(raw_path, "wb") as fh:
            fh.write(raw_bytes)
        os.chmod(raw_path, 0o600)
    except Exception:
        raw_path = None

    cur = conn.execute(
        "INSERT OR IGNORE INTO messages("
        "mailbox,uidvalidity,uid,message_id,received_utc,from_hdr,to_hdr,delivered_to,"
        "route_tag,subject,links,raw_path,fetched_utc) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mailbox, uidvalidity, uid, message_id, received, from_hdr, to_hdr, delivered_to,
         route_tag, subject, json.dumps(links), raw_path, now_utc_iso()),
    )
    if cur.rowcount == 0:
        return False  # уже было (дедуп)
    msg_ref = conn.execute(
        "SELECT id FROM messages WHERE mailbox=? AND uidvalidity=? AND uid=?",
        (mailbox, uidvalidity, uid)).fetchone()[0]

    etype = classify(from_hdr, subject)
    uc, conf = bind_channel(conn, uc_hits, route_tag)
    idem = hashlib.sha256(
        f"{message_id or (uidvalidity, uid)}|{etype}|{uc or ''}".encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO events("
        "message_ref,type,uc,account,source,event_time,binding_confidence,idem_key,created_utc) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (msg_ref, etype, uc, None, from_hdr, received, conf, idem, now_utc_iso()),
    )
    if route_tag:
        conn.execute(
            "INSERT INTO mail_route(route_tag,status,last_delivery_utc) VALUES(?,?,?) "
            "ON CONFLICT(route_tag) DO UPDATE SET last_delivery_utc=excluded.last_delivery_utc",
            (route_tag, "configured", now_utc_iso()),
        )
    return True


def poll_once(cfg, conn):
    mailbox = "INBOX"
    M = imap_connect(cfg)
    try:
        uidvalidity = select_readonly(M, mailbox)
        stored_uv = get_state(conn, "uidvalidity")
        last_uid = int(get_state(conn, "last_uid", 0))
        if stored_uv is None:
            # не инициализировано — ставим на текущий максимум (пилот с этого момента)
            cur_max = max_uid(M)
            set_state(conn, "uidvalidity", uidvalidity)
            set_state(conn, "last_uid", cur_max)
            set_state(conn, "last_run_utc", now_utc_iso())
            conn.commit()
            print(json.dumps({"init": True, "uidvalidity": uidvalidity, "last_uid": cur_max}))
            return
        if int(stored_uv) != uidvalidity:
            # UIDVALIDITY сменился → UID-ы невалидны. Ресинк: берём текущий максимум,
            # не арифметику. Дедуп по Message-ID защитит от повторов.
            cur_max = max_uid(M)
            set_state(conn, "uidvalidity", uidvalidity)
            set_state(conn, "last_uid", cur_max)
            set_state(conn, "last_run_utc", now_utc_iso())
            set_state(conn, "resync_utc", now_utc_iso())
            conn.commit()
            print(json.dumps({"resync": True, "new_uidvalidity": uidvalidity, "last_uid": cur_max}))
            return
        typ, data = M.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        new_uids = [int(x) for x in data[0].split()] if (typ == "OK" and data and data[0]) else []
        new_uids = [u for u in new_uids if u > last_uid]
        processed = 0
        for uid in sorted(new_uids):
            typ, fetched = M.uid("FETCH", str(uid), "(RFC822)")
            if typ != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            if process_message(conn, mailbox, uidvalidity, uid, raw, cfg["MAIL_IMAP_USER"]):
                processed += 1
            set_state(conn, "last_uid", uid)  # checkpoint только после сохранения
            conn.commit()
        set_state(conn, "last_run_utc", now_utc_iso())
        conn.commit()
        print(json.dumps({"polled": True, "new": len(new_uids), "processed": processed,
                          "last_uid": get_state(conn, "last_uid")}))
    finally:
        try:
            M.logout()
        except Exception:
            pass


def cmd_status(conn):
    out = {
        "db": DB_PATH,
        "uidvalidity": get_state(conn, "uidvalidity"),
        "last_uid": get_state(conn, "last_uid"),
        "last_run_utc": get_state(conn, "last_run_utc"),
        "resync_utc": get_state(conn, "resync_utc"),
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "events_by_type": dict(conn.execute(
            "SELECT type, COUNT(*) FROM events GROUP BY type").fetchall()),
        "events_by_confidence": dict(conn.execute(
            "SELECT binding_confidence, COUNT(*) FROM events GROUP BY binding_confidence").fetchall()),
        "routes": conn.execute("SELECT COUNT(*) FROM mail_route").fetchone()[0],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    conn = db_connect()
    db_init(conn)
    if args.status:
        cmd_status(conn)
        return
    cfg = load_env()
    if args.init:
        # поставить checkpoint на текущий максимум, не читая содержимого писем
        M = imap_connect(cfg)
        try:
            uidvalidity = select_readonly(M)
            cur_max = max_uid(M)
        finally:
            try:
                M.logout()
            except Exception:
                pass
        set_state(conn, "uidvalidity", uidvalidity)
        set_state(conn, "last_uid", cur_max)
        set_state(conn, "last_run_utc", now_utc_iso())
        conn.commit()
        print(json.dumps({"init": True, "uidvalidity": uidvalidity, "last_uid": cur_max}))
        return
    if args.once:
        poll_once(cfg, conn)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
