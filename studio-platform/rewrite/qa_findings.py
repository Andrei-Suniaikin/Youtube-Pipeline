# -*- coding: utf-8 -*-
"""qa_findings — контур [F]: накопитель находок QA + добор детерминированных чеков [A].
(2026-07-21, по мотивам агентных QA-волн ww2: агрегация находок → правки пресетов
дала 72→88; пост-фикс добивает один текст, агрегация двигает все будущие.)

Пишет в job_queue.db таблицу qa_findings (additive, схему jobs НЕ трогает).
Источники: отчёт судьи (retention.json-структура) · строки cleaner'а · собственные
детерминированные чеки (палевный класс цитат, обрыв текста, длина против таргета).
Всё non-fatal: любой сбой записи не влияет на джобу.

Агрегация: python3 qa_findings.py aggregate [--since DAYS] — отчёт «системные
дефекты» за период (запускать раз в волну).
"""

import json
import os
import re
import sqlite3
from contextlib import closing
import sys
import time
from typing import Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_queue.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_findings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    created_at REAL NOT NULL,
    source     TEXT NOT NULL,   -- judge | cleaner | detcheck
    ftype      TEXT NOT NULL,   -- score | gate_fail | gate_unknown | planned_delete |
                                -- escalate | edit_applied | flag | warn | fabrication_risk |
                                -- truncated | length_dev
    severity   TEXT NOT NULL,   -- info | warn | block
    quote      TEXT,            -- улика (обрезана до 300 симв); находка без улики не пишется,
                                -- кроме агрегатных типов (score/edit_applied/length_dev)
    pos_pct    REAL,
    data       TEXT             -- JSON-довесок
);
CREATE INDEX IF NOT EXISTS idx_qaf_job   ON qa_findings(job_id);
CREATE INDEX IF NOT EXISTS idx_qaf_ftype ON qa_findings(ftype);
"""

_QUOTELESS_OK = {"score", "edit_applied", "length_dev", "gate_unknown"}


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    c = sqlite3.connect(db_path or DB_PATH, timeout=15)
    c.executescript(_SCHEMA)
    return c


def record(job_id: str, source: str, ftype: str, severity: str = "info",
           quote: str = "", pos_pct: Optional[float] = None,
           data: Optional[Dict] = None, db_path: Optional[str] = None) -> bool:
    """Одна находка. Принцип «находка без улики не существует»: для типов вне
    _QUOTELESS_OK пустая цитата → запись отбрасывается (return False)."""
    if ftype not in _QUOTELESS_OK and not (quote or "").strip():
        return False
    try:
        with closing(_conn(db_path)) as c, c:
            c.execute(
                "INSERT INTO qa_findings (job_id, created_at, source, ftype, severity,"
                " quote, pos_pct, data) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, time.time(), source, ftype, severity,
                 (quote or "")[:300], pos_pct,
                 json.dumps(data, ensure_ascii=False) if data else None),
            )
        return True
    except Exception:
        return False


# ── детерминированные чеки [A]-добор ─────────────────────────────────────────

# палевный класс: закавыченный спан ≥5 слов рядом с глаголом цитирования —
# именно так мутировала фабрикация в волне 3 (дописанные «дословные» цитаты).
_QUOTE_SPAN = re.compile(r"[«„“\"]([^«»„“”\"]{25,240})[»“”\"]")
_CITE_VERBS = re.compile(
    r"(řekl|řekla|napsal|napsala|vzpomínal|vzpomínala|prohlásil|zapsal|hlásil"
    r"|powiedział|powiedziała|napisał|napisała|wspominał|wspominała|zeznał|oświadczył"
    r"|a spus|a scris|a declarat|a mărturisit|își amintea|nota|raporta"
    r"|сказал|сказала|написал|написала|вспоминал|вспоминала|заявил|признался"
    r"|said|wrote|recalled|testified)", re.IGNORECASE)


def check_fabrication_risk(text: str) -> List[Dict]:
    """Флаги «проверить цитату руками» — НЕ автофикс. Улика = сама цитата."""
    out = []
    total = max(1, len(text))
    for m in _QUOTE_SPAN.finditer(text):
        span = m.group(1)
        if len(span.split()) < 5:
            continue
        ctx = text[max(0, m.start() - 60): m.end() + 60]
        if _CITE_VERBS.search(ctx):
            out.append({"quote": span[:200], "pos_pct": round(m.start() * 100.0 / total, 1)})
        if len(out) >= 12:
            break
    return out


_SENT_END = ("...", ".", "!", "?", "…", "»", "“", "\"", ")")


def check_truncated(text: str) -> Optional[Dict]:
    """Обрыв: последний абзац не закрыт пунктуацией (B_2-класс: 'кончился на Поповой')."""
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    if not paras:
        return None
    last = paras[-1]
    if not last.endswith(_SENT_END):
        # ловит и обрыв на полуслове, и битые огрызки типа «pr,»
        return {"quote": last[-120:], "pos_pct": 100.0}
    return None


def check_length(text: str, target_words: int, tol: float = 0.12) -> Optional[Dict]:
    """Отклонение длины от таргета сверх tol (длина неприкосновенна — приёмка ww2)."""
    if not target_words:
        return None
    w = len((text or "").split())
    dev = (w - target_words) / float(target_words)
    if abs(dev) > tol:
        return {"words": w, "target": target_words, "dev_pct": round(dev * 100, 1)}
    return None


# ── инжест всех источников одной джобы ───────────────────────────────────────

def ingest_job(job_id: str, text: str = "", judge_report: Optional[Dict] = None,
               cleaner_lines: Optional[List[str]] = None, target_words: int = 0,
               db_path: Optional[str] = None) -> int:
    """Пишет все находки джобы. Возвращает число записанных строк. Non-fatal."""
    n = 0
    try:
        # 1) судья
        r = judge_report or {}
        if r:
            n += record(job_id, "judge", "score", "info",
                        data={"score": r.get("score_of_90"), "verdict": r.get("verdict"),
                              "mode": r.get("mode"), "breakdown": r.get("breakdown"),
                              "payoff_pct": r.get("payoff_pct")}, db_path=db_path)
            for gname, g in (r.get("gates_before") or {}).items():
                st = g.get("status")
                if st == "fail":
                    n += record(job_id, "judge", "gate_fail", "warn",
                                quote=f"{gname}: {json.dumps(g, ensure_ascii=False)[:200]}",
                                pos_pct=g.get("pct"), data={"gate": gname}, db_path=db_path)
                elif st == "unknown":
                    n += record(job_id, "judge", "gate_unknown", "info",
                                data={"gate": gname}, db_path=db_path)
            for d in (r.get("planned_deletes") or []):
                n += record(job_id, "judge", "planned_delete", "info",
                            quote=str(d.get("reason", ""))[:200],
                            data={"paras": d.get("paras"), "class": d.get("class"),
                                  "words": d.get("words")}, db_path=db_path)
            for e in (r.get("escalate") or []):
                n += record(job_id, "judge", "escalate", "warn",
                            quote=str(e.get("quote", ""))[:200],
                            data={"type": e.get("type")}, db_path=db_path)
            ap = r.get("applied") or {}
            if ap.get("applied"):
                n += record(job_id, "judge", "edit_applied", "info", data=ap, db_path=db_path)

        # 2) cleaner: ФЛАГ/WARN — находки; авто-правки — info
        for line in (cleaner_lines or []):
            if "ФЛАГ" in line:
                n += record(job_id, "cleaner", "flag", "warn", quote=line[:300], db_path=db_path)
            elif "WARN" in line:
                n += record(job_id, "cleaner", "warn", "warn", quote=line[:300], db_path=db_path)

        # 3) детерминированные чеки
        if text:
            for f in check_fabrication_risk(text):
                n += record(job_id, "detcheck", "fabrication_risk", "warn",
                            quote=f["quote"], pos_pct=f["pos_pct"], db_path=db_path)
            tr = check_truncated(text)
            if tr:
                n += record(job_id, "detcheck", "truncated", "block",
                            quote=tr["quote"], pos_pct=tr["pos_pct"], db_path=db_path)
            ld = check_length(text, target_words)
            if ld:
                n += record(job_id, "detcheck", "length_dev", "warn", data=ld, db_path=db_path)
    except Exception:
        pass
    return n


# ── агрегация «системные дефекты за волну» ───────────────────────────────────

def aggregate(days: float = 7.0, db_path: Optional[str] = None) -> str:
    since = time.time() - days * 86400
    lines = [f"=== QA-агрегат за {days:g} дн. ==="]
    try:
        with closing(_conn(db_path)) as c, c:
            jobs = c.execute("SELECT COUNT(DISTINCT job_id) FROM qa_findings WHERE created_at>?",
                             (since,)).fetchone()[0]
            lines.append(f"джоб с находками: {jobs}")
            rows = c.execute(
                "SELECT source, ftype, severity, COUNT(*) FROM qa_findings WHERE created_at>?"
                " GROUP BY source, ftype, severity ORDER BY COUNT(*) DESC", (since,)).fetchall()
            lines.append(f"{'источник':<10}{'тип':<18}{'sev':<6}{'шт':>4}  доля джоб")
            for src, ft, sev, cnt in rows:
                jn = c.execute(
                    "SELECT COUNT(DISTINCT job_id) FROM qa_findings"
                    " WHERE created_at>? AND source=? AND ftype=?", (since, src, ft)).fetchone()[0]
                share = f"{jn}/{jobs}" if jobs else "-"
                lines.append(f"{src:<10}{ft:<18}{sev:<6}{cnt:>4}  {share}")
            lines.append("")
            lines.append("— топ-улики повторяющихся типов (gate_fail/escalate/warn) —")
            for src, ft in (("judge", "gate_fail"), ("judge", "escalate"),
                            ("cleaner", "warn"), ("detcheck", "fabrication_risk")):
                ex = c.execute(
                    "SELECT quote FROM qa_findings WHERE created_at>? AND source=? AND ftype=?"
                    " ORDER BY id DESC LIMIT 3", (since, src, ft)).fetchall()
                for (q,) in ex:
                    if q:
                        lines.append(f"  [{ft}] {q[:110]}")
            scores = [json.loads(d)["score"] for (d,) in c.execute(
                "SELECT data FROM qa_findings WHERE created_at>? AND ftype='score'",
                (since,)).fetchall() if d and json.loads(d).get("score") is not None]
            if scores:
                scores.sort()
                lines.append("")
                lines.append(f"судья-баллы: n={len(scores)}, медиана={scores[len(scores)//2]}, "
                             f"min={scores[0]}, max={scores[-1]}")
    except Exception as e:
        lines.append(f"aggregate error: {e}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "aggregate":
        days = 7.0
        if "--since" in sys.argv:
            days = float(sys.argv[sys.argv.index("--since") + 1])
        print(aggregate(days))
    else:
        print(__doc__)
