#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Канал промптов для картинок на экране «Ключи» — поведение в браузере (AC-16).

Карточка «Промпты для картинок» — рядом со «Своими ключами»: выпадающий список из
четырёх каналов (OpenAI / Claude / Codex / альт-Claude, порядок как у кабинета) плюс
«Авто», и под выбранным каналом — ровно одна форма, перерисованная в единственную
обёртку `#pchan-fields` (тем же приёмом, что модалка «Добавить ключ»). Claude и Codex
рисует переиспользованный `subFormHtml`/`subEmptyText`/`wireSubFile` (ревью 22.09.2026,
Issue 2) — у Codex поэтому есть выбор файла auth.json и предупреждение не путать вход
с подпиской для текста. Значения ключей сюда не приходят с сервера вовсе — только
«задан/не задан» на канал (`GET /ui/keys`, раздел `prompt`). Сохранение уходит в
`POST /ui/keys` полем `prompt.*` — тем же путём, что и обычные ключи, без второго
места хранения секрета.

На той же странице (администратор) рисуется и `textSubsCard` — у него тоже есть форма
с `#sf-secret`/`#sf-file` при открытии. Id внутри `subFormHtml` не уникальны на
странице специально: карточка промптов держит РОВНО ОДНУ обёртку и ищет свои поля
только внутри себя (`card.querySelector`, не `document.querySelector`) — сценарий ниже
открывает обе формы одновременно и проверяет именно это.

Ревью 22.09.2026, Issue 1 (Critical): у альт-Claude адрес/модель отправляются ТОЛЬКО
непустыми — пустая строка в этих двух полях и только в них кабинет читает как «очистить»
(`server/main.py:17783-17788`), а не как «не менять», и раньше сохранение одного лишь
ключа молча стирало уже сохранённые адрес/модель. Явная «-» по-прежнему обязана дойти
буквально (это и есть штатный способ очистить). Отдельно проверяется, что содержимое
файла auth.json, только что показанное в поле, не переживает собственное успешное
сохранение нигде в перерисованном DOM (секрет с сервера не приходит вовсе, и после
`render()` его не должно быть видно и в оставшейся с клиента разметке).

Настоящий `web/index.html` в headless Chrome, `api()` подменён — ни стенда, ни сети,
ни настоящих ключей. Маршрутизация — через `hashchange`/`render`, как в проде.

    .venv/bin/python tests/test_prompt_channel_browser.py
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
  const out = {sent: [], errs: []};
  addEventListener('error', e => out.errs.push(e.message));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // Чтение файла (`File.text()`) под `--virtual-time-budget` разрешается не сразу.
  // Здесь уже стояло «ждём условие, а не время» — но ждало оно ПО ЧАСАМ, а часы в стенде
  // виртуальные: Date.now() бежит вместе с ними, и «две секунды» истекают почти
  // мгновенно, тогда как чтение файла идёт в настоящем времени. Под нагрузкой ожидание
  // сдавалось, и тест падал тремя проверками сразу, оставаясь зелёным в одиночку.
  // Считаем ШАГИ: каждый отдаёт управление, и настоящая работа продвигается.
  const waitFor = async (fn, steps, step) => {
    for (let i = 0; i < (steps || 120); i++){
      if (fn()) return true;
      await sleep(step || 25);
    }
    return false;
  };
  const KEYS = {providers: [], resting: [], own_keys: [], rr_cursor: [], busy: [],
                fastgen: null, unavailable: [],
                rewrite: {ok: true, own_openai: false, own_claude: false,
                          server_openai: false, server_claude: false, source: 'none'},
                prompt: {selected: 'claude',
                         channels: {openai: {own_set: false}, claude: {own_set: true},
                                    codex: {own_set: false}, altclaude: {own_set: false}}}};
  try {
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent.push([path, JSON.parse(opts.body)]);
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler'}], note: 'заметка'};
      if (path === '/ui/subscriptions') return {items: [], env: {known: false}, pool: {}};
      if (path === '/ui/me') return {name: 'dev', role: 'admin'};
      return {ok: true};
    };
    ME = {name: 'dev', role: 'admin'};
    addEventListener('hashchange', render);
    const go = async h => { location.hash = h; await sleep(250); };

    // Карточка перерисовывается заново после каждого сохранения (`await render()`),
    // поэтому её ищем по тексту заголовка заново, а не держим старую ссылку.
    const card = () => [...document.querySelectorAll('.card')]
      .find(c => /Промпты для картинок/.test(c.textContent));

    await go('#/keys');
    out.cardFound = !!card();
    out.opts = [...card().querySelectorAll('#pchan option')].map(o => o.value);
    out.selectedValue = card().querySelector('#pchan').value;

    // Изначально выбран Claude (пришло с сервера) — под ним ровно одна форма с одним
    // #sf-secret, пароль, без файлового пикера (у Claude его нет).
    out.claudeSecretCount = card().querySelectorAll('#sf-secret').length;
    out.claudeFieldType = card().querySelector('#sf-secret').type;
    out.claudeHasFile = !!card().querySelector('#sf-file');
    // Значение ключа с сервера не приходит вовсе — поле обязано быть пустым.
    out.claudeFieldEmpty = card().querySelector('#sf-secret').value === '';

    // Переключение канала — под ним меняется форма, обёртка ОДНА (`#pchan-fields`):
    // старый #sf-secret Claude не остаётся в DOM рядом с новым Codex.
    const sel = () => card().querySelector('#pchan');
    sel().value = 'codex';
    sel().dispatchEvent(new Event('change'));
    await sleep(50);
    out.codexSecretCount = card().querySelectorAll('#sf-secret').length;
    out.codexIsTextarea = card().querySelector('#sf-secret').tagName;
    out.codexHasFile = !!card().querySelector('#sf-file');
    // Issue 2 (ревью 22.09.2026): своя папка CODEX_HOME для промптов картинок и
    // предупреждение не переиспользовать auth.json подписки для текста — не общий
    // `~/codex-studio`, а отдельная папка, и явный текст про разные хранилища.
    out.codexPaneText = card().querySelector('#pchan-fields').textContent;

    // Открываем форму Codex у СОСЕДНЕЙ карточки («Подписки для текста») — на странице
    // временно два независимых #sf-secret с одинаковым id. Карточка промптов не должна
    // это заметить: все её поиски — внутри своей карточки.
    const subs = document.querySelector('#subs-card');
    if (subs) subs.querySelector('#sub-add-codex').click();
    await sleep(50);
    out.documentWideSecretCount = document.querySelectorAll('#sf-secret').length;
    out.cardSecretCountWithNeighbourOpen = card().querySelectorAll('#sf-secret').length;

    // Файл auth.json — содержимое встаёт в #sf-secret именно КАРТОЧКИ ПРОМПТОВ, а не
    // соседней формы, несмотря на общий id.
    const dt = new DataTransfer();
    dt.items.add(new File(['{"auth_mode":"chatgpt","tokens":{"access_token":"a"}}'],
                          'auth.json', {type: 'application/json'}));
    const file = card().querySelector('#sf-file');
    file.files = dt.files;
    file.dispatchEvent(new Event('change'));
    const secretEl = card().querySelector('#sf-secret');
    await waitFor(() => !!secretEl.value);
    out.fileContent = secretEl.value;
    out.fileName = card().querySelector('#sf-fname').textContent;
    // Соседняя форма (Codex подписки для текста) свой #sf-secret не тронула.
    out.neighbourSecretUntouched = subs
      ? subs.querySelector('#sf-secret').value === '' : null;

    // Сохранение — содержимое файла уходит в prompt.codex_auth.
    card().querySelector('#pchan-save').click();
    await sleep(250);
    out.savedSent = out.sent[out.sent.length - 1];

    // После успешного сохранения `render()` перерисовал ВСЮ страницу заново из
    // ответа `/ui/keys` (мок `KEYS`, без единого значения ключа) — содержимое файла
    // auth.json, которое только что лежало в #sf-secret, нигде в свежем DOM остаться
    // не должно (AC-14/AC-16 в связке: секрет не переживает собственный сабмит).
    // Ищем только внутри `#app` (куда рендерится вся страница), не в `document.body`
    // целиком — тело страницы держит и сам инжектированный `<script>` сценария, чей
    // ИСХОДНЫЙ ТЕКСТ буквально содержит строку `access_token` (это литерал File() в
    // самом сценарии, а не значение из настоящего DOM) и дал бы ложное совпадение.
    out.domHasCodexSecretAfterSave = document.getElementById('app').innerHTML
      .includes('access_token');

    // Сохранение зовёт render() — карточка перерисована заново.
    // Переключение на OpenAI — своя простая разметка, без #sf-secret.
    sel().value = 'openai';
    sel().dispatchEvent(new Event('change'));
    await sleep(50);
    out.openaiHasPfOpenai = !!card().querySelector('#pf-openai');
    out.openaiSecretCount = card().querySelectorAll('#sf-secret').length;

    // Альт-Claude — ключ + адрес/модель, тоже своя разметка.
    sel().value = 'altclaude';
    sel().dispatchEvent(new Event('change'));
    await sleep(50);
    out.altHasFields = !!card().querySelector('#pf-altclaude')
      && !!card().querySelector('#pf-altclaude-base')
      && !!card().querySelector('#pf-altclaude-model');
    out.altSecretCount = card().querySelectorAll('#sf-secret').length;

    // Issue 1 (ревью 22.09.2026, Critical): адрес/модель, оставленные пустыми, не
    // должны уйти как заданные поля формы вовсе — кабинет отличает «поле есть, но
    // пусто» (=очистить) от «поля не было» (=не менять) именно у alt_llm_base/
    // alt_llm_model (`server/main.py:17783-17788`), и раньше пустая строка сюда
    // молча стирала бы уже сохранённые адрес и модель при каждом сохранении ключа.
    card().querySelector('#pf-altclaude').value = 'sk-ant-alt-key';
    card().querySelector('#pf-altclaude-base').value = '';
    card().querySelector('#pf-altclaude-model').value = '';
    card().querySelector('#pchan-save').click();
    await sleep(250);
    out.altEmptySent = out.sent[out.sent.length - 1];

    // render() перерисовал карточку заново — выбираем альт-Claude ещё раз и теперь
    // пробуем явную «-» в обоих полях: она обязана дойти буквально (это единственный
    // способ ОЧИСТИТЬ уже сохранённые адрес/модель через это же соглашение).
    sel().value = 'altclaude';
    sel().dispatchEvent(new Event('change'));
    await sleep(50);
    card().querySelector('#pf-altclaude').value = '-';
    card().querySelector('#pf-altclaude-base').value = '-';
    card().querySelector('#pf-altclaude-model').value = '-';
    card().querySelector('#pchan-save').click();
    await sleep(250);
    out.altDashSent = out.sent[out.sent.length - 1];

    // «Авто» — ни одно поле не показано.
    sel().value = '';
    sel().dispatchEvent(new Event('change'));
    await sleep(50);
    out.autoFieldsEmpty = card().querySelector('#pchan-fields').innerHTML.trim() === '';
    out.autoSecretCount = card().querySelectorAll('#sf-secret').length;
  } catch (e) { out.errs.push(String(e && e.stack || e)); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=20000)
    for e in res.get("errs") or []:
        print("  ошибка на странице:", e)

    print("Карточка «Промпты для картинок»")
    check("карточка найдена", res.get("cardFound"), True)
    # Мутация: убрать app.append(promptChannelCard(d)) из viewKeys() — падает здесь.
    check("четыре канала есть в списке (плюс «Авто»), порядок как у кабинета",
          res.get("opts"), ["", "openai", "claude", "altclaude", "codex"])
    check("выбранный сервером канал подставлен в select", res.get("selectedValue"), "claude")

    print("Панель Claude — переиспользованный subFormHtml")
    check("под Claude ровно один #sf-secret", res.get("claudeSecretCount"), 1)
    check("поле Claude — пароль (однострочный токен)", res.get("claudeFieldType"), "password")
    check("у Claude нет файлового пикера", res.get("claudeHasFile"), False)
    # Мутация: подставить значение ключа в поле с сервера — падает здесь (секрет не
    # приходит с сервера вовсе, поле обязано остаться пустым).
    check("поле Claude пустое — значение ключа с сервера не приходит",
          res.get("claudeFieldEmpty"), True)

    print("Переключение на Codex — тоже subFormHtml, но со своими параметрами (Issue 2)")
    # Мутация: держать обе панели в DOM одновременно вместо одной обёртки — падает здесь
    # (было бы 2).
    check("после переключения в карточке остаётся ровно один #sf-secret",
          res.get("codexSecretCount"), 1)
    check("поле Codex — textarea (auth.json целиком)", res.get("codexIsTextarea"), "TEXTAREA")
    check("у Codex есть выбор файла auth.json", res.get("codexHasFile"), True)
    codex_text = res.get("codexPaneText") or ""
    # Мутация: не передать opts в subFormHtml (потерять третий параметр) — обе проверки
    # ниже падают: остаётся общий `~/codex-studio` и не появляется предупреждение.
    check("своя папка CODEX_HOME для промптов картинок (не общая с текстом)",
          "codex-studio-prompts" in codex_text, True)
    check("есть предупреждение не переиспользовать auth.json подписки для текста",
          "ПРОМПТОВ КАРТИНОК" in codex_text, True)

    print("Соседняя карточка (подписки для текста) открыта одновременно — id общие")
    check("на странице два независимых #sf-secret (общий id, разные карточки)",
          res.get("documentWideSecretCount"), 2)
    # Мутация: искать #sf-secret через `document.querySelector` вместо `card.querySelector`
    # в wirePromptChannel/promptSubPaneHtml — эта проверка либо падает (нашёл бы чужой),
    # либо файл попал бы не в то поле (следующая проверка).
    check("но внутри своей карточки — по-прежнему ровно один",
          res.get("cardSecretCountWithNeighbourOpen"), 1)

    print("Файл auth.json — содержимое в своём поле, соседнее не тронуто")
    check("файл попал в #sf-secret карточки промптов",
          res.get("fileContent"), '{"auth_mode":"chatgpt","tokens":{"access_token":"a"}}')
    check("имя файла показано", res.get("fileName"), "auth.json")
    check("соседняя форма (подписка Codex для текста) не тронута",
          res.get("neighbourSecretUntouched"), True)

    print("Сохранение — POST /ui/keys с prompt.channel и prompt.codex_auth")
    sent = res.get("savedSent") or [None, None]
    # Мутация: в wirePromptChannel не собрать prompt.codex_auth для канала codex —
    # падает здесь.
    check("путь — /ui/keys", sent[0], "/ui/keys")
    check("тело — prompt.channel=codex и codex_auth содержимым файла",
          sent[1], {"prompt": {"channel": "codex",
                               "codex_auth": '{"auth_mode":"chatgpt","tokens":{"access_token":"a"}}'}})
    # Мутация: в wireKeys/render перестать перечитывать `/ui/keys` после сохранения
    # (оставить старую разметку в DOM) — эта проверка ловит: содержимое auth.json,
    # только что лежавшее в #sf-secret, осталось бы видно в свежей странице.
    check("после успешного сохранения секрет не остался нигде в DOM",
          res.get("domHasCodexSecretAfterSave"), False)

    print("OpenAI и альт-Claude — своя разметка, без #sf-secret")
    check("у OpenAI есть #pf-openai", res.get("openaiHasPfOpenai"), True)
    check("у OpenAI нет #sf-secret", res.get("openaiSecretCount"), 0)
    check("у альт-Claude есть ключ + адрес + модель", res.get("altHasFields"), True)
    check("у альт-Claude нет #sf-secret", res.get("altSecretCount"), 0)

    print("Альт-Claude, пустые адрес/модель — Issue 1 (ревью 22.09.2026, Critical)")
    alt_empty = res.get("altEmptySent") or [None, None]
    check("путь — /ui/keys", alt_empty[0], "/ui/keys")
    # Мутация: вернуть безусловное `prompt.alt_llm_base = base`/`alt_llm_model = model`
    # в wirePromptChannel (web/index.html) — эта проверка ловит: тело нёсло бы
    # `alt_llm_base: ""`/`alt_llm_model: ""`, которые кабинет читает как «очистить»
    # сохранённые адрес/модель, хотя человек их не трогал вовсе.
    check("пустые адрес/модель НЕ отправлены как заданные поля вовсе",
          alt_empty[1], {"prompt": {"channel": "altclaude", "alt_llm": "sk-ant-alt-key"}})

    print("Альт-Claude, явная «-» в адресе/модели — доходит буквально (это и есть очистка)")
    alt_dash = res.get("altDashSent") or [None, None]
    check("тело — «-» в обоих полях, буквально, не омисс",
          alt_dash[1], {"prompt": {"channel": "altclaude", "alt_llm": "-",
                                   "alt_llm_base": "-", "alt_llm_model": "-"}})

    print("«Авто» — ни одно поле канала не показано")
    check("обёртка полей пуста", res.get("autoFieldsEmpty"), True)
    check("и #sf-secret в карточке нет", res.get("autoSecretCount"), 0)

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
