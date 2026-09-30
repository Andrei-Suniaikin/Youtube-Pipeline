#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сорок одинаковых причин — одна строка с числом, а непохожие не склеиваются.

Владелец 30.09.2026 сказал «да» на предложение сгруппировать. Повод: когда падает
общий ключ или ложится провайдер, экран очереди показывает сорок строк с одной и той
же причиной. Человек их не читает — он их прокручивает, и вместе с ними прокручивает
ту единственную, которая про другое.

Опасность правки — ровно обратная: склеить непохожее. Две беды под одной подписью
хуже сорока строк, потому что вторая беда исчезает с экрана совсем. Поэтому тест
проверяет обе стороны:

  * одинаковое по СМЫСЛУ (номера задач, попытки, время, маски ключей внутри текста
    разные) сходится в одну группу с числом;
  * одинаковое начало при разном конце — две группы;
  * код ответа провайдера остаётся различием: `HTTP 402` (кончились деньги) и
    `HTTP 429` (лимит) — разные беды с разным действием;
  * разное состояние и разный сервис не сходятся, даже если фраза буква в букву та же:
    группа рисует ОДНУ плашку состояния и ОДНО действие на всех;
  * раскрытие показывает настоящие строки, а не пересказ;
  * «повторить всю группу» спрашивает подтверждение С ЧИСЛОМ — повтор оплачиваемого
    вызова тратит деньги ещё раз, и сорок повторов тратят сорок раз; «Нет» не
    отправляет ничего.

Проверяются НАСТОЯЩИЕ `viewQueue()`, `viewReleases()` и `reasonKey()` из
`web/index.html` в headless Chrome; `api()`, `post()` и `render()` подменены, сети и
стенда не нужно.

    .venv/bin/python tests/test_error_groups_browser.py
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
  const out = {errs: [], sent: [], браузерные_окна: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  window.confirm = (t) => { out.браузерные_окна.push('confirm: ' + t); return true; };
  window.alert   = (t) => { out.браузерные_окна.push('alert: ' + t); };

  // ── смысл причины отдельно от разметки ──
  const KEY = s => reasonKey(s);
  out.ключи = {
    // Одна беда, записанная по-разному: номер задачи, попытка, время, маска ключа.
    ключ_A: KEY('Не хватило ключа FastGen (задача 8412, попытка 3, 12:04:07)'),
    ключ_B: KEY('не хватило ключа FastGen (задача 77, попытка 11, 09:15:42)'),
    ключ_C: KEY('Не хватило ключа FastGen (задача 8412, попытка 3, 12:04:07).'),
    // Тот же ключ, но другой конец — другая беда.
    хвост:  KEY('Не хватило ключа FastGen, ключ отдыхает до утра'),
    // Слово, которое НАЧИНАЕТСЯ как код ответа, но кодом не является. Разбор
    // 30.09.2026: такое слово ловила ветка «слово с цифрой», а разбор начинался с
    // проверки на «code», и поиск трёх цифр возвращал пустоту — экран очереди падал
    // целиком из-за одной причины странного вида.
    странное: KEY('Сбой code42 у поставщика'),
    странное2: KEY('status7 не распознан'),
    // Код ответа — сама причина, его обезличивать нельзя.
    код402: KEY('Провайдер отказал: HTTP 402'),
    код429: KEY('Провайдер отказал: HTTP 429'),
    // Путь, ссылка и файл — обстоятельства.
    путь_A: KEY('нет файла /srv/projects/p_a66a80ca50/scene_07.png'),
    путь_B: KEY('нет файла /srv/projects/p_ff0012ab77/scene_19.png'),
    ссылка_A: KEY('донор недоступен: https://youtu.be/abcd1234'),
    ссылка_B: KEY('донор недоступен: https://youtu.be/zzzz9999'),
    // Пустое остаётся пустым: молчание — не причина.
    пусто: KEY('   '),
  };

  // ── очередь ──
  const err = (i) => ({
    service: 'vsgen', id: 'j' + i, title: 'Озвучка ' + i, kind: 'tts',
    kind_title: 'Озвучка', state: 'error', status: 'error', created: 1758300000 + i,
    error: `Не хватило ключа FastGen (задача ${8000 + i}, попытка ${1 + i % 4}, ` +
           `1${i % 9}:0${i % 6}:07)`});
  const QROWS = [];
  for (let i = 0; i < 40; i++) QROWS.push(err(i));
  QROWS.push({service: 'vsgen', id: 'j-tail', title: 'Озвучка хвост', kind: 'tts',
    kind_title: 'Озвучка', state: 'error', status: 'error', created: 1758300100,
    error: 'Не хватило ключа FastGen, ключ отдыхает до утра'});
  QROWS.push({service: 'vsgen', id: 'j-402', title: 'Кадры', kind: 'images',
    kind_title: 'Картинки', state: 'error', status: 'error', created: 1758300101,
    error: 'Провайдер отказал: HTTP 402'});
  QROWS.push({service: 'vsgen', id: 'j-429', title: 'Кадры', kind: 'images',
    kind_title: 'Картинки', state: 'error', status: 'error', created: 1758300102,
    error: 'Провайдер отказал: HTTP 429'});
  // Та же фраза, но задача ещё ждёт: состояние другое — значит и группа другая.
  for (let i = 0; i < 2; i++) QROWS.push({service: 'vsgen', id: 'j-wait' + i,
    title: 'Ждёт', kind: 'tts', kind_title: 'Озвучка', state: 'queued',
    status: 'queued', created: 1758300110 + i,
    error: 'Не хватило ключа FastGen (задача 9000, попытка 1, 10:00:00)'});
  // Та же фраза у соседа по сервису «Текст» — другой сервис, другая группа.
  for (let i = 0; i < 2; i++) QROWS.push({service: 'rewrite', id: 'r' + i,
    title: 'Рерайт', kind: 'text', kind_title: 'Текст', state: 'error',
    status: 'error', created: 1758300120 + i,
    error: 'Не хватило ключа FastGen (задача 400, попытка 2, 11:02:03)'});
  // Выпуски: повторять у них нечего, но сворачивать — надо.
  for (let i = 0; i < 3; i++) QROWS.push({service: 'release', id: 'rel-' + i,
    title: 'Выпуск ' + i, kind: 'release', kind_title: 'выпуск · озвучка',
    state: 'queued', status: 'blocked', created: 1758300130 + i,
    release_id: 'rel-' + i, plan_item_id: 500 + i,
    error: 'ждём: общий ключ ElevenLabs исчерпан (проверка 17:0' + i + ':00)'});
  // Строка без причины — сама по себе.
  QROWS.push({service: 'vsgen', id: 'j-run', title: 'Рендер', kind: 'render',
    kind_title: 'Сборка', state: 'running', status: 'running', created: 1758300140,
    progress: 0.3});

  const QUEUE = {services: [{key: 'vsgen', title: 'Видео', ok: true, count: 40, error: ''}],
                 channels: [], states: [], periods: [], shown: QROWS.length,
                 total: QROWS.length, rows: QROWS};

  window.api = async (p) => {
    if (p.startsWith('/ui/queue-pause')) return {paused: false};
    if (p.startsWith('/ui/queue')) return QUEUE;
    if (p.startsWith('/ui/releases')) return RELEASES;
    if (p.startsWith('/ui/channels')) return {channels: []};
    return {};
  };
  window.post = async (p) => { out.sent.push(p); return {ok: true}; };
  window.render = async () => { out.перерисовок = (out.перерисовок || 0) + 1; };

  await viewQueue();

  const heads = [...document.querySelectorAll('tr.grp')];
  out.групп = heads.length;
  out.сводки = heads.map(tr => tr.querySelector('.grpsum').textContent
    .replace(/\s+/g, ' ').trim());
  out.плашки_групп = heads.map(tr => [...tr.querySelectorAll('.pill')]
    .map(p => p.textContent.trim()));
  // Клетки заголовка: сервис, всё остальное одной клеткой, действия. Времени создания
  // у группы нет вовсе — у сорока строк оно разное, и одно число на всех было бы выдумкой.
  out.клеток_в_заголовке = heads.map(tr => tr.children.length);
  out.время_в_заголовке = heads.some(tr => tr.querySelector('td.when'));

  const visible = () => [...document.querySelectorAll('table tbody tr')]
    .filter(tr => !tr.hidden).length;
  out.строк_всего = document.querySelectorAll('table tbody tr').length;
  out.видно_свёрнутым = visible();

  // ── раскрытие показывает сами строки ──
  // Дальше всё под оговоркой «если группа вообще есть»: пропавшая группировка — это
  // провал проверок выше, а не повод уронить сценарий и остаться без отчёта. Без
  // оговорки тест на такую поломку говорил бы «не отработал» вместо внятного FAIL.
  const fold = heads.length ? heads[0].querySelector('button.fold') : null;
  if (fold) {
    out.подсказка_свёрнутого = fold.getAttribute('title');
    fold.click();
    await new Promise(r => setTimeout(r, 120));
    out.видно_после_раскрытия = visible();
    out.раскрытые_имена = [...document.querySelectorAll('tr.grp-item')]
      .filter(tr => !tr.hidden).slice(0, 2)
      .map(tr => tr.querySelector('td:nth-child(2) div').textContent.trim());
    out.подсказка_раскрытого = fold.getAttribute('title');
    out.признак_раскрытия = fold.getAttribute('aria-expanded');
    fold.click();
    await new Promise(r => setTimeout(r, 120));
    out.видно_после_сворачивания = visible();
  }

  // ── действие на группу ──
  out.повтор_у_групп = heads.map(tr => !!tr.querySelector('button.retry-all'));
  out.подсказка_повтора = heads.map(tr => (tr.querySelector('button.retry-all') || {})
    .title || '');

  const retry = heads.length ? heads[0].querySelector('button.retry-all') : null;
  if (retry) retry.click();
  await new Promise(r => setTimeout(r, 200));
  let dlg = document.querySelector('dialog[open]');
  out.вопрос_открылся = !!dlg;
  out.заголовок_вопроса = dlg ? dlg.querySelector('.sec').textContent
    .replace('×', '').replace(/\s+/g, ' ').trim() : '';
  out.текст_вопроса = dlg ? dlg.querySelector('.mbody').textContent
    .replace(/\s+/g, ' ').trim() : '';
  out.фокус = document.activeElement ? document.activeElement.textContent.trim() : '';
  if (dlg) {
    [...dlg.querySelectorAll('button')].find(b => b.textContent.trim() === 'Нет').click();
    await new Promise(r => setTimeout(r, 150));
    out.после_нет = out.sent.slice();

    retry.click();                                    // dlg есть — значит есть и кнопка
    await new Promise(r => setTimeout(r, 200));
    dlg = document.querySelector('dialog[open]');
    if (dlg)
      [...dlg.querySelectorAll('button')].find(b => b.textContent.trim() === 'Да').click();
    await new Promise(r => setTimeout(r, 1500));
    out.отправлено_всего = out.sent.length;
    out.все_повторы = out.sent.every(p => /^\/ui\/actions\/vsgen\/j\d+\/retry$/.test(p));
    out.отправлено_уникальных = new Set(out.sent).size;
  }

  // ── список выпусков ──
  const rel = (i, status, reason) => ({
    release_id: 'r' + i, donor_title: 'Донор ' + i, donor_url: '', channel: 'Канал',
    lang: 'RU', slot_at: 1758400000 + i * 3600, status: status,
    phase_title: 'озвучка', phase_index: 1, phase_total: 6, bar: [],
    idle_sec: 600 + i, attempts: 2, blocked_reason: reason});
  const RELEASES = {phases: [], releases: [
    rel(1, 'blocked', 'Кончился общий ключ ElevenLabs (заказ 88421, 12:03:41)'),
    rel(2, 'blocked', 'кончился общий ключ ElevenLabs (заказ 91, 09:15:02)'),
    rel(3, 'blocked', 'Кончился общий ключ ElevenLabs (заказ 7, 23:59:59)'),
    rel(4, 'blocked', 'Кончился общий ключ ElevenLabs, замена уже поставлена'),
    // Та же фраза, но выпуск не остановлен, а ждёт: другая судьба — другая группа.
    rel(5, 'pending', 'Кончился общий ключ ElevenLabs (заказ 12, 01:02:03)'),
    rel(6, 'pending', 'Кончился общий ключ ElevenLabs (заказ 13, 01:02:04)'),
    rel(7, 'running', '')]};

  await viewReleases();
  const rheads = [...document.querySelectorAll('tr.grp')];
  out.групп_выпусков = rheads.length;
  out.сводки_выпусков = rheads.map(tr => (tr.querySelector('.grpsum') || {}).textContent
    ? tr.querySelector('.grpsum').textContent.replace(/\s+/g, ' ').trim() : '');
  out.клеток_в_заголовке_выпусков = rheads.map(tr => tr.children.length);
  out.видно_выпусков_свёрнутым = [...document.querySelectorAll('table tbody tr')]
    .filter(tr => !tr.hidden).length;
  // Раскрытие в списке выпусков не должно уводить в выпуск: строка сама кликабельна.
  const был_адрес = location.hash;
  const rfold = rheads.length ? rheads[0].querySelector('button.fold') : null;
  if (rfold) rfold.click();
  await new Promise(r => setTimeout(r, 120));
  out.адрес_не_изменился = location.hash === был_адрес;
  out.видно_выпусков_после = [...document.querySelectorAll('table tbody tr')]
    .filter(tr => !tr.hidden).length;

  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=25000)
    err = res.get("error")
    if err:
        # «Браузера нет» и «страница не отработала» — разное, и путать их нельзя.
        # Первое — честный пропуск. Второе — провал: сломанная разметка причин роняет
        # сценарий целиком, и назвать это пропуском значило бы зачесть тесту чужую
        # зелень ровно там, где он должен краснеть.
        if "Chrome" in err:
            print("  ПРОПУЩЕНО:", err)
            return 2      # пропуск не успех: без браузера проверка не выполнялась
        print("  FAIL сценарий не отработал:", err)
        print("\nПРОВАЛЕНО:\n  - сценарий не отработал")
        return 1

    k = res.get("ключи") or {}
    print("Смысл причины: обстоятельства обезличены, сама причина — нет")
    check("падений нет", res.get("errs"), [])
    check("номер задачи, попытка и время не мешают узнать одну беду",
          k.get("ключ_A") == k.get("ключ_B"), True)
    check("точка в конце — не другая беда", k.get("ключ_A") == k.get("ключ_C"), True)
    check("одинаковое начало при разном конце — две беды",
          k.get("ключ_A") == k.get("хвост"), False)
    check("HTTP 402 и HTTP 429 остаются разными бедами",
          k.get("код402") == k.get("код429"), False)
    check("код ответа виден в смысле причины",
          ("код402" in (k.get("код402") or ""), "код429" in (k.get("код429") or "")),
          (True, True))
    check("пути и имена файлов не мешают узнать одну беду",
          k.get("путь_A") == k.get("путь_B"), True)
    check("разные ссылки донора — одна беда",
          k.get("ссылка_A") == k.get("ссылка_B"), True)
    check("пустая причина остаётся пустой", k.get("пусто"), "")

    print("\nОчередь: сорок одинаковых причин — одна строка с числом")
    # Группы: 40 одинаковых упавших, 2 ждущих с той же фразой, 2 у Текста, 3 выпуска.
    check("групп ровно четыре", res.get("групп"), 4)
    check("первая сводка — число и причина, одной строкой", (res.get("сводки") or [None])[0],
          "40 задач · Не хватило ключа FastGen (задача 8000, попытка 1, 10:00:07)")
    check("остальные сводки — со своими числами", (res.get("сводки") or [])[1:],
          ["2 задачи · Не хватило ключа FastGen (задача 9000, попытка 1, 10:00:00)",
           "2 задачи · Не хватило ключа FastGen (задача 400, попытка 2, 11:02:03)",
           "3 выпуска · ждём: общий ключ ElevenLabs исчерпан (проверка 17:00:00)"])
    check("состояние группы названо плашкой, одной на всех", res.get("плашки_групп"),
          [["Видео", "ошибка"], ["Видео", "в очереди"], ["Текст", "ошибка"],
           ["Выпуск", "в очереди"]])
    check("в заголовке три клетки: сервис, сводка, действие",
          res.get("клеток_в_заголовке"), [3, 3, 3, 3])
    check("времени создания группа не выдумывает", res.get("время_в_заголовке"), False)
    # 51 строка данных + 4 заголовка групп = 55; видно 4 заголовка и 4 одиночных строки.
    check("строк в таблице (с заголовками групп)", res.get("строк_всего"), 55)
    check("свёрнутым видно 8 строк вместо 51", res.get("видно_свёрнутым"), 8)

    print("\nРаскрытие показывает сами строки, а не пересказ")
    check("подсказка свёрнутого говорит, сколько внутри",
          res.get("подсказка_свёрнутого"), "Показать все — 40 задач")
    check("после раскрытия видно 48 строк", res.get("видно_после_раскрытия"), 48)
    check("внутри — настоящие задачи", res.get("раскрытые_имена"),
          ["Озвучка 0", "Озвучка 1"])
    check("подсказка раскрытого предлагает свернуть",
          res.get("подсказка_раскрытого"), "Свернуть — 40 задач")
    check("раскрытость объявлена для чтения вслух", res.get("признак_раскрытия"), "true")
    check("сворачивается обратно", res.get("видно_после_сворачивания"), 8)

    print("\nДействие на группу: только там, где оно есть у строки, и только с вопросом")
    check("повтор есть у упавших, нет у ждущих и у выпусков",
          res.get("повтор_у_групп"), [True, False, True, False])
    check("подсказка повтора называет число", (res.get("подсказка_повтора") or [None])[0],
          "Повторить все — 40 задач")
    check("вопрос открылся", res.get("вопрос_открылся"), True)
    check("число — в самом заголовке вопроса", res.get("заголовок_вопроса"),
          "Повторить 40 задач?")
    check("вопрос говорит про деньги и про число", res.get("текст_вопроса"),
          "Каждая задача пойдёт заново, и платные вызовы внутри неё будут оплачены ещё "
          "раз — всего 40 задач. Уже потраченное не вернётся. Причина у всех одна: если "
          "она не устранена, повтор упрётся в неё снова и деньги уйдут впустую.")
    check("фокус стоит на «Нет» — Enter вслепую денег не тратит", res.get("фокус"), "Нет")
    check("«Нет» не отправляет ничего", res.get("после_нет"), [])
    check("«Да» отправляет ровно сорок повторов", res.get("отправлено_всего"), 40)
    check("каждый повтор — своей задаче", res.get("отправлено_уникальных"), 40)
    check("отправлены именно повторы задач группы", res.get("все_повторы"), True)
    check("браузерные confirm/alert не звались", res.get("браузерные_окна"), [])

    print("\nСписок выпусков: то же правило, та же осторожность")
    check("групп две: остановленные и ждущие", res.get("групп_выпусков"), 2)
    check("сводки выпусков со склонением", res.get("сводки_выпусков"),
          ["3 выпуска · Кончился общий ключ ElevenLabs (заказ 88421, 12:03:41)",
           "2 выпуска · Кончился общий ключ ElevenLabs (заказ 12, 01:02:03)"])
    # Одна причина странного вида не должна уносить весь экран: раньше слово вроде
    # «code42» давало падение разбора, и список задач не рисовался вовсе.
    check("слово, похожее на код, но без кода — не роняет разбор",
          bool((res.get("ключи") or {}).get("странное")), True)
    check("и второе такое же", bool((res.get("ключи") or {}).get("странное2")), True)
    check("оно не выдаётся за код ответа",
          "код" in ((res.get("ключи") or {}).get("странное") or ""), False)

    check("заголовок группы занимает строку целиком",
          res.get("клеток_в_заголовке_выпусков"), [1, 1])
    # 7 выпусков + 2 заголовка = 9 строк; видно 2 заголовка и 2 одиночных выпуска.
    check("свёрнутым видно 4 строки вместо 7", res.get("видно_выпусков_свёрнутым"), 4)
    check("раскрытие не уводит в выпуск", res.get("адрес_не_изменился"), True)
    check("после раскрытия видно 7 строк", res.get("видно_выпусков_после"), 7)

    print("\nПРОВАЛЕНО:" if FAILS else "\nвсе проверки прошли")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
