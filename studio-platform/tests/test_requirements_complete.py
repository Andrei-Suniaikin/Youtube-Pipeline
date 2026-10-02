#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Всё, что код импортирует, записано в requirements (02.10.2026).

Повод. На боевой машине встал конвейер: во вкладке «Очередь» висело «ждём: на сервере
нет youtube-transcript-api». Пакет был поставлен руками на стенде и в
`requirements-dev.txt` не попал НИКОГДА — значит на сервер он не попадал тоже, и
попасть не мог: `deploy/update.sh` доставляет зависимости только когда меняется их
список. Ролики стояли, а выглядело это как загадочная поломка.

За первым блокером стоял второй того же рода: выход субтитров по умолчанию —
SOCKS-туннель (`socks5h://127.0.0.1:1080`), а `PySocks` в окружении не было, и
`requests` не умел с ним говорить.

Поэтому проверка: каждый сторонний импорт кода студии должен быть в
`requirements-dev.txt`. Своё (соседние каталоги платформы) не считается — его импортируют
по пути, а не ставят из пакета.

Исключения перечислены поимённо и с причиной. Список коротким быть обязан: он не способ
обойти проверку, а место, где видно, чем именно мы рискуем.

    .venv/bin/python tests/test_requirements_complete.py
"""
from __future__ import annotations

import ast
import io
import os
import pathlib
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
КОРНИ = ("server", "web")

# Имя при импорте → имя пакета, которым он ставится. Совпадают не всегда.
ПАКЕТ = {
    "PIL": "pillow",
    "socks": "PySocks",
    "youtube_transcript_api": "youtube-transcript-api",
    "edge_tts": "edge-tts",
    "google": "google-api-python-client",
    "googleapiclient": "google-api-python-client",
    "dateutil": "python-dateutil",
    "yaml": "PyYAML",
}

# Чего в списке нет НАМЕРЕННО, и почему. Каждая строка — обещание, что без этого пакета
# студия работает.
ИСКЛЮЧЕНИЯ = {
    "claude_limits": "не пакет с PyPI, а общий на машину семафор лимита подписки: его "
                     "ставят рядом с сервисами, и он намеренно необязателен — нет его, "
                     "работаем как раньше, только вслепую (server/main.py)",
    "langdetect": "необязателен: импорт обёрнут в try/except, без него автоопределение "
                  "языка голоса просто выключено. Включение меняет поведение на боевой "
                  "машине — это решение владельца, а не побочный эффект выкатки",
}

FAILS = []


def check(label, ok):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILS.append(label)


def свои_модули() -> set:
    """Всё, что лежит в самой платформе: это импортируется по пути, а не ставится."""
    свои = set()
    for p in pathlib.Path(BASE).iterdir():
        if p.is_dir() and not p.name.startswith((".", "__")):
            свои.add(p.name)
            for f in p.glob("*.py"):
                свои.add(f.stem)
            for d in p.iterdir():
                if d.is_dir() and (d / "__init__.py").exists():
                    свои.add(d.name)
    for f in pathlib.Path(BASE).glob("*.py"):
        свои.add(f.stem)
    return свои


def импорты() -> dict:
    """Сторонний импорт → файл, где он встретился первым."""
    стандарт = set(sys.stdlib_module_names)
    свои = свои_модули()
    out = {}
    for корень in КОРНИ:
        for p in sorted(pathlib.Path(BASE, корень).rglob("*.py")):
            try:
                дерево = ast.parse(io.open(p, encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for n in ast.walk(дерево):
                if isinstance(n, ast.Import):
                    имена = [a.name.split(".")[0] for a in n.names]
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    имена = [n.module.split(".")[0]]
                else:
                    continue
                for имя in имена:
                    if имя in стандарт or имя in свои or имя.startswith("_"):
                        continue
                    out.setdefault(имя, str(p.relative_to(BASE)))
    return out


def main() -> int:
    req = io.open(os.path.join(BASE, "requirements-dev.txt"), encoding="utf-8").read()
    записано = {line.split("==")[0].strip().lower().replace("_", "-")
                for line in req.splitlines() if line.strip()}

    print("Сторонние импорты кода студии записаны в requirements")
    найдено = импорты()
    check("импорты вообще нашлись (разбор не сломался)", len(найдено) > 3)
    пропущены = []
    for имя, где in sorted(найдено.items()):
        if имя in ИСКЛЮЧЕНИЯ:
            continue
        пакет = ПАКЕТ.get(имя, имя).lower().replace("_", "-")
        if пакет not in записано:
            пропущены.append(f"{имя} (пакет {пакет}) ← {где}")
    for p in пропущены:
        print("        не записан:", p)
    check("ни одного незаписанного", not пропущены)

    print("\nТе два, из-за которых встал конвейер 02.10.2026")
    # Сторож по именам: оба однажды уже отсутствовали, и оба раза это выглядело не как
    # нехватка пакета, а как поломка непонятно чего.
    check("youtube-transcript-api на месте", "youtube-transcript-api" in записано)
    check("PySocks на месте (туннель субтитров без него не работает)",
          "pysocks" in записано)

    print("\nИсключения названы поимённо и с причиной")
    check("список исключений короткий", len(ИСКЛЮЧЕНИЯ) <= 3)
    for имя, почему in ИСКЛЮЧЕНИЯ.items():
        check(f"у «{имя}» есть причина", len(почему) > 40)
        # Исключение имеет смысл, только пока импорт действительно необязателен:
        # он обязан быть обёрнут в try/except там, где встречается.
        где = найдено.get(имя)
        if где:
            текст = io.open(os.path.join(BASE, где), encoding="utf-8").read()
            # Ищем САМ ИМПОРТ, а не первое упоминание имени: у `claude_limits` первым
            # идёт путь в комментарии, и поиск по имени смотрел бы не туда.
            место = re.search(rf"^\s*(?:import|from)\s+{re.escape(имя)}\b",
                              текст, re.M)
            check(f"импорт «{имя}» в коде найден", bool(место))
            кусок = текст[max(0, место.start() - 300):место.start()] if место else ""
            check(f"и «{имя}» импортируется защищённо", "try:" in кусок)

    print("\nВерсии закреплены — иначе машины разъедутся молча")
    без_версии = [line for line in req.splitlines()
                  if line.strip() and "==" not in line]
    check("у каждой строки указана версия", not без_версии)

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
