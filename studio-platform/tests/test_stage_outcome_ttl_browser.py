#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отказ на кнопке «Начать» не превращается в призрак.

Жалоба владельца 21.09.2026: «нажал начать делать картинки а он их не делает, пишет
мол уже в процессе, хотя я его не вижу». В базе в этот момент по проекту не было ни
одной задачи картинок в работе: мешавшие `prompts` и `tts` давно отменены, и тот же
запрос, которым кабинет решает «дубль или нет», отвечал «старт разрешён».

Врал не сервер, а экран. Исход нажатия сохранялся, чтобы пережить отложенную
перерисовку (после успешного старта экран обновляется через 1,2 с, и «Поставлено:
задача #N» иначе пропадало непрочитанным). Но срока у записи не было. Пока этап
стоит в очереди, экран автообновляется, и старая надпись рисовалась заново снова и
снова — человек видел отказ, которого уже не существовало, и не нажимал кнопку,
которая сработала бы.

Цена ошибки несимметрична: отсутствие надписи стоит одного лишнего нажатия, а ложный
отказ останавливает работу совсем. Поэтому исход теперь живёт 15 секунд — достаточно,
чтобы пережить ту самую перерисовку, и слишком мало, чтобы дожить до следующего
вопроса человека.

Проверяется в настоящем браузере, часы подменены — иначе пришлось бы ждать минуту:
  * отказ виден сразу после нажатия;
  * он переживает перерисовку через 1,2 с — ради чего всё и затевалось;
  * через минуту его нет, и кнопка не оболгана;
  * успешный «Поставлено: задача #N» живёт по тем же правилам.

Ни сети, ни стенда: сервер подменён, браузер настоящий.

    .venv/bin/python tests/test_stage_outcome_ttl_browser.py
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
  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const outcomeText = () => txt(document.querySelector('#stage-outcome'));

  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  const D = {project: {id: 'p_проба', title: 'Проба'}, files: [], jobs: []};
  window.api = async (p) => {
    if (p.indexOf('/ui/projects/') === 0 && p.indexOf('/settings') > 0) return {groups: []};
    return {};
  };
  let ОТВЕТ = {outcome: 'duplicate'};
  window.post = async () => ОТВЕТ;

  // Часы под нашим управлением: ждать настоящую минуту в тесте нечестно и долго.
  let СЕЙЧАС = 1700000000000;
  Date.now = () => СЕЙЧАС;

  const нарисовать = async () => { app.innerHTML = ''; await renderStageTab(D, 'images'); };

  // ── 1. Отказ виден сразу после нажатия ────────────────────────────────────
  await нарисовать();
  document.querySelector('#stage-go').click();
  await tick(200);
  out.после_нажатия = outcomeText();

  // ── 2. Переживает перерисовку через 1,2 с — ради этого всё и делалось ─────
  СЕЙЧАС += 1200;
  await нарисовать();
  out.через_1_2с = outcomeText();

  // ── 3. Через минуту призрака нет ──────────────────────────────────────────
  СЕЙЧАС += 60000;
  await нарисовать();
  out.через_минуту = outcomeText();

  // ── 4. И он не воскресает при следующей перерисовке ───────────────────────
  СЕЙЧАС += 1000;
  await нарисовать();
  out.ещё_позже = outcomeText();

  // ── 5. Успешный старт живёт по тем же правилам ────────────────────────────
  ОТВЕТ = {outcome: 'started', job_id: 41};
  await нарисовать();
  document.querySelector('#stage-go').click();
  await tick(200);
  out.успех_после_нажатия = outcomeText();
  СЕЙЧАС += 1200;
  await нарисовать();
  out.успех_через_1_2с = outcomeText();
  СЕЙЧАС += 60000;
  await нарисовать();
  out.успех_через_минуту = outcomeText();

  } catch (e) { out.errs.push('сценарий: ' + (e && e.message || e)); }
  finish();
})();
"""


def main() -> int:
    print("Проверка: отказ на «Начать» не переживает свой повод")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=8000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2

    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nОтказ")
    check("виден сразу после нажатия",
          res.get("после_нажатия"), "Уже идёт — вторая такая же задача не ставится")
    check("переживает перерисовку через 1,2 с",
          res.get("через_1_2с"), "Уже идёт — вторая такая же задача не ставится")
    check("через минуту его нет", res.get("через_минуту"), "")
    check("и он не воскресает", res.get("ещё_позже"), "")

    print("\nУспешный старт — те же правила")
    check("виден сразу", res.get("успех_после_нажатия"), "Поставлено: задача #41")
    check("переживает перерисовку", res.get("успех_через_1_2с"), "Поставлено: задача #41")
    check("через минуту его нет", res.get("успех_через_минуту"), "")

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
