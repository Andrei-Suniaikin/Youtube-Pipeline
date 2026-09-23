#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обмен с машиной, которая заливает: кабинет отдаёт задание, нода приносит вердикт.

Сделано по образцу рендер-ноды (исходящий опрос, свой токен, клейм с проверкой), но с
четырьмя отличиями — каждое закрывает способ получить второй ролик на канале:

1. **Истёкшая аренда ведёт в карантин, а не назад в очередь.** У рендера молчание ноды
   возвращает задачу в очередь: для рендера это безопасно, для заливки — второй ролик.
2. **Один профиль — одна заливка. Разные профили — параллельно.** Задание по профилю не
   выдаётся, пока по нему идёт заливка: два мастера Studio в одном профиле ломают друг
   друга (диалог отдаёт поля нулевого размера, воркер падает с пустой ошибкой), а тот же
   профиль на второй машине рвёт куки. Но профили независимы, поэтому свободные выдаются
   одновременно — сколько у ноды рабочих мест, столько роликов и льётся.
   Тот же канал тоже исключается: два мастера на одном канале путают Studio.
3. **Повторный запрос возвращает ТОТ ЖЕ токен.** Если ответ не доехал до ноды (перезапуск
   кабинета, 502), нода спросит снова и получит своё же задание, а не второе.
4. **Вердикт разбирается по evidence, а не по коду возврата.** «Стоит в расписании» ставится
   только при совпадении канала, video ID, времени и признака SCHEDULED.
"""

from __future__ import annotations

import json
import os
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from pub_log import add as log_line, add_lines

# Сколько ждать вестей от ноды, прежде чем считать заливку зависшей.
# С запасом на самый долгий наблюдавшийся мастер Studio (~9 минут на больном прокси).
UPLOAD_LEASE_SEC = 45 * 60

# Успешные исходы Studio. Всё остальное — не успех, даже если video_id получен.
OK_STATES = ("SCHEDULED",)
PUBLISHED_STATES = ("PUBLIC",)


def register_upload_routes(app, ctx):
    db = ctx["db"]
    _now = ctx["_now"]
    token_of = ctx["upload_token"]          # общий секрет ноды заливки
    log = ctx.get("log") or (lambda *a: None)

    def _same_secret(got: str, want: str) -> bool:
        """Сравнение секретов постоянного времени, устойчивое к мусору в заголовке.

        23.09.2026, найдено на боевой выкатке: `secrets.compare_digest` на строках с
        не-ASCII бросает TypeError, и нода, приславшая мятый заголовок, получала 500
        «внутренняя ошибка» вместо честного «не пущу». Пятисотка тут вреднее отказа:
        она выглядит как поломка сервера и уводит разбор не туда.
        """
        if not want or not got:
            return False
        try:
            return secrets.compare_digest(got.encode("utf-8"), want.encode("utf-8"))
        except (UnicodeError, TypeError):                          # noqa: BLE001
            return False

    def _auth(request: Request) -> None:
        if not _same_secret(request.headers.get("X-Upload-Token", ""), token_of()):
            raise HTTPException(401, "нужен токен ноды заливки")

    def _own(request: Request, rel) -> None:
        if not _same_secret(request.headers.get("X-Upload-Claim", ""),
                            rel["claim_token"] or ""):
            raise HTTPException(403, "чужой или устаревший клейм")

    def _release(c, release_id: str):
        return c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?",
                         (release_id,)).fetchone()

    # ── взять задание ──
    @app.post("/api/upload/claim")
    async def upload_claim(request: Request):
        _auth(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        host = str(body.get("host") or "").strip()[:40] or "unknown"
        # 04.09.2026: машина заливки сообщает своё свободное место тем же опросом —
        # см. pub_calls.note_node_disk. Старый пуллер его не шлёт, и это не ошибка.
        if body.get("free_gb") is not None:
            try:
                from pub_calls import note_node_disk
                _c = db()
                try:
                    note_node_disk(_c, host, "upload", body.get("free_gb"),
                                   body.get("total_gb"))
                    _c.commit()
                finally:
                    _c.close()
            except Exception:                                      # noqa: BLE001
                pass
        # Рабочее место ноды. Без него одна машина смогла бы держать только одну заливку,
        # и правило «разные профили льются параллельно» не работало бы.
        worker = str(body.get("worker") or "1").strip()[:20]
        now = _now()

        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            # (3) Повторный запрос той же ноды — возвращаем её же задание с тем же токеном.
            mine = c.execute(
                "SELECT * FROM pub_upload_jobs WHERE status='running' AND host=? AND worker=? "
                "ORDER BY claimed LIMIT 1", (host, worker)).fetchone()
            if mine is not None:
                c.commit()
                return JSONResponse(_task_payload(c, mine, repeat=True))

            # (2) Профили, по которым сейчас уже льют, — исключаем целиком.
            running = c.execute(
                "SELECT profile_id, uc FROM pub_upload_jobs WHERE status='running'").fetchall()
            busy_profiles = {r["profile_id"] for r in running if r["profile_id"] is not None}
            busy_channels = {r["uc"] for r in running}
            # Только задания ЖИВЫХ выпусков: отменённый или карантинный выпуск мог оставить
            # своё задание в очереди — заливать его значит заливать ролик без хозяина.
            rows = c.execute(
                "SELECT u.* FROM pub_upload_jobs u JOIN pub_releases r "
                "ON r.release_id=u.release_id AND r.status IN ('pending','running') "
                "WHERE u.status='queued' ORDER BY u.slot_at, u.id").fetchall()
            task = None
            for r in rows:
                # Задание могло состариться в очереди: запланировать прошедший момент
                # нельзя, отсеиваем ДО того, как нода начнёт качать файл.
                if r["schedule_iso"] and r["slot_at"] <= now + 1800:
                    c.execute("UPDATE pub_upload_jobs SET status='error', error=?, "
                              "updated=? WHERE id=? AND status='queued'",
                              ("к времени выхода не успели — задание состарилось в очереди",
                               now, r["id"]))
                    log_line(c, r["release_id"], "заливка",
                             "задание состарилось в очереди — время выхода прошло",
                             level="error", now=now)
                    continue
                if r["profile_id"] not in busy_profiles and r["uc"] not in busy_channels:
                    task = r
                    break
            if task is None:
                c.commit()
                return JSONResponse({"task": None})

            token = "upc_" + secrets.token_urlsafe(18)
            done = c.execute(
                "UPDATE pub_upload_jobs SET status='running', claim_token=?, claimed=?, "
                "host=?, worker=?, lease_until=?, updated=? WHERE id=? AND status='queued'",
                (token, now, host, worker, now + UPLOAD_LEASE_SEC, now, task["id"]))
            if done.rowcount != 1:
                c.commit()                        # кто-то успел раньше — пусть спросит снова
                return JSONResponse({"task": None})
            c.commit()
            fresh = c.execute("SELECT * FROM pub_upload_jobs WHERE id=?", (task["id"],)).fetchone()
            log_line(c, fresh["release_id"], "заливка",
                     f"выдал задание машине {host}, место {worker}, профиль "
                     f"{fresh['profile_id']}, выход {fresh['schedule_iso'] or 'сразу'}", now=now)
            c.commit()          # запись журнала после commit иначе теряется при закрытии
            log(f"заливка: выдал задание {fresh['release_id']} → {host}/{worker}, "
                f"профиль {fresh['profile_id']}")
            return JSONResponse(_task_payload(c, fresh))
        finally:
            c.close()

    def _task_payload(c, row, repeat: bool = False) -> dict:
        return {"task": {
            "release_id": row["release_id"],
            "claim_token": row["claim_token"],
            "profile": row["profile_id"],
            "channel_url": row["channel_url"],
            "title": row["title"],
            "description": row["description"],
            "schedule": row["schedule_iso"] or "",
            "visibility": row["visibility"] or "public",
            "collaborator": row["collaborator"] or "",
            "video_sha256": row["video_sha256"] or "",
            "repeat": bool(repeat),
        }}

    # ── забрать файлы ──
    @app.get("/api/upload/{release_id}/assets")
    def upload_assets(release_id: str, request: Request):
        _auth(request)
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            return JSONResponse({"video": rel["video_path"], "cover": rel["cover_path"] or ""})
        finally:
            c.close()

    # ── скачать файл ──
    @app.get("/api/upload/{release_id}/file")
    def upload_file(release_id: str, kind: str, request: Request):
        """Отдать ролик или обложку.

        Минская нода чистит рабочую папку после рендера (проверено: там ноль файлов),
        поэтому готовый ролик живёт на сервере и его надо привезти обратно.
        """
        _auth(request)
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            path = rel["video_path"] if kind == "video" else (rel["cover_path"] or "")
        finally:
            c.close()
        if not path or not os.path.exists(path):
            raise HTTPException(404, f"файла нет: {kind}")
        media = "video/mp4" if kind == "video" else "image/jpeg"
        return FileResponse(path, media_type=media, filename=os.path.basename(path))

    # ── биение и фазы ──
    @app.post("/api/upload/{release_id}/progress")
    async def upload_progress(release_id: str, request: Request):
        _auth(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        # Раньше здесь стояло [:40], и в базу попадал огрызок JSON вида
        # {"phase":"profile_opened","upload_phase_ — разобрать его нельзя, поэтому
        # движок не мог узнать, дошла ли машина до отправки файла, и считал любую
        # неудачу необратимой. Держим целиком: строка короткая, места не жалко.
        phase = str(body.get("phase") or "")[:400]
        now = _now()
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            c.execute("UPDATE pub_upload_jobs SET phase=?, heartbeat=?, lease_until=?, updated=? "
                      "WHERE id=?", (phase or rel["phase"], now, now + UPLOAD_LEASE_SEC, now,
                                     rel["id"]))
            # Фазу дублируем в журнал выпуска: именно по нему движок понимает, что заливка
            # уже началась, и запрещает повтор навсегда.
            if phase:
                # Машина шлёт фазу целым JSON по контракту (`upload_contract`), а сравнение
                # ниже ждало голого имени: `phase == "upload_started"` не совпадало НИКОГДА,
                # и фаза «идёт заливка» в журнал выпуска не попадала ни разу. Разбираем имя;
                # старый формат (голая строка) продолжает работать.
                said = phase
                try:
                    parsed = json.loads(phase)
                    if isinstance(parsed, dict) and parsed.get("phase"):
                        said = str(parsed["phase"])
                except (ValueError, TypeError):
                    pass
                log_line(c, release_id, "заливка", f"машина сообщает: {said}", now=now)
                stdout = str(body.get("stdout") or "")
                if stdout:
                    add_lines(c, release_id, "заливщик", stdout)
                from pub_engine import log_phase
                known = {r[0] for r in c.execute(
                    "SELECT phase FROM pub_release_events WHERE release_id=?",
                    (release_id,)).fetchall()}
                if said == "upload_started" and "upload_running" not in known:
                    log_phase(c, release_id, "upload_running", now)
            c.commit()
            return JSONResponse({"ok": True})
        finally:
            c.close()

    # ── вердикт ──
    @app.post("/api/upload/{release_id}/result")
    async def upload_result(release_id: str, request: Request):
        _auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "жду результат заливки объектом JSON")
        now = _now()
        c = db()
        try:
            rel = _release(c, release_id)
            if rel is None:
                raise HTTPException(404, "нет такого задания")
            _own(request, rel)
            verdict = read_verdict(body, expected_uc=rel["uc"] or "",
                                   expected_iso=rel["schedule_iso"] or "")
            if rel["status"] not in ("running", "error"):
                # Задание уже закрыто (двойная доставка) — не перетираем итог.
                return JSONResponse({"ok": True, "verdict": rel["verdict"] or "already"})
            if rel["status"] == "error" and verdict["kind"] not in ("scheduled", "published"):
                # Задание снято по молчанию; поздний НЕуспех ничего не меняет.
                return JSONResponse({"ok": True, "verdict": "already_error"})
            c.execute(
                "UPDATE pub_upload_jobs SET status=?, verdict=?, video_id=?, evidence=?, "
                "error=?, finished=?, updated=?, claim_token=NULL WHERE id=?",
                (verdict["state"], verdict["kind"], verdict["video_id"],
                 json.dumps(verdict["evidence"], ensure_ascii=False) if verdict["evidence"] else None,
                 verdict["error"], now, now, rel["id"]))
            if rel["status"] == "error" and verdict["kind"] in ("scheduled", "published"):
                # Поздний успех живой ноды: выпуск мог уже уехать в разбор — поднимаем его.
                c.execute("UPDATE pub_releases SET status='running', blocked_reason=NULL, "
                          "updated=? WHERE release_id=? AND status='blocked'",
                          (now, release_id))
                log_line(c, release_id, "заливка",
                         "нода домолчалась до успеха: ролик подтверждён, снимаю разбор",
                         now=now)
            level = {"scheduled": "info", "published": "info"}.get(verdict["kind"], "error")
            log_line(c, release_id, "заливка",
                     f"вердикт: {verdict['kind']}"
                     + (f", видео {verdict['video_id']}" if verdict["video_id"] else "")
                     + (f" — {verdict['error']}" if verdict["error"] else ""),
                     level=level, now=now)
            if body.get("stdout"):
                add_lines(c, release_id, "заливщик", str(body["stdout"]), level=level)
            c.commit()
            log(f"заливка: вердикт {release_id} → {verdict['kind']}")
            return JSONResponse({"ok": True, "verdict": verdict["kind"]})
        finally:
            c.close()


# ─────────────────────────── разбор вердикта ───────────────────────────

def _iso_epoch(iso: str):
    """ISO-время задания → целые секунды. Не разобралось — None (сверку пропускаем)."""
    import datetime as _dt
    try:
        return int(_dt.datetime.fromisoformat(iso).timestamp())
    except Exception:
        return None


def read_verdict(body: dict, expected_uc: str = "", expected_iso: str = "") -> dict:
    """Ответ заливщика → однозначный исход.

    Успех требует ВСЁ разом: точное подтверждение Studio (не строковое из списка),
    `success=true` от самого заливщика, совпадение канала и момента выхода с заданием.
    Любое расхождение — «нужен разбор»: ролик, скорее всего, на канале, но не там или
    не тогда, и повторная заливка запрещена.
    """
    if not isinstance(body, dict):
        return {"kind": "broken", "state": "error", "video_id": "", "evidence": None,
                "error": "ответ не объект"}
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    video_id = str(result.get("video_id") or "")
    exact = result.get("exact_studio_evidence")
    evidence = exact or result.get("studio_row_evidence")
    state = ""
    if isinstance(evidence, dict):
        state = str(evidence.get("studio_state") or "")
    error = str(result.get("error") or "")[:400]
    no_reupload = bool(result.get("do_not_reupload"))
    success = bool(result.get("success"))

    def _mismatch() -> str:
        if not isinstance(exact, dict):
            return "нет точного подтверждения Studio (только строковое)"
        if not success:
            return "заливщик сам не считает итог успехом"
        got_uc = str(exact.get("verified_channel_uc") or "")
        if expected_uc and got_uc and got_uc != expected_uc:
            return f"канал в подтверждении {got_uc} не совпал с заданием"
        want = _iso_epoch(expected_iso) if expected_iso else None
        got = exact.get("scheduled_time_seconds")
        if want is not None and isinstance(got, int) and got != want:
            return "время в подтверждении не совпало с заданием"
        return ""

    if state in OK_STATES:
        why = _mismatch()
        if why:
            return {"kind": "needs_repair", "state": "repair", "video_id": video_id,
                    "evidence": evidence, "error": f"SCHEDULED, но {why}"}
        return {"kind": "scheduled", "state": "done", "video_id": video_id,
                "evidence": evidence, "error": ""}
    if state in PUBLISHED_STATES:
        return {"kind": "published", "state": "done", "video_id": video_id,
                "evidence": evidence, "error": ""}
    if video_id or no_reupload:
        # Ролик на канале есть, но подтверждения нет: повторять заливку нельзя.
        return {"kind": "needs_repair", "state": "repair", "video_id": video_id,
                "evidence": evidence,
                "error": error or f"ролик залит, но Studio показывает «{state or 'неизвестно'}»"}
    return {"kind": "failed", "state": "error", "video_id": "", "evidence": evidence,
            "error": error or "заливка не удалась"}


def sweep_stuck(c, now: float) -> list:
    """Задания, от которых нет вестей дольше аренды, — В КАРАНТИН, не назад в очередь.

    Возврат в очередь здесь означал бы второй ролик на канале: нода могла в этот момент
    продолжать заливку, просто молча.
    """
    rows = c.execute(
        "SELECT * FROM pub_upload_jobs WHERE status='running' AND lease_until IS NOT NULL "
        "AND lease_until < ?", (now,)).fetchall()
    out = []
    for r in rows:
        # claim_token СОХРАНЯЕМ: нода могла продолжать молча и довести мастер до конца —
        # её поздний вердикт с этим клеймом обязан доехать и поднять запись до успеха.
        c.execute(
            "UPDATE pub_upload_jobs SET status='error', error=?, finished=?, updated=? "
            "WHERE id=?",
            (f"нода молчит дольше {UPLOAD_LEASE_SEC // 60} минут — нужен разбор",
             now, now, r["id"]))
        out.append(r["release_id"])
    return out
