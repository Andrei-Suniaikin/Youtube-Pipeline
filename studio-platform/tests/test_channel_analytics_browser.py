#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вкладка «Статистика»: что видно в каждом из четырёх состояний (28.09.2026).

Владелец: «в канале сделать вкладку стату, по дефолту отображать то, что без ключа, а
если ключ есть — показывать подробную».

Состояний ровно четыре, и в каждом экран обязан говорить правду:

  1. не подключено          — объяснить, чего не хватает и откуда взять токен;
  2. не заданы реквизиты    — сказать про них ПЕРВЫМ делом: без них токен бесполезен;
  3. подключено, есть числа — показать их и сравнить с прошлым отрезком;
  4. доступ перестал жить   — сказать прямо, а не рисовать вчерашние числа как свежие.

Отдельно проверяется, что токен на экран не попадает НИКОГДА и что «нет показов» не
превращается в «ноль показов».

    .venv/bin/python tests/test_channel_analytics_browser.py
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


def flat(text: str) -> str:
    out = str(text or "")
    for space in (" ", " ", " ", "\n", "\t"):
        out = out.replace(space, " ")
    return out


def has(label, text, part):
    check(label, part in flat(text), True)


def hasnt(label, text, part):
    check(label, part in flat(text), False)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const SECRET = '1//очень-секретный-refresh-токен-0000';
  try {
    ME = {name: 'boss', role: 'admin'};
    const NUMBERS = {
      access: {connected: true, tail: '0000', state: 'ok', since: 1790000000,
               last_ok: 1790000000, error: '', client: true},
      days: 365, first: '2025-09-19', last: '2026-09-19',
      totals: {span: 28,
        now: {days: 28, views: 44000, minutes: 132000, subs_gained: 900, subs_lost: 140,
              subs_net: 760, likes: 3000, comments: 400, impressions: null,
              avg_sec: 310, avg_pct: 42, from: '2026-08-23', to: '2026-09-19'},
        was: {days: 28, views: 40000, minutes: 120000, subs_gained: 700, subs_lost: 200,
              subs_net: 500, likes: 2600, comments: 380, impressions: null,
              avg_sec: 300, avg_pct: 39, from: '2026-07-26', to: '2026-08-22'}},
      series: []};
    let analytics = {access: {connected: false, tail: '', state: '', since: 0,
                              last_ok: 0, error: '', client: true},
                     days: 0, first: '', last: '', totals: {}, series: []};

    const channel = () => ({
      channel: {id: 1, name: 'Канал', handle: '@ch', lang: 'PL', niche: '', uc: 'UC1',
                status: 'active', tz: 'Europe/Warsaw', horizon_days: 3, lead_days: 2,
                deferred: false, deferred_limit: 5, schedule: {mon: ['10:00']},
                per_week: 1, host: '', profile: '', presets: {}, note: '',
                profile_id: 1, host_id: 1, preset_vsgen_id: 1,
                preset_rewrite: '', preset_cover: ''},
      calendar: [], counts: {}, uploads: [], by_day: {}, tz: 'Europe/Warsaw',
      channel_preset: {}, waiting_without_slot: 0, waiting_items: [], history_days: 14,
      stats: {subs: 12340, views: 1204880, videos: 214, hidden: false, key: true,
              day: '2026-09-28', taken: 1790000000, delta: {'7': {subs: 58}}, series: []},
      analytics: analytics});

    const sentTo = [];
    window.api = async (p, opt) => {
      if (p === '/ui/me') return ME;
      if (p === '/ui/channels') return {channels: []};
      if (p === '/ui/channels/stats') return {channels: []};
      if (p.startsWith('/ui/channels/1')) return channel();
      return {};
    };
    window.post = async (p, body) => { sentTo.push({p, body}); return {loaded_days: 365}; };
    addEventListener('hashchange', render);

    const card = () => [...document.querySelectorAll('.card')]
      .find(el => (el.textContent || '').includes('Подробная статистика'));
    const go = async () => { location.hash = '#/channels'; await sleep(120);
                             location.hash = '#/channel/1/statistika'; await sleep(350); };

    // 1. Не подключено.
    await go();
    out.неПодключено = (card() || {}).textContent || '';
    out.полеЕсть = !!(card() || document.body).querySelector('#tok');
    // 09.10.2026: основной путь — кнопка Google; ручная вставка свёрнута ниже.
    out.кнопкаGoogle = (((card() || document.body).querySelector('#google') || {})
                        .textContent || '').trim();

    // Вставляем токен и смотрим, что ушло на сервер и не осело на экране.
    const input = card().querySelector('#tok');
    input.value = SECRET;
    card().querySelector('#on').click();
    await sleep(250);
    out.ушлоНаСервер = sentTo.map(x => x.p);
    out.токенУшёлВТеле = sentTo.some(x => (x.body || {}).token === SECRET);
    // Искать по всему body нельзя: исходник самого сценария лежит в <script> внутри
    // body, и SECRET нашёлся бы в нём — проверка бы «ловила» сама себя.
    out.токенВКарточке = ((card() || {}).textContent || '').includes(SECRET);
    out.токенОсталсяВПоле = [...document.querySelectorAll('input')]
      .some(el => (el.value || '').includes(SECRET));
    out.токенВАдресе = location.href.includes('refresh') || location.href.includes(SECRET);

    // 2. Реквизиты приложения не заданы.
    analytics = Object.assign({}, analytics, {
      access: Object.assign({}, analytics.access, {client: false})});
    await go();
    out.безРеквизитов = (card() || {}).textContent || '';

    // 3. Подключено, числа есть.
    analytics = NUMBERS;
    await go();
    out.сЧислами = (card() || {}).textContent || '';

    // 4. Доступ перестал работать.
    analytics = Object.assign({}, NUMBERS, {
      access: Object.assign({}, NUMBERS.access,
                            {state: 'dead', error: 'согласие отозвано'})});
    await go();
    out.мёртвый = (card() || {}).textContent || '';
    // У мёртвого ключа инструмент обязан быть ПОД РУКОЙ, а не только диагноз: поле для
    // нового ключа, кнопка и раскрытый гайд.
    out.мёртвыйПолеЕсть = !!document.querySelector('#tok');
    out.мёртвыйКнопка = ((document.querySelector('#on') || {}).textContent || '').trim();
    {
      const g = document.querySelector('details.keyblock');
      out.мёртвыйГайдРаскрыт = !!(g && g.open);
    }

    // 5. Работающий канал: блок на месте, но свёрнут — чтобы не мешал числам.
    analytics = NUMBERS;
    await go();
    out.живойПолеЕсть = !!document.querySelector('#tok');
    out.живойКнопка = ((document.querySelector('#on') || {}).textContent || '').trim();
    {
      const g = document.querySelector('details.keyblock');
      out.живойГайдЕсть = !!g;
      out.живойГайдСвёрнут = !!(g && !g.open);
      out.живойГайдВнутри = g ? (g.textContent || '').includes('OAuth Playground') : false;
    }

    // 6. Неподключённый канал: гайд раскрыт сразу.
    analytics = {access: {connected: false, client: true}, days: 0};
    await go();
    {
      const g = document.querySelector('details.keyblock');
      out.пустойГайдРаскрыт = !!(g && g.open);
    }
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

    print("Канал не подключён")
    off = res.get("неПодключено") or ""
    has("сказано, почему без подключения не видно", off, "публичными")
    has("сказано, что токен берут внутри профиля", off, "AdsPower")
    has("названа страница, где его берут", off, "OAuth Playground")
    # ОБА права. Пара yt-analytics.readonly + youtube.readonly — минимальный по
    # привилегиям набор, удовлетворяющий требованию `reports.query`: врезка страницы
    # метода и журнал изменений от 18.06.2018 требуют youtube.readonly прямым текстом,
    # а таблица «Authorization» ниже требует «at least one of» аналитических прав и
    # youtube.readonly не упоминает вовсе. Google там противоречит сам себе — примеры
    # кода на той же странице просят одно право, — поэтому ни одна его библиотека не
    # предупредит: токен выдастся, а отчёт может не прийти. Пара верна при любом чтении.
    # Расширить уже выданное согласие нельзя, только собрать заново по каждому каналу:
    # цена неполной инструкции равна числу каналов.
    has("назван нужный доступ", off, "yt-analytics.readonly")
    has("и второй, без которого отчёт не придёт", off, "youtube.readonly")
    has("сказано, что нужны оба", off, "оба")
    # Согласие дробное: снятая галочка даёт рабочий с виду токен, который не отвечает.
    has("предупреждено про снятые галочки", off, "не снимать")
    # Токен действует за ОДИН канал, и при нескольких каналах аккаунта Google
    # спрашивает, какой. Выбранный по ошибке даёт статистику чужого канала.
    has("предупреждён выбор канала", off, "какой канал")
    has("предупреждён экран «не проверено»", off, "не проверено")
    check("поле для токена есть", res.get("полеЕсть"), True)
    check("кнопка «Подключить через Google» есть", res.get("кнопкаGoogle"),
          "Подключить через Google")

    print("Токен уходит на сервер и не остаётся на экране")
    check("ушёл нужный маршрут", res.get("ушлоНаСервер"), ["/ui/channels/1/analytics"])
    check("значение ушло в теле запроса", res.get("токенУшёлВТеле"), True)
    check("в карточке не напечатался", res.get("токенВКарточке"), False)
    check("в поле не остался после перерисовки", res.get("токенОсталсяВПоле"), False)
    check("в адресную строку не попал", res.get("токенВАдресе"), False)

    print("Реквизиты приложения не заданы")
    nokey = res.get("безРеквизитов") or ""
    # Строка «идентификатор и секрет приложения» есть и в общей инструкции ниже, поэтому
    # ищем именно предупреждение — иначе проверка пройдёт и без него.
    has("предупреждение показано", nokey, "Сначала впишите")
    has("и куда вписать", nokey, "Ключи")

    print("Подключено: числа и сравнение с прошлым")
    got = res.get("сЧислами") or ""
    has("просмотры", got, "44 000")
    has("часы просмотра", got, "2 200")
    has("досмотренность", got, "42%")
    has("чистый прирост подписчиков", got, "+760")
    has("и из чего он сложился", got, "900")
    # Именно прирост просмотров, а не «где-то на карточке есть +10%»: у часов просмотра
    # процент тот же, и нестрогая проверка прошла бы при вовсе убранном сравнении.
    has("прирост просмотров назван числом", got, "+4 000")
    has("и долей к прошлому отрезку", got, "+10% к прошлым 28 дн")
    has("отрезок назван", got, "2026-08-23")
    has("история названа", got, "2025-09-19")

    print("Показов нет — так и сказано, а не «ноль»")
    hasnt("нуля показов нет", got, "Показов превью: 0")
    has("объяснено, что они приходят иначе", got, "суточными выгрузками")

    print("Доступ перестал работать")
    dead = res.get("мёртвый") or ""
    has("сказано прямо", dead, "перестал работать")
    has("названа причина", dead, "согласие отозвано")
    has("сказано, что делать", dead, "нужен новый ключ")
    # Главное в этом месте: у мёртвого ключа должен быть не только диагноз, но и
    # инструмент. Раньше карточка говорила «нужен новый токен» и на этом кончалась —
    # ни поля, ни инструкции, где его взять.
    check("поле для нового ключа есть", res.get("мёртвыйПолеЕсть"), True)
    check("кнопка говорит о замене", res.get("мёртвыйКнопка"), "Заменить")
    check("и гайд раскрыт сразу", res.get("мёртвыйГайдРаскрыт"), True)

    print("Гайд есть в канале всегда, а не только у неподключённого")
    # Владелец 01.10.2026: «нужно в каждом канале на уровне ключа выводить гайд».
    check("у работающего канала гайд на месте", res.get("живойГайдЕсть"), True)
    check("но свёрнут, чтобы не мешал числам", res.get("живойГайдСвёрнут"), True)
    check("внутри он тот же", res.get("живойГайдВнутри"), True)
    check("и поле замены рядом", res.get("живойПолеЕсть"), True)
    check("кнопка — «Заменить»", res.get("живойКнопка"), "Заменить")
    check("у неподключённого гайд раскрыт", res.get("пустойГайдРаскрыт"), True)

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
