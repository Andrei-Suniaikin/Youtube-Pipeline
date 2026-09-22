#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Лента канала: название-ссылка на донора/youtu.be, честная подпись, клик в проект
(пункт 1 владельца, фаза 3, 22.09.2026).

Проверяется прямой вызов НАСТОЯЩЕЙ `calendarFeed()` из `web/index.html` (тот же приём,
что и `test_donor_length_warn_browser.py` для `donorsHtml`) на трёх фиктивных строках
— своя фикстура, без сети и без стенда:

  A. `done` со СЛОТОМ В БУДУЩЕМ — ссылка ведёт на донора (не youtu.be), подпись честная
     «загружен, выйдет …», не «опубликован» (AC-9, Edge Case #1, `done` ≠ опубликовано).
  B. `done` со слотом В ПРОШЛОМ — ссылка ведёт на `https://youtu.be/<video_id>`, подпись
     «опубликован» (AC-10).
  C. `planned` БЕЗ проекта — некликабельна: нет `tabindex`, нет `role=link`, класс
     `clickable` не проставлен (AC-11, «честно показать»).
  E. `done` со СЛОТОМ В БУДУЩЕМ и ПУСТЫМ `video_id` — ревью 22.09.2026, Issue 2: до
     фикса такая строка бралась за `CAL_TITLE.done` («вышел»), хотя `done` ≠
     опубликовано ни разу не подтверждена даже заливка. Подпись обязана остаться
     нейтральной («готов, выйдет …») — без «вышел» и без «опубликован».

И для строк A/B (обе с `project_id`) — клик по строке ВНЕ ссылки ведёт на
`#/project/<project_id>` (AC-11), Enter/Space с клавиатуры делают то же самое (тот же
паттерн доступности, что у `.chcard`), а клик ПО САМОЙ ссылке названия — в переход по
проекту не превращается (закрывается `ev.target.closest('a')`).

Строка A заодно проверяет Issue 3 ревью 22.09.2026: `title`-подсказка у ссылки-названия
называет, куда клик поведёт («донор»/«YouTube»), а не только цвет/подчёркивание.

Итерация 2 ревью (22.09.2026, Issue 1): Enter/Space, отправленные ПРЯМО на саму ссылку
названия внутри кликабельной строки, не должны перехватываться обработчиком строки и
уводить в проект — иначе ссылку на донора/YouTube с клавиатуры было бы не открыть.
Прежняя версия теста слала клавиши только на саму строку и эту дыру не видела.

    .venv/bin/python tests/test_calendar_feed_links_browser.py
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


NOW = time.time()

ROW_FUTURE_DONE = {
    "at": NOW + 3600, "past": False, "beyond_horizon": False, "state": "done",
    "donor_title": "Донор будущего выпуска", "donor_url": "https://youtu.be/donorFuture",
    "release_id": "rel-future", "phase": "upload", "phase_title": "заливка",
    "project_id": "proj-future", "project_name": "Проект Будущее",
    "blocked_reason": "", "video_id": "FUTVIDEOID",
}
ROW_PAST_DONE = {
    "at": NOW - 3600, "past": True, "beyond_horizon": False, "state": "done",
    "donor_title": "Донор прошлого выпуска", "donor_url": "https://youtu.be/donorPast",
    "release_id": "rel-past", "phase": "done", "phase_title": "",
    "project_id": "proj-past", "project_name": "Проект Прошлое",
    "blocked_reason": "", "video_id": "PASTVIDEOID",
}
ROW_PLANNED_NO_PROJECT = {
    "at": NOW + 7200, "past": False, "beyond_horizon": False, "state": "planned",
    "donor_title": "Донор в ожидании", "donor_url": "https://youtu.be/donorPlanned",
    "release_id": "", "phase": "", "phase_title": "",
    "project_id": "", "project_name": "",
    "blocked_reason": "", "video_id": "",
}
# Ряд D — есть `release_id`, но НЕТ `project_id` (выпуск уже создан, а привязку к
# проекту ещё не проставили — редкий, но допустимый по схеме случай, `project_id`
# в `pub_releases` может быть NULL). Без этого ряда проверка «кликабельность решает
# именно `project_id`, а не `release_id`» не различает мутацию `!!r.project_id` →
# `!!r.release_id`: в рядах A/B/C оба поля либо оба пусты, либо оба заполнены разом,
# и подмена одного на другое ничего не меняет в их исходах.
ROW_RELEASE_NO_PROJECT = {
    "at": NOW + 10800, "past": False, "beyond_horizon": False, "state": "running",
    "donor_title": "Донор без проекта", "donor_url": "https://youtu.be/donorNoProject",
    "release_id": "rel-no-project", "phase": "media", "phase_title": "материалы",
    "project_id": "", "project_name": "",
    "blocked_reason": "", "video_id": "",
}
# Ряд E — ревью 22.09.2026, Issue 2: `done`, слот ещё в будущем, `video_id` ПУСТ
# (расписание подтвердилось раньше, чем ролик вообще залит — редкий, но честный
# случай). До фикса подпись брала `CAL_TITLE.done` = «вышел» именно в этой ветке.
ROW_FUTURE_DONE_NO_VIDEO = {
    "at": NOW + 14400, "past": False, "beyond_horizon": False, "state": "done",
    "donor_title": "Донор без видео пока", "donor_url": "https://youtu.be/donorPendingUpload",
    "release_id": "rel-future-novideo", "phase": "upload", "phase_title": "заливка",
    "project_id": "proj-future-novideo", "project_name": "Проект без видео",
    "blocked_reason": "", "video_id": "",
}
D = {"calendar": [ROW_FUTURE_DONE, ROW_PAST_DONE, ROW_PLANNED_NO_PROJECT,
                  ROW_RELEASE_NO_PROJECT, ROW_FUTURE_DONE_NO_VIDEO]}

SCENARIO = r"""
(async () => {
  const out = {};
  try {
    const D = __D_JSON__;
    const box = document.createElement('div');
    document.body.append(box);
    box.append(calendarFeed(D));

    const rows = [...box.querySelectorAll('.cal')];
    out.rowCount = rows.length;

    const byHref = (needle) => rows.find(r => {
      const a = r.querySelector('.what > a');
      return a && a.getAttribute('href').includes(needle);
    });

    // A. done, слот в будущем — ссылка на донора, подпись честная.
    const rowA = byHref('donorFuture');
    out.rowA_found = !!rowA;
    if (rowA){
      const a = rowA.querySelector('.what > a');
      out.rowA_href = a.getAttribute('href');
      out.rowA_target = a.getAttribute('target');
      out.rowA_rel = a.getAttribute('rel');
      out.rowA_text = a.textContent.trim();
      out.rowA_title = a.getAttribute('title');
      out.rowA_muted = rowA.querySelector('.muted').textContent;
      out.rowA_clickable = rowA.classList.contains('clickable');
      out.rowA_tabindex = rowA.getAttribute('tabindex');
      out.rowA_role = rowA.getAttribute('role');
      out.rowA_dataProject = rowA.dataset.project;
    }

    // B. done, слот в прошлом — ссылка на youtu.be, подпись «опубликован».
    const rowB = byHref('PASTVIDEOID');
    out.rowB_found = !!rowB;
    if (rowB){
      const a = rowB.querySelector('.what > a');
      out.rowB_href = a.getAttribute('href');
      out.rowB_text = a.textContent.trim();
      out.rowB_muted = rowB.querySelector('.muted').textContent;
      out.rowB_clickable = rowB.classList.contains('clickable');
    }

    // C. planned без проекта — некликабельна.
    const rowC = byHref('donorPlanned');
    out.rowC_found = !!rowC;
    if (rowC){
      out.rowC_clickable = rowC.classList.contains('clickable');
      out.rowC_tabindex = rowC.getAttribute('tabindex');
      out.rowC_role = rowC.getAttribute('role');
      out.rowC_hasDataProject = rowC.hasAttribute('data-project');
    }

    // D. есть release_id, но НЕТ project_id — тоже некликабельна. Различает
    // `clickable = !!r.project_id` от `!!r.release_id` (в A/B/C оба поля совпадают по
    // «пусто/непусто», только здесь они расходятся).
    const rowD = byHref('donorNoProject');
    out.rowD_found = !!rowD;
    if (rowD){
      out.rowD_clickable = rowD.classList.contains('clickable');
      out.rowD_tabindex = rowD.getAttribute('tabindex');
      out.rowD_hasDataProject = rowD.hasAttribute('data-project');
    }

    // E. done, слот в будущем, video_id ПУСТ (ревью 22.09.2026, Issue 2) — подпись
    // обязана остаться нейтральной, без «вышел» и без «опубликован».
    const rowE = byHref('donorPendingUpload');
    out.rowE_found = !!rowE;
    if (rowE){
      const a = rowE.querySelector('.what > a');
      out.rowE_href = a.getAttribute('href');
      out.rowE_muted = rowE.querySelector('.muted').textContent;
    }

    // Клик по строке ВНЕ ссылки (точка в самой строке .cal, но не в <a>) — переход
    // на #/project/<id>. Кликаем по <time>, оно точно вне <a>.
    location.hash = '';
    rowA.querySelector('time').dispatchEvent(new MouseEvent('click', {bubbles: true}));
    out.afterRowClick_hash = location.hash;

    // Клик ПО САМОЙ ссылке — переход по проекту НЕ срабатывает (закрыт ev.target.closest('a')).
    location.hash = '';
    rowB.querySelector('.what > a').dispatchEvent(new MouseEvent('click', {bubbles: true}));
    out.afterLinkClick_hash = location.hash;

    // Клавиатура: Enter и Space на кликабельной строке — тот же переход, что и клик
    // (паттерн `.chcard`).
    location.hash = '';
    rowA.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
    out.afterEnter_hash = location.hash;
    location.hash = '';
    rowA.dispatchEvent(new KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true}));
    out.afterSpace_hash = location.hash;

    // Issue 1 ревью 22.09.2026 (итерация 2): Enter/Space, отправленные ПРЯМО на саму
    // ссылку названия (не на строку) — событие всплывает до `.cal`, и обработчик
    // строки не должен его перехватывать: ссылка должна получить шанс сама себя
    // открыть, а не быть подмятой переходом в проект.
    const linkA = rowA.querySelector('.what > a');
    location.hash = '';
    const evEnterOnLink = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
    linkA.dispatchEvent(evEnterOnLink);
    out.afterEnterOnLink_hash = location.hash;
    out.afterEnterOnLink_defaultPrevented = evEnterOnLink.defaultPrevented;

    location.hash = '';
    const evSpaceOnLink = new KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true});
    linkA.dispatchEvent(evSpaceOnLink);
    out.afterSpaceOnLink_hash = location.hash;
    out.afterSpaceOnLink_defaultPrevented = evSpaceOnLink.defaultPrevented;

    // Некликабельная строка (D) — release_id есть, project_id нет: тоже никакого перехода.
    location.hash = '';
    rowD.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    rowD.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
    out.afterRowD_hash = location.hash;

    // Некликабельная строка (C) — клик и клавиатура ничего не делают.
    location.hash = '';
    rowC.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    rowC.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
    out.afterRowC_hash = location.hash;
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""
SCENARIO = SCENARIO.replace("__D_JSON__", json.dumps(D, ensure_ascii=False))


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=10000)
    if res.get("error") and "Chrome не найден" in str(res.get("error")):
        print("  ПРОПУЩЕНО: " + res["error"])
        FAILS.append("браузерная часть не выполнена")
        print()
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1

    print("Лента канала: done в будущем, done в прошлом, planned без проекта, "
          "release без проекта, done в будущем без video_id")
    check("сценарий выполнен без ошибок", res.get("error"), None)
    check("пять строк нарисованы", res.get("rowCount"), 5)

    print("\nA. done, слот ещё в будущем")
    check("строка A найдена", res.get("rowA_found"), True)
    # Мутация-проверка: убери `&& r.at <= nowSec` из вычисления `published` в
    # `calendarFeed` — эта проверка первой поймает разницу: ссылка станет вести на
    # youtu.be раньше времени.
    check("ссылка ведёт на донора, НЕ на youtu.be", res.get("rowA_href"),
          "https://youtu.be/donorFuture")
    check("target=_blank", res.get("rowA_target"), "_blank")
    check("rel=noopener", res.get("rowA_rel"), "noopener")
    check("текст ссылки — настоящее название донора", res.get("rowA_text"),
          "Донор будущего выпуска")
    check("подпись честная — «загружен, выйдет …», не «опубликован»",
          "загружен, выйдет" in (res.get("rowA_muted") or ""), True)
    check("и точно не «опубликован» раньше времени",
          "опубликован" in (res.get("rowA_muted") or ""), False)
    check("строка кликабельна (project_id есть)", res.get("rowA_clickable"), True)
    check("tabindex=0 — паттерн .chcard", res.get("rowA_tabindex"), "0")
    check("role=link — паттерн .chcard", res.get("rowA_role"), "link")
    check("data-project — id настоящего проекта", res.get("rowA_dataProject"), "proj-future")
    # Issue 3 ревью 22.09.2026: title-подсказка называет ссылку донором, не youtube.
    # Мутация-проверка: убери `title="${esc(linkTitle)}"` из шаблона ссылки в
    # `calendarFeed` — атрибут пропадёт, и эта проверка поймает разницу первой.
    check("title-подсказка называет ссылку донором (не YouTube)",
          "донор" in (res.get("rowA_title") or "").lower(), True)

    print("\nB. done, слот уже в прошлом")
    check("строка B найдена", res.get("rowB_found"), True)
    # Мутация-проверка: поменяй условие публикации на просто `r.state === 'done'`
    # (без сверки с `r.at`) — обе строки A и B начнут вести на youtu.be одинаково, и
    # эта проверка перестанет отличать их.
    check("ссылка ведёт на youtu.be/<video_id>", res.get("rowB_href"),
          "https://youtu.be/PASTVIDEOID")
    check("текст ссылки — название донора (не голая ссылка)", res.get("rowB_text"),
          "Донор прошлого выпуска")
    check("подпись — «опубликован»", "опубликован" in (res.get("rowB_muted") or ""), True)
    check("строка кликабельна", res.get("rowB_clickable"), True)

    print("\nC. planned без проекта — честно некликабельна")
    check("строка C найдена", res.get("rowC_found"), True)
    # Мутация-проверка: замени `clickable = !!r.project_id` на `!!r.release_id` (или
    # уберём условие вовсе) — строка без проекта снова станет ложно кликабельной.
    check("класс 'clickable' НЕ проставлен", res.get("rowC_clickable"), False)
    check("нет tabindex — строка выпадает из табуляции", res.get("rowC_tabindex"), None)
    check("нет role=link", res.get("rowC_role"), None)
    check("нет data-project вовсе", res.get("rowC_hasDataProject"), False)

    print("\nD. release_id есть, project_id нет — тоже честно некликабельна")
    check("строка D найдена", res.get("rowD_found"), True)
    # Мутация-проверка (та, которую строки A/B/C не различают): замени
    # `clickable = !!r.project_id` на `!!r.release_id` — здесь `release_id` НЕПУСТ, а
    # `project_id` пуст, и мутация делает строку D ложно кликабельной, хотя вести
    # клику некуда (страница проекта не существует без project_id).
    check("класс 'clickable' НЕ проставлен (несмотря на release_id)",
          res.get("rowD_clickable"), False)
    check("нет tabindex", res.get("rowD_tabindex"), None)
    check("нет data-project", res.get("rowD_hasDataProject"), False)

    print("\nE. done, слот ещё в будущем, video_id ПУСТ (ревью 22.09.2026, Issue 2)")
    check("строка E найдена", res.get("rowE_found"), True)
    check("ссылка всё равно ведёт на донора (video_id пуст — youtu.be вести некуда)",
          res.get("rowE_href"), "https://youtu.be/donorPendingUpload")
    # Мутация-проверка: верни `CAL_TITLE.done` в ветку `r.video_id ? … : …` внутри
    # `calendarFeed` (`web/index.html`) — эта проверка первой поймает разницу: подпись
    # снова станет «вышел» для ролика, который ещё даже не залит.
    check("подпись НЕ «вышел» (done ≠ опубликовано, даже без video_id)",
          "вышел" in (res.get("rowE_muted") or ""), False)
    check("и не «опубликован» раньше времени",
          "опубликован" in (res.get("rowE_muted") or ""), False)
    check("подпись нейтральная — «готов, выйдет …»",
          "готов, выйдет" in (res.get("rowE_muted") or ""), True)

    print("\nКлик и клавиатура")
    check("клик ВНЕ ссылки по строке A — переход на #/project/proj-future",
          res.get("afterRowClick_hash"), "#/project/proj-future")
    # Мутация-проверка: убери проверку `ev.target.closest('a')` в обработчике клика —
    # клик по самой ссылке начнёт ТАКЖЕ триггерить переход в проект, и хэш здесь
    # перестанет оставаться пустым.
    check("клик ПО ссылке названия — переход в проект НЕ срабатывает",
          res.get("afterLinkClick_hash"), "")
    check("Enter на строке A — тот же переход, что и клик",
          res.get("afterEnter_hash"), "#/project/proj-future")
    check("Space на строке A — тот же переход", res.get("afterSpace_hash"),
          "#/project/proj-future")
    # Issue 1 ревью 22.09.2026 (итерация 2): Enter/Space на самой ссылке названия не
    # должны уводить в проект — иначе ссылку донора/YouTube с клавиатуры не открыть.
    # Мутация-проверка: убери `if (ev.target !== el) return;` из `el.onkeydown` в
    # `calendarFeed` (`web/index.html`) — обе проверки ниже поймают разницу первой:
    # хэш начнёт меняться на `#/project/proj-future`, и `defaultPrevented` станет `true`.
    check("Enter на ссылке названия — переход в проект НЕ срабатывает",
          res.get("afterEnterOnLink_hash"), "")
    check("Enter на ссылке названия — событие не перехвачено (defaultPrevented=false)",
          res.get("afterEnterOnLink_defaultPrevented"), False)
    check("Space на ссылке названия — переход в проект НЕ срабатывает",
          res.get("afterSpaceOnLink_hash"), "")
    check("Space на ссылке названия — событие не перехвачено (defaultPrevented=false)",
          res.get("afterSpaceOnLink_defaultPrevented"), False)
    check("клик/Enter на некликабельной строке C — хэш не меняется",
          res.get("afterRowC_hash"), "")
    check("клик/Enter на некликабельной строке D (release без проекта) — хэш не меняется",
          res.get("afterRowD_hash"), "")

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
