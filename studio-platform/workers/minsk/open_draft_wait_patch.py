#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yt_studio.open_draft_wizard: дождаться кнопку «Edit draft», а не кликать вслепую.

18.10.2026. Доведение черновика по номеру добралось до открытия мастера и встало:

    FINALIZE: мастер по videoId=ju9V-TY1neI не открылся — текстовый фолбэк
    finalize: черновик не открылся (exact-id-open-failed; text fallback forbidden)

По времени видно, что именно не сработало: от начала открытия до отказа прошло 12 секунд.
Перебор authuser тут ни при чём — он бы занял минимум 50 секунд на пяти номерах. Значит
страница /video/<id>/edit открылась с первой попытки (никаких «Oops» и «permission»), а
следом `click(SEL["edit_draft_btn"])` не нашёл кнопку и функция сразу вернула False.

Кнопку никто не ждёт: после `driver.get` стоит ровно `time.sleep(wait)` (10 секунд), и
если тяжёлая страница Studio к этому моменту не дорисовала шапку действий, клик уходит в
пустоту. Сама проверка «страница открылась» смотрит лишь на первые 120 символов body, то
есть проходит и на полузагруженной странице.

Что меняем:
  • ждём появления кнопки до EDIT_DRAFT_WAIT секунд (по умолчанию 30) и только потом
    кликаем — у Studio это обычная задержка отрисовки;
  • если за это время кнопки нет, пишем в журнал, какие кнопки на странице ЕСТЬ (их id и
    видимый текст) и заголовок страницы. Без этого следующий такой отказ снова
    невозможно разобрать: сообщение «не открылся» не говорит, что было на экране.
Поведение при успехе прежнее. Текстовый фолбэк и строгий запрет на него не тронуты.

Бэкап: yt_studio.py.bak-20261018-opendraftwait
"""
import os
import py_compile
import shutil
import sys

PATH = "/home/vova/VideoStudio/src/yt_studio.py"

OLD = '''    if not opened:
        logger.warning("open_draft_wizard: %s не открылся ни под одним authuser", video_id)
        return False
    if not click(driver, SEL["edit_draft_btn"]):
        return False
'''
NEW = '''    if not opened:
        logger.warning("open_draft_wizard: %s не открылся ни под одним authuser", video_id)
        return False
    # Кнопку «Edit draft» ждём: страница правки дорисовывает шапку действий заметно позже
    # готовности body, и клик сразу после паузы уходил в пустоту (разбор 18.10.2026).
    if wait_until(driver, lambda d: _js(d, "return !!deepFind(arguments[0], true);",
                                        SEL["edit_draft_btn"]),
                  timeout=int(os.environ.get("EDIT_DRAFT_WAIT", "30") or 30),
                  poll=2.0, desc="кнопка «Edit draft»") is None:
        try:
            seen = _js(driver, """
var out = [];
Array.prototype.forEach.call(document.querySelectorAll('ytcp-button, button'), function(e){
  var r = e.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) out.push((e.id || '-') + ':' + (e.innerText || '').trim().slice(0, 30));
});
return 'title=' + (document.title || '').slice(0, 60) + ' :: ' + out.slice(0, 12).join(' | ');
""")
        except Exception:                      # noqa: BLE001
            seen = "прочитать не удалось"
        logger.warning("open_draft_wizard: кнопки «Edit draft» (%s) нет на /video/%s/edit; "
                       "видимые кнопки страницы: %s", SEL["edit_draft_btn"], video_id, seen)
        return False
    if not click(driver, SEL["edit_draft_btn"]):
        logger.warning("open_draft_wizard: кнопка «Edit draft» есть, но клик не прошёл")
        return False
'''

check_only = "--check" in sys.argv
src = open(PATH, encoding="utf-8").read()
if "кнопка «Edit draft»" in src:
    print("патч уже применён — ничего не делаю")
    raise SystemExit(0)
n = src.count(OLD)
if n != 1:
    raise SystemExit(f"якорь встречается {n} раз, ожидался 1")
if "\nimport os" not in src and "\nimport os," not in src:
    raise SystemExit("в yt_studio.py нет import os — патч требует его")
out = src.replace(OLD, NEW)
if check_only:
    print("якорь на месте, патч применим (без записи)")
    raise SystemExit(0)
backup = PATH + ".bak-20261018-opendraftwait"
shutil.copy2(PATH, backup)
tmp = PATH + ".odw.tmp"
open(tmp, "w", encoding="utf-8").write(out)
py_compile.compile(tmp, doraise=True)
os.replace(tmp, PATH)
print("готово, бэкап", backup)
