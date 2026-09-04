#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единая платформа: один процесс, один порт, три сервиса внутри.

Было три приложения на трёх портах, каждое со своим systemd-юнитом, своим деплоем и
своим перезапуском. Здесь они собраны в одно ASGI-приложение:

    /            кабинет — весь его интерфейс и API, пути не изменились
    /studio      пульт — новый единый интерфейс
    /rewrite/*   Rewrite Studio
    /cover/*     Cover Studio

Корень пока за кабинетом, а не за пультом, и это временно. У кабинета около двадцати
экранов, у пульта шесть; отдать ему `/` сейчас значило бы объявить переезд законченным
до того, как он закончен. Корень переедет, когда переедут экраны.

**Что пришлось починить, чтобы это вообще заработало.** Три отдельных процесса скрывали
две вещи, которые в одном процессе не спрячешь:

* `sessions.db` у Rewrite и Cover резолвился от РАБОЧЕГО каталога процесса. В бою это
  прикрывал systemd (`WorkingDirectory=`), а один процесс имеет один рабочий каталог на
  всех — сервис открыл бы чужую или пустую базу. Теперь путь считается от каталога модуля.
* `auth.py` существовал трижды: у Rewrite, у Cover и у пульта. Одинаковые имена на
  общем `sys.path` — это подмена одного модуля другим в зависимости от порядка импорта,
  молча и с разными последствиями каждый запуск. Файлы разведены по именам.

**Про lifespan.** Смонтированное подприложение своих startup/shutdown не получает —
Starlette их не запускает. Здесь они запускаются явно, иначе у кабинета не поднялся бы
планировщик, у Rewrite — рабочие потоки, у Cover — очередь: сервисы отвечали бы на
запросы и не делали бы работу.

Запуск:

    dev/run.sh                       (или)
    .venv/bin/python -m uvicorn web.platform:app --port 8080
"""
from __future__ import annotations

import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

# Порядок важен: каталог пульта первым, чтобы его собственные модули не перекрывались
# одноимёнными у соседей. Имена уже разведены, но правило дешевле надежды.
for path in (HERE, os.path.join(BASE, "server"), os.path.join(BASE, "rewrite"),
             os.path.join(BASE, "cover")):
    if path not in sys.path:
        sys.path.append(path)

from fastapi import FastAPI                                       # noqa: E402
from fastapi.responses import RedirectResponse                    # noqa: E402

import main as cabinet                                            # noqa: E402
import server as rewrite                                          # noqa: E402
import cover_server as cover                                      # noqa: E402
import app as pult                                                # noqa: E402

class StripPrefix:
    """Внутри подприложения путь должен быть таким, каким он был до монтирования.

    Starlette в этой версии кладёт префикс в `root_path`, но `path` оставляет полным.
    Подприложения этого не знают: у Rewrite и Cover есть middleware, которое сравнивает
    путь со списком открытых («/auth/status», «/api/v1/…»), и после монтирования эти
    сравнения перестают совпадать — открытые страницы начинают требовать вход.

    Чинить сравнения внутри каждого сервиса значило бы править чужую логику в трёх
    местах и помнить об этом впредь. Дешевле отдать сервису тот путь, которого он ждёт.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if self.prefix and path.startswith(self.prefix):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix):] or "/"
                raw = scope.get("raw_path")
                if isinstance(raw, (bytes, bytearray)):
                    cut = raw[len(self.prefix.encode()):]
                    scope["raw_path"] = cut or b"/"
        await self.app(scope, receive, send)


PARTS = (
    ("/studio", pult.app, "пульт"),
    ("/rewrite", rewrite.app, "Rewrite Studio"),
    ("/cover", cover.app, "Cover Studio"),
    ("", cabinet.app, "кабинет"),          # корень монтируется последним
)


@contextlib.asynccontextmanager
async def lifespan(_app):
    """Запустить startup каждого подприложения и погасить их в обратном порядке.

    Без этого платформа поднимается, отвечает на запросы и не делает ничего: очереди
    стоят, планировщик спит, ключи не подгружены. Молча — что хуже, чем не подняться.
    """
    async with contextlib.AsyncExitStack() as stack:
        for _prefix, sub, title in PARTS:
            try:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            except Exception as exc:                               # noqa: BLE001
                # Падение одного сервиса не должно уносить остальные: платформа
                # поднимется без него, и это будет видно, а не выяснится потом.
                print(f"[платформа] {title}: startup не прошёл — {type(exc).__name__}: {exc}")
        yield


app = FastAPI(title="Studio Platform", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/платформа", include_in_schema=False)
@app.get("/_platform", include_in_schema=False)
def parts():
    """Что смонтировано и куда идти. Полезно ровно один раз — когда что-то не отвечает."""
    return {"parts": [{"prefix": p or "/", "title": t,
                       "routes": len(getattr(a, "routes", []))}
                      for p, a, t in PARTS]}


@app.get("/studio", include_in_schema=False)
def studio_root():
    # Монтирование по префиксу без завершающей косой оставляет /studio «за бортом».
    return RedirectResponse("/studio/", 307)


for prefix, sub_app, _title in PARTS:
    if prefix:
        app.mount(prefix, StripPrefix(sub_app, prefix))
# Кабинет монтируется на корень последним: всё, что не разобрали префиксы выше,
# достаётся ему — то есть его пути не изменились ни на символ.
app.mount("/", cabinet.app)
