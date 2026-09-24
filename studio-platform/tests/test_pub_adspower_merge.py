#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Опись профилей AdsPower, Фаза 2b (task-spec.md, profiles-2026-09-23): слияние итога
успешной описи (`AdsPowerFetch` из Фазы 2a) в справочник `pub_profiles`.

Проверяется здесь только `server/pub_adspower.py::merge_host_profiles` — функция не ходит
в сеть вообще, только в переданный `sqlite3.Connection` (сбор без базы уже проверен
`tests/test_pub_adspower_fetch.py`, эта функция принимает результат сбора готовым, через
конструирование `ProfileRecord`/`AdsPowerFetch` вручную — так же, как это будет делать
Фаза 3 после успешного `fetch_host_profiles`):

  а) первая опись заводит профили (UPSERT → INSERT), `title` = `name` из AdsPower;
  б) вторая опись с одним пропавшим serial ставит ЕМУ `missing_since`, остальным — нет,
     и не перетирает `title`, если он уже был непустым (ручная заметка владельца);
  в) третья опись, где пропавший профиль снова нашёлся, сбрасывает `missing_since`
     обратно в `NULL` (`reappeared`);
  г) неуспех НЕ пишет ничего — здесь это проверено как транзакционность самой функции:
     если слияние обрывается на середине (исключение внутри транзакции), уже сделанные
     в РАМКАХ ЭТОГО вызова изменения откатываются целиком, старые данные не искажены;
  д) два профиля с одинаковым `serial`, но разными `default_host_id`, — две независимые
     строки, `active_now`/`missing_since` одной машины не трогают строку другой;
  е) `active_now` пересчитывается заново каждый раз по `active_user_ids` — профиль,
     переставший быть активным, теряет пометку, а не копит её.

Сеть не требуется, база — в памяти:

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_pub_adspower_merge.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                              # noqa: E402
import pub_adspower as A                                              # noqa: E402

FAILS = []

NOW0 = 1_800_000_000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, cond):
    check(label, bool(cond), True)


class Clock:
    """Растущее фиктивное время — каждый вызов `merge_host_profiles` получает свою,
    отличную от предыдущей, метку `at`/`missing_since`/`last_seen` (проверяется, что
    значения действительно берутся из инжектированного `now`, а не из `time.time()`)."""

    def __init__(self, start: float = NOW0) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def fresh_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    c.commit()
    return c


def add_host(c: sqlite3.Connection, code: str, title: str) -> int:
    c.execute(
        "INSERT INTO pub_hosts(tenant_id,code,title,adspower_url,status,created) "
        "VALUES('dev',?,?,?,'active',?)", (code, title, "http://127.0.0.1:50325", NOW0))
    c.commit()
    return c.execute("SELECT id FROM pub_hosts WHERE code=?", (code,)).fetchone()["id"]


def rows_for_host(c: sqlite3.Connection, host_id: int) -> dict:
    return {r["serial"]: dict(r) for r in c.execute(
        "SELECT * FROM pub_profiles WHERE default_host_id=?", (host_id,)).fetchall()}


def dump_all(c: sqlite3.Connection) -> list:
    return [dict(r) for r in c.execute("SELECT * FROM pub_profiles ORDER BY id").fetchall()]


def rec(serial, uid, name="", group_name="", remark="", last_open_time="") -> A.ProfileRecord:
    return A.ProfileRecord(serial=serial, adspower_user_id=uid, name=name,
                            group_name=group_name, remark=remark,
                            last_open_time=last_open_time)


# ─────────────────────────── а/б/в: жизненный цикл одной машины ───────────────────────────

def check_lifecycle_new_missing_reappeared():
    print("а) первая опись заводит профили (UPSERT → INSERT)")
    c = fresh_db()
    clock = Clock()
    minsk = add_host(c, "minsk", "Минская машина")

    fetch1 = A.AdsPowerFetch(
        profiles=(
            rec("46", "u46", name="Профиль 46", group_name="history-pl",
                last_open_time="2026-09-22 10:00:00"),
            rec("47", "u47", name="Профиль 47"),
            rec("48", "u48", name="", remark="без имени в AdsPower"),
        ),
        active_user_ids=frozenset({"u46"}),
        pages=1,
    )
    s1 = A.merge_host_profiles(c, "dev", minsk, fetch1, now=clock)
    c.commit()

    check("а) MergeSummary.host_id", s1.host_id, minsk)
    check("а) MergeSummary.pages", s1.pages, 1)
    check("а) found == 3", s1.found, 3)
    check("а) new == 3 (все впервые)", s1.new, 3)
    check("а) reappeared == 0", s1.reappeared, 0)
    check("а) missing_now == 0", s1.missing_now, 0)
    check("а) active_now (сводка) == 1", s1.active_now, 1)
    check("а) at пришёл из инжектированного clock, не из time.time()",
          s1.at, clock.t)

    by_serial = rows_for_host(c, minsk)
    check("а) три строки заведены", set(by_serial), {"46", "47", "48"})
    check("а) title 46 = name из AdsPower", by_serial["46"]["title"], "Профиль 46")
    check("а) title 48 = NULL, т.к. name из AdsPower пуст", by_serial["48"]["title"], None)
    check("а) group_name 46 сохранён", by_serial["46"]["group_name"], "history-pl")
    check("а) last_open_time 46 сохранён", by_serial["46"]["last_open_time"],
          "2026-09-22 10:00:00")
    check("а) remark 48 сохранён", by_serial["48"]["remark"], "без имени в AdsPower")
    check("а) active_now 46 == 1 (в active_user_ids)", by_serial["46"]["active_now"], 1)
    check("а) active_now 47 == 0", by_serial["47"]["active_now"], 0)
    check("а) active_now 48 == 0", by_serial["48"]["active_now"], 0)
    check("а) missing_since всех трёх == NULL", [by_serial[s]["missing_since"] for s in by_serial],
          [None, None, None])

    # Владелец правит заметку профиля 48 руками — опись не должна её потом перетереть.
    c.execute("UPDATE pub_profiles SET title=? WHERE serial='48' AND default_host_id=?",
              ("Ручная заметка владельца", minsk))
    c.commit()

    print("\nб) вторая опись: 47 пропал, остальные — нет; ручная заметка 48 не перетёрта")
    fetch2 = A.AdsPowerFetch(
        profiles=(
            rec("46", "u46", name="Профиль 46 (переименован в AdsPower)"),
            rec("48", "u48", name="Новое имя 48 из AdsPower"),
        ),
        active_user_ids=frozenset({"u48"}),
        pages=1,
    )
    s2 = A.merge_host_profiles(c, "dev", minsk, fetch2, now=clock)
    c.commit()

    check("б) found == 2", s2.found, 2)
    check("б) new == 0", s2.new, 0)
    check("б) reappeared == 0 (47 ещё не считался пропавшим ДО этого вызова)", s2.reappeared, 0)
    check("б) missing_now == 1 (47 пропал впервые)", s2.missing_now, 1)
    check("б) active_now (сводка) == 1 (теперь 48)", s2.active_now, 1)

    by_serial2 = rows_for_host(c, minsk)
    check("б) 47 всё ещё существует строкой (не удалена)", "47" in by_serial2, True)
    check("б) 47 помечен пропавшим", by_serial2["47"]["missing_since"] is not None, True)
    check("б) 46 и 48 НЕ помечены пропавшими",
          [by_serial2["46"]["missing_since"], by_serial2["48"]["missing_since"]], [None, None])
    check("б) title 46 НЕ перезаписан — старый title уже был непустым ('Профиль 46')",
          by_serial2["46"]["title"], "Профиль 46")
    check("б) title 48 НЕ перетёрт ручной заметкой", by_serial2["48"]["title"],
          "Ручная заметка владельца")
    check("б) active_now 46 == 0 (пересчитан заново, больше не активен)",
          by_serial2["46"]["active_now"], 0)
    check("б) active_now 48 == 1", by_serial2["48"]["active_now"], 1)

    print("\nв) третья опись: 47 снова нашёлся — missing_since сброшен")
    fetch3 = A.AdsPowerFetch(
        profiles=(
            rec("46", "u46", name="Профиль 46"),
            rec("47", "u47", name="Профиль 47"),
            rec("48", "u48", name="Новое имя 48 из AdsPower"),
        ),
        active_user_ids=frozenset(),
        pages=1,
    )
    s3 = A.merge_host_profiles(c, "dev", minsk, fetch3, now=clock)
    c.commit()

    check("в) found == 3", s3.found, 3)
    check("в) new == 0", s3.new, 0)
    check("в) reappeared == 1 (47 вернулся)", s3.reappeared, 1)
    check("в) missing_now == 0 (в этом проходе никто не пропал)", s3.missing_now, 0)
    check("в) active_now (сводка) == 0 (active_user_ids пуст)", s3.active_now, 0)

    by_serial3 = rows_for_host(c, minsk)
    check("в) 47.missing_since сброшен в NULL", by_serial3["47"]["missing_since"], None)
    check("в) все active_now == 0 после пустого active_user_ids",
          [by_serial3[s]["active_now"] for s in by_serial3], [0, 0, 0])
    check("в) id строк не изменились между вызовами (47 — та же строка, не новая)",
          by_serial3["47"]["id"], by_serial2["47"]["id"])

    return c, minsk


# ─────────────────────────── г: неуспех/обрыв — откат целиком ───────────────────────────

class BoomAfter:
    """Обёртка над настоящим sqlite3.Connection: N-ный `execute` бросает исключение.

    Имитирует обрыв слияния ПОСЛЕ того, как часть строк уже вставлена/обновлена внутри
    транзакции — проверяет, что `merge_host_profiles` откатывает ВСЁ (не оставляет
    частично слитых изменений этого вызова), а не просто пробрасывает исключение дальше.
    """

    def __init__(self, real: sqlite3.Connection, boom_at_call: int) -> None:
        self._real = real
        self._n = 0
        self._boom_at_call = boom_at_call

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction

    def execute(self, sql, params=()):
        self._n += 1
        if self._n == self._boom_at_call:
            raise RuntimeError("имитация обрыва слияния посреди транзакции")
        return self._real.execute(sql, params)

    def commit(self) -> None:
        self._real.commit()

    def rollback(self) -> None:
        self._real.rollback()


def check_partial_failure_rolls_back_everything():
    print("\nг) обрыв посреди слияния откатывает ВСЁ (ничего не остаётся частично слитым)")
    c = fresh_db()
    minsk = add_host(c, "minsk", "Минская машина")
    # Одна строка уже существует ДО этого вызова (как если бы прошлая опись была успешной).
    baseline = A.AdsPowerFetch(profiles=(rec("1", "u1", name="Профиль 1"),),
                                active_user_ids=frozenset(), pages=1)
    A.merge_host_profiles(c, "dev", minsk, baseline, now=Clock())
    c.commit()
    before = dump_all(c)

    # Вызов "должен" завести 90 и 91 новыми строками, но обрывается на разборе 91-й
    # (execute #1 — BEGIN, #2 — SELECT «90», #3 — INSERT «90», #4 — SELECT «91» → бум).
    fetch = A.AdsPowerFetch(
        profiles=(rec("90", "u90", name="Профиль 90"), rec("91", "u91", name="Профиль 91")),
        active_user_ids=frozenset(), pages=1)
    boom = BoomAfter(c, boom_at_call=4)
    raised = False
    try:
        A.merge_host_profiles(boom, "dev", minsk, fetch, now=Clock())
    except RuntimeError:
        raised = True
    check_true("г) исключение действительно пробрасывается наружу", raised)

    after = dump_all(c)
    check("г) в базе не осталось ни одной новой/изменённой строки от оборвавшегося вызова",
          after, before)
    check("г) профиль «90», вставленный ДО обрыва, тоже откатился (не половинчатый коммит)",
          any(r["serial"] == "90" for r in after), False)


# ─────────────────────────── д: тот же serial на двух машинах — не конфликт ───────────────

def check_same_serial_two_hosts_do_not_conflict():
    print("\nд) один и тот же serial на двух разных машинах — две независимые строки")
    c = fresh_db()
    minsk = add_host(c, "minsk", "Минская машина")
    gdansk = add_host(c, "gdansk", "Гданьская машина")

    A.merge_host_profiles(c, "dev", minsk,
                           A.AdsPowerFetch(profiles=(rec("46", "u46-minsk", name="М-46"),),
                                            active_user_ids=frozenset({"u46-minsk"}), pages=1),
                           now=Clock())
    c.commit()
    A.merge_host_profiles(c, "dev", gdansk,
                           A.AdsPowerFetch(profiles=(rec("46", "u46-gdansk", name="Г-46"),),
                                            active_user_ids=frozenset(), pages=1),
                           now=Clock())
    c.commit()

    rows = c.execute("SELECT * FROM pub_profiles WHERE serial='46' "
                      "ORDER BY default_host_id").fetchall()
    check("д) два профиля с serial=46 существуют", len(rows), 2)
    check("д) у них разные default_host_id", {r["default_host_id"] for r in rows},
          {minsk, gdansk})
    by_host = {r["default_host_id"]: dict(r) for r in rows}
    check("д) title каждой строки — свой", by_host[minsk]["title"], "М-46")
    check("д) title другой строки — свой, не перепутан", by_host[gdansk]["title"], "Г-46")
    check("д) active_now минской строки == 1 (была в её active_user_ids)",
          by_host[minsk]["active_now"], 1)
    check("д) active_now гданьской строки == 0 (в её собственном active_user_ids её не было)",
          by_host[gdansk]["active_now"], 0)

    # Повторная опись гданьской машины не должна трогать минскую строку с тем же serial.
    A.merge_host_profiles(c, "dev", gdansk,
                           A.AdsPowerFetch(profiles=(), active_user_ids=frozenset(), pages=1),
                           now=Clock())
    c.commit()
    row_minsk_after = c.execute(
        "SELECT missing_since FROM pub_profiles WHERE serial='46' AND default_host_id=?",
        (minsk,)).fetchone()
    check("д) опись гданьской машины (даже пустая) не помечает пропавшим минский профиль",
          row_minsk_after["missing_since"], None)
    row_gdansk_after = c.execute(
        "SELECT missing_since FROM pub_profiles WHERE serial='46' AND default_host_id=?",
        (gdansk,)).fetchone()
    check("д) а свой (гданьский) профиль пустая опись помечает пропавшим",
          row_gdansk_after["missing_since"] is not None, True)


def main() -> int:
    check_lifecycle_new_missing_reappeared()
    check_partial_failure_rolls_back_everything()
    check_same_serial_two_hosts_do_not_conflict()

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
