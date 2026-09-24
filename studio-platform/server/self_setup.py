# -*- coding: utf-8 -*-
"""Студия настраивает себя сама при запуске.

Решение владельца, 23.09.2026, дословно: «нет понятия rewrite и cover, есть понятие
студия, нет такого что ключи отдельно заводятся». Человек заводит ОДНУ учётную запись
и заходит в ОДНУ студию. Всё, что внутри, — наша кухня, и она не имеет права требовать
от него шагов, команд и паролей.

Что здесь происходит. Внутри платформы работают модули, которые пишут текст и делают
обложки. Исторически каждый из них — отдельный сервис со своей учётной записью и своим
ключом доступа, и на пустой базе они отвечают «account is disabled», пока кто-то не
заведёт их вручную. Раньше это делал человек скриптом. Теперь — этот модуль, на старте,
молча и один раз.

Как: всё в своём же процессе, без HTTP и без пароля человека. Пароль модулям не нужен
вовсе — вход в них идёт сессией студии (единый вход), а собственный пароль у их учётной
записи существует только потому, что этого требует их старая форма входа. Поэтому он
случайный и нигде не хранится: им никто никогда не воспользуется.

Идемпотентно: при каждом запуске проверяет и доделывает недостающее. Уже настроенное
не трогает — в частности, НИКОГДА не меняет пароль человека (прежний скрипт это делал,
и это была ошибка).
"""
from __future__ import annotations

import secrets
import sys
import time
from typing import Callable, Optional

# Как модуль зовётся внутри и как он называется в журнале для человека.
MODULES = (
    ("rewrite", "тексты"),
    ("cover", "обложки"),
)


def _owner(c) -> Optional[dict]:
    """Хозяин студии: администратор с заданным паролем, самый первый по счёту.

    Заводские `admin` и `demo` без пароля не в счёт — под ними никто не работает,
    и выдавать доступы от их имени значило бы привязать данные к учётке-призраку.
    """
    row = c.execute(
        "SELECT id, name, tenant_id FROM users "
        "WHERE role='admin' AND password_hash IS NOT NULL AND password_hash<>'' "
        "AND COALESCE(disabled,0)=0 ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"],
            "tenant": str(row["tenant_id"] or row["id"])}


def _module(name: str):
    """Модуль текста или обложек — тот самый объект, что уже поднят в этом процессе."""
    for candidate in (f"{name}_server", "server" if name == "rewrite" else "cover_server"):
        mod = sys.modules.get(candidate)
        if mod is not None and hasattr(mod, "license_mgr"):
            return mod
    return None


def _ensure_account(mod, login: str, title: str, log: Callable) -> bool:
    """Завести учётную запись модуля, если у него нет ни одной.

    Первая заведённая получает номер хозяина студии и наследует все его данные и
    пресеты — так устроен сам модуль. Пароль случайный: вход идёт сессией студии.
    """
    mgr = getattr(mod, "license_mgr", None)
    if mgr is None:
        return False
    try:
        if mgr.has_users():
            return True
        ok, detail, _name = mgr.setup_admin(login, secrets.token_urlsafe(24), "127.0.0.1")
        if not ok:
            log(f"[студия] {title}: учётная запись не завелась — {detail}")
            return False
        log(f"[студия] {title}: учётная запись заведена")
        return True
    except Exception as exc:                                       # noqa: BLE001
        log(f"[студия] {title}: учётная запись не завелась — {type(exc).__name__}: {exc}")
        return False


def _new_key(mod, name: str, tenant: str) -> str:
    """Выписать ключ доступа к модулю. Значение показывается один раз — сразу прячем."""
    label = f"studio-{int(time.time())}"
    if name == "rewrite":
        import os
        from pathlib import Path
        sessions_db = os.environ.get("SESSION_DB") or str(
            Path(getattr(mod, "BASE_DIR", ".")) / "sessions.db")
        made = mod.create_api_key(sessions_db, tenant, "", label)
    else:
        made = mod.create_api_key(tenant, label)
    for field in ("plain_key", "key", "api_key", "plain", "token", "secret"):
        if isinstance(made, dict) and made.get(field):
            return str(made[field])
    raise RuntimeError(f"в ответе нет самого ключа, только {sorted(made or {})}")


def _share_text_key(c, name: str) -> None:
    """Обложкам нужен тот же ключ к сервису текста: подписками распоряжается он один.

    Кладём ключ в окружение процесса, а не в файл: части студии живут в одном процессе,
    и второго места хранения секрета заводить незачем.
    """
    if name != "rewrite":
        return
    import os

    import pub_remote

    key = pub_remote.get_key(c, "rewrite")
    if key:
        os.environ["COVER_TEXT_SERVICE_KEY"] = key


def ensure_internal_access(db: Callable, log: Callable = print) -> dict:
    """Доделать всё, чего не хватает для работы. Возвращает короткий отчёт для журнала.

    Ничего не просит и ничего не спрашивает: человек про эти доступы не знает и знать
    не должен. Сбой одного модуля не мешает другому и не роняет запуск студии.
    """
    import pub_remote

    done, skipped, failed = [], [], []
    c = db()
    try:
        who = _owner(c)
        if who is None:
            # Учётки ещё нет — студия только что развёрнута. Это не ошибка: доступы
            # появятся на следующем запуске, сразу после заведения администратора.
            return {"ok": False, "why": "хозяин студии ещё не заведён"}
        for name, title in MODULES:
            mod = _module(name)
            if mod is None:
                failed.append(title)
                log(f"[студия] {title}: модуль не поднят в этом процессе")
                continue
            if not _ensure_account(mod, who["name"], title, log):
                failed.append(title)
                continue
            if pub_remote.get_key(c, name):
                skipped.append(title)
                _share_text_key(c, name)
                continue
            try:
                pub_remote.set_key(c, name, _new_key(mod, name, who["tenant"]), time.time())
                c.commit()
                done.append(title)
                log(f"[студия] {title}: доступ выдан")
                _share_text_key(c, name)
            except Exception as exc:                               # noqa: BLE001
                failed.append(title)
                log(f"[студия] {title}: доступ не выдан — {type(exc).__name__}: {exc}")
    finally:
        c.close()
    return {"ok": not failed, "выдано": done, "было": skipped, "не вышло": failed}
