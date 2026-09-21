#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA фазы 3 батча «Пресеты целые и частичные»: раздел 4 «Интерфейс» (`web/index.html`).

Стенд для проверки интерфейса не нужен — как и в `tests/test_channel_settings_layout.py`,
разметка и логика JS проверяются статически по исходнику: файл не настоящий JS AST, но
верхнеуровневые функции в нём не вкладываются друг в друга, и грубого чтения текста
достаточно, чтобы поймать расхождение с текстом спецификации. То, что можно проверить
только в браузере (реальный DOM, клики, рендер), здесь не проверяется — для этого нужен
был бы отдельный браузерный стенд, которого в проекте нет.

Покрывает то, чего не покрывают другие тесты фазы 3:
- список пресетов разбит на «Целые»/«Частичные» (по частям), у каждого — «кто бежит»;
- заведение идёт внутри формы (`#/preset/new`), отдельного экрана «название + тип»
  нет, а сам тип — кнопки-группа на семь вариантов, а не выпадающий список;
- редактор частичного показывает только вкладку своей части;
- редактор целого: переключатель «Свои поля / Из частичного пресета», подпись
  «из пресета «X»» со ссылкой «править его →», поля связанной группы — `disabled`
  (значит, не уедут при сохранении — `form.read()` их пропускает);
- «Кто бежит»: обязательная фраза про заморозку уже поставленных задач, проекты —
  не больше 20 + счёт остальных, у частичного — ещё целые пресеты и каналы через них;
- `link_problems` — красная плашка (`.err`) на вкладке группы, предупреждающий значок
  на самой вкладке;
- нигде в файле нет `alert()`/`confirm()`/`prompt()`;
- инлайн-JS синтаксически корректен (`node --check`) — если `node` недоступен в PATH,
  эта одна проверка помечается пропущенной, остальные не зависят от `node`.
- `PART_TITLES` (интерфейс) не разошёлся с `PARTS` (`server/preset_fields.py`) —
  без этого «Только X» в кнопках типа пресета могло бы называть части иначе, чем
  сервер.

    .venv/bin/python tests/test_preset_ui_qa.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import preset_fields as PF                                        # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def function_body(src: str, signature: str) -> str:
    """Текст функции от строки с сигнатурой до строки с одинокой закрывающей `}» —
    тот же приём, что в `tests/test_channel_settings_layout.py`."""
    lines = src.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(signature)), None)
    if start is None:
        return ""
    out = []
    for ln in lines[start:]:
        out.append(ln)
        if ln == "}":
            break
    return "\n".join(out)


def main() -> int:
    index_html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()

    print("PART_TITLES интерфейса не разошёлся с PARTS сервера (иначе кнопки типа "
          "пресета называли бы части иначе, чем API и `preset_fields.py`)")
    m = re.search(r"const PART_TITLES = \[(.*?)\];", index_html, re.S)
    check("PART_TITLES найден в index.html", bool(m), True)
    ui_parts = re.findall(r"\['(\w+)', '([^']+)'\]", m.group(1)) if m else []
    check("PART_TITLES — пять частей, порядок и подписи как в preset_fields.PARTS",
          ui_parts, list(PF.PARTS))

    print("\nСписок пресетов (viewPresets): «Целые»/«Частичные», частичные — по частям")
    vsgen_body = function_body(index_html, "  const vsgenBodyHtml = items => {")
    check("vsgenBodyHtml() найдена", bool(vsgen_body), True)
    check("заголовок «Целые»", "<div class=\"sec\">Целые</div>" in vsgen_body, True)
    check("заголовок «Частичные»", "<div class=\"sec\">Частичные</div>" in vsgen_body, True)
    check("частичные разбиваются подзаголовками по PART_TITLES (не одним общим списком)",
          "for (const [code, title] of PART_TITLES)" in vsgen_body, True)
    check("деление идёт по полю scope (whole = full, остальное — частичные)",
          "i.scope === 'full'" in vsgen_body, True)

    preset_item = function_body(index_html, "  const presetItemHtml = (g, i) => {")
    check("presetItemHtml() найдена", bool(preset_item), True)
    check("«кто бежит»: каналы", "`каналы: ${i.users.channels}`" in preset_item, True)
    check("«кто бежит»: проекты", "`проекты: ${i.users.projects}`" in preset_item, True)
    check("«кто бежит»: у частичного ещё «в пресетах» (только когда scope != full)",
          "i.scope !== 'full'" in preset_item and "`в пресетах: ${i.users.presets}`"
          in preset_item, True)
    # Ревью цикла 1 фазы 3, Issue 1: частичный пресет никогда не привязан к каналу
    # напрямую (канал ссылается на целый), поэтому «в работе»/точка-статус не могут
    # считаться по одному i.channels — иначе используемый через целый частичный
    # показывался бы серой точкой и «ни один канал им не пользуется».
    check("признак «в работе» учитывает использование через целые пресеты "
          "(viaPresets из i.users.presets у частичного), не только i.channels",
          "const viaPresets = i.scope !== 'full' && i.users ? i.users.presets : 0;"
          in preset_item, True)
    check("dot/inUse строится по i.channels.length ИЛИ viaPresets, не по одному "
          "i.channels",
          "const inUse = i.channels.length > 0 || viaPresets > 0;" in preset_item, True)
    check("текст «ни один канал им не пользуется» показывается только когда нет "
          "и прямых каналов, и viaPresets",
          "viaPresets > 0" in preset_item
          and "ни один канал им не пользуется" in preset_item, True)

    # 21.09.2026: отдельного экрана заведения (viewPresetNew) больше нет — он требовал
    # имя и тип ДО того, как человек что-нибудь настроил, то есть держал настройки за
    # двумя переходами (жалоба владельца «имя — как будто уже настройка пресета»).
    # Выбор типа при этом никуда не делся и остался кнопками-группой: прежнее решение
    # владельца — «вариантов шесть, их надо видеть сразу» — в силе, просто он переехал
    # в саму форму, на `#/preset/new`.
    print("\nЗаведение — внутри формы (viewPreset при id === 'new'): тип кнопками-"
          "группой, отдельного экрана «название + тип» нет")
    view_preset = function_body(index_html, "async function viewPreset(id){")
    check("отдельного экрана заведения не осталось",
          "async function viewPresetNew(" in index_html, False)
    check("адреса `#/preset/new` в роутере отдельной строкой нет — это тот же редактор",
          "() => viewPresetNew()" in index_html, False)
    check("редактор знает про ещё не заведённый пресет",
          "const creating = String(id) === 'new';" in view_preset, True)
    check("форма для незаведённого берётся своей ручкой, а не по номеру",
          "'/ui/presets/vsgen/new/form?scope='" in view_preset, True)
    check("группа переключателей #scope-modes есть", 'id="scope-modes"' in view_preset, True)
    check("класс .modes (тот же виджет кнопок-группы, что и везде в пульте, "
          "не select/dropdown)", 'class="modes" id="scope-modes"' in view_preset, True)
    check("внутри блока типа пресета нет <select (это не выпадающий список)",
          "<select" not in view_preset.split('id="scope-modes"')[1].split("</div>")[0]
          if 'id="scope-modes"' in view_preset else True, True)
    check("кнопки типа показываются только пока пресета нет",
          "if (creating){" in view_preset, True)
    check("шесть кнопок «Только <часть>» строятся из PART_TITLES (та же карта частей, "
          "что и сервер)", "PART_TITLES.map(([code, title])" in view_preset, True)
    check("подпись кнопки — «Только <часть в нижнем регистре>»",
          "Только ${esc(title.toLowerCase())}" in view_preset, True)
    check("заведение отправляет выбранный тип на сервер",
          "post('/ui/presets/vsgen', {name: nm, scope: draft.scope})" in view_preset, True)
    # Главное в жалобе: настройки открываются сразу, а имя спрашивают в момент записи и
    # говорят об этом у самого поля. Тост поверх экрана не показывает, где поправить.
    check("пустое имя останавливает только запись, и отказ показан у поля",
          "nameErr.hidden = false;" in view_preset
          and "why.inplace = true;" in view_preset, True)

    print("\nРедактор частичного (viewPreset): показана только вкладка своей части "
          "(раздел 4: «Только вкладка своей части»)")
    check("viewPreset() найдена", bool(view_preset), True)
    check("группы частичного отфильтрованы по d.scope === g.part",
          "d.scope === 'full' ? d.groups\n"
          "                                     : d.groups.filter(g => g.part === d.scope)"
          in view_preset, True)
    # Ссылку на частичный некуда писать, пока пресета нет: ручка `/links` адресуется
    # номером. Поэтому у ещё не заведённого переключателя быть не должно.
    check("переключатель ссылок вешается только у целого и только у заведённого",
          "if (d.scope === 'full' && !creating){" in view_preset, True)
    # Ревью цикла 1 фазы 3, Issue 2: клик по переключателю меняет вид сразу (до ответа
    # сервера), поэтому при неудачной записи ссылки экран обязан перерисоваться по
    # факту (render()) и вне try, а не только при успехе — иначе переключатель после
    # ошибки показывает несуществующее состояние, а ссылка на самом деле не менялась.
    m = re.search(r"formGroups\.onLinkChange = async \(part, presetId\) => \{"
                  r"(.*?)\n    \};", view_preset, re.S)
    check("onLinkChange найден внутри viewPreset", bool(m), True)
    link_change = m.group(1) if m else ""
    check("render() после записи ссылки вызывается один раз БЕЗ условия успеха — "
          "вне try/catch, значит выполняется и при ошибке",
          re.search(r"catch\(e\)\{\s*toast\(e\.message\);\s*\}\s*await render\(\);",
                     link_change) is not None, True)

    print("\nРедактор целого — переключатель «Свои поля / Из частичного пресета» "
          "(linkBarHtml)")
    link_bar = function_body(index_html, "function linkBarHtml(g){")
    check("linkBarHtml() найдена", bool(link_bar), True)
    check("кнопка «Свои поля»", ">Свои поля</button>" in link_bar, True)
    check("кнопка «Из частичного пресета»", ">Из частичного пресета</button>" in link_bar, True)
    check("подпись при заблокированной группе: «из пресета «X»»",
          "Поля только для чтения — из пресета «${esc(g.link_name)}»" in link_bar, True)
    check("ссылка «Править его →» на связанный частичный пресет",
          "Править его →" in link_bar, True)
    check("`.err` (красная плашка) — только когда есть g.link_problem",
          '${g.link_problem\n    ? `<div class="err"' in link_bar, True)
    check("нет частичных этой части — предупреждение «сначала заведите один»",
          "Частичных пресетов этой части нет" in link_bar, True)

    field_form = function_body(index_html, "function fieldForm(groups){")
    check("fieldForm() найдена", bool(field_form), True)
    check("linkBar рисуется только у групп с частью и активным колбэком (не у формы "
          "проекта и не у частичного пресета — там onLinkChange не передают)",
          "(g.part && onLinkChange) ? linkBarHtml(g) : ''" in field_form, True)
    check("поля заблокированной группы получают disabled (select)",
          "(f.off || locked) ? ' disabled' : ''" in field_form, True)
    check("текстовые поля заблокированной группы тоже disabled",
          "`<input id=\"${id}\" data-key=\"${esc(f.key)}\" value=\"${esc(f.value)}\"\n"
          "                 placeholder=\"${esc(f.placeholder)}\"${off}${locked ? ' disabled' "
          "'\"} '\">`" in field_form
          or "${off}${locked ? ' disabled' : ''}>`" in field_form, True)
    check("клик «Свои поля» шлёт снятие ссылки, только если она реально стояла "
          "(data-hadlink) — просто переключение вида без записи ссылку не трогает",
          "if (ownBtn.dataset.hadlink) onLinkChange(part, null);" in field_form, True)
    check("значение заблокированного поля не стирается в разметке (это то же `value=` "
          "выражение, что и у обычного поля — просто рядом добавлен disabled)",
          'value="${esc(f.value)}"' in field_form, True)
    check("form.read() пропускает disabled-поля — связанная группа не уедет на "
          "сохранение как «свои поля»",
          "if (!el.disabled) values[el.dataset.key] = el.value;" in field_form, True)

    print("\n«Кто бежит» (presetUsersHtml): обязательная фраза про заморозку задач, "
          "проекты — не больше 20 + остаток, у частичного — целые пресеты и каналы через "
          "них")
    users_html = function_body(index_html, "function presetUsersHtml(scope, users){")
    check("presetUsersHtml() найдена", bool(users_html), True)
    check("обязательная фраза про заморозку присутствует дословно",
          "Уже поставленные задачи правка не\n      меняет — их настройки заморожены "
          "в момент запуска. Изменится со следующего\n      запуска." in users_html, True)
    check("проекты обрезаются до 20", "projects.slice(0, 20)" in users_html, True)
    check("остаток проектов подписан «и ещё N»", ", и ещё ${rest}" in users_html, True)
    check("блок «Целые пресеты, ссылающиеся на этот» — только у частичного (scope != full)",
          "scope === 'full' ? '' : `" in users_html
          and "Целые пресеты, ссылающиеся на этот" in users_html, True)
    check("блок «Каналы через них» — тоже только у частичного",
          "Каналы через них" in users_html, True)

    # 21.09.2026, свёртка фильтра в один список с множественным выбором: форма
    # запомненного состояния сменилась с `{q, type}` на `{q, types: []}`. Старую форму
    # сторож обязан читать — иначе у каждого, кто уже пользовался экраном, фильтр молча
    # слетел бы в «Все»; и обязан сверять коды со списком — иначе внутрь проехал бы
    # мусор из ключа зрителя (в том числе вид, которого больше нет).
    print("\nФильтр: смена формы ключа в localStorage не теряет прежний выбор")
    reader = index_html.split("const presetFilter = (() => {")[1].split("})();")[0] \
        if "const presetFilter = (() => {" in index_html else ""
    check("сторож фильтра найден", bool(reader), True)
    check("старая форма {q, type} читается как набор из одного вида",
          "typeof v.type === 'string' ? [v.type]" in reader, True)
    check("новая форма {q, types} читается как есть",
          "Array.isArray(v.types) ? v.types" in reader, True)
    check("коды сверяются со списком видов",
          "raw.filter(c => PRESET_TYPE_TITLE.has(c))" in reader, True)
    check("«Все» в карту подписей не попадает — это пустой набор, а не вид",
          "PRESET_FILTER_TYPES.filter(([c]) => c !== 'all')" in index_html, True)

    print("\nПроблемы ссылок: предупреждающий значок на вкладке (viewPreset)")
    check("вкладка с link_problem получает значок ⚠",
          "const warn = g && g.link_problem ? ' ⚠' : '';" in view_preset, True)

    print("\nНикаких alert()/confirm()/prompt() — только toast/askInline/confirmInline "
          "(во всём инлайн-JS, не только в новом коде фазы 3)")
    # Только сам JS (не CSS/HTML — там в комментариях тоже упоминается alert()/confirm()/
    # prompt() как объяснение, почему от них отказались), и без строк-комментариев JS
    # (`//`) — по той же причине: объясняющие комментарии не запрещены, запрещены вызовы.
    script_only = re.search(r"<script>(.*?)</script>", index_html, re.S)
    js_text = script_only.group(1) if script_only else ""
    js_code_lines = [ln for ln in js_text.splitlines() if not ln.strip().startswith("//")]
    calls = re.findall(r"\b(alert|confirm|prompt)\s*\(", "\n".join(js_code_lines))
    check("вызовов alert(/confirm(/prompt( в исполняемом JS нет (комментарии не считаются)",
          calls, [])
    check("toast используется (хотя бы где-то)", "function toast(" in index_html, True)
    check("askInline используется (хотя бы где-то)", "function askInline(" in index_html, True)
    check("confirmInline используется (хотя бы где-то)",
          "function confirmInline(" in index_html, True)

    print("\nИнлайн-JS `web/index.html` синтаксически корректен (node --check)")
    node = shutil.which("node")
    if not node:
        print("  ПРОПУЩЕНО  node не найден в PATH — проверка синтаксиса не выполнена")
    else:
        scripts = re.findall(r"<script>(.*?)</script>", index_html, re.S)
        check("ровно один инлайн <script>-блок в index.html (тот же приём вырезания, "
              "что описан в задании фазы 3)", len(scripts), 1)
        tmpdir = tempfile.mkdtemp(prefix="preset-ui-qa-")
        try:
            js_path = os.path.join(tmpdir, "inline.js")
            with open(js_path, "w", encoding="utf-8") as fh:
                fh.write(scripts[0] if scripts else "")
            proc = subprocess.run([node, "--check", js_path],
                                  capture_output=True, text=True)
            check("node --check инлайн-JS", proc.returncode, 0)
            if proc.returncode != 0:
                print("        " + proc.stderr.strip().replace("\n", "\n        "))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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
