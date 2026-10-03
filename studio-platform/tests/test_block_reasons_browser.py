#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт показывает причину остановки словами: чья беда и что делать (03.10.2026).

Владелец: «нужно чётко показывать, что не выбран пресет обложек» и «нужно чётко
показывать, что проблема в доноре, чтобы человек понимал». Перевод делает сервер
(`block_reasons.explain` → поле `block`), экран обязан его показать — в списке выпусков,
в заголовке группы, на карточке выпуска и на плитке канала, — и не предлагать
«Вернуть в работу» там, где повтор даст тот же отказ.

    .venv/bin/python tests/test_block_reasons_browser.py
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
  const out = {errs: [], posts: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  try {
    ME = {name: 'хозяин', role: 'admin'};
    const ПРЕСЕТ = {kind: 'channel', tag: 'Настройка канала',
      title: 'у канала не выбран пресет обложек', what: 'Ролик собран, стоит на обложке.',
      fix: 'Выберите пресет обложки в настройках канала.', revive: true, short: ''};
    const ДОНОР = {kind: 'donor', tag: 'Проблема в доноре',
      title: 'ролик удалён или закрыт автором', what: 'Это не сбой студии.',
      fix: 'Уберите ссылку и поставьте другой ролик-донор.', revive: false, short: ''};
    const base = {lang: 'PL', donor_url: 'https://youtu.be/dead', slot_at: 1790000000,
      phase: 'media_ready', phase_title: 'ролик готов', phase_index: 3, phase_total: 9,
      status: 'blocked', attempts: 2, bar: [], video_id: '', project_id: 'p',
      idle_sec: 60, hand_confirmed: 0, hand_by: '', plan_item_id: 0};
    const rels = [
      {...base, release_id: 'c1', channel_id: 7, channel: 'Nocne Biografie', donor_title: 'Обложка 1',
       blocked_reason: 'у канала не выбран пресет обложек', block: ПРЕСЕТ, plan_item_id: 11},
      {...base, release_id: 'c2', channel_id: 8, channel: 'Mroczne Akta', donor_title: 'Обложка 2',
       blocked_reason: 'у канала не выбран пресет обложек', block: ПРЕСЕТ, plan_item_id: 12},
      {...base, release_id: 'd1', channel_id: 9, channel: 'Тёмные Тайны', donor_title: 'Донор',
       phase: 'created', phase_title: 'заведён', phase_index: 0,
       blocked_reason: 'у ролика нет субтитров (VideoUnplayable)', block: ДОНОР, plan_item_id: 13},
      {...base, release_id: 'u1', channel_id: 9, channel: 'Тёмные Тайны', donor_title: 'Чужое',
       phase: 'upload_running', blocked_reason: 'зависло на шаге «upload_running»',
       block: {}, plan_item_id: 14},
      // Идёт, но канал не настроен: пресета текста нет — сценарий на умолчаниях.
      {...base, release_id: 'g1', channel_id: 10, channel: 'Tajemství prvků', donor_title: 'СВИНЕЦ',
       status: 'running', phase: 'created', phase_title: 'заведён', phase_index: 0,
       blocked_reason: '', block: {}, plan_item_id: 15,
       gaps: [{kind: 'channel', tag: 'Настройка канала', level: 'warn',
               title: 'у канала не выбран пресет текста',
               what: 'Сценарий пишется на умолчаниях рерайта: без имени ведущего.',
               fix: 'Выберите пресет текста в настройках канала.'}]},
    ];
    window.api = async (p) => {
      if (p === '/ui/me') return ME;
      if (p.startsWith('/ui/releases/')) {
        const id = decodeURIComponent(p.split('/').pop());
        return {release: rels.find(r => r.release_id === id), stages: [], events: [], log: []};
      }
      if (p.startsWith('/ui/releases')) return {releases: rels, phases: []};
      if (p === '/ui/channels') return {channels: [
        {id: 7, name: 'Nocne Biografie', handle: '@nb', lang: 'PL', status: 'active', niche: '',
         uc: 'UC1', tags: [], tz: 'Europe/Warsaw', horizon_days: 14, lead_days: 3, per_day: 7,
         has_profile: true, has_uc: true, has_cover_preset: false, has_text_preset: false,
         supply: {known: true, items: 5, days: 5, covered: true}},
        {id: 8, name: 'Mroczne Akta', handle: '@ma', lang: 'PL', status: 'active', niche: '',
         uc: 'UC2', tags: [], tz: 'Europe/Warsaw', horizon_days: 14, lead_days: 3, per_day: 7,
         has_profile: true, has_uc: true, has_cover_preset: true, has_text_preset: true,
         supply: {known: true, items: 5, days: 5, covered: true}}]};
      return {};
    };
    window.post = async (p) => { out.posts.push(p); return {ok: true}; };
    addEventListener('hashchange', render);

    // ── список выпусков ──
    location.hash = '#/releases';
    await sleep(400);
    const heads = [...document.querySelectorAll('.grpcard .grpbar')].map(txt);
    out.заголовки = heads;
    const grpCh = [...document.querySelectorAll('.grpcard')].find(el => /Настройка канала/.test(txt(el.querySelector('.grpbar'))));
    out.ссылкиКаналов = grpCh ? [...grpCh.querySelectorAll('.grpbar a')].map(a => a.getAttribute('href')) : [];
    out.классКарточки = grpCh ? grpCh.className : '';
    out.строкиКарточки = grpCh ? [...grpCh.querySelectorAll('tr.row')].map(tr => tr.dataset.id) : [];
    out.остальные = [...document.querySelectorAll('#rel-rest tr.row')].map(tr => tr.dataset.id);
    out.подписьОстальных = txt(document.querySelector('#rel-rest .restcap'));
    const rowD = document.querySelector('tr.row[data-id="d1"]');
    out.меткаДонора = txt(rowD && rowD.querySelector('.blktag.donor'));
    out.строкаДонора = txt(rowD && rowD.querySelector('.note'));
    const rowU = document.querySelector('tr.row[data-id="u1"]');
    out.чужаяКрасная = txt(rowU && rowU.querySelector('.note.bad'));
    const rowG = document.querySelector('tr.row[data-id="g1"]');
    out.строкаНедосмотра = txt(rowG && rowG.querySelector('.note'));

    // ── карточка: идёт, но канал не настроен; «назад» — в список выпусков ──
    location.hash = '#/release/g1';
    await sleep(400);
    const gap = document.querySelector('.blk.gap');
    out.недосмотр = txt(gap);
    out.недосмотрКуда = gap && gap.querySelector('a') ? gap.querySelector('a').getAttribute('href') : '';
    out.недосмотрБлокаОстановки = !!document.querySelector('#blk');
    const back = document.querySelector('a.back');
    out.назадИзСписка = back ? [back.getAttribute('href'), txt(back)] : [];
    // Пришли из проектов — туда же и назад (страница выпуска сама CAME_FROM не трогает).
    CAME_FROM = '#/projects';
    location.hash = '#/release/c1';
    await sleep(400);
    const back2 = document.querySelector('a.back');
    out.назадИзПроектов = back2 ? [back2.getAttribute('href'), txt(back2)] : [];
    CAME_FROM = '';
    location.hash = '#/release/d1';
    await sleep(400);
    const back3 = document.querySelector('a.back');
    out.назадПрямо = back3 ? [back3.getAttribute('href'), txt(back3)] : [];

    // ── карточка: донор ──
    location.hash = '#/release/d1';
    await sleep(400);
    const blkD = document.querySelector('#blk');
    out.донорКласс = blkD ? blkD.className : '';
    out.донорТекст = txt(blkD);
    out.донорВернуть = !!document.querySelector('#revive');
    out.донорРуками = !!document.querySelector('#byhand');
    out.донорУбрать = !!document.querySelector('#blk-drop');
    out.донорКуда = (document.querySelector('#blk-fix') || {}).getAttribute
      ? document.querySelector('#blk-fix').getAttribute('href') : '';
    out.донорСсылка = [...(blkD ? blkD.querySelectorAll('a[target=_blank]') : [])]
      .map(a => a.getAttribute('href'));
    // «Убрать ссылку» — через вопрос, тем же /ui/plan/{id}/cancel, и уводит в канал
    document.querySelector('#blk-drop').click();
    await sleep(100);
    const ok = document.querySelector('.ask-ok');
    out.донорСпросил = !!ok;
    if (ok) ok.click();
    await sleep(300);
    out.послеУбрать = location.hash;

    // ── карточка: пресет обложек ──
    location.hash = '#/release/c1';
    await sleep(400);
    const blkC = document.querySelector('#blk');
    out.каналКласс = blkC ? blkC.className : '';
    out.каналТекст = txt(blkC);
    out.каналКуда = document.querySelector('#blk-fix') ? document.querySelector('#blk-fix').getAttribute('href') : '';
    out.каналВернуть = !!document.querySelector('#revive');
    out.каналРуками = !!document.querySelector('#byhand');

    // ── карточка: незнакомая причина — как раньше ──
    location.hash = '#/release/u1';
    await sleep(400);
    out.чужойБлок = !!document.querySelector('#blk');
    out.чужойВернуть = !!document.querySelector('#revive');
    out.чужойРуками = !!document.querySelector('#byhand');
    out.чужойТекст = [...document.querySelectorAll('.sec')].map(txt).find(t => /Остановлен/.test(t)) || '';

    // ── плитки каналов ──
    location.hash = '#/channels';
    await sleep(500);
    const tile = id => document.querySelector(`.chcard[data-id="${id}"]`);
    out.плиткаБез = txt(tile(7));
    out.плиткаС = txt(tile(8));
  } catch (e) { out.errs.push('scenario: ' + (e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=15000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])

    print("Список выпусков: причина словами, с меткой")
    heads = res.get("заголовки") or []
    check("одинаковая причина собрана в группу «Настройка канала»",
          any("Настройка канала: у канала не выбран пресет обложек" in h for h in heads), True)
    check("в группе названы каналы, ссылками в их настройки",
          res.get("ссылкиКаналов"), ["#/channel/7/nastroyki", "#/channel/8/nastroyki"])
    check("группа — отдельная карточка с кромкой «настройка канала»",
          "grpcard" in (res.get("классКарточки") or "") and "channel" in res.get("классКарточки"),
          True)
    check("в карточке ровно её выпуски", res.get("строкиКарточки"), ["c1", "c2"])
    check("остальные — в своей карточке", res.get("остальные"), ["d1", "u1", "g1"])
    check("и она подписана", res.get("подписьОстальных"), "Остальные выпуски — 3")
    check("недосмотр канала виден в строке выпуска",
          res.get("строкаНедосмотра"), "Настройка канала у канала не выбран пресет текста")
    check("у донора метка «Проблема в доноре»", res.get("меткаДонора"), "Проблема в доноре")
    check("и смысл вместо имени исключения",
          res.get("строкаДонора"), "Проблема в доноре ролик удалён или закрыт автором")
    check("незнакомая причина — красной строкой, как раньше",
          res.get("чужаяКрасная"), "зависло на шаге «upload_running»")

    print("\nКарточка выпуска: донор")
    check("блок помечен как донор", "donor" in (res.get("донорКласс") or ""), True)
    t = res.get("донорТекст") or ""
    check("метка и смысл", "Проблема в доноре ролик удалён или закрыт автором" in t, True)
    check("что делать — словами", "Что делать: Уберите ссылку" in t, True)
    check("запись движка видна мелко, для разбора", "VideoUnplayable" in t, True)
    check("«Вернуть в работу» НЕ предлагается — повтор даст тот же отказ",
          res.get("донорВернуть"), False)
    check("«Подтвердить руками» тоже — заливки не было", res.get("донорРуками"), False)
    check("есть «Убрать ссылку из плана»", res.get("донорУбрать"), True)
    check("и дорога поставить другой ролик на канал", res.get("донорКуда"), "#/channel/9")
    check("и сам донор — посмотреть глазами", res.get("донорСсылка"), ["https://youtu.be/dead"])
    check("убрать — через вопрос", res.get("донорСпросил"), True)
    check("тем же /ui/plan/{id}/cancel, что и календарь",
          res.get("posts"), ["/ui/plan/13/cancel"])
    check("и после — в канал, ставить другой ролик", res.get("послеУбрать"), "#/channel/9")

    print("\nКарточка выпуска: пресет обложек")
    check("блок помечен как настройка канала", "channel" in (res.get("каналКласс") or ""), True)
    t = res.get("каналТекст") or ""
    check("метка и смысл", "Настройка канала у канала не выбран пресет обложек" in t, True)
    check("ссылка ведёт в настройки ЭТОГО канала", res.get("каналКуда"), "#/channel/7/nastroyki")
    check("«Вернуть в работу» есть — после выбора пресета он поможет",
          res.get("каналВернуть"), True)
    check("«Подтвердить руками» нет", res.get("каналРуками"), False)

    print("\nКарточка выпуска: идёт, но канал не настроен")
    t = res.get("недосмотр") or ""
    check("сказано, чья недоработка", "Настройка канала у канала не выбран пресет текста" in t, True)
    check("и что это предупреждение, выпуск идёт", "выпуск идёт, это предупреждение" in t, True)
    check("и что делать", "Что делать: Выберите пресет текста" in t, True)
    check("ссылка — в настройки этого канала", res.get("недосмотрКуда"), "#/channel/10/nastroyki")
    check("блока остановки нет — выпуск не остановлен", res.get("недосмотрБлокаОстановки"), False)

    print("\n«Назад» — туда, откуда пришёл")
    check("из списка выпусков — в список выпусков",
          res.get("назадИзСписка"), ["#/releases", "← ко всем выпускам"])
    check("из проектов — в проекты", res.get("назадИзПроектов"), ["#/projects", "← ко всем проектам"])
    check("открыли выпуск напрямую — в проекты (вкладка выпуска — «Проекты»)",
          res.get("назадПрямо"), ["#/projects", "← ко всем проектам"])

    print("\nКарточка выпуска: незнакомая причина — как было")
    check("нового блока нет", res.get("чужойБлок"), False)
    check("обе прежние кнопки на месте",
          (res.get("чужойВернуть"), res.get("чужойРуками")), (True, True))
    check("текст движка как есть", "зависло на шаге" in (res.get("чужойТекст") or ""), True)

    print("\nПлитки каналов")
    check("без пресета обложек — плашка", "нет пресета обложек" in (res.get("плиткаБез") or ""), True)
    check("с пресетом — плашки нет", "нет пресета обложек" in (res.get("плиткаС") or ""), False)
    check("без пресета текста — своя плашка", "нет пресета текста" in (res.get("плиткаБез") or ""), True)
    check("с ним — нет", "нет пресета текста" in (res.get("плиткаС") or ""), False)

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
