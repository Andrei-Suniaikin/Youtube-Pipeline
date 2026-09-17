#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресеты целые и частичные — одно разрешение на всех (батч «Пресеты целые и частичные»).

Решение владельца: пресет производства можно завести **целым** (все пять групп) или
**частичным** — только под одну часть: озвучку, картинки, клипы, сборку, текст и
обложку. Это одна и та же строка таблицы `presets` — у частичного просто заполнена
одна группа, а служебные ключи `_scope`/`_links` в `presets.settings` (см.
`server/preset_fields.py:PARTS`) говорят, какая часть и на что она сослалась. Новой
таблицы и новой колонки — нет.

Модель метаданных (внутри `presets.settings`, ключи с `_` — кабинет их и так выбрасывает
при переносе настроек в проект, `main.py:_preset_project_payload`):

    {"_scope": "voice", ...}                 — частичный пресет части "voice"
    {"_links": {"voice": 12, "images": 7}}   — целый пресет: часть → id частичного

Правило 3 архитектуры (не поле за полем): если у группы стоит ссылка, все поля группы
берутся из частичного пресета целиком, а свои поля этой группы у целого игнорируются —
так же, как `.env` побеждает базу целиком у сроков хранения (`pub_retention.py`), а не
объединяется с ней построчно. Смешение источников дало бы настройку, которую никто не
задавал.

Правило 4: ссылок на ссылки нет. Частичный пресет не может сам на что-то ссылаться —
это закрывает циклы по построению, а не проверкой обхода. `resolve()` поэтому не
рекурсивный: у поля `_links` целого читается ровно один частичный пресет на часть, и
дальше в нём самом `_links` не ищутся вовсе.

Одна функция отвечает на вопрос «чем делается ролик по этому пресету» — `resolve()`.
Все потребители (создание проекта, применение пресета, выпуск, форма пульта) обязаны
звать её, а не собирать настройки сами: разойдясь, они начнут показывать или
производить не то, что задумано (правило 2 архитектуры).
"""
from __future__ import annotations

import json

import preset_fields

FULL = "full"
PARTS = tuple(part for part, _title in preset_fields.PARTS)


def scope_of(settings: dict) -> str:
    """`"full"` или часть, записанная в `_scope`.

    Отсутствие ключа — целый пресет: пресеты, заведённые до этого батча, метаданных не
    имеют и обязаны продолжать работать как целые. Незнакомое значение — тоже целый:
    молча превращать чужую опечатку в частичный пресет с невидимыми группами опаснее,
    чем показать все поля.
    """
    s = settings if isinstance(settings, dict) else {}
    scope = str(s.get("_scope") or "").strip().lower()
    return scope if scope in PARTS else FULL


def group_keys(scope: str) -> set:
    """Ключи полей группы этой части. Для `full` (и для незнакомой части) — пусто: у
    целого пресета нет одной-единственной группы, разрешение работает по каждой части
    `_links` отдельно."""
    return preset_fields.part_group_keys(scope) if scope in PARTS else set()


def parse_link_ref(raw):
    """Число id частичного пресета из значения `_links.<часть>`, или `None`, если
    ссылка сломана.

    Правило: только целое число или строка из цифр — так пишет новый код (фаза 2,
    Minor 5 ревью цикла 2 требует приводить к `int` при записи и отвергать
    произвольную строку), а старые данные до этого правила бывают числом-строкой
    ("10"). Объект или список (Issue 3 ревью цикла 3) — тоже сломанная ссылка, а
    не то, что можно молча передать в SQL: `sqlite3.ProgrammingError` на таком
    значении ронял `resolve()` и вместе с ним продвижение выпуска.

    Публичная функция (Issue 1 ревью цикла 2 фазы 2): один разбор на `resolve()`,
    `users_of()` и пульт (`web/app.py`, отображение `link_id`/`link_name`) — иначе
    у формы и у разрешения были бы разные мнения о том, что считать числом ссылки.
    """
    if isinstance(raw, bool):
        return None                # bool — подкласс int в Python, но тут это не id
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


# Обратная совместимость: `tests/test_preset_links.py` (фаза 1) зовёт разбор по
# прежнему приватному имени — функция та же, имя публичное добавлено в фазе 2.
_parse_link_ref = parse_link_ref


def partial_fits_part(settings: dict, part: str) -> bool:
    """True, если частичный пресет с настройками `settings` пригоден быть ссылкой
    под часть `part`: его `_scope` — ровно `part`, и у него самого нет ссылок
    (правило 4 — ссылок на ссылки нет).

    Публичная функция (Issue 1 ревью цикла 2 фазы 2): та же проверка, что делает
    `resolve()` при разрешении ссылки, нужна пульту дважды — при выборе варианта
    (`_partial_options_by_part`) и при записи ссылки (`ui_preset_link_set`). Раньше
    оба места в `web/app.py` повторяли условие самостоятельно; теперь оно одно.
    """
    if not isinstance(settings, dict):
        return False
    if scope_of(settings) != part:
        return False
    own_links = settings.get("_links")
    return not (isinstance(own_links, dict) and own_links)


def _load_row(c, preset_id):
    """`{"tenant_id":.., "settings": dict}` пресета, или `None` — пресета нет / битый
    JSON / не объект. `None` — сигнал «прочитать не удалось», отдельный от валидного
    `{}`.

    Тенант читается вместе с настройками, а не отдельным запросом: `resolve()` обязан
    сверять тенант частичного пресета с тенантом целого (иначе чужая ссылка, вписанная
    в обход проверок API, молча утащит чужие настройки в производство), и без тенанта
    в этой же строке пришлось бы читать пресет дважды.
    """
    if not preset_id:
        return None
    row = c.execute(
        "SELECT tenant_id, settings FROM presets WHERE id=?", (preset_id,)
    ).fetchone()
    if not row:
        return None
    raw = row["settings"]
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
    return {"tenant_id": row["tenant_id"], "settings": data}


def resolve(c, preset_id) -> dict:
    """Настройки для производства: свои поля целого пресета, с группами, у которых
    стоит ссылка, заменёнными группой частичного пресета целиком.

    Пресета нет / битый JSON — как если бы пресет был пустым целым (все поля
    умолчальные), а не отказ: производство не должно падать из-за чужой уборки.

    Возвращает словарь с полями + служебными `_scope`, `_links`, `_link_problems` —
    что с ними делать, решает потребитель (обычный путь — выбросить их, как это уже
    делает `_preset_project_payload`).
    """
    row = _load_row(c, preset_id)
    settings = row["settings"] if row else {}
    tenant_id = row["tenant_id"] if row else None
    scope = scope_of(settings)
    out = dict(settings)
    problems = []
    links = settings.get("_links") if scope == FULL else None
    if not isinstance(links, dict):
        links = {}
    for part, linked_raw in links.items():
        if part not in PARTS:
            continue
        keys = group_keys(part)
        linked_id = parse_link_ref(linked_raw)
        partial_row = None
        why = ""
        if linked_id is None:
            # Объект, список или произвольная строка вместо числа (Issue 3 ревью
            # цикла 3) — тот же класс сломанной ссылки, что и удалённый пресет, а
            # не повод передавать мусор в SQL и падать `sqlite3.ProgrammingError`.
            why = "ссылка повреждена"
        else:
            partial_row = _load_row(c, linked_id)
            if partial_row is None:
                why = "частичный пресет удалён или его настройки повреждены"
            elif tenant_id is not None and partial_row["tenant_id"] != tenant_id:
                # Тенант целого известен (пресет реально прочитался) и не совпадает с
                # тенантом частичного — записанная в обход API ссылка на чужой пресет.
                # «Одноместное разрешение» обязано само отвечать на вопрос «чем делается
                # ролик», а не полагаться на то, что чужого id туда никто не впишет.
                why = "частичный пресет другого тенанта"
            elif scope_of(partial_row["settings"]) != part:
                why = "частичный пресет не той части"
            elif not partial_fits_part(partial_row["settings"], part):
                # Тут `scope_of` уже совпал с `part` (ветка выше), значит единственная
                # причина, по которой `partial_fits_part` может отказать — собственные
                # ссылки частичного (правило 4). Общая функция вместо повтора условия.
                why = "у частичного пресета есть свои ссылки — так нельзя"
        if why:
            # Ссылка сломана — молча подставлять нельзя: группа остаётся со своими
            # полями (они и так ещё в `out`, потому что `out` начат копией `settings`),
            # а причину кладём в `_link_problems` — потребитель сам решит, что сказать.
            problems.append({"part": part, "preset_id": linked_raw, "why": why})
            continue
        partial = partial_row["settings"]
        for k in keys:
            out.pop(k, None)
        for k in keys:
            if k in partial:
                out[k] = partial[k]
    out["_scope"] = scope
    out["_links"] = links
    out["_link_problems"] = problems
    return out


def users_of(c, preset_id) -> dict:
    """Кто бежит на этом пресете — для карточки «кто бежит».

    `channels` — каналы, у которых `pub_channels.preset_vsgen_id = id`.
    `projects` — проекты, у которых в `settings._applied_preset` лежит этот id.
    `presets`  — только для частичного: целые пресеты, у которых он есть в `_links`.
    `via`      — только для частичного: каналы, бегущие на тех целых пресетах.
                 Транзитивно, но на один уровень — ссылок на ссылки не бывает
                 (правило 4), дальше идти некуда.

    Всё ограничено тенантом самого пресета — без этого id, случайно совпавший у
    другого тенанта (или дописанный в `_links` в обход API), показал бы в карточке
    чужие каналы и проекты. Проекты отбираются фильтром в самом SQL
    (`json_extract`), а не разбором каждой строки таблицы в Python — иначе список
    пресетов пульта (фаза 2, по одному вызову `users_of` на пресет) на каждой
    загрузке заново парсил бы JSON всех проектов и всех пресетов тенанта.
    """
    out = {"channels": [], "projects": [], "presets": [], "via": []}
    # Разбор тем же `parse_link_ref`, что и `resolve()` (Issue 1 ревью цикла 4):
    # id может прийти строкой (из URL пульта, фаза 2), а держателей в `_links`
    # `resolve()` находит по числу — если здесь сравнивать со строкой как есть,
    # `users_of("10")` и `users_of(10)` расходятся в поле `presets`.
    preset_id = parse_link_ref(preset_id)
    if preset_id is None:
        return out
    row = _load_row(c, preset_id)
    if row is None:
        return out
    tenant_id = row["tenant_id"]
    scope = scope_of(row["settings"])

    out["channels"] = [
        {"id": r["id"], "name": r["name"]}
        for r in c.execute(
            "SELECT id, name FROM pub_channels WHERE tenant_id=? AND preset_vsgen_id=?",
            (tenant_id, preset_id),
        ).fetchall()
    ]

    # `json_valid(...) AND json_extract(...)` — не «на всякий случай»: `json_extract`
    # на настоящем мусоре в `settings` (пресет с `settings='не json'`, Issue 1 ревью
    # цикла 1 показал, что такие строки в production реальны) не возвращает NULL, а
    # роняет весь запрос `OperationalError: malformed JSON`. `json_valid` идёт первым
    # в том же AND и отсекает такие строки до того, как до них доберётся `json_extract`
    # — SQLite вычисляет операнды AND слева направо и по короткому замыканию.
    out["projects"] = [
        {"id": r["id"], "name": r["name"]}
        for r in c.execute(
            "SELECT id, name FROM projects WHERE tenant_id=? "
            "AND settings IS NOT NULL AND settings != '' AND json_valid(settings) "
            "AND CAST(json_extract(settings,'$._applied_preset') AS TEXT)=CAST(? AS TEXT)",
            (tenant_id, preset_id),
        ).fetchall()
    ]

    if scope != FULL:
        # Держателем может быть только ссылка ПОД ТОЙ ЖЕ частью, что и `_scope`
        # этого частичного пресета — `resolve()` смотрит именно `_links.<scope>`,
        # а не любой из пяти ключей (Issue 4 ревью цикла 3: раньше здесь искали по
        # ЛЮБОЙ из пяти частей через OR, и частичный «voice», на который где-то
        # по ошибке сослались как на `_links.images`, попадал в карточку «кто
        # бежит», хотя `resolve()` такую ссылку никогда не примет).
        #
        # `CAST(... AS TEXT)` с обеих сторон — тем же приёмом, что уже используется
        # выше для `_applied_preset` (Minor 5 ревью цикла 2): `_links` могут хранить
        # ссылку и числом, и строкой (легаси-данные до того, как запись стала строго
        # числовой в фазе 2), а `json_extract`/`?` тут — оба выражения без колоночной
        # affinity, поэтому без приведения INTEGER 12 и TEXT "12" не совпали бы, хотя
        # `resolve()` через `id=?` (где affinity есть у самой колонки `id`) находит
        # частичный пресет по такой ссылке. Без этого приведения `users_of` и
        # `resolve()` расходились бы в разборе одной и той же ссылки.
        #
        # SQL-условие ниже — только предварительный отбор кандидатов (сужает
        # полный скан тенанта), а не окончательный ответ: объект/список в
        # `_links.<part>` тоже пройдёт CAST(...AS TEXT) не упав, но не совпадёт по
        # значению почти никогда, и на этот случай ниже стоит второй, уже питоновский
        # разбор через `parse_link_ref` — тем же кодом, что и `resolve()` (Issue 4).
        holder_rows = c.execute(
            "SELECT id, name, settings FROM presets WHERE tenant_id=? "
            "AND settings IS NOT NULL AND settings != '' AND json_valid(settings) "
            f"AND CAST(json_extract(settings,'$._links.{scope}') AS TEXT)=CAST(? AS TEXT)",
            (tenant_id, preset_id),
        ).fetchall()
        holders = []
        for r in holder_rows:
            try:
                hs = json.loads(r["settings"]) if r["settings"] else {}
            except (ValueError, TypeError):
                continue
            if not isinstance(hs, dict):
                continue
            if scope_of(hs) != FULL:
                # Держателем считается только ЦЕЛЫЙ пресет (правило 4: у
                # частичного своих ссылок нет и не может быть) — `resolve()`
                # игнорирует `_links` не-целого пресета целиком, значит и здесь
                # такой пресет не в счёт, даже если в его JSON завалялся мусорный
                # `_links` (запрещённый, но не проверяемый на записи до фазы 2).
                continue
            if parse_link_ref((hs.get("_links") or {}).get(scope)) != preset_id:
                # Сама ссылка сломана (объект/список/не то значение) — resolve()
                # её тоже не примет, значит и в «кто бежит» её не показываем.
                continue
            holders.append({"id": r["id"], "name": r["name"]})
        out["presets"] = holders
        holder_ids = [h["id"] for h in holders]
        if holder_ids:
            placeholders = ",".join("?" for _ in holder_ids)
            out["via"] = [
                {"id": r["id"], "name": r["name"]}
                for r in c.execute(
                    "SELECT id, name FROM pub_channels "
                    f"WHERE tenant_id=? AND preset_vsgen_id IN ({placeholders})",
                    (tenant_id, *holder_ids),
                ).fetchall()
            ]

    return out
