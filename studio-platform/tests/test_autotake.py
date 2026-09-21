#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовый результат доходит до проекта сам — но только там, где выбора нет.

Две жалобы владельца 21.09, у которых одна природа:

  «код сделал обложку, но записал в корень проекта и нельзя её выкачать с сайта»
  «чтобы сценарий присвоился, нужно тыкать ещё кнопку, автоматически не присваивается»

В обоих случаях оплаченный результат существовал, но проекта не достигал: перенести его
умела единственная кнопка, спрятанная внутри свёрнутой строки задачи. От «Готово» до
результата в проекте было два клика, и первый ничем не подписан как обязательный. Из-за
этого же не загоралась плитка «Обложка» в шапке — она смотрит на файл `cover.jpg`
проекта, а не на статус задачи (и это правильно: выпуск не должен обещать то, чего нет).

Автоматика намеренно узкая, и тест сторожит именно границы, а не сам перенос:

* **Результат ровно один.** Когда вариантов несколько, выбор между оплаченными
  картинками или между языками текста принадлежит владельцу, а не платформе.
* **Место свободно.** Непустой сценарий не перезаписывается — это правило владелец
  задал отдельно 20.09 («новый текст его не заменит, пока вы не нажмёте „Взять в
  проект"»). Существующий `cover.jpg` не трогается тем более: его читает publication
  plane перед заливкой, и молча подменить обложку уехавшего релиза хуже всего.
* **Задача завершена.** Незавершённая не даёт ничего.

Сети здесь нет: соседи подменены.

    .venv/bin/python tests/test_autotake.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
os.environ.setdefault("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
os.environ.setdefault("VS_SERVER_DB", os.environ["VS_DEV_DB"])

from web import app                                                # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def test_cover(tmp) -> None:
    print("\nОбложка")
    pdir = os.path.join(tmp, "proj"); os.makedirs(pdir, exist_ok=True)

    class _Mod:
        @staticmethod
        def _project_dir(tenant, pid): return pdir

    was_mod = sys.modules.get("main")
    sys.modules["main"] = _Mod()
    was_open = app.urllib.request.urlopen

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"JPEGDATA"
    app.urllib.request.urlopen = lambda *a, **k: _R()
    try:
        one = [{"id": "j1", "status": "completed", "output_files": ["a/cover.jpg"]}]
        check("один готовый вариант — взяли", app._autotake_cover("p", one, "sid"),
              "a/cover.jpg")
        check("файл лежит в проекте",
              open(os.path.join(pdir, "cover.jpg"), "rb").read(), b"JPEGDATA")

        # Повторный вызов не должен переписать уже стоящую обложку.
        check("второй раз не трогаем", app._autotake_cover("p", one, "sid"), "")

        os.remove(os.path.join(pdir, "cover.jpg"))
        two = [{"id": "j1", "status": "completed", "output_files": ["a.jpg", "b.jpg"]}]
        check("два варианта — выбирает человек", app._autotake_cover("p", two, "sid"), "")
        running = [{"id": "j1", "status": "running", "output_files": ["a.jpg"]}]
        check("задача не готова — брать нечего",
              app._autotake_cover("p", running, "sid"), "")
        check("после отказов файла так и нет",
              os.path.exists(os.path.join(pdir, "cover.jpg")), False)
    finally:
        app.urllib.request.urlopen = was_open
        if was_mod is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = was_mod


def test_text() -> None:
    print("\nСценарий")
    saved = []
    was_scen, was_proxy, was_form = (app._project_scenario, app._proxy_get,
                                     app._cabinet_form)
    app._cabinet_form = lambda *a, **k: saved.append(a) or {}
    try:
        def proxy(base, path, sid, files=("готовый.txt",), text="новый текст"):
            return (200, list(files)) if path.endswith("/files") else (200, text)

        app._project_scenario = lambda mod, pid: ""
        app._proxy_get = proxy
        rows = [{"id": "r1", "status": "completed"}]
        check("пустой проект, один файл — взяли",
              app._autotake_text("p", rows, "sid", None), "готовый.txt")
        check("текст ушёл в проект", bool(saved), True)

        saved.clear()
        app._project_scenario = lambda mod, pid: "уже написанный сценарий"
        check("непустой сценарий не перезаписываем",
              app._autotake_text("p", rows, "sid", None), "")
        check("и ничего не сохраняли", saved, [])

        app._project_scenario = lambda mod, pid: ""
        app._proxy_get = lambda b, p, s: proxy(b, p, s, files=("ru.txt", "en.txt"))
        check("несколько языков — выбирает человек",
              app._autotake_text("p", rows, "sid", None), "")
        app._proxy_get = proxy
        check("задача не готова — брать нечего",
              app._autotake_text("p", [{"id": "r1", "status": "running"}], "sid", None), "")
        check("за все отказы ни одного сохранения", saved, [])
    finally:
        app._project_scenario, app._proxy_get, app._cabinet_form = (was_scen, was_proxy,
                                                                    was_form)


def main() -> int:
    print("Проверка: результат доходит до проекта сам")
    with tempfile.TemporaryDirectory() as tmp:
        test_cover(tmp)
    test_text()
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
