# -*- coding: utf-8 -*-
"""Единая таблица каналов и аккаунтов: приём листов из Google-таблицы и выдача ноде Минска.

Схема (22.10.2026):
  Google-таблица ──Apps Script (push, X-Table-Token)──▶ VPS ──pull (X-Upload-Token)──▶ Минск
  Минск: table_import.py (план → предохранители → запись книг) ──report──▶ VPS ──▶ Telegram

Зачем так, а не читать таблицу с Минска напрямую: для прямого чтения нужен сервисный
аккаунт Google (проект, ключ, доступ к таблице), а скрипт внутри самой таблицы отправляет
листы сам, под правами того, кто его поставил. VPS здесь — почтовый ящик: хранит последний
снимок, отдаёт его ноде и пересказывает её отчёт в Telegram.

Хранение — файлы рядом с vsgen.db: `<data>/table_sync/incoming/<штамп>-<sha8>/{channels,accounts}.csv`,
`current.json` (что лежит сейчас и что нода с этим сделала), `reports/`. Секретов тут нет:
в таблице почты, профили, роли — пароли живут только в книгах на Минске.
"""
import hashlib
import json
import os
import re
import threading
import time

LIMIT_BYTES = 2_000_000          # один лист больше 2 МБ — это не таблица каналов
KEEP = 40                         # сколько снимков держим в incoming/
NOTIFY_STATUSES = {"applied", "blocked", "error", "plan"}


def sync_dir(base: str = "") -> str:
    base = base or os.path.dirname(os.environ.get("VS_SERVER_DB") or "/srv/studio/data/vsgen.db")
    d = os.path.join(base, "table_sync")
    for sub in ("", "incoming", "reports"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def _header(csv_text: str) -> str:
    return (csv_text or "").lstrip("﻿").split("\n", 1)[0].lower()


def classify(sheets: dict) -> dict:
    """Какой лист — каналы, какой — аккаунты. По ключу (channels/accounts), иначе по шапке:
    лист с UC — каналы, лист, где первый столбец «почта», — аккаунты. Имена листов не важны."""
    out = {}
    for key, text in (sheets or {}).items():
        if not isinstance(text, str):
            continue
        k = str(key).strip().lower()
        if k in ("channels", "каналы"):
            out["channels"] = text
        elif k in ("accounts", "аккаунты"):
            out["accounts"] = text
    for key, text in (sheets or {}).items():
        if not isinstance(text, str):
            continue
        h = _header(text)
        if "channels" not in out and re.search(r"(^|[;,])\s*uc\s*([;,]|$)", h):
            out["channels"] = text
        elif "accounts" not in out and re.match(r"^\s*\"?почта", h) and "uc" not in h:
            out["accounts"] = text
    return out


def _rows(csv_text: str) -> int:
    return max(0, sum(1 for line in (csv_text or "").splitlines() if line.strip()) - 1)


def _sha(channels: str, accounts: str) -> str:
    return hashlib.sha256((channels + "\n---\n" + accounts).encode("utf-8")).hexdigest()


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def current(d: str, with_text: bool = False) -> dict:
    cur = _read(os.path.join(d, "current.json")) or {}
    if with_text and cur.get("dir"):
        for name in ("channels", "accounts"):
            try:
                with open(os.path.join(cur["dir"], f"{name}.csv"), encoding="utf-8") as f:
                    cur[name] = f.read()
            except OSError:
                cur[name] = ""
    return cur


def ingest_push(d: str, sheets: dict, source: str = "", now: float = None) -> dict:
    """Принять листы. Тот же текст, что лежит, — ничего не пишем (`changed: false`)."""
    now = now or time.time()
    got = classify(sheets)
    missing = [k for k in ("channels", "accounts") if k not in got]
    if missing:
        raise ValueError("не нашёл лист: " + ", ".join("каналы" if m == "channels" else "аккаунты" for m in missing)
                         + " (нужен лист с колонкой UC и лист с первой колонкой «Почта»)")
    for k in ("channels", "accounts"):
        if len(got[k].encode("utf-8")) > LIMIT_BYTES:
            raise ValueError(f"лист {k}: больше {LIMIT_BYTES // 1_000_000} МБ")
    if _rows(got["channels"]) == 0:
        raise ValueError("лист каналов пустой (одна шапка) — не принимаю")
    sha = _sha(got["channels"], got["accounts"])
    cur = current(d)
    if cur.get("sha") == sha:
        cur["seen"] = now
        _write(os.path.join(d, "current.json"), cur)
        return {"changed": False, "sha": sha, "rows": cur.get("rows", {})}
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    sub = os.path.join(d, "incoming", f"{stamp}-{sha[:8]}")
    os.makedirs(sub, exist_ok=True)
    for k in ("channels", "accounts"):
        with open(os.path.join(sub, f"{k}.csv"), "w", encoding="utf-8") as f:
            f.write(got[k])
    rows = {"channels": _rows(got["channels"]), "accounts": _rows(got["accounts"])}
    _write(os.path.join(sub, "meta.json"), {"sha": sha, "at": now, "source": source, "rows": rows})
    new = {"sha": sha, "stamp": stamp, "at": now, "seen": now, "source": source, "rows": rows,
           "dir": sub, "report": None, "notified": cur.get("notified")}
    _write(os.path.join(d, "current.json"), new)
    _prune(os.path.join(d, "incoming"))
    return {"changed": True, "sha": sha, "rows": rows}


def _prune(incoming: str) -> None:
    try:
        subs = sorted(x for x in os.listdir(incoming) if os.path.isdir(os.path.join(incoming, x)))
    except OSError:
        return
    for old in subs[:-KEEP]:
        p = os.path.join(incoming, old)
        for name in os.listdir(p):
            try:
                os.remove(os.path.join(p, name))
            except OSError:
                pass
        try:
            os.rmdir(p)
        except OSError:
            pass


STATUS_WORDS = {
    "applied": "записано в книги",
    "plan": "есть изменения, запись выключена — ждёт включения",
    "blocked": "НЕ записано, нужен человек",
    "error": "ошибка записи",
    "same": "без изменений",
}


def record_report(d: str, body: dict, now: float = None) -> dict:
    """Отчёт ноды о снимке: статус, сводка, строки плана. Возвращает текст для Telegram,
    если об этом снимке с таким исходом ещё не сообщали."""
    now = now or time.time()
    status = str(body.get("status") or "").strip()
    if status not in STATUS_WORDS:
        raise ValueError(f"неизвестный статус «{status}»")
    sha = str(body.get("sha") or "")
    lines = [str(x) for x in (body.get("lines") or [])][:60]
    summary = str(body.get("summary") or "").strip()
    cur = current(d)
    rec = {"sha": sha, "status": status, "summary": summary, "lines": lines, "at": now,
           "host": str(body.get("host") or ""), "for_current": (sha == cur.get("sha"))}
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    _write(os.path.join(d, "reports", f"{stamp}-{status}.json"), rec)
    text = ""
    if rec["for_current"]:
        cur["report"] = {"status": status, "summary": summary, "at": now, "lines": lines[:20]}
        key = f"{sha}:{status}:{summary}"
        if status in NOTIFY_STATUSES and cur.get("notified") != key:
            cur["notified"] = key
            text = tg_text(cur, rec)
        _write(os.path.join(d, "current.json"), cur)
    return {"ok": True, "notify": bool(text), "text": text}


def tg_text(cur: dict, rec: dict) -> str:
    rows = cur.get("rows") or {}
    head = f"Единая таблица: {STATUS_WORDS.get(rec['status'], rec['status'])}"
    if rec.get("summary"):
        head += f" — {rec['summary']}"
    body = [head, f"снимок {cur.get('stamp', '?')}, каналов {rows.get('channels', '?')}, аккаунтов {rows.get('accounts', '?')}"]
    shown = [l for l in rec.get("lines") or [] if l.strip()][:20]
    body += ["  " + l.strip() for l in shown]
    if len(rec.get("lines") or []) > len(shown):
        body.append(f"  … ещё {len(rec['lines']) - len(shown)}")
    return "\n".join(body)[:3900]


def register_table_sync_routes(app, ctx):
    """`/studio/api/table-sync/{push,pull,report,status}` (снаружи; внутри пульта без /studio).

    push — из Google-таблицы (Apps Script), свой токен `X-Table-Token`: он лежит в чужой
    таблице и не должен открывать ничего, кроме приёма листов. pull/report/status — токен
    ноды заливки `X-Upload-Token`: это та же машина Минска.
    """
    import secrets as _secrets
    from fastapi import HTTPException, Request

    db = ctx["db"]                                   # noqa: F841 — единообразие с соседями
    upload_token = ctx["upload_token"]
    table_token = ctx["table_token"]
    log = ctx.get("log") or (lambda *a: None)
    base_dir = ctx.get("data_dir") or ""
    tg = ctx.get("tg")                               # объект с .send(text) или None

    def _same(got: str, want: str) -> bool:
        if not want or not got:
            return False
        try:
            return _secrets.compare_digest(got.encode("utf-8"), want.encode("utf-8"))
        except (UnicodeError, TypeError):            # noqa: BLE001
            return False

    def _need_node(request):
        if not _same(request.headers.get("X-Upload-Token", ""), upload_token()):
            raise HTTPException(401, "нужен токен ноды заливки")

    def _need_table(request):
        if not _same(request.headers.get("X-Table-Token", ""), table_token()):
            raise HTTPException(401, "нужен токен таблицы")

    def _notify(text: str) -> None:
        if not text or tg is None:
            return

        def run():
            try:
                tg.send(text)
            except Exception as exc:                 # noqa: BLE001
                log(f"единая таблица: телеграм не принял: {exc}")
        threading.Thread(target=run, daemon=True).start()

    async def api_push(request):
        _need_table(request)
        raw = await request.body()
        if len(raw) > 2 * LIMIT_BYTES:
            raise HTTPException(413, "слишком большой запрос")
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise HTTPException(400, "ожидаю JSON {sheets: {…}}")
        try:
            out = ingest_push(sync_dir(base_dir), body.get("sheets") or {}, str(body.get("source") or ""))
        except ValueError as exc:
            log(f"единая таблица: push отклонён: {exc}")
            raise HTTPException(400, str(exc))
        if out["changed"]:
            log(f"единая таблица: новый снимок {out['sha'][:8]}: каналов {out['rows']['channels']}, "
                f"аккаунтов {out['rows']['accounts']}")
        return out

    async def api_pull(request):
        _need_node(request)
        cur = current(sync_dir(base_dir), with_text=(request.query_params.get("meta") != "1"))
        cur.pop("dir", None)
        return cur

    async def api_report(request):
        _need_node(request)
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except ValueError:
            raise HTTPException(400, "ожидаю JSON")
        try:
            out = record_report(sync_dir(base_dir), body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        log(f"единая таблица: отчёт ноды — {body.get('status')}: {str(body.get('summary') or '')[:120]}")
        _notify(out.get("text", ""))
        return {"ok": True, "notified": out["notify"]}

    async def api_status(request):
        if not (_same(request.headers.get("X-Upload-Token", ""), upload_token())
                or _same(request.headers.get("X-Table-Token", ""), table_token())):
            raise HTTPException(401, "нужен токен")
        cur = current(sync_dir(base_dir))
        cur.pop("dir", None)
        return cur

    api_push.__annotations__ = {"request": Request}
    api_pull.__annotations__ = {"request": Request}
    api_report.__annotations__ = {"request": Request}
    api_status.__annotations__ = {"request": Request}
    # Пульт смонтирован под /studio со срезанием префикса (web/platform.py), поэтому
    # снаружи это /studio/api/table-sync/…, а здесь — без /studio.
    app.post("/api/table-sync/push")(api_push)
    app.get("/api/table-sync/pull")(api_pull)
    app.post("/api/table-sync/report")(api_report)
    app.get("/api/table-sync/status")(api_status)
