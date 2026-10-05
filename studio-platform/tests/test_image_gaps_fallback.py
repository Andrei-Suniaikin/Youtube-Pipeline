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
    check("и то, чем заменена", "соседний" in data["frames"][0]["fallback"], True)
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

    print("\nКонец прогона устроен именно так")
    src = open(os.path.join(BASE, "workers", "googler_worker.py"), encoding="utf-8").read()
    tail = src[src.find("_final_gaps = [i for i in range(total)"):]
    tail = tail[:tail.find("def cmd_check_usage")]
    check("в пределах 2% — сцены принимаются и записываются",
          "len(_final_gaps) <= image_gaps_allowed(total)" in tail
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
    check("повтор позже — только кого стоит и только в пределах 2%",
          "late_retry_indices(_gaps_now)" in late and "image_gaps_allowed(total)" in late, True)
    check("и с паузой между раундами", "time.sleep(IMAGE_LATE_RETRY_SEC)" in late, True)

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
