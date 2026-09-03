#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Клиент журнала платных вызовов для движков.

Только stdlib: движки запускаются в разных окружениях (VPS, минская нода, ноутбук
разработчика), и новая зависимость ради журнала обошлась бы дороже самого журнала.

**Не настроен — значит выключен.** Если в окружении нет VS_CALLS_URL, VS_JOB_ID и
VS_JOB_TOKEN, все функции возвращают «делай как раньше», и движок ведёт себя ровно как
до подключения журнала. Это важно: движки сегодня запускают и руками из командной
строки, и такой запуск не должен требовать поднятого кабинета.

Ошибка сети тоже не останавливает работу — но и не выдаёт разрешение платить повторно:
при недоступном кабинете reserve() возвращает `unknown`, и вызывающий сам решает, что
безопаснее. Для платных вызовов безопаснее пропустить кадр, чем оплатить дважды.

Порядок использования внутри движка:

    j = CallJournal()
    d = j.reserve(unit="007", fingerprint=j.fingerprint(model=m, prompt=p, seed=s))
    if d.action == "skip":       # результат уже есть
        return d.result_path
    if d.action == "verify":     # заказ создавался — спросить провайдера, НЕ создавать
        ...
    order = provider.create(...)
    j.ordered(d.call_id, order)
    ...
    j.delivered(d.call_id, path, cost=4.0)
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request

TIMEOUT = 10

# Решения, как их отдаёт кабинет.
NEW = "new"
SKIP = "skip"
VERIFY = "verify"
RETRY = "retry"
UNKNOWN = "unknown"          # кабинет недоступен: решение принимает вызывающий
OFF = "off"                  # журнал не настроен: вести себя как раньше


class Decision:
    __slots__ = ("action", "call_id", "remote_order_id", "result_path", "attempts", "error")

    def __init__(self, action, call_id=0, remote_order_id="", result_path="",
                 attempts=0, error=""):
        self.action = action
        self.call_id = call_id
        self.remote_order_id = remote_order_id
        self.result_path = result_path
        self.attempts = attempts
        self.error = error

    @property
    def may_call(self) -> bool:
        """Можно ли обращаться к провайдеру.

        `off` — да: журнала нет, поведение прежнее. `unknown` — НЕТ: кабинет не ответил,
        и мы не знаем, не оплачен ли этот вызов уже.
        """
        return self.action in (NEW, RETRY, OFF)

    def __repr__(self):
        return f"<Decision {self.action} call_id={self.call_id}>"


class CallJournal:
    def __init__(self, base=None, job_id=None, job_token=None, log=None):
        self.base = (base or os.environ.get("VS_CALLS_URL") or "").rstrip("/")
        self.job_id = job_id or os.environ.get("VS_JOB_ID") or ""
        self.token = job_token or os.environ.get("VS_JOB_TOKEN") or ""
        self.log = log or (lambda *_a, **_k: None)

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.job_id and self.token)

    @staticmethod
    def fingerprint(**parts) -> str:
        """Отпечаток входа. Совпадает с pub_calls.fingerprint — правила общие.

        Всё, что влияет на результат и цену, кладём сюда; что не влияет — не кладём,
        иначе один и тот же кадр будет оплачен заново.
        """
        clean = {k: v for k, v in parts.items() if v not in (None, "", [], {})}
        blob = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _post(self, path: str, payload: dict):
        body = dict(payload)
        body["job_id"] = self.job_id
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.base}{path}", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Job-Token", self.token)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def reserve(self, *, unit, fingerprint, kind="", provider="", model="") -> Decision:
        if not self.enabled:
            return Decision(OFF)
        try:
            r = self._post("/api/calls/reserve", {
                "unit": str(unit), "fingerprint": fingerprint,
                "kind": kind, "provider": provider, "model": model})
        except urllib.error.HTTPError as exc:
            self.log(f"[журнал] заявка отклонена: HTTP {exc.code}")
            return Decision(UNKNOWN, error=f"HTTP {exc.code}")
        except Exception as exc:                                   # noqa: BLE001
            self.log(f"[журнал] кабинет недоступен: {type(exc).__name__}")
            return Decision(UNKNOWN, error=type(exc).__name__)
        return Decision(r.get("action") or UNKNOWN, r.get("call_id") or 0,
                        r.get("remote_order_id") or "", r.get("result_path") or "",
                        r.get("attempts") or 0)

    def ordered(self, call_id, remote_order_id) -> bool:
        """Заказ создан. Записать НАДО до того, как ждать результат."""
        return self._quiet(call_id, "ordered", {"remote_order_id": str(remote_order_id)})

    def delivered(self, call_id, result_path, cost=None) -> bool:
        return self._quiet(call_id, "delivered",
                           {"result_path": str(result_path), "cost": cost})

    def failed(self, call_id, error, ordered=False) -> bool:
        """`ordered=True` — заказ уже был создан: строка останется «спросить провайдера».

        `ordered=False` — провайдер отказал ДО создания заказа (явный 429, отказ
        валидации): денег нет, повтор безопасен.
        """
        return self._quiet(call_id, "failed",
                           {"error": str(error)[:500], "ordered": bool(ordered)})

    def _quiet(self, call_id, verb, payload) -> bool:
        if not self.enabled or not call_id:
            return False
        try:
            self._post(f"/api/calls/{int(call_id)}/{verb}", payload)
            return True
        except Exception as exc:                                   # noqa: BLE001
            # Отчёт не доехал — работу не останавливаем: строка останется незакрытой,
            # и её подберёт разбор незавершённых после прогона. Потерять запись хуже,
            # чем потерять кадр, но остановить прогон из-за журнала — ещё хуже.
            self.log(f"[журнал] отчёт «{verb}» не записан: {type(exc).__name__}")
            return False
