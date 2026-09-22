#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вкладка «Обложка» сама подставляет ссылку, запомненную на вкладке «Сценарий».

Просьба владельца 21.09.2026: «на странице с обложкой нужно предлагать автоматически
подставлять ссылку, которая запомнилась в тексте». Ссылка донора у проекта одна, и
вставлять её второй раз на соседней вкладке — лишняя работа и лишний шанс ошибиться.

Что закрепляется:

* в режиме «по ссылке» пустое поле получает запомненную ссылку, и под полем сказано,
  откуда она взялась, — подстановка не должна выглядеть как чужая правка;
* запуск по-прежнему за кнопкой: открытие вкладки ничего не заказывает;
* стёртую человеком ссылку обратно не возвращаем, в том числе при перерисовке;
* свою ссылку, набранную в поле, не затираем;
* запомнилась НОВАЯ ссылка — её снова предлагают;
* у выпуска канала своя ссылка, а своей у проекта нет — подставляется она, с
  подписью именно про выпуск.

    .venv/bin/python tests/test_cover_link_prefill_browser.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []
LINK = "https://youtu.be/Ng0hmsrr7l4"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


SCENARIO = r"""
(async () => {
  const out = {errs: [], posts: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const pause = ms => new Promise(r => setTimeout(r, ms));
  const q = sel => document.querySelector(sel);
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {
  const ССЫЛКА = 'https://youtu.be/Ng0hmsrr7l4';
  const COVERS = {modes: [{key: 'url', title: 'По ссылке', note: ''},
                          {key: 'title', title: 'По заголовку', note: ''}],
    presets: [], options: [{label: 'Модель', provider: 'x', model: 'y'}],
    languages: ['Русский'], aspect: '16:9', jobs: [],
    rules: [{when: 'edit', kills: ['стиль'], why: 'так'},
            {when: 'not_url', why: 'без ссылки'}]};
  window.api = async p => p.startsWith('/ui/covers') ? COVERS : {};
  window.post = async (p, b) => { out.posts.push({p, b}); return {ok: true, job: {}}; };
  const D = origin => ({project: {id: 'p1', name: 'Проба'}, files: [], jobs: [],
                        origin});
  const открыть = async origin => {
    app.innerHTML = '';
    await renderCoverTab(D(origin));
    await pause(200);
  };
  const видно = sel => { const el = q(sel); return !!el && !el.hidden; };
  const набрать = v => { const t = q('#cv-urls'); t.value = v;
                         t.dispatchEvent(new Event('input')); };

  // 1. Запомненная ссылка подставлена в пустое поле, и сказано откуда.
  await открыть({kind: 'link', donor_url: ССЫЛКА, video_id: 'Ng0hmsrr7l4'});
  out.подставлена = q('#cv-urls').value;
  out.подпись_видна = видно('#cv-urls-from');
  out.подпись = q('#cv-urls-from').textContent.trim();
  out.ничего_не_заказано = out.posts.length;

  // По кнопке уходит именно она.
  q('#cv-go').click();
  await pause(300);
  out.ушло = (out.posts.find(x => x.p === '/ui/covers') || {}).b;
  out.ушло = out.ушло ? out.ушло.urls : null;

  // 2. Стёрли — не возвращаем, даже при перерисовке вкладки.
  набрать('');
  out.стёрли_подпись_ушла = !видно('#cv-urls-from');
  await открыть({kind: 'link', donor_url: ССЫЛКА, video_id: 'Ng0hmsrr7l4'});
  out.стёртая_не_вернулась = q('#cv-urls').value;

  // 3. Своя ссылка в поле — не затираем её запомненной.
  набрать('https://youtu.be/своя');
  await открыть({kind: 'link', donor_url: 'https://youtu.be/новая', video_id: 'новая'});
  out.своя_цела = q('#cv-urls').value;

  // 4. Поле пустое, а запомнилась НОВАЯ ссылка — её снова предлагают.
  набрать('');
  await открыть({kind: 'link', donor_url: 'https://youtu.be/новая', video_id: 'новая'});
  out.новая_предложена = q('#cv-urls').value;

  // 5. Своей ссылки у проекта нет, есть ссылка выпуска канала. Сервер в этом случае
  // кладёт её же в donor_url и помечает from: 'release' (`_project_origin`).
  delete COVER_STATE.p1;
  await открыть({kind: 'link', donor_url: 'https://youtu.be/выпуск', from: 'release',
                 release_url: ''});
  out.выпуск = q('#cv-urls').value;
  out.выпуск_подпись = q('#cv-urls-from').textContent.trim();

  // 5б. Подставленное и не тронутое следует за «Сценарием»: ссылку сменили там —
  // меняется и здесь; забыли — поле пустеет.
  delete COVER_STATE.p1;
  await открыть({kind: 'link', donor_url: ССЫЛКА, from: 'project'});
  await открыть({kind: 'link', donor_url: 'https://youtu.be/сменили', from: 'project'});
  out.сменили = q('#cv-urls').value;
  out.сменили_подпись_видна = видно('#cv-urls-from');
  await открыть({kind: 'text', donor_url: '', from: 'project'});
  out.забыли = q('#cv-urls').value;

  // 6. Никакой ссылки — поле пустое, подписи нет.
  delete COVER_STATE.p1;
  await открыть({kind: '', donor_url: ''});
  out.пусто = q('#cv-urls').value;
  out.пусто_без_подписи = !видно('#cv-urls-from');
  } catch (e) { out.errs.push('сценарий: ' + (e && e.stack || e)); }
  finish();
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок в браузере нет", res.get("errs"), [])

    print("\nЗапомненная ссылка подставляется сама")
    check("в пустое поле — ссылка со «Сценария»", res.get("подставлена"), LINK)
    check("под полем сказано, откуда она", res.get("подпись_видна"), True)
    check("словами", res.get("подпись"),
          "подставлена ссылка, запомненная на вкладке «Сценарий»")
    check("открытие вкладки ничего не заказывает", res.get("ничего_не_заказано"), 0)
    check("по кнопке уходит именно она", res.get("ушло"), [LINK])

    print("\nВоля человека сильнее подстановки")
    check("стёрли — подпись убрана", res.get("стёрли_подпись_ушла"), True)
    check("стёртую ссылку перерисовка не возвращает", res.get("стёртая_не_вернулась"), "")
    check("свою ссылку в поле не затираем", res.get("своя_цела"), "https://youtu.be/своя")
    check("запомнилась новая — её снова предлагают", res.get("новая_предложена"),
          "https://youtu.be/новая")

    print("\nПодставленное следует за «Сценарием», пока его не трогали")
    check("ссылку сменили там — сменилась и здесь", res.get("сменили"),
          "https://youtu.be/сменили")
    check("с подписью, откуда она", res.get("сменили_подпись_видна"), True)
    check("ссылку забыли — поле опустело, старый донор не остался", res.get("забыли"), "")

    print("\nСсылка выпуска и её отсутствие")
    check("нет своей — подставлена ссылка выпуска", res.get("выпуск"),
          "https://youtu.be/выпуск")
    check("с подписью про выпуск", res.get("выпуск_подпись"),
          "подставлена ссылка выпуска канала")
    check("ссылки нет — поле пустое", res.get("пусто"), "")
    check("и без подписи", res.get("пусто_без_подписи"), True)

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
