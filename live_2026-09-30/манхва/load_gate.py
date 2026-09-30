# -*- coding: utf-8 -*-
"""Гибкий предел одновременных вызовов: по занятости машины, а не по числу из настройки.

Зачем. Вызов Codex CLI ест ~0,15 ядра ВСЁ время, пока льётся ответ (замер 20.09.2026:
служба 3,74 ядра, из них сам Python 0,03). Жёсткое число («24 вызова») либо не догружает
машину, либо душит соседей. Здесь предел живой: пока занятость машины ниже порога —
пускаем новые вызовы, выше — ждём.

Как считается занятость. Доля занятых ядер за последние секунды по /proc/stat (не load
average: он считает и ждущих диск). К измеренному добавляется «ещё не видимая» нагрузка
только что пущенных вызовов: между замерами их работа в /proc/stat ещё не отразилась.

Настройки:
  MANGA_LOAD_GATE=on|off      — включить гибкий предел (по умолчанию off: всё как было)
  MANGA_LOAD_TARGET=0.9       — до какой занятости машины пускаем (доля всех ядер)
  MANGA_LOAD_CORE_PER_CALL=0.15 — во сколько ядер обходится один идущий вызов
  MANGA_LOAD_MIN_CALLS=4      — столько пускаем всегда, даже если машина занята соседями
  MANGA_LOAD_MAX_CALLS=64     — предохранитель сверху
  MANGA_LOAD_PROBE_SECONDS=5  — как часто перезамерять занятость
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, List, Optional

import claude_pool as CP


def _num(name: str, default: float, lo: float, hi: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name}: нужно число от {lo} до {hi}, а не {raw!r}") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name}: нужно число от {lo} до {hi}, а не {value}")
    return value


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("MANGA_LOAD_GATE", "")).strip().lower() in {"1", "on", "true", "yes", "да"}


class Machine:
    """Занятость машины по /proc/stat: доля занятых ядер между двумя чтениями."""

    def __init__(self, path: str = "/proc/stat", clock=time.time):
        self.path, self.clock = path, clock
        self._prev = None
        self._busy = 0.0
        self._at = 0.0

    def cores(self) -> int:
        return os.cpu_count() or 1

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = [int(x) for x in line.split()[1:11]]
                    idle = parts[3] + parts[4]          # idle + iowait
                    return sum(parts), idle
        raise RuntimeError("/proc/stat без строки cpu")

    def busy(self, max_age: float = 5.0) -> float:
        """Доля занятых ядер (0..1). Между чтениями возвращает прошлый замер."""
        now = self.clock()
        if self._prev is not None and now - self._at < max_age:
            return self._busy
        total, idle = self._read()
        if self._prev is not None:
            dt, di = total - self._prev[0], idle - self._prev[1]
            if dt > 0:
                self._busy = max(0.0, min(1.0, 1.0 - di / dt))
        self._prev, self._at = (total, idle), now
        return self._busy


class Gate:
    """Пропускает вызовы, пока машина не занята на MANGA_LOAD_TARGET."""

    def __init__(self, machine: Optional[Machine] = None, env=None, sleep=time.sleep, clock=time.time):
        env = os.environ if env is None else env
        self.machine = machine or Machine(clock=clock)
        self.target = _num("MANGA_LOAD_TARGET", 0.9, 0.1, 1.0)
        self.per_call = _num("MANGA_LOAD_CORE_PER_CALL", 0.15, 0.01, 4.0)
        self.min_calls = int(_num("MANGA_LOAD_MIN_CALLS", 4, 1, 256))
        self.max_calls = int(_num("MANGA_LOAD_MAX_CALLS", 64, 1, 512))
        self.probe = _num("MANGA_LOAD_PROBE_SECONDS", 5, 0.5, 120)
        self.sleep, self.clock = sleep, clock
        self.inflight = 0
        self._started_since_probe = 0
        self._before_probe, self._probe_at = 0, None    # 27.09.2026 A2-LOAD01: см. room()
        self._lock = threading.Lock()
        self._admit, self._probing, self._measured = threading.Lock(), threading.Lock(), None  # A2-LOAD01: _take()
        self.waited_seconds = 0.0

    def room(self, measured: Optional[float] = None) -> bool:
        """Есть ли место ещё для одного вызова прямо сейчас. measured — готовый замер машины (0..1)."""
        with self._lock:
            inflight, fresh = self.inflight, self._started_since_probe
        if inflight >= self.max_calls:
            return False
        if inflight < self.min_calls:
            return True
        cores = self.machine.cores()
        busy = (self.machine.busy(self.probe) if measured is None else measured) * cores
        # Свежие вызовы в /proc/stat ещё не видны: досчитываем их вручную.
        # 27.09.2026, A2-LOAD01: счёт свежих не обнулялся — вызов, который замер уже видит, шёл
        # второй раз (замер 0,6 + ещё 2×0,15 за те же 2 вызова -> «места нет»). Новый замер:
        # свежими остаются пущенные за прошлый промежуток (в нём видны лишь частью) и после него.
        with self._lock:
            at = getattr(self.machine, "_at", None)
            if at != self._probe_at:
                self._probe_at, self._before_probe, self._started_since_probe = at, self._started_since_probe, 0
            fresh = min(self.inflight, self._before_probe + self._started_since_probe)
        busy += fresh * self.per_call
        return busy + self.per_call <= self.target * cores

    def _take(self) -> bool:
        """Проверка места и занятие — одна секция. 27.09.2026, A2-LOAD01: room() и «+1» шли
        порознь — два потока оба видели место и оба входили (предел 1 -> шло 2). Под _admit —
        только решение. Машину (у чужой это ssh, до 20 с) меряет вне замка один поток; прочие
        берут прошлый замер (не старше probe) или ждут в acquire с check/stop, а не стоят
        в очереди N × таймаут ssh."""
        measured = None
        while True:
            with self._admit:
                if self.inflight >= self.max_calls:
                    return False
                if measured is not None or self.inflight < self.min_calls:
                    if not self.room(measured):
                        return False
                    with self._lock:
                        self.inflight += 1
                        self._started_since_probe += 1
                    return True
            if self._probing.acquire(blocking=False):
                try:
                    self.machine.cores()
                    measured = self.machine.busy(self.probe)
                    self._measured = (measured, self.clock())
                finally:
                    self._probing.release()
            elif self._measured is None or self.clock() - self._measured[1] > self.probe:
                return False            # замер идёт, а прошлого нет или он устарел: ждём в acquire
            else:
                measured = self._measured[0]

    def acquire(self, check: Optional[Callable] = None, stop: Optional[Callable] = None,
                log: Optional[Callable] = None) -> float:
        """Ждёт места. Возвращает, сколько секунд простоял. check/stop — как у пула подписок:
        задачу забрали или служба останавливается, ждать больше не нужно."""
        t0 = self.clock()
        said = t0
        while not self._take():
            if check is not None:
                check()
            if stop is not None:
                stop()
            now = self.clock()
            if log is not None and now - said >= 60:
                said = now
                log(f"ждёт места: машина занята на {self.machine.busy(self.probe) * 100:.0f}%, "
                    f"идёт вызовов {self.inflight}")
            self.sleep(0.5)
        waited = self.clock() - t0
        self.waited_seconds += waited
        return waited

    def release(self) -> None:
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            self._started_since_probe = max(0, self._started_since_probe - 1)

    def state(self) -> dict:
        return {"inflight": self.inflight, "busy": round(self.machine.busy(self.probe), 3),
                "cores": self.machine.cores(), "target": self.target}


class RemoteMachine(Machine):
    """Занятость ДРУГОЙ машины: /proc/stat и число ядер спрашиваем по ssh, ответ кэшируем."""

    def __init__(self, ssh_cmd: List[str], cores_hint: int = 0, clock=time.time, run=None):
        super().__init__(path="", clock=clock)
        self.ssh_cmd, self._cores, self._run = ssh_cmd, cores_hint, run or _run_ssh
        self._raw = ""
        self._failed_at = None      # 27.09.2026 A2-LOAD01: см. busy()

    def cores(self) -> int:
        if not self._cores:
            code, out = self._run(self.ssh_cmd + ["nproc"], 20)
            self._cores = int(out.strip().split()[0]) if code == 0 and out.strip()[:1].isdigit() else 4
        return self._cores

    def _read(self):
        code, out = self._run(self.ssh_cmd + ["head -1 /proc/stat"], 20)
        if code != 0 or not out.startswith("cpu"):
            raise RuntimeError("занятость машины не прочиталась")
        parts = [int(x) for x in out.split()[1:11]]
        return sum(parts), parts[3] + parts[4]

    def busy(self, max_age: float = 5.0) -> float:
        # 27.09.2026, A2-LOAD01: неудачное чтение помним max_age секунд, как удачное, — иначе при
        # сбое ssh ворота заново ждали таймаут (до 20 с) при каждой проверке места.
        if self._failed_at is not None and self.clock() - self._failed_at < max_age:
            return 0.5
        try:
            return super().busy(max_age)
        except Exception:   # noqa: BLE001 — не прочиталось: считаем машину наполовину занятой
            self._failed_at = self.clock()
            return 0.5


def _run_ssh(cmd: List[str], timeout: int):
    import subprocess
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or ""
    except Exception as e:   # noqa: BLE001
        return 1, str(e)


GATES: dict = {}


def gate_for(name: str, ssh_cmd: Optional[List[str]] = None, cores: int = 0) -> Optional[Gate]:
    """Пропускник нужной машины: у каждой своя занятость и свой счёт идущих вызовов."""
    if not enabled():
        return None
    key = name or "local"
    if key not in GATES:
        machine = RemoteMachine(ssh_cmd, cores) if ssh_cmd else Machine()
        GATES[key] = Gate(machine=machine)
        CP.log.warning("гибкий предел вызовов для «%s»: пускаем, пока машина занята меньше чем на %.0f%%",
                       key, GATES[key].target * 100)
    return GATES[key]


GATE: Optional[Gate] = None


def gate() -> Optional[Gate]:
    """Общий пропускник службы; None — гибкий предел выключен."""
    global GATE
    if not enabled():
        return None
    if GATE is None:
        GATE = Gate()
        CP.log.warning("гибкий предел вызовов включён: пускаем, пока машина занята меньше чем на %.0f%%",
                       GATE.target * 100)
    return GATE
