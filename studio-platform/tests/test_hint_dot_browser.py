#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Знак вопроса прячет руководство — и НЕ прячет предупреждения про деньги.

Жалоба владельца 21.09.2026: «экраны перегружены текстом». Разбор показал, что текст на
экране двух разных сортов, и мерить их одной меркой нельзя:

* **руководство** («за что отвечает поле», «что показывает экран») висит всегда и
  читается один раз в жизни — оно и есть перегрузка;
* **предупреждение** («денег это не стоило», «повтор заказа — это вторые деньги»)
  останавливает руку ДО необратимого. Предупреждение, которое надо раскрыть кликом, уже
  не предупреждение, и прятать его нельзя ни под каким видом.

Поэтому тест ходит парой: проверяет, что подсказка работает, и тут же — что на том же
экране предупреждения остались обычным текстом.

Что закрепляется:

* подсказка раскрывается наведением, кликом и фокусом с клавиатуры (на тач-экране нет
  hover, у клавиатуры нет ни hover, ни мыши) и закрывается Esc;
* она не вылезает за правый край экрана — знаки вопроса стоят и у правой колонки;
* клик по знаку вопроса внутри `<label>` не переключает поле, а внутри кликабельной
  строки не открывает её: иначе «прочитать подсказку» означало бы «случайно нажать»;
* подзаголовок экрана снят с экрана, но его текст доступен по знаку вопроса у заголовка;
* предупреждения про деньги на экране «Оплачено, но не забрано» остались ТЕКСТОМ;
* число у вкладки проекта — кружок-счётчик с подписью, чего именно N, а не голая цифра
  рядом с названием (владелец прочёл «Озвучка 2» как «вторая озвучка»).

Ни сети, ни стенда: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_hint_dot_browser.py
"""
from __future__ import annotations

import os
import re
import sys

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


def has(label, text, needle):
    check(label, needle in (text or ""), True)


def hasnt(label, text, needle):
    check(label, needle in (text or ""), False)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  const tick = ms => new Promise(r => setTimeout(r, ms || 200));
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  window.api = async (p) => {
    if (p === '/ui/paid-texts') return {
      free: 'Подбор денег не тратит: он спрашивает статус уже оплаченной задачи.',
      batch: 40,
      how: 'Список собран по журналу оплат.',
      collect: {running: false}, reasons: {},
      rows: [{release_id: 'r1', donor_title: 'Ролик', ch_name: 'Канал', lang: 'pl',
              job_id: 'j1', src: 'job', waiting_sec: 600}],
    };
    if (p === '/ui/me') return {name: 'dev'};
    return {};
  };
  window.post = async () => ({ok: true});

  addEventListener('hashchange', render);
  location.hash = '#/paid-texts';
  await tick(500);

  const h1 = document.querySelector('#app h1');
  const dot = h1 && h1.querySelector('.q');
  out.знак_у_заголовка = !!dot;
  out.подзаголовка_нет = !document.querySelector('#app p.sub');
  out.текст_экрана = document.querySelector('#app').textContent.replace(/\s+/g, ' ');

  // Наведение — подсказка появилась и ушла.
  dot.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
  await tick(120);
  let box = document.querySelector('.qbox');
  out.наведение_открыло = !!box;
  out.в_подсказке = box ? box.textContent : '';
  dot.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
  await tick(120);
  out.уход_курсора_закрыл = !document.querySelector('.qbox');

  // Клик — подсказка прикалывается и уход курсора её уже не снимает.
  dot.click();
  await tick(120);
  out.клик_открыл = !!document.querySelector('.qbox');
  dot.dispatchEvent(new MouseEvent('mouseout', {bubbles: true}));
  await tick(120);
  out.приколотую_не_снесло = !!document.querySelector('.qbox');
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await tick(120);
  out.esc_закрыл = !document.querySelector('.qbox');

  // Фокус с клавиатуры — тоже раскрытие.
  dot.focus();
  await tick(120);
  out.фокус_открыл = !!document.querySelector('.qbox');
  dot.blur();
  await tick(120);
  out.потеря_фокуса_закрыла = !document.querySelector('.qbox');

  // У правого края экрана подсказка не вылезает за обрез.
  const probe = document.createElement('div');
  probe.style.cssText = 'position:absolute;right:2px;top:300px';
  probe.innerHTML = hintDot('Длинная подсказка, которой у самого правого края экрана '
    + 'не хватило бы места, если бы её просто центрировали по знаку вопроса.');
  document.body.append(probe);
  const edgeDot = probe.querySelector('.q');
  edgeDot.click();
  await tick(150);
  box = document.querySelector('.qbox');
  const r = box.getBoundingClientRect();
  out.края = [Math.round(r.left) >= 0,
              Math.round(r.right) <= Math.round(document.documentElement.clientWidth)];
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  probe.remove();
  await tick(100);

  // Знак вопроса внутри <label> не переключает поле.
  const lab = document.createElement('label');
  lab.setAttribute('for', 'hint-probe-box');
  lab.innerHTML = 'Копить ' + hintDot('Складывать задачи, пока их не наберётся пачка.');
  const cb = document.createElement('input');
  cb.type = 'checkbox'; cb.id = 'hint-probe-box';
  document.body.append(lab, cb);
  lab.querySelector('.q').click();
  await tick(150);
  out.галочка_не_тронута = cb.checked === false;
  out.подсказка_поля_открылась = !!document.querySelector('.qbox');
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));

  // И внутри кликабельной строки не открывает её.
  let открыта = 0;
  const row = document.createElement('div');
  row.innerHTML = 'Задача ' + hintDot('Номер задачи у соседнего сервиса.');
  row.onclick = () => открыта++;
  document.body.append(row);
  row.querySelector('.q').click();
  await tick(150);
  out.строка_не_открылась = открыта === 0;
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  lab.remove(); cb.remove(); row.remove();

  } catch (e) { out.errs.push('сценарий: ' + ((e && e.stack) || e)); }
  finish();
})();
"""


def main() -> int:
    index_html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()

    print("Подсказка у знака вопроса")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])
    check("у заголовка экрана есть знак вопроса", res.get("знак_у_заголовка"), True)
    check("а подзаголовка под ним больше нет", res.get("подзаголовка_нет"), True)
    check("наведение раскрывает", res.get("наведение_открыло"), True)
    has("в подсказке — тот самый снятый подзаголовок", res.get("в_подсказке"),
        "Выпуски, за текст которых рерайту уже заплачено")
    check("уход курсора закрывает", res.get("уход_курсора_закрыл"), True)
    check("клик тоже раскрывает (на тач-экране hover нет)", res.get("клик_открыл"), True)
    check("и приколотую кликом уход курсора уже не снимает — иначе длинный текст "
          "нельзя было бы дочитать", res.get("приколотую_не_снесло"), True)
    check("Esc закрывает", res.get("esc_закрыл"), True)
    check("фокус с клавиатуры раскрывает", res.get("фокус_открыл"), True)
    check("потеря фокуса закрывает", res.get("потеря_фокуса_закрыла"), True)
    check("у правого края не вылезает за обрез", res.get("края"), [True, True])

    print("\nЗнак вопроса не нажимает то, внутри чего стоит")
    check("клик по нему внутри <label> не ставит галочку",
          res.get("галочка_не_тронута"), True)
    check("но подсказку показывает", res.get("подсказка_поля_открылась"), True)
    check("клик внутри кликабельной строки не открывает строку",
          res.get("строка_не_открылась"), True)

    print("\nПредупреждения про деньги остались текстом, а не уехали под знак вопроса")
    экран = res.get("текст_экрана")
    has("«подбор денег не тратит» видно сразу", экран, "Подбор денег не тратит")
    has("«денег не тратит» стоит и на самой кнопке", экран, "денег не тратит)")
    has("«повтор заказа — это вторые деньги» тоже на виду", экран,
        "повтор заказа — это вторые деньги")
    hasnt("а руководство про размер прохода с экрана снято", экран,
          "За одно нажатие опрашивается")

    print("\nЧисло у вкладки проекта — счётчик, а не номер запуска")
    check("цифра стоит в кружке `.cnt`, а не просто серым текстом",
          '<span class="cnt" title=' in index_html, True)
    check("у кружка есть стиль круглого значка",
          bool(re.search(r"\.subtabs \.cnt\{[^}]*border-radius:999px", index_html)), True)
    check("подпись значка говорит, чего именно N",
          'const what = `файлов: ${n}`' in index_html, True)
    check("у озвучки и видео сказано, что это части ОДНОГО результата",
          "${partsWord(n)} одного результата" in index_html, True)
    check("единица измерения словом рядом с названием вкладки больше не пишется",
          "' ' + partsWord(n)" in index_html, False)

    # Фраза «у Видео и Выпусков дочитывать нечего… пустое место там значит „не
    # записано“» здесь раньше числилась снятой. Это не история и не обоснование, а
    # смысл пустой клетки очереди: без неё два разных «пусто» на одном экране читаются
    # одинаково. Её возвращение закрепляет tests/test_queue_row_browser.py.

    print("\nЗнак вопроса — сосед подписи поля, а не её часть")
    # Кнопка внутри <label> недопустима по HTML и дописывает текст подсказки к имени
    # поля: читалка объявляла бы абзац руководства на каждом заходе в поле.
    внутри = []
    for m in re.finditer(r"hintDot\(", index_html):
        before = index_html[max(0, m.start() - 600):m.start()]
        if before.rfind("<label") > before.rfind("</label>"):
            внутри.append(index_html.count("\n", 0, m.start()) + 1)
    check("ни одного знака вопроса внутри <label>", внутри, [])

    print("\nИстория и обоснование с экрана убраны (группа «в»)")
    for label, needle in [
        ("ссылки на ТЗ в видимом тексте нет", "как ТЗ запрещает"),
        ("«правило унаследовано из боя»", "унаследовано из боя"),
        ("«здесь их формы не повторяются намеренно»", "Здесь их формы не повторяются"),
        ("«его здесь не спрашивают и не дают выбрать»", "не дают выбрать"),
        ("слово «Сегодня» у ссылок в никуда", '<div class="note muted">Сегодня'),
    ]:
        check(label + " — снято", needle in index_html, False)

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
