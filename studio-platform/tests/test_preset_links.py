#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресеты целые и частичные: разрешение ссылок (`server/preset_links.py`), фаза 1
батча «Пресеты целые и частичные».

Без сети: сама функция разрешения и её потребители проверяются на базе в памяти.
Единственный поход к боевому коду — импорт `main` ради `_preset_project_payload`
(проверить, что служебные `_`-ключи не утекают в задачу), тем же способом, что уже
делают другие тесты кабинета (см. `test_dynamic_threads.py`).

    .venv/bin/python tests/test_preset_links.py
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("VS_SERVER_DISABLE_BACKGROUND", "1")
os.environ.setdefault("VS_SERVER_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
os.environ.setdefault("VS_TENANTS_DIR", os.path.join(BASE, "dev", "data", "tenants"))
# Кабинет тянет движки по зашитому пути ~/VideoStudio/src — тот же изолированный HOME,
# что и у dev/run.sh. Без него импорт main.py падает на googler_worker.
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import preset_fields as PF                                         # noqa: E402
import preset_links                                                # noqa: E402
import pub_engine                                                  # noqa: E402
import pub_services                                                # noqa: E402
import main as M                                                   # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh() -> sqlite3.Connection:
    """Минимальная база в памяти: только то, что читают preset_links и его потребители.

    `tenant_id` есть у всех трёх таблиц — Issue 3/4 ревью цикла 1 потребовали, чтобы
    `resolve`/`users_of` сами отвечали «того же тенанта», а не полагались на то, что
    чужой id туда никто не впишет.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE presets(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, "
              "settings TEXT)")
    c.execute("CREATE TABLE pub_channels(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, "
              "preset_vsgen_id INTEGER)")
    c.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, "
              "settings TEXT)")
    return c


DEV = "dev"


def put(c, pid, settings, tenant=DEV):
    c.execute("INSERT INTO presets(id, tenant_id, name, settings) VALUES(?, ?, ?, ?)",
              (pid, tenant, f"пресет {pid}", json.dumps(settings, ensure_ascii=False)))


def _is_params_name(n):
    """`params` или `p` — единственные два имени, под которыми `_build_cmd` и
    `_render_config_from_params` получают настройки задачи."""
    return isinstance(n, ast.Name) and n.id in ("params", "p")


def _const_str(n):
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def _loop_uses_name(node, name):
    """Есть ли внутри узла `for` вызов `params.get(name)`/`p.get(name)` или
    индексация `params[name]`/`p[name]` — то есть переменная цикла реально ушла в
    чтение настроек, а не просто перечислена рядом."""
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get" and _is_params_name(sub.func.value)
                and sub.args and isinstance(sub.args[0], ast.Name)
                and sub.args[0].id == name):
            return True
        if (isinstance(sub, ast.Subscript) and _is_params_name(sub.value)
                and isinstance(sub.slice, ast.Name) and sub.slice.id == name):
            return True
    return False


def _collect_stage_keys(fn_node) -> set:
    """Ключи, которые функция реально читает у `params`/`p` — разобрано из
    исходника, а не переписано руками (Issue 2 ревью цикла 3: старый тест сверял
    карту саму с собой и не заметил бы новое чтение).

    Три формы чтения, которые встречаются в `_build_cmd`/`_render_config_from_params`:
      1. `params.get("ключ")` / `p["ключ"]` — литерал прямо в вызове/индексации;
      2. `for k in ("а", "б", ...): ... p.get(k)` — ключ приходит через переменную
         цикла по кортежу литералов (пример: min/max_image_duration, media_offset_*);
      3. `for k_ui, k_cfg in (("а","а"), ...): ... p.get(k_ui)` — то же самое, но
         цикл идёт по кортежам пар (пример: main_audio_volume/video_audio_volume).
    Вызов вида `(params or {}).get(key)` (главы, main.py ≈7414) НЕ совпадает ни с
    одной формой — объект вызова там не голое имя `params`/`p`, а `BoolOp`, и это
    осознанно: те ключи (`chapters_target`/`chapters_min_gap`/…) не поле пресета
    вообще, кортеж читается только для kind == "chapters", в разбор частей ТЗ его
    никто не относил (таблица «Ответы на уточнения фазы 1», вопрос 1).
    """
    keys = set()
    for node in ast.walk(fn_node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and _is_params_name(node.func.value)
                and node.args):
            k = _const_str(node.args[0])
            if k:
                keys.add(k)
        if isinstance(node, ast.Subscript) and _is_params_name(node.value):
            k = _const_str(node.slice)
            if k:
                keys.add(k)
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            elt_keys = [_const_str(e) for e in node.iter.elts]
            if elt_keys and all(elt_keys) and _loop_uses_name(node, node.target.id):
                keys.update(elt_keys)
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Tuple)
                and len(node.target.elts) == 2
                and all(isinstance(t, ast.Name) for t in node.target.elts)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            pairs_ok = all(isinstance(e, ast.Tuple) and len(e.elts) == 2
                           and _const_str(e.elts[0]) for e in node.iter.elts)
            if pairs_ok and node.iter.elts:
                first_name = node.target.elts[0].id
                if _loop_uses_name(node, first_name):
                    keys.update(_const_str(e.elts[0]) for e in node.iter.elts)
    return keys


def _stage_key_sources() -> dict:
    """`{"_build_cmd": {...ключи...}, "_render_config_from_params": {...}}` — из
    настоящего `server/main.py`, не из памяти агента."""
    main_path = os.path.join(BASE, "server", "main.py")
    tree = ast.parse(open(main_path, encoding="utf-8").read(), filename=main_path)
    wanted = ("_build_cmd", "_render_config_from_params")
    found = {n.name: n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name in wanted}
    missing = [name for name in wanted if name not in found]
    if missing:
        raise AssertionError(f"в server/main.py не нашлись функции: {missing} — "
                              "переименовали? тест против дрейфа их не видит вслепую")
    return {name: _collect_stage_keys(node) for name, node in found.items()}


def main() -> int:
    print("Целый пресет без ссылок разрешается в свои поля")
    c = fresh()
    put(c, 1, {"engine": "edge", "speed": "1.2"})
    r = preset_links.resolve(c, 1)
    check("engine на месте", r["engine"], "edge")
    check("speed на месте", r["speed"], "1.2")
    check("_scope по умолчанию — full", r["_scope"], "full")
    check("_links пуст", r["_links"], {})
    check("_link_problems пуст", r["_link_problems"], [])

    print("\nУдалённый / битый пресет — как пустой целый, а не отказ")
    c = fresh()
    r = preset_links.resolve(c, 404)
    check("несуществующий пресет — пустые свои поля", r["_scope"], "full")
    check("и пустые _links", r["_links"], {})
    c = fresh()
    c.execute("INSERT INTO presets(id, tenant_id, name, settings) VALUES(5, 'dev', 'битый', 'не json')")
    r = preset_links.resolve(c, 5)
    check("битый JSON не роняет разрешение", r["_scope"], "full")

    print("\nСсылка заменяет группу «Озвучка» целиком")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean",
                "voice_uuid": "11111111-1111-1111-1111-111111111111"})
    put(c, 1, {"engine": "edge", "speed": "9.9", "_links": {"voice": 10}})
    r = preset_links.resolve(c, 1)
    check("engine — из частичного", r["engine"], "lumean")
    check("voice_uuid — из частичного", r.get("voice_uuid"),
          "11111111-1111-1111-1111-111111111111")
    check("своё поле группы, которого нет в частичном, в результате отсутствует",
          "speed" in r, False)
    check("метаданные о ссылке остались видны", r["_links"], {"voice": 10})
    check("проблем нет", r["_link_problems"], [])

    print("\nСсылка не трогает другие группы")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean"})
    put(c, 1, {"engine": "edge", "model": "OPENAI", "_links": {"voice": 10}})
    r = preset_links.resolve(c, 1)
    check("картинки (чужая группа) не тронуты", r.get("model"), "OPENAI")

    print("\nСломанные ссылки — свои поля остаются, причина в _link_problems")
    c = fresh()
    put(c, 1, {"engine": "edge", "speed": "1.0", "_links": {"voice": 999}})
    r = preset_links.resolve(c, 1)
    check("частичный удалён — своё поле группы осталось", r.get("engine"), "edge")
    check("причина: пресет удалён/повреждён", r["_link_problems"],
          [{"part": "voice", "preset_id": 999,
            "why": "частичный пресет удалён или его настройки повреждены"}])

    c = fresh()
    put(c, 20, {"_scope": "images", "model": "NARWHAL"})   # не та часть для "voice"
    put(c, 1, {"engine": "edge", "_links": {"voice": 20}})
    r = preset_links.resolve(c, 1)
    check("частичный не той части — своё поле осталось", r.get("engine"), "edge")
    check("причина: не та часть", r["_link_problems"][0]["why"],
          "частичный пресет не той части")

    c = fresh()
    put(c, 30, {"_scope": "voice", "engine": "lumean", "_links": {"images": 99}})
    put(c, 1, {"engine": "edge", "_links": {"voice": 30}})
    r = preset_links.resolve(c, 1)
    check("частичный сам со ссылками — своё поле осталось", r.get("engine"), "edge")
    check("причина: свои ссылки у частичного", r["_link_problems"][0]["why"],
          "у частичного пресета есть свои ссылки — так нельзя")

    print("\nЧастичный пресет другого тенанта — своё поле остаётся, причина "
          "«другого тенанта» (Issue 4 ревью цикла 1)")
    c = fresh()
    put(c, 40, {"_scope": "voice", "engine": "lumean"}, tenant="other")
    put(c, 1, {"engine": "edge", "_links": {"voice": 40}}, tenant="dev")
    r = preset_links.resolve(c, 1)
    check("частичный чужого тенанта — своё поле осталось", r.get("engine"), "edge")
    check("причина: другой тенант", r["_link_problems"],
          [{"part": "voice", "preset_id": 40, "why": "частичный пресет другого тенанта"}])

    print("\n_preset_project_payload выбрасывает служебные ключи (main.py)")
    resolved = {"engine": "edge", "_scope": "full", "_links": {"voice": 10},
                "_link_problems": []}
    payload = M._preset_project_payload(resolved, 1)
    check("_scope выброшен", "_scope" in payload, False)
    check("_links выброшен", "_links" in payload, False)
    check("_link_problems выброшен", "_link_problems" in payload, False)
    check("engine остался", payload.get("engine"), "edge")
    check("_applied_preset проставлен", payload.get("_applied_preset"), 1)

    print("\nmain._preset_settings с битым JSON пресета — None, как было до подключения "
          "resolve() (Issue 1 ревью цикла 1): вызывающая сторона (создание проекта "
          "с пресетом) обязана и дальше идти по пути наследования настроек последнего "
          "проекта тенанта, а не молча получать умолчания кабинета.\n"
          "База — в памяти (Issue 2 ревью цикла 4): на время этой проверки M.db() "
          "подменяется на in-memory соединение, чтобы тест не писал в dev/vsgen.dev.db "
          "и не двигал её автоинкремент при прогоне вне стенда.")
    test_tenant = "test-preset-links-issue1"
    mem_conn = sqlite3.connect(":memory:")
    mem_conn.row_factory = sqlite3.Row
    mem_conn.execute(
        "CREATE TABLE presets(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, "
        "settings TEXT, created REAL)"
    )
    orig_db = M.db
    M.db = lambda: mem_conn
    try:
        cur = mem_conn.execute(
            "INSERT INTO presets(tenant_id, name, settings, created) VALUES(?,?,?,?)",
            (test_tenant, "битый (тест фазы 1)", "не json", 0.0),
        )
        broken_pid = cur.lastrowid
        mem_conn.commit()
        got = M._preset_settings(test_tenant, broken_pid)
        check("битый JSON пресета — None, а не словарь умолчаний", got, None)
    finally:
        M.db = orig_db
        mem_conn.close()

    print("\nusers_of: кто бежит на пресете")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean"})
    put(c, 1, {"engine": "edge", "_links": {"voice": 10}})
    c.execute("INSERT INTO pub_channels(id, tenant_id, name, preset_vsgen_id) "
              "VALUES(1, 'dev', 'Канал А', 1)")
    c.execute("INSERT INTO pub_channels(id, tenant_id, name, preset_vsgen_id) "
              "VALUES(2, 'dev', 'Канал Б напрямую на частичный', 10)")
    c.execute("INSERT INTO projects(id, tenant_id, name, settings) "
              "VALUES('p1', 'dev', 'Проект 1', ?)",
              (json.dumps({"_applied_preset": 1}),))
    c.execute("INSERT INTO projects(id, tenant_id, name, settings) "
              "VALUES('p2', 'dev', 'Проект 2', ?)",
              (json.dumps({"_applied_preset": 999}),))
    # Мусор в settings — не должен ронять весь запрос (Issue 3 ревью цикла 1:
    # json_extract на невалидном JSON бросает OperationalError, если его не
    # отсечь json_valid раньше).
    c.execute("INSERT INTO projects(id, tenant_id, name, settings) "
              "VALUES('p-broken', 'dev', 'Проект битый', 'не json')")

    u1 = preset_links.users_of(c, 1)
    check("целый: свои каналы", [x["id"] for x in u1["channels"]], [1])
    check("целый: свои проекты", [x["id"] for x in u1["projects"]], ["p1"])
    check("целый: presets (не про него)", u1["presets"], [])
    check("целый: via (не про него)", u1["via"], [])

    u10 = preset_links.users_of(c, 10)
    check("частичный: прямые каналы", [x["id"] for x in u10["channels"]], [2])
    check("частичный: целые пресеты, ссылающиеся на него",
          [x["id"] for x in u10["presets"]], [1])
    check("частичный: транзитивные каналы через эти целые",
          [x["id"] for x in u10["via"]], [1])
    check("частичный: свои проекты (у него их не бывает напрямую)",
          u10["projects"], [])

    print("\nusers_of: id пресета строкой даёт тот же результат, что и числом "
          "(Issue 1 ревью цикла 4) — разбор тем же _parse_link_ref, что и resolve()")
    check("users_of('10') совпадает с users_of(10) для частичного пресета",
          preset_links.users_of(c, "10"), u10)
    check("users_of('1') совпадает с users_of(1) для целого пресета",
          preset_links.users_of(c, "1"), u1)

    print("\nusers_of: держатель считается только по ссылке ПОД СВОЕЙ частью "
          "(Issue 4 ревью цикла 3) — resolve() смотрит именно _links.<scope>, "
          "а не любой из пяти ключей")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean"})
    # Целый пресет ссылается на частичный id=10 (часть voice) не той частью — под
    # ключом _links.images. resolve() эту ссылку никогда не примет (scope_of(10)
    # == "voice" != "images" → "частичный пресет не той части"), значит и в «кто
    # бежит» частичного 10 этот целый быть не должно.
    put(c, 2, {"engine": "edge", "_links": {"images": 10}})
    u10_wrong_part = preset_links.users_of(c, 10)
    check("ссылка под чужой частью не делает пресет держателем",
          u10_wrong_part["presets"], [])
    check("и транзитивных каналов через неё тоже нет",
          u10_wrong_part["via"], [])

    print("\nusers_of: держатель обязан сам быть ЦЕЛЫМ (Issue 4 ревью цикла 3) — "
          "правило 4 запрещает частичному иметь свои _links, поэтому даже если в "
          "обход API они там оказались, resolve() их не читает вовсе")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean"})
    # Испорченные данные: у САМОГО частичного (своя _scope="images") завёлся
    # _links.voice=10 — так быть не должно (правило 4), но проверка на запись
    # появляется только в фазе 2, а resolve() уже сейчас обязан не давать такому
    # мусору участвовать в «кто бежит».
    put(c, 3, {"_scope": "images", "model": "OPENAI", "_links": {"voice": 10}})
    u10_not_full = preset_links.users_of(c, 10)
    check("не-целый пресет со своими _links не считается держателем",
          u10_not_full["presets"], [])

    c = fresh()
    check("users_of для несуществующего пресета — пустой набор",
          preset_links.users_of(c, 12345),
          {"channels": [], "projects": [], "presets": [], "via": []})

    print("\nusers_of: чужой тенант не попадает в выдачу (Issue 3 ревью цикла 1)")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean"}, tenant="dev")
    put(c, 1, {"engine": "edge", "_links": {"voice": 10}}, tenant="dev")
    # `presets.id` — сквозной автоинкремент, второго пресета с id=1 быть не может.
    # Реальный риск — канал или проект ЧУЖОГО тенанта, у которого preset_vsgen_id /
    # _applied_preset случайно (рассинхрон, ручная правка) указывает на id=1: он не
    # должен попасть в карточку «кто бежит» пресета тенанта dev.
    c.execute("INSERT INTO pub_channels(id, tenant_id, name, preset_vsgen_id) "
              "VALUES(1, 'dev', 'Канал dev', 1)")
    c.execute("INSERT INTO pub_channels(id, tenant_id, name, preset_vsgen_id) "
              "VALUES(2, 'other', 'Канал чужого тенанта', 1)")
    c.execute("INSERT INTO projects(id, tenant_id, name, settings) "
              "VALUES('p1', 'dev', 'Проект dev', ?)",
              (json.dumps({"_applied_preset": 1}),))
    c.execute("INSERT INTO projects(id, tenant_id, name, settings) "
              "VALUES('p-other', 'other', 'Проект чужого тенанта', ?)",
              (json.dumps({"_applied_preset": 1}),))
    u1_dev = preset_links.users_of(c, 1)
    check("чужой канал с тем же preset_vsgen_id не попал",
          [x["id"] for x in u1_dev["channels"]], [1])
    check("чужой проект с тем же _applied_preset не попал",
          [x["id"] for x in u1_dev["projects"]], ["p1"])

    put(c, 20, {"_scope": "voice", "engine": "edge"}, tenant="dev")
    put(c, 21, {"engine": "edge", "_links": {"voice": 20}}, tenant="dev")
    put(c, 99, {"engine": "edge", "_links": {"voice": 20}}, tenant="other")
    u20 = preset_links.users_of(c, 20)
    check("чужой целый пресет с той же ссылкой не попал в presets",
          [x["id"] for x in u20["presets"]], [21])

    print("\nСсылка, записанная строкой в старых данных (Minor 5 ревью цикла 2): "
          "resolve() и users_of() обязаны разбирать её одинаково")
    c = fresh()
    put(c, 50, {"_scope": "voice", "engine": "lumean"}, tenant="dev")
    # Легаси-запись: _links.voice хранит id частичного пресета строкой, а не числом
    # (до того, как запись стала строго числовой в фазе 2).
    c.execute("INSERT INTO presets(id, tenant_id, name, settings) VALUES(51, 'dev', "
              "'целый (строковая ссылка)', ?)",
              (json.dumps({"engine": "edge", "_links": {"voice": "50"}}),))
    r_str = preset_links.resolve(c, 51)
    check("resolve() находит частичный по ссылке-строке", r_str.get("engine"), "lumean")
    check("и не заводит _link_problems на ровном месте", r_str["_link_problems"], [])
    u_str = preset_links.users_of(c, 50)
    check("users_of() видит тот же целый пресет по той же ссылке-строке",
          [x["id"] for x in u_str["presets"]], [51])

    print("\nСсылка, записанная объектом или списком (Issue 3 ревью цикла 3): "
          "resolve() раньше падал sqlite3.ProgrammingError на bind — теперь это "
          "просто сломанная ссылка, свои поля остаются, причина в _link_problems")
    c = fresh()
    put(c, 1, {"engine": "edge", "speed": "1.0", "_links": {"voice": {"id": 1}}})
    r_obj = preset_links.resolve(c, 1)
    check("объект в _links.voice не роняет resolve()", r_obj.get("engine"), "edge")
    check("причина: ссылка повреждена (объект)", r_obj["_link_problems"],
          [{"part": "voice", "preset_id": {"id": 1}, "why": "ссылка повреждена"}])

    c = fresh()
    put(c, 1, {"engine": "edge", "_links": {"voice": [1, 2]}})
    r_list = preset_links.resolve(c, 1)
    check("список в _links.voice не роняет resolve()", r_list.get("engine"), "edge")
    check("причина: ссылка повреждена (список)", r_list["_link_problems"],
          [{"part": "voice", "preset_id": [1, 2], "why": "ссылка повреждена"}])

    check("_parse_link_ref: int — как есть", preset_links._parse_link_ref(12), 12)
    check("_parse_link_ref: числовая строка — int", preset_links._parse_link_ref(" 12 "), 12)
    check("_parse_link_ref: bool — не id (подкласс int, но не число ссылки)",
          preset_links._parse_link_ref(True), None)
    check("_parse_link_ref: объект — None", preset_links._parse_link_ref({"id": 1}), None)
    check("_parse_link_ref: список — None", preset_links._parse_link_ref([1]), None)
    check("_parse_link_ref: нечисловая строка — None",
          preset_links._parse_link_ref("не число"), None)
    check("_parse_link_ref: None — None", preset_links._parse_link_ref(None), None)

    print("\nscope_of / group_keys — прямые проверки (правило 2: одно разрешение на всех)")
    check("full по умолчанию", preset_links.scope_of({}), "full")
    check("частичный распознан", preset_links.scope_of({"_scope": "voice"}), "voice")
    check("незнакомое значение — full (fail safe, не выдумываем часть)",
          preset_links.scope_of({"_scope": "бред"}), "full")
    check("регистр не важен", preset_links.scope_of({"_scope": "VOICE"}), "voice")
    check("не словарь — full", preset_links.scope_of(None), "full")
    check("group_keys(full) пуст — у целого нет одной группы",
          preset_links.group_keys("full"), set())
    check("group_keys(voice) совпадает с preset_fields.part_group_keys",
          preset_links.group_keys("voice"), PF.part_group_keys("voice"))
    check("group_keys неизвестной части — пусто, а не исключение",
          preset_links.group_keys("не часть"), set())

    print("\nresolve(): мусор в _links не должен ронять разрешение (защита от того, что "
          "в обход API кто-то допишет в JSON пресета не то, что ожидает describe())")
    c = fresh()
    put(c, 1, {"engine": "edge", "_links": "не словарь"})
    r = preset_links.resolve(c, 1)
    check("_links не словарь — своё поле осталось", r.get("engine"), "edge")
    check("_links в ответе — пустой словарь, а не исходный мусор", r["_links"], {})
    check("проблем не заведено — это не сломанная ссылка, а отсутствие ссылок",
          r["_link_problems"], [])

    c = fresh()
    put(c, 1, {"engine": "edge", "_links": {"неизвестная_часть": 10}})
    r = preset_links.resolve(c, 1)
    check("незнакомый ключ в _links тихо игнорируется", r.get("engine"), "edge")
    check("не превращается в _link_problems (это не ссылка на существующую часть)",
          r["_link_problems"], [])

    print("\npreset_fields.PARTS и part_group_keys — части из спецификации "
          "(с 18.09.2026 «Сценарий» и «Обложка» разделены)")
    check("части соответствуют группам GROUPS", dict(PF.PARTS),
          {"voice": "Озвучка", "images": "Картинки", "clips": "Клипы",
           "render": "Сборка", "text": "Сценарий", "cover": "Обложка"})
    # Ответ на вопрос 1 (ревью цикла 2, вариант б): группа части — поля формы
    # GROUPS ∪ PART_EXTRA_KEYS (ключи, которые читает сборщик параметров этапа, но
    # форма их не показывает). Ниже — форма ∪ карта, а не только форма, как было
    # до исправления Major-замечания.
    check("voice — «Озвучка» ∪ ключи, которые читает tts-сборщик",
          PF.part_group_keys("voice"),
          {"engine", "speed", "voice_by_lang", "voice", "model_id",
           "split_type", "stability", "voice_uuid"} | PF.PART_EXTRA_KEYS["voice"])
    check("images — «Картинки» ∪ ключи images-контракта/сборщика",
          PF.part_group_keys("images"),
          {"model", "aspect_ratio", "style", "sentences_per_prompt", "seed",
           "analyze_story", "split_long_sentences", "max_solo_scene_seconds"}
          | PF.PART_EXTRA_KEYS["images"])
    check("clips — «Клипы» ∪ t2v-ключи", PF.part_group_keys("clips"),
          {"video_method", "first_n", "prompt"} | PF.PART_EXTRA_KEYS["clips"])
    check("render — «Сборка» ∪ ключи _render_config_from_params",
          PF.part_group_keys("render"),
          {"quality", "transitions", "sync_audio", "kenburns", "subtitles",
           "subtitle_lang", "chapters", "material_order",
           "min_image_duration", "max_image_duration"} | PF.PART_EXTRA_KEYS["render"])
    check("text — только пресет рерайта", PF.part_group_keys("text"), {"preset_rewrite"})
    check("cover — только пресет обложки", PF.part_group_keys("cover"), {"preset_cover"})
    check("сценарий и обложка не пересекаются ключами",
          PF.part_group_keys("text") & PF.part_group_keys("cover"), set())
    check("неизвестная часть — пусто, а не исключение", PF.part_group_keys("не часть"), set())

    print("\nВспомогательные проверки самой карты PART_EXTRA_KEYS (не зависят от "
          "источника — просто сверяют карту саму с собой)")
    all_extra = [(part, key) for part, keys in PF.PART_EXTRA_KEYS.items() for key in keys]
    seen = {}
    dupes = []
    for part, key in all_extra:
        if key in seen and seen[key] != part:
            dupes.append((key, seen[key], part))
        seen[key] = part
    check("ни один ключ PART_EXTRA_KEYS не отнесён к двум частям сразу", dupes, [])
    # И ключ из PART_EXTRA_KEYS одной части не должен совпадать с полем формы GROUPS
    # другой части — иначе объединение «своих» ключей у двух частей пересечётся, и
    # правило 3 (группа берётся из одного источника целиком) перестанет однозначно
    # определять, чья это группа.
    cross = []
    for part, key in all_extra:
        own_title = dict(PF.PARTS)[part]
        for title, fields in PF.GROUPS:
            if title == own_title:
                continue
            if key in {f.key for f in fields}:
                cross.append((key, part, title))
    check("ключ PART_EXTRA_KEYS не совпадает с полем формы чужой группы", cross, [])
    # SHARED_KEYS/TASK_LEVEL_KEYS — те же требования: не дублировать поле части и
    # не пересекаться друг с другом (иначе «одно место» перестанет быть одним).
    part_owned = {key for part in PF.PART_EXTRA_KEYS for key in PF.part_group_keys(part)}
    check("SHARED_KEYS не пересекается с полями/PART_EXTRA_KEYS ни одной части",
          sorted(PF.SHARED_KEYS & part_owned), [])
    check("TASK_LEVEL_KEYS не пересекается с полями/PART_EXTRA_KEYS ни одной части",
          sorted(PF.TASK_LEVEL_KEYS & part_owned), [])
    check("SHARED_KEYS и TASK_LEVEL_KEYS не пересекаются друг с другом",
          sorted(PF.SHARED_KEYS & PF.TASK_LEVEL_KEYS), [])

    print("\nТест против дрейфа (Major, Issue 2 ревью цикла 3): множество ключей "
          "берётся ИЗ ИСХОДНИКА _build_cmd/_render_config_from_params (разбор "
          "ast, а не переписанный вручную список), и каждый такой ключ обязан "
          "быть отнесён ровно в одно место — часть (GROUPS ∪ PART_EXTRA_KEYS), "
          "SHARED_KEYS или TASK_LEVEL_KEYS. Доказательство, что это ловит "
          "дрейф: без исправления Issue 1 (smart_mode/num_images) этот тест "
          "падает и называет оба ключа")
    stage_keys = _stage_key_sources()
    check("_build_cmd что-то читает (сам разбор не сломан)",
          bool(stage_keys.get("_build_cmd")), True)
    check("_render_config_from_params что-то читает (сам разбор не сломан)",
          bool(stage_keys.get("_render_config_from_params")), True)

    def _owners(key):
        owners = [part for part, _title in PF.PARTS if key in PF.part_group_keys(part)]
        if key in PF.SHARED_KEYS:
            owners.append("shared")
        if key in PF.TASK_LEVEL_KEYS:
            owners.append("task-level")
        return owners

    for fn_name, keys in stage_keys.items():
        unresolved = [(key, _owners(key)) for key in sorted(keys)
                      if len(_owners(key)) != 1]
        check(f"{fn_name}: каждый прочитанный ключ отнесён ровно в одно место",
              unresolved, [])
    # use_cache — известное намеренное пересечение чтения (Q2/решение цикла 3):
    # его читают tts, images и video, но владелец распределения — один (shared),
    # а не три части. Задокументировано явно, а не тихо пропущено фильтром.
    check("use_cache прочитан обеими функциями и отнесён к shared",
          ("use_cache" in stage_keys.get("_build_cmd", set()), _owners("use_cache")),
          (True, ["shared"]))

    print("\nresolve() на самом частичном пресете: свои поля, а свои (запрещённые) "
          "_links молча гасятся, а не читаются как ссылка (правило 4)")
    c = fresh()
    put(c, 30, {"_scope": "voice", "engine": "lumean", "_links": {"images": 99}})
    r = preset_links.resolve(c, 30)
    check("своя часть в _scope", r["_scope"], "voice")
    check("своё поле на месте", r["engine"], "lumean")
    check("собственные _links частичного не резолвятся как ссылка", r["_links"], {})
    check("проблем нет — резолвится сам частичный, а не то, на что он (незаконно) сослался",
          r["_link_problems"], [])

    print("\npub_engine.channel_presets: «Сценарий» и «Обложка» — отдельные части")
    c = fresh()
    put(c, 20, {"_scope": "text", "preset_rewrite": "Из частичного"})
    put(c, 21, {"_scope": "cover", "preset_cover": "Обложка Х"})
    put(c, 1, {"preset_rewrite": "своё, но перекрыто ссылкой", "preset_cover": "своя обложка",
               "_links": {"text": 20, "cover": 21}})
    check("каждое поле — из своего частичного пресета",
          pub_engine.channel_presets(c, 1, "старый рерайт", "старая обложка"),
          ("Из частичного", "Обложка Х"))

    # Главное ради чего делили: обложку можно подменить, не трогая текст.
    c = fresh()
    put(c, 21, {"_scope": "cover", "preset_cover": "Обложка Х"})
    put(c, 1, {"preset_rewrite": "свой рерайт", "_links": {"cover": 21}})
    check("ссылка только на обложку не трогает пресет рерайта целого",
          pub_engine.channel_presets(c, 1, "старый рерайт", "старая обложка"),
          ("свой рерайт", "Обложка Х"))

    # Исчезнувшая часть: пресетов с ней нет, но если такая запись появится, она обязана
    # быть видна как проблема ссылки, а не потеряться молча.
    c = fresh()
    put(c, 1, {"preset_rewrite": "своё", "_links": {"text_cover": 20}})
    r = preset_links.resolve(c, 1)
    check("ссылка на разделённую часть — в _link_problems",
          [(x["part"], "разделена" in x["why"]) for x in r["_link_problems"]],
          [("text_cover", True)])
    check("свои поля целого при этом на месте", r["preset_rewrite"], "своё")

    c = fresh()
    put(c, 1, {"preset_rewrite": "своё"})
    check("без ссылки — поведение как раньше (test_preset_binding.py не задет)",
          pub_engine.channel_presets(c, 1, "старый рерайт", "старая обложка"),
          ("своё", "старая обложка"))

    print("\nPubServices._preset_settings зовёт preset_links.resolve, а не читает сама "
          "(server/pub_services.py ~813, потребитель фазы 1)")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean"})
    put(c, 1, {"engine": "edge", "_links": {"voice": 10}})
    svc = pub_services.PubServices({"create_project": lambda *a, **k: "",
                                    "enqueue": lambda *a, **k: None,
                                    "project_dir": lambda *a, **k: ""})
    ps = svc._preset_settings(c, 1)
    check("группа взята из частичного пресета", ps.get("engine"), "lumean")
    check("метаданные ссылки видны потребителю (сам решает, что с ними делать)",
          ps.get("_links"), {"voice": 10})

    c = fresh()
    put(c, 1, {"engine": "edge", "_links": {"voice": 404}})
    ps = svc._preset_settings(c, 1)
    check("сломанная ссылка видна потребителю как _link_problems", ps.get("_link_problems"),
          [{"part": "voice", "preset_id": 404,
            "why": "частичный пресет удалён или его настройки повреждены"}])

    print("\ndo_project_ready: сломанная ссылка -> warn в журнал выпуска, метаданные "
          "не уезжают в projects.settings (server/pub_services.py ~343)")
    c = fresh()
    c.executescript("""
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, project_id TEXT,
        tenant_id TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        service TEXT, kind TEXT, payload TEXT);
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT);
    """)
    put(c, 1, {"engine": "edge", "speed": "1.0", "_links": {"voice": 404}})
    c.execute("INSERT INTO pub_releases(release_id, project_id, tenant_id) "
              "VALUES('rel-x', NULL, 'dev')")
    c.execute("INSERT INTO pub_release_jobs(release_id, service, kind, payload) "
              "VALUES(?,?,?,?)",
              ("rel-x", "rewrite", "text", json.dumps({"text": "готовый текст ролика " * 20})))
    c.commit()

    captured = {}

    def fake_create_project(tenant_id, name, source_text="", settings=None):
        captured["tenant_id"] = tenant_id
        captured["settings"] = settings
        return "proj-x"

    svc2 = pub_services.PubServices({"create_project": fake_create_project,
                                     "enqueue": lambda *a, **k: None,
                                     "project_dir": lambda *a, **k: ""})
    rel = {"release_id": "rel-x", "tenant_id": "dev", "project_id": None, "lang": "ru",
           "preset_vsgen_id": 1, "ch_name": "Канал"}
    out = svc2.do_project_ready(c, rel)
    check("проект создан", out, {"payload": {"project_id": "proj-x"}})
    settings = captured["settings"]
    check("своё поле группы осталось — ссылка сломана", settings.get("engine"), "edge")
    check("служебные ключи (кроме _applied_preset) не уехали в проект",
          sorted(k for k in settings if str(k).startswith("_")), ["_applied_preset"])
    check("_applied_preset проставлен явно", settings.get("_applied_preset"), 1)
    check("предупреждение про сломанную ссылку записано в журнал выпуска уровнем warn",
          any("ссылка" in row[0] and row[1] == "warn" for row in
              c.execute("SELECT message, level FROM pub_release_log "
                        "WHERE release_id='rel-x'")),
          True)

    print("\ndo_project_ready: пустой пресет ('{}') по-прежнему пишет warn «настройки "
          "пресета не прочитались» (Issue 2 ревью цикла 1) — resolve() всегда добавляет "
          "_scope/_links/_link_problems, и проверка непустоты обязана идти уже после "
          "снятия этих служебных ключей, иначе warn перестаёт срабатывать")
    c = fresh()
    c.executescript("""
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, project_id TEXT,
        tenant_id TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        service TEXT, kind TEXT, payload TEXT);
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT);
    """)
    put(c, 1, {})   # целый пресет без единого настоящего поля
    c.execute("INSERT INTO pub_releases(release_id, project_id, tenant_id) "
              "VALUES('rel-empty', NULL, 'dev')")
    c.execute("INSERT INTO pub_release_jobs(release_id, service, kind, payload) "
              "VALUES(?,?,?,?)",
              ("rel-empty", "rewrite", "text", json.dumps({"text": "готовый текст ролика " * 20})))
    c.commit()
    captured_empty = {}

    def fake_create_project_empty(tenant_id, name, source_text="", settings=None):
        captured_empty["settings"] = settings
        return "proj-empty"

    svc3 = pub_services.PubServices({"create_project": fake_create_project_empty,
                                     "enqueue": lambda *a, **k: None,
                                     "project_dir": lambda *a, **k: ""})
    rel_empty = {"release_id": "rel-empty", "tenant_id": "dev", "project_id": None,
                 "lang": "ru", "preset_vsgen_id": 1, "ch_name": "Канал"}
    svc3.do_project_ready(c, rel_empty)
    check("проект всё равно уходит только с _applied_preset",
          sorted(captured_empty["settings"].keys()), ["_applied_preset"])
    check("warn «настройки пресета не прочитались» записан",
          any("не прочитались" in row[0] and row[1] == "warn" for row in
              c.execute("SELECT message, level FROM pub_release_log "
                        "WHERE release_id='rel-empty'")),
          True)

    print("\nСтоп-правило про `_`-ключи: сохранение формы кабинетом "
          "(POST /api/presets/{prid}/edit гоняет settings через _canonical_image_params "
          "и _normalize_lumean_settings, main.py ~12513) их не выбрасывает")
    raw = {"_scope": "voice", "_links": {"voice": 12}, "engine": "edge", "model": "OPENAI"}
    canon = M._canonical_image_params(dict(raw))
    check("_scope пережил _canonical_image_params", canon.get("_scope"), "voice")
    check("_links пережил _canonical_image_params", canon.get("_links"), {"voice": 12})

    normalized = M._normalize_lumean_settings(canon)
    check("_scope пережил _normalize_lumean_settings (движок не lumean — ранний возврат)",
          normalized.get("_scope"), "voice")
    check("_links пережил _normalize_lumean_settings", normalized.get("_links"), {"voice": 12})

    lumean_raw = {"_scope": "voice", "_links": {}, "engine": "lumean",
                  "voice": "11111111-1111-1111-1111-111111111111",
                  "voice_uuid": "11111111-1111-1111-1111-111111111111"}
    lumean_out = M._normalize_lumean_settings(lumean_raw)
    check("_scope переживает и ветку с настоящей нормализацией Lumean",
          lumean_out.get("_scope"), "voice")
    check("_links переживает ветку Lumean", lumean_out.get("_links"), {})

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
