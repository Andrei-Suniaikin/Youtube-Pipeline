#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Заливщик: доведение черновика по номеру опознаёт ролик по номеру, а не по названию.

18.10.2026. Семь черновиков висят в разборе: файл залит, ролик на канале есть, надо только
дописать расписание. Доведение по номеру (`--finalize-draft --finalize-video-id`) находило
строку правильно, переключалось на нужный канал через меню («Return to Studio») и падало:

    strict_existing_title_mismatch: candidate title is not exact; fresh upload forbidden

Почему это неверно именно для доведения. Такие черновики появились ровно потому, что
загрузка оборвалась ДО шага Details: поверх мастера встал «Verify it's you», наше название
в форму не попало, и Studio сохранила строку под именем файла (это прямо отмечено в самом
заливщике: «у прерванной загрузки заголовок часто = имя файла, а не наш title»). То есть
точного совпадения названия у черновика не может быть по построению, и проверка заваливает
каждую такую попытку.

При этом опознание в доведении уже строгое и без названия: `_strict_finalize_row` требует
РОВНО ОДНУ строку с нашим номером ролика, номер проверяется на формат, состояние строки
обязано быть DRAFT. Название — это то, что доведение сейчас впишет (шаг Details его
набирает), а не улика, по которой ролик ищут.

Что меняем:
  • `_strict_existing_blocker` получает флаг `identity_by_id`. С ним проверка названия
    перестаёт быть опознанием и становится записью в журнал: видно, что было в Studio и
    что мы впишем. Остальные ворота (формат номера, состояние строки) не ослаблены.
  • оба места доведения зовут его с этим флагом: `_strict_finalize_row` и повторная
    проверка найденной строки в дедупе.
Свежая заливка, обычный дедуп и сверка номера из мастера идут прежним путём: там названия
по-прежнему обязаны совпадать точно, потому что там ролик ищут именно по названию.

Бэкап: upload_worker.py.bak-20261018-finalizetitle
"""
import os
import py_compile
import shutil
import sys

PATH = "/home/vova/VideoStudio/src/upload_worker.py"

OLD_BLOCKER = '''def _strict_existing_blocker(row: dict, requested_title: str,
                             studio_state: str, expected_state: str) -> str:
    """A fuzzy match is a reason to stop, never permission to upload again."""
    if not isinstance(row, dict) or not _exact_title_proof(
        requested_title, row.get("title")
    )["matches"]:
        return "strict_existing_title_mismatch: candidate title is not exact; fresh upload forbidden"
'''
NEW_BLOCKER = '''def _strict_existing_blocker(row: dict, requested_title: str,
                             studio_state: str, expected_state: str,
                             identity_by_id: bool = False) -> str:
    """A fuzzy match is a reason to stop, never permission to upload again.

    identity_by_id=True — доведение черновика по номеру (18.10.2026): строка опознана
    по РОВНО ОДНОМУ совпадению нашего videoId, и название здесь не улика, а то, что
    шаг Details сейчас впишет. У прерванной загрузки заголовок черновика — имя файла,
    поэтому точного совпадения не бывает по построению. Остальные ворота те же.
    """
    if not isinstance(row, dict):
        return "strict_existing_title_mismatch: candidate title is not exact; fresh upload forbidden"
    _title_proof = _exact_title_proof(requested_title, row.get("title"))
    if not _title_proof["matches"]:
        if not identity_by_id:
            return "strict_existing_title_mismatch: candidate title is not exact; fresh upload forbidden"
        logger.warning(
            "FINALIZE: заголовок черновика в Studio отличается от нашего — опознаю по "
            "номеру ролика, название впишу на шаге Details. В Studio: %r, впишем: %r",
            str(row.get("title") or "")[:120], str(requested_title or "")[:120],
        )
'''

OLD_FINALIZE_ROW = '''    row = matches[0]
    blocker = _strict_existing_blocker(row, requested_title, YS.classify(row), "DRAFT")
'''
NEW_FINALIZE_ROW = '''    row = matches[0]
    blocker = _strict_existing_blocker(row, requested_title, YS.classify(row), "DRAFT",
                                      identity_by_id=True)
'''

OLD_DEDUP = '''                                    _blocker = _strict_existing_blocker(
                                        _v, title, _st_kind,
                                        "DRAFT" if finalize_video_id else (
                                            "SCHEDULED" if schedule_date is not None else VISIBILITY
                                        ),
                                    )
'''
NEW_DEDUP = '''                                    _blocker = _strict_existing_blocker(
                                        _v, title, _st_kind,
                                        "DRAFT" if finalize_video_id else (
                                            "SCHEDULED" if schedule_date is not None else VISIBILITY
                                        ),
                                        identity_by_id=bool(finalize_video_id),
                                    )
'''

check_only = "--check" in sys.argv
src = open(PATH, encoding="utf-8").read()
if "identity_by_id" in src:
    print("патч уже применён — ничего не делаю")
    raise SystemExit(0)
for old in (OLD_BLOCKER, OLD_FINALIZE_ROW, OLD_DEDUP):
    n = src.count(old)
    if n != 1:
        raise SystemExit(f"якорь встречается {n} раз, ожидался 1:\n{old[:90]}")
out = (src.replace(OLD_BLOCKER, NEW_BLOCKER)
          .replace(OLD_FINALIZE_ROW, NEW_FINALIZE_ROW)
          .replace(OLD_DEDUP, NEW_DEDUP))
if check_only:
    print("все 3 якоря на месте, патч применим (без записи)")
    raise SystemExit(0)
backup = PATH + ".bak-20261018-finalizetitle"
shutil.copy2(PATH, backup)
tmp = PATH + ".ftitle.tmp"
open(tmp, "w", encoding="utf-8").write(out)
py_compile.compile(tmp, doraise=True)
os.replace(tmp, PATH)
print("готово, бэкап", backup)
print("перезапуск пуллеров не нужен: заливщик запускается на каждое задание заново")
