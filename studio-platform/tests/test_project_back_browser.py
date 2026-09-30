#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Возврат из проекта: ссылка назад на месте, вкладки не копят историю браузера.

Жалоба владельца 20.09.2026: «когда захожу в проекты и тыкаю на проект, потом нажимаю
обратно — показывает почему-то, что я на вкладке с каналами в топбаре».

Причин было две, и проверяются обе:

  * ссылка назад у экрана проекта выглядела не так, как у всех соседей («‹ Все проекты»
    против «← ко всем каналам/выпускам/пресетам»), и глазом не находилась — человек жал
    «назад» в браузере;
  * каждый клик по вкладке проекта (#/project/{pid}/{tab}) был ОТДЕЛЬНЫМ шагом истории.
    «Назад» отыгрывал их по одному и в конце доходил до адреса без хеша, а это умолчание
    `ROUTES` — «Каналы». Отсюда и «показывает, что я на вкладке с каналами».
    (С 30.09.2026 вход без хеша встречает сводкой — `homeHash()`; здесь по-прежнему
    проверяется сам `ROUTES`, его последняя строка-ловушка, и она отдаёт «Каналы».)

Здесь же — подсветка вкладки в топбаре для всех адресов этих переходов: `ROUTES` — это
единственный источник подсветки (`render()` красит по `hit[0]`), поэтому проверяем его
напрямую, без отрисовки всех экранов.

Сети и стенда не нужно: `api()` и `render()` подменены, браузер настоящий.

    .venv/bin/python tests/test_project_back_browser.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


SCENARIO = r"""
(async () => {
  const out = {errs: [], перерисовки: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));

  // Какую вкладку топбара подсветит роутер на каждом адресе этих переходов.
  out.подсветка = ['#/projects', '#/project/p_bc85c4bc85', '#/project/p_bc85c4bc85/tts',
                   '#/project/p_bc85c4bc85/cover', '#/channels', '#/queue', '']
    .map(h => (ROUTES.find(r => r[1](h || '#/channels')) || [''])[0]);

  const PROJECT = {project: {id: 'p_bc85c4bc85', name: 'Проба', release_id: '', phase: '',
                             applied_preset: null,
                             stage: {segments: [{stage: 'text', state: 'done'}],
                                     caption: 'готово', tone: 'ok'}},
                   files: [], paid: [], file_base: '/f/', zip_base: '/z/'};
  window.api = async (p) => {
    if (p.indexOf('/ui/projects/') === 0) return PROJECT;
    if (p === '/ui/presets') return {groups: []};
    return {};
  };
  // Содержимое вкладок не рисуем: проверяется шапка и переходы, а не этапы.
  window.renderScenarioTab = async () => {};
  window.renderStageTab = async () => {};
  window.renderCoverTab = async () => {};
  // Роутер подменён: нас интересует, на каком адресе он был позван, а не что нарисовал.
  window.render = async () => { out.перерисовки.push(location.hash); };
  addEventListener('hashchange', render);

  location.hash = '#/project/p_bc85c4bc85';
  await new Promise(r => setTimeout(r, 200));
  await viewProject('p_bc85c4bc85');

  const back = document.querySelector('a.back');
  out.ссылка_назад = back ? back.textContent.trim() : '';
  out.куда_назад = back ? back.getAttribute('href') : '';

  const tabs = [...document.querySelectorAll('.subtabs a')];
  out.вкладки = tabs.map(a => a.textContent.trim());

  // ── переключение вкладок: адрес меняется, шагов истории не прибавляется ──
  const шагов_до = history.length;
  tabs[1].click();                                  // «Озвучка»
  await new Promise(r => setTimeout(r, 200));
  out.адрес_после_вкладки = location.hash;
  out.шагов_после_вкладки = history.length - шагов_до;

  tabs[4].click();                                  // «Рендер»
  await new Promise(r => setTimeout(r, 200));
  out.адрес_после_второй = location.hash;
  out.шагов_после_двух = history.length - шагов_до;
  out.роутер_позвали = out.перерисовки.slice(-2);

  // Cmd/Ctrl-клик — просьба открыть отдельно, её перехватывать нельзя.
  const ev = new MouseEvent('click', {bubbles: true, cancelable: true, metaKey: true});
  tabs[0].dispatchEvent(ev);
  out.командный_клик_не_перехвачен = ev.defaultPrevented === false;

  // Контроль: обычная ссылка шаг истории добавляет — значит, измеряем мы правильно,
  // и «не прибавилось» выше не артефакт headless-браузера.
  const шагов_перед_обычной = history.length;
  back.click();
  await new Promise(r => setTimeout(r, 200));
  out.адрес_после_назад = location.hash;
  out.обычная_ссылка_прибавила = history.length - шагов_перед_обычной;

  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=10000)
    if res.get("error"):
        print("  ПРОПУЩЕНО:", res["error"])
        return 2          # пропуск не успех: без браузера проверка не выполнялась

    print("Ссылка назад у экрана проекта — в том же виде, что у соседних экранов")
    check("падений нет", res.get("errs"), [])
    check("ссылка называется как у соседей", res.get("ссылка_назад"), "← ко всем проектам")
    check("ведёт к списку проектов", res.get("куда_назад"), "#/projects")

    print("\nВкладки проекта не копят историю браузера")
    check("шесть вкладок на месте", res.get("вкладки"),
          ["Сценарий", "Озвучка", "Изображения", "Видео", "Рендер", "Обложка"])
    check("адрес вкладки в хеше", res.get("адрес_после_вкладки"),
          "#/project/p_bc85c4bc85/tts")
    check("первый клик по вкладке шага истории не добавил",
          res.get("шагов_после_вкладки"), 0)
    check("адрес второй вкладки в хеше", res.get("адрес_после_второй"),
          "#/project/p_bc85c4bc85/render")
    check("два клика по вкладкам — по-прежнему ноль новых шагов",
          res.get("шагов_после_двух"), 0)
    check("роутер позван на каждом переходе (экран перерисовывается)",
          res.get("роутер_позвали"),
          ["#/project/p_bc85c4bc85/tts", "#/project/p_bc85c4bc85/render"])
    check("Cmd/Ctrl-клик по вкладке не перехвачен (откроется отдельной вкладкой браузера)",
          res.get("командный_клик_не_перехвачен"), True)
    check("контроль: обычная ссылка шаг истории добавляет",
          res.get("обычная_ссылка_прибавила"), 1)
    check("«← ко всем проектам» ведёт к списку", res.get("адрес_после_назад"), "#/projects")

    print("\nПодсветка вкладки топбара соответствует экрану")
    check("проекты, проект и его вкладки — «Проекты»; ловушка ROUTES — «Каналы»",
          res.get("подсветка"),
          ["projects", "projects", "projects", "projects", "channels", "queue", "channels"])

    print("\nПРОВАЛЕНО:" if FAILS else "\nвсе проверки прошли")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
