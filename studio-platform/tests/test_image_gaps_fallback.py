#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Недостающий кадр: повтор позже, потом соседний кадр, сверх 2% — разбор (05.10.2026).

Владелец, по тревогам «Не хватает 1 кадров из 646: сцены 25» — «что минимально повлияет на
контент?» — и решение:
  1. «Generation failed, please try again later» — повторить кадр позже (15 мин, до 3 раз);
  2. отказ фильтра после трёх смягчений и всё, что не далось, — соседний кадр;
  3. больше 2% ролика — подмены нет, задача падает, как раньше: это уже беда темы.

Подмена честная. Файл сцены не подделывается и квитанция ему не выписывается: сцена
пишется в googler/image_gaps.json с причиной и исключается из проверки квитанций, а рендер
и так отдаёт время сцены без картинки предыдущей (render_worker._collapse_missing_images).

Проверяется: правило 2%; кого повторять позже; запись и уборка image_gaps.json; что
проверка квитанций с исключённой сценой проходит, а без исключения — падает, как раньше;
что конец прогона действительно так устроен.

    .venv/bin/python tests/test_image_gaps_fallback.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))
os.environ.setdefault("VS_TEST_NO_NET", "1")

import googler_worker as G                                        # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def project(n=5, missing=(3,)):
    """Проект с n сценами; сцены из missing не получены (квитанция stale, файла нет)."""
    pdir = os.path.realpath(tempfile.mkdtemp(prefix="gaps-"))   # macOS: /var → /private/var
    out = os.path.join(pdir, G.GOOGLER_IMAGES_DIR)
    os.makedirs(out)
    frames = {}
    for i in range(1, n + 1):
        key = f"{i:03d}"
        gen = hashlib.sha256(f"кадр {i}".encode()).hexdigest()
        if i in missing:
            frames[key] = {"generation_fingerprint": gen, "completed_fingerprint": None,
                           "status": "stale", "output_sha256": None,
                           "trusted_by_user": False, "legacy_trusted_at": None}
            continue
        data = f"картинка {i}".encode()
        open(os.path.join(out, f"{key}.jpg"), "wb").write(data)
        frames[key] = {"generation_fingerprint": gen, "completed_fingerprint": gen,
                       "status": "fresh", "output_sha256": hashlib.sha256(data).hexdigest(),
                       "trusted_by_user": False, "legacy_trusted_at": None}
    receipts = {"schema_version": G.IMAGE_RECEIPTS_SCHEMA, "frames": frames}
    rpath = os.path.join(out, G.IMAGE_RECEIPTS_FILE)
    G._atomic_write_json(rpath, receipts)
    return pdir, out, rpath, receipts


def main() -> int:
    print("Сколько сцен можно отдать соседнему кадру — 2%, но не меньше одной")
    check("240 сцен → 4", G.image_gaps_allowed(240), 4)
    check("646 сцен → 12", G.image_gaps_allowed(646), 12)
    check("40 сцен → 1", G.image_gaps_allowed(40), 1)

    print("\nКого повторять позже")
    giveup = {3: (G.GAP_FILTER, "фильтр"), 7: (G.GAP_LATER, "try again later"), 9: (G.GAP_OTHER, "x")}
    check("фильтр — нет; «позже» и сбой — да; без записи — да",
          G.late_retry_indices([3, 7, 9, 11], giveup), [7, 9, 11])
    check("вид сбоя по тексту провайдера",
          (G._giveup_kind("Generation failed, please try again later"), G._giveup_kind("rc=1")),
          (G.GAP_LATER, G.GAP_OTHER))

    print("\nЗапись сцен, отданных соседнему кадру")
    pdir, out, rpath, receipts = project()
    rows = G.write_image_gaps(out, [2], {2: (G.GAP_FILTER, "провайдер отказал фильтром безопасности (смягчений: 3)")})
    data = json.load(open(os.path.join(out, G.IMAGE_GAPS_FILE), encoding="utf-8"))
    check("сцена 3 записана с причиной и видом",
          (data["frames"][0]["scene"], data["frames"][0]["kind"], "фильтр" in data["frames"][0]["reason"]),
          (3, G.GAP_FILTER, True))
    check("и то, чем заменена", "предыдущей сцены" in data["frames"][0]["fallback"], True)
    check("функция вернула те же строки", rows == data["frames"], True)
    G.write_image_gaps(out, [])
    check("пропусков нет — файл убран", os.path.exists(os.path.join(out, G.IMAGE_GAPS_FILE)), False)

    print("\nПроверка квитанций")
    pdir, out, rpath, receipts = project()
    all_names = [f"{i:03d}.jpg" for i in range(1, 6)]
    try:
        G._reconcile_authoritative_image_set(pdir, rpath, receipts, all_names)
        got = "прошла"
    except G.ref_contract.ReferenceContractError as exc:
        got = str(exc)
    check("без исключения — падает, как раньше", got, "authoritative image receipt is not exact for frame 003")
    pdir, out, rpath, receipts = project()
    res = G._reconcile_authoritative_image_set(pdir, rpath, receipts, all_names,
                                               skip_frames={"003"})
    check("сцена 3 исключена — проходит", len(res["verified"]), 4)
    check("квитанции для сцены 3 не выписано", "003" in receipts["frames"], False)
    check("файла для сцены 3 не появилось", os.path.exists(os.path.join(out, "003.jpg")), False)

    print("\nОтказ фильтра на всех моделях — соседним кадром до 10% (11.10.2026)")
    # Ultima Scrisoare de pe Front: 5 из 242 под фильтром — раньше «больше 2%, нужен разбор».
    filt = {i: (G.GAP_FILTER, "фильтр") for i in (21, 27, 93, 94, 95)}
    check("242 сцены → всего до 24", G.image_filter_gaps_allowed(242), 24)
    check("5 отказов фильтра из 242 — принимаются",
          G.image_gaps_acceptable(list(filt), 242, filt), True)
    late5 = {i: (G.GAP_LATER, "try again later") for i in (1, 2, 3, 4, 5)}
    check("5 обычных сбоев из 242 — нет, как раньше (2%)",
          G.image_gaps_acceptable(list(late5), 242, late5), False)
    mix = {**filt, **{i: (G.GAP_OTHER, "x") for i in (1, 2, 3, 4)}}
    check("5 фильтра + 4 сбоя — принимаются", G.image_gaps_acceptable(list(mix), 242, mix), True)
    many = {i: (G.GAP_FILTER, "фильтр") for i in range(25)}
    check("25 отказов фильтра из 242 (>10%) — нет, нужен разбор",
          G.image_gaps_acceptable(list(many), 242, many), False)
    check("маленький ролик: 1 из 40 — да, как раньше",
          G.image_gaps_acceptable([3], 40, {3: (G.GAP_OTHER, "x")}), True)

    print("\nКонец прогона устроен именно так")
    src = open(os.path.join(BASE, "workers", "googler_worker.py"), encoding="utf-8").read()
    tail = src[src.find("_final_gaps = [i for i in range(total)"):]
    tail = tail[:tail.find("def cmd_check_usage")]
    check("в пределах допуска — сцены принимаются и записываются",
          "image_gaps_acceptable(_final_gaps, total)" in tail
          and "write_image_gaps(output_dir, _accepted)" in tail, True)
    check("и исключаются из проверки квитанций",
          'skip_frames={f"{index + 1:03d}" for index in _accepted}' in tail, True)

    print("\nУстаревший файл пропущенной сцены — в карантин, а не в рендер")
    pdir, out, rpath, receipts = project()
    open(os.path.join(out, "003.jpg"), "wb").write(b"old")
    res = G._reconcile_authoritative_image_set(pdir, rpath, receipts, all_names, skip_frames={"003"})
    check("убран из папки кадров", os.path.exists(os.path.join(out, "003.jpg")), False)
    check("и посчитан в карантине", res["quarantined"], 1)

    print("\nПропуск, которого не объявили, — по-прежнему ошибка")
    pdir, out, rpath, receipts = project(missing=(2, 3))
    try:
        G._reconcile_authoritative_image_set(pdir, rpath, receipts, all_names, skip_frames={"003"})
        got = "прошла"
    except G.ref_contract.ReferenceContractError as exc:
        got = str(exc)
    check("сцена 2 не объявлена — падает", got, "authoritative image receipt is not exact for frame 002")
    late = src[src.find("ПОВТОР ПОЗЖЕ (владелец 05.10.2026)"):src.find("_final_gaps = [i for i in range(total)")]
    check("повтор позже — только кого стоит и только в пределах допуска",
          "late_retry_indices(_gaps_now)" in late
          and "image_gaps_acceptable(_gaps_now, total)" in late, True)
    check("и с паузой между раундами", "_wake = time.time() + IMAGE_LATE_RETRY_SEC" in late, True)
    # 13.10.2026: молчаливая пауза = GEN_LEASE сервера → «завис», перезапуск с раунда 1 по кругу.
    check("в паузе воркер подаёт голос чаще, чем сервер считает его зависшим",
          "print_user(f\"⏳ Жду повтора" in late and G.IMAGE_LATE_WAIT_PULSE_SEC < 900, True)

    print("\nДвижок переносит подмену в журнал выпуска — один раз")
    import sqlite3
    sys.path.insert(0, os.path.join(BASE, "server"))
    import pub_services as S
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE job_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, ts REAL, line TEXT);
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT, repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
    """)
    c.execute("INSERT INTO job_logs(job_id, ts, line) VALUES(7, 1, ?)",
              ("[08:46:47.938] ⚠️ 🧩 [25] Сцена 25 заменена кадром предыдущей сцены: провайдер отказал "
               "фильтром безопасности (смягчений: 3)",))
    c.execute("INSERT INTO job_logs(job_id, ts, line) VALUES(7, 2, 'прочее')")
    svc = S.PubServices({"create_project": None, "enqueue": None, "project_dir": None})
    rel = {"release_id": "r1"}
    first = svc._note_image_gaps(c, rel, "7")
    again = svc._note_image_gaps(c, rel, "7")
    msgs = [r[0] for r in c.execute("SELECT message FROM pub_release_log")]
    check("одна строка в журнале выпуска", (first, again, len(msgs)), (1, 0, 1))
    check("со сценой и причиной", msgs and msgs[0].startswith("сцена 25 заменена кадром предыдущей сцены: провайдер"), True)

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
