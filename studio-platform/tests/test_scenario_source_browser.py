#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Блок «Откуда взять сценарий» на вкладке проекта — поведение в настоящем браузере.

Жалоба владельца 20.09.2026, дословно: «ссылку на донора не сохранило, плюс на этой
странице не понимаю почему у нас сверху есть ссылка на донора а потом снизу висит
исходный текст какой то, лучше вверх переместить и дать на выбор какой источник тебе
удобен, желательно чтобы в строку он и текст и ссылку распознавал, а файл сделать
кастомную кнопку upload как в муи а не дефолтную».

Проверяется то, что человек видит и на что тратит деньги:

  * блок источника — ПЕРВАЯ карточка вкладки, и отдельной врезки «Исходный текст»
    внизу больше нет: сценарий проекта живёт в том же блоке, свёрнутым, с подписью
    «сколько знаков», а не безымянным полем непонятно про что;
  * записанная у проекта ссылка донора подставляется в поле при открытии вкладки —
    это вторая половина жалобы «не сохранило»;
  * поле ОДНО на ссылку и на текст, а что именно вставили, решает СЕРВЕР
    (`POST /ui/source/detect`): экран печатает его ответ («ссылка на ролик»,
    «текст, N знаков») и его же `problem`, своих правил разбора не заводит;
  * предупреждение сервера («по этой ссылке уже заказывали») видно ДО нажатия
    платной кнопки, а не прилетает после списания;
  * разбор, который сервер не принимает (две ссылки в поле), выключает платную
    кнопку, и нажатие ничего не отправляет;
  * заказ уходит с режимом и источником ИЗ ОТВЕТА разбора, а не из поля: голый
    идентификатор ролика сервер собирает в ссылку, и уехать должна она;
  * бесплатное действие отделено от платного: «Сохранить сценарий» и запись ссылки
    шлют только `/ui/projects/{pid}/source`, ни одного вызова рерайта;
  * ссылка донора запоминается САМА, как только сервер её распознал (владелец
    21.09.2026: «блок с запомнить ссылку убираем»): кнопки у ссылки нет, записанную
    повторно не пишем, а забытую не возвращаем даже при перерисовке экрана;
  * файл выбирается своей кнопкой; дефолтный `<input type=file>` остаётся в DOM и
    НЕ display:none — иначе он выпал бы из обхода Tab и файл нельзя было бы выбрать
    без мыши. Чтение .txt — в браузере, не-UTF-8 отказывается с внятной причиной.

Находки рецензента 21.09.2026 — здесь же, потому что ломается всё это на том же
экране и теми же руками:

  * [ДАННЫЕ] «Сохранить сценарий» больше не затирает существующий `source.txt` молча:
    когда в проекте уже есть текст и он ДРУГОЙ, экран спрашивает своим окном. До
    ответа «Да» на сервер не уходит ничего, ответ «Нет» оставляет сценарий как был.
    Врать при этом нельзя вдвойне: рядом висит предупреждение сервера «в проекте уже
    есть сценарий», и человек читает его как обещание, что текст цел;
  * ссылка выпуска канала и записанный источник — две РАЗНЫЕ мысли, и «по этой ссылке
    заказывали текст» относится к той, по которой платили, а не к выпуску. Сервер
    склеивает их в одно поле `why`; экран разводит обратно;
  * записанную ссылку можно забыть: сервер это умел всегда (пустая ссылка чистит
    запись), экран такого запроса не слал;
  * поле источника — ОДНА строка (владелец: «чтобы в строку он и текст и ссылку
    распознавал»), которая растёт, когда в неё вставили сценарий.

Сети и стенда не нужно: `api()` и `post()` подменены, браузер настоящий.

    .venv/bin/python tests/test_scenario_source_browser.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []

FILE_TEXT = "Сценарий из файла: первый абзац.\nВторой абзац — с переводом строки."


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def hasnt(label, got, needle):
    if needle not in (got or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        не должно было быть: {needle!r}"
              f"\n        а в тексте: {got!r}")
        FAILS.append(label)


def has(label, got, needle):
    if needle in (got or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали: {needle!r}\n        в тексте: {got!r}")
        FAILS.append(label)


SCENARIO = r"""
(async () => {
  const out = {errs: [], sent: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));

  const ССЫЛКА = 'https://youtu.be/Ng0hmsrr7l4';
  const ДРУГАЯ = 'https://youtu.be/bbbbbbbbbbb';
  const СОБРАННАЯ = 'https://www.youtube.com/watch?v=Ng0hmsrr7l4';
  const СЦЕНАРИЙ = 'Старый сценарий проекта.';
  let SOURCE = СЦЕНАРИЙ;              // что лежит в `source.txt` проекта прямо сейчас

  // Пометка источника у проекта — тем же видом, что отдаёт сервер.
  const origin = (o) => Object.assign({kind: '', value: '', donor_url: '', video_id: '',
    topic: '', file_name: '', release_url: '', from: '', ordered: 0, updated: 1789000000,
    scenario_chars: СЦЕНАРИЙ.length, why: ''}, o);

  const ЗАПИСАН = origin({kind: 'link', value: ССЫЛКА, donor_url: ССЫЛКА,
    video_id: 'Ng0hmsrr7l4', from: 'project', ordered: 1789000000,
    why: 'по этой ссылке заказывали текст 20.09 21:15'});

  let ORIGIN = ЗАПИСАН;
  const D = () => ({
    project: {id: 'p1', name: 'Первый', status: '', created: 1, updated: 1,
              release_id: '', phase: '', stage: '', applied_preset: ''},
    ready: {}, jobs: [], files: [], paid: null, source: SOURCE, origin: ORIGIN});

  const TEXTDATA = {jobs: [], modes: [
      {key: 'link', title: 'Ссылка на YouTube', note: 'текст берётся из субтитров ролика'},
      {key: 'text', title: 'Вставленный текст', note: 'свой текст, без донора'},
      {key: 'topic', title: 'С нуля по теме', note: 'текста нет, есть тема и бриф'}],
    models: [{value: 'gpt-4.1', label: 'GPT-4.1'}],
    presets: {generation: ['Бриф канала'], rewrite: []},
    languages: [{code: 'ru', title: 'Русский'}, {code: 'pl', title: 'Польский'}],
    channels: []};

  window.api = async (p) => {
    if (p.startsWith('/ui/text')) return TEXTDATA;
    if (p.startsWith('/ui/projects/p1')) return D();
    return {};
  };

  // Ответы разбора — ровно того вида, что отдаёт `POST /ui/source/detect`.
  const ПОВТОР = 'по этой ссылке текст для проекта уже заказывали 20.09 21:15 — '
               + 'повтор создаст вторую задачу и второй платный рерайт';
  const ПРО_СЦЕНАРИЙ = 'в проекте уже есть сценарий (' + СЦЕНАРИЙ.length + ' знаков): '
               + 'новый текст его не заменит, пока вы не нажмёте «Взять в проект»';
  let ДЕТЕКТ_ПАДАЕТ = false;
  const detect = (v) => {
    const raw = (v || '').trim();
    if (!raw) return {kind: 'empty', mode: '', value: '', url: '', video_id: '', chars: 0,
      links: [], title: 'пусто', note: '', problem: 'источник не задан',
      topic_possible: false, warn: ''};
    if (raw === ССЫЛКА) return {kind: 'link', mode: 'link', value: ССЫЛКА, url: ССЫЛКА,
      video_id: 'Ng0hmsrr7l4', chars: raw.length, links: [ССЫЛКА],
      title: 'ссылка на ролик YouTube', note: 'текст возьмём из субтитров этого ролика',
      problem: '', topic_possible: false, warn: ПОВТОР};
    if (raw === ДРУГАЯ) return {kind: 'link', mode: 'link', value: ДРУГАЯ, url: ДРУГАЯ,
      video_id: 'bbbbbbbbbbb', chars: raw.length, links: [ДРУГАЯ],
      title: 'ссылка на ролик YouTube', note: 'текст возьмём из субтитров этого ролика',
      problem: '', topic_possible: false,
      // Как сервер: предупреждение считается от того, что записано СЕЙЧАС.
      warn: (ORIGIN.donor_url && ORIGIN.donor_url !== ДРУГАЯ)
        ? 'у проекта записана другая ссылка — заказ по новой её заменит' : ''};
    if (raw === 'Ng0hmsrr7l4') return {kind: 'link', mode: 'link', value: СОБРАННАЯ,
      url: СОБРАННАЯ, video_id: 'Ng0hmsrr7l4', chars: raw.length, links: [СОБРАННАЯ],
      title: 'идентификатор ролика YouTube',
      note: 'это одиннадцать знаков идентификатора — считаем ссылкой на ролик',
      problem: '', topic_possible: false, warn: ПОВТОР};
    if (raw.indexOf(' ') > 0 && raw.split(' ').every(x => x.startsWith('http')))
      return {kind: 'many_links', mode: '', value: raw, url: '', video_id: '',
        chars: raw.length, links: raw.split(' '), title: 'несколько ссылок', note: '',
        problem: 'в поле 2 ссылки, а ролик делается из одного донора — оставьте одну',
        topic_possible: false, warn: ''};
    return {kind: 'text', mode: 'text', value: raw, url: '', video_id: '',
      chars: raw.length, links: [], title: 'текст',
      note: raw.length + ' знаков — отправим в рерайт как есть', problem: '',
      topic_possible: false, warn: ПРО_СЦЕНАРИЙ};
  };

  let ЗАПИСЬ_ПАДАЕТ = false;
  window.post = async (p, b) => {
    out.sent.push({path: p, body: b});
    if (p === '/ui/projects/p1/source' && ЗАПИСЬ_ПАДАЕТ) throw new Error('кабинет недоступен');
    if (p === '/ui/source/detect'){
      if (ДЕТЕКТ_ПАДАЕТ) throw new Error('сеть недоступна');
      return detect(b.value);
    }
    if (p === '/ui/projects/p1/source'){
      if (b.link) ORIGIN = origin({kind: 'link', value: b.link,
        donor_url: b.link, video_id: b.link === ДРУГАЯ ? 'bbbbbbbbbbb' : 'Ng0hmsrr7l4',
        from: 'project', why: ''});
      else if (b.link === ''){
        // Так отвечает сервер на пустую ссылку (`ui_project_source_save`): запись о
        // доноре стёрта, пометка источника снята, сценарий не тронут.
        ORIGIN = origin({kind: SOURCE ? 'text' : '', value: '', donor_url: '',
                         video_id: '', from: 'project', ordered: 0,
                         scenario_chars: SOURCE.length});
      }
      if (b.text !== undefined){
        SOURCE = b.text;
        ORIGIN = origin(b.file
          ? {kind: 'file', file_name: b.file, scenario_chars: b.text.length}
          : {kind: 'text', scenario_chars: b.text.length});
      }
      return {ok: true, origin: ORIGIN};
    }
    if (p === '/ui/text') return {ok: true, job: {job_id: 'rw-9'},
      origin: origin({kind: 'link', value: b.source, donor_url: b.source,
        video_id: 'Ng0hmsrr7l4', ordered: 1789000000,
        why: 'по этой ссылке заказывали текст 20.09 21:15'})};
    return {ok: true};
  };

  const flat = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const toasts = () => [...document.querySelectorAll('#toasts .toast')].map(t => t.textContent);
  const pause = (ms) => new Promise(r => setTimeout(r, ms));
  const q = (sel) => document.querySelector(sel);
  // Наше окно вопроса (`confirmModal`). Закрытое окно из DOM удаляется — значит
  // `окно()` отвечает не «есть ли разметка», а «спрашивают ли прямо сейчас».
  const окно = () => document.querySelector('dialog.modal');
  const ответить = async (ответ) => {
    const d = окно();
    if (!d) throw new Error('вопроса не было, отвечать нечему: ' + ответ);
    d.querySelector(ответ === 'Да' ? '.m-ok' : '.m-no').click();
    await pause(400);
  };
  try{
  // Видно ли это человеку на самом деле: атрибут `hidden` слабее inline display,
  // и проверка атрибута прошла бы там, где элемент остался на экране.
  const видно = (sel) => getComputedStyle(q(sel)).display !== 'none';

  // Свежий заход на вкладку: состояние вкладки живёт в SCEN_STATE между перерисовками,
  // и «открыть заново» — это именно сброс его вместе с экраном.
  const открыть = async () => {
    clearAllLogTimers();
    delete SCEN_STATE.p1;
    app.innerHTML = '';
    await renderScenarioTab(D());
    await pause(400);                 // разбор подставленного поля (debounce + ответ)
  };
  const набрать = async (v) => {
    const f = q('#tx-any');
    f.value = v;
    f.dispatchEvent(new Event('input'));
    await pause(500);
  };

  // ── 1. Один блок в начале вкладки ───────────────────────────────────────────
  await открыть();
  out.первая_карточка = flat(app.firstElementChild.querySelector('.sec'));
  out.карточек = [...app.querySelectorAll('.card > .sec')].map(flat);
  out.врезки_исходный_текст_нет = !flat(app).includes('Исходный текст');
  out.поле_одно = app.querySelectorAll('#tx-any').length;
  out.сценарий_свёрнут = !q('#src-box').open;
  out.подпись_сценария = flat(q('#src-sum'));
  out.сценарий_на_месте = q('#src-text').value;
  out.режимы = [...app.querySelectorAll('.modes button')].map(b => flat(b));

  // ── 2. Ссылка проекта подставлена и разобрана ДО нажатия ────────────────────
  out.ссылка_подставлена = q('#tx-any').value;
  out.что_записано = flat(q('#or-now'));
  out.разбор_запрошен = out.sent.filter(s => s.path === '/ui/source/detect')
    .map(s => s.body.value);
  out.разбор_с_проектом = (out.sent.find(s => s.path === '/ui/source/detect') || {}).body;
  out.распознано = flat(q('#tx-seen'));
  out.предупреждение_видно = видно('#tx-warn');
  out.предупреждение = flat(q('#tx-warn'));
  out.платных_вызовов_пока = out.sent.filter(s => s.path === '/ui/text').length;
  out.кнопки_у_ссылки_нет = !видно('#tx-keep');
  out.бесплатная_подпись = flat(q('#tx-keep-why'));
  // Подставленная ссылка — та же, что записана: писать её заново незачем.
  out.записанную_не_переписали = !out.sent.some(s => s.path === '/ui/projects/p1/source');
  out.заказ_без_языков_выключен = q('#tx-go').disabled;
  out.заказ_без_языков_почему = flat(q('#tx-go-why'));

  // ── 3. Новая ссылка запоминается сама — бесплатно, рерайт не зовём ──────────
  const былоЗаказов = out.sent.filter(s => s.path === '/ui/text').length;
  await набрать(ДРУГАЯ);
  out.запрос_ссылки = out.sent.filter(s => s.path === '/ui/projects/p1/source')
                              .slice(-1)[0] || null;
  out.заказов_после_запоминания = out.sent.filter(s => s.path === '/ui/text').length
                                 - былоЗаказов;
  out.после_записи_записано = flat(q('#or-now'));
  // Разбор спрошен до записи и говорил «заказ по новой её заменит»; после записи это
  // неправда, и экран обязан спросить заново.
  out.после_записи_разбор_переспрошен = out.sent.filter(
      s => s.path === '/ui/source/detect' && s.body.value === ДРУГАЯ).length;
  out.после_записи_предупреждение_снято = !видно('#tx-warn');
  out.после_записи_подпись = flat(q('#tx-keep-why'));
  out.тостов_о_записи = toasts().filter(t => t.includes('запомнена')).length;
  // Тот же ролик другой формой адреса — запись не переписывается.
  const былоЗаписей = out.sent.filter(s => s.path === '/ui/projects/p1/source').length;
  await набрать('https://youtu.be/bbbbbbbbbbb ');
  out.та_же_ссылка_не_переписана = (out.sent.filter(
      s => s.path === '/ui/projects/p1/source').length === былоЗаписей);

  // ── 4. В то же поле — текст: распознан текстом, кнопка сменила смысл ─────────
  await набрать('Совсем другой текст, вставленный руками.');
  out.текст_распознан = flat(q('#tx-seen'));
  out.текст_кнопка = flat(q('#tx-keep'));
  out.текст_подпись = flat(q('#tx-keep-why'));
  out.текст_предупреждение = flat(q('#tx-warn'));

  // языки выбираются — без них заказ не даём
  const ru = [...app.querySelectorAll('#tx-langs label')].find(l => l.dataset.code === 'ru');
  ru.querySelector('input').checked = true;
  ru.querySelector('input').dispatchEvent(new Event('change'));
  out.заказ_с_языком_доступен = !q('#tx-go').disabled;
  out.заказ_подпись_текста = flat(q('#tx-go-why'));

  // ── 5. Две ссылки в поле: сервер так заказывать не даёт ─────────────────────
  await набрать('https://youtu.be/aaaaaaaaaaa https://youtu.be/bbbbbbbbbbb');
  out.две_ссылки_видно = flat(q('#tx-seen'));
  out.две_ссылки_заказ_выключен = q('#tx-go').disabled;
  out.две_ссылки_сохранение_выключено = q('#tx-keep').disabled;
  const былоВсего = out.sent.length;
  q('#tx-go').click();
  await pause(300);
  out.две_ссылки_ничего_не_ушло = (out.sent.length === былоВсего);

  // ── 6. Голый идентификатор: уезжает СОБРАННАЯ сервером ссылка ───────────────
  await набрать('Ng0hmsrr7l4');
  out.ид_распознан = flat(q('#tx-seen'));
  out.ид_предупреждение = flat(q('#tx-warn'));
  q('#tx-go').click();
  await pause(400);
  out.заказ = (out.sent.filter(s => s.path === '/ui/text').slice(-1)[0] || {}).body || null;
  out.тост_заказа = toasts().slice(-1)[0] || '';
  out.после_заказа_записано = flat(q('#or-now'));

  // ── 7. Разбор не отвечает: платную кнопку не даём ───────────────────────────
  ДЕТЕКТ_ПАДАЕТ = true;
  await набрать('Текст, который некому разобрать.');
  out.разбор_упал_видно = flat(q('#tx-seen'));
  out.разбор_упал_заказ_выключен = q('#tx-go').disabled;
  ДЕТЕКТ_ПАДАЕТ = false;

  // ── 7а. «С нуля по теме»: источника нет вовсе, нужен бриф ──────────────────
  await открыть();
  [...app.querySelectorAll('.modes button')].find(b => b.dataset.mode === 'topic').click();
  await pause(50);
  out.тема_поле_источника_скрыто = !видно('#tx-auto');
  out.тема_поле_темы_видно = видно('#tx-topic-box');
  out.тема_бриф_виден = видно('#tx-preset-box');
  out.тема_бесплатное_скрыто = !видно('#tx-keep-row');
  out.тема_сценарий_на_месте = !!q('#src-text');
  q('#tx-one').value = 'Как устроен конвейер';
  q('#tx-one').dispatchEvent(new Event('input'));
  // Язык выбирается ДО замера: иначе «выключено» означало бы только «нет языка», и
  // потерянное требование брифа прошло бы мимо проверки (поймано мутацией).
  const ru2 = [...app.querySelectorAll('#tx-langs label')].find(l => l.dataset.code === 'ru');
  ru2.querySelector('input').checked = true;
  ru2.querySelector('input').dispatchEvent(new Event('change'));
  out.тема_без_брифа_выключено = q('#tx-go').disabled;
  out.тема_без_брифа_почему = flat(q('#tx-go-why'));
  const бриф = q('#tx-preset');
  бриф.value = 'Бриф канала';
  бриф.dispatchEvent(new Event('change'));
  out.тема_заказ_доступен = !q('#tx-go').disabled;
  q('#tx-go').click();
  await pause(400);
  out.тема_заказ = (out.sent.filter(s => s.path === '/ui/text').slice(-1)[0] || {}).body || null;
  [...app.querySelectorAll('.modes button')].find(b => b.dataset.mode === 'auto').click();
  await pause(50);
  out.назад_поле_видно = видно('#tx-auto');
  out.назад_бесплатное_видно = видно('#tx-keep-row');

  // ── 8. Кнопка файла вместо дефолтной ────────────────────────────────────────
  await открыть();
  const inp = q('#src-file');
  const pick = q('.filepick .pickbtn');
  out.файл_кнопка_есть = flat(pick);
  out.файл_вход_в_метке = !!(inp && inp.closest('label.filepick'));
  out.файл_вход_не_display_none = inp ? getComputedStyle(inp).display !== 'none' : false;
  out.файл_вход_невидим = inp ? (inp.getBoundingClientRect().width <= 2) : false;
  out.файл_имя_до = flat(q('#tx-fname'));
  out.файл_снять_скрыто = !видно('#tx-fdrop');

  // Файл кладётся через DataTransfer — FileList настоящий, событие настоящее. Обёртка
  // вокруг onchange нужна только чтобы ДОЖДАТЬСЯ асинхронного чтения: время в стенде
  // виртуальное, и пауза проскочила бы мимо него.
  const pickFile = async (name, data) => {
    const dt = new DataTransfer();
    dt.items.add(new File([data], name, {type: 'text/plain'}));
    inp.files = dt.files;
    const orig = inp.onchange;
    let работа;
    inp.onchange = e => (работа = orig.call(inp, e));
    inp.dispatchEvent(new Event('change'));
    inp.onchange = orig;
    await работа;
    await pause(400);
  };

  await pickFile('сценарий.txt', __FILE_TEXT__);
  out.файл_лёг_в_поле = (q('#tx-any').value === __FILE_TEXT__);
  out.файл_имя_после = flat(q('#tx-fname'));
  out.файл_снять_видно = видно('#tx-fdrop');
  out.файл_вход_очищен = (inp.value === '');
  out.файл_тост = toasts().slice(-1)[0] || '';
  out.файл_распознан = flat(q('#tx-seen'));
  out.файл_кнопка_сохранения = flat(q('#tx-keep'));

  // Сохранение: текст И имя файла уезжают одной ручкой, платного вызова нет.
  // Сценарий у проекта уже есть и он другой — сначала обязан быть вопрос.
  const былоТекста = out.sent.filter(s => s.path === '/ui/text').length;
  const былоВсегоФ = out.sent.length;
  q('#tx-keep').click();
  await pause(300);
  out.файл_спросили = flat(окно());
  out.файл_до_ответа_молчим = (out.sent.length === былоВсегоФ);
  await ответить('Да');
  out.файл_запрос = out.sent.slice(-1)[0] || null;
  out.файл_платного_не_было = (out.sent.filter(s => s.path === '/ui/text').length
                               === былоТекста);
  out.файл_поле_освободилось = (q('#tx-any').value === '');
  out.файл_сценарий_обновился = (q('#src-text').value === __FILE_TEXT__);
  out.файл_подпись_сценария = flat(q('#src-sum'));
  out.файл_что_записано = flat(q('#or-now'));
  out.файл_тост_сохранения = toasts().slice(-1)[0] || '';

  // ── 9. Файл не в UTF-8: отказ с причиной, поле не тронуто ───────────────────
  const битый = new Uint8Array([0xd2, 0xe5, 0xea, 0xf1, 0xf2, 0x20, 0xe2, 0x20, 0x31, 0x32,
                                0x35, 0x31]);
  await pickFile('старый.txt', битый);
  out.кракозябры_поле_пустое = (q('#tx-any').value === '');
  out.кракозябры_тост = toasts().slice(-1)[0] || '';
  out.кракозябры_имя = flat(q('#tx-fname'));

  // ── 10. Две ссылки — две строки, и заказ приписан той, по которой платили ───
  // `why` здесь ровно такой, каким его склеивает сервер (`_project_origin`): сперва
  // про источник в поле, потом, через «; », про ссылку выпуска.
  SOURCE = СЦЕНАРИЙ;
  ORIGIN = origin({kind: 'link', value: ССЫЛКА, donor_url: ССЫЛКА, from: 'project',
    ordered: 1789000000, release_url: 'https://youtu.be/zzzzzzzzzzz',
    why: 'по этой ссылке заказывали текст 20.09 21:15; '
       + 'у выпуска канала записана другая ссылка — движок берёт её'});
  await открыть();
  out.выпуск_видно = видно('#or-release');
  out.выпуск_строка = flat(q('#or-release'));
  out.источник_строка = flat(q('#or-now'));

  // ── 11. Записанную ссылку можно забыть ──────────────────────────────────────
  ORIGIN = ЗАПИСАН;
  await открыть();
  out.забыть_видно = видно('#or-forget');
  const былоЗ = out.sent.length;
  q('#or-forget').click();
  await pause(300);
  out.забыть_спросили = flat(окно());
  out.забыть_до_ответа_молчим = (out.sent.length === былоЗ);
  await ответить('Нет');
  out.забыть_после_нет_молчим = (out.sent.length === былоЗ);
  out.забыть_после_нет_записано = flat(q('#or-now'));
  q('#or-forget').click();
  await pause(300);
  await ответить('Да');
  out.забыть_запрос = out.sent.slice(-1)[0] || null;
  out.забыть_записано = flat(q('#or-now'));
  out.забыть_кнопка_ушла = !видно('#or-forget');
  out.забыть_поле_цело = q('#tx-any').value;
  out.забыть_тост = toasts().slice(-1)[0] || '';
  // Экран проекта перерисовывается сам (после запуска, по возврату на вкладку), а
  // состояние вкладки при этом живёт. Забытая ссылка в поле не должна записаться
  // обратно ни при такой перерисовке, ни при повторном разборе.
  const былоПосле = out.sent.filter(s => s.path === '/ui/projects/p1/source').length;
  app.innerHTML = '';
  await renderScenarioTab(D());
  await pause(500);
  out.забытая_не_вернулась = (out.sent.filter(
      s => s.path === '/ui/projects/p1/source').length === былоПосле);
  out.забытая_подпись = flat(q('#tx-keep-why'));
  // А правка поля — ясная воля: ссылка снова запоминается сама.
  await набрать(ССЫЛКА + ' ');
  await набрать(ССЫЛКА);
  out.после_правки_записана = out.sent.filter(s => s.path === '/ui/projects/p1/source')
                                      .slice(-1)[0] || null;

  // Ссылка выпуска — чужая запись: её пульт не заводил и стереть не может.
  ORIGIN = origin({kind: 'link', value: 'https://youtu.be/zzzzzzzzzzz',
    donor_url: 'https://youtu.be/zzzzzzzzzzz', from: 'release',
    why: 'ссылка выпуска канала — из неё заведён проект'});
  await открыть();
  out.забыть_чужую_нельзя = !видно('#or-forget');

  // ── 12. Сценарий поверх существующего — только с ответом «Да» ───────────────
  SOURCE = СЦЕНАРИЙ;
  ORIGIN = ЗАПИСАН;
  await открыть();
  await набрать('Совсем другой сценарий, вставленный руками.');
  const былоС = out.sent.length;
  q('#tx-keep').click();
  await pause(300);
  out.замена_вопрос = flat(окно());
  await ответить('Нет');
  out.замена_нет_молчим = (out.sent.length === былоС);
  out.замена_нет_сценарий_цел = q('#src-text').value;
  q('#tx-keep').click();
  await pause(300);
  await ответить('Да');
  out.замена_да_запрос = out.sent.slice(-1)[0] || null;
  out.замена_да_сценарий = q('#src-text').value;

  // Сценария у проекта нет — затирать нечего, вопроса быть не должно.
  SOURCE = '';
  ORIGIN = origin({kind: '', value: '', donor_url: '', scenario_chars: 0});
  await открыть();
  await набрать('Первый сценарий проекта.');
  q('#tx-keep').click();
  await pause(400);
  out.пустой_без_вопроса = !окно();
  out.пустой_запрос = out.sent.slice(-1)[0] || null;

  // ── 13. Поле источника — одна строка, растущая под содержимое ───────────────
  SOURCE = СЦЕНАРИЙ;
  ORIGIN = ЗАПИСАН;
  await открыть();
  const высота = () => Math.round(q('#tx-any').getBoundingClientRect().height);
  out.поле_строк = q('#tx-any').rows;
  out.поле_высота_ссылки = высота();
  await набрать(Array.from({length: 12},
                           (_, i) => 'строка сценария номер ' + i).join('\n'));
  out.поле_высота_текста = высота();
  // Сходить в «с нуля по теме», пережить там перерисовку экрана и вернуться. Так и
  // бывает на самом деле: вкладка перерисовывается после каждого действия, а набранное
  // живёт в SCEN_STATE и возвращается в поле. Рисуется поле при этом уже СКРЫТЫМ, и
  // измерить его в тот момент нельзя — scrollHeight у скрытого ноль. Высоту ему обязан
  // поставить `paintMode`, когда показывает обратно, иначе поле возвращается в одну
  // линию, а лежит в нём двенадцать (поймано мутацией).
  const режим = (m) => [...app.querySelectorAll('.modes button')]
    .find(b => b.dataset.mode === m).click();
  режим('topic');
  await pause(60);
  app.innerHTML = '';
  await renderScenarioTab(D());
  await pause(400);
  out.поле_после_перерисовки_скрыто = !видно('#tx-auto');
  режим('auto');
  await pause(60);
  out.поле_высота_после_возврата = высота();
  out.поле_строк_в_тексте = q('#tx-any').value.split('\n').length;
  // Сценарий в 12 000 знаков не должен выдавливать с экрана всё остальное: у роста
  // есть потолок, а дальше поле листается внутри себя.
  await набрать(Array.from({length: 60},
                           (_, i) => 'строка сценария номер ' + i).join('\n'));
  out.поле_высота_простыни = высота();
  out.поле_листается = (q('#tx-any').scrollHeight > q('#tx-any').clientHeight + 2);

  // ── 14. Запись ссылки не прошла ─────────────────────────────────────────────
  SOURCE = СЦЕНАРИЙ;
  ORIGIN = origin({kind: 'text', scenario_chars: СЦЕНАРИЙ.length});
  await открыть();
  ЗАПИСЬ_ПАДАЕТ = true;
  await набрать(ДРУГАЯ);
  out.сбой_подпись = flat(q('#tx-keep-why'));
  // Любая перерисовка действий (галочка языка) не должна писать поверх отказа.
  const pl = [...app.querySelectorAll('#tx-langs label')].find(l => l.dataset.code === 'pl');
  pl.querySelector('input').checked = !pl.querySelector('input').checked;
  pl.querySelector('input').dispatchEvent(new Event('change'));
  out.сбой_после_галочки = flat(q('#tx-keep-why'));
  out.сбой_кнопка = видно('#tx-keep') ? flat(q('#tx-keep')) : null;
  ЗАПИСЬ_ПАДАЕТ = false;
  q('#tx-keep').click();
  await pause(600);
  out.повтор_ушёл = (out.sent.filter(s => s.path === '/ui/projects/p1/source')
                            .slice(-1)[0] || {}).body || null;
  out.повтор_кнопка_скрыта = !видно('#tx-keep');
  out.повтор_подпись = flat(q('#tx-keep-why'));

  }catch(e){ out.error = String((e && e.stack) || e); }
  clearAllLogTimers();
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    # json.dumps даёт корректный строковый литерал и для JS — с экранированным \n.
    scenario = SCENARIO.replace("__FILE_TEXT__", json.dumps(FILE_TEXT))
    res = browser_harness.run_scenario(scenario, budget_ms=30000)
    if res.get("error"):
        print("НЕ ЗАПУСТИЛОСЬ:", res["error"])
        return 1
    check("сценарий доработал до конца", res.get("error"), None)
    check("сценарий без ошибок в консоли", res.get("errs"), [])

    print("\n1. Источник — один блок в НАЧАЛЕ вкладки, врезки «Исходный текст» внизу нет")
    check("первая карточка вкладки — «Откуда взять сценарий»",
          res.get("первая_карточка"), "Откуда взять сценарий")
    check("отдельного блока «Исходный текст» на вкладке больше нет",
          res.get("врезки_исходный_текст_нет"), True)
    check("поле источника ровно одно", res.get("поле_одно"), 1)
    check("сценарий проекта — свёрнут, а не висит безымянной простынёй",
          res.get("сценарий_свёрнут"), True)
    has("подпись сценария говорит, сколько в нём знаков",
        res.get("подпись_сценария"), "24 знаков")
    check("сам сценарий никуда не делся — правится там же",
          res.get("сценарий_на_месте"), "Старый сценарий проекта.")
    check("режимов два: одно поле на ссылку и текст — и «с нуля по теме» отдельно",
          res.get("режимы"), ["Ссылка или готовый текст", "С нуля по теме"])

    print("\n2. Записанная ссылка донора подставлена в поле и разобрана ДО нажатия")
    check("ссылка проекта в поле", res.get("ссылка_подставлена"),
          "https://youtu.be/Ng0hmsrr7l4")
    has("над полем сказано, что записано у проекта", res.get("что_записано"),
        "Записан донор: https://youtu.be/Ng0hmsrr7l4")
    has("и когда по нему платили", res.get("что_записано"),
        "по этой ссылке заказывали текст 20.09 21:15")
    check("разбор спрошен у сервера сразу на подставленное значение",
          res.get("разбор_запрошен"), ["https://youtu.be/Ng0hmsrr7l4"])
    check("разбор спрошен вместе с проектом — иначе сервер не скажет про повтор",
          (res.get("разбор_с_проектом") or {}).get("project_id"), "p1")
    has("человек видит, ЧТО распознано", res.get("распознано"),
        "Распознано: ссылка на ролик YouTube — текст возьмём из субтитров этого ролика")
    check("предупреждение о повторе показано до нажатия",
          res.get("предупреждение_видно"), True)
    has("и говорит про вторую оплату", res.get("предупреждение"),
        "повтор создаст вторую задачу и второй платный рерайт")
    check("ни одного платного вызова при открытии вкладки",
          res.get("платных_вызовов_пока"), 0)
    check("кнопки «Запомнить ссылку» нет — ссылка запоминается сама",
          res.get("кнопки_у_ссылки_нет"), True)
    has("и сказано, что она уже записана и куда пойдёт", res.get("бесплатная_подпись"),
        "ссылка запомнена у проекта — вкладка «Обложка» подставит её сама")
    check("записанную ссылку при открытии не переписываем",
          res.get("записанную_не_переписали"), True)
    check("заказ без языков не даётся", res.get("заказ_без_языков_выключен"), True)
    has("и сказано почему", res.get("заказ_без_языков_почему"), "хотя бы один язык")

    print("\n3. Новая ссылка запоминается сама — только запись, рерайт не зовётся")
    check("уходит POST /ui/projects/p1/source",
          (res.get("запрос_ссылки") or {}).get("path"), "/ui/projects/p1/source")
    check("телом — только ссылка (сценарий не трогаем)",
          (res.get("запрос_ссылки") or {}).get("body"),
          {"link": "https://youtu.be/bbbbbbbbbbb"})
    check("ни одного заказа текста", res.get("заказов_после_запоминания"), 0)
    has("над полем видно, что записано", res.get("после_записи_записано"),
        "Записан донор: https://youtu.be/bbbbbbbbbbb")
    has("и под полем тоже", res.get("после_записи_подпись"), "ссылка запомнена у проекта")
    check("без тоста — это не событие, а обычное сохранение",
          res.get("тостов_о_записи"), 0)
    check("тот же ролик другой формой адреса запись не переписывает",
          res.get("та_же_ссылка_не_переписана"), True)
    check("после записи разбор спрошен заново", res.get("после_записи_разбор_переспрошен"), 2)
    check("и устаревшее «заказ по новой её заменит» снято",
          res.get("после_записи_предупреждение_снято"), True)

    print("\n4. То же поле принимает текст — и смысл кнопки меняется")
    has("распознан текст", res.get("текст_распознан"), "Распознано: текст")
    check("кнопка стала «Сохранить сценарий»", res.get("текст_кнопка"),
          "Сохранить сценарий")
    has("и обещает бесплатно", res.get("текст_подпись"), "бесплатно, рерайт не зовём")
    has("предупреждение отвечает на «почему сверху ссылка, а снизу текст»",
        res.get("текст_предупреждение"), "не заменит, пока вы не нажмёте «Взять в проект»")
    check("с языком заказ разрешён", res.get("заказ_с_языком_доступен"), True)
    has("и сказано, что произойдёт", res.get("заказ_подпись_текста"),
        "текст уйдёт в рерайт как есть")

    print("\n5. Чего сервер не принимает, того экран не даёт нажать")
    has("причина показана словами сервера", res.get("две_ссылки_видно"),
        "оставьте одну")
    check("платная кнопка выключена", res.get("две_ссылки_заказ_выключен"), True)
    check("и бесплатная тоже — сохранять нечего",
          res.get("две_ссылки_сохранение_выключено"), True)
    check("нажатие ничего не отправило", res.get("две_ссылки_ничего_не_ушло"), True)

    print("\n6. Заказ уходит с режимом и источником ИЗ ОТВЕТА разбора")
    has("голый идентификатор распознан", res.get("ид_распознан"),
        "идентификатор ролика YouTube")
    check("режим выбран сервером, а не человеком",
          (res.get("заказ") or {}).get("mode"), "link")
    check("уехала собранная сервером ссылка, а не одиннадцать знаков из поля",
          (res.get("заказ") or {}).get("source"),
          "https://www.youtube.com/watch?v=Ng0hmsrr7l4")
    check("проект в заказе назван", (res.get("заказ") or {}).get("project_id"), "p1")
    has("тост о задаче", res.get("тост_заказа"), "Задача текста создана")
    has("после заказа экран показывает, что источник запомнен",
        res.get("после_заказа_записано"), "по этой ссылке заказывали текст")

    print("\n7. Разбор не ответил — платную кнопку не даём (угадывать в браузере нельзя)")
    has("сказано, что случилось и что делать", res.get("разбор_упал_видно"),
        "Не удалось разобрать поле")
    check("кнопка заказа выключена", res.get("разбор_упал_заказ_выключен"), True)

    print("\n7а. «С нуля по теме» — отдельный режим: источника нет, нужен бриф")
    check("поле источника убрано — тут вставлять нечего",
          res.get("тема_поле_источника_скрыто"), True)
    check("вместо него поле темы", res.get("тема_поле_темы_видно"), True)
    check("и бриф — без него модель не пишет", res.get("тема_бриф_виден"), True)
    check("бесплатного «сохранить источник» в этом режиме нет",
          res.get("тема_бесплатное_скрыто"), True)
    check("сценарий проекта при этом никуда не делся",
          res.get("тема_сценарий_на_месте"), True)
    check("без брифа заказ не даётся", res.get("тема_без_брифа_выключено"), True)
    has("и сказано почему", res.get("тема_без_брифа_почему"), "выберите бриф")
    check("с брифом и языком — даётся", res.get("тема_заказ_доступен"), True)
    check("заказ уходит режимом topic с темой и брифом",
          res.get("тема_заказ"),
          {"mode": "topic", "source": "Как устроен конвейер", "languages": ["ru"],
           "model": "gpt-4.1", "preset": "Бриф канала", "project_id": "p1"})
    check("возврат в обычный режим возвращает поле источника",
          res.get("назад_поле_видно"), True)
    check("и бесплатное действие", res.get("назад_бесплатное_видно"), True)

    print("\n8. Файл — своя кнопка, дефолтного поля не видно, но с клавиатуры достижимо")
    check("видимая кнопка есть", res.get("файл_кнопка_есть"), "Выбрать файл .txt")
    check("сам input остался внутри метки", res.get("файл_вход_в_метке"), True)
    check("input НЕ display:none — иначе его не поймать табом",
          res.get("файл_вход_не_display_none"), True)
    check("и при этом его не видно", res.get("файл_вход_невидим"), True)
    check("до выбора написано «файл не выбран»", res.get("файл_имя_до"), "файл не выбран")
    check("кнопки «Убрать файл» до выбора нет", res.get("файл_снять_скрыто"), True)
    check("содержимое файла легло в то же поле источника",
          res.get("файл_лёг_в_поле"), True)
    check("имя файла показано", res.get("файл_имя_после"), "файл: сценарий.txt")
    check("выбор можно снять", res.get("файл_снять_видно"), True)
    check("input очищен — тот же файл можно выбрать снова",
          res.get("файл_вход_очищен"), True)
    has("тост говорит, сколько прочитано", res.get("файл_тост"), "Файл прочитан")
    has("прочитанное разобрано как текст", res.get("файл_распознан"), "Распознано: текст")
    check("кнопка предлагает сохранить сценарий", res.get("файл_кнопка_сохранения"),
          "Сохранить сценарий")

    print("\n8а. Сохранение из файла: текст и имя одной ручкой, без платного вызова")
    has("перед заменой чужого сценария спрашивают", res.get("файл_спросили"),
        "Заменить сценарий проекта?")
    check("до ответа на сервер не уходит ничего",
          res.get("файл_до_ответа_молчим"), True)
    check("ручка — POST /ui/projects/p1/source",
          (res.get("файл_запрос") or {}).get("path"), "/ui/projects/p1/source")
    check("серверу уехал ТЕКСТ и ИМЯ файла (сам файл никуда не ходит)",
          (res.get("файл_запрос") or {}).get("body"),
          {"text": FILE_TEXT, "file": "сценарий.txt"})
    check("платного вызова при этом нет", res.get("файл_платного_не_было"), True)
    check("поле источника освободилось — второй копии текста на экране нет",
          res.get("файл_поле_освободилось"), True)
    check("сценарий проекта обновился", res.get("файл_сценарий_обновился"), True)
    has("подпись сценария пересчиталась", res.get("файл_подпись_сценария"),
        f"{len(FILE_TEXT)} знаков")
    has("и сказано, что источник — файл", res.get("файл_что_записано"),
        "Сценарий взят из файла «сценарий.txt»")
    has("тост подтверждает сохранение", res.get("файл_тост_сохранения"),
        "Сценарий сохранён")

    print("\n9. Файл не в UTF-8 — отказ с причиной, в поле ничего не подставлено")
    check("поле осталось пустым", res.get("кракозябры_поле_пустое"), True)
    has("сказано, что не так", res.get("кракозябры_тост"), "не в UTF-8")
    has("и что денег это не стоило", res.get("кракозябры_тост"), "Денег не потрачено")
    check("имя битого файла не осталось пометкой источника",
          res.get("кракозябры_имя"), "файл не выбран")

    print("\n10. Ссылка выпуска канала — отдельной строкой: по ней пойдёт ночной движок")
    check("строка показана", res.get("выпуск_видно"), True)
    has("с самой ссылкой", res.get("выпуск_строка"), "https://youtu.be/zzzzzzzzzzz")
    has("и с тем, кто по ней ходит", res.get("выпуск_строка"), "движок берёт её")
    # Вот ради чего правка: заказ был сделан по ссылке ИЗ ПОЛЯ, и приписывать его
    # выпуску нельзя — человек по такой строке пойдёт искать вторую оплату не там.
    hasnt("чужой заказ выпуску не приписан", res.get("выпуск_строка"), "заказывали")
    has("а сказан там, где ему место — у записанного донора",
        res.get("источник_строка"), "по этой ссылке заказывали текст 20.09 21:15")
    has("рядом с самой ссылкой донора", res.get("источник_строка"),
        "Записан донор: https://youtu.be/Ng0hmsrr7l4")
    hasnt("и ссылка выпуска в эту строку не затесалась",
          res.get("источник_строка"), "zzzzzzzzzzz")

    print("\n11. Записанную ссылку можно забыть — сервер это умел, экран не давал")
    check("кнопка есть, пока ссылка записана", res.get("забыть_видно"), True)
    has("спрашивают окном", res.get("забыть_спросили"), "Забыть записанную ссылку донора?")
    has("и говорят, что другой копии нет", res.get("забыть_спросили"),
        "другой копии этой ссылки у него нет")
    check("до ответа ничего не уходит", res.get("забыть_до_ответа_молчим"), True)
    check("ответ «Нет» ничего не отправляет", res.get("забыть_после_нет_молчим"), True)
    has("и ссылка на месте", res.get("забыть_после_нет_записано"),
        "https://youtu.be/Ng0hmsrr7l4")
    check("ответ «Да» шлёт пустую ссылку — это и значит «забыть»",
          res.get("забыть_запрос"),
          {"path": "/ui/projects/p1/source", "body": {"link": ""}})
    hasnt("записанного донора больше нет", res.get("забыть_записано"),
          "https://youtu.be/Ng0hmsrr7l4")
    check("кнопка ушла — забывать больше нечего", res.get("забыть_кнопка_ушла"), True)
    check("а в поле ссылка осталась: её ещё можно вернуть",
          res.get("забыть_поле_цело"), "https://youtu.be/Ng0hmsrr7l4")
    has("и об этом сказано", res.get("забыть_тост"), "сама больше не запомнится")
    check("перерисовка экрана забытую ссылку не записывает обратно",
          res.get("забытая_не_вернулась"), True)
    has("и под полем сказано, почему она не записывается",
        res.get("забытая_подпись"), "эту ссылку велели забыть")
    check("правка поля — и ссылка снова запоминается сама",
          res.get("после_правки_записана"),
          {"path": "/ui/projects/p1/source", "body": {"link": "https://youtu.be/Ng0hmsrr7l4"}})
    check("ссылку выпуска забыть нельзя — она не наша",
          res.get("забыть_чужую_нельзя"), True)

    print("\n12. Чужой сценарий не затирается молча")
    has("вопрос назван вопросом", res.get("замена_вопрос"), "Заменить сценарий проекта?")
    has("и назван размер того, что потеряется", res.get("замена_вопрос"),
        "уже есть сценарий — 24 знаков")
    has("и то, чем заменят", res.get("замена_вопрос"), "(43 знаков)")
    check("«Нет» — ни одного запроса", res.get("замена_нет_молчим"), True)
    check("и сценарий проекта цел", res.get("замена_нет_сценарий_цел"),
          "Старый сценарий проекта.")
    check("«Да» — уходит сохранение", res.get("замена_да_запрос"),
          {"path": "/ui/projects/p1/source",
           "body": {"text": "Совсем другой сценарий, вставленный руками."}})
    check("и сценарий на экране обновился", res.get("замена_да_сценарий"),
          "Совсем другой сценарий, вставленный руками.")
    check("пустой проект вопросом не мучают", res.get("пустой_без_вопроса"), True)
    check("и текст уходит сразу", res.get("пустой_запрос"),
          {"path": "/ui/projects/p1/source", "body": {"text": "Первый сценарий проекта."}})

    print("\n13. Поле источника — строка, а не простыня (владелец: «чтобы в строку»)")
    check("в разметке одна строка", res.get("поле_строк"), 1)
    check("и со ссылкой поле высотой в строку",
          (res.get("поле_высота_ссылки") or 999) <= 52, True)
    check("вставили сценарий — поле выросло",
          (res.get("поле_высота_текста") or 0) >= (res.get("поле_высота_ссылки") or 0) + 40,
          True)
    check("в режиме «с нуля по теме» поля источника не видно",
          res.get("поле_после_перерисовки_скрыто"), True)
    check("набранное пережило перерисовку экрана", res.get("поле_строк_в_тексте"), 12)
    check("и возврат обратно поле не схлопывает — в нём всё те же двенадцать строк",
          res.get("поле_высота_после_возврата"), res.get("поле_высота_текста"))
    check("целый сценарий поле не растит до бесконечности — у роста есть потолок",
          (res.get("поле_высота_простыни") or 9999) <= 322, True)
    check("и листается внутри себя, а не выдавливает кнопки с экрана",
          res.get("поле_листается"), True)

    print("\n14. Запись ссылки не прошла — это видно, пока не повторят")
    has("отказ на экране", res.get("сбой_подпись"),
        "ссылку запомнить не удалось: кабинет недоступен")
    has("и не затёрт следующей перерисовкой", res.get("сбой_после_галочки"),
        "ссылку запомнить не удалось: кабинет недоступен")
    check("кнопка вернулась как повтор", res.get("сбой_кнопка"), "Запомнить ссылку ещё раз")
    check("повтор шлёт ссылку", res.get("повтор_ушёл"), {"link": "https://youtu.be/bbbbbbbbbbb"})
    check("после удачи кнопка снова скрыта", res.get("повтор_кнопка_скрыта"), True)
    has("и сказано, что ссылка запомнена", res.get("повтор_подпись"),
        "ссылка запомнена у проекта")

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО {len(FAILS)}: " + "; ".join(FAILS))
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
