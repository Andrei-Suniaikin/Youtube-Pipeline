#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фаза 4 (task-spec.md): настройки канала открыты сразу (§5), отступы карточек (§6),
бюджет статуса FastGen в форме и .gitignore для job_logs (доработки 4–5, Дополнение
16.09).

Стенд не нужен: §5 и §6 проверяются статически по исходнику `web/index.html» (тот же
приём, что в `tests/test_channel_without_id.py` и `tests/test_fastgen_usage.py» — читаем
файл и ищем ожидаемую разметку/CSS), доработка 4 — прямым вызовом `preset_fields`, а
доработка 5 — `git check-ignore` без создания реальных файлов (приём из
`tests/test_gpt_image_25.py»).

    .venv/bin/python tests/test_channel_settings_layout.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
ROOT = os.path.abspath(os.path.join(BASE, ".."))          # корень репозитория (.gitignore)
sys.path.insert(0, os.path.join(BASE, "server"))

import preset_fields as PF                                        # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def function_body(src: str, signature: str) -> str:
    """Текст функции от строки с сигнатурой до строки с одинокой закрывающей `}` —
    тем же грубым приёмом, что используют существующие тесты для index.html: файл не
    настоящий JS AST, но верхнеуровневые функции в нём не вкладываются друг в друга."""
    lines = src.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(signature)), None)
    if start is None:
        return ""
    out = []
    for ln in lines[start:]:
        out.append(ln)
        if ln == "}":
            break
    return "\n".join(out)


def main() -> int:
    index_html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()

    print("§5. Настройки канала открыты сразу, без кнопки «Настроить»")

    view_channel = function_body(index_html, "async function viewChannel(id){")
    check("viewChannel() найдена", bool(view_channel), True)
    check("кнопки «Настроить» больше нет в шапке канала",
          'id="edit"' in view_channel, False)
    check("старая сводка (грид «Расписание») из шапки убрана",
          '<div class="k">Расписание</div>' in view_channel, False)
    check("форма встроена сразу в шапку (editbox есть без промежуточного тумблера)",
          'id="editbox"' in view_channel, True)

    wire_edit = function_body(index_html, "async function wireChannelEdit(root, c){")
    check("wireChannelEdit() найдена", bool(wire_edit), True)
    check("тумблера «свернуть/показать форму» по клику на #edit больше нет",
          "root.querySelector('#edit')" in wire_edit, False)
    check("старой проверки box.innerHTML для сворачивания формы нет",
          "if (box.innerHTML)" in wire_edit, False)
    check("поле «Профиль AdsPower» осталось в форме редактируемым",
          "'Профиль AdsPower'" in wire_edit or '"Профиль AdsPower"' in wire_edit, True)
    check("поле «Машина заливки» осталось в форме редактируемым",
          "'Машина заливки'" in wire_edit or '"Машина заливки"' in wire_edit, True)

    # Вкладка «Ролики» не должна потеряться вместе с кнопкой «Настроить»: до правки
    # viewChannel() выходила из функции раньше показа настроек, если открыта вкладка
    # «Ролики» (`if (tab === 'roliki'){ await channelReleases(id, d); return; }`).
    # Если этот маршрут пропал вовсе — вкладка «Ролики» всегда показывает форму
    # настроек вместо календаря выпусков, что само ТЗ §5 не просило.
    check("вкладка «Ролики» по-прежнему ведёт на channelReleases (список/календарь), "
          "а не всегда на форму настроек",
          "channelReleases(" in view_channel, True)

    print("\n§6. Отступы карточек — 18px по бокам, 16px снизу, в тон .sec")

    check("у .fields снят нулевой боковой отступ (было padding:6px 0 4px)",
          re.search(r"\.fields\{[^}]*padding:6px 0 4px", index_html) is None, True)
    check(".fields получил боковой 18px и нижний 16px",
          bool(re.search(r"\.fields\{[^}]*padding:6px 18px 16px", index_html)), True)
    check("класс .secbody объявлен с отступом 18px/16px",
          bool(re.search(r"\.secbody\{padding:0 18px 16px\}", index_html)), True)

    spots = [
        ('шапка канала (editbox)', '<div id="editbox" class="secbody">'),
        # Ищем пару «заголовок → тело», а не соседство с textarea: над полем ссылок
        # может стоять предупреждение о том, чего не хватает каналу.
        ('«Поставить ролики»',
         '<div class="sec">Поставить ролики</div>\n    <div class="secbody">'),
        ('новый проект', '<div class="sec">Новый проект</div>\n      <div class="secbody">'),
        ('перенос каналов списком',
         '<div class="sec">Перенести каналы списком</div>\n      <div class="secbody">'),
        ('новый канал', '<div class="sec">Новый канал</div>\n      <div class="secbody">'),
        ('«Сделать всё» проекта', '<div class="sec">Сделать всё</div>\n    <div class="secbody">'),
        ('«Завести человека»',
         'Завести человека</div>\n    <div class="secbody">'),
        ('новый пресет (поле названия)',
         '<div class="secbody">\n    <input id="p-name" placeholder='),
        ('существующий пресет (поле названия)',
         '<div class="secbody">\n    <input id="p-name" value='),
        ('профиль: карточка «Ключи провайдеров»',
         'class="note muted secbody">Ключи принадлежат человеку'),
        ('профиль: карточка «Система»', 'class="note muted secbody">'),
    ]
    for title, needle in spots:
        check(f"отступ карточки применён: {title}", needle in index_html, True)

    # .fields несёт собственный отступ (проверено выше) — если бы карточку с ним ещё и
    # оборачивали в .secbody, отступ бы удвоился (36px вместо 18px). Проверяем ровно
    # функцию, которая строит .fields (общая форма пресета/проекта/канала).
    field_form = function_body(index_html, "function fieldForm(groups){")
    check("fieldForm() найдена", bool(field_form), True)
    check("класс .fields в разметке fieldForm() есть", 'class="fields"' in field_form, True)
    check("fieldForm() не оборачивает свои карточки вторым классом .secbody "
          "(иначе отступ утроился бы)",
          "secbody" in field_form, False)

    # Таблицы остаются «от края до края» — секция явно оставляет их без нового отступа.
    table_lines_with_secbody = [ln for ln in index_html.splitlines()
                                if "<table" in ln and "secbody" in ln]
    check("ни одна строка с <table не получила класс .secbody",
          table_lines_with_secbody, [])

    print("\nДоработка 4 (Дополнение 16.09): статус FastGen в форме — с бюджетом времени")

    check("бюджет — положительное число не больше 3 секунд, как велит доработка 4",
          0 < PF._FG_FORM_BUDGET_SEC <= 3.0, True)

    class Cabinet:
        IMAGE_MODELS = {"NARWHAL": ("Nano Banana 2 · Flow", "4 кр", "recrafter")}

        @staticmethod
        def keys():
            return {"googler": "fg_live_test"}

        @staticmethod
        def _first_credential(cfg, family):
            return cfg.get(family, "")

    class CabinetFast(Cabinet):
        @staticmethod
        def fastgen_status(key):
            return {"models": [{"id": "NARWHAL", "txt": "работает"}]}

    t0 = time.monotonic()
    fast = dict(PF._image_models(CabinetFast))
    elapsed_fast = time.monotonic() - t0
    check("быстрый ответ — статус реальный, а не «нет данных»",
          fast["NARWHAL"], "Nano Banana 2 · Flow · 4 кр · работает")
    check("быстрый ответ не ждёт бюджет целиком",
          elapsed_fast < PF._FG_FORM_BUDGET_SEC, True)

    class CabinetHang(Cabinet):
        @staticmethod
        def fastgen_status(key):
            time.sleep(20)                      # заметно дольше бюджета в 3с
            return {"models": [{"id": "NARWHAL", "txt": "работает"}]}

    t0 = time.monotonic()
    hung = dict(PF._image_models(CabinetHang))
    elapsed_hang = time.monotonic() - t0
    check("зависший провайдер — «нет данных», форма не ждёт ответа",
          hung["NARWHAL"], "Nano Banana 2 · Flow · 4 кр · нет данных")
    check("время ожидания уложилось в бюджет (не 20с)",
          elapsed_hang < PF._FG_FORM_BUDGET_SEC + 1, True)

    print("\nДоработка 5 (Дополнение 16.09): job_logs рерайта — в .gitignore, не в git")

    gitignore = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    check("studio-platform/rewrite/job_logs/ в .gitignore",
          "studio-platform/rewrite/job_logs/" in gitignore, True)
    block = gitignore.split("# выхлоп стенда рерайта")[-1] if \
        "# выхлоп стенда рерайта" in gitignore else ""
    check("job_logs/ лежит в том же блоке .gitignore, что outputs/ и uploads/ (§7)",
          "outputs/" in block and "uploads/" in block and "job_logs/" in block, True)

    # git check-ignore сверяет путь-пробу с паттернами, не заглядывая на диск — каталога
    # job_logs в свежем клоне может и не быть, проверка не должна создавать его сама
    # (тот же приём, что в tests/test_gpt_image_25.py для outputs/uploads).
    probe = os.path.join(BASE, "rewrite", "job_logs", "dev", "probe.log")
    chk = subprocess.run(["git", "check-ignore", "-q", probe], cwd=ROOT)
    check("новый файл в rewrite/job_logs игнорируется git", chk.returncode, 0)

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
