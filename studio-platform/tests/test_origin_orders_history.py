#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Предупреждение «по этой ссылке уже заказывали» переживает смену запомненной ссылки.

Найдено ревью 21.09.2026 и воспроизведено на копии базы. С этого дня ссылка донора
запоминается САМА, как только её вставили в поле. Пометка о заказе жила одним полем
`project_origin.ordered`, и смена ссылки его обнуляла. Итог:

  1. по ролику A заказан платный рерайт — пометка стоит;
  2. человек по ошибке вставил ссылку B — она запомнилась сама, пометка обнулилась;
  3. вернул A — запомнилась снова, но пометки уже нет;
  4. «Получить текст» — второй платный рерайт A без единого предупреждения.

Сервер повторный заказ не останавливает (`ui_text_create` дубли не ищет), так что
это предупреждение было единственной защитой. Теперь оплаченные ролики проекта
записаны отдельно, по одному на ролик, и предупреждение смотрит туда.

Без стенда и сети: копия базы-образца, соседние сервисы подменены (как в
tests/test_batch_text_cover.py).

    .venv/bin/python tests/test_origin_orders_history.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import test_batch_text_cover as T                                 # noqa: E402

FAILS = []
REPEAT = "повтор создаст вторую задачу и второй платный рерайт"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    if not os.path.exists(T.SEED):
        print(f"НЕ ПРОВЕРЕНО: нет базы-образца {T.SEED}")
        return 1
    work = tempfile.mkdtemp(prefix="origin-orders-")
    try:
        db_path = os.path.join(work, "vsgen.db")
        shutil.copy(T.SEED, db_path)
        tenants = os.path.join(work, "tenants")
        os.environ["VS_DEV_DB"] = db_path
        os.environ["VS_TENANTS_DIR"] = tenants
        os.environ["VS_DEV_TENANT"] = "dev"
        sys.path.insert(0, BASE)
        sys.path.insert(0, os.path.join(BASE, "server"))
        import web.app as pult                                     # noqa: E402

        fake = T.Fake()
        pult._rw = fake._rw
        pult._cover = fake._cover
        pult.donor_subs = fake.donor_subs
        A = "https://youtu.be/AAAAAAAAAAA"
        B = "https://youtu.be/BBBBBBBBBBB"
        fake.subs[A] = "расшифровка донора " * 20
        fake.subs[B] = "расшифровка другого " * 20
        pult.donor_text = lambda u: fake.subs[u]

        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        T.make_project(c, "p1", "Проба", tenants=tenants)
        c.close()
        req = T.Req()
        detect = lambda v: pult.ui_source_detect(pult.DetectIn(value=v, project_id="p1"))
        remember = lambda v: pult.ui_project_source_save("p1", pult.SourceIn(link=v), req)

        print("Заказ по A, потом по ошибке B, потом снова A")
        pult.ui_text_create(pult.TextIn(mode="link", source=A, languages=["ru"],
                                        project_id="p1"), req)
        check("после заказа — предупреждение о повторе A",
              REPEAT in detect(A)["warn"], True)
        remember(B)                                  # запомнилась сама
        check("на B, пока запомнена B, про повтор A ни слова",
              REPEAT in detect(B)["warn"], False)
        check("но A, вставленная обратно, предупреждает о повторе и сейчас",
              REPEAT in detect(A)["warn"], True)
        o = remember(A)["origin"]                    # вернули — запомнилась снова
        check("пометка заказа вернулась к A", bool(o.get("ordered")), True)
        check("и видна над полем", "по этой ссылке заказывали" in (o.get("why") or ""),
              True)
        check("предупреждение о повторе на месте", REPEAT in detect(A)["warn"], True)
        check("форма адреса не важна — тот же ролик",
              REPEAT in detect("https://www.youtube.com/watch?v=AAAAAAAAAAA")["warn"], True)

        print("\nЗаказ по теме пометку ссылки обнуляет — история заказа A остаётся")
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        T.make_project(c, "p4", "Тема после ссылки", tenants=tenants)
        c.close()
        pult.ui_text_create(pult.TextIn(mode="link", source=A, languages=["ru"],
                                        project_id="p4"), req)
        pult._origin_topic("p4", "О чём-то другом")   # так пишет заказ «с нуля по теме»
        check("A после заказа по теме всё равно предупреждает о повторе",
              REPEAT in pult.ui_source_detect(
                  pult.DetectIn(value=A, project_id="p4"))["warn"], True)

        print("\n«Забыть ссылку» забывает запись о доноре, а не заказ")
        remember("")
        check("забытая и вставленная заново A предупреждает о повторе",
              REPEAT in detect(A)["warn"], True)

        print("\nЗаказ, сделанный до появления истории, тоже не теряется")
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        T.make_project(c, "p2", "Старый", tenants=tenants)
        c.close()
        # Так выглядит проект, чей заказ сделан до 21.09: пометка есть только в строке
        # источника, в истории заказов его нет.
        pult._origin_write("p2", kind="link", donor_url=A, video_id="AAAAAAAAAAA",
                           ordered=1789000000.0)
        pult.ui_project_source_save("p2", pult.SourceIn(link=B), req)
        check("после смены ссылки старый заказ A помнится",
              REPEAT in pult.ui_source_detect(
                  pult.DetectIn(value=A, project_id="p2"))["warn"], True)
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        T.make_project(c, "p3", "Старый, забытый", tenants=tenants)
        c.close()
        pult._origin_write("p3", kind="link", donor_url=A, video_id="AAAAAAAAAAA",
                           ordered=1789000000.0)
        pult.ui_project_source_save("p3", pult.SourceIn(link=""), req)
        check("и после «Забыть ссылку» тоже",
              REPEAT in pult.ui_source_detect(
                  pult.DetectIn(value=A, project_id="p3"))["warn"], True)

        print("\nНе заказанное повтором не называется")
        check("B не заказывали — предупреждения о повторе нет",
              REPEAT in pult.ui_source_detect(
                  pult.DetectIn(value=B, project_id="p2"))["warn"], False)
    finally:
        shutil.rmtree(work, ignore_errors=True)

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
