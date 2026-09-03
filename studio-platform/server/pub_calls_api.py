#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обмен движка с журналом платных вызовов.

Движки — подпроцессы кабинета на той же машине, и у их задачи уже есть секрет:
`queue_tasks.claim_token`. Отдельный общий токен, как у ноды заливки, здесь не нужен и
вреден: общий секрет позволил бы одному движку писать в журнал чужого проекта.

Поэтому движок предъявляет НОМЕР ЗАДАЧИ и ЕЁ токен, а `tenant_id` и `project_id` сервер
берёт из строки очереди сам. Движок называет только то, что знает про свой вызов: вид,
единицу (номер сцены, язык обложки) и отпечаток входа. Испортить чужой журнал он не может
даже при ошибке в собственном коде.

Токен передаётся движку переменными окружения, а не argv: argv видно в `ps` любому
пользователю машины.

Роуты:
    POST /api/calls/reserve          заявить вызов ДО обращения к провайдеру
    POST /api/calls/{id}/ordered     заказ создан, известен его идентификатор
    POST /api/calls/{id}/delivered   результат получен и лежит на диске
    POST /api/calls/{id}/failed      вызов не удался (ordered решает, можно ли повторять)
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

import pub_calls as pc


def register_calls_routes(app, ctx):
    db = ctx["db"]
    _now = ctx["_now"]

    def _job(request: Request, body: dict):
        """Задача, от имени которой пишет движок. Возвращает строку queue_tasks.

        Проверяется пара (номер задачи, её claim_token) — тот же секрет, которым задача
        уже защищена от чужого heartbeat. Задача должна быть ЖИВОЙ: писать в журнал от
        имени завершённой задачи незачем, а вот перепутать проект так можно.
        """
        try:
            jid = int(body.get("job_id") or 0)
        except (TypeError, ValueError):
            jid = 0
        token = request.headers.get("X-Job-Token", "") or str(body.get("job_token") or "")
        if jid <= 0 or not token:
            raise HTTPException(400, "нужны job_id и токен задачи")
        c = db()
        try:
            row = c.execute("SELECT * FROM queue_tasks WHERE job_id=?", (jid,)).fetchone()
        finally:
            c.close()
        if row is None:
            raise HTTPException(404, "нет такой задачи")
        want = row["claim_token"] or ""
        if not want or not secrets.compare_digest(str(token), str(want)):
            raise HTTPException(403, "чужой или устаревший токен задачи")
        if row["status"] != "running":
            raise HTTPException(409, "задача больше не выполняется")
        return row

    def _call_of(c, call_id: int, job):
        """Строка журнала, принадлежащая проекту этой задачи.

        Проверка project_id обязательна: без неё движок с валидным токеном своей задачи
        мог бы закрыть чужой вызов по угаданному id.
        """
        row = c.execute("SELECT * FROM provider_calls WHERE id=?", (int(call_id),)).fetchone()
        if row is None:
            raise HTTPException(404, "нет такого вызова")
        if row["project_id"] != job["project_id"]:
            raise HTTPException(403, "вызов принадлежит другому проекту")
        return row

    @app.post("/api/calls/reserve")
    async def calls_reserve(request: Request):
        body = await request.json()
        job = _job(request, body)
        unit = str(body.get("unit") or "").strip()[:120]
        fp = str(body.get("fingerprint") or "").strip()[:128]
        if not unit or not fp:
            raise HTTPException(400, "нужны unit и fingerprint")
        kind = str(body.get("kind") or job["kind"] or "").strip()[:32]
        c = db()
        try:
            rel = c.execute("SELECT release_id FROM pub_releases WHERE project_id=?",
                            (job["project_id"],)).fetchone()
            res = pc.reserve(
                c,
                tenant_id=job["tenant_id"],
                project_id=job["project_id"],
                release_id=(rel["release_id"] if rel else ""),
                kind=kind,
                unit=unit,
                fp=fp,
                provider=str(body.get("provider") or job["provider"] or "")[:40],
                model=str(body.get("model") or "")[:60],
                now=_now(),
            )
            c.commit()
        finally:
            c.close()
        row = res["row"]
        return {"action": res["action"], "status": res["status"], "call_id": res["call_id"],
                "remote_order_id": row["remote_order_id"], "result_path": row["result_path"],
                "attempts": row["attempts"]}

    @app.post("/api/calls/{call_id}/ordered")
    async def calls_ordered(call_id: int, request: Request):
        body = await request.json()
        job = _job(request, body)
        order = str(body.get("remote_order_id") or "").strip()[:120]
        if not order:
            raise HTTPException(400, "нужен remote_order_id")
        c = db()
        try:
            _call_of(c, call_id, job)
            pc.mark_ordered(c, call_id, order, now=_now())
            c.commit()
        finally:
            c.close()
        return {"ok": True}

    @app.post("/api/calls/{call_id}/delivered")
    async def calls_delivered(call_id: int, request: Request):
        body = await request.json()
        job = _job(request, body)
        path = str(body.get("result_path") or "").strip()[:400]
        if not path:
            raise HTTPException(400, "нужен result_path: «готово» доказывается файлом")
        cost = body.get("cost")
        c = db()
        try:
            _call_of(c, call_id, job)
            pc.mark_delivered(c, call_id, path,
                              cost=(float(cost) if cost not in (None, "") else None),
                              now=_now())
            c.commit()
        finally:
            c.close()
        return {"ok": True}

    @app.post("/api/calls/{call_id}/failed")
    async def calls_failed(call_id: int, request: Request):
        body = await request.json()
        job = _job(request, body)
        c = db()
        try:
            _call_of(c, call_id, job)
            pc.mark_failed(c, call_id, str(body.get("error") or "")[:500],
                           ordered=bool(body.get("ordered")), now=_now())
            c.commit()
        finally:
            c.close()
        return {"ok": True}
