#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кнопка «Показать лог» и окно лога — в настоящем браузере.

Просьба владельца 20.09.2026, дословно: «можем добавить лог?». В тот день он полтора
часа чинил интернет, которого не ломал: заказ текста трижды падал с «Ошибка: сеть
недоступна (E-NET)», а настоящая причина —

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local
    issuer certificate

— лежала в СЫРОМ логе задачи рерайта и наружу не выходила: публичный фильтр соседа
меняет текст исключения на код. Саму поломку чинит `dev/run.sh` (связка certifi),
сырой лог отдаёт `GET /ui/jobs/{service}/{id}/log` (`tests/test_job_log.py`), а здесь
проверяется последнее звено — то, что видит человек:

  * кнопка есть у задач СОСЕДЕЙ (Текст, Обложки) и в очереди, и в списке задач
    проекта. У задач кабинета её нет: у них свой лог, своей ручкой, и подменять одно
    другим нельзя;
  * стоит она в клетке состояния — там же, где напечатана подменённая причина;
  * у упавшей задачи заметная, у прочих тихая: лог упавшей ищут первым делом;
  * переживает тик опроса. Тик перерисовывает клетку состояния целиком, и кнопка
    обязана уцелеть — иначе она пропадала бы ровно в тот миг, когда задача упала;
  * окно показывает сырой лог моноширинным текстом и говорит, ОТКУДА он. Если это не
    сырой файл, а публичный пересказ, — говорит это громко и даже тогда, когда сервер
    объяснения не прислал: молчание здесь возвращает исходную ловушку, человек снова
    примет код за причину;
  * обрезанный хвост назван обрезанным, с размерами и путём к целому файлу;
  * чтение бесплатно: за всё окно не уходит ни одного POST, а «Обновить» перечитывает
    лог тем же GET.

Ни сети, ни стенда, ни соседей: ответы сервера подменены, браузер настоящий.

    .venv/bin/python tests/test_job_log_browser.py
"""
from __future__ import annotations

import json
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


def has(label, got, needle):
    if needle in (got or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали:   {needle!r}\n        в тексте: {got!r}")
        FAILS.append(label)


def hasnt(label, got, needle):
    if needle not in (got or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        не должно было быть: {needle!r}"
              f"\n        а в тексте: {got!r}")
        FAILS.append(label)


# Очередь того самого вечера: две задачи рерайта (упавшая и идущая), обложка, своя
# задача кабинета и выпуск. Поля — как их отдаёт `GET /ui/queue` (web/app.py).
QUEUE = {
    "rows": [
        {"service": "rewrite", "id": "5a6a1f2eb6ba", "title": "Пробы пера",
         "title_why": "", "kind": "text", "kind_title": "текст", "status": "failed",
         "state": "error", "stage": "Проверка качества",
         "error": "Ошибка: сеть недоступна (E-NET) · 1 из 1 задач не выполнено",
         "warning": "", "reason_pending": False, "progress": None, "provider": "",
         "user": "dev", "channel": "", "channel_id": None, "lang": "",
         "project_id": "p1", "release_id": "", "created": 1758386400,
         "age_text": "заняла 10 мин", "times_known": True},
        {"service": "rewrite", "id": "0a6c6716dbd1", "title": "Пробы пера",
         "title_why": "", "kind": "text", "kind_title": "текст", "status": "running",
         "state": "running", "stage": "ru: старт — 1 частей", "error": "", "warning": "",
         "reason_pending": False, "progress": None, "provider": "", "user": "dev",
         "channel": "", "channel_id": None, "lang": "", "project_id": "p1",
         "release_id": "", "created": 1758390240, "age_text": "идёт 12 мин",
         "times_known": True},
        {"service": "cover", "id": "c77e01", "title": "Как жили в осаде",
         "title_why": "", "kind": "cover", "kind_title": "обложка", "status": "failed",
         "state": "error", "stage": "", "error": "нет ключа провайдера", "warning": "",
         "reason_pending": False, "progress": None, "provider": "", "user": "dev",
         "channel": "", "channel_id": None, "lang": "", "project_id": "p1",
         "release_id": "", "created": 1758380000, "age_text": "заняла 10 мин",
         "times_known": True},
        {"service": "vsgen", "id": "j-772", "title": "Своя, упавшая", "title_why": "",
         "kind": "render", "kind_title": "сборка", "status": "failed", "state": "error",
         "stage": "Сборка", "error": "", "warning": "", "reason_pending": False,
         "progress": None, "provider": "", "user": "dev", "channel": "",
         "channel_id": None, "lang": "", "project_id": "p1", "release_id": "",
         "created": 1758390000, "age_text": "заняла 10 мин", "times_known": True},
        {"service": "release", "id": "rel-1", "title": "Выпуск канала", "title_why": "",
         "kind": "release", "kind_title": "выпуск", "status": "running",
         "state": "running", "stage": "ожидание", "error": "", "warning": "",
         "reason_pending": False, "progress": None, "provider": "", "user": "",
         "channel": "Малоизвестное", "channel_id": 3, "lang": "RU",
         "project_id": "p1", "release_id": "r1", "plan_item_id": 5,
         "created": 1758391000, "age_text": "идёт 5 мин", "times_known": True},
    ],
    "services": [{"key": "rewrite", "title": "Текст", "ok": True, "error": "", "count": 2},
                 {"key": "cover", "title": "Обложки", "ok": True, "error": "", "count": 1},
                 {"key": "vsgen", "title": "Видео", "ok": True, "error": "", "count": 1}],
    "channels": [{"id": 3, "name": "Малоизвестное"}],
    "states": [{"key": "error", "title": "ошибка"}],
    "periods": [{"key": "1h", "title": "за час"}],
    "total": 5, "shown": 5,
    "details": {"asked": 3, "needed": 3, "budget": 3},
}

# Сырой лог — строка в строку из `rewrite/job_logs/dev/0a6c6716dbd1.log` того вечера,
# уже пропущенный через редактуру сервера (ключ вырезан им, а не здесь).
RAW = ("[19:41:02] 🌍 ru: начинаю (1 частей)\n"
       "[19:41:02] POST https://api.openai.com/v1/responses key=[REDACTED]\n"
       "[19:41:09] ❌ ru: сетевая ошибка при обращении к провайдеру: "
       "SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate "
       "verify failed: unable to get local issuer certificate (_ssl.c:1000)')\n")
# А это то, что человек видел на экране всё это время.
PUBLIC = ("[19:41:02] Задача принята\n"
          "[19:41:02] ru: старт — 1 частей\n"
          "[19:41:09] Ошибка: сеть недоступна (E-NET)\n")

SCENARIO = r"""
(async () => {
  const out = {errs: [], gets: [], posts: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const QUEUE = __QUEUE__, RAW = __RAW__, PUBLIC = __PUBLIC__;

  // Ответы ручки лога — ровно того вида, что отдаёт `GET /ui/jobs/{svc}/{id}/log`.
  const ЛОГИ = {
    '5a6a1f2eb6ba': {service: 'rewrite', job_id: '5a6a1f2eb6ba', status: 'failed',
      error: '1 из 1 задач не выполнено', source: 'raw',
      why: 'сырой лог задачи с диска — то, что писал сам пайплайн', problem: '',
      path: '/srv/rewrite/job_logs/dev/5a6a1f2eb6ba.log', text: RAW,
      bytes: 421, chars: 300, total_bytes: 421, truncated: false,
      redacted: 'точная и по образцу'},
    'публичный': {service: 'rewrite', job_id: 'x1', status: 'failed',
      error: '1 из 1 задач не выполнено', source: 'public',
      why: 'ПУБЛИЧНЫЙ лог: настоящая причина заменена кодом (E-NET, E-TIMEOUT и т. п.). '
         + 'Сырой лежит файлом на машине рерайта — /srv/rewrite/job_logs/dev/x1.log',
      problem: 'файла сырого лога здесь нет: либо рерайт работает на другой машине, '
             + 'либо задача ещё ничего не писала',
      path: '/srv/rewrite/job_logs/dev/x1.log', text: PUBLIC,
      bytes: 120, chars: 90, total_bytes: 120, truncated: false, redacted: 'только по образцу'},
    'без_объяснения': {service: 'rewrite', job_id: 'x2', status: 'failed', error: '',
      source: 'public', why: '', problem: '', path: '', text: PUBLIC,
      bytes: 120, chars: 90, total_bytes: 120, truncated: false, redacted: ''},
    'хвост': {service: 'rewrite', job_id: 'x3', status: 'failed', error: '',
      source: 'raw', why: 'сырой лог задачи с диска — то, что писал сам пайплайн',
      problem: '', path: '/srv/rewrite/job_logs/dev/x3.log', text: RAW,
      bytes: 6395, chars: 4000, total_bytes: 204800, truncated: true,
      redacted: 'точная и по образцу'},
    'пусто': {service: 'rewrite', job_id: 'x4', status: 'pending', error: '',
      source: 'public', why: 'ПУБЛИЧНЫЙ лог: настоящая причина заменена кодом',
      problem: 'лог пуст: задача ещё не начиналась или ничего не записала',
      path: '', text: '', bytes: 0, chars: 0, total_bytes: 0, truncated: false,
      redacted: 'точная и по образцу'},
    'c77e01': {service: 'cover', job_id: 'c77e01', status: 'failed',
      error: 'нет ключа провайдера', source: 'service',
      why: 'лог задачи, как его отдаёт Cover: у него он один и не фильтруется',
      problem: '', path: '', text: "Обложка: старт\nTraceback: KeyError('api_key')\n",
      bytes: 60, chars: 48, total_bytes: 60, truncated: false,
      redacted: 'точная и по образцу'},
  };
  let ЛОГ = ЛОГИ['5a6a1f2eb6ba'];
  let ЛОГ_ПАДАЕТ = '';
  let СПИСОК = null;                       // ответ `/ui/text?project_id=` для списка задач

  window.api = async (p) => {
    out.gets.push(p);
    if (p.startsWith('/ui/queue-pause')) return {paused: false};
    if (p.startsWith('/ui/queue')) return QUEUE;
    if (p.indexOf('/log') > 0 && p.startsWith('/ui/jobs/')){
      if (ЛОГ_ПАДАЕТ) throw new Error(ЛОГ_ПАДАЕТ);
      return ЛОГ;
    }
    if (p.startsWith('/ui/text')) return СПИСОК;
    return {};
  };
  window.post = async (p, b) => { out.posts.push({path: p, body: b}); return {ok: true}; };

  const flat = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const pause = (ms) => new Promise(r => setTimeout(r, ms));
  const окно = () => document.querySelector('dialog.modal');
  const закрыть = async () => { const d = окно(); if (d) d.querySelector('.m-no').click();
                                await pause(50); };
  // Открыть лог и дождаться ответа. Возвращает снимок окна: что в нём написано, какими
  // классами (цвет подписи — это и есть «громко»), и моноширинный ли текст лога.
  const открытьЛог = async (svc, id, ключ) => {
    ЛОГ = ЛОГИ[ключ] || ЛОГ;
    await showJobLog(svc, id);
    await pause(200);
    const d = окно();
    const pre = d && d.querySelector('pre');
    const первая = d && d.querySelector('.mbody > div');
    const snap = {
      заголовок: flat(d && d.querySelector('.sec')),
      тело: flat(d && d.querySelector('.mbody')),
      первая_класс: первая ? первая.className : '',
      // Не просто «моноширинный»: у <pre> он и по умолчанию такой. Проверяем, что
      // лог показан ТОЙ ЖЕ моноширинной гарнитурой, что и остальной пульт (`.mono`),
      // и что длинные строки переносятся, а не уезжают за край окна.
      моно: pre ? /ui-monospace/.test(getComputedStyle(pre).fontFamily) : false,
      переносы: pre ? getComputedStyle(pre).whiteSpace : '',
      кнопки: d ? [...d.querySelectorAll('.mfoot button')].map(b => b.textContent) : [],
    };
    return snap;
  };

  try{
  // ── 1. Очередь: у кого кнопка есть и какая ──────────────────────────────────
  await viewQueue();
  out.строки = [...document.querySelectorAll('#app tbody tr')].map(tr => {
    const b = tr.querySelector('button.joblog');
    return {задача: flat(tr.children[1]).slice(0, 24),
            состояние: flat(tr.children[3]),
            кнопка: b ? b.textContent.trim() : '',
            заметная: b ? !b.classList.contains('ghost') : null,
            в_клетке_состояния: b ? (b.closest('td') === tr.children[3]) : null,
            сервис: b ? b.dataset.service : '', задача_id: b ? b.dataset.job : ''};
  });

  // ── 2. Нажатие в строке очереди открывает окно с сырым логом ────────────────
  const былоPost = out.posts.length;
  document.querySelector('#app tbody tr button.joblog').click();
  await pause(250);
  const d1 = окно();
  out.клик_окно_открылось = !!d1;
  out.клик_заголовок = flat(d1 && d1.querySelector('.sec'));
  out.клик_тело = flat(d1 && d1.querySelector('.mbody'));
  out.клик_ничего_не_записал = (out.posts.length === былоPost);
  // «Обновить» перечитывает тем же GET — это бесплатно и безопасно.
  const былоGet = out.gets.filter(p => p.indexOf('/log') > 0).length;
  d1.querySelector('.m-ok').click();
  await pause(250);
  out.обновить_перечитало =
    out.gets.filter(p => p.indexOf('/log') > 0).length - былоGet;
  await закрыть();
  out.окно_закрылось = !окно();

  // ── 3. Что в окне написано: сырой лог ───────────────────────────────────────
  out.сырой = await открытьЛог('rewrite', '5a6a1f2eb6ba', '5a6a1f2eb6ba');
  await закрыть();

  // ── 4. Публичный лог: об этом говорят громко ────────────────────────────────
  out.публичный = await открытьЛог('rewrite', 'x1', 'публичный');
  await закрыть();
  // И даже когда сервер объяснения не прислал: молчание тут возвращает ловушку.
  out.без_объяснения = await открытьЛог('rewrite', 'x2', 'без_объяснения');
  await закрыть();

  // ── 5. Хвост, пустота, Cover, отказ ручки ───────────────────────────────────
  out.хвост = await открытьЛог('rewrite', 'x3', 'хвост');
  await закрыть();
  out.пусто = await открытьЛог('rewrite', 'x4', 'пусто');
  await закрыть();
  out.обложка = await открытьЛог('cover', 'c77e01', 'c77e01');
  await закрыть();
  ЛОГ_ПАДАЕТ = 'нет такой задачи';
  out.отказ = await открытьЛог('rewrite', 'нет', 'пусто');
  ЛОГ_ПАДАЕТ = '';
  await закрыть();

  // ── 6. Список задач текста проекта: кнопка есть и переживает тик опроса ─────
  const job = (st) => ({id: 'rw-1', kind: 'rewrite', title: 'Пробы пера', status: st,
                        state: st, progress: null, detail: '', langs: ['ru'],
                        created: 1758390240});
  СПИСОК = {jobs: [job('running')]};
  const box = document.createElement('div');
  app.innerHTML = '';
  app.append(box);
  await refreshScenarioJobs(box, 'p1', null);
  await pause(200);
  const кн = () => box.querySelector('tr[data-job] button.joblog');
  out.список_кнопка = кн() ? кн().textContent.trim() : '';
  out.список_сервис = кн() ? кн().dataset.service : '';
  out.список_задача = кн() ? кн().dataset.job : '';
  out.список_тихая = кн() ? кн().classList.contains('ghost') : null;
  // Задача упала — тик перерисовывает клетку состояния. Кнопка обязана уцелеть и
  // стать заметной: именно здесь её и будут искать.
  СПИСОК = {jobs: [job('error')]};
  await pause(3600);
  out.после_тика_кнопка = кн() ? кн().textContent.trim() : '';
  out.после_тика_заметная = кн() ? !кн().classList.contains('ghost') : null;
  out.после_тика_состояние = flat(box.querySelector('.jl-state'));
  // Нажатие на кнопку не раскрывает строку результатов: это разные действия.
  const былоСтрок = box.querySelectorAll('tr').length;
  кн().click();
  await pause(250);
  out.список_окно = !!окно();
  out.список_строк_не_прибавилось = (box.querySelectorAll('tr').length === былоСтрок);
  await закрыть();

  out.postов_всего = out.posts.length;

  }catch(e){ out.error = String((e && e.stack) || e); }
  clearAllLogTimers();
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    scenario = (SCENARIO
                .replace("__QUEUE__", json.dumps(QUEUE, ensure_ascii=False))
                .replace("__RAW__", json.dumps(RAW, ensure_ascii=False))
                .replace("__PUBLIC__", json.dumps(PUBLIC, ensure_ascii=False)))
    res = browser_harness.run_scenario(scenario, budget_ms=30000)
    if res.get("error"):
        print("НЕ ПРОГНАТЬ:", res["error"])
        return 2
    check("сценарий без ошибок в консоли", res.get("errs"), [])
    rows = res.get("строки") or []
    if len(rows) != 5:
        print("строки очереди не собрались:", rows)
        return 1
    упал_текст, идёт_текст, упала_обложка, своя, выпуск = rows

    print("1. Кнопка есть у задач соседей — и там, где человек читает причину")
    check("у упавшей задачи текста", упал_текст["кнопка"], "Показать лог")
    check("и она заметная — лог упавшей ищут первым делом",
          упал_текст["заметная"], True)
    check("стоит рядом с причиной, в клетке состояния",
          упал_текст["в_клетке_состояния"], True)
    has("а причина на экране — та самая подменённая", упал_текст["состояние"],
        "сеть недоступна (E-NET)")
    check("сервис и номер задачи в кнопке названы",
          (упал_текст["сервис"], упал_текст["задача_id"]), ("rewrite", "5a6a1f2eb6ba"))
    check("у идущей задачи кнопка тоже есть", идёт_текст["кнопка"], "Показать лог")
    check("но тихая: там смотрят «как идёт», а не «что случилось»",
          идёт_текст["заметная"], False)
    check("у обложек кнопка есть", упала_обложка["кнопка"], "Показать лог")
    check("и сервис у неё свой", упала_обложка["сервис"], "cover")
    check("у задачи кабинета этой кнопки нет — у неё свой лог", своя["кнопка"], "")
    check("и у выпуска нет — это не задача сервиса", выпуск["кнопка"], "")

    print("\n2. Нажатие открывает окно; чтение ничего не записывает")
    check("окно открылось", res.get("клик_окно_открылось"), True)
    has("и названо задачей, чей это лог", res.get("клик_заголовок"),
        "Лог задачи · Текст · 5a6a1f2eb6ba")
    has("в нём настоящая причина, а не её код", res.get("клик_тело"),
        "CERTIFICATE_VERIFY_FAILED")
    check("ни одной записи на сервер", res.get("клик_ничего_не_записал"), True)
    check("«Обновить» перечитывает лог", res.get("обновить_перечитало"), 1)
    check("окно закрывается", res.get("окно_закрылось"), True)

    print("\n3. Сырой лог: причина, моноширинно и с подписью, откуда он")
    сырой = res.get("сырой") or {}
    has("та половина причины, которую ищут глазами", сырой.get("тело"),
        "unable to get local issuer certificate")
    hasnt("кода вместо причины в окне нет", сырой.get("тело"), "E-NET")
    has("сказано, что это сырой лог с диска", сырой.get("тело"),
        "сырой лог задачи с диска")
    check("подпись спокойная — подменять тут нечего",
          сырой.get("первая_класс"), "note muted")
    check("лог показан моноширинным — гарнитурой пульта, а не браузерной по умолчанию",
          сырой.get("моно"), True)
    check("длинные строки переносятся, а не уезжают за край",
          сырой.get("переносы"), "pre-wrap")
    has("сказано, что ключи вырезаны", сырой.get("тело"), "Ключи и токены из лога вырезаны")
    check("кнопки окна — перечитать и закрыть", сырой.get("кнопки"),
          ["Обновить", "Закрыть"])

    print("\n4. Публичный лог назван публичным — иначе вся правка обнуляется")
    публ = res.get("публичный") or {}
    has("сказано прямо", публ.get("тело"), "ПУБЛИЧНЫЙ лог")
    has("и чем заменена причина", публ.get("тело"), "заменена кодом")
    has("и где лежит настоящий", публ.get("тело"), "/srv/rewrite/job_logs/dev/x1.log")
    check("подпись громкая, а не проходная", публ.get("первая_класс"), "note warn")
    has("почему сырого нет — тоже сказано", публ.get("тело"), "на другой машине")
    has("сам публичный лог показан как есть", публ.get("тело"), "E-NET")
    без = res.get("без_объяснения") or {}
    has("сервер промолчал — предупреждает экран", без.get("тело"),
        "НЕ сырой лог задачи")
    check("и тоже громко", без.get("первая_класс"), "note warn")

    print("\n5. Обрезка, пустота, Cover и отказ ручки — каждый со своими словами")
    хвост = res.get("хвост") or {}
    has("обрезку назвали обрезкой", хвост.get("тело"), "Показан только конец лога")
    has("с размерами", хвост.get("тело"), "6 КБ из 200 КБ")
    has("и с путём к целому файлу", хвост.get("тело"),
        "/srv/rewrite/job_logs/dev/x3.log")
    пусто = res.get("пусто") or {}
    has("пустой лог объясняет себя", пусто.get("тело"), "лог пуст")
    has("а не притворяется логом", пусто.get("тело"), "Показывать нечего")
    обл = res.get("обложка") or {}
    has("у Cover сказано, что лог его и не фильтруется", обл.get("тело"),
        "у него он один и не фильтруется")
    has("и сам лог показан", обл.get("тело"), "KeyError('api_key')")
    отказ = res.get("отказ") or {}
    has("отказ ручки — словами", отказ.get("тело"), "Лог не прочитался: нет такой задачи")
    has("и сказано, что повтор ничего не стоит", отказ.get("тело"),
        "бесплатно и ничего не запускает")

    print("\n6. Список задач проекта: кнопка есть и переживает тик опроса")
    check("кнопка в строке задачи текста", res.get("список_кнопка"), "Показать лог")
    check("сервис и номер названы",
          (res.get("список_сервис"), res.get("список_задача")), ("rewrite", "rw-1"))
    check("у идущей она тихая", res.get("список_тихая"), True)
    check("после тика опроса кнопка на месте", res.get("после_тика_кнопка"),
          "Показать лог")
    check("и стала заметной — задача упала", res.get("после_тика_заметная"), True)
    has("состояние тик тоже обновил", res.get("после_тика_состояние"), "ошибка")
    check("нажатие открыло окно лога", res.get("список_окно"), True)
    check("и не раскрыло строку результатов — это разные действия",
          res.get("список_строк_не_прибавилось"), True)
    check("за всё окно ни одной записи на сервер", res.get("postов_всего"), 0)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО {len(FAILS)}: " + "; ".join(FAILS))
        return 1
    print("всё сходится")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
