#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Удержание автоподтверждения протухает через сутки (решение владельца 18.10.2026).

Что было. После неуспешной попытки «Verify it's you» пара профиль+канал попадает в
удержание: следующая попытка требует «клиренса» — отдельного файла-улики
`clearances/<профиль>.json`, свежего, не старше 15 минут и ещё не использованного. Такой
файл не пишет никто: ни заливщик, ни пуллер, ни человек. Значит выйти из удержания было
нечем, и пара запиралась навсегда.

Чем это обернулось 18.10: 20 выпусков в разборе с «previous confirmation is held», пять
профилей (26, 28, 34, 54, 63) по кругу открывают браузер, упираются в замок, через два часа
закрываются и открываются снова. Скрипт при этом работает правильно — он видит плашку
Verify, смотрит в журнал попыток и отказывается вводить пароль второй раз. Правило верное,
не хватало только срока.

Что меняем: удержание старше VIDEO_UPLOAD_AUTH_HOLD_TTL_SEC секунд (по умолчанию 86400,
сутки) перестаёт блокировать — разрешается одна новая попытка, которая сама станет новым
удержанием на следующие сутки. То есть на пару профиль+канал выходит не больше одной
попытки в сутки: от блокировки аккаунта за частый ввод пароля это защищает так же, а
вечного тупика больше нет. Ноль в переменной возвращает прежнее поведение «держать всегда».

Ничего другого не ослаблено: проверки личности, брони профиля и совпадения канала на
месте, история попыток по-прежнему только дописывается, клиренс (если он когда-нибудь
появится) по-прежнему снимает удержание немедленно.

Бэкап: upload_puller_auth.py.bak-20261018-holdttl
"""
import os
import py_compile
import shutil
import sys

PATH = "/home/vova/VideoStudio/src/upload_puller_auth.py"

OLD = '''            # Тот же выпуск после конфигурационного отказа тоже не держим: секреты не вводились.
            blocking = [a for a in blocking
                        if outcomes.get(a['attempt_id'], {}).get('reason_category') not in NO_HOLD_CATEGORIES]
'''
NEW = '''            # Тот же выпуск после конфигурационного отказа тоже не держим: секреты не вводились.
            blocking = [a for a in blocking
                        if outcomes.get(a['attempt_id'], {}).get('reason_category') not in NO_HOLD_CATEGORIES]
            # 18.10.2026, решение владельца: удержание старше суток считается протухшим.
            # Клиренс не пишет никто, поэтому без срока пара профиль+канал запиралась
            # навсегда (20 выпусков и пять профилей по кругу). Срок оставляет не больше
            # одной попытки в сутки на пару — от блокировки аккаунта это защищает так же.
            # VIDEO_UPLOAD_AUTH_HOLD_TTL_SEC=0 возвращает прежнее «держать всегда».
            _hold_ttl = float(os.environ.get('VIDEO_UPLOAD_AUTH_HOLD_TTL_SEC', '86400') or 0)
            if _hold_ttl > 0:
                _fresh_from = time.time() - _hold_ttl
                blocking = [a for a in blocking if float(a.get('at') or 0) >= _fresh_from]
'''

check_only = "--check" in sys.argv
src = open(PATH, encoding="utf-8").read()
if "VIDEO_UPLOAD_AUTH_HOLD_TTL_SEC" in src:
    print("патч уже применён — ничего не делаю")
    raise SystemExit(0)
n = src.count(OLD)
if n != 1:
    raise SystemExit(f"якорь встречается {n} раз, ожидался 1")
for need in ("import os", "import time"):
    if need not in src:
        raise SystemExit(f"в файле нет «{need}» — патч требует его")
out = src.replace(OLD, NEW)
if check_only:
    print("якорь на месте, патч применим (без записи)")
    raise SystemExit(0)
backup = PATH + ".bak-20261018-holdttl"
shutil.copy2(PATH, backup)
tmp = PATH + ".holdttl.tmp"
open(tmp, "w", encoding="utf-8").write(out)
py_compile.compile(tmp, doraise=True)
os.replace(tmp, PATH)
print("готово, бэкап", backup)
print("нужен перезапуск пуллеров: модуль читается при старте процесса")
