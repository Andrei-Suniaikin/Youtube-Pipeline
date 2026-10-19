#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единый список каналов: вердикт «чего не хватает каналу для заливки» (19.10.2026).

Живые случаи 19.10, на которых это и писалось:
  • профиль 54 — канал «A Háború Nyers Oldala» виден под третьим аккаунтом, а привязка
    стоит на первом: заливка входит не туда, выпуск встаёт «активный аккаунт не управляет
    каналом». Пять выпусков стояли так;
  • профиль 34 — «Poveștile din Pădure» виден под аккаунтом, которого нет в реестре:
    привязку записать некуда, нужен пароль и ключ 2FA от ребят;
  • профиль 40 — «Poslední Dopis z Fronty» не виден ни под одним аккаунтом профиля,
    хотя права менеджера, по словам, выданы. Десять выпусков;
  • профили 22 и 28 — сверка не прошла, профиль был занят заливкой: это «не знаем»,
    а не «всё хорошо».

Что проверяется: вердикт по каждому из этих случаев; что секреты в отчёт не попадают;
что отчёт профиля заменяет прежние факты этого профиля и не трогает чужие.

    .venv/bin/python tests/test_access.py
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_access as A                                           # noqa: E402

FAILS = []
NOW = 1_800_000_000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def acc(name, authuser, sees=False, manager=False, in_registry=False,
        has_secrets=False, bound=False):
    return {"sha": sha(name), "mask": name[:3] + "…@gmail.com", "authuser": authuser,
            "sees": sees, "manager": manager, "in_registry": in_registry,
            "has_secrets": has_secrets, "bound": bound}


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE pub_channels(id INTEGER PRIMARY KEY, tenant_id TEXT, "
              "name TEXT, uc TEXT)")
    for cid, uc, name in ((1, "UC" + "a" * 22, "A Háború Nyers Oldala"),
                          (2, "UC" + "b" * 22, "Poveștile din Pădure"),
                          (3, "UC" + "c" * 22, "Poslední Dopis z Fronty"),
                          (4, "UC" + "d" * 22, "Krv i Blato"),
                          (5, "UC" + "e" * 22, "Hihetetlen Háborús Történetek")):
        c.execute("INSERT INTO pub_channels VALUES(?,?,?,?)", (cid, "3", name, uc))
    c.execute("INSERT INTO pub_channels VALUES(9,'чужой','чужой канал','UC" + "z" * 22 + "')")
    A.ensure_schema(c)
    return c


def test_verdicts():
    print("Вердикты по живым случаям 19.10")
    c = fresh()

    # Профиль 54: канал виден третьим аккаунтом (без секретов), привязка на первом.
    A.ingest(c, {"profile": "54", "checked": NOW, "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [
            acc("phe0640280072", 0, sees=False, in_registry=True, has_secrets=True, bound=True),
            acc("phe0653243624", 1, sees=False, in_registry=True, has_secrets=True),
            acc("a01099887766", 2, sees=True, manager=True)]},
        {"uc": "UC" + "e" * 22, "accounts": [
            acc("phe0653243624", 1, sees=True, in_registry=True, has_secrets=True, bound=True),
            acc("a01099887766", 2, sees=True, manager=True)]},
    ]}, now=NOW)
    v = A.verdicts(c, "3")

    print("  канал виден только аккаунтом без секретов")
    check("состояние", v[1]["state"], "no_secrets")
    check("словами", v[1]["text"], A.STATE_TEXT["no_secrets"])
    check("в подсказке назван аккаунт", "#2" in v[1]["fix"], True)
    check("и назван профиль", "профиле 54" in v[1]["fix"], True)

    print("  тот же профиль, другой канал — привязка на видящий аккаунт с секретами")
    check("всё хорошо", v[5]["state"], "ok")

    print("  профиль 34: канала не видит никто из тех, у кого есть секреты")
    A.ingest(c, {"profile": "34", "checked": NOW, "channels": [
        {"uc": "UC" + "b" * 22, "accounts": [
            acc("a01066661588", 0, sees=False, in_registry=True, has_secrets=True),
            acc("a01099993800", 2, sees=True, manager=True)]}]}, now=NOW)
    v = A.verdicts(c, "3")
    check("состояние", v[2]["state"], "no_secrets")

    print("  профиль 40: канала нет ни под одним аккаунтом")
    A.ingest(c, {"profile": "40", "checked": NOW, "channels": [
        {"uc": "UC" + "c" * 22, "accounts": [
            acc("min0628625171", 0, in_registry=True, has_secrets=True, bound=True),
            acc("a01086843560", 1, in_registry=True, has_secrets=True)]}]}, now=NOW)
    v = A.verdicts(c, "3")
    check("состояние", v[3]["state"], "not_in_profile")
    check("подсказка про права менеджера", "права менеджера" in v[3]["fix"], True)

    print("  профиль 28 не сверялся — это «не знаем», а не «всё хорошо»")
    check("состояние", v[4]["state"], "unchecked")
    check("подсказка — прогнать сверку", "сверку" in v[4]["fix"], True)

    print("\nПривязка на аккаунт, который канала не видит — главная мина")
    c = fresh()
    A.ingest(c, {"profile": "63", "checked": NOW, "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [
            acc("aon0640285422", 0, sees=False, in_registry=True, has_secrets=True, bound=True),
            acc("a01099887766", 2, sees=True, manager=True, in_registry=True, has_secrets=True)]},
    ]}, now=NOW)
    v = A.verdicts(c, "3")
    check("состояние", v[1]["state"], "wrong_account")
    check("сказано, кто канал видит", "#2" in v[1]["fix"], True)
    check("подсказка называет инструмент", "bindings_fix" in v[1]["fix"], True)

    print("\nКанал виден и секреты есть, но привязки нет вообще")
    c = fresh()
    A.ingest(c, {"profile": "73", "checked": NOW, "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [
            acc("a01000011122", 0, sees=True, manager=True, in_registry=True, has_secrets=True)]},
    ]}, now=NOW)
    check("состояние", A.verdicts(c, "3")[1]["state"], "no_binding")


def test_no_secrets_travel():
    print("\nСекреты в студию не ездят")
    c = fresh()
    A.ingest(c, {"profile": "54", "checked": NOW, "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [
            dict(acc("phe0640280072", 0, sees=True, in_registry=True, has_secrets=True,
                     bound=True), password="s3cret", totp="ABCD EFGH")]}]}, now=NOW)
    cols = {r[1] for r in c.execute("PRAGMA table_info(pub_channel_access)")}
    check("в таблице нет столбца под пароль", "password" in cols, False)
    check("и под код 2FA", {"totp", "totp_secret"} & cols, set())
    dump = " ".join(str(x) for row in c.execute("SELECT * FROM pub_channel_access")
                    for x in row)
    check("пароль не записался нигде", "s3cret" in dump, False)
    check("код 2FA не записался нигде", "ABCD" in dump, False)
    check("маска записана", "phe…@gmail.com" in dump, True)


def test_report_replaces_own_profile_only():
    print("\nОтчёт заменяет факты своего профиля и не трогает чужие")
    c = fresh()
    A.ingest(c, {"profile": "54", "checked": NOW, "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [acc("phe1", 0, sees=True, in_registry=True,
                                                 has_secrets=True, bound=True)]}]}, now=NOW)
    A.ingest(c, {"profile": "34", "checked": NOW, "channels": [
        {"uc": "UC" + "b" * 22, "accounts": [acc("a34", 0, sees=True, in_registry=True,
                                                 has_secrets=True, bound=True)]}]}, now=NOW)
    check("оба профиля записаны", A.verdicts(c, "3")[1]["state"], "ok")
    check("и второй тоже", A.verdicts(c, "3")[2]["state"], "ok")

    # Аккаунт из профиля 54 убрали из браузера — повторный отчёт должен это отразить.
    A.ingest(c, {"profile": "54", "checked": NOW + 60, "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [acc("phe1", 0, sees=False, in_registry=True,
                                                 has_secrets=True, bound=True)]}]},
             now=NOW + 60)
    v = A.verdicts(c, "3")
    check("старый факт «видит» не остался", v[1]["state"], "not_in_profile")
    check("чужой профиль не пострадал", v[2]["state"], "ok")

    print("  канал не из студии в отчёте — считается, но не ломает приём")
    out = A.ingest(c, {"profile": "54", "checked": NOW, "channels": [
        {"uc": "UC" + "q" * 22, "accounts": [acc("phe1", 0, sees=True)]}]}, now=NOW)
    check("посчитан как чужой", out["unknown_channels"], 1)
    check("и записей не добавил", out["accounts"], 0)

    print("  чужой арендатор в сводку не попадает")
    check("каналов в вердиктах", sorted(A.verdicts(c, "3")), [1, 2, 3, 4, 5])


def test_summary():
    print("\nСводка по состояниям")
    c = fresh()
    A.ingest(c, {"profile": "54", "checked": NOW, "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [acc("a", 0, sees=True, in_registry=True,
                                                 has_secrets=True, bound=True)]},
        {"uc": "UC" + "b" * 22, "accounts": [acc("b", 0, sees=True)]},
    ]}, now=NOW)
    s = A.summary(c, "3")
    check("один настроен", s.get("ok"), 1)
    check("один без секретов", s.get("no_secrets"), 1)
    check("три не сверялись", s.get("unchecked"), 3)


def test_bad_payload():
    print("\nМусор в отчёте отвергается словами")
    c = fresh()
    for label, payload in (("нет профиля", {"channels": []}),
                           ("нет списка каналов", {"profile": "54"})):
        try:
            A.ingest(c, payload, now=NOW)
            check(label, False, True)
        except ValueError:
            check(label, True, True)
    try:
        A.ingest(c, {"profile": "54", "channels": [
            {"uc": "UC" + "a" * 22,
             "accounts": [acc(f"x{i}", i) for i in range(A.MAX_ACCOUNTS + 1)]}]}, now=NOW)
        check("слишком много аккаунтов", False, True)
    except ValueError:
        check("слишком много аккаунтов", True, True)
    out = A.ingest(c, {"profile": "54", "channels": [
        {"uc": "UC" + "a" * 22, "accounts": [{"sha": "коротко", "mask": "x"}]}]}, now=NOW)
    check("кривой хэш пропущен молча", out["accounts"], 0)


def main():
    test_verdicts()
    test_no_secrets_travel()
    test_report_replaces_own_profile_only()
    test_summary()
    test_bad_payload()
    print()
    if FAILS:
        print(f"провалов: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("все проверки прошли")


if __name__ == "__main__":
    main()
