#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экран «Разложить ссылки» — в настоящем index.html (владелец 30.09.2026).

«Он создаёт кучу карточек со вставленными ссылками и названиями видео в скобочках в
заголовке карточки; и тогда просто справа в дропдауне по названию канал выбираешь,
опять же с фильтром по названию».

Что здесь проверяется и почему именно это:

  • НАЗВАНИЕ В СКОБКАХ — так владелец и описал заголовок карточки; по нему человек
    решает, тот ли это ролик.
  • «НАЗВАНИЯ НЕ ДОБЫЛИ» ≠ ПУСТЫЕ СКОБКИ. Пустота читается как «у ролика нет названия»,
    а это другое утверждение.
  • ПОДСКАЗКА НИЧЕГО НЕ СТАВИТ САМА. Список справа после разбора пуст у ВСЕХ строк,
    включая ту, у которой подсказка есть: ролик, ушедший на чужой канал, необратим.
    В выбор подсказка попадает только нажатием.
  • ДУБЛЬ ПОМЕЧЕН И НЕ ВЫБРАН, но остаётся доступным руками: один донор иногда нарочно
    идёт на два языка.
  • СТРОКА БЕЗ КАНАЛА НЕ УХОДИТ НА СЕРВЕР ВОВСЕ — «Поставить» отправляет ровно то, что
    выбрано.
  • ЧАСТИЧНЫЙ ОТКАЗ ВИДЕН ПОСТРОЧНО, и удавшееся не откатывается на экране.
  • ПОИСК В СПИСКЕ КАНАЛОВ РАБОТАЕТ. Список из полутора сотен строк без поиска
    бесполезен, и делать его здесь своими руками нельзя: он общий для всей студии
    (`wireSearchableSelects`).

    .venv/bin/python tests/test_links_spread_browser.py
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


def flat(text):
    out = str(text or "")
    for sp in (" ", " ", " ", "\n", "\t"):
        out = out.replace(sp, " ")
    return " ".join(out.split())


SCENARIO = r"""
(async () => {
  const out = {errs: [], sent: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};

    const CH = [
      {id: 1, name: 'Nocne Biografie', lang: 'pl', niche: 'биографии',
       status: 'active', tags: []},
      {id: 2, name: 'Noční Portréty', lang: 'cs', niche: 'портреты',
       status: 'active', tags: []},
      {id: 3, name: 'Front Wschodni', lang: 'pl', niche: 'история',
       status: 'active', tags: []},
      {id: 4, name: 'Amor en Viñetas', lang: 'es', niche: 'романтика',
       status: 'paused', tags: []},
      {id: 5, name: 'Dark Documentary', lang: 'en', niche: 'история',
       status: 'active', tags: []},
      {id: 6, name: 'Historias de Guerra', lang: 'es', niche: 'история',
       status: 'active', tags: []},
      {id: 7, name: 'Wojenne Opowieści', lang: 'pl', niche: 'война',
       status: 'active', tags: []},
    ];
    const U1 = 'https://www.youtube.com/watch?v=aaaaaaaaaaa';   // название есть, дубль
    const U2 = 'https://www.youtube.com/watch?v=bbbbbbbbbbb';   // подсказка на чешский
    const U3 = 'https://www.youtube.com/watch?v=ccccccccccc';   // названия нет
    const PREVIEW = {
      asked: 3, limit: 200,
      note: 'Канал не подставляется сам.',
      no_channel: 'Строка без выбранного канала никуда не ставится и ждёт.',
      channels: CH,
      rows: [
        {url: U1, video_id: 'aaaaaaaaaaa', title: 'Nocna opowieść', title_note: '',
         duplicate: {channel_id: 1, channel: 'Nocne Biografie', status: 'planned',
                     slot_at: 0},
         hint: {channel_id: 3, lang: 'pl', why: 'язык «pl» и ниша «история»'}},
        {url: U2, video_id: 'bbbbbbbbbbb', title: 'Noční příběh', title_note: '',
         duplicate: null,
         hint: {channel_id: 2, lang: 'cs', why: 'единственный канал на языке «cs»'}},
        {url: U3, video_id: 'ccccccccccc', title: '',
         title_note: 'название не добыли', duplicate: null,
         hint: {channel_id: 0, lang: '', why: 'язык ролика по названию не виден'}},
      ],
    };
    const APPLY = {ok: true, asked: 2, counts: {placed: 1, queued: 0, skipped: 0,
                                                failed: 1},
      rows: [
        {url: U2, channel_id: 2, channel: 'Noční Portréty', state: 'placed',
         slot: '2026-10-01T10:00:00+00:00', why: ''},
        {url: U3, channel_id: 7, channel: 'Wojenne Opowieści', state: 'failed',
         slot: '', why: 'канала №7 нет — возможно, его уже убрали'},
      ]};
    const CHECKED = {donors: [
        {url: U1, verdict: 'ok', has_subs: true, chars: 30000, minutes: 25,
         length_from: 'субтитры', lang_ru: 'польский', why: ''},
        {url: U2, verdict: 'unknown', has_subs: false, chars: 0, minutes: 0,
         reason: 'exit_dead', why: 'проверить не удалось: выход не отвечает'},
        {url: U3, verdict: 'reject', has_subs: false, chars: 0, minutes: 0,
         why: 'у ролика нет субтитров'},
      ], donor_summary: {ok: 1, warn: 0, reject: 1, unknown: 1},
      wall_sec: 30, timed_out: false, duplicates: 0,
      donor_note: 'Проверка бесплатна: квоту YouTube Data API она не тратит.'};

    window.api = async p => { if (p === '/ui/me') return ME; return {}; };
    window.post = async (p, payload) => {
      out.sent.push({path: p, payload: JSON.parse(JSON.stringify(payload || {}))});
      if (p === '/ui/spread/preview') return PREVIEW;
      if (p === '/ui/spread/check') return CHECKED;
      if (p === '/ui/spread/apply') return APPLY;
      throw new Error('неизвестный запрос ' + p);
    };
    addEventListener('hashchange', render);

    location.hash = '#/spread';
    await sleep(300);
    out.экранЕсть = !!document.querySelector('#sp-in');

    document.querySelector('#sp-in').value = 'вот список:\n' + U1 + '\n' + U2 + ' , '
      + U3;
    document.querySelector('#sp-read').click();
    await sleep(400);

    const cards = () => [...document.querySelectorAll('.sp-row')];
    out.карточек = cards().length;
    out.заголовки = cards().map(c => c.querySelector('div').textContent.trim());
    out.выборСначала = cards().map(c => c.querySelector('.sp-ch').value);
    out.дубльСказан = (cards()[0].querySelector('.sp-dup') || {}).textContent || '';
    out.дубльНеУДругих = !cards()[1].querySelector('.sp-dup');
    // У дубля своя кнопка «Взять подсказку» есть: руками поставить его можно.
    out.дубльВзятьРуками = !!cards()[0].querySelector('.sp-take');
    out.подсказкаСтрока2 = (cards()[1].querySelector('.sp-hint') || {}).textContent || '';
    out.подсказкиНет3 = (cards()[2].querySelector('.sp-hint') || {}).textContent || '';
    out.кнопкаПоставитьЗакрыта = document.querySelector('#sp-place').disabled;
    out.счётчикСначала = (document.querySelector('#sp-count') || {}).textContent || '';

    // Поиск по длинному списку каналов — общий для всей студии, здесь он обязан быть.
    const box = cards()[1].querySelector('.sfind');
    out.поискЕсть = !!box;
    if (box){
      box.querySelector('button').click();
      await sleep(60);
      const pop = document.getElementById('sfind-pop');
      const find = pop.querySelector('input');
      find.value = 'wschod';
      find.dispatchEvent(new Event('input', {bubbles: true}));
      await sleep(60);
      out.найдено = [...pop.querySelectorAll('li')].map(li => li.textContent.trim());
      pop.querySelector('li').click();
      await sleep(60);
      out.послеПоиска = cards()[1].querySelector('.sp-ch').value;
      out.подписьКнопки = box.querySelector('button').textContent.trim();
      // Возвращаем выбор обратно к пустому: дальше проверяем подсказку.
      const sel = cards()[1].querySelector('.sp-ch');
      sel.value = '';
      sel.dispatchEvent(new Event('change', {bubbles: true}));
      await sleep(40);
    }

    // Подсказка попадает в выбор только нажатием.
    cards()[1].querySelector('.sp-take').click();
    await sleep(60);
    out.послеВзятия = cards().map(c => c.querySelector('.sp-ch').value);
    out.счётчикПослеВзятия = (document.querySelector('#sp-count') || {}).textContent;
    out.кнопкаОткрылась = !document.querySelector('#sp-place').disabled;

    // Пачкой подставляются только строки без пометки дубля.
    document.querySelector('#sp-hints').click();
    await sleep(60);
    out.послеПачки = cards().map(c => c.querySelector('.sp-ch').value);

    // Проверка доноров — отдельной кнопкой и по требованию.
    document.querySelector('#sp-check').click();
    await sleep(300);
    out.проверкаЗапрошена = out.sent.filter(s => s.path === '/ui/spread/check').length;
    out.проверкаСсылки = (out.sent.find(s => s.path === '/ui/spread/check')
                          || {payload: {}}).payload.urls;
    out.строкаДонора1 = flatText(cards()[0].querySelector('.sp-donor'));
    out.строкаДонора2 = flatText(cards()[1].querySelector('.sp-donor'));

    // Руками ставим третью строку на канал, которого «уже нет» — частичный отказ.
    const sel3 = cards()[2].querySelector('.sp-ch');
    sel3.value = '7';
    sel3.dispatchEvent(new Event('change', {bubbles: true}));
    await sleep(40);

    document.querySelector('#sp-place').click();
    await sleep(300);
    const put = out.sent.find(s => s.path === '/ui/spread/apply') || {payload: {}};
    out.ушлоНаСервер = (put.payload.rows || []).map(r => [r.url, r.channel_id]);
    out.итог1 = flatText(cards()[0].querySelector('.sp-res'));
    out.итог2 = flatText(cards()[1].querySelector('.sp-res'));
    out.итог3 = flatText(cards()[2].querySelector('.sp-res'));
    out.заперт2 = cards()[1].querySelector('.sp-ch').disabled;
    out.заперт3 = cards()[2].querySelector('.sp-ch').disabled;
    out.сводка = flatText(document.querySelector('#sp-res'));

    function flatText(node){ return node ? node.textContent.replace(/\s+/g, ' ').trim()
                                         : ''; }
  } catch (e) { out.errs.push(String(e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])

    print("Экран и карточки")
    check("поле для вставки есть", res.get("экранЕсть"), True)
    check("карточка на каждую ссылку", res.get("карточек"), 3)

    print("Заголовок: ссылка и название ролика в скобках")
    heads = [flat(h) for h in (res.get("заголовки") or ["", "", ""])]
    check("ссылка в заголовке", "watch?v=aaaaaaaaaaa" in heads[0], True)
    check("название в скобках", "(Nocna opowieść)" in heads[0], True)
    check("названия нет — сказано словами",
          "(название не добыли)" in heads[2], True)
    check("а не пустые скобки", "()" in heads[2], False)

    print("Подсказка ничего не ставит сама")
    check("после разбора канал не выбран нигде", res.get("выборСначала"), ["", "", ""])
    check("«Поставить» закрыта, пока ничего не выбрано",
          res.get("кнопкаПоставитьЗакрыта"), True)
    check("и это сказано словами",
          "не выбран ни у одной" in flat(res.get("счётчикСначала")), True)
    check("подсказка названа подсказкой и названа поимённо",
          "Подсказка:" in flat(res.get("подсказкаСтрока2"))
          and "Noční Portréty" in flat(res.get("подсказкаСтрока2")), True)
    check("нет уверенности — сказано «подсказки нет» с причиной",
          flat(res.get("подсказкиНет3")).startswith("подсказки нет:"), True)
    check("и канал наугад в такой строке не подставлен",
          (res.get("выборСначала") or [])[2], "")

    print("Дубль помечен, но выбор за человеком")
    check("дубль назван и назван канал",
          "уже стоит у канала «Nocne Biografie»" in flat(res.get("дубльСказан")), True)
    check("и сказано, что сам он не встанет",
          "Сам собой он никуда не встанет" in flat(res.get("дубльСказан")), True)
    check("у не-дубля такой пометки нет", res.get("дубльНеУДругих"), True)
    check("но руками его поставить можно — кнопка подсказки на месте",
          res.get("дубльВзятьРуками"), True)

    print("Поиск по списку каналов — общий для студии")
    check("список получил поиск", res.get("поискЕсть"), True)
    check("поиск сузил список до одного", res.get("найдено"), ["Front Wschodni · PL"])
    check("выбранное из поиска попало в строку", res.get("послеПоиска"), "3")
    check("и подпись кнопки это показывает",
          "Front Wschodni" in flat(res.get("подписьКнопки")), True)

    print("Взять подсказку — это нажатие человека")
    check("после нажатия канал стоит только у своей строки",
          res.get("послеВзятия"), ["", "2", ""])
    check("счётчик пересчитан",
          "канал выбран у 1" in flat(res.get("счётчикПослеВзятия")), True)
    check("«Поставить» открылась", res.get("кнопкаОткрылась"), True)
    # У первой строки подсказка ЕСТЬ (канал 3), но она помечена дублем — пачкой её
    # подставлять нельзя: это обошло бы предупреждение одним нажатием.
    check("подстановка пачкой обошла дубль с подсказкой",
          res.get("послеПачки"), ["", "2", ""])

    print("Проверка доноров — отдельной кнопкой")
    check("запрошена один раз", res.get("проверкаЗапрошена"), 1)
    check("и спрошены все три ссылки", len(res.get("проверкаСсылки") or []), 3)
    check("вердикт в строке", "годится" in flat(res.get("строкаДонора1")), True)
    check("«не смогли проверить» не выдаётся за «донор плохой»",
          "не смогли проверить" in flat(res.get("строкаДонора2")), True)

    print("Поставить: уходит ровно выбранное")
    check("строка без канала на сервер не ушла",
          res.get("ушлоНаСервер"), [["https://www.youtube.com/watch?v=bbbbbbbbbbb", 2],
                                    ["https://www.youtube.com/watch?v=ccccccccccc", 7]])
    check("дубль без выбора не отправлен",
          any(r[0].endswith("aaaaaaaaaaa") for r in res.get("ушлоНаСервер") or []),
          False)

    print("Частичный отказ виден построчно")
    check("что встало — сказано с датой",
          "встало на дату" in flat(res.get("итог2")), True)
    check("и с именем канала", "Noční Portréty" in flat(res.get("итог2")), True)
    check("что не вышло — сказано с причиной",
          "не вышло" in flat(res.get("итог3"))
          and "уже убрали" in flat(res.get("итог3")), True)
    check("у неотправленной строки итога нет", res.get("итог1"), "")
    check("поставленное больше не выбирается", res.get("заперт2"), True)
    check("а отказавшее — выбирается снова", res.get("заперт3"), False)
    check("сводка называет все четыре исхода",
          all(w in flat(res.get("сводка"))
              for w in ("Встало на дату:", "без даты:", "пропущено:", "не вышло:")),
          True)
    check("и говорит, что остальное не откатилось",
          "не отменил остальные" in flat(res.get("сводка")), True)

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
