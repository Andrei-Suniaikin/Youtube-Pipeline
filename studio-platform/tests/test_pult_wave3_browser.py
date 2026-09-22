#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Четыре экрана пульта для работы, уже сделанной на сервере (19.09.2026).

Какие дефекты ловятся. Все четыре — про одно: сервер знает ответ, а человек его
не видит, и потому чинит не то.

1. **Мёртвый выход субтитров не видно с экрана.** Туннель `127.0.0.1:1080` не поднят,
   заказы уходят в него и ждут таймаут; выпуски стоят. С пульта это выглядит как «всё
   встало», и причину ищут в очереди, в ключах, у соседей — где угодно, кроме связи.
   Сторож в кабинете знает ответ с 19.09; плитка обязана показать его и НЕ обязана
   решать за владельца: мёртвый выход возвращается сам, выключать его не надо.
   Отдельно проверяется, что «мертвы все» на экране не превращается в «выпуски
   остановлены»: ротация таким сведениям не верит, и работа идёт.

2. **Оплаченный и не забранный текст нигде не показан.** 146 выпусков простояли до
   18.09 с уже оплаченной задачей рерайта: деньги ушли, файл лежит у соседа, забрать
   его было некому и неоткуда. Экран должен показывать список и давать кнопку подбора
   — но так, чтобы её НЕ прочитали как «заказать»: повтор заказа это вторые деньги.
   Проход ходит в сеть и думает до 30 с на задачу, поэтому запрос пульта им не
   держится, а ожидание названо вслух («опрошено N из M»), иначе экран неотличим
   от зависшего.

3. **Про паузу очереди нельзя было ответить ни на один человеческий вопрос.**
   Что остановлено, с какого времени, кем, что это держит — рубильник одно поле в
   базе, а экран говорил «ждём». И снималась пауза вслепую: августовский залп — это
   ровно один клик «Возобновить». Здесь состояние печатается словами, а перед снятием
   доступен сухой прогон; самой кнопки снятия в пульте нет и быть не должно.

4. **Предпросмотр ссылок принимал донора без субтитров молча.** Выяснялось это через
   сутки, на шаге текста, уже оплаченным заказом. Теперь видно, есть ли дорожка и
   сколько в ней знаков. Главное здесь — «не смогли проверить» ОТДЕЛЬНО от «субтитров
   нет»: первое случается ровно тогда, когда лёг наш туннель, и красить его как отказ
   значит отправить владельца выбрасывать исправные ссылки.

Две части. Первая — настоящий `web/index.html` в headless Chrome с подменённым
`api()`: ни стенда, ни сети. Вторая — те же ручки на живом стенде.

    .venv/bin/python tests/test_pult_wave3_browser.py

Стенд для второй части (КОПИЯ базы, свой порт — правило проекта):

    cp dev/vsgen.dev.db /tmp/stand/vsgen.db
    VS_PORT=8104 VS_SERVER_DB=/tmp/stand/vsgen.db VS_DEV_DB=/tmp/stand/vsgen.db dev/run.sh

Без стенда вторая часть НЕ выполняется, и это не успех: выход 2 и отдельная строка.
Зелёный прогон без стенда был неотличим от зелёного со стендом, а на стенде живут
проверки про деньги и про то, что сырой ответ соседа наружу не уходит. Осознанный
прогон одной браузерной части объявляется явно: `VS_PULT_TEST_NO_STAND=1`.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8104/studio")
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")

FAILS = []
SKIPPED = []

# Пропуск стенда — не успех, а «не выполнено», и у него свой код возврата: 0 успех,
# 1 провал, 2 не выполнено. Перепутать эти три случая нельзя, потому что на стенде
# проверяются ровно деньги и утечка `payload`. Осознанный прогон без стенда
# объявляется переменной — умолчание выбрано здесь, а не владельцем.
NO_STAND_OK = os.environ.get("VS_PULT_TEST_NO_STAND") == "1"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────────────── часть 1: браузер ───────────────────────────

SCENARIO = r"""
(async () => {
  const out = {natives: [], asked: []};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // Браузерные попапы в пульте запрещены: они останавливают страницу и не дают
  // сравнить два числа. Любой вызов здесь будет замечен.
  window.alert = (...a) => out.natives.push(['alert', String(a[0])]);
  window.confirm = (...a) => { out.natives.push(['confirm', String(a[0])]); return true; };
  window.prompt = (...a) => { out.natives.push(['prompt', String(a[0])]); return 'x'; };
  try {

    /* ── 1. Выходы субтитров ── */
    const EXITS = {
      kind: 'warn', text: '18 из 21 работают, 3 не отвечают, 1 на отдыхе',
      total: 21, alive: 18, dead: 3, unknown: 0, resting: 1, trusted: true,
      probe_url: 'https://www.cloudflare.com/cdn-cgi/trace', period: 600,
      checked_ago: 120, running: false,
      shared_ip: {'95.1.2.3': ['socks5h://***@10.0.0.7:1080', 'напрямую']},
      how: 'Проверка стучится не в YouTube, а в нейтральный адрес.',
      rest_vs_dead: 'Отдыхает — это 429. Мёртв — не отвечает на проверку.',
      shared_ip_note: 'Делят квоту и репутацию.',
      exits: [
        {name: 'socks5h://***@10.0.0.7:1080', state: 'работает', alive: true, fresh: true,
         dead: false, resting: false, rest_min: 0, ok_ago: 30, checked_ago: 30, ms: 120,
         ip: '95.1.2.3', error: '', ip_shared: true},
        {name: 'socks5h://127.0.0.1:1080', state: 'мёртв', alive: false, fresh: true,
         dead: true, resting: false, rest_min: 0, ok_ago: null, checked_ago: 60, ms: 0,
         ip: '', error: 'туннель не отвечает: ConnectionRefusedError', ip_shared: false},
        {name: 'напрямую', state: 'отдыхает', alive: true, fresh: true, dead: false,
         resting: true, rest_min: 12, ok_ago: 900, checked_ago: 900, ms: 210,
         ip: '95.1.2.3', error: '', ip_shared: true},
      ],
    };
    const exCard = subsExitsCard(EXITS);
    document.body.append(exCard);
    out.exitStates = [...exCard.querySelectorAll('tbody tr td:nth-child(2) .pill')]
      .map(x => x.textContent.trim());
    out.exitDeadError = exCard.textContent.includes('ConnectionRefusedError');
    out.exitRestMin = exCard.textContent.includes('ещё 12 мин');
    out.exitSharedIp = exCard.textContent.includes('95.1.2.3');
    out.exitProbeUrl = exCard.textContent.includes('cdn-cgi/trace');
    out.exitNoDisable = !/выключит|удалит/i.test(exCard.textContent);
    // Пароль прокси чистится на сервере; экран обязан печатать то, что ему дали,
    // и ничего не «восстанавливать».
    out.exitNoSecret = !exCard.textContent.includes('s3cret');

    const allDead = JSON.parse(JSON.stringify(EXITS));
    allDead.trusted = false; allDead.kind = 'err'; allDead.dead = 21; allDead.alive = 0;
    allDead.text = 'ни один из 21 выходов не отвечает на проверку';
    const deadCard = subsExitsCard(allDead);
    out.allDeadSaysWorking = deadCard.textContent.includes('выпуски идут');
    out.allDeadNoStopClaim = !deadCard.textContent.includes('выпуски остановлены');

    const brokenCard = subsExitsCard({read_error: 'кабинет не отвечает'});
    // Переносы строк в вёрстке — не смысл: сравниваем текст, схлопнув пробелы.
    const flat = el => el.textContent.replace(/\s+/g, ' ').trim();
    out.exitErrorThree = ['кабинет не отвечает', 'Денег это не стоило',
                          'Обновите страницу'].every(w => flat(brokenCard).includes(w));

    /* ── 2. Пауза очереди ── */
    const PAUSE = {
      paused: true, all: true, providers: ['prompt_googler'], mine_prompt: false,
      blocks_my_texts: true,
      lines: ['Что остановлено: вся очередь студии — новые задачи не выдаются никому',
              'С какого времени: 31.08 14:02 (19 дн назад) · поставил: неизвестно',
              'Держится прямо сейчас: задач в очереди: 12 (озвучка 7, кадры 5); '
              + 'выпусков ждут новый текст: 187.'],
      targets: [{key: 'all', title: 'снять общую паузу'},
                {key: 'prompt_googler', title: 'возобновить: Googler'}],
      where_to_resume: 'Снимают паузу в админ-панели кабинета.',
    };
    api = async (path, opts) => {
      out.asked.push(path);
      if (path.startsWith('/ui/queue-pause/dry-run'))
        return {target: 'all', title: 'снятие общей паузы',
                text: 'Сухой прогон: снятие общей паузы.\nНемедленно стартуют задачи: '
                      + '34 шт. — озвучка 20, кадры 14.',
                note: 'Прогон ничего не снял и не записал.'};
      return {};
    };
    const pCard = pauseCard(PAUSE);
    document.body.append(pCard);
    out.pauseLines = [...pCard.querySelectorAll('.facts')].map(x => x.textContent.trim());
    // Единственные кнопки на карточке — сухой прогон. Кнопки «возобновить» в пульте
    // нет: у рубильника одна точка записи, и она в админ-панели.
    out.pauseButtons = [...pCard.querySelectorAll('button')].map(b => b.className.trim());
    out.pauseWhereResume = pCard.textContent.includes('админ-панели');
    pCard.querySelector('button.pause-dry').click();
    await sleep(300);
    const dlg = document.querySelector('dialog.modal');
    out.dryDialog = !!dlg && dlg.open;
    out.dryText = dlg ? dlg.querySelector('.mbody').textContent.includes('34 шт') : false;
    if (dlg) dlg.querySelector('.m-ok').click();
    await sleep(100);
    out.dryClosed = !document.querySelector('dialog.modal');
    out.pauseCardWhenFree = pauseCard({paused: false});

    /* ── 3. Проверка доноров ── */
    const CHECK = {donors: [
      {url: 'https://youtu.be/AAA', video_id: 'AAA', title: 'Годный донор', has_subs: true,
       chars: 24000, lang: 'pl', lang_ru: 'Польский', seconds: 1872, minutes: 31.2,
       length_from: 'субтитры', cached: false, verdict: 'ok', reason: 'ok',
       why: 'субтитры есть, 24000 знаков', warns: [], can_plan: true, checked: true},
      {url: 'https://youtu.be/BBB', video_id: 'BBB', title: 'Без дорожки', has_subs: false,
       chars: 0, lang: '', lang_ru: '', seconds: 0, minutes: 0, length_from: '',
       cached: false, verdict: 'reject', reason: 'no_subs',
       why: 'у ролика нет субтитров — текст выпуска брать не из чего',
       warns: [], can_plan: false, checked: true},
      {url: 'https://youtu.be/CCC', video_id: 'CCC', title: '', has_subs: null, chars: 0,
       lang: '', lang_ru: '', seconds: 0, minutes: 0, length_from: '', cached: false,
       verdict: 'unknown', reason: 'exit_dead',
       why: 'выход субтитров не отвечает (туннель) — проверить не смогли',
       warns: [], can_plan: true, checked: false},
      {url: 'https://youtu.be/DDD', video_id: 'DDD', title: '', has_subs: null, chars: 0,
       lang: '', lang_ru: '', seconds: 0, minutes: 0, length_from: '', cached: false,
       verdict: 'unknown', reason: 'exits_quota',
       why: 'выходы субтитров исчерпали квоту YouTube — проверим позже',
       warns: [], can_plan: true, checked: false},
      {url: 'https://youtu.be/EEE', video_id: 'EEE', title: 'Оценённый', has_subs: true,
       chars: 5000, lang: 'cs', lang_ru: 'Чешский', seconds: 714, minutes: 11.9,
       length_from: 'оценка по знакам', cached: true, verdict: 'warn',
       reason: 'long_estimate', why: 'выше потолка по оценке', warns: [], can_plan: true,
       checked: true},
    ], donor_summary: {ok: 1, warn: 1, reject: 1, unknown: 2}, timed_out: false,
       wall_sec: 30, donor_note: 'Проверка бесплатна: квоту YouTube Data API не тратит.'};
    const dBox = document.createElement('div');
    document.body.append(dBox);
    dBox.innerHTML = donorsHtml(CHECK);
    const rows = [...dBox.querySelectorAll('.stage')];
    out.donorRows = rows.length;
    out.donorPills = rows.map(r => r.querySelector('.pill').className.replace('pill ', ''));
    out.donorTexts = rows.map(r => r.querySelector('.facts').textContent.trim());
    // «Не смогли проверить» не имеет права выглядеть как «субтитров нет».
    out.unknownSaysNoSubs = rows.slice(2, 4).some(r => r.textContent.includes('субтитров нет'));
    out.rejectSaysNoSubs = rows[1].textContent.includes('субтитров нет');
    // Туннель и квота — разные строки с разными советами.
    out.deadAdvice = dBox.textContent.includes('нужен владелец');
    out.quotaAdvice = dBox.textContent.includes('проходит само')
                      || dBox.textContent.includes('пройдёт само');
    out.estimateLabelled = rows[4].textContent.includes('оценка по знакам');
    out.measuredPlain = out.donorTexts[0].includes('31 мин')
                        && !out.donorTexts[0].includes('оценка');
    // Повтор ссылки в пачке проверяется один раз — и экран обязан сказать об этом
    // словами, иначе строк меньше, чем ссылок в поле, и это читается как пропуск.
    out.dupSilentWhenNone = !dBox.textContent.includes('повторов');
    const dupBox = document.createElement('div');
    document.body.append(dupBox);
    dupBox.innerHTML = donorsHtml(Object.assign({}, CHECK, {duplicates: 2}));
    out.dupSaid = dupBox.textContent.includes('повторов 2')
                  && dupBox.textContent.includes('это не пропуск');
    out.subsTexts = [donorSubsText({has_subs: null, chars: 0}),
                     donorSubsText({has_subs: false, chars: 0}),
                     donorSubsText({has_subs: true, chars: 700})];

    // Ошибка проверки отвечает на три вопроса.
    api = async () => { throw new Error('рерайт не отвечает'); };
    const failBox = document.createElement('div');
    document.body.append(failBox);
    await checkDonors(failBox, 1, ['https://youtu.be/AAA']);
    out.checkFailThree = ['рерайт не отвечает', 'Денег', 'Поставить']
      .every(w => failBox.textContent.includes(w));

    /* ── 4. Оплачено, но не забрано ── */
    const REASONS = {taken: 'текст забран, фаза двинута', waiting: 'у соседа ещё не готово',
                     missing: 'задача пропала у соседа — нужен разбор, повтор оплачивается заново'};
    const PAID = {
      rows: [{release_id: 'r_ff9d03851f', tenant_id: 'dev', project_id: '', rel_status: 'pending',
              slot_at: 0, ch_name: 'Демо-канал', lang: 'pl', donor_title: 'Донор про танки',
              job_id: '77001', job_status: 'ordered', ordered_at: 0, src: 'job',
              waiting_sec: 90000}],
      batch: 25, reasons: REASONS,
      collect: {running: false, done: 0, total: 0, taken: 0, current: '', res: null,
                error: '', elapsed: 0},
      free: 'Подбор денег не тратит: он спрашивает статус уже оплаченной задачи.',
      how: 'Номер задачи взят из нашей строки о заказе либо из журнала оплат.',
    };
    api = async (path, opts) => {
      out.asked.push(path + (opts && opts.method ? ' ' + opts.method : ''));
      if (path === '/ui/paid-texts') return JSON.parse(JSON.stringify(PAID));
      if (path === '/ui/paid-texts/collect')
        return {running: true, done: 0, total: 1, taken: 0, current: '', elapsed: 0,
                reasons: REASONS, batch: 25};
      return {};
    };
    await viewPaidTexts();
    await sleep(100);
    out.paidButton = document.getElementById('pt-go').textContent.trim();
    out.paidFree = app.textContent.includes('денег не тратит')
                   || app.textContent.includes('денег не тратит'.toUpperCase());
    out.paidJob = app.textContent.includes('77001');
    out.paidWaiting = app.textContent.includes('1 дн');
    // «Заказать заново» рядом с оплаченной строкой — это вторые деньги за один текст.
    out.paidNoReorder = !/заказать заново|повторить рерайт|заказать текст/i
      .test(app.textContent);

    const cBox = document.createElement('div');
    document.body.append(cBox);
    paintCollect(cBox, {running: true, done: 3, total: 25, taken: 1, elapsed: 42,
                        current: 'Демо-канал · r_ff9d03851f'}, REASONS);
    out.collectWaiting = cBox.textContent.replace(/\s+/g, ' ').trim();
    paintCollect(cBox, {running: false, elapsed: 90, res: {
      candidates: 25, checked: 5, taken: 2, skipped: 3, left: 20,
      stopped: 'рерайт не отвечает', by_reason: {taken: 2, waiting: 3},
      items: [{release_id: 'r_1', channel: 'Демо-канал', job_id: '1', lang: 'pl',
               result: 'taken', note: '', chars: 18000}]}}, REASONS);
    out.collectDone = cBox.textContent.replace(/\s+/g, ' ').trim();

    /* Живучесть опроса. Опрос — единственный источник правды на этом экране: пока он
       жив, «идёт подбор» правда; как только он умер молча, экран врёт до конца сессии,
       а кнопка остаётся заблокированной (включает её только успешная ветка тика). */
    const goBtn = document.getElementById('pt-go');

    // (а) одно мигание связи опрос обязан пережить и продолжить спрашивать.
    goBtn.disabled = true;
    const okBox = document.createElement('div');
    document.body.append(okBox);
    let asks = 0;
    api = async () => {
      if (++asks <= 2) throw new Error('связь мигнула');
      return {running: true, done: asks, total: 25, taken: 0, current: '', elapsed: 10};
    };
    pollCollect(okBox, REASONS);
    await sleep(9000);
    out.pollSurvivedBlink = okBox.textContent.includes('опрошено');
    out.pollKeptAsking = asks >= 3;
    clearTimeout(PAID_POLL);

    // (б) связи нет совсем — опрос обязан сдаться ВСЛУХ, а не показывать «идёт» вечно.
    const deadBox = document.createElement('div');
    document.body.append(deadBox);
    let asks2 = 0;
    api = async () => { asks2++; throw new Error('пульт не отвечает'); };
    goBtn.disabled = true;
    pollCollect(deadBox, REASONS);
    await sleep(20000);
    out.pollTries = asks2;
    out.pollGaveUpThree = ['пульт не отвечает', 'Денег', 'Обновите страницу']
      .every(w => deadBox.textContent.includes(w));
    out.pollStillSaysRunning = deadBox.textContent.includes('Идёт подбор');
    out.pollUnlocked = goBtn.disabled === false;
    const seen = asks2;
    await sleep(10000);
    out.pollStoppedAsking = (asks2 === seen);   // сдались — значит перестали дёргать
    clearTimeout(PAID_POLL);

    // (в) истёкшая сессия — это «войдите снова», а не «опрос не отвечает». Во всём
    // остальном пульте 401 ведёт на экран входа; молча глотать его здесь нельзя.
    // Идёт последним: экран входа затирает всё, что показано выше.
    const nBox = document.createElement('div');
    document.body.append(nBox);
    api = async () => { throw new NeedLogin('нужен вход'); };
    pollCollect(nBox, REASONS);
    await sleep(3000);
    out.pollNeedLogin = !!document.querySelector('.login');
    out.pollNoErrorOn401 = !nBox.textContent.includes('не читается');
    clearTimeout(PAID_POLL);

  } catch (e) { out.error = String(e && e.stack || e); }
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def browser_part() -> None:
    # Виртуальное время: проверка живучести опроса ждёт ~40 с браузерных, а не наших.
    res = browser_harness.run_scenario(SCENARIO, budget_ms=70000)
    if res.get("error") and "Chrome не найден" in str(res.get("error")):
        print("  ПРОПУЩЕНО: " + res["error"])
        FAILS.append("браузерная часть не выполнена")
        return
    check("сценарий выполнен без ошибок", res.get("error"), None)
    check("ни alert, ни confirm, ни prompt", res.get("natives"), [])

    print("\n1. Выходы субтитров")
    check("три состояния названы словами", res.get("exitStates"),
          ["работает", "мёртв", "отдыхает"])
    check("ошибка мёртвого выхода видна с экрана", res.get("exitDeadError"), True)
    check("отдых показан сроком, а не смертью", res.get("exitRestMin"), True)
    check("общий внешний адрес показан", res.get("exitSharedIp"), True)
    check("видно, что проверка не ходит в YouTube", res.get("exitProbeUrl"), True)
    check("выключить выход не предлагается", res.get("exitNoDisable"), True)
    check("пароля прокси на экране нет", res.get("exitNoSecret"), True)
    check("«мертвы все» = работа идёт", res.get("allDeadSaysWorking"), True)
    check("«мертвы все» НЕ значит «выпуски остановлены»",
          res.get("allDeadNoStopClaim"), True)
    check("неудача чтения отвечает на три вопроса", res.get("exitErrorThree"), True)

    print("\n2. Пауза очереди")
    check("состояние напечатано словами", len(res.get("pauseLines") or []), 3)
    check("что остановлено — первой строкой",
          (res.get("pauseLines") or [""])[0].startswith("Что остановлено"), True)
    check("на карточке только сухой прогон",
          sorted(set(res.get("pauseButtons") or [])), ["ghost small pause-dry"])
    check("сказано, где снимают паузу", res.get("pauseWhereResume"), True)
    check("прогон открывается окном, а не попапом", res.get("dryDialog"), True)
    check("в прогоне видно, сколько стартует немедленно", res.get("dryText"), True)
    check("окно закрывается", res.get("dryClosed"), True)
    check("без паузы карточки нет", res.get("pauseCardWhenFree"), None)

    print("\n3. Проверка доноров в предпросмотре")
    check("строка на каждую ссылку", res.get("donorRows"), 5)
    check("«не смогли проверить» не покрашено как отказ", res.get("donorPills"),
          ["s-done", "s-error", "s-idle", "s-idle", "s-queued"])
    check("непроверенное НЕ названо «субтитров нет»",
          res.get("unknownSaysNoSubs"), False)
    check("настоящее отсутствие дорожки названо прямо",
          res.get("rejectSaysNoSubs"), True)
    check("мёртвый туннель: чинит владелец", res.get("deadAdvice"), True)
    check("квота выходов: проходит само", res.get("quotaAdvice"), True)
    check("оценка длины подписана как оценка", res.get("estimateLabelled"), True)
    check("измеренная длина печатается без оговорки", res.get("measuredPlain"), True)
    check("субтитры: три разных ответа", res.get("subsTexts"),
          ["субтитры не проверяли", "субтитров нет", "субтитры есть · 700 знаков"])
    check("знаки расшифровки видны",
          "24000 знаков" in (res.get("donorTexts") or [""])[0], True)
    check("неудача проверки отвечает на три вопроса", res.get("checkFailThree"), True)
    check("схлопнутые повторы названы числом", res.get("dupSaid"), True)
    check("без повторов про них молчат", res.get("dupSilentWhenNone"), True)

    print("\n4. Оплачено, но не забрано")
    check("кнопка не читается как «заказать»", res.get("paidButton"),
          "Забрать оплаченное (денег не тратит)")
    check("сказано, что денег не тратит", res.get("paidFree"), True)
    check("номер оплаченной задачи виден", res.get("paidJob"), True)
    check("сколько ждём — видно", res.get("paidWaiting"), True)
    check("«заказать заново» на экране нет", res.get("paidNoReorder"), True)
    wait = res.get("collectWaiting") or ""
    check("ожидание названо числами", "опрошено 3 из 25" in wait, True)
    check("видно, кого спрашиваем сейчас", "r_ff9d03851f" in wait, True)
    check("видно, сколько уже ждём", "Ждём уже" in wait, True)
    done = res.get("collectDone") or ""
    check("итог прохода в числах", "забрано текстов 2 из 5" in done, True)
    check("остановка не выдаётся за «всё забрано»", "НЕ опрошены" in done, True)
    check("сказано, что осталось и что нажать", "Осталось 20" in done, True)
    check("коды исхода переведены", "у соседа ещё не готово" in done, True)
    check("мигание связи опрос переживает", res.get("pollSurvivedBlink"), True)
    check("и продолжает спрашивать после неудачи", res.get("pollKeptAsking"), True)
    check("без связи опрос пробует несколько раз", res.get("pollTries"), 4)
    check("а потом сдаётся вслух, на три вопроса", res.get("pollGaveUpThree"), True)
    check("и не оставляет на экране «идёт подбор»",
          res.get("pollStillSaysRunning"), False)
    check("кнопка не заперта навсегда", res.get("pollUnlocked"), True)
    check("сдались — значит перестали дёргать", res.get("pollStoppedAsking"), True)
    check("истёкшая сессия ведёт на экран входа", res.get("pollNeedLogin"), True)
    check("и не выдаётся за отказ опроса", res.get("pollNoErrorOn401"), True)


# ─────────────────────────── часть 2: стенд ───────────────────────────

class Client:
    """Браузер на минималках: помнит куки, отдаёт код и тело."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(PULT + path, data=data, method=method
                                     or ("POST" if data else "GET"))
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode() or "{}")
            except Exception:                                      # noqa: BLE001
                return exc.code, {}


def stand_part() -> bool:
    me = Client()
    try:
        code, _ = me.go("/ui/login", {"login": LOGIN, "password": PASSWORD})
    except Exception as exc:                                       # noqa: BLE001
        print(f"  ПРОПУЩЕНО: стенд {PULT} не отвечает ({type(exc).__name__}). "
              f"Поднимите его на КОПИИ базы: VS_PORT=8104 VS_SERVER_DB=… dev/run.sh")
        return False
    if code != 200:
        print(f"  ПРОПУЩЕНО: вход на стенд не принят ({code}). "
              f"Пароль задаётся VS_DEV_PASSWORD, учётка — dev/provision.py")
        return False

    print("\n5. Ручки на живом стенде")
    code, exits = me.go("/ui/subs-exits")
    check("состояние выходов отдаётся", code, 200)
    check("вид состояния из списка", exits.get("kind") in ("ok", "warn", "err", "none"),
          True)
    check("сказано, куда стучится проверка",
          "youtube" not in str(exits.get("probe_url", "")).lower(), True)
    check("на каждый выход по строке", len(exits.get("exits") or []),
          exits.get("total"))
    check("ротации видно, верит ли она проверке", "trusted" in exits, True)

    code, probe = me.go("/ui/subs-exits/probe", {})
    check("будильник проверки отвечает сразу", code, 200)
    check("и говорит, что именно сделал", bool(probe.get("text")), True)

    code, pause = me.go("/ui/queue-pause")
    check("состояние паузы отдаётся", code, 200)
    check("состояние словами — готовыми строками",
          isinstance(pause.get("lines"), list), True)
    check("сказано, где снимают паузу", bool(pause.get("where_to_resume")), True)
    if pause.get("paused"):
        check("есть что напечатать", len(pause.get("lines") or []) > 0, True)
        target = (pause.get("targets") or [{}])[0].get("key")
        code, dry = me.go("/ui/queue-pause/dry-run?target=" + str(target))
        check("сухой прогон отдаётся", code, 200)
        check("прогон ничего не менял", dry.get("changed"), False)
        check("в прогоне есть числа освобождаемого", "tasks" in (dry.get("freed") or {}),
              True)
        code, again = me.go("/ui/queue-pause")
        check("после прогона пауза на месте", again.get("paused"), True)
    else:
        print("  (пауза на стенде не стоит — прогон не проверяется)")
    code, _bad = me.go("/ui/queue-pause/dry-run?target=net-takoy-pauzy")
    check("несуществующая цель отклоняется", code, 400)

    code, paid = me.go("/ui/paid-texts")
    check("список оплаченного отдаётся", code, 200)
    check("русские фразы к кодам исхода приложены",
          bool((paid.get("reasons") or {}).get("missing")), True)
    check("сырой ответ соседа наружу не отдаётся",
          any("payload" in r for r in (paid.get("rows") or [])), False)
    check("сказано, что подбор денег не тратит",
          "не тратит" in str(paid.get("free", "")), True)
    code, state = me.go("/ui/paid-texts/collect", method="GET")
    check("состояние прохода читается без запуска", code, 200)
    check("и не идёт само по себе", state.get("running"), False)

    code, prev = me.go("/ui/links/preview", {"channel_id": 1, "links": "не-ссылка"})
    check("предпросмотр без ссылок отказывает словами", code, 400)

    # Повтор ОДНОГО РОЛИКА в двух написаниях — две разные строки, и `parse_links` их
    # не схлопывает; ловит такой дубль `add_links` по `video_id`. Проверяем, что из
    # пары выбрасывается ровно одна строка, а вторая остаётся и уходит на проверку
    # донора: потерять обе — значит поставить ролик в план без единой проверки и без
    # единой строки на экране. Ссылка заведомо не из плана, сети здесь нет
    # (предпросмотр названий не добывает), запись откатывается.
    same = ["https://youtu.be/dupDUPdup01",
            "https://www.youtube.com/watch?v=dupDUPdup01"]
    code, prev2 = me.go("/ui/links/preview",
                        {"channel_id": 1, "links": "\n".join(same)})
    check("предпросмотр с повтором ролика отвечает", code, 200)
    check("повтор того же ролика отсеян", len(prev2.get("skipped") or []), 1)
    check("а вставшая копия ушла на проверку донора", prev2.get("fresh"), same[:1])

    # Проверка доноров: не-YouTube ссылки, чтобы не дёргать выходы субтитров.
    code, dup = me.go("/ui/links/check",
                      {"channel_id": 1, "urls": ["https://example.com/a",
                                                 "https://example.com/a",
                                                 "https://example.com/b"]})
    check("проверка доноров отвечает", code, 200)
    check("повтор в пачке проверен один раз", len(dup.get("donors") or []), 2)
    check("и об этом сказано числом", dup.get("duplicates"), 1)
    code, _foreign = me.go("/ui/links/check", {"channel_id": 987654321,
                                               "urls": ["https://example.com/a"]})
    check("чужой канал проверка доноров не принимает", code, 404)
    return True


def unchecked_part() -> None:
    """Ссылка, которую не успели проверить, не превращается в отказ.

    Проверяется прямым вызовом, без стенда и браузера: это единственное место, где
    пульт сам сочиняет ответ про донора, и ошибка в нём стоит выброшенных исправных
    ссылок. Первый вариант этого кода отдавал «это не ссылка на ролик» ровно там, где
    надо было сказать «не успели».
    """
    print("\n6. Не успели проверить — это не отказ")
    sys.path.insert(0, os.path.join(BASE, "server"))
    sys.path.insert(0, os.path.join(BASE, "shared"))
    sys.path.insert(0, BASE)
    os.environ.setdefault("VS_SERVER_DISABLE_BACKGROUND", "1")
    os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
    try:
        from web.app import donor_unchecked                        # noqa: PLC0415
        from pub_tags import donor_limits                          # noqa: PLC0415
    except Exception as exc:                                       # noqa: BLE001
        print(f"  ПРОПУЩЕНО: модуль пульта не поднялся ({type(exc).__name__}: {exc})")
        FAILS.append("прямая проверка непроверенной ссылки не выполнена")
        return
    lim = donor_limits(None)
    yt = donor_unchecked("https://www.youtube.com/watch?v=dQw4w9WgXcQ", lim)
    check("вердикт — «не смогли проверить»", yt["verdict"], "unknown")
    check("причина названа машинным кодом", yt["reason"], "budget")
    check("такую ссылку ставить можно", yt["can_plan"], True)
    check("и про ролик мы ничего не узнали", yt["checked"], False)
    check("субтитры не объявлены отсутствующими", yt["has_subs"], None)
    other = donor_unchecked("https://example.com/", lim)
    check("а не-ссылка на ролик остаётся отказом", other["verdict"], "reject")
    check("и по своей причине", other["reason"], "not_youtube")


def collect_start_part() -> None:
    """Сорвавшийся старт потока не запирает кнопку подбора до перезапуска пульта.

    Флаг «идёт» ставится ДО `Thread.start()` намеренно — иначе два нажатия подряд
    запустят два прохода по одному списку. Но если старт не удался, снять флаг больше
    некому: он залипал навсегда, все нажатия получали 409 «подбор уже идёт», а экран
    вечно показывал «идёт». Проверяется прямым вызовом: стенду такое не подстроишь.
    """
    print("\n7. Подбор: сорвавшийся старт не запирает кнопку")
    try:
        from fastapi import HTTPException                          # noqa: PLC0415
        from web import app as pult                                # noqa: PLC0415
    except Exception as exc:                                       # noqa: BLE001
        print(f"  ПРОПУЩЕНО: модуль пульта не поднялся ({type(exc).__name__}: {exc})")
        FAILS.append("проверка сорвавшегося старта подбора не выполнена")
        return

    class Boom:
        """Поток, который не стартует: ровно то, чем отвечает OS на нехватку потоков."""

        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    real_thread = pult.threading.Thread
    pult.threading.Thread = Boom
    code, detail = 0, ""
    try:
        pult.ui_paid_texts_collect(pult.CollectIn(limit=1))
    except HTTPException as exc:
        code, detail = exc.status_code, str(exc.detail)
    except Exception as exc:                                       # noqa: BLE001
        # Старое поведение: ошибка старта улетала наружу как есть — голый 500 без
        # слов, а флаг «идёт» оставался стоять. Ловим, чтобы это было видно строкой
        # «FAIL», а не обрывом прогона на полуслове.
        code, detail = 500, f"{type(exc).__name__}: {exc}"
    finally:
        pult.threading.Thread = real_thread
    check("сорвавшийся старт отвечает отказом, а не «идёт»", code, 503)
    check("и говорит, что денег это не стоило", "енег" in detail, True)
    check("флаг «идёт подбор» снят", pult._COLLECT["running"], False)
    check("причина записана в снимок", bool(pult._COLLECT["error"]), True)
    # Самое важное: следующее нажатие больше не упирается в 409.
    check("следующее нажатие не получает «уже идёт»",
          pult._collect_snapshot()["running"], False)


def donor_budget_part() -> None:
    """Бюджет проверки доноров тратится на вердикты, а не на украшения.

    Две отдельные ошибки, обе про один и тот же дефицит — время сетевой части.
    Первая: ссылка, про которую субтитры УЖЕ известны из кэша, но название не
    добывалось, уходила в сеть наравне с теми, про кого не известно ничего, — и
    `donor_title` (48,4 с при бюджете 20 с) забирал срок целиком. Вторая: при
    `VS_DONOR_CHECK_BUDGET_SEC=0` фоновый поток оставался без срока вообще.
    """
    print("\n8. Бюджет проверки доноров: сначала вердикты, потом названия")
    try:
        from web.app import donor_net_deadline, donor_second_pass  # noqa: PLC0415
    except Exception as exc:                                       # noqa: BLE001
        print(f"  ПРОПУЩЕНО: модуль пульта не поднялся ({type(exc).__name__}: {exc})")
        FAILS.append("проверка бюджета доноров не выполнена")
        return

    urls = ["u/named-ok", "u/nameless-ok", "u/unknown"]
    found = {
        "u/named-ok": {"verdict": "ok", "title": "Есть всё"},
        "u/nameless-ok": {"verdict": "ok", "title": ""},
        "u/unknown": {"verdict": "unknown", "title": "Название есть"},
    }
    need, cosmetic = donor_second_pass(found, urls)
    check("в сеть по существу идёт только непроверенное", need, [(2, "u/unknown")])
    check("безымянная, но проверенная ссылка — это косметика",
          cosmetic, [(1, "u/nameless-ok")])
    check("про кого всё известно — в сеть не идём вовсе",
          any(u == "u/named-ok" for _n, u in need + cosmetic), False)

    check("нулевой бюджет не значит «без срока»",
          donor_net_deadline(1000.0, 0, 30.0), 1030.0)
    check("пустой бюджет тоже", donor_net_deadline(1000.0, None, 30.0), 1030.0)
    check("заданный бюджет остаётся своим",
          donor_net_deadline(1000.0, 20.0, 30.0), 1020.0)


def main() -> int:
    print("Пульт: экраны для сделанного на сервере (браузер + стенд)\n")
    browser_part()
    if not stand_part():
        SKIPPED.append("раздел 5: ручки на живом стенде")
    unchecked_part()
    collect_start_part()
    donor_budget_part()
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    if SKIPPED and not NO_STAND_OK:
        print("СТЕНД НЕ ПОДНЯТ — проверки не выполнены:")
        for s in SKIPPED:
            print("  -", s)
        print("Это не успех. На стенде проверяются деньги и то, что сырой ответ соседа"
              " наружу не уходит. Поднимите стенд (см. заголовок файла) либо объявите"
              " пропуск осознанно: VS_PULT_TEST_NO_STAND=1.")
        return 2
    print("все проверки прошли" + (" (стенд пропущен осознанно)" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
