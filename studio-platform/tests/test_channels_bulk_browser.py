#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Массовый выбор каналов и действия над выбранными — экран (владелец 30.09.2026).

«Можно сделать как в айфоне в галерее — кнопку "Выбрать", и потом уже выбор действия с
этими карточками». До этого на паузу ставили по одному: на ста пятидесяти каналах это
час кликанья.

Что здесь проверяется и почему именно это:

  • КЛИК В РЕЖИМЕ ВЫБОРА НЕ УВОДИТ СО СТРАНИЦЫ. Главная ловушка: привычный клик по
    карточке ведёт в канал, и посреди выбора он стёр бы всё отмеченное. Поэтому в
    режиме выбора тот же клик отмечает.
  • ВЫБОР РАБОТАЕТ ПОВЕРХ ОТБОРА. «Выбрать все отобранные» берёт ровно то, что сейчас
    на экране, — не все каналы вообще. Иначе человек меняет то, чего не видел.
  • ЧАСТИЧНЫЙ ОТКАЗ НЕ ОТМЕНЯЕТ УДАВШЕЕСЯ И НАЗВАН ПОИМЁННО. «Сделано 2, не вышло у
    одного» без имени отправляет человека искать этот один вручную по всему списку;
    отмеченным остаётся именно он — повтор идёт по нему, а не по всей пачке.
  • АРХИВ СПРАШИВАЕТ, И С ЧИСЛОМ. Массовый архив — не то, что ждёшь увидеть случайно.
  • УДАЛЕНИЯ В МАССОВЫХ ДЕЙСТВИЯХ НЕТ. Удаление необратимо и не проходит у канала с
    историей; пачкой это однажды снесёт не то.
  • СТРОКА ДЕЙСТВИЙ ПРИБИТА К НИЗУ. Выбор идёт по всему списку, и кнопка, до которой
    надо доскроллить, превращает выбор в две работы вместо одной.

    .venv/bin/python tests/test_channels_bulk_browser.py
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
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    const ch = (id, name, lang, niche, status) =>
      ({id, name, handle: '@h' + id, uc: 'UC' + id + 'x'.repeat(20), lang, niche, status,
        tags: [], tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2, per_day: 2,
        has_profile: true, has_uc: true,
        supply: {known: true, items: 4, days: 4, covered: 4}});

    const ALL = [
      ch(1, 'Nocne Biografie', 'PL', 'биографии', 'active'),
      ch(2, 'Noční Portréty', 'CS', 'биографии', 'active'),
      ch(3, 'Front Wschodni', 'PL', 'история', 'active'),
      ch(4, 'Cień Trzeciej Rzeszy', 'PL', 'история', 'active'),
      ch(5, 'Amor en Viñetas', 'ES', 'романтика', 'active'),
    ];
    const calls = [];

    const reply = body => {
      calls.push(body);
      if (body.action === 'paused'){
        // Один канал якобы занят другой правкой: частичный отказ. Остальные два
        // изменились по-настоящему — их новое состояние обязано доехать до экрана.
        for (const c of ALL)
          if (body.ids.indexOf(c.id) >= 0 && c.id !== 3) c.status = 'paused';
        return {ok: true, action: 'поставить на паузу', asked: body.ids.length,
                done: ['Nocne Biografie', 'Cień Trzeciej Rzeszy'], same: [],
                failed: [{id: 3, name: 'Front Wschodni',
                          why: 'канал занят другой правкой, повторите'}]};
      }
      if (body.action === 'archived'){
        for (const c of ALL) if (body.ids.indexOf(c.id) >= 0) c.status = 'archived';
        return {ok: true, action: 'убрать в архив', asked: body.ids.length,
                done: ALL.filter(c => body.ids.indexOf(c.id) >= 0).map(c => c.name),
                same: [], failed: []};
      }
      if (body.action === 'tag_add'){
        for (const c of ALL) if (body.ids.indexOf(c.id) >= 0) c.tags = ['срочно'];
        return {ok: true, action: 'проставить тег', asked: body.ids.length,
                done: ALL.filter(c => body.ids.indexOf(c.id) >= 0).map(c => c.name),
                same: [], failed: []};
      }
      return {ok: true, action: 'назначить пресет канала', asked: body.ids.length,
              done: ['Nocne Biografie'], same: [], failed: []};
    };

    window.api = async (p, opt) => {
      if (p === '/ui/me') return ME;
      // Копия массива, но те же объекты: правки, которые «сделал сервер», доезжают до
      // экрана, а обнуление списка на странице не выносит наш собственный.
      if (p === '/ui/channels') return {channels: ALL.slice()};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p === '/ui/channel-presets') return {presets: [{id: 7, name: 'Ночной'}]};
      if (p === '/ui/channels/bulk') return reply(JSON.parse(opt.body));
      return {};
    };
    addEventListener('hashchange', render);
    try { localStorage.removeItem('vs.channels.sieve'); } catch (e) {}

    const names = () => [...document.querySelectorAll('.chcard h3')]
      .map(h => h.textContent.trim());
    const marked = () => [...document.querySelectorAll('.chcard.on h3')]
      .map(h => h.textContent.trim()).sort();
    const card = name => [...document.querySelectorAll('.chcard')]
      .find(el => el.querySelector('h3').textContent.trim() === name);
    const bar = () => document.querySelector('#bulkbar');
    const count = () => (document.querySelector('#b-count') || {}).textContent || '';
    const act = a => document.querySelector(`.b-act[data-act="${a}"]`);
    const locked = () => [...document.querySelectorAll('.b-act')].every(b => b.disabled);
    const dlg = () => [...document.querySelectorAll('dialog.modal')].pop();
    const lastToast = () => {
      const t = [...document.querySelectorAll('#toasts .toast')].pop();
      return t ? t.textContent : '';
    };
    const set = (id, v) => {
      const n = document.querySelector(id);
      n.value = v;
      n.dispatchEvent(new Event('change', {bubbles: true}));
    };

    location.hash = '#/channels';
    await sleep(400);
    out.кнопкаВыбрать = (document.querySelector('#pickmode') || {}).textContent;
    out.полосыСначалаНет = !!(bar() && bar().hidden);
    out.отметокСначалаНет = document.querySelectorAll('.chmark').length;

    document.querySelector('#pickmode').click();
    await sleep(80);
    out.полосаВидна = !!(bar() && bar().offsetHeight > 0);
    out.полосаПрибита = bar() ? getComputedStyle(bar()).position : '';
    out.отметокПоявилось = document.querySelectorAll('.chmark').length;
    out.счётчикПусто = count();
    out.действияЗаперты = locked();

    // Отбор: только польские. Выбор обязан работать ПОВЕРХ него.
    set('#s-lang', 'PL');
    await sleep(80);
    out.отобрано = names().length;
    out.отметокПослеОтбора = document.querySelectorAll('.chmark').length;

    document.querySelector('#b-all').click();
    await sleep(60);
    out.выбраныВсеОтобранные = marked();
    out.счётчикПослеВыбора = count();
    out.действияОткрылись = !locked();

    // Снимаем отбор: отмеченными обязаны остаться те же три, а не все пять. Проверка
    // именно здесь, а не по числу в счётчике: пока отбор стоит, скрытый канал не виден
    // и в счётчик не попадёт даже ошибочно отмеченный.
    set('#s-lang', '');
    await sleep(80);
    out.видноВсехБезОтбора = names().length;
    out.отмеченыБезОтбора = marked();
    set('#s-lang', 'PL');
    await sleep(80);
    out.отмеченыСнова = marked().length;

    // Сужаем отбор так, что ни один отмеченный в него не попадает: выбор обязан
    // опустеть, иначе действие задело бы каналы, которых на экране уже нет, а счётчик
    // обещал бы работу над тем, что человек не видит.
    set('#s-lang', 'CS');
    await sleep(80);
    out.чужойОтборПоказал = names();
    out.счётчикПриЧужомОтборе = count();
    out.действияЗаперлисьСнова = locked();
    set('#s-lang', 'PL');
    await sleep(80);
    document.querySelector('#b-all').click();
    await sleep(60);

    // Клик по карточке в режиме выбора: отмечает и НЕ уводит со страницы.
    card('Front Wschodni').click();
    out.хешСразуПослеКлика = location.hash;
    await sleep(60);
    out.хешПослеКлика = location.hash;
    out.послеКликаОтмечены = marked();
    card('Front Wschodni').click();
    await sleep(40);
    out.второйКликВернул = marked().length;

    document.querySelector('#b-none').click();
    await sleep(40);
    out.послеСнятия = marked().length;
    out.снятиеЗаперлоДействия = locked();

    document.querySelector('#b-all').click();
    await sleep(40);
    // Частичный отказ.
    act('paused').click();
    await sleep(140);
    out.телоЗапроса = calls[calls.length - 1];
    out.тостПаузы = lastToast();
    out.осталисьОтмечены = marked();
    out.режимНеВыключился = !!(bar() && !bar().hidden);
    out.карточкаОбновилась = card('Nocne Biografie').textContent;

    // Архив спрашивает, и с числом.
    document.querySelector('#b-all').click();
    await sleep(40);
    const звонков = calls.length;
    act('archived').click();
    await sleep(80);
    out.архивСпросил = dlg() ? dlg().textContent : '';
    out.доОтветаНеОтправлено = calls.length === звонков;
    dlg().querySelector('.m-no').click();
    await sleep(60);
    out.послеНетНеОтправлено = calls.length === звонков;
    out.послеНетВыборНаМесте = marked().length;

    act('archived').click();
    await sleep(80);
    dlg().querySelector('.m-ok').click();
    await sleep(160);
    out.архивОтправлен = calls[calls.length - 1].action;
    out.архивВзялОтобранных = calls[calls.length - 1].ids.slice().sort();
    out.послеАрхиваВыборПуст = marked().length;

    // Тег: пустое поле не отправляется.
    document.querySelector('#b-all').click();
    await sleep(40);
    const звонков2 = calls.length;
    act('tag_add').click();
    await sleep(80);
    dlg().querySelector('.m-ok').click();
    await sleep(80);
    out.пустойТегНеОтправлен = calls.length === звонков2;
    out.пустойТегСказан = dlg() ? dlg().querySelector('.m-err').textContent : '';
    const поле = dlg().querySelector('#b-tag');
    поле.value = 'срочно';
    dlg().querySelector('.m-ok').click();
    await sleep(160);
    out.тегОтправлен = {action: calls[calls.length - 1].action,
                        tag: calls[calls.length - 1].tag};
    out.тегПопалВОтбор = [...document.querySelectorAll('#s-tag option')]
      .map(o => o.value).indexOf('срочно') >= 0;

    // Удаления в полосе действий нет.
    out.кнопкиПолосы = [...bar().querySelectorAll('button')].map(b => b.textContent.trim());
    out.полосаПроУдаление = bar().querySelector('.bnote').textContent;

    // Выход из режима: отметки уходят, клик снова ведёт в канал.
    document.querySelector('#b-off').click();
    await sleep(60);
    out.полосаУшла = !!bar().hidden;
    out.отметокНеОсталось = document.querySelectorAll('.chmark').length;
    out.кнопкаВернулась = document.querySelector('#pickmode').textContent;
    card('Front Wschodni').click();
    out.хешПослеВыхода = location.hash;
  } catch (e) { out.errs.push(String(e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=14000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])

    print("Режим выбора включается кнопкой, а не живёт всегда")
    check("кнопка называется «Выбрать»", flat(res.get("кнопкаВыбрать")), "Выбрать")
    check("до нажатия строки действий не видно", res.get("полосыСначалаНет"), True)
    check("и отметок на карточках нет", res.get("отметокСначалаНет"), 0)
    check("после нажатия строка действий видна", res.get("полосаВидна"), True)
    check("и она прибита к низу, а не уезжает при прокрутке",
          res.get("полосаПрибита"), "fixed")
    check("отметки появились у всех пяти", res.get("отметокПоявилось"), 5)
    check("пока никого не выбрали — так и сказано",
          flat(res.get("счётчикПусто")), "ничего не выбрано")
    check("и действия нажать нельзя", res.get("действияЗаперты"), True)

    print("Выбор работает поверх отбора")
    check("отбор оставил трёх польских", res.get("отобрано"), 3)
    check("отметки остались только у них", res.get("отметокПослеОтбора"), 3)
    check("«выбрать все отобранные» взяло ровно их", res.get("выбраныВсеОтобранные"),
          ["Cień Trzeciej Rzeszy", "Front Wschodni", "Nocne Biografie"])
    check("счётчик называет число", flat(res.get("счётчикПослеВыбора")),
          "выбрано 3 канала")
    check("действия стали доступны", res.get("действияОткрылись"), True)
    check("после снятия отбора видно всех пятерых",
          res.get("видноВсехБезОтбора"), 5)
    check("а отмечены по-прежнему только отобранные тогда три",
          res.get("отмеченыБезОтбора"),
          ["Cień Trzeciej Rzeszy", "Front Wschodni", "Nocne Biografie"])
    check("возврат отбора выбор не теряет", res.get("отмеченыСнова"), 3)
    check("отбор, в который отмеченные не попали, показал чужого",
          res.get("чужойОтборПоказал"), ["Noční Portréty"])
    check("и выбор при нём пуст, а не тайно жив",
          flat(res.get("счётчикПриЧужомОтборе")), "ничего не выбрано")
    check("и действия снова заперты", res.get("действияЗаперлисьСнова"), True)

    print("Клик по карточке в режиме выбора не уводит со страницы")
    check("хеш не поменялся в тот же миг", res.get("хешСразуПослеКлика"), "#/channels")
    check("и не поменялся после", res.get("хешПослеКлика"), "#/channels")
    check("клик снял отметку именно с этой карточки",
          res.get("послеКликаОтмечены"),
          ["Cień Trzeciej Rzeszy", "Nocne Biografie"])
    check("второй клик вернул её", res.get("второйКликВернул"), 3)
    check("«снять выбор» снимает всех", res.get("послеСнятия"), 0)
    check("и снова запирает действия", res.get("снятиеЗаперлоДействия"), True)

    print("Частичный отказ: удавшееся остаётся, неудавшееся названо поимённо")
    body = res.get("телоЗапроса") or {}
    check("на сервер ушли только отобранные", sorted(body.get("ids") or []), [1, 3, 4])
    check("и названо действие", body.get("action"), "paused")
    тост = flat(res.get("тостПаузы"))
    check("сказано, сколько сделано", "сделано 2" in тост, True)
    check("и у кого не вышло — по имени", "Front Wschodni" in тост, True)
    check("и почему", "занят другой правкой" in тост, True)
    check("сказано, что денег это не тратит", "денег это не тратит" in тост, True)
    check("отмеченным остался только тот, у кого не вышло",
          res.get("осталисьОтмечены"), ["Front Wschodni"])
    check("режим выбора не выключился сам", res.get("режимНеВыключился"), True)
    check("удавшийся канал показан уже на паузе",
          "пауза" in flat(res.get("карточкаОбновилась")), True)

    print("Архив спрашивает, и с числом")
    check("вопрос назвал число каналов",
          "Убрать в архив 3 канала?" in flat(res.get("архивСпросил")), True)
    check("до ответа ничего не отправлено", res.get("доОтветаНеОтправлено"), True)
    check("«Нет» ничего не отправляет", res.get("послеНетНеОтправлено"), True)
    check("и выбор после отказа на месте", res.get("послеНетВыборНаМесте"), 3)
    check("«Да» отправляет архив", res.get("архивОтправлен"), "archived")
    check("ровно по отобранным", res.get("архивВзялОтобранных"), [1, 3, 4])
    check("после удачи выбор снят", res.get("послеАрхиваВыборПуст"), 0)

    print("Тег спрашивают, пустой не отправляют")
    check("пустой тег не ушёл на сервер", res.get("пустойТегНеОтправлен"), True)
    check("и человеку сказано, чего не хватает",
          "впишите тег" in flat(res.get("пустойТегСказан")), True)
    check("вписанный тег ушёл вместе с действием", res.get("тегОтправлен"),
          {"action": "tag_add", "tag": "срочно"})
    check("новый тег сразу виден в отборе", res.get("тегПопалВОтбор"), True)

    print("Удаления в массовых действиях нет")
    кнопки = [flat(b) for b in (res.get("кнопкиПолосы") or [])]
    check("ни одной кнопки про удаление",
          [b for b in кнопки if "удал" in b.lower() or "снести" in b.lower()], [])
    check("набор действий — ровно обещанный", кнопки,
          ["выбрать все отобранные", "снять выбор", "Поставить на паузу",
           "Вернуть в работу", "Убрать в архив", "Проставить тег", "Снять тег",
           "Назначить пресет", "Готово"])
    проудаление = flat(res.get("полосаПроУдаление"))
    check("и сказано, почему удаления здесь нет",
          "Удалить каналы пачкой нельзя" in проудаление, True)
    check("и что делать вместо", "архив" in проудаление, True)

    print("Выход из режима возвращает обычный клик")
    check("строка действий ушла", res.get("полосаУшла"), True)
    check("отметки ушли", res.get("отметокНеОсталось"), 0)
    check("кнопка снова зовёт выбирать", flat(res.get("кнопкаВернулась")), "Выбрать")
    check("клик по карточке снова ведёт в канал",
          res.get("хешПослеВыхода"), "#/channel/3")

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
