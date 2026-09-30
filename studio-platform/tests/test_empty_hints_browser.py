#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пустое место говорит, чего не хватает (владелец 30.09.2026: «да, делаем»).

Пустой список молчит одинаково в двух совершенно разных случаях: когда заводить ещё
нечего и когда спросить не удалось. Человек, который видит экран впервые, эти случаи
не различает и идёт спрашивать.

Что здесь проверяется и почему именно это:

  • ПУСТО ОТВЕЧАЕТ НА ТРИ ВОПРОСА. Почему пусто, что это значит и что нажать. Третье —
    ссылкой ровно туда, где это чинится: у канала без расписания в его настройки, у
    пустой очереди — на каналы, где ставятся ссылки.
  • «НЕ ЗНАЕМ» ≠ «ПУСТО». Очередь, часть которой не ответила, пустой очередью называть
    нельзя, и предлагать в ней «поставить ролики» — врать экраном: работа там, может,
    и идёт. Такое место помечено машинно (`data-empty="unknown"`), а не только словами.
  • ОТБОР ≠ ОТСУТСТВИЕ. Под узким отбором пусто — это не «выпусков нет»; чинится это
    снятием отбора, а не заведением первого выпуска.
  • ГДЕ ДАННЫЕ ЕСТЬ — ПОДСКАЗКИ НЕТ. Иначе она превращается в шум, который перестают
    читать ровно к тому дню, когда она нужна.

    .venv/bin/python tests/test_empty_hints_browser.py
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


def check_in(label, needle, haystack):
    if needle.lower() in (haystack or "").lower():
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали:  {needle!r}\n        в тексте: {haystack!r}")
        FAILS.append(label)


def check_not_in(label, needle, haystack):
    if needle.lower() not in (haystack or "").lower():
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        не должно было быть: {needle!r}"
              f"\n        в тексте: {haystack!r}")
        FAILS.append(label)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection', e => out.errs.push(
    'promise: ' + (e.reason && e.reason.message || e.reason)));
  try {
    ME = {name: 'boss', role: 'user'};
    const sleep = ms => new Promise(r => setTimeout(r, ms));

    // Всё пустое место на экране, как его видит человек: вид, слова и куда ведёт.
    const boxes = () => [...document.querySelectorAll('#app .empty[data-empty]')].map(el => {
      const a = el.querySelector('a');
      return {вид: el.getAttribute('data-empty'),
              текст: el.textContent.replace(/\s+/g, ' ').trim(),
              ссылка: a ? a.getAttribute('href') : ''};
    });

    // ───────── канал: с расписанием и без него, всё прочее одинаково ─────────
    const CH_FULL = {id: 1, name: 'Канал', handle: '@ch', lang: 'PL', niche: 'история',
      uc: 'UC1', status: 'active', tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2,
      deferred: false, deferred_limit: 5, schedule: {mon: ['10:00']}, per_week: 1,
      host: 'mac', profile: 'p1', presets: {}, note: '', profile_id: 1, host_id: 1,
      preset_vsgen_id: 1, preset_rewrite: '', preset_cover: ''};
    let CH = CH_FULL, CAL = [];
    const chBody = () => ({
      channel: CH, calendar: CAL, counts: {}, uploads: [], by_day: {},
      tz: 'Europe/Warsaw', channel_preset: {}, waiting_without_slot: 0,
      waiting_items: [], history_days: 14,
      stats: {}, analytics: {access: {connected: false, client: true}},
      video_stats: {}});

    // ───────── очередь и выпуски ─────────
    let SERVICES = [{key: 'rewrite', title: 'Текст', ok: true, error: '', count: 0},
                    {key: 'vsgen', title: 'Видео', ok: true, error: '', count: 0}];
    let ROWS = [];
    const ROW = {service: 'vsgen', id: 'j-1', title: 'Ролик', title_why: '',
      kind: 'render', kind_title: 'сборка', status: 'running', state: 'running',
      stage: 'Сборка', error: '', warning: '', reason_pending: false, progress: null,
      provider: '', user: 'boss', channel: 'Канал', channel_id: 1, lang: 'PL',
      project_id: 'p_1', release_id: '', created: 1790000000, started: 1790000060,
      finished: null, waiting: 60, working: 600, age_text: 'идёт 10 мин'};
    let RELEASES = [];

    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p.startsWith('/ui/queue-pause')) return {paused: false};
      if (p.startsWith('/ui/queue')) return {rows: ROWS, services: SERVICES,
        channels: [{id: 1, name: 'Канал'}],
        states: [{key: 'running', title: 'в работе'}],
        periods: [{key: '1h', title: 'за час'}],
        total: ROWS.length, shown: ROWS.length, details: {}};
      if (p.startsWith('/ui/releases')) return {releases: RELEASES,
        phases: [{key: 'render', title: 'сборка'}]};
      if (p === '/ui/channels') return {channels: [CH]};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p.startsWith('/ui/channels/1')) return chBody();
      return {};
    };
    window.post = async () => ({});

    // 1. Канал без расписания: нечего трогать, кроме расписания.
    CH = Object.assign({}, CH_FULL, {schedule: {}});
    CAL = [];
    await viewChannel('1');
    await sleep(30);
    out.каналБезРасписания = boxes();

    // 2. Тот же канал с расписанием — подсказке тут делать нечего.
    CH = CH_FULL;
    CAL = [{at: 1790000000, state: 'planned', past: false, what: 'ролик',
            release_id: 'r1', url: ''}];
    await viewChannel('1');
    await sleep(30);
    out.каналСРасписанием = boxes();

    // 3. Очередь: одно хранилище не ответило. Это НЕ пустая очередь.
    SERVICES = [{key: 'rewrite', title: 'Текст', ok: false,
                 error: 'нет связи с хранилищем текстов', count: 0},
                {key: 'vsgen', title: 'Видео', ok: true, error: '', count: 0}];
    ROWS = [];
    await viewQueue();
    await sleep(30);
    out.очередьНеЗнаем = boxes();

    // 4. Очередь: все ответили, но выбран отбор, под который ничего не подошло.
    SERVICES = [{key: 'rewrite', title: 'Текст', ok: true, error: '', count: 0},
                {key: 'vsgen', title: 'Видео', ok: true, error: '', count: 0}];
    QFILTER.state = 'running';
    QFILTER.channel = '1';
    await viewQueue();
    await sleep(30);
    out.очередьПодОтбором = boxes();
    // Снятие отборов — это действие, а не украшение: адрес у очереди один и тот же,
    // и без обработчика ссылка не делала бы ничего.
    const clr = document.querySelector('#q-clear');
    out.ссылкаСнятьЕсть = !!clr;
    if (clr){
      clr.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
      out.отборыПослеКлика = Object.values(QFILTER).filter(Boolean).length;
    }
    for (const k of Object.keys(QFILTER)) QFILTER[k] = '';

    // 5. Очередь честно пуста.
    await viewQueue();
    await sleep(30);
    out.очередьПуста = boxes();

    // 6. В очереди есть задачи — подсказки быть не должно.
    ROWS = [ROW];
    await viewQueue();
    await sleep(30);
    out.очередьСЗадачами = boxes();
    ROWS = [];

    // 7. Пресеты: пусто с объяснением и ссылкой на завод первого.
    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/presets') return {broken: [], groups:
        [{service: 'vsgen', title: 'Производство', items: []}]};
      if (p === '/ui/channel-presets') return {presets: [], note: ''};
      if (p === '/ui/lumean/templates') return {ok: true, templates: [], note: ''};
      return {};
    };
    try { localStorage.removeItem('presetFilter'); } catch (e) {}
    presetFilter.q = ''; presetFilter.types = [];
    await viewPresets();
    await sleep(60);
    out.пресетыПусто = boxes();

    // 8. Ключи: про FastGen мы просто ничего не спрашивали — это «не знаем».
    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/keys') return {busy: [], providers: [], resting: [], fastgen: null,
        own_keys: [], unavailable: [], prompt: {selected: '', channels: {}}};
      return {};
    };
    await viewKeys();
    await sleep(60);
    out.ключиFastGen = boxes().filter(b => /fastgen/i.test(b.текст));

    // 9. Каналов нет: ссылка раскрывает ту же форму, что кнопка в шапке.
    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: []};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p === '/ui/channels/new') return {languages: [{code: 'PL', title: 'польский'}]};
      return {};
    };
    await viewChannels();
    await sleep(40);
    out.каналовНет = boxes();
    const first = document.querySelector('#ch-first');
    if (first){
      first.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
      await sleep(40);
      // Форма завода канала раскрылась на месте, а не «кнопка выше» словами.
      out.формаКаналаОткрылась = !!document.querySelector('#newch-form');
    }

    // 10. Проектов нет: то же правило, и кнопка тут же заводит проект.
    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/projects') return {projects: []};
      return {};
    };
    let завели = 0;
    window.post = async p => { if (p === '/ui/projects') завели++; return {id: 'p_1'}; };
    await viewProjects();
    await sleep(40);
    out.проектовНет = boxes();
    const pfirst = document.querySelector('#pr-first');
    if (pfirst){
      pfirst.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
      await sleep(60);
      out.проектЗаведён = завели;
    }
    window.post = async () => ({});

    // 11. Выпуски: под отбором и без него.
    window.api = async p => {
      if (p === '/ui/me') return ME;
      if (p.startsWith('/ui/releases')) return {releases: RELEASES,
        phases: [{key: 'render', title: 'сборка'}]};
      if (p === '/ui/channels') return {channels: [CH_FULL]};
      return {};
    };
    FILTERS.phase = 'render';
    await viewReleases();
    await sleep(30);
    out.выпускиПодОтбором = boxes();
    for (const k of Object.keys(FILTERS)) FILTERS[k] = '';
    await viewReleases();
    await sleep(30);
    out.выпускиПусто = boxes();
  } catch (e) { out.errs.push('scenario: ' + (e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def one(res, key):
    """Ровно одно пустое место на экране — иначе проверять нечего и не о чем."""
    got = res.get(key) or []
    if len(got) == 1:
        return got[0]
    print(f"  FAIL {key}: ожидалось одно пустое место, получено {len(got)}: {got!r}")
    FAILS.append(key)
    return {"вид": "", "текст": "", "ссылка": ""}


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("НЕ ПРОГНАТЬ:", res["error"])
        return 2
    check("ошибок в консоли нет", res.get("errs"), [])

    print("\nКанал без расписания: чего не хватает и куда идти")
    b = one(res, "каналБезРасписания")
    check("это пусто, а не «не знаем»", b["вид"], "none")
    check_in("названо, чего нет", "расписание не задано", b["текст"])
    check_in("сказано, чем это мешает", "даты", b["текст"])
    check("ссылка ведёт в настройки этого канала", b["ссылка"], "#/channel/1/nastroyki")

    print("\nУ канала с расписанием подсказки нет")
    check("пустых мест на экране", len(res.get("каналСРасписанием") or []), 0)

    print("\nОчередь, часть которой не ответила: это не пустая очередь")
    b = one(res, "очередьНеЗнаем")
    check("помечено как «не знаем»", b["вид"], "unknown")
    check_in("сказано вслух, что не знаем", "не знаем", b["текст"])
    check_not_in("не названо пустой очередью", "очередь пуста", b["текст"])
    check_not_in("не названо «задач нет»", "задач нет.", b["текст"])
    check_not_in("не предлагает заводить работу", "поставить ролики", b["текст"])
    check("ссылки «куда идти» здесь нет", b["ссылка"], "")

    print("\nОчередь под отбором: пусто не значит «задач нет»")
    b = one(res, "очередьПодОтбором")
    check("это пусто, а не «не знаем»", b["вид"], "none")
    check_in("названа причина — отбор", "отбор", b["текст"])
    check_not_in("не предлагает заводить работу", "поставить ролики", b["текст"])
    check("ссылка снимает отборы", b["ссылка"], "#/queue")
    check("ссылка «Снять отборы» есть", res.get("ссылкаСнятьЕсть"), True)
    check("после клика отборов не осталось", res.get("отборыПослеКлика"), 0)

    print("\nОчередь честно пуста: сказано и почему, и что нажать")
    b = one(res, "очередьПуста")
    check("это пусто, а не «не знаем»", b["вид"], "none")
    check_in("названо словами", "очередь пуста", b["текст"])
    check_in("сказано, что это нормально", "так и должно быть", b["текст"])
    check("ссылка ведёт туда, где ставятся ссылки", b["ссылка"], "#/channels")

    print("\nГде задачи есть — подсказки нет")
    check("пустых мест на экране", len(res.get("очередьСЗадачами") or []), 0)

    print("\nВыпуски: отбор и настоящая пустота — разные слова и разные ссылки")
    b = one(res, "выпускиПодОтбором")
    check_in("названа причина — отбор", "отбор", b["текст"])
    check("ссылка снимает отборы", b["ссылка"], "#/releases")
    b = one(res, "выпускиПусто")
    check_in("названо, что ссылок не поставлено", "не поставлено", b["текст"])
    check("ссылка ведёт к заводу выпуска", b["ссылка"], "#/new")

    print("\nПресеты: чем пусто мешает и куда идти")
    boxes = res.get("пресетыПусто") or []
    whole = [b for b in boxes if "целых пресетов нет" in b["текст"].lower()]
    check("карточка целых пресетов сказала, что пуста", len(whole), 1)
    if whole:
        check_in("сказано, чем пресет вообще нужен", "выпуску нечем", whole[0]["текст"])
        check("ссылка ведёт к заводу пресета", whole[0]["ссылка"], "#/preset/new")
    chp = [b for b in boxes if "пресетов каналов нет" in b["текст"].lower()]
    check("карточка пресетов каналов сказала, что пуста", len(chp), 1)
    if chp:
        check_in("сказано, что такой пресет задаёт", "по умолчанию", chp[0]["текст"])
    check("ни одно пустое место на пресетах не названо отказом",
          [b["вид"] for b in boxes if b["вид"] != "none"], [])

    print("\nКлючи: про FastGen мы не спрашивали — это не «лимит свободен»")
    b = one(res, "ключиFastGen")
    check("помечено как «не знаем»", b["вид"], "unknown")
    check_in("сказано, чего не знаем", "неизвестны", b["текст"])
    check_not_in("не выдано за «лимит свободен»", "лимит свободен.", b["текст"])
    check_in("оговорено вслух, что это не «свободно»", "это не «лимит свободен»",
             b["текст"])

    print("\nПроектов нет: сказано, откуда проекты берутся")
    b = one(res, "проектовНет")
    check("это пусто, а не «не знаем»", b["вид"], "none")
    check_in("сказано, что обычно он заводится сам", "заводится сам", b["текст"])
    check_not_in("не отправляет искать кнопку глазами", "кнопка выше", b["текст"])
    check("по ссылке проект правда заведён", res.get("проектЗаведён"), 1)

    print("\nКаналов нет: ссылка, а не «кнопка выше»")
    b = one(res, "каналовНет")
    check("это пусто, а не «не знаем»", b["вид"], "none")
    check_in("сказано, из чего состоит канал", "расписание выходов", b["текст"])
    check_not_in("не отправляет искать кнопку глазами", "кнопка выше", b["текст"])
    check("ссылка на месте", b["ссылка"], "#/channels")
    check("по ссылке раскрылась форма завода канала",
          res.get("формаКаналаОткрылась"), True)

    print()
    if FAILS:
        print(f"провалов: {len(FAILS)} — {', '.join(FAILS[:6])}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
