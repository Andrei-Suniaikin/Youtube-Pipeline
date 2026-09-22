#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выключенный исполнитель виден на экране, а не только в терминале.

Жалоба владельца 21.09.2026: «поставил озвучку ничего не сделалось, лог пуст».
Задача `#19 tts` по проекту `p_bc85c4bc85` действительно стояла `queued` 559 минут и
ни разу не была взята (`claim_token` пуст). Сломано ничего не было: стенд поднят через
`dev/run.sh`, где `VS_SERVER_DISABLE_BACKGROUND=1` стоит нарочно — там разбирают
интерфейс, а не тратят деньги.

Пустой лог здесь не улика поломки, а её отсутствие: логу неоткуда взяться, если задача
не начиналась. Но экран об этом молчал, и человек девять часов искал неисправность там,
где её нет. Кабинет про это состояние знает и пишет о нём в своей админ-панели — пульт
не показывал ничего.

Состояние читается у кабинета (`main.BACKGROUND_ENABLED`) — по тому самому значению,
которым планировщик решает, брать ли задачи. Посчитанное второй раз, оно рано или поздно
разойдётся с правдой, а экран, который врёт про рубильник, хуже отсутствующего.

Три состояния, и «не знаем» — отдельное от «выключен»: не сумев спросить, экран молчит,
а не пугает выдуманным отказом.

    .venv/bin/python tests/test_scheduler_visible.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(HERE, "helpers"))

import browser_harness                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class _Cabinet:
    """Подставной модуль кабинета: от него нужен ровно один флаг."""

    def __init__(self, enabled):
        self.BACKGROUND_ENABLED = enabled


def test_state() -> None:
    print("\nСостояние читается у кабинета, а не считается заново")
    os.environ.setdefault("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
    os.environ.setdefault("VS_SERVER_DB", os.environ["VS_DEV_DB"])
    from web import app                                           # noqa: PLC0415

    # Подменяем УЖЕ поднятый модуль кабинета: пульт спрашивает именно его и импорт
    # не форсирует (иначе миграции кабинета встают поперёк открытого соединения —
    # `database is locked`, ровно это и поймал test_project_source).
    import sys as _sys                                             # noqa: PLC0415
    was = _sys.modules.get("main")
    try:
        _sys.modules["main"] = _Cabinet(True)
        on = app._scheduler_state()
        check("исполнитель включён — состояние известно", on["known"], True)
        check("...и он включён", on["on"], True)
        check("...и объяснять нечего", on["why"], "")

        _sys.modules["main"] = _Cabinet(False)
        off = app._scheduler_state()
        check("исполнитель выключен — состояние известно", off["known"], True)
        check("...и он выключен", off["on"], False)
        check("...объяснение называет причину", "dev/run.sh" in off["why"], True)
        check("...и называет лекарство", "dev/run-live.sh" in off["why"], True)
        check("...и обещает, что поставленное не пропало",
              "никуда не денется" in off["why"], True)

        _sys.modules.pop("main", None)          # кабинет вообще не поднят
        unknown = app._scheduler_state()
        # «Не смогли спросить» — не «выключен». Выдуманный отказ хуже молчания.
        check("не смогли спросить — так и говорим", unknown["known"], False)
        check("...и не выдаём это за отказ", unknown["on"], True)
    finally:
        if was is None:
            _sys.modules.pop("main", None)
        else:
            _sys.modules["main"] = was


def test_endpoints_carry_it() -> None:
    print("\nСостояние доезжает до обоих экранов, где человек ждёт задачу")
    src = open(os.path.join(BASE, "web", "app.py"), encoding="utf-8").read()
    check("очередь отдаёт его", src.count('"scheduler": _scheduler_state(),') >= 2, True)


SCENARIO = r"""
(async () => {
  const out = {errs: []};
  addEventListener('error', e => out.errs.push('onerror: ' + e.message));
  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const finish = () => document.documentElement.setAttribute('data-result',
                                                             JSON.stringify(out));
  try {

  const карточка = (sch) => {
    const c = schedulerCard(sch);
    return c ? txt(c) : null;
  };

  out.включён = карточка({known: true, on: true, why: ''});
  out.неизвестно = карточка({known: false, on: true, why: ''});
  out.ничего_не_передали = карточка(undefined);
  const off = карточка({known: true, on: false,
                        why: 'Исполнитель задач выключен: стенд поднят через dev/run.sh.'});
  out.выключен_есть = off !== null;
  out.выключен_текст = off || '';
  // Сервер сам пишет «поставленное никуда не денется» — второй раз то же самое карточка
  // не повторяет (так и было до 21.09.2026: одна мысль двумя фразами подряд).
  const сервер = карточка({known: true, on: false, why: 'Исполнитель задач выключен: '
    + 'стенд поднят через dev/run.sh. Поставленное никуда не денется и начнётся само, '
    + 'когда стенд будет поднят через dev/run-live.sh.'}) || '';
  out.успокоений = (сервер.match(/не денется|не потеряется/g) || []).length;

  } catch (e) { out.errs.push('сценарий: ' + (e && e.message || e)); }
  finish();
})();
"""


def test_card() -> None:
    print("\nКарточка на экране: появляется только когда есть что сказать")
    res = browser_harness.run_scenario(SCENARIO, budget_ms=5000)
    if res.get("error"):
        print(f"  ПРОПУЩЕНО: {res['error']}")
        return
    check("ошибок в браузере нет", res.get("errs"), [])
    check("исполнитель работает — карточки нет", res.get("включён"), None)
    check("состояние неизвестно — карточки нет", res.get("неизвестно"), None)
    check("данных не пришло вовсе — карточки нет", res.get("ничего_не_передали"), None)
    check("выключен — карточка есть", res.get("выключен_есть"), True)
    check("...и называет, что задачи никто не выполняет",
          "Задачи никто не выполняет" in (res.get("выключен_текст") or ""), True)
    check("...и успокаивает, что поставленное не потеряется",
          "не потеряется" in (res.get("выключен_текст") or ""), True)
    check("...но ровно один раз, если причина уже это говорит",
          res.get("успокоений"), 1)


def main() -> int:
    print("Проверка: пульт говорит, когда задачи некому брать")
    test_state()
    test_endpoints_carry_it()
    test_card()
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
