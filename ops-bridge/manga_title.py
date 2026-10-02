#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Заголовок ролика — Кодексом подписки, а не ключом прокси.

Зачем. Мост делает каждому ролику заголовок для YouTube: из рабочего названия проекта —
человеческое название на языке канала. Делал это прокси `triklz` по ключу `alt_llm`, и
02.10.2026 ключ встал: «API key token allowance exhausted» — лимит выбран. Девяносто
девять готовых роликов остановились на шаге, который стоит копейки.

Заказчик разрешил пользоваться Кодексом (02.10.2026), и это надёжнее: у подписки нет
квоты, которая кончается посреди дня, и она уже настроена на этой машине — тот же
Кодекс, которым работает рерайт.

КАК ЗОВЁМ. `codex exec --json` с теми же запретами, что у рерайта
(`rewrite_core._CODEX_TEXT_ONLY_CONFIG`): никакой оболочки, никакой сети, песочница
только на чтение. Заголовок — задача на один ответ, и давать ей что-то кроме текста
незачем. Разбираем машинный поток событий, а не печатный вывод: в печатном Кодекс
повторяет ответ дважды и добавляет свои рамки, и выдёргивать заголовок оттуда значит
однажды взять не ту строку.

ЧТО ОСТАВЛЕНО КАК БЫЛО. Промпт, чистка и разбор отказа берутся у кабинета
(`pub_publish`): заголовки должны выходить такими же, какими их делал прокси. Классы
отказа тоже его: `TitleUnavailable` — ролик ЖДЁТ (ресурс, настройка, пустой ответ),
`TitleError` — виновата строка (нет исходного названия, отказ фильтра, слишком короткий
ответ). Мост на них уже умеет отвечать правильно, и менять это ради смены провайдера
нельзя.

ЗАПАСНОЙ ПУТЬ. Кодекс не настроен — идём прокси, как раньше. Пополнят ключ — мост
продолжит работать без правок.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pub_publish

# Запреты один в один как у рерайта: Кодексу на этой задаче не нужны ни оболочка, ни
# сеть, ни файлы. Список скопирован намеренно, а не импортирован: рерайт живёт своей
# жизнью, и менять нам поведение заголовков его правкой нельзя.
TEXT_ONLY = [
    "--strict-config",
    "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "multi_agent",
    "--disable", "apps", "--disable", "remote_plugin", "--disable", "browser_use",
    "--disable", "computer_use", "--disable", "image_generation", "--disable", "goals",
    "--disable", "shell_snapshot",
    "-c", 'approval_policy="never"',
    "-c", 'sandbox_mode="read-only"',
    "-c", 'shell_environment_policy.inherit="none"',
    "-c", "shell_environment_policy.include_only=[]",
    "-c", 'web_search="disabled"',
]

TIMEOUT = int(os.environ.get("MANGA_TITLE_CODEX_TIMEOUT") or 180)


def codex_setup() -> dict:
    """Чем звать Кодекс: бинарь, дом с доступом, модель. Пусто — значит не настроен.

    Берём настройки рерайта: он на этой машине уже работает, и заводить вторые значения
    тех же самых вещей значит однажды обнаружить, что они разошлись.
    """
    бинарь = (os.environ.get("MANGA_TITLE_CODEX_BIN")
              or os.environ.get("REWRITE_CODEX_BIN") or "").strip()
    дом = (os.environ.get("MANGA_TITLE_CODEX_HOME")
           or os.environ.get("REWRITE_CODEX_HOME") or "").strip()
    модель = (os.environ.get("MANGA_TITLE_CODEX_MODEL")
              or os.environ.get("REWRITE_STUDIO_CODEX_MODEL") or "").strip()
    # «codex:gpt-5.6-terra» — это имя модели в списках студии; самому Кодексу приставка
    # не нужна.
    if модель.startswith("codex:"):
        модель = модель.split(":", 1)[1]
    if not бинарь or not os.path.exists(бинарь):
        return {}
    if дом and not os.path.isdir(дом):
        return {}
    return {"bin": бинарь, "home": дом, "model": модель}


def ready() -> bool:
    return bool(codex_setup())


def _answer(поток: str) -> str:
    """Последний ответ Кодекса из машинного потока событий.

    Берём именно `agent_message`: `turn.completed` несёт только расход, а печатный вывод
    повторяет ответ дважды и обрамляет своими строками.
    """
    ответ = ""
    for строка in (поток or "").splitlines():
        строка = строка.strip()
        if not строка.startswith("{"):
            continue
        try:
            событие = json.loads(строка)
        except ValueError:
            continue
        item = событие.get("item") or {}
        if событие.get("type") == "item.completed" and item.get("type") == "agent_message":
            ответ = str(item.get("text") or "") or ответ
    return ответ


def _run(промпт: str, настройки: dict, runner=None) -> str:
    """Один заход к Кодексу. Возвращает его ответ; пусто — значит ответа не было."""
    work = tempfile.mkdtemp(prefix="manga-title-")
    try:
        cmd = [настройки["bin"], "exec", "--json", *TEXT_ONLY,
               "--ignore-user-config", "--skip-git-repo-check", "-C", work]
        if настройки.get("model"):
            cmd += ["-m", настройки["model"]]
        cmd.append(промпт)
        env = dict(os.environ)
        if настройки.get("home"):
            env["CODEX_HOME"] = настройки["home"]
        бег = runner or _subprocess_run
        код, вывод = бег(cmd, env, TIMEOUT)
        if код != 0 and not _answer(вывод):
            raise pub_publish.TitleUnavailable(
                "Кодекс не дал заголовка (код %s): %s" % (код, " ".join(вывод.split())[:160]))
        return _answer(вывод)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _subprocess_run(cmd, env, timeout):
    """Настоящий запуск. Вынесен отдельно, чтобы проверки не ходили в Кодекс."""
    try:
        p = subprocess.run(cmd, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace")
    except subprocess.TimeoutExpired:
        raise pub_publish.TitleUnavailable(
            "Кодекс не ответил за %d с — ролик ждёт" % timeout)
    except OSError as exc:
        raise pub_publish.TitleUnavailable("Кодекс не запустился: %s" % exc)
    return p.returncode, p.stdout or ""


def make_title(donor_title: str, lang: str, llm: dict, runner=None) -> str:
    """Заголовок на языке канала. Кодекс настроен — им; нет — прокси, как раньше."""
    source = " ".join((donor_title or "").split())
    if not source:
        raise pub_publish.TitleError("у донора нет названия — не из чего делать заголовок")
    настройки = codex_setup()
    if not настройки:
        return pub_publish.make_title(donor_title, lang, llm)

    язык = pub_publish.LANG_NAMES.get((lang or "").lower(), lang or "русский")
    общее = ("%s\n\nЯзык канала: %s.\nНазвание оригинала: %s\n\n"
             "Ответь ТОЛЬКО готовым заголовком, одной строкой, без кавычек и пояснений."
             % (pub_publish.SYSTEM, язык, source))
    заголовок = pub_publish.clean_title(_run(общее, настройки, runner))
    if pub_publish._REFUSAL.match(заголовок):
        # Та же одна попытка с просьбой о нейтральной формулировке, что у кабинета:
        # модель иногда отказывается переписывать «страшные» названия.
        ещё = общее + "\n\n" + pub_publish.RETRY_HINT
        заголовок = pub_publish.clean_title(_run(ещё, настройки, runner))
        if pub_publish._REFUSAL.match(заголовок):
            raise pub_publish.TitleError(
                "модель отказалась переписывать заголовок: %s" % заголовок[:120])
    if not заголовок:
        raise pub_publish.TitleUnavailable("Кодекс вернул пустой ответ — ролик ждёт")
    if len(заголовок) < 10:
        raise pub_publish.TitleError(
            "модель вернула слишком короткое название: %r" % заголовок)
    return заголовок
