#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Профиль: вход по имени в шапке, ключи и люди — на месте (20.09.2026).

Жалоба владельца: «профиль перенести при нажатии на логин в топбаре, плюс сделать
разделы более нативными, сейчас как-то странно что чтобы создать пользователя то можно
остаться в профиле, а если хочу ключ добавить то нужно куда-то переходить».

Что проверяется здесь и почему именно так:

  * в профиль ведёт имя человека в шапке, и ведёт ТОЛЬКО оно — второй вкладки
    «Профиль» в навигации нет (два входа в одно место — выбор без разницы);
  * подсветка раздела не потерялась вместе с вкладкой: имя светится на адресах
    профиля (#/profile, #/keys) и гаснет на чужом разделе;
  * ключ добавляется ПРЯМО в профиле: кнопка на месте, окно на месте, значение
    уходит в `/ui/keys`, и адрес страницы при этом не меняется;
  * человек заводится там же и тем же приёмом — список, одна кнопка, окно с формой;
    постоянно раскрытой формы на экране больше нет (из-за неё разделы и выглядели
    по-разному);
  * права не ослабли: раздел людей рисуется только когда `/ui/accounts` ответил,
    а когда он отказал — профиль остаётся рабочим, ключи никуда не делись;
  * экран #/keys жив: на него ведёт ссылка из карточки, и он по-прежнему рисуется.

Настоящий `web/index.html` в headless Chrome, `api()` подменён — ни стенда, ни сети,
ни настоящих ключей.

    .venv/bin/python tests/test_profile_native_browser.py
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
  const out = {sent: []};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let ADMIN = true;
  const KEYS = {providers: [], resting: [], own_keys: [], rr_cursor: [], busy: [],
                fastgen: null, unavailable: [],
                rewrite: {ok: true, own_openai: false, own_claude: false,
                          server_openai: false, server_claude: false, source: 'none'}};
  const ACCOUNTS = {users: [{id: 1, name: 'andrew', role: 'admin', tenant_id: 'dev',
                             quota: '', disabled: 0, has_password: true}],
                    me: 1, admins: 1,
                    roles: [{key: 'admin', title: 'администратор'},
                            {key: 'user', title: 'пользователь'}],
                    note: 'вход у человека один на всю платформу'};
  try {
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent.push([path, JSON.parse(opts.body)]);
      if (path === '/ui/me') return {name: 'Андрей', role: 'admin'};
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler / Recrafter'}], note: 'заметка'};
      if (path === '/ui/accounts'){
        // Не администратор — кабинет отказывает; карточка людей обязана исчезнуть
        // сама, а не прятаться галочкой на клиенте.
        if (!ADMIN) throw new Error('только администратор');
        return JSON.parse(JSON.stringify(ACCOUNTS));
      }
      if (path === '/ui/channels') return {channels: []};
      return {};
    };
    const txt = () => app.textContent.replace(/\s+/g, ' ');

    // ── 1. Шапка: имя — единственный вход в профиль ──────────────────────────
    location.hash = '#/profile';
    await render();
    await sleep(50);
    out.navTabs = [...document.querySelectorAll('nav a.tab')].map(a => a.textContent.trim());
    out.profileLinksInNav =
      [...document.querySelectorAll('nav a[href="#/profile"]')].map(a => a.id);
    out.nameIsTheLink = !!document.querySelector('#who a#tab-profile');
    out.onProfile = document.getElementById('tab-profile').classList.contains('on');

    // ── 1a. Разделы свёрнуты: за нужным не надо листать ──────────────────────
    const cardBy = re => [...app.querySelectorAll('.card')]
      .find(c => c.querySelector('.sec') && re.test(c.querySelector('.sec').textContent));
    const keysCard = cardBy(/Ключи провайдеров/);
    out.разделыСвёрнуты = [...app.querySelectorAll('.card')]
      .filter(c => c.querySelector('.sec .iconbtn.fold'))
      .every(c => c.classList.contains('folded'));
    out.счётВЗаголовке = (keysCard.querySelector('.sec .refcount') || {}).textContent || '';
    keysCard.querySelector('.sec .iconbtn.fold').click();
    await sleep(60);
    out.послеРаскрытия = !keysCard.classList.contains('folded');
    out.соседОстался = cardBy(/Люди/).classList.contains('folded');

    // ── 2. Ключи работают здесь же ───────────────────────────────────────────
    out.keyBtnInProfile = !!app.querySelector('#addkey');
    out.keysScreenLink = !!app.querySelector('a[href="#/keys"]');
    out.sysLinks = [...app.querySelectorAll('a[href="#/admin"], a[href="#/limits"]')]
      .map(a => a.getAttribute('href')).sort();
    app.querySelector('#addkey').click();
    await sleep(300);
    out.keyModal = !!document.querySelector('dialog.modal #kfam');
    document.querySelector('#kfam').value = 'googler';
    document.querySelector('#kval').value = 'sk-проверочный';
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(300);
    out.hashAfterKey = location.hash;
    out.keyModalClosed = !document.querySelector('dialog.modal');

    // ── 3. Человек заводится тем же приёмом ──────────────────────────────────
    out.peopleCard = /Люди/.test(txt());
    out.noOpenPeopleForm = !app.querySelector('#n-login');
    out.peopleBtn = (app.querySelector('#n-go') || {}).textContent;
    app.querySelector('#n-go').click();
    await sleep(300);
    const pm = document.querySelector('dialog.modal');
    out.peopleModal = !!(pm && pm.querySelector('#n-login'));
    pm.querySelector('#n-login').value = 'petya';
    pm.querySelector('#n-pw').value = 'парольпарольпароль';
    pm.querySelector('#n-role').value = 'user';
    pm.querySelector('#n-quota').value = '5';
    pm.querySelector('.m-ok').click();
    await sleep(300);
    out.hashAfterUser = location.hash;

    // ── 4. Не администратор: людей нет, ключи есть ───────────────────────────
    ADMIN = false;
    await render();
    await sleep(50);
    out.noPeopleForGuest = !/Люди/.test(txt());
    out.keysForGuest = !!app.querySelector('#addkey');

    // ── 5. Экран ключей жив, имя всё ещё подсвечено ──────────────────────────
    location.hash = '#/keys';
    await render();
    await sleep(50);
    out.keysScreen = /Свои ключи пользователей/.test(txt());
    out.keysScreenBtn = !!app.querySelector('#addkey');
    out.keysBack = !!app.querySelector('a.back[href="#/profile"]');
    out.onKeys = document.getElementById('tab-profile').classList.contains('on');

    // ── 6. Чужой раздел — имя гаснет ─────────────────────────────────────────
    location.hash = '#/channels';
    await render();
    await sleep(50);
    out.offElsewhere = !document.getElementById('tab-profile').classList.contains('on');
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=40000)
    sent = res.get("sent") or []
    check("сценарий выполнен без ошибок", res.get("error"), None)

    print("\nВход в профиль — имя в шапке")
    check("в навигации остались только разделы производства плюс имя",
          res.get("navTabs"), ["Каналы", "Проекты", "Очередь", "Пресеты", "Андрей"])
    check("вход в профиль в шапке ровно один", res.get("profileLinksInNav"),
          ["tab-profile"])
    check("и это имя человека, а не отдельная вкладка", res.get("nameIsTheLink"), True)
    check("на профиле имя подсвечено", res.get("onProfile"), True)
    check("на экране ключей — тоже (это тот же раздел)", res.get("onKeys"), True)
    check("на чужом разделе подсветка снята", res.get("offElsewhere"), True)

    print("\nРазделы свёрнуты, за нужным не надо листать")
    check("всё свёрнуто при открытии", res.get("разделыСвёрнуты"), True)
    check("в полосе заголовка — счёт", res.get("счётВЗаголовке"), "своих ключей нет")
    check("нажатие раскрывает", res.get("послеРаскрытия"), True)
    check("соседний раздел остаётся свёрнутым", res.get("соседОстался"), True)

    print("\nКлючи — прямо в профиле")
    check("кнопка «Добавить ключ» на экране профиля", res.get("keyBtnInProfile"), True)
    check("форма ключа открывается окном", res.get("keyModal"), True)
    check("ключ ушёл в кабинет как есть",
          [p for p, _b in sent][:1] and sent[0],
          ["/ui/keys", {"add": {"googler": ["sk-проверочный"]}}])
    check("экран при этом не сменился", res.get("hashAfterKey"), "#/profile")
    check("окно закрылось после сохранения", res.get("keyModalClosed"), True)
    check("на полный экран ключей ведёт ссылка", res.get("keysScreenLink"), True)

    print("\nЛюди — тем же приёмом, что и ключи")
    check("раздел людей у администратора есть", res.get("peopleCard"), True)
    check("постоянно раскрытой формы на экране нет", res.get("noOpenPeopleForm"), True)
    check("кнопка названа действием", (res.get("peopleBtn") or "").strip(),
          "Завести человека")
    check("форма человека открывается тем же окном", res.get("peopleModal"), True)
    check("человек ушёл в кабинет целиком",
          next((b for p, b in sent if p == "/ui/accounts"), None),
          {"login": "petya", "password": "парольпарольпароль",
           "role": "user", "quota": "5"})
    check("экран при этом не сменился", res.get("hashAfterUser"), "#/profile")

    print("\nПрава и соседний экран")
    check("кабинет отказал — раздела людей нет", res.get("noPeopleForGuest"), True)
    check("но ключи остались рабочими", res.get("keysForGuest"), True)
    check("экран #/keys жив", res.get("keysScreen"), True)
    check("и кнопка ключа на нём та же", res.get("keysScreenBtn"), True)
    check("с него есть обратный путь в профиль", res.get("keysBack"), True)
    check("«Система» осталась ссылками", res.get("sysLinks"), ["#/admin", "#/limits"])

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
