#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Меню «Switch account» в заливке: скрипт исполняется, а не читается глазами.

Почему тест именно такой. В нашей копии `upload/yt_studio.py` (срез 03.09) пункт
«Switch account» искался так:

    deepAll(document).filter(function(e){ return e.tagName === arguments[0] ... })

`arguments` внутри вложенной функции — это аргументы САМОЙ вложенной функции, то есть
`arguments[0]` там равен элементу `e`, а не селектору. Сравнение никогда не совпадало,
пункт не находился никогда, и переключение аккаунта молча не работало — при этом код
читается как правильный. Поймать это можно только исполнением, поэтому скрипт
вытаскивается из исходника через `ast` и запускается в node с подставными `deepAll` и
`realClick` (приём взят из production-теста
`live_2026-09-18/код/заливка/tests/test_account_menu_callback.py`).

Вторая проверка — статическая и общая: ни в одном скрипте, который мы отдаём браузеру
через `_js`, `arguments[...]` не должен стоять внутри вложенной `function`. Это правило
из гайда изменений production («параметры скрипта сохранять в переменные сразу»); без
такого сторожа та же грабля вернётся в следующем скрипте.

    .venv/bin/python tests/test_account_menu.py
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
UPLOAD = os.path.join(BASE, "upload")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def js_scripts(path):
    """Все строки, которые файл отдаёт браузеру через `_js(driver, "...", ...)`."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_js"):
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                and isinstance(node.args[1].value, str):
            out.append((node.lineno, node.args[1].value))
    return out


def nested_arguments(js: str):
    """Строки скрипта, где `arguments[` стоит внутри тела вложенной `function`.

    Считаем фигурные скобки и запоминаем глубину, на которой открылось тело каждой
    `function`. Стрелочные функции `arguments` не переопределяют, поэтому смотрим только
    на ключевое слово `function`. Строковые литералы и комментарии в наших скриптах
    фигурных скобок не содержат, разбирать их полноценным парсером не нужно.
    """
    depth, fn_depths, pending_fn = 0, [], False
    line_no, bad = 1, []
    i = 0
    while i < len(js):
        ch = js[i]
        if js.startswith("function", i) and (i == 0 or not js[i - 1].isalnum()):
            pending_fn = True
            i += len("function")
            continue
        if ch == "{":
            depth += 1
            if pending_fn:
                fn_depths.append(depth)
                pending_fn = False
        elif ch == "}":
            if fn_depths and fn_depths[-1] == depth:
                fn_depths.pop()
            depth -= 1
        elif ch == "\n":
            line_no += 1
        elif js.startswith("arguments[", i) and fn_depths:
            bad.append((line_no, js.splitlines()[line_no - 1].strip()))
            i += len("arguments[")
            continue
        i += 1
    return bad


def switch_script():
    """Тот самый скрипт поиска пункта «Switch account» — из живого исходника."""
    tree = ast.parse(open(os.path.join(UPLOAD, "yt_studio.py"), encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "open_account_menu")
    return next(n.args[1].value for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_js" and isinstance(n.args[1], ast.Constant)
                and "var sw =" in n.args[1].value)


def run_switch(node, source, items):
    """Исполнить скрипт в node: deepAll отдаёт items, realClick копит нажатое."""
    code = (
        "const items=%s, clicked=[];\n"
        "const run=new Function('deepAll','realClick','document',"
        "'return function(){'+%s+'}');\n"
        "const found=run(()=>items,e=>clicked.push(e.key),{})"
        "('ytd-compact-link-renderer','SWITCH_ACCOUNTS');\n"
        "console.log(JSON.stringify({found,clicked}));"
    ) % (json.dumps(items), json.dumps(source))
    res = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        return {"node_error": (res.stderr or "").strip()[:400]}
    return json.loads(res.stdout)


def main() -> int:
    print("Скрипт «Switch account» исполняется в node")

    node = shutil.which("node")
    source = switch_script()
    if not node:
        print("  ПРОПУЩЕНО: node не найден — исполнить скрипт нечем")
        FAILS.append("node не найден: исполняемую проверку пропустить нельзя")
    else:
        menu = [
            {"tagName": "YTD-COMPACT-LINK-RENDERER", "key": "help",
             "data": {"icon": {"iconType": "HELP"}}},
            {"tagName": "YTD-COMPACT-LINK-RENDERER", "key": "switch",
             "data": {"icon": {"iconType": "SWITCH_ACCOUNTS"}}},
        ]
        check("пункт найден и нажат именно он",
              run_switch(node, source, menu), {"found": True, "clicked": ["switch"]})

        # Ложного нажатия быть не должно: пустое меню, пункт без .data (Studio отдаёт
        # такие в момент подгрузки) и чужой тег с нужной иконкой.
        for title, items in [
            ("пустое меню", []),
            ("пункт без data", [{"tagName": "YTD-COMPACT-LINK-RENDERER", "key": "empty"}]),
            ("чужой тег с нужной иконкой",
             [{"tagName": "DIV", "key": "wrong", "data": {"icon": {"iconType": "SWITCH_ACCOUNTS"}}}]),
        ]:
            check(f"ничего не нажато: {title}",
                  run_switch(node, source, items), {"found": False, "clicked": []})

    print("\n`arguments` не живёт внутри вложенных функций ни в одном скрипте заливки")

    checked = 0
    for name in sorted(os.listdir(UPLOAD)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(UPLOAD, name)
        for lineno, js in js_scripts(path):
            checked += 1
            bad = nested_arguments(js)
            if bad:
                where = "; ".join(f"{t!r}" for _, t in bad)
                check(f"{name}:{lineno} — параметры скрипта сохранены в переменные",
                      where, "")
    check("скрипты вообще найдены (иначе проверка пустая)", checked > 0, True)

    # Сторож самого сторожа: на заведомо сломанном образце он обязан сработать, иначе
    # «проверок нет» и «всё чисто» выглядят одинаково.
    broken = "var sw = all.filter(function(e){ return e.tagName === arguments[0]; })[0];"
    check("сторож ловит вложенный arguments", bool(nested_arguments(broken)), True)
    fixed = "var tag = arguments[0];\nvar sw = all.filter(function(e){ return e.tagName === tag; })[0];"
    check("и не ругается на правильный код", nested_arguments(fixed), [])

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
