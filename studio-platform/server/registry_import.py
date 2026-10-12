# -*- coding: utf-8 -*-
"""Единый список каналов: загрузка раскладки «02 — Профили и каналы.xlsx» (12.10.2026).

Команда договорилась держать связку «канал → владелец → менеджер → профиль AdsPower» в
одном месте — в студии. До этого она жила в пяти списках, и они расходились: у 14
каналов тридцатки профиль в студии не совпадал ни с «Заливка из», ни с «Где владелец»
xlsx, а 5 каналов ВВ2 студия лила из профиля, где канала нет.

Загрузка всегда начинается с ПРОБНОГО прогона: отчёт «что поменяется» без записи. Писать
— только по отдельному нажатию, и даже тогда:
  • `profile_id` (откуда льём сейчас) НЕ трогается — маршрут заливки меняется отдельным
    шагом и по одному менеджеру;
  • профиль владельца, которому xlsx противоречит и которого не подтверждают свежие
    заливки, НЕ пишется — он уходит в список «спорные» на проверку человеком.

Правило старшинства: если с профиля студии за последние 14 дней были успешные заливки
канала, этот профиль и есть место владельца — xlsx в этом месте отстал. Проверено
12.10: 9 таких каналов тридцатки и 17 ВВ2 льются нормально вопреки xlsx.

xlsx читается без сторонних библиотек: это zip с XML внутри, а нужен нам один текст.
Пароли и 2FA из соседних файлов сюда не попадают — в этом xlsx их нет, и разбор берёт
только перечисленные колонки.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RECENT_SEC = 14 * 86400
UC_RE = re.compile(r"UC[\w-]{22}")

# Колонки по заголовку, а не по букве: в xlsx колонки переставляют чаще, чем переименуют.
CHANNEL_COLS = {
    "manager": "№ менеджера", "upload_from": "заливка из", "code": "код", "name": "канал",
    "lang": "язык", "uc": "youtube", "owner_email": "почта владельца",
    "owner_where": "где владелец", "rights": "права manager", "minsk": "studio на минске",
    "long": "видео >15", "note": "комментарий",
}
MANAGER_COLS = {
    "num": "№", "profile": "профиль", "short": "короткое имя", "email": "почта менеджера",
    "rights": "права приняты", "note": "комментарий",
}


class RegistryError(Exception):
    """Отказ, который можно показать человеку целиком."""


# ───────────────────────────────── чтение xlsx ─────────────────────────────────

def _col(ref: str) -> str:
    return re.match(r"[A-Z]+", ref).group()


def read_xlsx(data: bytes) -> Dict[str, List[Dict[str, str]]]:
    """{имя листа: [ {буква колонки: текст} по строкам ]}."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise RegistryError("это не xlsx — сохраните таблицу в формате Excel (.xlsx)")
    names = set(z.namelist())
    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter("{%s}t" % NS["m"]))
                  for si in root.findall("m:si", NS)]
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out: Dict[str, List[Dict[str, str]]] = {}
    rid_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sh in wb.iter("{%s}sheet" % NS["m"]):
        path = target.get(sh.get(rid_key), "")
        path = path.lstrip("/")
        path = path if path.startswith("xl/") else "xl/" + path
        if path not in names:
            continue
        rows = []
        for r in ET.fromstring(z.read(path)).iter("{%s}row" % NS["m"]):
            d = {}
            for cell in r.iter("{%s}c" % NS["m"]):
                v = cell.find("m:v", NS)
                text = v.text if v is not None and v.text is not None else ""
                if cell.get("t") == "s" and text:
                    text = shared[int(text)]
                elif cell.get("t") == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter("{%s}t" % NS["m"]))
                d[_col(cell.get("r"))] = text.strip()
            rows.append(d)
        out[sh.get("name") or ""] = rows
    return out


def _table(rows: List[Dict[str, str]], cols: Dict[str, str], must: str) -> List[Dict[str, str]]:
    """Найти строку заголовка (в ней есть колонка `must`) и разложить строки под ним."""
    for i, row in enumerate(rows):
        heads = {k: v.lower() for k, v in row.items()}
        if not any(h.startswith(cols[must].lower()) for h in heads.values()):
            continue
        letter = {}
        for key, title in cols.items():
            for col, h in heads.items():
                if h.startswith(title.lower()) and col not in letter.values():
                    letter[key] = col
                    break
        out = []
        for r in rows[i + 1:]:
            item = {k: r.get(c, "") for k, c in letter.items()}
            if any(item.values()):
                out.append(item)
        return out
    return []


def _sheet(book, *starts) -> List[Dict[str, str]]:
    for name, rows in book.items():
        if any(name.lower().startswith(s) for s in starts):
            return rows
    return []


def serial_of(text: str) -> str:
    """«P30», «№30», «30» → «30». Пусто, если номера нет."""
    m = re.search(r"\d+", str(text or ""))
    return str(int(m.group())) if m else ""


def rights_of(text: str) -> str:
    t = str(text or "").strip().lower()
    if not t or t.startswith("не ") or t in ("нет", "-", "—"):
        return ""
    if "принят" in t or t in ("да", "yes", "✓", "+"):
        return "accepted"
    if "отправ" in t:
        return "sent"
    if "снят" in t or "отозв" in t:
        return "revoked"
    return ""


def yes_no(text: str) -> Optional[int]:
    t = str(text or "").strip().lower()
    if t in ("да", "yes", "✓", "+", "можно", "есть"):
        return 1
    if t in ("нет", "no", "-", "—", "нельзя"):
        return 0
    return None


def parse(book) -> dict:
    channels = _table(_sheet(book, "канал"), CHANNEL_COLS, "uc")
    managers = _table(_sheet(book, "менеджер"), MANAGER_COLS, "email")
    if not channels:
        raise RegistryError("не нашёл лист «Каналы» с колонкой «YouTube / точный UC»")
    mans = []
    for m in managers:
        num = serial_of(m.get("num"))
        if not num or "@" not in (m.get("email") or ""):
            continue
        mans.append({"num": int(num), "short": m.get("short", ""), "email": m["email"],
                     "profile": serial_of(m.get("profile")),
                     "rights": m.get("rights", ""), "note": m.get("note", "")})
    chs = []
    for ch in channels:
        uc = UC_RE.search(ch.get("uc") or "")
        chs.append({"uc": uc.group() if uc else "", "name": ch.get("name", ""),
                    "code": ch.get("code", ""), "manager": serial_of(ch.get("manager")),
                    "upload_from": serial_of(ch.get("upload_from")),
                    "owner_where": serial_of(ch.get("owner_where")),
                    "owner_email": (ch.get("owner_email") or "").strip(),
                    "rights": rights_of(ch.get("rights")), "long_ok": yes_no(ch.get("long")),
                    "note": ch.get("note", "")})
    return {"managers": mans, "channels": chs}


# ──────────────────────────────── план и запись ────────────────────────────────

def _profiles(c, tenant: str) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for r in c.execute("SELECT id, serial, default_host_id FROM pub_profiles "
                       "WHERE tenant_id=?", (tenant,)):
        out.setdefault(serial_of(r["serial"]), []).append(r)
    return out


def _profile_id(profiles, serial: str, host_id=None) -> Optional[int]:
    rows = profiles.get(serial or "") or []
    if host_id is not None:
        same = [r for r in rows if r["default_host_id"] == host_id]
        if len(same) == 1:
            return same[0]["id"]
    return rows[0]["id"] if len(rows) == 1 else None


def plan(c, tenant: str, parsed: dict, now: Optional[float] = None) -> dict:
    """Что поменяется — без записи. Возвращает и сами правки, и отчёт для человека."""
    now = time.time() if now is None else now
    profiles = _profiles(c, tenant)
    serial_by_pid = {r["id"]: s for s, rows in profiles.items() for r in rows}
    studio = {r["uc"]: r for r in c.execute(
        "SELECT id, uc, name, niche, status, profile_id, host_id, owner_email, "
        "owner_profile_id, manager_id, mgr_rights, long_ok FROM pub_channels "
        "WHERE tenant_id=? AND COALESCE(uc,'')<>''", (tenant,))}
    known_mgr = {r["num"]: r for r in c.execute(
        "SELECT * FROM pub_managers WHERE tenant_id=?", (tenant,))}

    report = {"managers_new": 0, "managers_changed": 0, "channels_changed": 0,
              "xlsx_stale": [], "disputed": [], "not_in_studio": [], "not_in_xlsx": [],
              "no_profile": [], "same": 0}
    writes = {"managers": [], "channels": []}

    for m in parsed["managers"]:
        pid = _profile_id(profiles, m["profile"])
        if m["profile"] and pid is None:
            report["no_profile"].append(f"менеджер №{m['num']}: профиля P{m['profile']} "
                                        "нет в студии — сверьте профили с AdsPower")
        row = {"num": m["num"], "short": m["short"], "email": m["email"], "profile_id": pid}
        old = known_mgr.get(m["num"])
        if old is None:
            report["managers_new"] += 1
        elif any(old[k] != v for k, v in row.items()):
            report["managers_changed"] += 1
        writes["managers"].append(row)

    seen = set()
    for x in parsed["channels"]:
        if not x["uc"]:
            continue
        seen.add(x["uc"])
        ch = studio.get(x["uc"])
        if ch is None:
            report["not_in_studio"].append(f"{x['name'] or x['code']} ({x['uc']})")
            continue
        cur = serial_by_pid.get(ch["profile_id"], "")
        ok_recent = c.execute(
            "SELECT COUNT(*) FROM pub_upload_jobs WHERE channel_id=? AND profile_id=? "
            "AND status='done' AND updated>?", (ch["id"], ch["profile_id"], now - RECENT_SEC)
        ).fetchone()[0] if ch["profile_id"] is not None else 0

        owner_pid = None
        if cur and cur == x["owner_where"]:
            owner_pid = ch["profile_id"]
        elif cur and ok_recent:
            # Свежие заливки — факт; xlsx в этом месте отстал.
            owner_pid = ch["profile_id"]
            if cur != x["owner_where"]:
                report["xlsx_stale"].append({
                    "channel": ch["name"], "uc": x["uc"], "studio": cur,
                    "xlsx_owner": x["owner_where"], "xlsx_upload": x["upload_from"],
                    "uploads_14d": ok_recent})
        else:
            report["disputed"].append({
                "channel": ch["name"], "uc": x["uc"], "studio": cur,
                "xlsx_owner": x["owner_where"], "xlsx_upload": x["upload_from"],
                "uploads_14d": ok_recent})

        mgr_num = int(x["manager"]) if x["manager"] else None
        new = {"owner_email": x["owner_email"] or None, "manager_num": mgr_num,
               "mgr_rights": x["rights"], "long_ok": x["long_ok"]}
        if owner_pid is not None:
            new["owner_profile_id"] = owner_pid
        old_mgr_num = None
        if ch["manager_id"] is not None:
            r = c.execute("SELECT num FROM pub_managers WHERE id=?", (ch["manager_id"],)).fetchone()
            old_mgr_num = r["num"] if r else None
        changed = (new["owner_email"] != ch["owner_email"] or mgr_num != old_mgr_num
                   or new["mgr_rights"] != (ch["mgr_rights"] or "")
                   or new["long_ok"] != ch["long_ok"]
                   or ("owner_profile_id" in new and new["owner_profile_id"] != ch["owner_profile_id"]))
        if changed:
            report["channels_changed"] += 1
            writes["channels"].append({"id": ch["id"], **new})
        else:
            report["same"] += 1

    for uc, ch in studio.items():
        if uc not in seen and ch["status"] != "archived":
            report["not_in_xlsx"].append(f"{ch['name']} ({uc})")
    return {"report": report, "writes": writes}


def apply(c, tenant: str, planned: dict, now: Optional[float] = None) -> dict:
    """Записать план. `profile_id` каналов не трогается никогда — см. шапку модуля."""
    now = time.time() if now is None else now
    for m in planned["writes"]["managers"]:
        c.execute("INSERT INTO pub_managers(tenant_id,num,short,email,profile_id,created,updated) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,num) DO UPDATE SET "
                  "short=excluded.short, email=excluded.email, profile_id=excluded.profile_id, "
                  "updated=excluded.updated",
                  (tenant, m["num"], m["short"], m["email"], m["profile_id"], now, now))
    mgr = {r["num"]: r["id"] for r in c.execute(
        "SELECT id, num FROM pub_managers WHERE tenant_id=?", (tenant,))}
    for ch in planned["writes"]["channels"]:
        old = c.execute("SELECT mgr_rights FROM pub_channels WHERE id=?", (ch["id"],)).fetchone()
        sets = {"owner_email": ch["owner_email"],
                "manager_id": mgr.get(ch["manager_num"]) if ch["manager_num"] else None,
                "mgr_rights": ch["mgr_rights"], "long_ok": ch["long_ok"], "updated": now}
        if (old["mgr_rights"] or "") != ch["mgr_rights"]:
            sets["mgr_rights_at"] = now
        if "owner_profile_id" in ch:
            sets["owner_profile_id"] = ch["owner_profile_id"]
        c.execute("UPDATE pub_channels SET " + ", ".join(f"{k}=?" for k in sets)
                  + " WHERE id=? AND tenant_id=?", (*sets.values(), ch["id"], tenant))
    return {"managers": len(planned["writes"]["managers"]),
            "channels": len(planned["writes"]["channels"])}
