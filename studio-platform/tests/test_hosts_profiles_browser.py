#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Машины заливки и профили AdsPower на экране «Каналы» (Фаза 4, task-spec.md §8).

HTTP-контракт §6.4 зафиксирован в спецификации и пишется параллельно (Фаза 3) —
здесь `api()` подменяется целиком, реальный сервер не нужен. Проверяется:

  • карточка «Машины заливки»: список (включая `disabled`), добавление, правка,
    статус («Убрать»/«Включить» не удаляет строку), «Обновить список профилей» —
    успех тостом со сводкой, неудача — честной ошибкой, список не пустеет;
  • карточка «Профили AdsPower»: занятость тремя состояниями («занят» / «свободен» /
    «не знаем», причём «не знаем» — это НЕ «свободен»: человек, увидевший «свободен»,
    откроет профиль руками и порвёт куки идущей заливке), метка «на машине больше
    нет», машина-владелец у каждой строки; «Обновить все» — только по активным машинам,
    строго по одной одновременно (не параллельно) и без остановки на ошибке одной;
  • честная ошибка вместо пустого списка, если сам `GET /ui/hosts`/`GET /ui/profiles`
    не отвечает;
  • главный сценарий — ловушка index.html:6318 → 6396 → pub_ui.py:207: карточка
    канала с пропавшим текущим профилем показывает его (с пометкой) и НЕ стирает
    `profile_id` при сохранении, потому что форма запрашивает `channel_id` в
    `GET /ui/channels/new` (§6.4, п.3).

Настоящий `web/index.html` в headless Chrome, `api()` подменён — ни стенда, ни сети.

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_hosts_profiles_browser.py
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
  const out = {errs: [], sent: []};
  addEventListener('error', e => out.errs.push(String(e.message || e)));
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ── «сервер» в памяти — состояние мутируется теми же запросами, что настоящий ──
  let HOSTS = [
    {id: 1, code: 'minsk', title: 'Минская машина',
     adspower_url: 'http://127.0.0.1:50325', status: 'active', note: '',
     created: 1758585600, profiles_count: 2, last_refresh: 1758600000},
    {id: 2, code: 'gdansk', title: 'Гданьская машина',
     adspower_url: 'http://192.168.1.10:50325', status: 'disabled', note: 'запасная',
     created: 1758585600, profiles_count: 0, last_refresh: null},
  ];
  let nextHostId = 3;
  const PROFILES = [
    {id: 12, serial: '46', title: 'Профиль 46', group_name: 'history-pl', remark: '',
     last_open_time: '2026-09-22 10:11:03', status: 'active', active_now: true,
     busy: 'busy', busy_why: '', missing_since: null, last_seen: 1758600000,
     host: {id: 1, code: 'minsk', title: 'Минская машина'}},
    {id: 99, serial: '12', title: 'Профиль 12', group_name: '', remark: '',
     last_open_time: '2026-09-20 08:00:00', status: 'active', active_now: false,
     busy: 'unknown', busy_why: 'профиль пропал с машины при последней описи',
     missing_since: 1758600500, last_seen: 1758595000,
     host: {id: 1, code: 'minsk', title: 'Минская машина'}},
  ];
  // Канал 17 уже настроен и указывает на профиль 99 — тот самый, который «пропал».
  const CHANNEL_FULL = {
    channel: {
      id: 17, name: 'Channel A', handle: 'chA', uc: 'UCabcdefghijklmno1234',
      lang: 'PL', lang_raw: 'pl', niche: 'история', status: 'active',
      tz: 'Europe/Warsaw', horizon_days: 14, lead_days: 2, deferred: false,
      deferred_limit: 5, schedule: {'*': ['10:00']}, per_week: 7,
      host: 'minsk · Минская машина', profile: '12 · Профиль 12',
      presets: {rewrite: '', vsgen: null, cover: ''}, note: '',
      profile_id: 99, host_id: 1, preset_vsgen_id: null,
      preset_rewrite: '', preset_cover: '',
    },
    calendar: [], counts: {}, uploads: [], by_day: {}, tz: 'Europe/Warsaw',
    channel_preset: {id: null, name: '', inherited: [], own: {}, all: []},
    waiting_without_slot: 0, waiting_items: [], history_days: 14,
  };
  // Список для формы нового канала — БЕЗ пропавшего профиля (§6.4 п.1).
  const REF_PLAIN = () => ({
    profiles: [{id: 12, title: 'Профиль 46', serial: '46', host_code: 'minsk',
                missing: false}],
    hosts: [{id: 1, title: 'Минская машина', code: 'minsk'}],
    presets: {vsgen: [], rewrite: [], cover: []},
    languages: [{value: 'pl', title: 'Польский'}],
    days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], any_day: '*',
    statuses: [{key: 'active', title: 'работает'}, {key: 'paused', title: 'пауза'},
               {key: 'archived', title: 'в архиве'}],
    note: '',
  });
  // Тот же список, но с `channel_id=17` — текущий (пропавший) профиль канала
  // дописан отдельно, с честной пометкой (§6.4 п.3).
  const REF_FOR_17 = () => {
    const r = REF_PLAIN();
    r.profiles = [...r.profiles,
      {id: 99, title: 'Профиль 12', serial: '12', host_code: 'minsk',
       missing: true, current: true}];
    return r;
  };

  let hostsFail = false, profilesFail = false;
  const FAIL_HOST_IDS = new Set();
  let concurrentRefresh = 0, maxConcurrentRefresh = 0;
  const refreshCalls = [];

  window.api = async (p, opts) => {
    const method = (opts && opts.method) || 'GET';
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    if (method !== 'GET') out.sent.push([method, p, body]);

    if (p === '/ui/me') return {name: 'dev', role: 'admin'};
    if (p === '/ui/channels') return {channels: [
      {id: 17, name: 'Channel A', handle: 'chA', lang: 'PL', per_day: 1,
       horizon_days: 14, lead_days: 2, has_profile: true, status: 'active',
       has_uc: true}]};

    if (p === '/ui/hosts' && method === 'GET'){
      if (hostsFail) throw new Error('машины не читаются: сеть недоступна');
      return {hosts: HOSTS.map(h => ({...h}))};
    }
    if (p === '/ui/hosts' && method === 'POST'){
      const id = nextHostId++;
      HOSTS.push({id, code: body.code, title: body.title,
                  adspower_url: body.adspower_url, note: body.note || '',
                  status: 'active', created: 1, profiles_count: 0, last_refresh: null});
      return {ok: true, id, code: body.code};
    }
    let m = p.match(/^\/ui\/hosts\/(\d+)$/);
    if (m && method === 'POST'){
      const h = HOSTS.find(x => x.id === Number(m[1]));
      Object.assign(h, {code: body.code, title: body.title,
                        adspower_url: body.adspower_url, note: body.note || ''});
      return {...h};
    }
    m = p.match(/^\/ui\/hosts\/(\d+)\/status$/);
    if (m && method === 'POST'){
      const h = HOSTS.find(x => x.id === Number(m[1]));
      h.status = body.status;
      return {ok: true};
    }
    m = p.match(/^\/ui\/hosts\/(\d+)\/refresh-profiles$/);
    if (m && method === 'POST'){
      const id = Number(m[1]);
      refreshCalls.push(id);
      concurrentRefresh++;
      maxConcurrentRefresh = Math.max(maxConcurrentRefresh, concurrentRefresh);
      await sleep(20);
      concurrentRefresh--;
      if (FAIL_HOST_IDS.has(id))
        throw new Error('AdsPower: нет связи с машиной');
      return {ok: true, host_id: id, pages: 1, found: 2, new: 1, reappeared: 0,
              missing_now: 0, active_now: 1, at: 1758600000};
    }

    if (p === '/ui/profiles'){
      if (profilesFail) throw new Error('профили не читаются: сеть недоступна');
      return {profiles: PROFILES.map(x => ({...x}))};
    }

    if (p === '/ui/channels/17?history=14') return CHANNEL_FULL;
    if (p === '/ui/channels/new?channel_id=17') return REF_FOR_17();
    if (p === '/ui/channels/new') return REF_PLAIN();
    if (p === '/ui/channels/17' && method === 'POST'){
      out.channelSave = body;
      return {ok: true};
    }
    return {};
  };

  const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  ME = {name: 'dev', role: 'admin'};
  addEventListener('hashchange', render);
  const go = async h => { location.hash = h; await sleep(300); };

  try{

  const cardByTitle = re => [...document.querySelectorAll('#app .card')]
    .find(c => c.querySelector('.sec') && re.test(txt(c.querySelector('.sec'))));

  // ── 1. Список машин, включая disabled ───────────────────────────────────
  // 24.09.2026: справочники переехали на экран «Админ» — на «Каналах» они заслоняли
  // сами каналы (владелец: «машина для заливки которая должна быть в админке»). Сам
  // экран «Админ» тянет ещё четыре сводки, к этому тесту отношения не имеющие, поэтому
  // рисуем здесь только сами карточки; ИХ МЕСТО на экране проверяется отдельно, по
  // исходнику (см. питонью часть).
  app.innerHTML = '';
  app.append(await hostsProfilesSection());
  await sleep(150);
  let hostsCard = cardByTitle(/Машины заливки/);
  // Карточки длинные, поэтому свёрнуты: разворачиваем, иначе строк не видно.
  const unfold = card => {
    const b = card.querySelector('.sec .iconbtn.fold');
    if (b && card.classList.contains('folded')) b.click();
  };
  unfold(hostsCard);
  out.hostsCardFound = !!hostsCard;
  const hostRows = () => [...hostsCard.querySelectorAll('.pitem')];
  out.hostRowsCount = hostRows().length;
  out.disabledHostShown = hostRows().some(r => /Гданьская машина/.test(txt(r)));
  out.disabledHostStatusText = txt(hostRows()
    .find(r => /Гданьская машина/.test(txt(r))));
  out.activeHostUrlShown = txt(hostRows()
    .find(r => /Минская машина/.test(txt(r)))).includes('50325');

  // ── 2. Общий список профилей: метки ──────────────────────────────────────
  let profCard = cardByTitle(/Профили AdsPower/);
  out.profCardFound = !!profCard;
  unfold(profCard);
  const profRows = () => [...profCard.querySelectorAll('.pitem')];
  out.profRowsCount = profRows().length;
  const activeRow = profRows().find(r => /№46/.test(txt(r)));
  const missingRow = profRows().find(r => /№12/.test(txt(r)));
  out.activeNowShown = /занят/.test(txt(activeRow));
  out.unknownShown = /не знаем/.test(txt(missingRow));
  out.unknownNotFree = !/свободен/.test(txt(missingRow));
  out.unknownWhyShown = /пропал с машины/.test(txt(missingRow));
  out.missingShown = /на машине больше нет/.test(txt(missingRow));
  out.activeRowNotMissing = !/на машине больше нет/.test(txt(activeRow));
  out.hostLabelShown = /Минская машина/.test(txt(activeRow));

  // ── 3. Добавить машину ───────────────────────────────────────────────────
  hostsCard.querySelector('#host-new').click();
  await sleep(150);
  let dlg = document.querySelector('dialog.modal');
  out.addModalOpened = !!dlg;
  dlg.querySelector('#h-code').value = 'gdansk2';
  dlg.querySelector('#h-code').dispatchEvent(new Event('input', {bubbles: true}));
  dlg.querySelector('#h-title').value = 'Вторая гданьская';
  dlg.querySelector('#h-url').value = 'http://10.0.0.5:50325';
  dlg.querySelector('.m-ok').click();
  await sleep(250);
  out.addSent = out.sent.find(s => s[0] === 'POST' && s[1] === '/ui/hosts');
  hostsCard = cardByTitle(/Машины заливки/);
  unfold(hostsCard);
  out.hostCountAfterAdd = hostRows.length;                 // подстраховка ниже точнее
  out.newHostShown = [...hostsCard.querySelectorAll('.pitem')]
    .some(r => /Вторая гданьская/.test(txt(r)));

  // ── 4. Править машину ────────────────────────────────────────────────────
  const minskRow = () => [...hostsCard.querySelectorAll('.pitem')]
    .find(r => /Минская машина/.test(txt(r)));
  minskRow().querySelector('button.host-edit').click();
  await sleep(150);
  dlg = document.querySelector('dialog.modal');
  out.editModalPrefilled = dlg.querySelector('#h-code').value === 'minsk';
  dlg.querySelector('#h-title').value = 'Минская машина (правка)';
  dlg.querySelector('.m-ok').click();
  await sleep(250);
  out.editSent = out.sent.find(s => s[0] === 'POST' && s[1] === '/ui/hosts/1');
  hostsCard = cardByTitle(/Машины заливки/);
  unfold(hostsCard);
  out.editShown = [...hostsCard.querySelectorAll('.pitem')]
    .some(r => /Минская машина \(правка\)/.test(txt(r)));

  // ── 5. Статус — «Убрать» не удаляет строку ──────────────────────────────
  const minskRow2 = () => [...hostsCard.querySelectorAll('.pitem')]
    .find(r => /Минская машина/.test(txt(r)));
  const statusBtnBefore = minskRow2().querySelector('button.host-status');
  out.statusBtnTitleBefore = statusBtnBefore.getAttribute('title');
  statusBtnBefore.click();
  await sleep(250);
  out.statusSent = out.sent.find(s => s[0] === 'POST' && s[1] === '/ui/hosts/1/status');
  hostsCard = cardByTitle(/Машины заливки/);
  unfold(hostsCard);
  out.hostStillThereAfterDisable = [...hostsCard.querySelectorAll('.pitem')]
    .some(r => /Минская машина/.test(txt(r)));
  out.statusTextAfterDisable = txt([...hostsCard.querySelectorAll('.pitem')]
    .find(r => /Минская машина/.test(txt(r))));
  // Вернём как было — остальным шагам нужна активная минская машина.
  [...hostsCard.querySelectorAll('.pitem')]
    .find(r => /Минская машина/.test(txt(r)))
    .querySelector('button.host-status').click();
  await sleep(250);
  hostsCard = cardByTitle(/Машины заливки/);
  unfold(hostsCard);

  // ── 6. «Обновить список профилей» у одной машины — успех и отказ ────────
  const minskRow3 = () => [...hostsCard.querySelectorAll('.pitem')]
    .find(r => /Минская машина/.test(txt(r)));
  minskRow3().querySelector('button.host-refresh').click();
  await sleep(250);
  out.refreshSent = out.sent.find(s => s[0] === 'POST'
    && s[1] === '/ui/hosts/1/refresh-profiles');
  out.refreshToast = txt(document.querySelector('.toast.ok'));

  FAIL_HOST_IDS.add(1);
  const profRowsBefore = profRows().length;
  hostsCard = cardByTitle(/Машины заливки/);
  unfold(hostsCard);
  [...hostsCard.querySelectorAll('.pitem')]
    .find(r => /Минская машина/.test(txt(r)))
    .querySelector('button.host-refresh').click();
  await sleep(250);
  out.refreshFailToast = txt(document.querySelector('.toast:not(.ok)'));
  profCard = cardByTitle(/Профили AdsPower/);
  unfold(profCard);
  out.profilesKeptAfterFailedRefresh = profRows().length === profRowsBefore
    && profRows().length > 0;
  FAIL_HOST_IDS.delete(1);

  // ── 7. «Обновить все» — только активные, по одной ────────────────────────
  refreshCalls.length = 0;
  maxConcurrentRefresh = 0;
  profCard.querySelector('#prof-refresh-all').click();
  await sleep(400);
  out.refreshAllCalledHosts = [...refreshCalls].sort();
  out.refreshAllMaxConcurrent = maxConcurrentRefresh;

  // ── 8. Честная ошибка вместо пустого списка ──────────────────────────────
  hostsFail = true; profilesFail = true;
  app.innerHTML = '';
  app.append(await hostsProfilesSection());
  await sleep(200);
  hostsCard = cardByTitle(/Машины заливки/);
  profCard = cardByTitle(/Профили AdsPower/);
  out.hostsErrorShown = /машины не читаются/.test(txt(hostsCard));
  out.profilesErrorShown = /профили не читаются/.test(txt(profCard));
  out.hostsNotSilentlyEmpty = !/Машин пока нет/.test(txt(hostsCard));
  hostsFail = false; profilesFail = false;

  // ── 9. Главный сценарий: карточка канала с пропавшим профилем ──────────
  await go('#/channel/17/nastroyki');
  const profSelect = document.querySelector('#f-prof');
  out.profSelectValue = profSelect ? profSelect.value : null;
  const missingOpt = profSelect
    ? [...profSelect.options].find(o => o.value === '99') : null;
  out.missingOptionPresent = !!missingOpt;
  out.missingOptionText = missingOpt ? missingOpt.textContent : '';
  out.missingOptionSelected = missingOpt ? missingOpt.selected : false;
  out.missingNoteShown = /пропал с машины/.test(txt(document.querySelector('#editbox')));
  document.querySelector('#save').click();
  await sleep(250);
  out.channelSaveProfileId = out.channelSave ? out.channelSave.profile_id : undefined;

} catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    print("Место на экране")
    src = open(os.path.join(os.path.dirname(HERE), "web", "index.html"),
               encoding="utf-8").read()
    admin = src.split("async function viewAdmin(")[1][:4000]
    channels = src.split("async function viewChannels(")[1][:4000]
    check("справочники рисуются на экране «Админ»",
          "hostsProfilesSection()" in admin, True)
    check("и убраны с экрана «Каналы» — там нужны сами каналы",
          "hostsProfilesSection()" in channels, False)

    res = browser_harness.run_scenario(SCENARIO, budget_ms=40000)
    for e in res.get("errs") or []:
        print("  ошибка на странице:", e)

    print("Машины заливки — список")
    check("карточка найдена", res.get("hostsCardFound"), True)
    check("обе машины видны (включая disabled)", res.get("hostRowsCount"), 2)
    check("disabled-машина показана", res.get("disabledHostShown"), True)
    check("статус disabled-машины словами", "убрана" in (res.get("disabledHostStatusText") or ""), True)
    check("адрес AdsPower активной машины виден", res.get("activeHostUrlShown"), True)

    print("\nПрофили AdsPower — общий список")
    check("карточка найдена", res.get("profCardFound"), True)
    check("оба профиля видны", res.get("profRowsCount"), 2)
    check("«занят» у открытого профиля", res.get("activeNowShown"), True)
    check("«не знаем» там, где свежих сведений нет", res.get("unknownShown"), True)
    check("«не знаем» не выдано за «свободен»", res.get("unknownNotFree"), True)
    check("и сказано, почему не знаем", res.get("unknownWhyShown"), True)
    check("активный не помечен пропавшим", res.get("activeRowNotMissing"), True)
    check("«на машине больше нет» у пропавшего", res.get("missingShown"), True)
    check("машина-владелец видна в строке", res.get("hostLabelShown"), True)

    print("\nДобавить машину")
    check("окно открылось", res.get("addModalOpened"), True)
    add_sent = res.get("addSent") or [None, None, {}]
    check("ушло на POST /ui/hosts", add_sent[:2], ["POST", "/ui/hosts"])
    check("…с введёнными полями",
          (add_sent[2] or {}).get("code"), "gdansk2")
    check("новая машина видна в списке без перезагрузки страницы",
          res.get("newHostShown"), True)

    print("\nПравить машину")
    check("окно предзаполнено текущим кодом", res.get("editModalPrefilled"), True)
    edit_sent = res.get("editSent") or [None, None, {}]
    check("ушло на POST /ui/hosts/1", edit_sent[:2], ["POST", "/ui/hosts/1"])
    check("правка видна в списке", res.get("editShown"), True)

    print("\nСтатус — «Убрать» не удаляет строку")
    check("кнопка звала действие «Убрать» у активной машины",
          res.get("statusBtnTitleBefore"), "Убрать")
    status_sent = res.get("statusSent") or [None, None, {}]
    check("ушло на POST /ui/hosts/1/status", status_sent[:2], ["POST", "/ui/hosts/1/status"])
    check("…со статусом disabled", (status_sent[2] or {}).get("status"), "disabled")
    check("строка машины осталась в списке (не удалена)",
          res.get("hostStillThereAfterDisable"), True)
    check("статус в строке изменился на «убрана»",
          "убрана" in (res.get("statusTextAfterDisable") or ""), True)

    print("\n«Обновить список профилей» у одной машины")
    refresh_sent = res.get("refreshSent") or [None, None, {}]
    check("ушло на POST /ui/hosts/1/refresh-profiles",
          refresh_sent[:2], ["POST", "/ui/hosts/1/refresh-profiles"])
    check("успех — тост со сводкой (найдено/новых)",
          all(w in (res.get("refreshToast") or "") for w in ["найдено", "новых"]), True)
    check("отказ — честная ошибка тостом, не тишина",
          "AdsPower" in (res.get("refreshFailToast") or ""), True)
    check("список профилей не опустел после неудачной описи",
          res.get("profilesKeptAfterFailedRefresh"), True)

    print("\n«Обновить все» — только активные машины, строго по одной")
    # К этому шагу в списке три машины: минская (снова активна после шага 5),
    # только что добавленная «Вторая гданьская» (id=3, активна) и исходная
    # «Гданьская машина» (id=2, так и осталась disabled) — её звать нельзя.
    check("званы только активные машины (disabled id=2 — не звана)",
          res.get("refreshAllCalledHosts"), [1, 3])
    check("запросы не пересекались во времени (последовательно, не параллельно)",
          res.get("refreshAllMaxConcurrent"), 1)

    print("\nЧестная ошибка вместо пустого списка")
    check("ошибка машин показана словами", res.get("hostsErrorShown"), True)
    check("ошибка профилей показана словами", res.get("profilesErrorShown"), True)
    check("это не «машин пока нет» (не выдаём сбой за пустоту)",
          res.get("hostsNotSilentlyEmpty"), True)

    print("\nГлавный сценарий: карточка канала с пропавшим текущим профилем")
    check("список профиля запрошен с channel_id (иначе пропавший профиль не пришёл бы)",
          res.get("missingOptionPresent"), True)
    check("пропавший профиль — текущий выбор канала", res.get("profSelectValue"), "99")
    check("…и он отмечен выбранным в самом <select>", res.get("missingOptionSelected"), True)
    check("подпись честно говорит «нет на машине»",
          "нет на машине" in (res.get("missingOptionText") or ""), True)
    check("рядом — заметное предупреждение про пропажу", res.get("missingNoteShown"), True)
    check("сохранение НЕ стирает profile_id (ловушка :6318→:6396→pub_ui.py:207)",
          res.get("channelSaveProfileId"), "99")

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
