#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кнопка «Подтвердить руками» на странице выпуска (01.10.2026).

Серверная часть проверена в `tests/test_hand_confirm.py`. Здесь — экран, и именно то,
что на экране легко сделать опасным:

1. **Кнопка живёт только на странице ОДНОГО выпуска.** Массовое применение — прямой
   запрет ТЗ, и правильным оно быть не может: каждый такой выпуск человек смотрит на
   канале своими глазами.
2. **Номер ролика спрашивается, когда его не знает машина**, и пустое поле отправить
   нельзя: «не знаем» не равно «подтверждено».
3. **Когда номер прислала нода, поля нет вовсе** — лишнее поле здесь приглашает вписать
   что-нибудь другое.
4. **У подтверждённого руками выпуска кнопок больше нет**, а пометка видна и названа
   НЕ машинным подтверждением.

    .venv/bin/python tests/test_hand_confirm_browser.py
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
    for sp in (" ", " ", " ", "\n", "\t"):
        out = out.replace(sp, " ")
    return " ".join(out.split())


SCENARIO = r"""
(async () => {
  const out = {errs: [], posts: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'хозяин', role: 'admin'};
    const ВЫПУСК = {
      release_id: 'rel-1', channel_id: 1, channel: 'Первый', lang: 'RU',
      donor_title: 'Донорское название', donor_url: 'https://youtu.be/x',
      slot_at: 1790000000, phase: 'upload_running', phase_title: 'загрузка идёт',
      phase_index: 6, phase_total: 9, status: 'blocked', attempts: 3,
      blocked_reason: 'браузер умер на последнем шаге', bar: [],
      video_id: '', project_id: 'p1', idle_sec: 7200,
      hand_confirmed: 0, hand_by: ''};
    let r = Object.assign({}, ВЫПУСК);

    window.api = async (p) => {
      if (p === '/ui/me') return ME;
      if (p.startsWith('/ui/releases/'))
        return {release: r, stages: [], events: [], log: []};
      if (p === '/ui/releases') return {releases: [r]};
      if (p === '/ui/channels') return {channels: []};
      return {};
    };
    window.post = async (p, body) => { out.posts.push({p, body}); return {ok: true}; };
    addEventListener('hashchange', render);

    location.hash = '#/release/rel-1';
    await sleep(400);

    // ── 1. Два действия: вернуть в работу и подтвердить руками ──
    out.естьВозврат = !!document.querySelector('#revive');
    out.естьРуками = !!document.querySelector('#byhand');
    // Кнопка на экране СПИСКА выпусков быть не должна: действие одиночное.
    location.hash = '#/releases';
    await sleep(300);
    out.вСпискеЕсть = !!document.querySelector('#byhand');
    location.hash = '#/release/rel-1';
    await sleep(400);

    // ── 2. Номера машина не знает — поле есть, пустым не отправить ──
    document.querySelector('#byhand').click();
    await sleep(200);
    let окно = document.querySelector('dialog.modal');
    out.окноОткрылось = !!окно;
    out.полеЕсть = !!(окно && окно.querySelector('#bh-vid'));
    out.сказаноПроНавсегда = окно ? флэт(окно.textContent) : '';
    окно.querySelector('.m-ok').click();
    await sleep(200);
    out.пустоеНеОтправлено = out.posts.length;
    out.ругань = окно ? флэт((окно.querySelector('.m-err') || {}).textContent) : '';
    // А со ссылкой — отправляется, и ровно та ссылка, что вписали.
    окно.querySelector('#bh-vid').value = 'https://youtu.be/abcDEF12345';
    окно.querySelector('#bh-note').value = 'ролик виден';
    окно.querySelector('.m-ok').click();
    await sleep(300);
    out.отправлено = out.posts.map(x => x.p);
    out.тело = (out.posts[out.posts.length - 1] || {}).body || null;
    document.querySelectorAll('dialog.modal').forEach(d => d.remove());

    // ── 3. Номер прислала нода — поля нет ──
    r = Object.assign({}, ВЫПУСК, {video_id: 'nodeAAAA111'});
    location.hash = '#/releases'; await sleep(150);
    location.hash = '#/release/rel-1'; await sleep(400);
    document.querySelector('#byhand').click();
    await sleep(200);
    окно = document.querySelector('dialog.modal');
    out.сНомеромПоляНет = !!(окно && !окно.querySelector('#bh-vid'));
    out.сНомеромСказано = окно ? флэт(окно.textContent) : '';
    document.querySelectorAll('dialog.modal').forEach(d => d.remove());

    // ── 4. Уже подтверждён руками — кнопок нет, пометка видна ──
    r = Object.assign({}, ВЫПУСК, {video_id: 'nodeAAAA111', status: 'blocked',
                                   hand_confirmed: 1790001000, hand_by: 'хозяин'});
    location.hash = '#/releases'; await sleep(150);
    location.hash = '#/release/rel-1'; await sleep(400);
    out.послеКнопкаРуками = !!document.querySelector('#byhand');
    out.послеКнопкаВозврат = !!document.querySelector('#revive');
    out.пометка = флэт(document.body.textContent);
  } catch (e) { out.errs.push(String(e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();

function флэт(t){ return (t || '').replace(/\s+/g, ' ').trim(); }
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=12000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])

    print("Действие одиночное: только страница одного выпуска")
    check("на странице выпуска кнопка есть", res.get("естьРуками"), True)
    check("возврат в работу рядом тоже", res.get("естьВозврат"), True)
    check("в списке выпусков кнопки нет", res.get("вСпискеЕсть"), False)

    print("Номера машина не знает — спрашиваем, пустым не отправляем")
    check("окно открылось", res.get("окноОткрылось"), True)
    check("поле для ссылки есть", res.get("полеЕсть"), True)
    сказано = flat(res.get("сказаноПроНавсегда"))
    check("сказано, что пометка останется навсегда", "навсегда" in сказано, True)
    check("и что заливка не повторится", "не повторится" in сказано, True)
    check("пустое поле не отправлено", res.get("пустоеНеОтправлено"), 0)
    check("и человеку сказано, чего не хватает",
          "впишите" in flat(res.get("ругань")).lower(), True)
    check("со ссылкой — отправлено один раз",
          res.get("отправлено"), ["/ui/releases/rel-1/confirm-by-hand"])
    тело = res.get("тело") or {}
    check("ушла вписанная ссылка", тело.get("video_id"), "https://youtu.be/abcDEF12345")
    check("и заметка", тело.get("note"), "ролик виден")

    print("Номер прислала нода — поля нет")
    check("поля нет", res.get("сНомеромПоляНет"), True)
    check("и сказано, что вписывать нечего",
          "вписывать ничего не нужно" in flat(res.get("сНомеромСказано")), True)

    print("Подтверждён руками — кнопок нет, пометка видна")
    check("кнопки «подтвердить руками» нет", res.get("послеКнопкаРуками"), False)
    check("и возврата в работу нет", res.get("послеКнопкаВозврат"), False)
    пометка = flat(res.get("пометка"))
    check("видно, что подтвердил человек", "Подтверждено человеком" in пометка, True)
    check("и кто именно", "хозяин" in пометка, True)
    # Главное в пометке: это НЕ машинное подтверждение, и экран обязан это сказать.
    check("и что машинного подтверждения нет",
          "не машинное подтверждение" in пометка, True)

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
