#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Сделать всё» объясняет, что поставлено и чего оно НЕ делает.

Жалоба владельца 20.09.2026 дословно: «нажал сделать всё, но не вижу чтобы начался
делаться сценарий и обложка». Денежную часть закрыл кабинет (за пустой проект больше
не платят, а в ответе появились поля `stages` и `note`), но на экране человек
по-прежнему видел молчание. Здесь проверяется ровно то, что он видит:

  * смешанная пачка (часть проектов уехала, часть пропущена) называет И числа, И
    причину. До правки причина показывалась только при `!enq && !def` — то есть
    когда не поставлено НИЧЕГО; такого ответа не бывает вовсе: если не уехал ни один
    проект, кабинет отвечает отказом 400, и ответ с `block_reason` до экрана не
    доходит. Условие было мёртвым, а пропущенный проект исчезал молча;
  * «Сделать всё» отвечает нашим <dialog> (не alert и не тост на пять секунд), и в
    окне сказано, что входит в «всё» (`stages` ответа) и что сценарий с обложкой
    заказываются своими кнопками (`note` ответа) — слова берутся из ответа, а не
    пересказываются экраном;
  * окно переживает перерисовку экрана: через 1,2 с после нажатия `viewProjects()`
    строит список заново, и врезка внутри карточки исчезла бы вместе с ним;
  * та же кнопка на экране одного проекта объясняет то же самое и тем же окном;
  * чистый прогон остаётся коротким тостом: окно — только там, где есть граница,
    о которой человек спотыкается.

Сети и стенда не нужно: `api()` и `post()` подменены, браузер настоящий.

    .venv/bin/python tests/test_batch_pipeline_report_browser.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []

# Причина отказа приходит от кабинета дословно — экран её не сочиняет и не сокращает.
REASON = ("нет ключа провайдера FastGen: ни серверного, ни своего — "
          "добавьте ключ в «Ключи»")
NOTE = ("Сценарий и обложка сюда не входят: их делают соседние сервисы — "
        "текст заказывается кнопкой «Текст», обложка — кнопкой «Обложка».")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def has(label, got, needle):
    if needle in (got or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали: {needle!r}\n        в тексте: {got!r}")
        FAILS.append(label)


def hasnt(label, got, needle):
    if needle not in (got or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        не должно было быть: {needle!r}"
              f"\n        в тексте: {got!r}")
        FAILS.append(label)


SCENARIO = r"""
(async () => {
  const out = {errs: [], sent: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));

  const ready = {text: true, audio: false, images: 0, clips: 0, render: false};
  const proj = (id, name) => ({id, name, ready, busy: '', release_id: '', phase: '',
                               phase_title: '', updated: 1758000000, plan_item_id: 0});
  const PROJECTS = {projects: [proj('p1', 'Первый'), proj('p2', 'Второй'),
                               proj('p3', 'Третий')]};
  const FIELDS = {kinds: {tts: {title: 'Озвучка', fields: []},
                          images: {title: 'Картинки', fields: []}},
    pult: {text:  {title: 'Текст',   post: '/ui/projects/batch/text'},
           cover: {title: 'Обложка', post: '/ui/projects/batch/cover'}}};

  // Один проект — тем же составом полей, что отдаёт `/ui/projects/{pid}` пульта
  // (web/app.py, `ui_project`): экран проекта строится по всему ответу целиком.
  const ONE = {
    project: {id: 'p1', name: 'Первый', status: '', created: 1758000000,
              updated: 1758000000, release_id: '', phase: '',
              stage: {segments: [], caption: '', tone: 'dim'}, applied_preset: null},
    ready, jobs: [], files: [], paid: [], source: 'короткий текст',
    file_base: '/p/p1/file/', zip_base: '/api/projects/p1/download/'};

  window.api = async (p) => {
    if (p === '/ui/projects') return PROJECTS;
    if (p === '/ui/projects/batch/fields') return FIELDS;
    if (p === '/ui/projects/p1') return ONE;
    if (p === '/ui/presets') return {groups: []};
    // С `project_id=…` в адресе тоже: вкладка «Сценарий» спрашивает задачи проекта.
    if (p.startsWith('/ui/text')) return {jobs: [], modes: [], presets: {generation: []},
      models: [], languages: [{code: 'ru', title: 'Русский'}]};
    if (p.startsWith('/ui/covers')) return {jobs: [], modes: [], rules: [],
      languages: ['Русский'], presets: [], options: []};
    return {};
  };

  const REASON = 'нет ключа провайдера FastGen: ни серверного, ни своего — '
               + 'добавьте ключ в «Ключи»';
  const NOTE = 'Сценарий и обложка сюда не входят: их делают соседние сервисы — '
             + 'текст заказывается кнопкой «Текст», обложка — кнопкой «Обложка».';
  // Смешанная пачка: два проекта уехали, третий пропущен по причине. Ровно тот ответ,
  // на котором экран молчал.
  const PIPE_MIXED = {ok: true, kind: 'pipeline', enqueued: 4, deferred: 4, skipped: 0,
    blocked: 1, block_reason: REASON,
    stages: ['tts', 'images', 'video', 'render'], note: NOTE};
  // Один проект с экрана проекта: всё поставлено, пропущенных нет.
  const PIPE_ONE = {ok: true, kind: 'pipeline', enqueued: 2, deferred: 2, skipped: 0,
    blocked: 0, block_reason: '',
    stages: ['tts', 'images', 'video', 'render'], note: NOTE};
  // Отдельный этап: та же смешанная пачка, но кнопкой «Озвучка» — тост, не окно.
  const TTS_MIXED = {ok: true, kind: 'tts', enqueued: 2, deferred: 0, skipped: 0,
    blocked: 1, block_reason: REASON};
  const TTS_CLEAN = {ok: true, kind: 'tts', enqueued: 3, deferred: 0, skipped: 0,
    blocked: 0, block_reason: ''};
  let ttsAnswer = TTS_MIXED;

  window.post = async (p, b) => {
    out.sent.push({path: p, body: b});
    if (p === '/ui/projects/batch/pipeline') return PIPE_MIXED;
    if (p === '/ui/projects/p1/run') return PIPE_ONE;
    if (p === '/ui/projects/batch/tts') return ttsAnswer;
    return {ok: true};
  };
  const flat = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const toasts = () => [...document.querySelectorAll('#toasts .toast')];
  const lastToast = () => toasts().slice(-1)[0] || null;
  const pause = (ms) => new Promise(r => setTimeout(r, ms));
  const pickAll = () => [...document.querySelectorAll('input.pick')].forEach(cb => {
    cb.checked = true; cb.dispatchEvent(new Event('change'));
  });

  await viewProjects();
  await pause(300);                       // варианты полей подтягиваются в фоне

  // ── «Сделать всё» на пачку: смешанный ответ ────────────────────────────────
  pickAll();
  document.getElementById('batchgo').click();
  await pause(300);
  out.запрос = out.sent[0] || null;
  const dlg = document.querySelector('dialog[open]');
  out.окно_открылось = !!dlg;
  out.заголовок = dlg ? flat(dlg.querySelector('.sec')) : '';
  out.в_окне = flat(dlg && dlg.querySelector('.mbody'));
  out.кнопка_окна = dlg ? dlg.querySelector('.m-ok').textContent.trim() : '';
  out.вторая_кнопка = dlg ? dlg.querySelector('.m-no').textContent.trim() : '';
  out.причина_красная = !!(dlg && dlg.querySelector('.mbody .note.bad'));
  // Тоста здесь быть не должно: пять секунд на такое объяснение — это и есть молчание.
  out.тостов_нет = (toasts().length === 0);

  // Через 1,2 с экран перерисовывается сам. Окно обязано пережить перерисовку.
  await pause(1600);
  out.перерисовка_была = (document.querySelectorAll('input.pick').length === 3
                          && !document.querySelector('input.pick:checked'));
  const dlg2 = document.querySelector('dialog[open]');
  out.окно_живо_после_перерисовки = !!dlg2;
  out.в_окне_после = flat(dlg2 && dlg2.querySelector('.mbody'));
  if (dlg2) dlg2.querySelector('.m-ok').click();
  await pause(200);
  out.окно_закрылось = !document.querySelector('dialog[open]');

  // ── Отдельный этап: смешанная пачка объясняется тостом ─────────────────────
  pickAll();
  document.querySelector('button.bgo[data-kind="tts"]').click();
  await pause(300);
  const t1 = lastToast();
  out.тост_смешанный = t1 ? t1.textContent : '';
  out.тост_смешанный_цвет = t1 ? t1.className : '';
  out.окна_на_этапе_нет = !document.querySelector('dialog[open]');
  await pause(1600);                      // перерисовка после этапной кнопки

  // ── Чистый прогон этапа: короткий зелёный тост, без причины ────────────────
  ttsAnswer = TTS_CLEAN;
  pickAll();
  document.querySelector('button.bgo[data-kind="tts"]').click();
  await pause(300);
  const t2 = lastToast();
  out.тост_чистый = t2 ? t2.textContent : '';
  out.тост_чистый_цвет = t2 ? t2.className : '';
  await pause(1600);

  // ── Та же кнопка на экране одного проекта ──────────────────────────────────
  let экранПроекта = true;
  try{ await viewProject('p1'); }catch(e){ экранПроекта = false;
                                           out.ошибка_экрана = String(e && e.message || e); }
  out.экран_проекта_открылся = экранПроекта;
  const runall = document.getElementById('runall');
  if (runall){
    runall.click();
    document.getElementById('rgo').click();
    await pause(300);
    const dlg3 = document.querySelector('dialog[open]');
    out.окно_проекта = !!dlg3;
    out.в_окне_проекта = flat(dlg3 && dlg3.querySelector('.mbody'));
    if (dlg3) dlg3.querySelector('.m-ok').click();
  }

  // ── Старый сервер: карточек соседей на экране нет ──────────────────────────
  // `/ui/projects/batch/fields` без `pult` — карточки «Текст» и «Обложка» не
  // рисуются. Оговорка сервера остаётся, а указание «карточки в этой же панели»
  // обязано исчезнуть: посылать к несуществующей кнопке хуже, чем молчать.
  FIELDS.pult = {};
  await viewProjects();
  await pause(300);
  out.карточек_соседей_нет = !document.querySelector('[data-pult="text"]');
  pickAll();
  document.getElementById('batchgo').click();
  await pause(300);
  const dlg4 = document.querySelector('dialog[open]');
  out.в_окне_без_карточек = flat(dlg4 && dlg4.querySelector('.mbody'));
  if (dlg4) dlg4.querySelector('.m-ok').click();

  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=20000)
    if res.get("error"):
        print("  ПРОПУЩЕНО:", res["error"])
        return 2          # пропуск не успех: без браузера проверка не выполнялась

    print("Пачка ушла в кабинет одним запросом")
    ask = res.get("запрос") or {}
    check("адрес пакетного «Сделать всё»", ask.get("path"), "/ui/projects/batch/pipeline")
    check("все отмеченные уехали", (ask.get("body") or {}).get("pids"), ["p1", "p2", "p3"])

    print("\nОтвет объяснён окном, а не молчанием")
    check("окно открылось", res.get("окно_открылось"), True)
    check("это наш <dialog>, и он озаглавлен", res.get("заголовок"),
          "Сделать всё: что поставлено×")
    check("кнопка окна закрывает, а не «отменяет пачку»", res.get("кнопка_окна"), "Понятно")
    check("и вторая кнопка тоже про закрытие", res.get("вторая_кнопка"), "Закрыть")
    check("тостом это не дублируется", res.get("тостов_нет"), True)

    print("\nЧисла названы полностью")
    has("сколько отмечено", res.get("в_окне"), "отмечено проектов: 3")
    has("сколько поставлено", res.get("в_окне"), "поставлено задач: 4")
    has("сколько отложено", res.get("в_окне"), "отложено: 4")
    has("сколько пропущено", res.get("в_окне"), "пропущено: 1")

    print("\nПропущенный проект не исчезает молча (жалоба 20.09.2026)")
    has("причина названа дословно", res.get("в_окне"), REASON)
    has("сказано про деньги", res.get("в_окне"), "Денег на них не потрачено")
    has("сказано, что делать", res.get("в_окне"), "нажмите только для пропущенных")
    check("причина выделена как отказ", res.get("причина_красная"), True)

    print("\nСказано, что такое «всё» и чего оно не делает")
    has("этапы из ответа сервера", res.get("в_окне"),
        "В «сделать всё» входит: озвучка → картинки → видео → рендер.")
    has("и оговорка про сценарий с обложкой — словами сервера", res.get("в_окне"), NOTE)
    has("с указанием, где эти кнопки на экране", res.get("в_окне"),
        "Карточки «Текст» и «Обложка» — в этой же панели")
    has("отложенное объяснено: стартует само", res.get("в_окне"),
        "стартует само, когда дозреют материалы")

    print("\nОкно переживает перерисовку экрана через 1,2 с")
    check("перерисовка действительно была", res.get("перерисовка_была"), True)
    check("окно на месте", res.get("окно_живо_после_перерисовки"), True)
    has("и текст в нём тот же", res.get("в_окне_после"), REASON)
    check("по «Понятно» окно закрылось", res.get("окно_закрылось"), True)

    print("\nОтдельный этап: смешанная пачка объяснена тостом")
    has("числа поставленного", res.get("тост_смешанный"), "поставлено задач: 2")
    has("число пропущенного", res.get("тост_смешанный"), "пропущено: 1")
    has("и причина — в том же тосте", res.get("тост_смешанный"), REASON)
    has("цвет не зелёный: часть пачки требует внимания",
        res.get("тост_смешанный_цвет"), "warn")
    check("окном этап не отвечает — там нет границы, о которой спотыкаются",
          res.get("окна_на_этапе_нет"), True)

    print("\nЧистый прогон остаётся коротким")
    has("числа на месте", res.get("тост_чистый"), "поставлено задач: 3")
    hasnt("причины нет — пропущенных нет", res.get("тост_чистый"), "пропущено")
    has("и тост зелёный", res.get("тост_чистый_цвет"), "ok")

    print("\nТа же кнопка на экране одного проекта")
    check("экран проекта открылся", res.get("экран_проекта_открылся"), True)
    if res.get("ошибка_экрана"):
        print("        экран упал на:", res["ошибка_экрана"])
    check("«Сделать всё» отвечает окном", res.get("окно_проекта"), True)
    has("с теми же этапами", res.get("в_окне_проекта"), "В «сделать всё» входит")
    has("и с оговоркой сервера", res.get("в_окне_проекта"), NOTE)
    has("с указанием, где заказывают на этом экране", res.get("в_окне_проекта"),
        "вкладки «Сценарий» и «Обложка»")

    print("\nСтарый сервер без карточек соседей: к несуществующей кнопке не посылаем")
    check("карточек «Текст» и «Обложка» на экране нет",
          res.get("карточек_соседей_нет"), True)
    has("оговорка сервера на месте", res.get("в_окне_без_карточек"), NOTE)
    hasnt("а указания на карточки — нет", res.get("в_окне_без_карточек"),
          "Карточки «Текст» и «Обложка»")

    print("\nОшибок страницы нет")
    check("ошибок в консоли", res.get("errs"), [])

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("ВСЁ ХОРОШО")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
