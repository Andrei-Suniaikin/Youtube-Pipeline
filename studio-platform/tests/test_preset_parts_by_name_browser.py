#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Целый пресет: сценарий и обложку он выбирает по названию, а не «частичным пресетом».

С 22.09.2026 (одна студия) пресеты текста и обложки — части хранилища текстов и
обложек; «Только сценарий» / «Только обложка» заводят именно их. Частичный пресет
производства для этих частей больше не создаётся, поэтому переключатель «Свои поля /
Из частичного пресета» у групп «Сценарий» и «Обложка» звал бы «сначала завести один» —
которого заведение уже не делает (ревью 22.09.2026). Группа выбирает пресет по
названию своим полем. Старую ссылку, если она есть, видно — чтобы её можно было снять.

    .venv/bin/python tests/test_preset_parts_by_name_browser.py
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
  addEventListener('error', e => out.errs.push(e.message));
  const pause = ms => new Promise(r => setTimeout(r, ms));
  try {
  const grp = (title, part, key, link) => ({title, part, fields: [{key, label: title,
    kind: 'text', value: ''}], source: link ? 'link' : 'own', readonly: false,
    link_id: link || null, link_name: link ? 'старый' : null, link_problem: '',
    link_options: []});
  let GROUPS = [];
  window.api = async p => p === '/ui/presets/vsgen/5'
    ? {id: 5, name: 'Целый', scope: 'full', links: {}, link_problems: [], note: '',
       users: {channels: [], projects: []}, groups: GROUPS} : {};
  window.post = async () => ({ok: true});
  const открыть = async (tab) => {
    history.replaceState(null, '', '#/preset/5/' + tab);
    await viewPreset('5');
    await pause(100);
    return !!document.querySelector('[data-linkmodes]');
  };
  GROUPS = [grp('Картинки', 'images', 'style'), grp('Обложка', 'cover', 'preset_cover'),
            grp('Сценарий', 'text', 'preset_rewrite')];
  out.картинки = await открыть('kartinki');
  out.обложка = await открыть('oblozhka');
  out.сценарий = await открыть('stsenariy');
  GROUPS = [grp('Картинки', 'images', 'style'), grp('Обложка', 'cover', 'preset_cover', 9)];
  out.обложка_со_старой_ссылкой = await открыть('oblozhka');
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=6000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return 2
    check("ошибок нет", res.get("errs"), [])
    check("у картинок «Из частичного пресета» есть", res.get("картинки"), True)
    check("у обложки — нет: пресет выбирается по названию", res.get("обложка"), False)
    check("у сценария — тоже нет", res.get("сценарий"), False)
    check("старая ссылка обложки видна — чтобы её можно было снять",
          res.get("обложка_со_старой_ссылкой"), True)
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
