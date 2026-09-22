#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Видео недоступно» — приговор донору только когда ролика действительно нет.

21.09.2026 владелец вставил ссылку, и пульт ответил «у ролика нет субтитров
(VideoUnavailable)», убрав донор в разбор. Проверка руками в ту же минуту, с того же
адреса и тем же выходом (`VS_SUBS_PROXIES=direct`) сняла с этого ролика 5429 знаков
расшифровки. То есть субтитры были, а строку похоронили.

Причина: `VideoUnavailable` стоял в закрытом списке `_no_subtitles`, то есть считался
свойством ролика. На деле библиотека отвечает так и когда YouTube просто не обслуживает
нас в этот момент.

Ошибка ровно того класса, о котором предупреждают соседние комментарии в том же файле:
«похоронить исправный донор из-за упавшего туннеля — этой ошибкой уже было убито сорок
с лишним выпусков» и «одна опечатка успела похоронить 48 исправных выпусков». Здесь
хоронил не туннель, а имя исключения.

Теперь перед приговором спрашивается улика — открытый oembed YouTube: на живой ролик он
отвечает 200 с названием, на удалённый 400 и пусто. Ключей и квоты не тратит.

Цена ошибок несимметрична, и тест это закрепляет: не смогли спросить — строка ждёт, а не
умирает. Лишнее ожидание стоит задержки, ложный приговор — исправного донора.

    .venv/bin/python tests/test_donor_unavailable.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
for _p in ("server", "shared", "workers"):
    sys.path.insert(0, os.path.join(BASE, _p))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


import pub_remote as pr                                            # noqa: E402
import pub_tags                                                    # noqa: E402


def with_oembed(title):
    """Подменяет улику: что oembed ответит про ролик."""
    was = pub_tags.donor_title
    pub_tags.donor_title = lambda url: title
    return was


def main() -> int:
    print("Проверка: «недоступно» требует улики, а не верится на слово")

    print("\nУлика: ролик жив (oembed вернул название)")
    was = with_oembed("Как вызвать инопланетян")
    try:
        check("хоронить нельзя", pr._video_really_gone("Oe0p_GZj3wg"), False)
    finally:
        pub_tags.donor_title = was

    print("\nУлика: ролика нет (oembed пуст)")
    was = with_oembed("")
    try:
        check("приговор обоснован", pr._video_really_gone("удалённый"), True)
    finally:
        pub_tags.donor_title = was

    print("\nУлику получить не смогли — строка ждёт, а не умирает")
    was = pub_tags.donor_title

    def _boom(url):
        raise RuntimeError("сеть недоступна")
    pub_tags.donor_title = _boom
    try:
        check("молчание сети не повод хоронить", pr._video_really_gone("Oe0p_GZj3wg"), False)
    finally:
        pub_tags.donor_title = was

    check("пустой идентификатор тоже не приговор", pr._video_really_gone(""), False)

    print("\nСписок двусмысленных имён — только про «недоступно»")
    check("VideoUnavailable двусмыслен",
          "VideoUnavailable" in pr._AMBIGUOUS_UNAVAILABLE, True)
    check("VideoUnplayable двусмыслен",
          "VideoUnplayable" in pr._AMBIGUOUS_UNAVAILABLE, True)
    # Эти три говорят именно про дорожки, а не про доступность ролика: для них улика не
    # нужна, и требовать её значило бы зря ходить в сеть на каждом честном отказе.
    for name in ("NoTranscriptFound", "TranscriptsDisabled", "NoTranscriptAvailable"):
        check(f"{name} остаётся однозначным", name in pr._AMBIGUOUS_UNAVAILABLE, False)
        check(f"...и по-прежнему считается отказом дорожек",
              pr._no_subtitles(type(name, (Exception,), {})()), True)

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
