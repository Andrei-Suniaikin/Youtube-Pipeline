#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строка очереди на ЭКРАНЕ: имя, стадия, сколько идёт и причина отказа.

Повод (20.09.2026). Владелец вставил ссылку в проекте, нажал «Получить текст» и
сказал: «нет сценария, просто висит задача в очереди, даже без названия». В базе
соседа в ту же минуту было другое: задача 0a6c6716dbd1 создана в 18:44, взята в
работу в 18:48, в логе «Подготовка → Обработка… → ru: старт — 1 частей», а
предыдущая 5a6a1f2eb6ba упала на сети («Ошибка: сеть недоступна (E-NET)»).
Работа шла — видно её не было.

Поля сервер отдаёт (`web/app.py`, проверено `tests/test_queue_row_visibility.py`).
Здесь проверяется ровно то, что делает с ними экран, и именно в браузере: разметка
строки собирается шаблоном, и «поле есть в ответе» ещё не значит «человек его видит».

Что должно быть верно и что ломалось:
  * имя, стадия и «сколько уже идёт» — в ОДНОЙ клетке, рядом. Задача, которая идёт
    двенадцатую минуту, должна выглядеть идущей, а не повисшей;
  * почему имя такое («в строке имя проекта») — подсказкой под именем, иначе человек
    читает имя проекта как имя ролика;
  * у упавшей — причина словами, а не только красный значок состояния;
  * `reason_pending` — это «НЕ ДОЧИТАЛИ причину», а не «причины нет». Врать тут
    нельзя: из-за этого всё и затевалось;
  * и это признак ТОЛЬКО соседских задач: своим (Видео, Выпуски) `web/app.py` его не
    ставит никогда — их этап и ошибку пишет сам кабинет в свою же базу, дочитывать
    нечего и не у кого. Находка рецензента 20.09.2026: подпись «у остальных помечено
    „не дочитано“» отвечала и за эти строки тоже, а у них на том же месте была голая
    пустота — то самое «причины нет», ради ухода от которого правка и делалась.
    Поэтому у своих строк своя правда: «этап не записан» и «причина не записана».

Ни сети, ни стенда, ни соседей: ответ сервера подменён, браузер настоящий.

    .venv/bin/python tests/test_queue_row_browser.py
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


def check_in(label, needle, haystack):
    if needle in (haystack or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали:   {needle!r}\n        в клетке: {haystack!r}")
        FAILS.append(label)


def check_not_in(label, needle, haystack):
    if needle not in (haystack or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        не должно было быть: {needle!r}"
              f"\n        в клетке: {haystack!r}")
        FAILS.append(label)


# Ответ /ui/queue — боевые строки владельца за 20.09.2026, слово в слово из его баз.
QUEUE = {
    "rows": [
        {"service": "rewrite", "id": "0a6c6716dbd1",
         "title": "Пробы пера", "title_why": "название ролика не сохранено: задачу "
                                             "поставили ссылкой с экрана проекта — "
                                             "в строке имя проекта",
         "kind": "text", "kind_title": "текст", "status": "running", "state": "running",
         "stage": "ru: старт — 1 частей", "error": "", "warning": "",
         "reason_pending": False, "progress": None, "provider": "", "user": "dev",
         "channel": "", "channel_id": None, "lang": "", "project_id": "p_a66a80ca50",
         "release_id": "", "created": 1758390240, "started": 1758390480,
         "finished": None, "waiting": 240, "working": 720,
         "age_text": "идёт 12 мин, ждала очереди 4 мин"},
        {"service": "rewrite", "id": "5a6a1f2eb6ba",
         "title": "Пробы пера", "title_why": "",
         "kind": "text", "kind_title": "текст", "status": "failed", "state": "error",
         "stage": "Проверка качества",
         "error": "Ошибка: сеть недоступна (E-NET) · 1 из 1 задач не выполнено "
                  "| no output files saved (0/1)",
         "warning": "", "reason_pending": False, "progress": None, "provider": "",
         "user": "dev", "channel": "Малоизвестное", "channel_id": 3, "lang": "RU",
         "project_id": "p_a66a80ca50", "release_id": "", "created": 1758386400,
         "started": 1758386460, "finished": 1758387060, "waiting": 60, "working": 600,
         "age_text": "заняла 10 мин"},
        {"service": "cover", "id": "c77e01",
         "title": "Как жили в осаде", "title_why": "",
         "kind": "cover", "kind_title": "обложка", "status": "done", "state": "done",
         "stage": "3/3 обложек", "error": "",
         "warning": "текст короче цели", "reason_pending": False, "progress": 1.0,
         "provider": "", "user": "dev", "channel": "Малоизвестное", "channel_id": 3,
         "lang": "RU", "project_id": "p_77", "release_id": "", "created": 1758380000,
         "started": 1758380060, "finished": 1758380660, "waiting": 60, "working": 600,
         "age_text": "заняла 10 мин"},
        # Бюджет показа кончился: причину у соседа не спрашивали вовсе.
        {"service": "rewrite", "id": "ee1122",
         "title": "Старая задача", "title_why": "",
         "kind": "text", "kind_title": "текст", "status": "failed", "state": "error",
         "stage": "", "error": "", "warning": "", "reason_pending": True,
         "progress": None, "provider": "", "user": "dev", "channel": "", "lang": "",
         "channel_id": None, "project_id": "", "release_id": "", "created": 1758300000,
         "started": 1758300060, "finished": 1758300660, "waiting": 60, "working": 600,
         "age_text": "заняла 10 мин"},
        # Идёт, но лог не дочитан: стадии нет — и это тоже надо сказать словами.
        {"service": "cover", "id": "ff3344",
         "title": "Ещё не названный", "title_why": "", "kind": "cover",
         "kind_title": "обложка", "status": "running", "state": "running",
         "stage": "", "error": "", "warning": "", "reason_pending": True,
         "progress": None, "provider": "", "user": "dev", "channel": "", "lang": "",
         "channel_id": None, "project_id": "", "release_id": "", "created": 1758389000,
         "started": 1758389060, "finished": None, "waiting": 60, "working": 600,
         "age_text": "идёт 10 мин"},
        # Своя задача кабинета: идёт, а этап воркер ещё не записал. Спрашивать
        # некого — `reason_pending` таким не ставят вовсе, и раньше здесь была пустота.
        {"service": "vsgen", "id": "j-771",
         "title": "Своя, идущая", "title_why": "", "kind": "images",
         "kind_title": "картинки", "status": "running", "state": "running",
         "stage": "", "error": "", "warning": "", "reason_pending": False,
         "progress": None, "provider": "", "user": "dev", "channel": "Малоизвестное",
         "lang": "RU", "channel_id": 3, "project_id": "p_77", "release_id": "",
         "created": 1758391000, "started": 1758391060, "finished": None,
         "waiting": 60, "working": 300, "age_text": "идёт 5 мин"},
        # Своя задача, упавшая без единого слова причины: её пишет наш же воркер, и
        # пусто значит «не записал», а не «мы не спросили».
        {"service": "vsgen", "id": "j-772",
         "title": "Своя, упавшая", "title_why": "", "kind": "render",
         "kind_title": "сборка", "status": "failed", "state": "error",
         "stage": "Сборка", "error": "", "warning": "", "reason_pending": False,
         "progress": None, "provider": "", "user": "dev", "channel": "Малоизвестное",
         "lang": "RU", "channel_id": 3, "project_id": "p_77", "release_id": "",
         "created": 1758390000, "started": 1758390060, "finished": 1758390660,
         "waiting": 60, "working": 600, "age_text": "заняла 10 мин"},
    ],
    "services": [{"key": "rewrite", "title": "Текст", "ok": True, "error": "", "count": 3},
                 {"key": "cover", "title": "Обложки", "ok": True, "error": "", "count": 2},
                 {"key": "vsgen", "title": "Видео", "ok": True, "error": "", "count": 2}],
    "channels": [{"id": 3, "name": "Малоизвестное"}],
    "states": [{"key": "running", "title": "в работе"}, {"key": "error", "title": "ошибка"}],
    "periods": [{"key": "1h", "title": "за час"}],
    "total": 7, "shown": 7,
    "details": {"asked": 3, "needed": 5, "budget": 3},
}


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  addEventListener('unhandledrejection',
                   e => out.errs.push('promise: ' + (e.reason && e.reason.message || e.reason)));
  const QUEUE = __QUEUE__;
  window.api = async (p) => {
    if (p.startsWith('/ui/queue-pause')) return {paused: false};
    if (p.startsWith('/ui/queue')) return QUEUE;
    return {};
  };

  await viewQueue();

  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  out.строк = document.querySelectorAll('#app tbody tr').length;
  out.над_списком = txt(document.querySelector('#q-details'));
  out.строки = [...document.querySelectorAll('#app tbody tr')].map(tr => ({
    сервис: txt(tr.children[0]),
    задача: txt(tr.children[1]),
    почему_имя: (tr.children[1].querySelector('.q') || {getAttribute: () => ''})
      .getAttribute('data-hint'),
    этап:   txt(tr.children[2]),
    состояние: txt(tr.children[3]),
  }));
  document.documentElement.setAttribute('data-result', JSON.stringify(out));
})();
"""


def main() -> int:
    res = browser_harness.run_scenario(SCENARIO.replace("__QUEUE__",
                                                        json.dumps(QUEUE, ensure_ascii=False)))
    if res.get("error"):
        print("НЕ ПРОГНАТЬ:", res["error"])
        return 2
    check("ошибок в консоли нет", res.get("errs"), [])
    check("строк в таблице", res.get("строк"), 7)
    rows = res.get("строки") or []
    if len(rows) != 7:
        print("строки не собрались:", rows)
        return 1
    идёт, упала, обложка, недочитана, безстадии, своя_идёт, своя_упала = rows

    print("Идущая задача выглядит идущей: имя, стадия и время — в одной клетке")
    check_in("имя ролика в строке", "Пробы пера", идёт["задача"])
    check_in("стадия рядом с именем", "ru: старт — 1 частей", идёт["задача"])
    check_in("сколько уже идёт — там же", "идёт 12 мин, ждала очереди 4 мин", идёт["задача"])
    check_in("что имя не из ролика — сказано коротко", "это не название ролика",
             идёт["задача"])
    check_in("а почему — под знаком вопроса", "в строке имя проекта",
             идёт.get("почему_имя") or "")
    check("и целой фразой в строке больше не стоит",
          "в строке имя проекта" in идёт["задача"], False)
    check_not_in("голого номера задачи вместо имени нет", "0a6c6716dbd1", идёт["задача"])
    check_in("вид задачи назван словом", "текст", идёт["этап"])

    print("\nУпавшая объясняет, ГДЕ встала и ПОЧЕМУ")
    check_in("причина словами", "сеть недоступна (E-NET)", упала["состояние"])
    check_in("итог соседа рядом с причиной", "no output files saved (0/1)", упала["состояние"])
    check_in("стадия — место обрыва, а не повтор причины", "Проверка качества", упала["задача"])
    check_not_in("причина не подменяет стадию", "E-NET", упала["задача"])
    check_in("сколько заняла", "заняла 10 мин", упала["задача"])

    print("\nПредупреждение при успехе — не отказ")
    check_in("предупреждение видно", "текст короче цели", обложка["состояние"])
    check_in("состояние осталось «готово»", "готово", обложка["состояние"])

    print("\n«Не дочитали» и «причины нет» — разное")
    check_in("причина помечена недочитанной", "причина не дочитана", недочитана["состояние"])
    check_not_in("«без причины» не пишем", "без причины", недочитана["состояние"])
    check_in("стадия тоже помечена", "стадия не дочитана", безстадии["задача"])
    check_in("подробности помечены", "подробности не дочитаны", безстадии["состояние"])
    check_in("но время работы честное", "идёт 10 мин", безстадии["задача"])

    print("\nСвоя задача — не соседская: у неё пусто по другой причине, и слова другие")
    check_in("идущая без этапа говорит это словами", "этап не записан", своя_идёт["задача"])
    check_not_in("но «не дочитали» ей не приписываем", "не дочитан", своя_идёт["задача"])
    check_in("и время работы честное", "идёт 5 мин", своя_идёт["задача"])
    check_in("упавшая без текста ошибки тоже объясняется",
             "причина не записана", своя_упала["состояние"])
    check_not_in("и это не «не дочитана»", "не дочитана", своя_упала["состояние"])
    check_not_in("а стадию свою она знает", "не записан", своя_упала["задача"])

    print("\nСколько строк осталось без подробностей — сказано один раз, над списком")
    check_in("счёт дочитанного", "дочитаны у 3 задач из 5", res.get("над_списком"))
    check_in("и что это значит", "это не «причины нет»", res.get("над_списком"))
    check_in("счёт назван соседским", "задач Текста и Обложек", res.get("над_списком"))
    check_in("а про свои сказано отдельно", "У Видео и Выпусков дочитывать нечего",
             res.get("над_списком"))

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  •", f)
        return 1
    print("Всё сошлось.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
