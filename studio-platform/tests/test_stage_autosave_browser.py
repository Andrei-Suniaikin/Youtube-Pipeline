#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настройки этапа сохраняются сами — и ничего этим не запускают.

Жалоба владельца 21.09.2026: «зачем кнопка сохранить, пусть сохраняет само». На вкладке
этапа кнопка «Сохранить» была единственной защитой от потери: уход на соседнюю вкладку
идёт через `location.replace` → `render()`, и набранное пропадало молча. Убрать кнопку и
не поставить ничего на её место значило бы терять правки ещё чаще.

Ручка у автозаписи та же, что была у кнопки: `POST /ui/projects/{pid}/settings`. Кабинет
по ней только СЛИВАЕТ присланные ключи в `projects.settings` — ни задачи, ни списания.
Поэтому сохранять по паузе в наборе здесь безопасно, а на соседних кнопках («Начать» —
платная задача, «Сделать всё» — весь конвейер, обложка — платный заказ, источник
сценария — затирание `source.txt`) — нет, и туда автозапись не ставится.

Проверяется в настоящем браузере (время виртуальное, сеть подменена):
  * «Начать» стоит в заголовке карточки настроек, отдельной карточки «Действие» нет;
  * пауза в наборе отправляет ТОЛЬКО изменившийся ключ, повтор того же значения молчит;
  * поле строгого формата (`voice` — у Lumean это UUID) ждёт ухода с поля, а не паузы:
    иначе кабинет законно отвечал бы 400 на полуслове;
  * отказ не откатывает набранное, держится на экране, ставит сторож ухода и говорит
    один раз на серию, а не на каждую букву;
  * следующая правка уносит и непрошедшее — отдельного «повторить» ждать не нужно;
  * «Начать» по-прежнему шлёт ВСЕ значения формы (пустое тело кабинет трактует как
    `engine=edge` и молча увёл бы проект с Lumean);
  * «Вернуть как было» не только чистит форму, но и ЗАПИСЫВАЕТ значения на момент входа:
    при автосохранении набранное уже лежит на сервере.

    .venv/bin/python tests/test_stage_autosave_browser.py
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
  const out = {errs: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const tick = ms => new Promise(r => setTimeout(r, ms || 150));
  const txt = sel => { const el = document.querySelector(sel);
                       return el ? el.textContent.replace(/\s+/g, ' ').trim() : null; };
  const toasts = () => Array.from(document.querySelectorAll('#toasts .toast'))
                            .map(t => t.textContent.trim());
  const поле = k => document.querySelector('#f-' + k);
  const набрать = (k, v, событие) => {
    const el = поле(k);
    el.value = v;
    el.dispatchEvent(new Event(событие || 'input', {bubbles: true}));
  };

  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  const ГРУППА = {title: 'Озвучка', fields: [
    {key: 'speed', label: 'Скорость', kind: 'text', value: '1.0', placeholder: '', hint: ''},
    {key: 'voice', label: 'Голос по умолчанию', kind: 'text', value: '',
     placeholder: '', hint: ''},
  ]};
  const D = {project: {id: 'p1', title: 'Проба'}, files: [], jobs: []};
  window.api = async (p) => {
    if (p.indexOf('/ui/projects/') === 0 && p.indexOf('/settings') > 0)
      return {groups: [ГРУППА]};
    return {};
  };
  const ушло = [];
  let ОТКАЗ = null;
  window.post = async (path, body) => {
    ушло.push({path, body});
    if (ОТКАЗ) throw new Error(ОТКАЗ);
    return {ok: true, outcome: 'started', job_id: 7};
  };
  // Экран целиком перерисовывать нечем (сервер подменён) — важно лишь, что откат его
  // запрашивает: форма обязана собраться из ответа сервера, а не из нашей памяти.
  window.viewProject = async () => { out.откат_перерисовал_экран = true; };

  app.innerHTML = '';
  await renderStageTab(D, 'tts');

  // ── 1. Раскладка: действие в заголовке настроек, кнопки «Сохранить» нет ─────
  out.кнопка_в_заголовке = !!document.querySelector('.sec.stage-head #stage-go');
  out.исход_рядом_с_кнопкой = !!document.querySelector('.sec.stage-head #stage-outcome');
  out.карточки_действие_нет = !Array.from(document.querySelectorAll('.sec'))
      .some(s => s.textContent.trim() === 'Действие');
  out.кнопки_сохранить_нет = !document.querySelector('#save');
  // Именно видимость, а не атрибут: у подвала свой display:flex, и он сильнее
  // браузерного [hidden]{display:none} — с одним атрибутом полоса осталась бы на
  // экране (та же ловушка, что у строки «Сохранить сценарий» на вкладке источника).
  const видно = sel => { const el = document.querySelector(sel);
    return !!(el && getComputedStyle(el).display !== 'none'); };
  out.подвал_молчит_до_правок = !видно('.stage-foot');

  // ── 2. Пауза в наборе: уезжает только изменившийся ключ ────────────────────
  набрать('speed', '1.5');
  await tick(1200);
  out.после_паузы = ушло.slice(-1)[0] || null;
  out.запросов_после_первой_правки = ушло.length;
  out.сказано_после_записи = txt('#stage-save');
  out.подвал_виден = видно('.stage-foot');

  // Повтор того же значения: слать нечего — пустое тело ручка отвергает.
  набрать('speed', '1.5');
  await tick(1200);
  out.повтор_того_же_молчит = (ушло.length === out.запросов_после_первой_правки);

  // ── 3. Поле строгого формата ждёт ухода с поля ─────────────────────────────
  набрать('voice', 'недобранный-uu');
  await tick(1200);
  out.строгое_поле_по_паузе_молчит = (ушло.length === out.запросов_после_первой_правки);
  поле('voice').dispatchEvent(new Event('focusout', {bubbles: true}));
  await tick(1200);
  out.строгое_поле_по_уходу = ушло.slice(-1)[0] || null;

  // ── 4. Отказ: набранное на месте, сторож ухода поставлен, тост один ─────────
  const тостов_до = toasts().length;
  ОТКАЗ = 'Lumean требует UUID Template ID';
  набрать('speed', '2.0');
  await tick(1200);
  набрать('speed', '2.2');
  await tick(1200);
  out.отказ_на_экране = txt('#stage-save');
  out.набранное_цело = поле('speed').value;
  out.сторож_ухода_стоит = !!(LEAVE_GUARD && LEAVE_GUARD.dirty());
  out.тостов_на_серию = toasts().length - тостов_до;
  out.повторить_предложено = видно('#stage-retry');

  // ── 5. Следующая правка уносит и непрошедшее ───────────────────────────────
  ОТКАЗ = null;
  набрать('speed', '2.5');
  await tick(1200);
  out.после_починки = ушло.slice(-1)[0] || null;
  out.сторож_снят = (LEAVE_GUARD === null);

  // ── 6. «Начать» по-прежнему шлёт ВСЕ значения формы ────────────────────────
  document.querySelector('#stage-go').click();
  await tick(400);
  out.старт = ушло.slice(-1)[0] || null;

  // ── 6б. Отказ, который больше ни о чём, снимается ────────────────────────────
  // Вернули поле к сохранённому — слать нечего, и прежний отказ обязан уйти: иначе
  // «не сохранено» висело бы вечно, сторож пугал бы потерей, которой нет, а «Сделать
  // всё» отказывалось бы запускать.
  ОТКАЗ = 'Скорость — число';
  набрать('speed', 'abc');
  await tick(1200);
  out.отказ_был = !!(LEAVE_GUARD && LEAVE_GUARD.dirty());
  ОТКАЗ = null;
  const было6 = ушло.length;
  набрать('speed', '2.5');                  // то, что уже лежит на сервере
  await tick(1200);
  out.вернули_ничего_не_ушло = (ушло.length === было6);
  out.вернули_подпись = txt('#stage-save');
  out.вернули_сторож_снят = (LEAVE_GUARD === null);
  out.вернули_повторить_скрыт = !видно('#stage-retry');
  out.вернули_запуск_разрешён = await STAGE_FLUSH();

  // ── 7. «Вернуть как было» — это запись, а не только чистка формы ───────────
  out.откат_предложен = видно('#stage-undo');
  const до_отката = ушло.length;
  // Откат переписывает ВСЁ, что изменилось с открытия вкладки, в том числе давно
  // сохранённое, — поэтому сперва вопрос, и до ответа на сервер не уходит ничего.
  document.querySelector('#stage-undo').click();
  await tick(200);
  out.откат_спрашивает = !!document.querySelector('.inline-ask .ask-ok');
  out.до_ответа_молчит = (ушло.length === до_отката);
  document.querySelector('.inline-ask .ask-no').click();
  await tick(200);
  out.на_нет_молчит = (ушло.length === до_отката);
  // На время вопроса кнопку выключают, чтобы откат не ушёл дважды. Передумавший
  // обязан получить её обратно — иначе экран ломается молча, до перезагрузки.
  out.после_нет_кнопка_жива = !document.querySelector('#stage-undo').disabled;

  // Откат не обгоняет запись, которая уже в полёте. Нажатие мышью сперва снимает
  // фокус с поля, и `focusout` отправляет набранное; если бы откат ушёл параллельно,
  // порядок прихода был бы не определён и на сервере могло остаться набранное.
  let отпустить;
  const медленно = new Promise(r => { отпустить = r; });
  const прежний_post = window.post;
  window.post = async (path, body) => {
    ушло.push({path, body});
    if (body && body.values && body.values.speed === '2.7') await медленно;
    return {ok: true};
  };
  набрать('speed', '2.7');
  поле('speed').dispatchEvent(new Event('focusout', {bubbles: true}));
  await tick(50);
  document.querySelector('#stage-undo').click();
  await tick(300);
  out.откат_ждёт_запись_в_полёте = !document.querySelector('.inline-ask .ask-ok');
  отпустить();
  await tick(300);
  document.querySelector('.inline-ask .ask-ok').click();
  await tick(400);
  window.post = прежний_post;
  out.порядок_записей = ушло.slice(-2).map(x => x.body.values.speed);
  out.откат = ушло.slice(-1)[0] || null;

  // ── 8. Отказ, прилетевший ПОСЛЕ ухода с вкладки, не поднимает сторож ───────
  // Записать стоило попытаться (уход с поля — тоже повод), но стеречь уже нечего:
  // формы с набранным на странице нет, и «остаться» ничего не вернёт. Сторож в этот
  // момент задал бы вопрос про правку, которой уже не существует.
  ОТКАЗ = 'кабинет не ответил';
  набрать('speed', '3.0');
  app.innerHTML = '';                       // человек ушёл на соседнюю вкладку
  await tick(1200);
  out.сторож_без_формы_не_ставится = (LEAVE_GUARD === null);
  out.но_сказано_вслух = toasts().slice(-1)[0] || '';

  // ── 9. Вопрос отката не переживает свой повод ──────────────────────────────
  // Пока висел вопрос «Вернуть?», человек сам вернул поле — откатывать нечего. Вопрос
  // уходит, а кнопка снова жива; раньше он прятался вместе с подвалом и всплывал
  // потом устаревшим, а кнопка оставалась выключенной до перерисовки.
  ОТКАЗ = null;
  app.innerHTML = '';
  await renderStageTab(D, 'tts');
  набрать('speed', '4.0');
  await tick(1200);
  document.querySelector('#stage-undo').click();
  await tick(200);
  out.вопрос_открыт = !!document.querySelector('.stage-foot .inline-ask');
  набрать('speed', '1.0');
  await tick(1200);
  out.вопрос_снят = !document.querySelector('.stage-foot .inline-ask');
  out.кнопка_отката_жива = !document.querySelector('#stage-undo').disabled;

  } catch (e) { out.errs.push('сценарий: ' + (e && e.message || e)); }
  finish();
})();
"""


def main() -> int:
    print("Проверка: настройки этапа сохраняются сами, ничего не запуская")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=20000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2

    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nРаскладка")
    check("«Начать» — в заголовке карточки настроек этапа",
          res.get("кнопка_в_заголовке"), True)
    check("строка исхода — там же, рядом с кнопкой",
          res.get("исход_рядом_с_кнопкой"), True)
    check("отдельной карточки «Действие» на экране нет",
          res.get("карточки_действие_нет"), True)
    check("кнопки «Сохранить» нет", res.get("кнопки_сохранить_нет"), True)
    check("подвал карточки молчит, пока сказать нечего",
          res.get("подвал_молчит_до_правок"), True)

    print("\nЗапись по паузе в наборе")
    check("уезжает ручка настроек и ТОЛЬКО изменившийся ключ",
          res.get("после_паузы"),
          {"path": "/ui/projects/p1/settings", "body": {"values": {"speed": "1.5"}}})
    check("правка — один запрос, а не хвост параллельных",
          res.get("запросов_после_первой_правки"), 1)
    check("сказано скромно и точно",
          bool((res.get("сказано_после_записи") or "").startswith("сохранено ")), True)
    check("подвал появился вместе с первой правкой", res.get("подвал_виден"), True)
    check("повтор того же значения не шлёт ничего",
          res.get("повтор_того_же_молчит"), True)

    print("\nПоле строгого формата (у Lumean голос — UUID)")
    check("по паузе в наборе не уезжает", res.get("строгое_поле_по_паузе_молчит"), True)
    check("уезжает по уходу с поля",
          res.get("строгое_поле_по_уходу"),
          {"path": "/ui/projects/p1/settings",
           "body": {"values": {"voice": "недобранный-uu"}}})

    print("\nОтказ кабинета посреди набора")
    check("виден на экране словами кабинета",
          res.get("отказ_на_экране"), "не сохранено: Lumean требует UUID Template ID")
    check("набранное не откачено", res.get("набранное_цело"), "2.2")
    check("сторож ухода поставлен — молча правка не пропадёт",
          res.get("сторож_ухода_стоит"), True)
    check("на серию отказов — один тост", res.get("тостов_на_серию"), 1)
    check("предложено повторить", res.get("повторить_предложено"), True)

    print("\nПочинка")
    check("следующая правка уносит и непрошедшее",
          res.get("после_починки"),
          {"path": "/ui/projects/p1/settings", "body": {"values": {"speed": "2.5"}}})
    check("сторож ухода снят", res.get("сторож_снят"), True)

    print("\nДеньги и работа остались за «Начать»")
    check("старт шлёт ВСЕ значения формы своим адресом",
          res.get("старт"),
          {"path": "/ui/projects/p1/stage/tts",
           "body": {"settings": {"speed": "2.5", "voice": "недобранный-uu"}}})

    print("\nОтказ, который больше ни о чём")
    check("отказ был", res.get("отказ_был"), True)
    check("вернули сохранённое — ничего не уходит", res.get("вернули_ничего_не_ушло"), True)
    check("«не сохранено» снято", res.get("вернули_подпись"), "")
    check("сторож ухода снят", res.get("вернули_сторож_снят"), True)
    check("«Повторить» скрыт", res.get("вернули_повторить_скрыт"), True)
    check("«Сделать всё» больше не заперто", res.get("вернули_запуск_разрешён"), None)

    print("\n«Вернуть как было»")
    check("предложено, пока значения разошлись со входом на вкладку",
          res.get("откат_предложен"), True)
    check("записывает значения на момент входа, а не только чистит форму",
          res.get("откат"),
          {"path": "/ui/projects/p1/settings",
           "body": {"values": {"speed": "1.0", "voice": ""}}})
    check("и перерисовывает экран из ответа сервера",
          res.get("откат_перерисовал_экран"), True)
    check("сперва спрашивает — это самая сильная запись на экране",
          res.get("откат_спрашивает"), True)
    check("до ответа ничего не уходит", res.get("до_ответа_молчит"), True)
    check("на «Нет» ничего не уходит", res.get("на_нет_молчит"), True)
    check("после «Нет» кнопка снова нажимается", res.get("после_нет_кнопка_жива"), True)
    check("не обгоняет запись в полёте: вопрос появляется, когда она закончилась",
          res.get("откат_ждёт_запись_в_полёте"), True)
    check("и ложится ПОСЛЕ набранного — на сервере остаётся возвращённое",
          res.get("порядок_записей"), ["2.7", "1.0"])

    print("\nВопрос отката не переживает свой повод")
    check("вопрос был открыт", res.get("вопрос_открыт"), True)
    check("вернули поле сами — вопрос снят", res.get("вопрос_снят"), True)
    check("и кнопка отката снова жива", res.get("кнопка_отката_жива"), True)

    print("\nОтказ уже после ухода с вкладки")
    check("сторож ухода не поднимается — стеречь нечего, формы с набранным нет",
          res.get("сторож_без_формы_не_ставится"), True)
    check("но человеку всё равно сказано, что запись не прошла",
          res.get("но_сказано_вслух"), "кабинет не ответил")

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
