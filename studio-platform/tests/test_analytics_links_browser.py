#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Каналы» → «Подключить статистику»: ссылки по профилям AdsPower (12.10.2026).

Андрей: подключение статистики — на уровне всех каналов, на главной странице, удобно и
лаконично. Проверяется: кнопка в шапке раскрывает карточку; строка на профиль, только
профили, где есть кого подключать; подключённые каналы отмечены ✓ и в ссылку не идут;
«Ссылка» просит у сервера ссылку ровно на неподключённые каналы профиля и отдаёт её
человеку (в буфер или полем); повторное нажатие кнопки в шапке карточку закрывает.
На вкладке статистики канала — «Скопировать ссылку для профиля» на этот канал.

    .venv/bin/python tests/test_analytics_links_browser.py
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
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ME = {name: 'boss', role: 'admin'};
    const URL_ = 'https://vovastudio.work/studio/oauth/go/abc';
    const GROUPS = {total: 5, connected: 2, client: true, groups: [
      {profile_id: 1, serial: '13', title: '', host: 'minsk', channels: [
        {id: 11, name: 'Noční Portréty', connected: false, dead: false},
        {id: 12, name: 'Tajný Archiv', connected: true, dead: false},
        {id: 13, name: 'Mrazivé Archivy', connected: false, dead: true}]},
      {profile_id: 2, serial: '23', title: '', host: 'minsk', channels: [
        {id: 21, name: 'Biografii de Noapte', connected: true, dead: false}]},
      {profile_id: 0, serial: '', title: '', host: '', channels: [
        {id: 31, name: 'Без профиля', connected: false, dead: false}]}]};
    const sent = [];
    window.api = async (p) => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: []};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p === '/ui/analytics/groups') return GROUPS;
      if (p.startsWith('/ui/channels/7')) return {channel: {id: 7, name: 'K', status: 'active'},
        plan: [], releases: [], stats: {}, analytics: {access: {connected: false, client: true}}};
      return {};
    };
    window.post = async (p, body) => { sent.push({p, body}); return {url: URL_, channels: 2}; };
    addEventListener('hashchange', render);
    location.hash = '#/channels'; await sleep(300);

    const btn = document.getElementById('stlinks');
    out.кнопка = btn ? btn.textContent.trim() : '';
    btn.click(); await sleep(250);
    const card = document.getElementById('stlinks-card');
    out.карточка = card ? card.textContent.replace(/\s+/g, ' ') : '';
    out.строк = card ? card.querySelectorAll('.strow').length : 0;
    out.кнопокСсылки = card ? [...card.querySelectorAll('button[data-ids]')].map(b => b.textContent.trim()) : [];
    const b = card && card.querySelector('button[data-ids]');
    if (b){ b.click(); await sleep(1400); }
    out.запрос = sent[0] || null;
    const box = card && card.querySelector('.linkbox');
    out.отдано = (b && b.textContent.includes('Скопировано')) || !!(box && box.value === URL_);
    btn.click(); await sleep(150);
    out.закрыта = !document.getElementById('stlinks-card');

    location.hash = '#/channel/7/statistika'; await sleep(400);
    const gl = document.getElementById('glink');
    out.кнопкаКанала = gl ? gl.textContent.trim() : '';
    if (gl){ gl.click(); await sleep(300); }
    out.запросКанала = sent[1] || null;
  } catch (e) { out.errs.push(String(e && e.message || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=9000)
    if res.get("error"):
        print("ПРОПУЩЕНО:", res["error"])
        return 0
    check("ошибок на странице нет", res.get("errs"), [])
    check("кнопка в шапке", res.get("кнопка"), "Подключить статистику")
    card = res.get("карточка") or ""
    check("счёт подключённых", "подключено 2 из 5" in card, True)
    check("строки только где есть кого подключать (№13 и без профиля)", res.get("строк"), 2)
    check("подключённый отмечен ✓", "✓ Tajný Archiv" in card, True)
    check("полностью подключённый профиль — одной строкой внизу", "Полностью подключены: 1 профиль" in card, True)
    check("без профиля — просьба вписать, без ссылки", "впишите профиль" in card, True)
    check("ссылка — на два неподключённых канала (умерший тоже)", res.get("кнопокСсылки"), ["Ссылка · 2"])
    check("запрос на сервер", res.get("запрос"),
          {"p": "/ui/analytics/link", "body": {"channels": [11, 13]}})
    check("ссылка отдана человеку", res.get("отдано"), True)
    check("повторное нажатие закрывает", res.get("закрыта"), True)
    check("на вкладке канала — кнопка ссылки", res.get("кнопкаКанала"), "Скопировать ссылку для профиля")
    check("и ссылка на этот канал", res.get("запросКанала"),
          {"p": "/ui/analytics/link", "body": {"channels": [7]}})
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
