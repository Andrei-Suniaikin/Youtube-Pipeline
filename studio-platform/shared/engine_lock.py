#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Замок движка: фон студии работает ровно в одном процессе, даже когда процессов два.

Зачем. Выкатка без простоя («синий/зелёный», deploy/BLUE_GREEN.md) поднимает новый
экземпляр студии РЯДОМ со старым и лишь потом переключает на него Caddy. Несколько
секунд (а при длинном ответе старого — минут) с одной базой работают два процесса.
Запросы они обслуживать могут оба: SQLite это переживает, как переживает таймеры
тревог и уборки. А вот фон — нет. На старте каждое подприложение ВОЗВРАЩАЕТ в очередь
задачи со статусом «running» (кабинет — `_recover_orphans`, рерайт —
`migrate_001(requeue_running=True)`, обложки — `recover_interrupted_jobs`): это
правильно после падения, когда их точно никто не ведёт, и разрушительно, когда их
ведёт сосед. Второй экземпляр без замка переклеймил бы живые задачи первого, а движок
выпусков в двух копиях дважды оплатил бы один и тот же шаг.

Как. Файловый замок `flock` на `$STUDIO_ENGINE_LOCK` (в бою /srv/studio/data/engine.lock).
Кто взял — тот и поднимает фон. Кто не взял — отвечает на запросы и раз в
`STUDIO_ENGINE_LOCK_POLL` секунд пробует снова; как только владелец вышел (замок
освобождается ядром при закрытии файла, то есть и при SIGKILL), ждущий берёт его и
поднимает фон — с тем же возвратом осиротевших задач, что и после обычного рестарта.
Передача получается сама собой, без сигналов и без второго источника правды.

Почему `flock`, а не pid-файл: pid-файл переживает своего владельца и требует
проверять «а жив ли», а замок `flock` умирает вместе с процессом — нечего чистить.
Почему не передаётся дочерним процессам: Python открывает файлы без наследования
(PEP 446), и воркеры, которых кабинет запускает через subprocess, замок не держат.

Без переменной `STUDIO_ENGINE_LOCK` модуль ничего не делает: `run_when_owner` зовёт
функцию сразу, как было. Так тесты, стенд и запуск отдельных сервисов не меняются.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, List, Optional, Tuple

try:
    import fcntl
except ImportError:                                                # не Linux/macOS
    fcntl = None  # type: ignore[assignment]

POLL_SEC_DEFAULT = 2.0


def _env_poll() -> float:
    try:
        v = float(os.environ.get("STUDIO_ENGINE_LOCK_POLL", "") or POLL_SEC_DEFAULT)
    except ValueError:
        v = POLL_SEC_DEFAULT
    return v if v > 0 else POLL_SEC_DEFAULT


class EngineLock:
    """Один замок на процесс. Состояния: off (замка нет), standby (ждём), owner (наш)."""

    def __init__(self, path: str, instance: str, poll: Optional[float] = None,
                 log: Callable[[str], None] = print):
        self.path = (path or "").strip()
        self.instance = (instance or "").strip() or f"pid{os.getpid()}"
        self.poll = poll if poll and poll > 0 else _env_poll()
        self._log = log
        self._fd: Optional[int] = None
        self._mu = threading.Lock()
        self._owner = threading.Event()
        self._pending: List[Tuple[str, Callable[[], None]]] = []
        self._started: List[str] = []
        self._waiter: Optional[threading.Thread] = None
        self._since: Optional[float] = None
        self._attempts = 0
        self._last_error = ""

    # ── состояние ────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self.path) and fcntl is not None

    def is_owner(self) -> bool:
        """Можно ли этому процессу вести фон. Без замка — всегда да (прежнее поведение)."""
        return (not self.enabled) or self._owner.is_set()

    def is_standby(self) -> bool:
        return self.enabled and not self._owner.is_set()

    def state(self) -> str:
        if not self.enabled:
            return "off"
        return "owner" if self._owner.is_set() else "standby"

    def holder(self) -> str:
        """Кто держит замок, по записи в файле. Подсказка для человека, не источник правды."""
        if not self.path:
            return ""
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(200).strip()
        except OSError:
            return ""

    def status(self) -> dict:
        with self._mu:
            pending = [t for t, _ in self._pending]
            started = list(self._started)
        return {
            "enabled": self.enabled,
            "path": self.path,
            "instance": self.instance,
            "state": self.state(),
            "since": self._since,
            "holder": self.holder() if self.enabled else "",
            "attempts": self._attempts,
            "pending": pending,
            "started": started,
            "last_error": self._last_error,
        }

    # ── захват ───────────────────────────────────────────────────────────────
    def try_acquire(self) -> bool:
        """Попробовать взять замок, не блокируясь. True — замок наш (или замка нет)."""
        if not self.enabled:
            return True
        with self._mu:
            if self._owner.is_set():
                return True
            self._attempts += 1
            fd = None
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                # O_CLOEXEC стоит по умолчанию (PEP 446): дочерние процессы замок не унесут.
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                # BlockingIOError — замок у другого процесса; остальное — тоже «не наш».
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._last_error = f"{type(exc).__name__}: {exc}"
                return False
            self._fd = fd
            self._since = time.time()
            self._last_error = ""
            try:
                os.ftruncate(fd, 0)
                os.write(fd, (f"{self.instance} pid={os.getpid()} "
                              f"since={time.strftime('%Y-%m-%d %H:%M:%S')}\n").encode())
            except OSError:
                pass                                               # запись — подсказка, не замок
            self._owner.set()
            return True

    def release(self) -> None:
        """Отпустить замок. Нужно тестам и аккуратному завершению; при выходе процесса
        ядро отпускает сам."""
        with self._mu:
            fd, self._fd = self._fd, None
            self._owner.clear()
            self._since = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    # ── запуск фона ──────────────────────────────────────────────────────────
    def run_when_owner(self, title: str, fn: Callable[[], None]) -> bool:
        """Выполнить `fn`, когда замок наш.

        Свободен или замка нет — сразу, в вызывающем потоке (порядок старта тот же, что
        был до замка). Занят — `fn` встаёт в очередь и выполнится из потока ожидания,
        когда владелец выйдет. Возвращает True, если выполнено сейчас.
        """
        if self.try_acquire():
            self._run(title, fn)
            return True
        with self._mu:
            self._pending.append((title, fn))
            if self._waiter is None or not self._waiter.is_alive():
                self._waiter = threading.Thread(target=self._wait_loop,
                                                name="engine-lock-wait", daemon=True)
                self._waiter.start()
        return False

    def _wait_loop(self) -> None:
        who = self.holder() or "неизвестно кто"
        self._log(f"[замок движка] экземпляр {self.instance}: фон не поднимаю, замок держит "
                  f"{who}; проверяю раз в {self.poll:g} с")
        while not self.try_acquire():
            time.sleep(self.poll)
        with self._mu:
            titles = [t for t, _ in self._pending]
        self._log(f"[замок движка] экземпляр {self.instance}: замок наш, поднимаю фон: "
                  + ", ".join(titles))
        while True:
            with self._mu:
                if not self._pending:
                    return
                title, fn = self._pending.pop(0)
            self._run(title, fn)

    def _run(self, title: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            with self._mu:
                self._started.append(title)
        except Exception as exc:                                   # noqa: BLE001
            # Падение одного фона не должно уносить остальные — так же, как в
            # web/platform.py при старте подприложений.
            self._log(f"[замок движка] {title}: фон не поднялся — {type(exc).__name__}: {exc}")


# Один замок на процесс: три подприложения платформы делят его, а не берут по своему.
LOCK = EngineLock(os.environ.get("STUDIO_ENGINE_LOCK", ""),
                  os.environ.get("STUDIO_INSTANCE", ""))


def run_when_owner(title: str, fn: Callable[[], None]) -> bool:
    return LOCK.run_when_owner(title, fn)


def is_owner() -> bool:
    return LOCK.is_owner()


def is_standby() -> bool:
    return LOCK.is_standby()


def status() -> dict:
    return LOCK.status()
