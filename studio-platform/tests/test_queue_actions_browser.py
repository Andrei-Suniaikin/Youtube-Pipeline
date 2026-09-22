#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Действия в строке очереди — значок вместо надписи, удаление спрашивается окном.

Жалоба владельца 20.09.2026: «в очереди зачем-то огромные кнопки повторить и убрать,
давай заменим на иконки, для убрать просто сделаем попап красивый с вопросом и кнопками
да/нет, а то сейчас экран съезжает аж».

Съезжал экран по двум причинам, и проверяются обе:

  * ширину колонке действий задавало САМОЕ ДЛИННОЕ слово в ней («Повторить», «Открыть»
    + «Убрать»), а слова в колонке разные у разных состояний — значит, каждое обновление
    очереди двигало таблицу. Теперь в колонке значки постоянного размера, слово живёт в
    `title`, а сама колонка объявлена фиксированной шириной (`th.acts/td.acts`);
  * вопрос «убрать?» рисовался ВРЕЗКОЙ ВНУТРЬ строки (`confirmInline`) и раздвигал её.
    Теперь это наш `<dialog>` с «Да»/«Нет»: он ничего не двигает. Браузерный `confirm`
    по-прежнему запрещён — сторож на это есть в `tests/test_project_screen_ui_qa.py`,
    здесь проверяется, что его действительно не зовут.

Отдельно — то, что легко сломать при переходе на значки: отказ сервиса. Кнопка внутри
себя больше не текст, а `<svg>`; вернуть её после неудачи через `textContent` значит
получить пустой квадрат, а положить длинный отказ в узкую колонку значков — снова
раздвинуть таблицу. Проверяется, что значок возвращается и что отказ уходит в колонку
состояния, не меняя ширины колонки действий.

Сети и стенда не нужно: `api()`, `post()` и `render()` подменены, браузер настоящий.

    .venv/bin/python tests/test_queue_actions_browser.py
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

  // Браузерные окна запрещены: если их позовут, это видно здесь, а не на показе.
  window.confirm = (t) => { out.браузерные_окна.push('confirm: ' + t); return true; };
  window.alert   = (t) => { out.браузерные_окна.push('alert: ' + t); };
  window.prompt  = (t) => { out.браузерные_окна.push('prompt: ' + t); return ''; };

  const QUEUE = {
    services: [{key: 'vsgen', title: 'Видео', ok: true, count: 3, error: ''}],
    channels: [], states: [], periods: [], shown: 4, total: 4,
    rows: [
      {service: 'vsgen', id: 'j-run', title: 'Кадры', kind: 'images', kind_title: 'Картинки',
       state: 'running', status: 'running', created: 1758300000, progress: 0.4},
      {service: 'vsgen', id: 'j-err', title: 'Озвучка', kind: 'tts', kind_title: 'Озвучка',
       state: 'error', status: 'error', created: 1758300000, error: 'провайдер молчит'},
      {service: 'vsgen', id: 'j-done', title: 'Рендер', kind: 'render', kind_title: 'Сборка',
       state: 'done', status: 'done', created: 1758300000},
      {service: 'release', id: 'r-1', title: 'Выпуск канала', kind: '', kind_title: '',
       state: 'queued', status: 'queued', created: 1758300000,
       release_id: 'rel-7', plan_item_id: 77}]};

  window.api = async (p) => {
    if (p.startsWith('/ui/queue-pause')) return {paused: false};
    if (p.startsWith('/ui/queue')) return QUEUE;
    return {};
  };
  // Отмена задачи отказывается — ради проверки возврата значка и места для отказа.
  window.post = async (p, b) => {
    out.sent.push(p);
    if (p.indexOf('/cancel') >= 0 && p.indexOf('/ui/actions/') === 0)
      throw new Error('нельзя отменить: задача уже ушла в работу на ноду');
    return {ok: true};
  };
  // render() перерисовал бы экран настоящими ручками — здесь только считаем вызовы.
  window.render = async () => { out.перерисовок = (out.перерисовок || 0) + 1; };

  await viewQueue();

  const rows = [...document.querySelectorAll('table tbody tr')];
  const acts = rows.map(tr => tr.querySelector('td.acts'));
  out.строк = rows.length;
  out.колонка_действий_у_всех = acts.every(Boolean);

  // Ни одной надписи: в колонке только значки.
  out.подписи = acts.map(td => td.textContent.trim());
  out.значков_в_строке = acts.map(td => td.querySelectorAll('svg').length);
  out.подсказки = acts.map(td => [...td.querySelectorAll('[title]')]
    .map(el => el.getAttribute('title')));

  // Ширина колонки одна и та же во всех строках, и она узкая.
  const w = acts.map(td => Math.round(td.getBoundingClientRect().width));
  out.ширины = w;
  out.ширина_одна = w.every(x => x === w[0]);
  out.ширина_узкая = w[0] <= 120;

  // ── «Убрать» у выпуска: окно с «Да»/«Нет», строка на месте ──
  const relRow = rows[3];
  const высота_до = Math.round(relRow.getBoundingClientRect().height);
  relRow.querySelector('button.rel-del').click();
  await new Promise(r => setTimeout(r, 200));
  const dlg = document.querySelector('dialog[open]');
  out.окно_открылось = !!dlg;
  out.кнопки_окна = dlg ? [...dlg.querySelectorAll('button')].map(b => b.textContent.trim())
                        : [];
  out.вопрос = dlg ? dlg.querySelector('.mbody').textContent.trim() : '';
  // Enter вслепую не должен удалять: фокус стоит на безопасном ответе.
  out.фокус = document.activeElement ? document.activeElement.textContent.trim() : '';
  out.врезки_в_строке_нет = !document.querySelector('.inline-ask');
  out.строка_не_раздулась = Math.round(relRow.getBoundingClientRect().height) === высота_до;
  out.ширина_при_вопросе = Math.round(acts[3].getBoundingClientRect().width);

  // Дальше — только если окно есть. Без этой оговорки сломанное удаление роняло бы
  // сценарий целиком, и тест говорил бы «пропущено» вместо внятного «не открылось».
  if (dlg) {
    // «Нет» — ничего не отправлено, окно закрылось.
    [...dlg.querySelectorAll('button')].find(b => b.textContent.trim() === 'Нет').click();
    await new Promise(r => setTimeout(r, 200));
    out.после_нет_окно = !!document.querySelector('dialog[open]');
    out.после_нет_отправлено = out.sent.slice();

    // «Да» — уходит ровно отмена строки плана.
    relRow.querySelector('button.rel-del').click();
    await new Promise(r => setTimeout(r, 200));
    const dlg2 = document.querySelector('dialog[open]');
    if (dlg2)
      [...dlg2.querySelectorAll('button')].find(b => b.textContent.trim() === 'Да').click();
    await new Promise(r => setTimeout(r, 400));
    out.после_да_отправлено = out.sent.slice();
    out.после_да_окно = !!document.querySelector('dialog[open]');
  }

  // ── Отказ сервиса на «Отменить»: значок вернулся, слова — в колонке состояния ──
  const cancelBtn = rows[0].querySelector('button.act');
  cancelBtn.click();
  await new Promise(r => setTimeout(r, 400));
  out.значок_вернулся = cancelBtn.querySelectorAll('svg').length === 1;
  out.кнопка_снова_жива = !cancelBtn.disabled;
  out.отказ_в_состоянии = (rows[0].querySelector('td.st .note.bad') || {}).textContent || '';
  out.отказа_в_колонке_значков_нет = !acts[0].querySelector('.note');
  out.ширина_после_отказа = Math.round(acts[0].getBoundingClientRect().width);

  // ── очередь без выпуска: колонка не должна стать другой ──
  // Главное про «экран съезжает»: очередь обновляется сама, и состав строк в ней
  // меняется. Если ширину колонки задаёт её содержимое, то приход или уход одной
  // строки с двумя значками двигает всю таблицу под рукой у человека.
  QUEUE.rows = [QUEUE.rows[0]];
  await viewQueue();
  const один = document.querySelector('table tbody tr td.acts');
  out.ширина_короткой_очереди = один ? Math.round(один.getBoundingClientRect().width) : null;

  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print("  ПРОПУЩЕНО:", res["error"])
        return 2          # пропуск не успех: без браузера проверка не выполнялась

    print("Колонка действий в очереди — значки, а не надписи")
    check("падений нет", res.get("errs"), [])
    check("нарисованы все четыре строки", res.get("строк"), 4)
    check("у каждой строки колонка действий", res.get("колонка_действий_у_всех"), True)
    check("надписей в колонке нет ни в одной строке", res.get("подписи"), ["", "", "", ""])
    check("значки: отменить, повторить, ничего, открыть+убрать",
          res.get("значков_в_строке"), [1, 1, 0, 2])
    check("слово осталось в подсказке (title)", res.get("подсказки"),
          [["Отменить"], ["Повторить"], [], ["Открыть", "Убрать"]])
    check("ширина колонки одинакова во всех строках", res.get("ширина_одна"), True)
    check(f"колонка узкая, не по слову «Повторить» (ширины {res.get('ширины')})",
          res.get("ширина_узкая"), True)

    print("\n«Убрать» спрашивает окном с «Да»/«Нет», а не врезкой в строке")
    check("окно открылось", res.get("окно_открылось"), True)
    check("в окне ровно «Да» и «Нет» (плюс крестик закрытия в шапке)",
          res.get("кнопки_окна"), ["×", "Да", "Нет"])
    check("вопрос говорит, что будет с материалами", res.get("вопрос"),
          "Выпуск уйдёт из плана канала. Материалы проекта останутся на диске.")
    check("фокус стоит на «Нет» — Enter вслепую ничего не удалит", res.get("фокус"), "Нет")
    check("врезки внутрь строки не появилось", res.get("врезки_в_строке_нет"), True)
    check("строка не стала выше", res.get("строка_не_раздулась"), True)
    check("колонка действий не поехала", res.get("ширина_при_вопросе"),
          (res.get("ширины") or [None])[0])
    check("«Нет» закрывает окно", res.get("после_нет_окно"), False)
    check("«Нет» ничего не отправляет", res.get("после_нет_отправлено"), [])
    check("«Да» отправляет отмену строки плана", res.get("после_да_отправлено"),
          ["/ui/plan/77/cancel"])
    check("после «Да» окно закрыто", res.get("после_да_окно"), False)
    check("браузерные confirm/alert/prompt не звались", res.get("браузерные_окна"), [])

    print("\nОтказ сервиса на «Отменить»: значок цел, слова — в колонке состояния")
    check("на кнопке снова значок, а не пустота", res.get("значок_вернулся"), True)
    check("кнопкой можно пользоваться снова", res.get("кнопка_снова_жива"), True)
    check("отказ показан в колонке состояния", res.get("отказ_в_состоянии"),
          "нельзя отменить: задача уже ушла в работу на ноду")
    check("в узкой колонке значков отказа нет", res.get("отказа_в_колонке_значков_нет"), True)
    check("ширина колонки действий не изменилась", res.get("ширина_после_отказа"),
          (res.get("ширины") or [None])[0])

    print("\nОбновление очереди не двигает таблицу")
    check("очередь из одной строки — та же ширина колонки действий",
          res.get("ширина_короткой_очереди"), (res.get("ширины") or [None])[0])

    print("\nПРОВАЛЕНО:" if FAILS else "\nвсе проверки прошли")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
