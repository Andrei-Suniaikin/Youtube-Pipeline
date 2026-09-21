#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Карточки «Текст» и «Обложка» в пакетном запуске на экране «Проекты».

Дефект, ради которого это писалось (решение владельца 20.09.2026): в карточке
«Запуск для отмеченных» были только задачи кабинета — озвучка, картинки, видео,
рендер. Текст и обложку приходилось заказывать по одному проекту, хотя источник
у них уже записан В ПРОЕКТЕ (ссылка донора выпуска, а без донора — сценарий).

Всё, что здесь проверяется, — про деньги и про то, что человек видит ДО оплаты:

  * обе карточки есть, и обычный случай — отметить проекты и нажать «Сделать»,
    ничего не заполняя: пустое поле значит «как в проекте», и это написано словами;
  * первое нажатие уходит СУХИМ ПРОГОНОМ (dry_run), и до подтверждения в нашем
    <dialog> ни одного настоящего заказа не уходит. Без этого пачка платных вызовов
    ставилась бы вслепую: сколько из отмеченных реально уедет, знает только сервер;
  * ограничение сервера «вставленный текст (или файл) уходит одному проекту»
    сказано человеку ДО нажатия и удерживает запрос, а не прилетает отказом после;
  * файл .txt («в тексте ещё файл» — дословная просьба владельца) читает БРАУЗЕР:
    в теле запроса поля `source_file` нет и не будет, сервер принимает только
    `source_text`. Проверяется, что содержимое выбранного файла доезжает до запроса
    ровно таким, каким лежало в файле; что при нескольких отмеченных проектах
    предупреждение появляется от самого выбора файла, а не после платного нажатия;
    и что файл не в UTF-8 не уходит вовсе (кракозябры вернулись бы мусором за деньги);
  * замена источника уезжает именно в те поля, которые ждёт сервер, а пара
    «провайдер+модель» обложки разбирается обратно из одного списка;
  * ответ сервера показан по строкам проектов: что поставлено, что пропущено,
    почему и кому уехало НЕ своё (`replaced`).

Сети и стенда не нужно: `api()` и `post()` подменены, браузер настоящий.

    .venv/bin/python tests/test_batch_cards_browser.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []

# Текст, который кладётся в файл .txt и обязан доехать до сервера ровно таким —
# вместе с переводом строки: абзацы в сценарии значимы.
FILE_TEXT = "Текст из файла: первый абзац.\nВторой абзац — с переводом строки."


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


SCENARIO = r"""
(async () => {
  const out = {errs: [], sent: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));

  const ready = {text: false, audio: false, images: 0, clips: 0, render: false};
  const PROJECTS = {projects: [
    {id: 'p1', name: 'Первый', ready: ready, busy: '', release_id: '',
     phase: '', phase_title: '', updated: 1758000000, plan_item_id: 0},
    {id: 'p2', name: 'Второй', ready: ready, busy: '', release_id: '',
     phase: '', phase_title: '', updated: 1758000000, plan_item_id: 0}]};
  const FIELDS = {kinds: {tts: {title: 'Озвучка', fields: []}},
    pult: {text:  {title: 'Текст',   post: '/ui/projects/batch/text'},
           cover: {title: 'Обложка', post: '/ui/projects/batch/cover'}}};

  window.api = async (p) => {
    if (p === '/ui/projects') return PROJECTS;
    if (p === '/ui/projects/batch/fields') return FIELDS;
    if (p === '/ui/text') return {jobs: [], modes: [], presets: {generation: []},
      models: [{value: 'gpt-4.1', label: 'GPT-4.1'}],
      languages: [{code: 'ru', title: 'Русский'}, {code: 'pl', title: 'Польский'}]};
    if (p === '/ui/covers') return {jobs: [], modes: [], rules: [],
      languages: ['Русский', 'Польский'], presets: ['Канальный'],
      options: [{label: 'Nano · OpenAI', provider: 'openai', model: 'gpt-image-nano'}]};
    return {};
  };

  const row = (o) => Object.assign({id: '', name: '', action: 'skip', why: '', source: '',
    source_note: '', replaced: false, preset: '', languages: [], skipped_languages: [],
    job_id: '', chars: 0, repeat_checked: true}, o);
  const TEXT_DRY = {ok: true, dry_run: true, service: 'rewrite', planned: 1, ordered: 0,
    skipped: 0, failed: 0, replaced: 0, projects: [row({id: 'p1', name: 'Первый',
      action: 'order', source: 'donor', languages: ['pl'],
      source_note: 'ссылка донора https://youtu.be/x — расшифровка 18 500 знаков'})]};
  const TEXT_REAL = {ok: true, dry_run: false, service: 'rewrite', planned: 0, ordered: 1,
    skipped: 0, failed: 0, replaced: 0, projects: [row({id: 'p1', name: 'Первый',
      action: 'ordered', source: 'donor', languages: ['pl'], job_id: 'rw-7'})]};
  const COVER_DRY = {ok: true, dry_run: true, service: 'cover', planned: 1, ordered: 0,
    skipped: 1, failed: 0, replaced: 1, projects: [
      row({id: 'p1', name: 'Первый', action: 'order', source: 'replacement-url',
           replaced: true, languages: ['Польский'], preset: 'Канальный'}),
      row({id: 'p2', name: 'Второй', action: 'skip',
           why: 'у проекта нет выпуска — превью донора взять неоткуда'})]};
  const COVER_REAL = {ok: true, dry_run: false, service: 'cover', planned: 0, ordered: 1,
    skipped: 1, failed: 0, replaced: 1, projects: [
      row({id: 'p1', name: 'Первый', action: 'ordered', source: 'replacement-url',
           replaced: true, languages: ['Польский'], job_id: 'cv-3'}),
      row({id: 'p2', name: 'Второй', action: 'skip',
           why: 'у проекта нет выпуска — превью донора взять неоткуда'})]};

  window.post = async (p, b) => {
    out.sent.push({path: p, body: b});
    if (p === '/ui/projects/batch/text') return b.dry_run ? TEXT_DRY : TEXT_REAL;
    if (p === '/ui/projects/batch/cover') return b.dry_run ? COVER_DRY : COVER_REAL;
    return {ok: true};
  };
  const flat = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const toasts = () => [...document.querySelectorAll('#toasts .toast')].map(t => t.textContent);
  const pause = (ms) => new Promise(r => setTimeout(r, ms));

  await viewProjects();
  await pause(300);                       // варианты полей подтягиваются в фоне

  const tc = document.querySelector('[data-pult="text"]');
  const cc = document.querySelector('[data-pult="cover"]');
  out.карточка_текста = !!tc;
  out.карточка_обложки = !!cc;
  out.соседи_на_месте = !!document.querySelector('[data-kind="tts"]');
  out.кнопка_выключена_без_галочек = tc.querySelector('button.pgo').disabled;
  // Без отметок панель — одна строка: раскрытая, она занимала 900 px над списком.
  const тело = document.querySelector('#batchbody');
  out.панель_свёрнута_без_галочек = !!тело && тело.getClientRects().length === 0;
  out.подсказка_без_галочек = flat(document.querySelector('#batchn'));
  out.отмены_у_текста_нет = !tc.querySelector('button.bcancel');
  out.про_источник = flat(tc.querySelector('.note'));
  out.языки_текста = [...tc.querySelectorAll('[data-pf="languages"] label')]
    .map(l => l.dataset.code);
  out.подпись_языков = flat(tc.querySelectorAll('.note')[1]);
  out.модели_текста = [...tc.querySelectorAll('[data-pf="model"] option')].map(o => o.value);
  out.брифа_у_текста_нет = !tc.querySelector('[data-pf="preset"]');
  out.источники_текста = [...tc.querySelectorAll('[data-pf="source_from"] option')]
    .map(o => o.value);
  out.пресеты_обложки = [...cc.querySelectorAll('[data-pf="preset"] option')].map(o => o.value);
  out.языки_обложки = [...cc.querySelectorAll('[data-pf="languages"] label')]
    .map(l => l.dataset.code);
  out.замена_свёрнута = [...tc.querySelectorAll('details')].map(d => d.open);

  // ── обычный случай: отметили проект, выбрали язык, нажали ──
  const picks = [...document.querySelectorAll('input.pick')];
  picks[0].checked = true; picks[0].dispatchEvent(new Event('change'));
  out.кнопка_включилась = !tc.querySelector('button.pgo').disabled;
  out.панель_раскрылась = тело.getClientRects().length > 0;
  out.счётчик_после_галочки = flat(document.querySelector('#batchn'));
  out.подпись_про_один_проект = flat(tc.querySelector('.pult-one'));

  const pl = [...tc.querySelectorAll('[data-pf="languages"] label')]
    .find(l => l.dataset.code === 'pl');
  pl.querySelector('input').checked = true;
  pl.querySelector('input').dispatchEvent(new Event('change'));

  tc.querySelector('button.pgo').click();
  await pause(300);
  out.первый_запрос = out.sent[0] || null;
  out.заказов_до_подтверждения = out.sent.filter(s => s.body.dry_run === false).length;
  const dlg = document.querySelector('dialog[open]');
  out.окно_открылось = !!dlg;
  out.в_окне = flat(dlg);
  out.кнопка_окна = dlg ? dlg.querySelector('.m-ok').textContent.trim() : '';
  if (dlg) dlg.querySelector('.m-ok').click();
  await pause(400);
  out.второй_запрос = out.sent[1] || null;
  out.окно_закрылось = !document.querySelector('dialog[open]');
  out.отчёт = flat(document.getElementById('pultres'));
  out.тост_текста = toasts().slice(-1)[0] || '';

  // ── вставленный текст на пачку: отказ ДО запроса ──
  picks[1].checked = true; picks[1].dispatchEvent(new Event('change'));
  const area = tc.querySelector('[data-pf="source_text"]');
  area.value = 'вставленный сценарий';
  area.dispatchEvent(new Event('input'));
  out.предупреждение_красное = tc.querySelector('.pult-one').classList.contains('bad');
  out.предупреждение = flat(tc.querySelector('.pult-one'));
  const было = out.sent.length;
  tc.querySelector('button.pgo').click();
  await pause(300);
  out.запрос_не_ушёл = (out.sent.length === было);
  out.окна_нет = !document.querySelector('dialog[open]');
  out.тост_отказа = toasts().slice(-1)[0] || '';
  area.value = '';
  area.dispatchEvent(new Event('input'));

  // ── обложка: референс, пресет и пара «провайдер+модель» ──
  cc.querySelector('[data-pf="preset"]').value = 'Канальный';
  cc.querySelector('[data-pf="option"]').value = 'openai|gpt-image-nano';
  cc.querySelector('[data-pf="reference_url"]').value = 'https://example.org/ref.jpg';
  cc.querySelector('button.pgo').click();
  await pause(300);
  out.запрос_обложки = out.sent.slice(-1)[0] || null;
  const dlg2 = document.querySelector('dialog[open]');
  out.в_окне_обложки = flat(dlg2);
  if (dlg2) dlg2.querySelector('.m-ok').click();
  await pause(400);
  out.заказ_обложки = out.sent.slice(-1)[0] || null;
  out.отчёт_обложки = flat(document.getElementById('pultres'));
  out.тост_обложки = toasts().slice(-1)[0] || '';

  // ── файл .txt: читает его БРАУЗЕР, а серверу уезжает текст ──────────────────
  // Владелец просил дословно «в тексте ещё файл», и это не украшение: поле
  // `source_file` в теле запроса не существует — сервер принимает только
  // `source_text`. Если чтение сломается, человек нажмёт «Сделать» с пустым полем и
  // оплатит рерайт не того, что выбрал.
  const tf = tc.querySelector('[data-pf="source_file"]');
  out.поле_файла_есть = !!tf;
  out.файл_принимает_txt = tf ? tf.getAttribute('accept') : '';
  const ИЗ_ФАЙЛА = 'Текст из файла: первый абзац.\nВторой абзац — с переводом строки.';
  out.обработчик_файла_есть = !!(tf && typeof tf.onchange === 'function');
  // Файл кладётся через DataTransfer — это настоящий FileList, и событие настоящее:
  // обработчик страницы вызывается обычным dispatchEvent. Обёртка вокруг onchange
  // ничего не подменяет, она нужна ровно затем, чтобы ДОЖДАТЬСЯ его работы: чтение
  // файла асинхронно (`await file.arrayBuffer()`), а время в стенде виртуальное —
  // пауза в миллисекундах проскакивает мимо чтения, и замер попадает на ещё пустое
  // поле. Проверено: с паузой вместо ожидания сценарий читал поле до чтения файла.
  const pickFile = async (name, data) => {
    const dt = new DataTransfer();
    dt.items.add(new File([data], name, {type: 'text/plain'}));
    tf.files = dt.files;
    const orig = tf.onchange;
    let работа;
    tf.onchange = e => (работа = orig.call(tf, e));
    tf.dispatchEvent(new Event('change'));
    tf.onchange = orig;
    await работа;
  };

  // Сначала ДВА отмеченных: предупреждение обязано появиться от самого выбора файла,
  // до нажатия платной кнопки.
  picks[0].checked = true; picks[0].dispatchEvent(new Event('change'));
  picks[1].checked = true; picks[1].dispatchEvent(new Event('change'));
  await pickFile('сценарий.txt', ИЗ_ФАЙЛА);
  out.файл_лёг_в_поле = (area.value === ИЗ_ФАЙЛА);
  out.файл_тост_чтения = toasts().slice(-1)[0] || '';
  out.файл_поле_очищено = (tf.value === '');
  out.файл_предупреждение_красное = tc.querySelector('.pult-one').classList.contains('bad');
  out.файл_предупреждение = flat(tc.querySelector('.pult-one'));
  const былоФ = out.sent.length;
  tc.querySelector('button.pgo').click();
  await pause(300);
  out.файл_запрос_не_ушёл = (out.sent.length === былоФ);
  out.файл_окна_нет = !document.querySelector('dialog[open]');
  out.файл_тост_отказа = toasts().slice(-1)[0] || '';

  // Сняли лишнюю галочку — тот же файл теперь уходит законно, одному проекту.
  picks[1].checked = false; picks[1].dispatchEvent(new Event('change'));
  out.файл_предупреждение_погасло = !tc.querySelector('.pult-one').classList.contains('bad');
  tc.querySelector('button.pgo').click();
  await pause(300);
  out.файл_сухой = out.sent.slice(-1)[0] || null;
  const dlg3 = document.querySelector('dialog[open]');
  if (dlg3) dlg3.querySelector('.m-ok').click();
  await pause(400);
  out.файл_заказ = out.sent.slice(-1)[0] || null;

  // Не UTF-8. Такой файл нельзя молча «раскодировать как получится»: кракозябры
  // уехали бы в рерайт и вернулись мусором за деньги. Поле не трогаем.
  const былоВПоле = area.value;
  await pickFile('битый.txt', new Uint8Array([0xff, 0xfe, 0x41, 0x00, 0x42, 0x00]));
  out.кракозябры_поле_не_тронуто = (area.value === былоВПоле);
  out.кракозябры_тост = toasts().slice(-1)[0] || '';
  out.кракозябры_запрос_не_ушёл = (out.sent.slice(-1)[0] === out.файл_заказ);

  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=16000)
    if res.get("error"):
        print("  ПРОПУЩЕНО:", res["error"])
        return 2          # пропуск не успех: без браузера проверка не выполнялась

    print("Карточки на месте, соседние не сломаны")
    check("карточка «Текст»", res.get("карточка_текста"), True)
    check("карточка «Обложка»", res.get("карточка_обложки"), True)
    check("кабинетные карточки остались", res.get("соседи_на_месте"), True)
    check("без галочек «Сделать» выключено", res.get("кнопка_выключена_без_галочек"), True)
    check("без галочек панель свёрнута в строку", res.get("панель_свёрнута_без_галочек"), True)
    check("и подсказывает, что делать", res.get("подсказка_без_галочек"),
          "отметьте проекты в списке")
    check("«Отменить» у соседних сервисов нет", res.get("отмены_у_текста_нет"), True)
    has("сказано, откуда берётся источник", res.get("про_источник"),
        "ссылка донора выпуска, а если донора нет — сценарий проекта")

    print("\nПоля — те же варианты, что у одиночных экранов")
    check("языки текста из /ui/text", res.get("языки_текста"), ["ru", "pl"])
    check("под списком языков сказано, что значит «ничего не отмечено»",
          res.get("подпись_языков"), "ничего не отмечено — уедет язык канала проекта")
    check("модели текста: первым пунктом «по умолчанию»",
          res.get("модели_текста"), ["", "gpt-4.1"])
    check("брифа в пакетном тексте нет (сервер его не принимает)",
          res.get("брифа_у_текста_нет"), True)
    check("выбор источника — три варианта, без «с нуля по теме»",
          res.get("источники_текста"), ["auto", "donor", "text"])
    check("пресеты обложки из /ui/covers", res.get("пресеты_обложки"), ["", "Канальный"])
    check("языки обложки — русскими названиями, как ждёт Cover",
          res.get("языки_обложки"), ["Русский", "Польский"])
    check("замена источника и доп. настройки свёрнуты",
          res.get("замена_свёрнута"), [False, False])

    print("\nСначала сухой прогон, деньги — только после подтверждения")
    check("галочка включила кнопку", res.get("кнопка_включилась"), True)
    check("и раскрыла панель", res.get("панель_раскрылась"), True)
    check("счётчик называет число", res.get("счётчик_после_галочки"), "отмечено: 1")
    first = res.get("первый_запрос") or {}
    check("первый запрос — в пультовую ручку текста",
          first.get("path"), "/ui/projects/batch/text")
    check("и он сухой", (first.get("body") or {}).get("dry_run"), True)
    check("язык из карточки уехал", (first.get("body") or {}).get("languages"), ["pl"])
    check("отмеченный проект уехал", (first.get("body") or {}).get("pids"), ["p1"])
    check("до подтверждения ни одного настоящего заказа",
          res.get("заказов_до_подтверждения"), 0)
    check("подтверждение спрошено нашим окном", res.get("окно_открылось"), True)
    has("в окне сказано, скольким проектам уйдёт", res.get("в_окне"),
        "Уйдёт в работу: 1 из 1")
    has("и сказано, что это ещё не оплата", res.get("в_окне"),
        "ничего не заказано и не оплачено")
    has("видно, что именно уедет проекту", res.get("в_окне"), "ссылка донора")
    check("кнопка окна называет число", res.get("кнопка_окна"), "Заказать: 1")

    second = res.get("второй_запрос") or {}
    check("после подтверждения — тот же адрес",
          second.get("path"), "/ui/projects/batch/text")
    check("и уже не сухой", (second.get("body") or {}).get("dry_run"), False)
    check("окно закрылось", res.get("окно_закрылось"), True)
    has("отчёт показывает, что поставлено", res.get("отчёт"), "поставлено: 1")
    has("и называет проект", res.get("отчёт"), "Первый")
    has("в тосте числа, а не «ок»", res.get("тост_текста"), "поставлено: 1")

    print("\nОграничение «одному проекту» сказано до нажатия")
    has("подпись знает, сколько отмечено", res.get("подпись_про_один_проект"),
        "Сейчас отмечено: 1")
    check("при двух отмеченных подпись краснеет", res.get("предупреждение_красное"), True)
    has("и говорит, что делать", res.get("предупреждение"), "снимите лишние галочки")
    check("запрос не ушёл вовсе", res.get("запрос_не_ушёл"), True)
    check("и окно не открывалось", res.get("окна_нет"), True)
    has("в отказе сказано, что деньги целы", res.get("тост_отказа"),
        "Денег не потрачено")

    print("\nОбложка: референс, пресет и пара «провайдер+модель»")
    ask = res.get("запрос_обложки") or {}
    body = ask.get("body") or {}
    check("адрес обложки", ask.get("path"), "/ui/projects/batch/cover")
    check("сухой прогон первым", body.get("dry_run"), True)
    check("референс уехал полем reference_url",
          body.get("reference_url"), "https://example.org/ref.jpg")
    check("пресет уехал", body.get("preset"), "Канальный")
    check("пара разобрана обратно в провайдера и модель",
          (body.get("provider"), body.get("model")), ("openai", "gpt-image-nano"))
    check("оба отмеченных проекта", body.get("pids"), ["p1", "p2"])
    check("«дорисовать превью» по умолчанию выключено", body.get("edit_preview"), False)
    check("«по одному за раз» по умолчанию включено", body.get("one_at_a_time"), True)
    has("в окне видно, что уедет не свой источник", res.get("в_окне_обложки"),
        "не свой источник")
    has("и видно причину пропуска", res.get("в_окне_обложки"),
        "превью донора взять неоткуда")
    check("после подтверждения заказ не сухой",
          ((res.get("заказ_обложки") or {}).get("body") or {}).get("dry_run"), False)
    has("отчёт по обложке: поставлено и пропущено", res.get("отчёт_обложки"),
        "поставлено: 1 · пропущено: 1")

    print("\nФайл .txt: читает браузер, серверу уезжает текст")
    check("поле для файла есть", res.get("поле_файла_есть"), True)
    check("берёт только .txt", res.get("файл_принимает_txt"), ".txt,text/plain")
    check("чтение файла вообще подключено", res.get("обработчик_файла_есть"), True)
    check("содержимое файла легло в поле сценария", res.get("файл_лёг_в_поле"), True)
    has("сказано, сколько прочитано, и что дальше решает человек",
        res.get("файл_тост_чтения"), "Файл прочитан:")
    has("и прочитанное предложено проверить", res.get("файл_тост_чтения"),
        "проверьте и нажмите «Сделать»")
    # Поле input[type=file] очищается нарочно: иначе повторный выбор ТОГО ЖЕ файла не
    # даёт события change, и человек думает, что файл взят, а поле пустое.
    check("поле файла очищено — тот же файл можно выбрать снова",
          res.get("файл_поле_очищено"), True)

    print("\n…и при двух отмеченных человек предупреждён ДО нажатия")
    check("подпись покраснела от самого выбора файла",
          res.get("файл_предупреждение_красное"), True)
    has("и говорит, что делать", res.get("файл_предупреждение"),
        "снимите лишние галочки")
    check("платный запрос не ушёл", res.get("файл_запрос_не_ушёл"), True)
    check("и окно подтверждения не открывалось", res.get("файл_окна_нет"), True)
    has("в отказе сказано, что деньги целы", res.get("файл_тост_отказа"),
        "Денег не потрачено")

    print("\nСняли лишнюю галочку — тот же файл уезжает текстом")
    check("предупреждение погасло", res.get("файл_предупреждение_погасло"), True)
    dry_file = res.get("файл_сухой") or {}
    check("сначала сухой прогон", (dry_file.get("body") or {}).get("dry_run"), True)
    check("в сухом прогоне уехал ТЕКСТ файла, а не файл",
          (dry_file.get("body") or {}).get("source_text"), FILE_TEXT)
    check("и одному проекту", (dry_file.get("body") or {}).get("pids"), ["p1"])
    ord_file = res.get("файл_заказ") or {}
    check("после подтверждения заказ не сухой",
          (ord_file.get("body") or {}).get("dry_run"), False)
    check("и текст файла уехал в настоящем заказе",
          (ord_file.get("body") or {}).get("source_text"), FILE_TEXT)

    print("\nФайл не в UTF-8 никуда не уходит")
    check("поле сценария не тронуто", res.get("кракозябры_поле_не_тронуто"), True)
    has("сказано, что делать с файлом", res.get("кракозябры_тост"),
        "пересохраните его в этой кодировке")
    has("и что деньги целы", res.get("кракозябры_тост"), "Денег не потрачено")
    check("запроса не было", res.get("кракозябры_запрос_не_ушёл"), True)

    print("\nОшибок в консоли нет")
    check("консоль чистая", res.get("errs"), [])

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
