#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA фазы 4 батча C «экран проекта»: раздел 4 «Интерфейс» (`web/index.html`).

Статический тест, стенд не нужен — по образцу `tests/test_preset_ui_qa.py`: разметка
и логика JS проверяются грубым чтением исходника (`function_body` вырезает функцию по
сигнатуре до одинокой закрывающей `}»), а не браузерным DOM. То, что можно проверить
только в браузере (реальный рендер, клики, сетевые ответы), здесь не проверяется — для
этого нужен отдельный браузерный стенд, которого в проекте нет; интеграционная
проверка контрактов ручек — в `tests/test_project_screen_api.py` (Ф2).

Покрывает:
- `PROJECT_TABS` — ровно шесть вкладок `scenario,tts,images,video,render,cover`, в этом
  порядке (контракт роутинга `#/project/{pid}/{tab}`);
- отдельного маршрута `.../settings` для проекта в `ROUTES` больше нет;
- ссылок `#/text`/`#/covers` внутри функций экрана проекта (`viewProject`,
  `renderScenarioTab`, `renderCoverTab`, `renderStageTab`) больше нет — сами эти роуты
  в `ROUTES` остаются достижимы по прямому хешу;
- панель пресета: список строится только по `scope === 'full'`, «Применить» спрашивает
  подтверждение через `confirmInline` с явным текстом про замену настроек целиком,
  «×» только чистит выбор в списке (без похода в сеть), «Снимок» проверяет непустое
  имя на клиенте до отправки, «пресет удалён» показывается при `applied.missing`;
- старт этапа: все пять исходов (`started`/`duplicate`/`images_busy`/
  `render_deferred`/`video_deferred`) различаются словами, «поставлено» — только у
  `started`;
- лог задачи: опрос останавливается и при сворачивании блока (`stopJobLogPoll` в
  `toggleJobLog`), и при уходе со страницы (`clearAllLogTimers` в `render()`);
- «Сделать всё» — ровно один блок на экране;
- нигде в файле нет `alert()`/`confirm()`/`prompt()`;
- инлайн-JS синтаксически корректен (`node --check`), если `node` доступен в PATH.

Регрессия на замечания ревью цикла 2 (`review-feedback-F4.md`, удалён после проверки, но
дефекты — платные и легко возвращаются при следующей правке файла, поэтому закрепляем
статическим тестом, а не полагаемся на память):
- Issue 1 (Critical, деньги): «Запустить» и во вкладке проекта «Обложка», и на `#/covers`
  берёт `provider`/`model` из выбора на МОМЕНT нажатия (`cd.options[st.option]` /
  `d.options[COVER_FORM.option]` внутри обработчика клика), не из значения, посчитанного
  один раз при отрисовке экрана;
- Issue 2: `paintFields()` вкладки «Обложка» проекта берёт `langs` заново при каждом вызове;
- Issue 3: исход «Начать» переживает отложенную перерисовку экрана (`STAGE_OUTCOME_STATE`,
  сверка `location.hash` перед отложенным `viewProject()`);
- Issue 4: списки задач вкладок «Сценарий»/«Обложка» честно подписаны как «по аккаунту, не
  по проекту», задача, созданная тут же, — первой, опрос продолжается, пока есть
  незавершённая, готовая раскрывается кликом по строке;
- Issue 5: `actions()`/`wireActions()` (отменить/повторить), прогресс и «с какими
  настройками пошла» — в строке задачи; «Во что обошёлся» — один раз в шапке; «Прочие
  задачи проекта» (виды вне `TAB_KINDS`) — на вкладке «Сценарий»;
- Issue 6: содержимое `fileCard`/карточки «Во что обошёлся» — в `.secbody`, не вплотную к
  рамке;
- Issue 7: опрос лога не запускает тик поверх ещё не ответившего (`busy`-флаг), сетевая
  ошибка тика не стирает уже показанный лог и не глушит опрос, `deleted` — терминальный
  статус;
- Issue 8: пустой блок «Результат» подписан заголовком, подпись режима обложки (`#cv-note`)
  и соотношение сторон видны сразу при первой отрисовке, не только после клика.

    .venv/bin/python tests/test_project_screen_ui_qa.py
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
    тот же приём, что в `tests/test_preset_ui_qa.py`/`tests/test_channel_settings_layout.py`."""
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

    print("PROJECT_TABS: ровно шесть вкладок, в порядке контракта роутинга "
          "#/project/{pid}/{tab}")
    m = re.search(r"const PROJECT_TABS = \[(.*?)\];", index_html, re.S)
    check("PROJECT_TABS найден в index.html", bool(m), True)
    tabs = re.findall(r"\['(\w+)',\s*'([^']+)'\]", m.group(1)) if m else []
    check("шесть вкладок в заданном порядке",
          [k for k, _t in tabs],
          ["scenario", "tts", "images", "video", "render", "cover"])

    print("\nОтдельного маршрута «.../settings» для проекта в ROUTES больше нет")
    routes = function_body(index_html, "const ROUTES = [")
    check("ROUTES найден", bool(routes), True)
    check("viewProjectSettings из ROUTES убран", "viewProjectSettings" not in routes, True)
    check("маршрут проекта — один (без ветки на endsWith('/settings'))",
          "endsWith('/settings')" not in routes, True)
    check("сама функция viewProjectSettings удалена из файла (не просто отвязан роут)",
          "function viewProjectSettings" not in index_html, True)

    print("\nСсылок #/text / #/covers внутри экрана проекта больше нет (сами роуты "
          "остаются достижимы по прямому хешу — проверяется отдельно)")
    view_project = function_body(index_html, "async function viewProject(id){")
    scenario_tab = function_body(index_html, "async function renderScenarioTab(d){")
    cover_tab = function_body(index_html, "async function renderCoverTab(d){")
    stage_tab = function_body(index_html, "async function renderStageTab(d, tab){")
    for name, body in (("viewProject", view_project), ("renderScenarioTab", scenario_tab),
                       ("renderCoverTab", cover_tab), ("renderStageTab", stage_tab)):
        check(f"{name}() найдена", bool(body), True)
        check(f"{name}(): нет ссылки на '#/text'", "'#/text'" in body, False)
        check(f"{name}(): нет ссылки на '#/covers'", "'#/covers'" in body, False)
    check("сам маршрут #/text в ROUTES остаётся (прямой хеш работает)",
          "h === '#/text'" in routes, True)
    check("сам маршрут #/covers в ROUTES остаётся (прямой хеш работает)",
          "h === '#/covers'" in routes, True)

    print("\nПанель пресета — только целые (scope === 'full'), «Применить» с "
          "подтверждением, «×» без сети, «Снимок» проверяет имя на клиенте")
    preset_panel_html = function_body(index_html, "function presetPanelHtml(applied, wholePresets){")
    wire_preset_panel = function_body(index_html, "function wirePresetPanel(card, pid){")
    check("presetPanelHtml() найдена", bool(preset_panel_html), True)
    check("wirePresetPanel() найдена", bool(wire_preset_panel), True)
    check("вызывающая сторона фильтрует список по scope === 'full' до передачи в панель",
          ".filter(i => i.scope === 'full')" in index_html, True)
    check("«пресет удалён» — при applied.missing",
          "applied.missing" in preset_panel_html and "пресет удалён" in preset_panel_html,
          True)
    check("строка про частичные — только когда есть applied.links",
          "(applied.links || []).length" in preset_panel_html, True)
    check("«Применить» спрашивает через confirmInline",
          "confirmInline(ev.currentTarget" in wire_preset_panel, True)
    check("текст подтверждения explicit про замену настроек ЦЕЛИКОМ",
          "целиком" in wire_preset_panel, True)
    check("«×» просто чистит select, без единого post()/api() в обработчике",
          re.search(r"#pp-clear'\)\.onclick = \(\) => \{ sel\.value = ''; \};",
                     wire_preset_panel) is not None, True)
    check("«Снимок» проверяет непустое имя на клиенте до похода в сеть",
          "if (!name){ toast('у пресета должно быть имя'); return; }" in wire_preset_panel,
          True)
    check("после применения экран перерисовывается (viewProject)",
          wire_preset_panel.count("await viewProject(pid);") >= 2, True)

    print("\nСтарт этапа: пять исходов различаются словами (принцип 3 — «поставлено» "
          "при дубле было бы ложью)")
    # STAGE_OUTCOME_TEXT — объект, а не функция: закрывается «};», а не одинокой «}»,
    # которую ищет function_body() (та рассчитана на функции) — вырезаем регуляркой.
    m_outcome = re.search(r"const STAGE_OUTCOME_TEXT = \{(.*?)\};", index_html, re.S)
    stage_outcome = m_outcome.group(1) if m_outcome else ""
    check("STAGE_OUTCOME_TEXT найден", bool(stage_outcome), True)
    for key in ("duplicate", "images_busy", "render_deferred", "video_deferred"):
        check(f"есть подпись для outcome={key}", f"{key}:" in stage_outcome, True)
    texts = re.findall(r"'([^']+)'", stage_outcome)
    check("все четыре подписи различны (не скопированы одна в другую)",
          len(set(texts)), len(texts))
    check("'started' обрабатывается отдельно, с номером задачи",
          "out.outcome === 'started'" in stage_tab and "out.job_id" in stage_tab, True)
    check("текст ошибки 400 — как есть от кабинета (e.message), без подмены",
          "outcomeBox.textContent = e.message;" in stage_tab, True)

    print("\n«Начать» шлёт текущие значения формы настроек вкладки (fieldForm(...).read()), "
          "а не пустое тело (поправка оркестратора к §5.4)")
    check("тело запроса строится из settingsForm.read()",
          "const settings = settingsForm ? settingsForm.read() : {};" in stage_tab, True)

    print("\nШапка проекта (ФР §4 п.1): пять сегментов статуса и подпись рисуются по "
          "project_stage_status (Ф1), не пересчитываются в пульте заново")
    stage_strip = function_body(index_html, "function stageStripHtml(stage){")
    check("stageStripHtml() найдена", bool(stage_strip), True)
    check("сегменты берутся из stage.segments (структура Ф1), не из своего расчёта",
          "stage.segments" in stage_strip, True)
    check("подпись — stage.caption как есть (одна правда о статусе, принцип 2 задания)",
          "stage.caption" in stage_strip, True)
    check("цвет подписи — по stage.tone через карту переменных пульта (STAGE_TONE_VAR)",
          "STAGE_TONE_VAR[stage.tone]" in stage_strip, True)
    check("шапка проекта вызывает stageStripHtml(p.stage) (сегменты видны на экране)",
          "stageStripHtml(p.stage)" in view_project, True)
    # Ссылка назад у экрана проекта — в том же виде, что у каналов, выпусков, очереди
    # и пресетов: «← ко всем …». Раньше здесь стояло «‹ Все проекты» — единственная
    # непохожая ссылка на весь пульт, и человек её просто не находил глазом, жал «назад»
    # в браузере и уезжал не туда (жалоба владельца 20.09.2026).
    check("шапка содержит «← ко всем проектам» → #/projects",
          'href="#/projects">← ко всем проектам</a>' in view_project, True)

    print("\nПорядок блоков production-вкладки (tts/images/video/render): Настройки "
          "этапа с «Начать» в заголовке → Результат → Лог задач")
    # 21.09.2026: отдельной карточки «Действие» больше нет. Заголовок «Действие» не
    # сообщал ничего — на вкладке «Озвучка» единственное действие и так озвучить, — а
    # рамка на одну кнопку и пустую строку исхода отодвигала настройки вниз на ~130 px
    # (жалоба владельца: «места много, смысла в рамке нет»). Это наше решение о
    # раскладке, а не контракт кабинета, поэтому маркер порядка правится вместе с кодом.
    check("отдельной карточки «Действие» в файле нет",
          '<div class="sec">Действие</div>' in index_html, False)
    # У вкладки «Обложка» карточка осталась — в ней настоящая форма платного заказа,
    # а не одна кнопка; там сменился только заголовок: он называет действие, а не
    # повторяет слово «Действие» над подписью «Сделать обложку».
    check("карточка обложки названа своим действием",
          '<div class="sec">Сделать обложку</div>' in cover_tab, True)
    check("«Начать» и строка исхода стоят в заголовке карточки настроек этапа",
          "sec.append(titleSpan, outcomeBox, goBtn);" in stage_tab, True)
    check("идентификаторы кнопки и строки исхода не переименованы "
          "(на них висит tests/test_stage_outcome_ttl_browser.py)",
          ("goBtn.id = 'stage-go';" in stage_tab,
           "outcomeBox.id = 'stage-outcome';" in stage_tab), (True, True))
    order_markers = ["app.append(box);", "// 3. Результат", "// 4. Лог задач"]
    positions = [stage_tab.find(m) for m in order_markers]
    check("все три маркера порядка найдены в renderStageTab", [p_ >= 0 for p_ in positions],
          [True] * len(positions))
    check("маркеры идут строго по возрастанию позиции (порядок блоков не перепутан)",
          positions == sorted(positions), True)

    print("\nНастройки этапа сохраняются сами — и только они (сохранение тут ничего не "
          "запускает и ни за что не платит)")
    check("кнопки «Сохранить» на вкладке этапа нет (saveBar не зовётся)",
          "saveBar(" in stage_tab, False)
    check("saveBar остаётся ровно у одного вызывающего — редактора пресета",
          index_html.count("app.append(saveBar("), 1)
    check("автозапись уходит в ту же ручку настроек, что была у кнопки",
          "/settings`, {values})" in stage_tab, True)
    # Форма не должна уметь ходить по платному адресу сама: от сохранения запрос
    # отличается ТОЛЬКО адресом, а кабинет по нему и настройки пишет, и ставит задачу.
    check("из автосохранения нет обращения к /stage/ (оно только в обработчике «Начать»)",
          stage_tab.count("/stage/${tab}`"), 1)
    check("пауза набора вынесена константой и лежит в 600–800 мс",
          600 <= int(re.search(r"const STAGE_SAVE_MS = (\d+);", index_html).group(1)) <= 800,
          True)
    check("правки слушаются на самой форме: набор, готовое значение и уход с поля",
          [f"settingsForm.addEventListener('{e}'" in stage_tab
           for e in ("input", "change", "focusout")], [True, True, True])
    check("Lumean-поля строгого формата ждут ухода с поля, а не паузы в наборе",
          "const STAGE_SAVE_ON_BLUR = new Set(['voice', 'voice_uuid']);" in index_html, True)
    check("уезжают только отличия от сохранённого (пустое тело ручка отвергает)",
          "const values = diff(saved);" in stage_tab, True)
    check("один запрос в полёте, следующий — очередью из последнего состояния",
          "if (inFlight){ again = true; return; }" in stage_tab, True)
    check("отказ не откатывает набранное и ставит сторож ухода",
          ("LEAVE_GUARD = {stageTab: skey" in stage_tab,
           "dirty: () => failReason != null" in stage_tab), (True, True))
    check("на серию отказов — один тост, а не по тосту на букву",
          "if (firstOfSeries) toast(e.message);" in stage_tab, True)
    # «Вернуть как было» при автосохранении обязано ПИСАТЬ: набранное уже на сервере, и
    # откат одной формы показывал бы человеку не то, что хранится.
    check("откат отправляет значения на момент входа на вкладку, а не только чистит форму",
          "Object.keys(diff(entry)).forEach(k => { values[k] = entry[k]; });"
          in stage_tab, True)

    print("\nГруппа настроек этапа (TAB_SETTINGS_GROUP) — те же 4 названия групп, что и "
          "у server/preset_fields.GROUPS (иначе fieldForm() тихо не найдёт группу — "
          "get('groups').find(...) даст undefined, форма настроек не появится)")
    m_tsg = re.search(r"const TAB_SETTINGS_GROUP = \{(.*?)\};", index_html, re.S)
    check("TAB_SETTINGS_GROUP найден", bool(m_tsg), True)
    tsg = dict(re.findall(r"(\w+):\s*'([^']+)'", m_tsg.group(1))) if m_tsg else {}
    pf_titles = {title for title, _fields in PF.GROUPS}
    check("группа вкладки «Озвучка» существует в preset_fields.GROUPS",
          tsg.get("tts") in pf_titles, True)
    check("группа вкладки «Картинки» существует в preset_fields.GROUPS",
          tsg.get("images") in pf_titles, True)
    check("группа вкладки «Клипы» существует в preset_fields.GROUPS",
          tsg.get("video") in pf_titles, True)
    check("группа вкладки «Сборка» существует в preset_fields.GROUPS",
          tsg.get("render") in pf_titles, True)
    check("у «Сценария» и «Обложки» своей группы настроек нет — §3: группы «Сценарий» "
          "и «Обложка» пресета в проект не идут",
          ("scenario" in tsg, "cover" in tsg), (False, False))
    check("«Сценарий» не строит fieldForm/settingsForm (нет блока «Настройки этапа»)",
          ("fieldForm(" in scenario_tab, "settingsForm" in scenario_tab), (False, False))
    check("«Обложка» не строит fieldForm/settingsForm (нет блока «Настройки этапа»)",
          ("fieldForm(" in cover_tab, "settingsForm" in cover_tab), (False, False))

    print("\nСценарий (ФР §4 п.5): «Взять в проект» подтверждает замену НЕПУСТОГО текущего "
          "текста через confirmInline, пустой заменяется без вопроса")
    check("текст задачи рерайта берётся POST /ui/text",
          "await post('/ui/text', {" in scenario_tab, True)
    check("«Взять в проект» кладёт текст в source.txt той же ручкой, что и «Сохранить текст»",
          re.search(r"await post\(`/ui/projects/\$\{encodeURIComponent\(pid\)\}/source`,"
                     r"\s*\{text\}\);", index_html) is not None, True)
    check("confirmInline спрашивается только когда текущий текст непустой (if (cur) ...)",
          "if (cur) confirmInline(btn, 'Заменить текущий текст проекта?', apply);"
          in index_html, True)
    check("пустой текущий текст заменяется без подтверждения (else-ветка)",
          "else await apply();" in index_html, True)

    print("\nОбложка (ФР §4 п.6): создание формой /ui/covers, варианты миниатюрами, "
          "«Поставить обложкой проекта» кладёт файл и перерисовывает экран")
    check("создание обложки — POST /ui/covers", "await post('/ui/covers', body);" in cover_tab,
          True)
    check("варианты — миниатюрами (<img ...>), не список ссылок",
          "<img src=\"${url(f)}\"" in index_html, True)
    check("кнопка «Поставить обложкой проекта» есть у каждого варианта",
          ">Поставить обложкой проекта</button>" in index_html, True)
    check("«Поставить обложкой» шлёт service_job_id/name в POST /ui/projects/{pid}/cover",
          re.search(r"await post\(`/ui/projects/\$\{encodeURIComponent\(pid\)\}/cover`,"
                     r"\s*\{service_job_id: jobId, name: btn\.dataset\.name\}\);", index_html)
          is not None, True)
    check("после успеха экран перерисовывается — новая обложка видна в «Результат»",
          "await viewProject(pid);         // перерисовать — новая обложка видна" in index_html,
          True)

    print("\nСписок задач вкладки с раскрывающимся логом — на КАЖДОЙ вкладке (ФР §4 п.7), "
          "не только на четырёх production-вкладках")
    check("renderStageTab добавляет jobsLogCard", "app.append(jobsLogCard(d, tab));"
          in stage_tab, True)
    check("renderScenarioTab добавляет jobsLogCard(d, 'scenario')",
          "app.append(jobsLogCard(d, 'scenario'));" in scenario_tab, True)
    check("renderCoverTab добавляет jobsLogCard(d, 'cover')",
          "app.append(jobsLogCard(d, 'cover'));" in cover_tab, True)
    check("«Сценарий» смотрит вид задачи prompts (таблица соответствия §5.5 исходной спеки)",
          "scenario: ['prompts']" in index_html, True)
    for tab_key, files in (("tts", "audio"), ("images", "images"), ("video", "clips")):
        check(f"TAB_FILES['{tab_key}'] содержит '{files}'",
              f"{tab_key}: ['{files}']" in index_html, True)
    check("TAB_FILES['render'] содержит renders/subs/chapters",
          "render: ['renders', 'subs', 'chapters']" in index_html, True)
    check("TAB_FILES['cover'] содержит новую категорию covers",
          "cover: ['covers']" in index_html, True)

    print("\nЛог задачи: опрос останавливается и при сворачивании блока, и при уходе "
          "со страницы (смена hash)")
    toggle_job_log = function_body(index_html, "function toggleJobLog(tr, btn){")
    check("toggleJobLog() найдена", bool(toggle_job_log), True)
    check("сворачивание блока останавливает опрос (stopJobLogPoll)",
          "stopJobLogPoll(jid);" in toggle_job_log, True)
    check("stopJobLogPoll использует clearInterval",
          "clearInterval(LOG_TIMERS[jid])" in index_html, True)
    render_fn = function_body(index_html, "async function render(){")
    check("render() найдена", bool(render_fn), True)
    check("render() снимает все таймеры логов при каждой смене маршрута "
          "(clearAllLogTimers — уход со страницы)",
          "clearAllLogTimers();" in render_fn, True)
    check("clearAllLogTimers снимает КАЖДЫЙ зарегистрированный таймер через clearInterval",
          "clearInterval(LOG_TIMERS[jid]); delete LOG_TIMERS[jid];"
          in function_body(index_html, "function clearAllLogTimers(){"), True)

    print("\n«Сделать всё» — ровно один блок на экране")
    # Считаем только исполняемую разметку, комментарии («ФР §4 п.8» и т.п.) не в счёт —
    # иначе пояснение рядом с кодом ложно удваивало бы «находку».
    vp_code_only = "\n".join(ln for ln in view_project.splitlines()
                             if not ln.strip().startswith("//"))
    check("кнопка-переключатель и заголовок карточки «Сделать всё» встречаются "
          "ровно по разу (то есть на экране один блок, не по одному на вкладку)",
          vp_code_only.count("Сделать всё"), 2)

    print("\nРегрессия review-feedback-F4 цикл 2, Issue 1 (Critical, деньги): «Запустить» "
          "берёт provider/model из ТЕКУЩЕГО выбора на момент клика, не из значения, "
          "посчитанного один раз при отрисовке экрана — и во вкладке проекта, и на #/covers "
          "(«тот же файл и тот же дефект с деньгами»)")
    view_covers = function_body(index_html, "async function viewCovers(){")
    check("viewCovers() найдена", bool(view_covers), True)
    check("вкладка «Обложка» проекта: curOpt вычисляется ровно один раз в файле функции",
          cover_tab.count("curOpt = cd.options[st.option]"), 1)
    idx_click = cover_tab.find("#cv-go').onclick")
    idx_curopt = cover_tab.find("curOpt = cd.options[st.option]")
    check("...и это вычисление физически ВНУТРИ обработчика клика «Запустить» (после его "
          "начала), а не до него", idx_click >= 0 and idx_curopt > idx_click, True)
    check("тело запроса вкладки берёт provider/model из этого curOpt",
          "provider: curOpt.provider || ''" in cover_tab
          and "model: curOpt.model || ''" in cover_tab, True)
    check("#/covers: curOpt вычисляется ровно один раз в файле функции",
          view_covers.count("curOpt = d.options[COVER_FORM.option]"), 1)
    idx_click2 = view_covers.find("btn.onclick = async () => {")
    idx_curopt2 = view_covers.find("curOpt = d.options[COVER_FORM.option]")
    check("...и тоже внутри обработчика клика «Запустить», а не до него",
          idx_click2 >= 0 and idx_curopt2 > idx_click2, True)
    check("тело запроса #/covers берёт provider/model из этого curOpt",
          "provider: curOpt.provider || ''" in view_covers
          and "model: curOpt.model || ''" in view_covers, True)

    print("\nРегрессия Issue 2: paintFields() вкладки «Обложка» проекта считает языки "
          "заново при каждом вызове (не из снимка, сделанного один раз при открытии)")
    check("const langs встречается в теле вкладки ровно один раз",
          cover_tab.count("const langs = [...st.langs];"), 1)
    idx_pf_def = cover_tab.find("function paintFields(){")
    idx_langs = cover_tab.find("const langs = [...st.langs];")
    check("...и объявлен ВНУТРИ paintFields(), а не во внешней области видимости вкладки",
          idx_pf_def >= 0 and idx_langs > idx_pf_def, True)

    print("\nРегрессия Issue 3: исход «Начать» переживает отложенную перерисовку экрана "
          "(STAGE_OUTCOME_STATE по ключу pid:tab), отложенный viewProject() не бьёт по уже "
          "покинутой вкладке")
    check("исход сохраняется в STAGE_OUTCOME_STATE по ключу pid:tab",
          "const skey = p.id + ':' + tab;" in stage_tab
          and "STAGE_OUTCOME_STATE[skey] = {text, cls, at: Date.now()};" in stage_tab, True)
    check("исход из STAGE_OUTCOME_STATE восстанавливается при (пере)отрисовке вкладки",
          "const savedOutcome = STAGE_OUTCOME_STATE[skey];" in stage_tab, True)
    check("ошибка старта (400 и т.п.) тоже сохраняется в STAGE_OUTCOME_STATE",
          "STAGE_OUTCOME_STATE[skey] = {text: e.message, cls: 'bad', at: Date.now()};"
          in stage_tab, True)
    # 21.09.2026: у записи появился срок. Без него отказ переживал не только ту самую
    # перерисовку, ради которой всё делалось, но и весь сеанс: владелец видел «Уже идёт»
    # по проекту, где мешавшие задачи давно отменены. Поведение проверяется в браузере
    # (tests/test_stage_outcome_ttl_browser.py), здесь пришпилен сам срок.
    check("у сохранённого исхода есть срок годности",
          "STAGE_OUTCOME_TTL_MS" in index_html, True)
    check("и протухший исход удаляется, а не показывается",
          "delete STAGE_OUTCOME_STATE[skey];" in stage_tab, True)
    check("адрес на момент клика запоминается (hashAtClick) ДО похода в сеть",
          "const hashAtClick = location.hash;" in stage_tab, True)
    check("отложенный viewProject() после успешного старта сверяет location.hash с "
          "hashAtClick перед срабатыванием",
          "if (location.hash === hashAtClick) viewProject(p.id);" in stage_tab, True)

    print("\nСписки задач вкладок «Сценарий»/«Обложка» — только задачи этого проекта "
          "(связь project_service_jobs, решение владельца 17.09.2026); задача, созданная "
          "тут же, — первой; опрос идёт, пока есть незавершённая; готовая раскрывается кликом")
    scen_refresh = function_body(index_html,
                                 "async function refreshScenarioJobs(box, pid, srcTextEl){")
    cover_refresh = function_body(index_html, "async function refreshCoverJobs(box, pid){")
    check("refreshScenarioJobs() найдена", bool(scen_refresh), True)
    check("refreshCoverJobs() найдена", bool(cover_refresh), True)
    check("источник — GET /ui/text с project_id (и на первом заходе, и на тике)",
          scen_refresh.count("api(`/ui/text?project_id=${encodeURIComponent(pid)}`)"), 2)
    check("подпись списка — задачи этого проекта",
          "Задачи текста этого" in scen_refresh, True)
    check("источник обложек — GET /ui/covers с project_id",
          cover_refresh.count("api(`/ui/covers?project_id=${encodeURIComponent(pid)}`)"), 2)
    check("подпись списка обложек — задачи этого проекта",
          "Задачи обложек этого" in cover_refresh, True)
    check("задача из вкладки уходит с project_id (текст и обложка)",
          "project_id: p.id," in index_html and "edit_preview: st.edit, project_id: p.id}" in index_html,
          True)
    check("задача, созданная тут же (st.lastJob), переставляется первой в списке текста",
          "if (i > 0){ const [j] = jobs.splice(i, 1); jobs.unshift(j); }" in scen_refresh,
          True)
    check("то же самое — для списка обложек",
          "if (i > 0){ const [j] = jobs.splice(i, 1); jobs.unshift(j); }" in cover_refresh,
          True)
    check("опрос списка задач текста продолжается, пока среди показанных есть "
          "незавершённая (JOB_LIST_TERMINAL)",
          "jobs.some(j => !JOB_LIST_TERMINAL.has(j.state))" in scen_refresh, True)
    check("то же самое — для списка задач обложки",
          "jobs.some(j => !JOB_LIST_TERMINAL.has(j.state))" in cover_refresh, True)
    check("готовая задача текста раскрывается кликом по строке и предлагает «Взять в "
          "проект» (та же механика RESULT_OPEN/wireResults, что у #/text)",
          "wireProjectJobRows(box, 'rewrite'" in scen_refresh
          and "paintTextTake(jid, host, pid, srcTextEl)" in scen_refresh, True)
    check("готовая задача обложки раскрывается кликом и предлагает «Поставить обложкой "
          "проекта»",
          "wireProjectJobRows(box, 'cover'" in cover_refresh
          and "paintCoverTake(jid, host, pid)" in cover_refresh, True)

    print("\nРегрессия review-feedback-F4 цикл 3, Issue 1: тик опроса списков Rewrite/"
          "Cover не перестраивает таблицу целиком (не стирает раскрытые result-row и "
          "confirmInline внутри них) — обновляет только изменившиеся строки через "
          "syncJobsTable()")
    sync_jobs_table = function_body(
        index_html, "function syncJobsTable(box, jobs, service, rowHtml, colspan, paint){")
    check("syncJobsTable() найдена", bool(sync_jobs_table), True)
    check("тик берёт таблицу тиком через syncJobsTable(), не через box.innerHTML",
          "syncJobsTable(box, jobs2, 'rewrite'" in scen_refresh, True)
    check("то же самое — для обложек",
          "syncJobsTable(box, jobs2, 'cover'" in cover_refresh, True)
    check("box.innerHTML внутри самого тика (после первого ответа) не встречается — "
          "полная перестройка есть только один раз, до объявления тика",
          scen_refresh.count("box.innerHTML") == 1
          and cover_refresh.count("box.innerHTML") == 1, True)
    check("syncJobsTable не трогает строку, только если не изменились состояние, "
          "прогресс и подпись (прогресс внутри running не застывает)",
          "if (tr.dataset.print === jobRowPrint(j)) return;" in sync_jobs_table
          and "tr.dataset.print = jobRowPrint(j);" in sync_jobs_table
          and "j.progress" in function_body(index_html, "function jobRowPrint(j){")
          and "j.detail" in function_body(index_html, "function jobRowPrint(j){"), True)
    check("новая задача — новая строка (список не переписывается целиком)",
          "tbody.insertAdjacentHTML('beforeend', rowHtml(j));" in sync_jobs_table, True)
    check("готовый результат уже раскрытой строки перечитывается РОВНО в момент "
          "перехода в терминальное состояние (не на каждом тике, где состояние не "
          "менялось)",
          "!wasTerminal && JOB_LIST_TERMINAL.has(j.state) && RESULT_OPEN.has(key)"
          in sync_jobs_table, True)
    check("новая строка получает клик через wireOneJobRow (не через повторный "
          "wireProjectJobRows на всей таблице — тот авто-кликнул бы уже открытые "
          "строки и закрыл бы их)",
          "wireOneJobRow(tbody.lastElementChild, service, colspan, paint);"
          in sync_jobs_table, True)

    print("\nРегрессия Issue 2: опрос списков Rewrite/Cover не переживает смену hash, "
          "если ответ был в полёте — тик обёрнут busy-флагом и перехватом ошибки, как "
          "startJobLogPoll")
    check("исходный hash запоминается ДО первого похода в сеть",
          "const hashAtStart = location.hash;" in scen_refresh
          and "const hashAtStart = location.hash;" in cover_refresh, True)
    check("после ПЕРВОГО ответа — проверка, что экран ещё актуален, до отрисовки "
          "таблицы и до решения заводить таймер",
          "if (location.hash !== hashAtStart || !box.isConnected) return;" in scen_refresh
          and "if (location.hash !== hashAtStart || !box.isConnected) return;"
          in cover_refresh, True)
    check("тик содержит busy-флаг (тот же приём, что у startJobLogPoll)",
          "let busy = false;" in scen_refresh and "if (busy) return;" in scen_refresh
          and "let busy = false;" in cover_refresh and "if (busy) return;" in cover_refresh,
          True)
    check("тик после КАЖДОГО своего ответа снова проверяет hash/box.isConnected и "
          "останавливает себя, если экран уже не тот",
          "if (location.hash !== hashAtStart || !box.isConnected){ stopOwnPoll(key, handle); "
          "return; }" in scen_refresh
          and "if (location.hash !== hashAtStart || !box.isConnected){ stopOwnPoll(key, handle); "
          "return; }" in cover_refresh, True)
    check("тик снимает СВОЙ таймер (дескриптор из замыкания), реестр чистит только "
          "если там всё ещё он; перед регистрацией снимается уже записанный",
          "if (LOG_TIMERS[key] === handle) delete LOG_TIMERS[key];"
          in function_body(index_html, "function stopOwnPoll(key, handle){")
          and all("stopJobLogPoll(key);\n  const handle = setInterval(tick, 3000);\n"
                  "  LOG_TIMERS[key] = handle;" in b for b in (scen_refresh, cover_refresh))
          and all("stopJobLogPoll(key)" not in b.split("const tick = async")[1]
                  .split("// Два наложившихся")[0] for b in (scen_refresh, cover_refresh)), True)
    m_scen_catch = re.search(r"const tick = async \(\) => \{.*?catch\(e\)\{(.*?)\}finally",
                              scen_refresh, re.S)
    scen_catch_body = m_scen_catch.group(1) if m_scen_catch else "<catch-блок не найден>"
    check("ошибка сети на тике списка задач текста не стирает уже показанный список и "
          "не глушит опрос — тот же приём, что в startJobLogPoll (Issue 7 цикла 1)",
          "box.innerHTML" not in scen_catch_body and "stopJobLogPoll" not in scen_catch_body,
          True)
    m_cover_catch = re.search(r"const tick = async \(\) => \{.*?catch\(e\)\{(.*?)\}finally",
                               cover_refresh, re.S)
    cover_catch_body = m_cover_catch.group(1) if m_cover_catch else "<catch-блок не найден>"
    check("то же самое — для списка задач обложек",
          "box.innerHTML" not in cover_catch_body
          and "stopJobLogPoll" not in cover_catch_body, True)

    print("\nРегрессия Issue 5: строка задачи вернула actions()/wireActions() (отменить/"
          "повторить), прогресс и «с какими настройками пошла»; «Во что обошёлся» — один "
          "раз в шапке; «Прочие задачи проекта» (виды вне TAB_KINDS) — на «Сценарии»")
    job_row = function_body(index_html, "function jobRowHtml(j){")
    jobs_table_card = function_body(index_html, "function jobsTableCard(title, jobs, emptyText){")
    check("jobRowHtml() найдена", bool(job_row), True)
    check("jobsTableCard() найдена", bool(jobs_table_card), True)
    check("строка задачи предлагает отменить/повторить через actions()",
          "actions({service: 'vsgen'" in job_row, True)
    check("jobsTableCard() навешивает wireActions() — кнопки реально работают",
          "wireActions(card);" in jobs_table_card, True)
    check("строка задачи показывает полоску прогресса (j.progress)",
          "j.progress != null" in job_row and 'class="bar"' in job_row, True)
    check("строка задачи показывает, с какими настройками пошла (j.applied)",
          "с какими настройками пошла" in job_row and "j.applied.map(a =>" in job_row, True)
    check("«Во что обошёлся» (d.paid) — ровно один блок на экране (в шапке), не по "
          "одному на вкладку",
          vp_code_only.count("Во что обошёлся"), 1)
    check("«Прочие задачи проекта» — виды queue_tasks вне TAB_KINDS, показаны на "
          "«Сценарии», а не молча спрятаны с экрана",
          "knownKinds.has(j.kind)" in scenario_tab
          and "Прочие задачи проекта" in scenario_tab, True)

    print("\nРегрессия Issue 6: содержимое fileCard() и хвостовая подпись карточки "
          "«Во что обошёлся» — внутри .secbody (правило батча A), не вплотную к рамке")
    file_card = function_body(index_html, "function fileCard(d, g){")
    check("fileCard() найдена", bool(file_card), True)
    check("содержимое fileCard идёт после отдельного .secbody (не сразу в .sec)",
          re.search(r'<div class="sec">.*?</div>\s*<div class="secbody">', file_card, re.S)
          is not None, True)
    check("хвостовая подпись карточки «Во что обошёлся» — тоже в .secbody",
          '<div class="secbody">' in view_project, True)

    print("\nРегрессия Issue 7: опрос лога не запускает тик поверх ещё не ответившего "
          "(busy-флаг), сетевая ошибка тика не стирает уже показанный лог и не глушит "
          "опрос идущей задачи, 'deleted' — терминальный статус")
    job_log_poll = function_body(index_html, "function startJobLogPoll(jid, pre){")
    check("startJobLogPoll() найдена", bool(job_log_poll), True)
    check("busy-флаг не даёт двум тикам работать одновременно",
          "let busy = false;" in job_log_poll and "if (busy) return;" in job_log_poll, True)
    m_catch = re.search(r"catch\(e\)\{(.*?)\}finally", job_log_poll, re.S)
    catch_body = m_catch.group(1) if m_catch else "<catch-блок не найден>"
    check("catch тика — не трогает уже показанный текст лога и не останавливает опрос "
          "идущей задачи (сеть иногда сбоит — задача условия, не повод молчать навсегда)",
          "pre.textContent" not in catch_body and "stopJobLogPoll" not in catch_body, True)
    check("тик сам себя останавливает, когда статус задачи терминальный (LOG_TERMINAL)",
          "if (LOG_TERMINAL.has(d.status)) stopJobLogPoll(jid);" in job_log_poll, True)
    m_term = re.search(r"const LOG_TERMINAL = new Set\((.*?)\);", index_html)
    check("'deleted' — тоже терминальный статус (иначе опрос лога удалённой задачи "
          "продолжался бы впустую)",
          "'deleted'" in (m_term.group(1) if m_term else ""), True)

    print("\nРегрессия Issue 8: пустой блок «Результат» подписан заголовком; подпись "
          "режима обложки (#cv-note) и соотношение сторон видны сразу при первой "
          "отрисовке вкладки, не только после клика по режиму")
    empty_result = function_body(index_html, "function emptyResultCard(){")
    check("emptyResultCard() найдена", bool(empty_result), True)
    check("пустой блок «Результат» содержит заголовок .sec, а не идёт безымянным",
          '<div class="sec">Результат</div>' in empty_result, True)
    check("#cv-note заполняется СРАЗУ при первой отрисовке вкладки (до первого клика по "
          "режиму — сразу за присвоением следует вызов paintFields())",
          re.search(r"noteBox\.textContent = initNote \? initNote\.note : '';"
                     r"\s*\n\s*paintFields\(\);", cover_tab) is not None, True)
    check("форма создания обложки показывает фиксированное соотношение сторон (cd.aspect) "
          "прямо на экране, не только в задаче",
          "cd.aspect" in cover_tab and "Соотношение сторон" in cover_tab, True)

    print("\nНикаких alert()/confirm()/prompt() — только toast/askInline/confirmInline")
    script_only = re.search(r"<script>(.*?)</script>", index_html, re.S)
    js_text = script_only.group(1) if script_only else ""
    js_code_lines = [ln for ln in js_text.splitlines() if not ln.strip().startswith("//")]
    calls = re.findall(r"\b(alert|confirm|prompt)\s*\(", "\n".join(js_code_lines))
    check("вызовов alert(/confirm(/prompt( в исполняемом JS нет (комментарии не считаются)",
          calls, [])

    print("\nИнлайн-JS `web/index.html` синтаксически корректен (node --check)")
    node = shutil.which("node")
    if not node:
        print("  ПРОПУЩЕНО  node не найден в PATH — проверка синтаксиса не выполнена")
    else:
        scripts = re.findall(r"<script>(.*?)</script>", index_html, re.S)
        check("ровно один инлайн <script>-блок в index.html", len(scripts), 1)
        tmpdir = tempfile.mkdtemp(prefix="project-ui-qa-")
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
