# -*- coding: utf-8 -*-
"""Готовый ролик из чужого кабинета — для канала другого кабинета (А5 владельца, 04.10.2026).

Готовые ролики ВВ2 Гринд делает в своём кабинете (тенант 18), а все 62 канала ВВ2 живут в
кабинете testpartner (тенант 3). Мост их не смешивал: движок ищет файлы проекта в папке
кабинета ВЫПУСКА (`_project_dir(tenant, pid)`), и 229 готовых роликов стояли. Владелец
выбрал: «научить мост брать ролик из кабинета проекта».

Как: выпуск заводится в кабинете канала, а в папке этого кабинета появляется ссылка на
папку проекта в кабинете Гринда. Проект остаётся у Гринда — он видит и правит его, как
раньше. Удаление «проекта» из кабинета канала ссылку не пройдёт: строки проекта там нет, а
shutil.rmtree по ссылке не идёт (проверено на машине 05.10).

Только из разрешённых кабинетов (PUB_BRIDGE_PROJECT_TENANTS, по умолчанию «18»): мост не
должен становиться способом заливать что угодно откуда угодно.
"""
from __future__ import annotations

import os

FOREIGN_PROJECT_TENANTS = {t.strip() for t in
                           os.environ.get("PUB_BRIDGE_PROJECT_TENANTS", "18").split(",")
                           if t.strip()}


def foreign_allowed(project_tenant, channel_tenant, allowed=None) -> bool:
    """Можно ли взять проект этого кабинета для канала другого."""
    allowed = FOREIGN_PROJECT_TENANTS if allowed is None else allowed
    return str(project_tenant) != str(channel_tenant) and str(project_tenant) in allowed


def link_path(tenants_dir: str, tenant, pid: str) -> str:
    return os.path.join(tenants_dir, str(tenant), "projects", pid)


def ensure_project_link(tenants_dir: str, project_tenant, channel_tenant, pid: str) -> str:
    """Ссылка на папку проекта в кабинете канала. Возвращает "" или причину отказа словами.

    Повтор безопасен: верная ссылка уже есть — ничего не делаем. Чужая папка или ссылка
    не туда на этом месте — отказ: ничего не перезаписываем и не удаляем.
    """
    target = link_path(tenants_dir, project_tenant, pid)
    place = link_path(tenants_dir, channel_tenant, pid)
    if not os.path.isdir(target):
        return f"папки проекта нет: {target}"
    if os.path.islink(place):
        if os.path.realpath(place) == os.path.realpath(target):
            return ""
        return f"на месте ссылки уже ссылка в другое место: {place} → {os.readlink(place)}"
    if os.path.lexists(place):
        return f"в кабинете канала уже есть своя папка с этим проектом: {place}"
    os.makedirs(os.path.dirname(place), exist_ok=True)
    os.symlink(target, place)
    return ""
