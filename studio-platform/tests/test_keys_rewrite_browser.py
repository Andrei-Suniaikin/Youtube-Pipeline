#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ключ рерайта на экране ключей (18.09.2026) — поведение в браузере.

Владелец не нашёл, куда вписать ключ для текста: искал его в «Своих ключах
пользователей». Теперь состояние ключа рерайта — там же, а вписывается он той же
кнопкой «Добавить ключ», отдельным провайдером. Настоящий `web/index.html` в headless
Chrome, `api()` подменён — ни стенда, ни сети, ни настоящих ключей.

    .venv/bin/python tests/test_keys_rewrite_browser.py
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
  let keysGetCalls = 0;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // Чтение файла (`File.text()`) под `--virtual-time-budget` иногда разрешается не
  // сразу — фиксированной паузы не хватает через раз. Ждём условие, а не время.
  const waitFor = async (fn, timeout, step) => {
    const t0 = Date.now();
    while (Date.now() - t0 < (timeout || 2000)){
      if (fn()) return true;
      await sleep(step || 50);
    }
    return false;
  };
  const KEYS = {providers: [], resting: [], own_keys: [], rr_cursor: [], busy: [],
                fastgen: null, unavailable: [],
                rewrite: {ok: true, own_openai: false, own_claude: false,
                          server_openai: false, server_claude: false, source: 'none'}};
  try {
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent.push([path, JSON.parse(opts.body)]);
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler / Recrafter'}], note: 'заметка'};
      if (path === '/ui/keys/rewrite') return {ok: true};
      if (path === '/ui/me') return {name: 'dev', role: 'admin'};
      return {};
    };
    const cardText = () => {
      const c = [...document.querySelectorAll('.card')]
        .find(x => /Свои ключи пользователей/.test(x.textContent));
      // Подсказки «?» — тоже текст для человека: имя провайдера живёт в ней.
      const hints = c ? [...c.querySelectorAll('.q')].map(q => q.getAttribute('data-hint'))
                         .join(' ') : '';
      return c ? (c.textContent + ' ' + hints).replace(/\s+/g, ' ') : '';
    };

    await viewKeys();
    out.noKey = cardText();

    // Вписываем ключ той же кнопкой «Добавить ключ» — форма открывается окном
    // (18.09.2026), поэтому ждём его и жмём «Сохранить» в нём.
    document.querySelector('#addkey').click();
    await sleep(300);
    out.opts = [...document.querySelectorAll('#kfam option')].map(o => o.value);
    out.inModal = !!document.querySelector('dialog.modal #kfam');
    document.querySelector('#kfam').value = '__rewrite';
    document.querySelector('#kval').value = 'sk-проверочный';
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(300);

    // Ключ задан: строка это показывает, рядом «снять свой».
    KEYS.rewrite = {ok: true, own_openai: true, own_claude: false,
                    server_openai: false, server_claude: false, source: 'user'};
    await viewKeys();
    out.withKey = cardText();
    // Issue 1 (ревью итерация 2) — проверка QA итерации 3: `out.withKey` смотрит на
    // весь текст карточки и не отличит «Текст:» из заголовка над плашками от «?»,
    // повисшего отдельной строкой (именно так выглядел исходный дефект — узел hintDot
    // прямым потомком `.secbody`, а не внутри той же строки, что подпись). Проверяем
    // DOM напрямую: подсказка `.q` обязана лежать внутри того же `.note`, чей текст
    // начинается с «Текст:».
    const card1 = [...document.querySelectorAll('.card')]
      .find(x => /Свои ключи пользователей/.test(x.textContent));
    const hintBtn = card1 ? card1.querySelector('.secbody .q') : null;
    out.hintInTextLine = !!(hintBtn && hintBtn.closest('.note')
      && /Текст:/.test(hintBtn.closest('.note').textContent));
    out.hasDrop = !!document.querySelector('#rw-drop');
    document.querySelector('#rw-drop').click();
    await sleep(50);
    const yes = [...document.querySelectorAll('button')]
      .find(b => /Да|Снять|Убрать|Ок/i.test(b.textContent));
    if (yes) yes.click();
    await sleep(200);

    // Рерайт не ответил — говорим об этом, а не молчим.
    KEYS.rewrite = {ok: false, why: 'рерайт не отвечает'};
    await viewKeys();
    out.down = cardText();

    // ── Issue 1 (ревью 22.09.2026): своего ключа нет, но сервер его держит — карточка
    // обязана сказать, что текст всё равно делается (серверным), а не «ключа нет —
    // текст делать нечем». До этой правки own_openai/own_claude/own_codex молчали про
    // server_*/*_source, если ни одного own не было.
    KEYS.rewrite = {ok: true, own_openai: false, own_claude: false, own_codex: false,
                    server_openai: true, server_claude: false,
                    source: 'server', claude_source: 'none', codex_source: 'none'};
    await viewKeys();
    out.serverOnly = cardText();

    // Отдельно — сервер держит именно Codex (server_codex булева признака в контракте
    // вообще нет, задача A1 отдаёт только codex_source): «ключа нет» тоже не должно
    // быть написано, раз codex_source === 'server'.
    KEYS.rewrite = {ok: true, own_openai: false, own_claude: false, own_codex: false,
                    server_openai: false, server_claude: false,
                    source: 'none', claude_source: 'none', codex_source: 'server'};
    await viewKeys();
    out.codexServerOnly = cardText();

    // ── Issue 2 (ревью итерация 2): claude_source === 'server' бывает и без настоящей
    // серверной подписки — тогда сервер тянет Claude обычным ключом API. Различать по
    // server_claude: с ним — «серверная подписка Claude владельца», без него — та же
    // нейтральная «серверный ключ владельца», что и у OpenAI.
    KEYS.rewrite = {ok: true, own_openai: false, own_claude: false, own_codex: false,
                    server_openai: false, server_claude: true,
                    source: 'none', claude_source: 'server', codex_source: 'none'};
    await viewKeys();
    out.claudeSubOnly = cardText();

    KEYS.rewrite = {ok: true, own_openai: false, own_claude: false, own_codex: false,
                    server_openai: false, server_claude: false,
                    source: 'none', claude_source: 'server', codex_source: 'none'};
    await viewKeys();
    out.claudeKeyOnlyNoSub = cardText();

    // ── 22.09.2026: своя подписка Codex/Claude — независимо от ключа OpenAI ──
    // Дальше — свой счётчик отправок (out.sent2): out.sent проверен выше как есть, и
    // разбавлять его новыми записями значило бы поменять формулировку уже прошедших
    // проверок. api() тоже переставляется писать в него же.
    out.sent2 = [];
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent2.push([path, JSON.parse(opts.body)]);
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler / Recrafter'}], note: 'заметка'};
      if (path === '/ui/keys/rewrite') return {ok: true};
      if (path === '/ui/me') return {name: 'dev', role: 'admin'};
      return {};
    };

    // Заданы все три разом — ни одна не прячет состояние и «снять» двух других.
    // Issue 8 (ревью): в моках *_source — только значения контракта рерайта
    // (`user`/`server`/`none`, `rewrite/server.py:2496`), не выдуманное `own`.
    KEYS.rewrite = {ok: true, own_openai: true, own_codex: true, own_claude: true,
                    server_openai: false, server_claude: false,
                    source: 'user', codex_source: 'user', claude_source: 'user'};
    await viewKeys();
    out.allThree = cardText();
    out.dropsAllThree = ['#rw-drop', '#rw-drop-codex', '#rw-drop-claude']
      .map(sel => !!document.querySelector(sel));

    // Снять свою подписку Codex — подтверждение, затем пустой codex_auth. `render()`
    // внутри onOk уходит на маршрут из location.hash (в этом стенде — не «Ключи»),
    // поэтому на экран ключей после каждого действия возвращаемся сами — тем же
    // приёмом, что и у существующей проверки снятия ключа OpenAI выше.
    document.querySelector('#rw-drop-codex').click();
    await sleep(50);
    let yesC = [...document.querySelectorAll('button')]
      .find(b => /Да|Снять|Убрать|Ок/i.test(b.textContent));
    if (yesC) yesC.click();
    await sleep(150);
    out.dropCodexSent = out.sent2[out.sent2.length - 1];
    await viewKeys();

    // Снять свою подписку Claude — тем же путём, пустой oauth_token.
    document.querySelector('#rw-drop-claude').click();
    await sleep(50);
    let yesL = [...document.querySelectorAll('button')]
      .find(b => /Да|Снять|Убрать|Ок/i.test(b.textContent));
    if (yesL) yesL.click();
    await sleep(150);
    out.dropClaudeSent = out.sent2[out.sent2.length - 1];
    await viewKeys();

    // Окно «Добавить ключ» — два новых пункта провайдера рядом с «OpenAI · текст».
    document.querySelector('#addkey').click();
    await sleep(200);
    out.opts2 = [...document.querySelectorAll('#kfam option')].map(o => o.value);
    const kfam = document.querySelector('#kfam');

    // Claude — поле-пароль и подсказка про setup-token; своя строка кнопок формы
    // спрятана, футер окна — единственное действие.
    kfam.value = '__rewrite_claude';
    kfam.dispatchEvent(new Event('change'));
    await sleep(50);
    out.claudeFormText = (document.querySelector('#kval-wrap').textContent || '')
      .replace(/\s+/g, ' ');
    out.claudeType = document.querySelector('#sf-secret').type;
    out.claudeSfRowHidden = document.querySelector('#sf-ok').closest('.row').hidden;

    // Пустая отправка (Claude) — ничего не уходит, сказано, что вставить.
    const beforeEmptyClaude = out.sent2.length;
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(50);
    out.emptyClaudeErr = document.querySelector('dialog.modal .m-err').textContent;
    out.sentAfterEmptyClaude = out.sent2.length - beforeEmptyClaude;

    // Обратно на Codex — текстовое поле и filepicker, обычный #kval пропал.
    kfam.value = '__rewrite_codex';
    kfam.dispatchEvent(new Event('change'));
    await sleep(50);
    out.codexFormText = (document.querySelector('#kval-wrap').textContent || '')
      .replace(/\s+/g, ' ');
    out.hasFile = !!document.querySelector('#sf-file');
    out.kvalGoneForCodex = !document.querySelector('#kval');

    // Утечка значения при переключении #kfam туда-обратно (§7 edge case): вписываем
    // в форму Codex, уходим на обычный провайдер — #kval обязан вернуться пустым, а
    // не унаследовать текст Codex; затем вписываем в обычный #kval и уходим на
    // Claude — #sf-secret обязан быть пустым, а не унаследовать текст обычного поля.
    document.querySelector('#sf-secret').value = 'CODEXLEAK-should-not-leak';
    kfam.value = 'googler';
    kfam.dispatchEvent(new Event('change'));
    await sleep(50);
    out.kvalRestoredOnNormal = !!document.querySelector('#kval');
    out.kvalNotLeakedFromCodex = document.querySelector('#kval').value;

    // Issue 3 (ревью): переключение МЕЖДУ ДВУМЯ обычными провайдерами не должно
    // стирать уже вписанное значение — раньше #kval пересобирался при любой смене
    // #kfam, даже когда поле и так уже обычное.
    kfam.value = '__rewrite';
    kfam.dispatchEvent(new Event('change'));
    await sleep(50);
    document.querySelector('#kval').value = 'KEEP-ME-normal-switch';
    kfam.value = 'googler';
    kfam.dispatchEvent(new Event('change'));
    await sleep(50);
    out.kvalKeptBetweenNormalProviders = document.querySelector('#kval').value;

    document.querySelector('#kval').value = 'OPENAILEAK-should-not-leak';
    kfam.value = '__rewrite_claude';
    kfam.dispatchEvent(new Event('change'));
    await sleep(50);
    out.sfSecretNotLeakedFromNormal = document.querySelector('#sf-secret').value;
    // Возвращаемся на Codex — дальше сценарий продолжает проверять именно эту форму.
    kfam.value = '__rewrite_codex';
    kfam.dispatchEvent(new Event('change'));
    await sleep(50);

    // Пустая отправка (Codex).
    const beforeEmptyCodex = out.sent2.length;
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(50);
    out.emptyCodexErr = document.querySelector('dialog.modal .m-err').textContent;
    out.sentAfterEmptyCodex = out.sent2.length - beforeEmptyCodex;

    // Файл auth.json — содержимое встаёт в поле, имя файла показано.
    const dt = new DataTransfer();
    dt.items.add(new File(['{"auth_mode":"chatgpt","tokens":{"x":"FILESECRET"}}'],
                          'auth.json', {type: 'application/json'}));
    const file = document.querySelector('#sf-file');
    file.files = dt.files;
    file.dispatchEvent(new Event('change'));
    const secretEl = document.querySelector('#sf-secret');
    await waitFor(() => !!secretEl.value);
    out.fileContent = secretEl.value;
    out.fileName = document.querySelector('#sf-fname').textContent;

    // Отказ сервера — ошибка показана, вставленное не стёрто.
    api = (orig => async (path, opts) => {
      if (path === '/ui/keys/rewrite' && opts && opts.method === 'POST'){
        out.sent2.push([path, JSON.parse(opts.body)]);
        throw new Error('codex_auth must use ChatGPT subscription login');
      }
      return orig(path, opts);
    })(api);
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(100);
    out.serverErr = document.querySelector('dialog.modal .m-err').textContent;
    out.keptAfterRefuse = secretEl.value.includes('FILESECRET');
    // Issue 6 (ревью): содержательная проверка секрета в DOM, снятая ПОКА окно ещё
    // открыто (secretInDomAnywhere/secretAnywhereInHtml ниже снимаются уже после
    // close() — они честно проверяют только то, что осталось вне окна).
    out.errNoSecretLeak = !out.serverErr.includes('FILESECRET');

    // Принято — ушло на /ui/keys/rewrite (не /ui/keys), поле стёрто, окно закрыто,
    // render() перечитывает состояние с сервера.
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent2.push([path, JSON.parse(opts.body)]);
      if (path === '/ui/keys'){ keysGetCalls++; return JSON.parse(JSON.stringify(KEYS)); }
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler / Recrafter'}], note: 'заметка'};
      if (path === '/ui/keys/rewrite') return {ok: true, set: {codex: true}};
      if (path === '/ui/me') return {name: 'dev', role: 'admin'};
      return {};
    };
    // Адрес ставим на «Ключи» специально перед сохранением, а не раньше: `render()`
    // внутри onOk смотрит текущий `location.hash`, и только так его настоящий вызов
    // (не подменённый вручную `viewKeys()`, как во всех шагах выше) реально уйдёт
    // именно на экран ключей и перечитает список заново с сервера.
    location.hash = '#/keys';
    out.keysGetBeforeSave = keysGetCalls;
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(200);
    out.codexSent = out.sent2[out.sent2.length - 1];
    out.modalClosedAfter = !document.querySelector('dialog.modal');
    out.secretClearedOnEl = secretEl.value.includes('FILESECRET');
    // К этому моменту close() уже удалил <dialog> из документа — сам input/textarea с
    // секретом внутри него больше не существует в DOM. Содержательна тут именно
    // secretClearedOnEl (ссылка на отсоединённый узел, сохранённая ДО close()) — она
    // и ловит отсутствие очистки. secretInDomAnywhere/secretAnywhereInHtml ниже
    // проверяют страницу ПОСЛЕ закрытия окна: остатки вне окна (плашки карточки,
    // тосты, атрибуты) — это честная, но более узкая гарантия (ревью 22.09.2026,
    // Issue 6; проверка секрета, пока окно ещё открыто, — errNoSecretLeak выше).
    out.secretInDomAnywhere = [...document.querySelectorAll('input,textarea')]
      .some(e => (e.value || '').includes('FILESECRET'));
    // Секрет — не только в полях: HTML тела страницы не должен содержать его ни в
    // тексте, ни в чьём-либо атрибуте (data-*, title, placeholder-как-значение и т.п.).
    // `<script>` из тела исключаем — это исходник самого сценария (в нём буквально
    // напечатана строка 'FILESECRET' как тестовое значение), а не утечка приложения.
    const bodyClone = document.body.cloneNode(true);
    bodyClone.querySelectorAll('script').forEach(s => s.remove());
    out.secretAnywhereInHtml = bodyClone.innerHTML.includes('FILESECRET');
    out.keysGetAfterSave = keysGetCalls;

    // ── Issue 2 (ревью 22.09.2026): своя подписка Claude — непустой токен реально
    // отправлен. До этой правки поле Claude проверялось только визуально (тип
    // поля/текст инструкции) и пустой отправкой — сама отправка непустого значения
    // ни разу не проходила ни одной проверки, подмена {codex_auth:} на {oauth_token:}
    // осталась бы незамеченной. Значение — свежее и отличное от Codex-секрета выше:
    // подмена ветки на codex-путь тоже обязана быть поймана.
    const CLAUDE_TOKEN = 'sk-ant-oat01-' + 'z'.repeat(40);
    document.querySelector('#addkey').click();
    await sleep(200);
    let kfamC = document.querySelector('#kfam');
    kfamC.value = '__rewrite_claude';
    kfamC.dispatchEvent(new Event('change'));
    await sleep(50);
    const claudeSecretEl = document.querySelector('#sf-secret');
    claudeSecretEl.value = CLAUDE_TOKEN;
    // Полноценный мок (не только /ui/keys/rewrite): onOk после сохранения зовёт
    // настоящий render(), а он читает /ui/keys и /ui/keys/families — обрезанный мок
    // без этих путей ронял бы саму карточку, а не только эту проверку.
    api = async (path, opts) => {
      if (opts && opts.method === 'POST') out.sent2.push([path, JSON.parse(opts.body)]);
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler / Recrafter'}], note: 'заметка'};
      if (path === '/ui/keys/rewrite') return {ok: true, set: {claude: true}};
      if (path === '/ui/me') return {name: 'dev', role: 'admin'};
      return {};
    };
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(150);
    out.claudeSent = out.sent2[out.sent2.length - 1];
    out.claudeModalClosed = !document.querySelector('dialog.modal');
    out.claudeSecretClearedOnEl = claudeSecretEl.value.includes(CLAUDE_TOKEN);

    // Отказ сервера для Claude — тем же путём, что уже проверен для Codex: текст в
    // .m-err, введённое не стёрто, окно остаётся открытым.
    document.querySelector('#addkey').click();
    await sleep(200);
    let kfamC2 = document.querySelector('#kfam');
    kfamC2.value = '__rewrite_claude';
    kfamC2.dispatchEvent(new Event('change'));
    await sleep(50);
    const claudeSecretEl2 = document.querySelector('#sf-secret');
    const REFUSED_CLAUDE_TOKEN = 'sk-ant-api03-' + 'q'.repeat(40);
    claudeSecretEl2.value = REFUSED_CLAUDE_TOKEN;
    api = async (path, opts) => {
      if (path === '/ui/keys/rewrite' && opts && opts.method === 'POST'){
        out.sent2.push([path, JSON.parse(opts.body)]);
        throw new Error('это ключ API Anthropic, а не подписка');
      }
      if (path === '/ui/keys') return JSON.parse(JSON.stringify(KEYS));
      if (path === '/ui/keys/families')
        return {families: [{key: 'googler', title: 'Googler / Recrafter'}], note: 'заметка'};
      if (path === '/ui/me') return {name: 'dev', role: 'admin'};
      return {};
    };
    document.querySelector('dialog.modal .m-ok').click();
    await sleep(100);
    out.claudeRefusedErr = document.querySelector('dialog.modal .m-err').textContent;
    out.claudeRefusedKept = claudeSecretEl2.value === REFUSED_CLAUDE_TOKEN;
    out.claudeRefusedModalStillOpen = !!document.querySelector('dialog.modal');
  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO, budget_ms=30000)
    sent = res.get("sent") or []
    check("сценарий выполнен без ошибок", res.get("error"), None)
    check("без ключа так и написано — текст делать нечем",
          "ключа нет — текст делать нечем" in (res.get("noKey") or ""), True)
    # С 22.09.2026 — одна студия: провайдер называется «OpenAI · текст», без имени
    # отдельного сервиса.
    check("сказано, какой кнопкой вписать",
          "Добавить ключ" in (res.get("noKey") or "")
          and "«OpenAI · текст»" in (res.get("noKey") or ""), True)
    check("в списке провайдеров есть пункт рерайта",
          "__rewrite" in (res.get("opts") or []), True)
    check("форма ключа открывается окном, а не строкой в карточке",
          res.get("inModal"), True)
    check("ключ уходит в рерайт, а не в кабинет",
          [p for p, _b in sent], ["/ui/keys/rewrite", "/ui/keys/rewrite"])
    check("значение передано как есть", sent[0][1] if sent else None,
          {"openai_api_key": "sk-проверочный"})
    check("свой ключ виден в карточке",
          "ваш ключ OpenAI" in (res.get("withKey") or ""), True)
    check("есть «снять свой»", res.get("hasDrop"), True)
    check("Issue 1 (ревью итерация 2): подпись «Текст:» видна рядом с плашкой ключа",
          "Текст:" in (res.get("withKey") or ""), True)
    check("Issue 1 (тестер, итерация 3): подсказка «?» — в той же строке, что «Текст:», "
          "не отдельным узлом под плашками",
          res.get("hintInTextLine"), True)
    check("снятие — пустым значением", sent[1][1] if len(sent) > 1 else None,
          {"openai_api_key": ""})
    check("недоступный рерайт назван причиной",
          "рерайт не отвечает" in (res.get("down") or ""), True)

    print("Issue 1 (ревью): серверный ключ владельца виден, когда своего нет")
    server_only = res.get("serverOnly") or ""
    check("сказано, что работает серверный ключ",
          "серверный ключ владельца" in server_only, True)
    check("а не «ключа нет — текст делать нечем»",
          "ключа нет — текст делать нечем" in server_only, False)
    codex_server_only = res.get("codexServerOnly") or ""
    check("сказано, что работает серверная подписка Codex (по codex_source, без "
          "отдельного булева server_codex)",
          "серверная подписка Codex владельца" in codex_server_only, True)
    check("а не «ключа нет» (Codex-only server)",
          "ключа нет — текст делать нечем" in codex_server_only, False)

    print("Issue 2 (ревью итерация 2): «подписка» только при настоящем server_claude")
    claude_sub_only = res.get("claudeSubOnly") or ""
    check("server_claude=true — сказано «серверная подписка Claude владельца»",
          "серверная подписка Claude владельца" in claude_sub_only, True)
    claude_key_only_no_sub = res.get("claudeKeyOnlyNoSub") or ""
    check("server_claude=false — сказано нейтральное «серверный ключ владельца»",
          "серверный ключ владельца" in claude_key_only_no_sub, True)
    check("…и НЕ названо «серверная подписка Claude владельца», которой нет",
          "серверная подписка Claude владельца" in claude_key_only_no_sub, False)

    print("Своя подписка Codex/Claude (22.09.2026)")
    all3 = res.get("allThree") or ""
    check("все три плашки видны разом — ни одна не прячет другую",
          "ваш ключ OpenAI" in all3 and "ваша подписка Codex" in all3
          and "ваша подписка Claude" in all3, True)
    check("у каждой из трёх есть своя ссылка «снять»",
          res.get("dropsAllThree"), [True, True, True])
    check("снять свою подписку Codex — пустым codex_auth",
          res.get("dropCodexSent"), ["/ui/keys/rewrite", {"codex_auth": ""}])
    check("снять свою подписку Claude — пустым oauth_token",
          res.get("dropClaudeSent"), ["/ui/keys/rewrite", {"oauth_token": ""}])
    check("в окне «Добавить ключ» есть оба новых пункта",
          {"__rewrite_codex", "__rewrite_claude"} <= set(res.get("opts2") or []), True)
    check("Claude — подсказка про setup-token",
          "claude setup-token" in (res.get("claudeFormText") or ""), True)
    check("Claude — поле пароля", res.get("claudeType"), "password")
    check("своя строка кнопок формы спрятана — футер окна один",
          res.get("claudeSfRowHidden"), True)
    check("пустая отправка Claude не уходит на сеть", res.get("sentAfterEmptyClaude"), 0)
    check("…и сказано, что вставить", res.get("emptyClaudeErr"), "вставьте токен")
    check("Codex — подсказка про отдельную папку входа",
          "CODEX_HOME=~/codex-studio codex login" in (res.get("codexFormText") or ""), True)
    check("Codex — есть filepicker", res.get("hasFile"), True)
    check("обычное поле ключа пропало для своей подписки",
          res.get("kvalGoneForCodex"), True)
    check("переключение #kfam туда-обратно: #kval восстановлен",
          res.get("kvalRestoredOnNormal"), True)
    check("…и не унаследовал текст, набранный в форме Codex",
          res.get("kvalNotLeakedFromCodex"), "")
    check("значение из обычного #kval не утекло в форму Claude",
          res.get("sfSecretNotLeakedFromNormal"), "")
    check("Issue 3: переключение между двумя обычными провайдерами не стирает #kval",
          res.get("kvalKeptBetweenNormalProviders"), "KEEP-ME-normal-switch")
    check("пустая отправка Codex не уходит на сеть", res.get("sentAfterEmptyCodex"), 0)
    check("…и сказано, что вставить", res.get("emptyCodexErr"), "вставьте содержимое auth.json")
    check("файл auth.json встал в поле", "FILESECRET" in (res.get("fileContent") or ""), True)
    check("имя файла показано", res.get("fileName"), "auth.json")
    check("отказ сервера показан в окне",
          res.get("serverErr"), "codex_auth must use ChatGPT subscription login")
    check("…и вставленное не стёрто", res.get("keptAfterRefuse"), True)
    check("Issue 6: текст ошибки (окно ещё открыто) не содержит секрет",
          res.get("errNoSecretLeak"), True)
    check("успешная отправка ушла на /ui/keys/rewrite, не /ui/keys", res.get("codexSent"),
          ["/ui/keys/rewrite",
           {"codex_auth": '{"auth_mode":"chatgpt","tokens":{"x":"FILESECRET"}}'}])
    check("окно закрылось после сохранения", res.get("modalClosedAfter"), True)
    check("секрет стёрт из поля сразу после сохранения",
          res.get("secretClearedOnEl"), False)
    check("секрета нет ни в одном input/textarea на странице",
          res.get("secretInDomAnywhere"), False)
    check("секрета нет нигде в HTML документа (ни в тексте, ни в атрибуте)",
          res.get("secretAnywhereInHtml"), False)
    check("сохранение вызвало настоящий render(), а не только локальную подмену — "
          "GET /ui/keys ушёл повторно",
          (res.get("keysGetAfterSave") or 0) > (res.get("keysGetBeforeSave") or 0), True)

    print("Issue 2 (ревью): непустая своя подписка Claude реально отправлена")
    check("ушло на /ui/keys/rewrite с oauth_token, не с codex_auth и не на /ui/keys",
          res.get("claudeSent"),
          ["/ui/keys/rewrite", {"oauth_token": "sk-ant-oat01-" + "z" * 40}])
    check("окно закрылось после сохранения Claude", res.get("claudeModalClosed"), True)
    check("поле Claude стёрто сразу после сохранения",
          res.get("claudeSecretClearedOnEl"), False)
    check("отказ сервера для Claude показан в окне",
          res.get("claudeRefusedErr"), "это ключ API Anthropic, а не подписка")
    check("…и вставленное не стёрто (Claude)", res.get("claudeRefusedKept"), True)
    check("…и окно осталось открытым (Claude)", res.get("claudeRefusedModalStillOpen"), True)

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
