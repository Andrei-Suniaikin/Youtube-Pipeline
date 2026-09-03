#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Клипы и журнал платных вызовов: порядок и запреты.

Провайдеров здесь нет — их функции подменены. Проверяется ровно то, ради чего журнал
и заводился: заказ фиксируется ДО ожидания результата, а когда платить нельзя, платного
вызова не происходит вовсе.

    .venv/bin/python tests/test_clip_journal.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

import googler_worker as G                                        # noqa: E402
from call_journal import Decision                                 # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeJournal:
    """Кабинет, записанный на бумажке: что спросили и что ответили."""

    enabled = True

    def __init__(self, decision=None):
        self.decision = decision or Decision("new", call_id=7)
        self.calls = []                    # порядок обращений — главное, что проверяем

    @staticmethod
    def fingerprint(**parts):
        return "fp:" + ",".join(f"{k}={parts[k]}" for k in sorted(parts))

    def reserve(self, *, unit, fingerprint, kind="", provider="", model=""):
        self.calls.append(("reserve", unit, kind, provider))
        return self.decision

    def ordered(self, call_id, remote_order_id):
        self.calls.append(("ordered", remote_order_id))
        return True

    def delivered(self, call_id, result_path, cost=None):
        self.calls.append(("delivered", os.path.basename(result_path or "")))
        return True

    def failed(self, call_id, error, ordered=False):
        self.calls.append(("failed", bool(ordered)))
        return True

    def verbs(self):
        return [c[0] for c in self.calls]


class Provider:
    """Заглушка провайдера: считает, сколько раз с него просили деньги."""

    def __init__(self, task_id="task-1", data="ZGF0YQ==", ok=True):
        self.task_id, self.data, self.ok = task_id, data, ok
        self.created = 0
        self.polled = []

    def request(self, path, data=None, **kw):
        self.created += 1
        if not self.ok:
            return {"error": "veo says no"}
        return {"data": {"task_id": self.task_id}}

    def poll(self, task_id, index=0, timeout=None):
        self.polled.append(task_id)
        return self.data if self.ok else None


def install(journal, provider):
    G._JOURNAL_SINGLETON = journal
    G.veo_request = provider.request
    G.veo_poll_task = provider.poll
    G._gen_attempts = lambda default: 1        # без двадцати попыток по минуте
    G._is_fatal_gen_error = lambda err: False
    # Настоящая запись, а не заглушка: журнал доказывает «готово» существованием файла,
    # и подменять это True значило бы проверять не то, что работает в бою.
    def _save(data, path, **kw):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"clip:" + str(data).encode("ascii", "replace"))
        return True
    G.save_base64_to_file = _save


def main() -> int:
    root = tempfile.mkdtemp(prefix="clip-journal-")
    frame = os.path.join(root, "007.jpg")
    with open(frame, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0" + b"0" * 64)
    out = os.path.join(root, "007_video.mp4")

    G.VIDEO_METHOD = "veo"
    saved_request, saved_poll = G.veo_request, G.veo_poll_task
    saved_attempts, saved_fatal = G._gen_attempts, G._is_fatal_gen_error
    saved_save = G.save_base64_to_file
    try:
        def clean():
            """Файл результата не должен переезжать из случая в случай: половина проверок
            как раз про то, есть он или нет."""
            if os.path.exists(out):
                os.remove(out)

        print("Обычный клип: заявка → заказ → результат")
        clean()
        j, p = FakeJournal(), Provider()
        install(j, p)
        idx, data = G.generate_video("маяк в тумане", frame, 6, False, "007", out)
        check("клип получен", bool(data), True)
        check("порядок обращений", j.verbs(), ["reserve", "ordered", "delivered"])
        check("id заказа записан до опроса", j.calls[1][1], "task-1")
        check("и заказ действительно опрошен", p.polled, ["task-1"])
        check("единица — номер сцены", j.calls[0][1], "007")
        check("вид работы — клип", j.calls[0][2], "video")

        print("\n  единица берётся из имени кадра, если не передана явно")
        clean()
        j, p = FakeJournal(), Provider()
        install(j, p)
        G.generate_video("x", frame, 0, False, None, out)
        check("имя кадра стало единицей", j.calls[0][1], "007")

        print("\nПовтор уже полученного клипа — платить нельзя")
        with open(out, "wb") as fh:
            fh.write(b"video-bytes")
        j = FakeJournal(Decision("skip", call_id=0, result_path=out))
        p = Provider()
        install(j, p)
        idx, data = G.generate_video("маяк в тумане", frame, 6, False, "007", out)
        check("готовый файл отдан", bool(data), True)
        check("к провайдеру не обращались", p.created, 0)
        check("и строку журнала не трогали", j.verbs(), ["reserve"])

        print("\n  клип числится полученным, а файла нет")
        j = FakeJournal(Decision("skip", call_id=0, result_path=os.path.join(root, "нет.mp4")))
        p = Provider()
        install(j, p)
        idx, data = G.generate_video("x", frame, 6, False, "007", None)
        check("клип пропущен", data, None)
        check("нового заказа не создано", p.created, 0)

        print("\nОбрыв после создания заказа — опрашиваем старый, не создаём новый")
        j = FakeJournal(Decision("verify", call_id=9, remote_order_id="task-было"))
        p = Provider()
        install(j, p)
        idx, data = G.generate_video("x", frame, 6, False, "007", out)
        check("результат забран", bool(data), True)
        check("опрошен именно старый заказ", p.polled, ["task-было"])
        check("новых заказов ноль", p.created, 0)

        print("\n  заказ был, а id неизвестен — клип пропускается")
        j = FakeJournal(Decision("verify", call_id=9, remote_order_id=""))
        p = Provider()
        install(j, p)
        idx, data = G.generate_video("x", frame, 6, False, "007", out)
        check("клип пропущен", data, None)
        check("платного вызова не было", p.created, 0)

        print("\nКабинет не ответил — платить нельзя")
        j = FakeJournal(Decision("unknown", error="URLError"))
        p = Provider()
        install(j, p)
        idx, data = G.generate_video("x", frame, 6, False, "007", out)
        check("клип пропущен", data, None)
        check("к провайдеру не обращались", p.created, 0)

        print("\nПровайдер отказал после создания заказа")
        clean()
        j = FakeJournal()
        p = Provider(ok=False)
        install(j, p)
        idx, data = G.generate_video("x", frame, 6, False, "007", out)
        check("клипа нет", data, None)
        check("отказ записан", j.verbs()[-1], "failed")
        check("но деньги считаются потраченными не всегда: заказа не было",
              j.calls[-1][1], False)

        print("\nЖурнал выключен — движок работает как раньше")
        clean()
        G._JOURNAL_SINGLETON = None
        saved_cls, G.CallJournal = G.CallJournal, None
        p = Provider()
        G.veo_request, G.veo_poll_task = p.request, p.poll
        try:
            idx, data = G.generate_video("x", frame, 6, False, "007", out)
            check("клип сгенерирован", bool(data), True)
            check("провайдер вызван один раз", p.created, 1)
        finally:
            G.CallJournal = saved_cls

        print("\nt2v: заявка одна на весь цикл попыток")
        clean()
        j, p = FakeJournal(), Provider(task_id="t2v-1")
        install(j, p)
        idx, data = G._t2v_generate_with_retry("город на рассвете", "landscape", 2,
                                               "003", out)
        check("клип получен", bool(data), True)
        check("порядок обращений", j.verbs(), ["reserve", "ordered", "delivered"])
        check("единица — номер сцены, а не позиция в выборке", j.calls[0][1], "003")

        print("\n  t2v: повтор уже полученного не платит")
        j = FakeJournal(Decision("skip", call_id=0, result_path=out))
        p = Provider()
        install(j, p)
        idx, data = G._t2v_generate_with_retry("x", "landscape", 2, "003", out)
        check("к провайдеру не обращались", p.created, 0)
        check("файл отдан", bool(data), True)
    finally:
        G.veo_request, G.veo_poll_task = saved_request, saved_poll
        G._gen_attempts, G._is_fatal_gen_error = saved_attempts, saved_fatal
        G.save_base64_to_file = saved_save
        G._JOURNAL_SINGLETON = None
        shutil.rmtree(root, ignore_errors=True)

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
