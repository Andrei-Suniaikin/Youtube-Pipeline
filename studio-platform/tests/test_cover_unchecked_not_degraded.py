#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложка, которую не успели сверить, — не брак, и перерисовывать её за деньги нельзя.

04.10.2026, 08:54–09:21 UTC: 13 выпусков ушли в разбор с «обложка деградировала дважды:
текст наложен поверх картинки». На деле обе картинки были нарисованы и оплачены, а в
журнале модуля обложек стояло:

    ⚠ проверка текста не выполнена: TimeoutError: timed out
    Картинка сохранена: она уже оплачена, и выбрасывать её из-за неработающего контролёра нельзя.

Сверка ходит в подписку Codex через сервис текста; подписка общая с рерайтом, промптами
картинок и названиями, и под нагрузкой ответ из одного слова шёл 124 с — двум картинкам
не хватало 300 с. Модуль обложек честно отдавал картинку с пометкой fallback («для
человека не готово»), а студия принимала любой fallback за брак и перезаказывала —
то есть платила второй раз за ту же поломку контролёра, а на втором разе сдавалась.

Что проверяется:
  • модуль обложек отличает «сверить не удалось» от «сверка забраковала»;
  • этот признак доходит до ответа API (`unchecked_files`) и переживает запись задачи;
  • студия такую обложку принимает и пишет в журнал, что надпись надо посмотреть глазами;
  • настоящий брак (сверка не прошла) по-прежнему перезаказывается;
  • со старым модулем обложек (без нового поля) поведение прежнее;
  • ждать ответа сверки — 900 с, а не 300.

    .venv/bin/python tests/test_cover_unchecked_not_degraded.py
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "cover"))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []
NOW = 1_800_000_000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


try:
    from PIL import Image                                          # noqa: E402
except ImportError:                                                # pragma: no cover
    print("ПРОПУЩЕНО: нет PIL")
    raise SystemExit(2)

os.environ.pop("COVER_SUBSCRIPTION_TIMEOUT", None)
import cover_core as cc                                            # noqa: E402


def _img(color="red"):
    return Image.new("RGB", (64, 36), color)


# ─────────────────────────── модуль обложек ───────────────────────────

def test_core_marks_unchecked() -> None:
    print("\nМодуль обложек: «не смогли сверить» и «сверка забраковала» — разные вещи")
    was_vision, was_gen = cc._call_vision, cc._journaled_generate
    made = _img("green")
    try:
        cc._journaled_generate = lambda *a, **kw: made

        def timeout(*a, **kw):
            raise TimeoutError("timed out")
        cc._call_vision = timeout
        preset = cc.Preset(name="проба", language="Польский", simple_mode=True)
        r = cc._generate_simple_edit(_img("red"), preset, "fastgen-ключ", "", log_fn=lambda m: None)
        check("картинка отдана", r.image is made and r.success, True)
        check("для человека — по-прежнему «не до конца» (fallback)", r.used_fallback, True)
        check("и отдельно помечено: не сверено", r.unchecked, True)

        cc._call_vision = lambda *a, **kw: '{"overall_pass": false, "issues": "текст не переведён"}'
        r = cc._generate_simple_edit(_img("red"), preset, "fastgen-ключ", "", log_fn=lambda m: None)
        check("настоящий отказ сверки — fallback", r.used_fallback, True)
        check("...но не «не сверено»", r.unchecked, False)
    finally:
        cc._call_vision, cc._journaled_generate = was_vision, was_gen


def test_core_batch_reports_unchecked() -> None:
    print("\nПакетный режим кладёт несверенный файл в unchecked_files")
    was_dl, was_gen = cc._download_and_analyze_url, cc._generate_for_language
    out = tempfile.mkdtemp(prefix="cov-unchk-")
    try:
        cc._download_and_analyze_url = lambda *a, **kw: (_img(), {"video_title": "Проба"}, None)
        cc._generate_for_language = lambda *a, **kw: cc.CoverResult(
            success=True, image=_img("green"), used_fallback=True, unchecked=True)
        files, fb, unchk = [], [], []
        cc.process_batch(["https://youtu.be/abcdefghijk"], cc.Preset(name="проба"),
                         "", "", "fg", output_dir=out, languages=["Польский"],
                         log_fn=lambda m: None, out_files=files, fallback_files=fb,
                         unchecked_files=unchk)
        check("файл записан", len(files), 1)
        check("он в fallback (человек видит «не до конца»)", fb, files)
        check("и в unchecked (студия знает, что это не брак)", unchk, files)

        cc._generate_for_language = lambda *a, **kw: cc.CoverResult(
            success=True, image=_img("green"), used_fallback=True)
        files, fb, unchk = [], [], []
        cc.process_batch(["https://youtu.be/bbcdefghijk"], cc.Preset(name="проба"),
                         "", "", "fg", output_dir=out, languages=["Польский"],
                         log_fn=lambda m: None, out_files=files, fallback_files=fb,
                         unchecked_files=unchk)
        check("настоящий брак в unchecked не попадает", unchk, [])
        check("...а в fallback попадает", fb, files)

        # Вызов без нового параметра (старые вызывающие) не ломается.
        files = []
        cc.process_batch(["https://youtu.be/cbcdefghijk"], cc.Preset(name="проба"),
                         "", "", "fg", output_dir=out, languages=["Польский"],
                         log_fn=lambda m: None, out_files=files)
        check("без unchecked_files работает по-старому", len(files), 1)
    finally:
        cc._download_and_analyze_url, cc._generate_for_language = was_dl, was_gen


def test_timeout_default() -> None:
    print("\nСверке дают дождаться ответа подписки")
    check("по умолчанию 900 с", cc.SUBSCRIPTION_TIMEOUT, 900.0)


def test_job_and_api() -> None:
    print("\nПризнак переживает запись задачи и доходит до ответа API")
    import job_queue_cover as jq
    job = jq.Job(id="j1", unchecked_files=json.dumps(["a/pl_x.jpg"]),
                 fallback_files=json.dumps(["a/pl_x.jpg"]))
    from dataclasses import asdict
    back = jq._job_from_data(asdict(job))
    check("unchecked_files сохранён", json.loads(back.unchecked_files), ["a/pl_x.jpg"])
    old = asdict(job); old.pop("unchecked_files")
    check("старая запись без поля читается", jq._job_from_data(old).unchecked_files, "[]")
    try:
        import api_cover
    except Exception as exc:                                       # noqa: BLE001
        print(f"  ПРОПУСК API: {type(exc).__name__}: {exc}")
        return
    job.status = "completed_partial"; job.created_at = NOW
    resp = api_cover._job_to_response(job)
    check("в ответе API есть unchecked_files", resp.unchecked_files, ["a/pl_x.jpg"])


def test_server_wiring() -> None:
    print("\nСервер обложек передаёт и сохраняет признак (по исходнику: цикл задачи без очереди и ключей не поднять)")
    src = open(os.path.join(BASE, "cover", "cover_server.py"), encoding="utf-8").read()
    call = src[src.index("results = process_batch("):]
    call = call[:call.index(")\n")]
    check("process_batch получает unchecked_files", "unchecked_files=unchecked_files" in call, True)
    check("признак пишется в задачу по ходу и в конце",
          src.count("job.unchecked_files = json.dumps(unchecked_files") >= 2, True)
    check("продолжение задачи подхватывает прежний список",
          "existing_unchecked" in src and "list(existing_unchecked) if skip_existing" in src, True)
    check("сброс брака сбрасывает и признак", 'job.unchecked_files = "[]"' in src, True)
    check("поле есть в публичном ответе сервера", '"fallback_files", "unchecked_files"' in src, True)


# ─────────────────────────── студия ───────────────────────────

def test_degraded_predicate() -> None:
    print("\nСтудия: что считать браком")
    import pub_remote as R
    f = "a/pl_x.jpg"
    check("только несверенный — не брак",
          R.cover_degraded({"fallback_files": [f], "unchecked_files": [f]}), False)
    check("и счётчик fallback его не перевешивает",
          R.cover_degraded({"fallback_files": [f], "unchecked_files": [f], "fallback_count": 1}), False)
    check("...но помечен как несверенный",
          R.cover_unchecked({"fallback_files": [f], "unchecked_files": [f]}), True)
    check("один несверенный + один забракованный — брак",
          R.cover_degraded({"fallback_files": [f, "b.jpg"], "unchecked_files": [f]}), True)
    check("старый модуль без поля: fallback — брак, как раньше",
          R.cover_degraded({"fallback_files": [f]}), True)
    check("чистая обложка — не брак", R.cover_degraded({"fallback_files": []}), False)
    check("чистая обложка — сверена", R.cover_unchecked({"fallback_files": []}), False)


class FakeCover:
    created = []
    info = {}

    def __init__(self, key):
        pass

    def create_job(self, preset, lang, url):
        FakeCover.created.append(url)
        return f"new-{len(FakeCover.created)}"

    def job(self, job_id):
        return dict(FakeCover.info)

    def download(self, job_id, lang):
        return b"\xff\xd8" + b"x" * 8000


def svc_db():
    import pub_schema
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "error,created,updated) VALUES('rel-1','cover','job','cj1','running','re-ordered-degraded',?,?)",
              (NOW, NOW))
    c.commit()
    return c


def test_studio_accepts_unchecked() -> None:
    print("\nСтудия принимает несверенную обложку, а не платит за вторую")
    import pub_services as S
    pdir = tempfile.mkdtemp(prefix="cov-proj-")
    was_client, was_key = S.CoverClient, S.get_key
    S.CoverClient, S.get_key = FakeCover, (lambda c, name: "ключ")
    try:
        svc = S.PubServices({"create_project": None, "enqueue": None,
                             "project_dir": lambda *a, **k: pdir})
        rel = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "p1", "lang": "pl",
               "preset_cover": "обложки", "donor_url": "https://youtu.be/aaa", "updated": NOW}
        f = "a/pl_x.jpg"
        # Второй круг: первый заказ уже был перезаказан (error='re-ordered-degraded').
        FakeCover.created, FakeCover.info = [], {
            "status": "completed_partial", "finished_at": NOW, "results_count": 1,
            "output_files": [f], "fallback_files": [f], "unchecked_files": [f]}
        c = svc_db()
        out = svc.do_cover_ready(c, rel)
        check("новой платной обложки не заказано", FakeCover.created, [])
        check("обложка принята", out, {"payload": {"cover_bytes": 8002}})
        check("файл лёг в проект", os.path.exists(os.path.join(pdir, "cover.jpg")), True)
        st = c.execute("SELECT status FROM pub_release_jobs WHERE release_id='rel-1'").fetchone()[0]
        check("задача обложки закрыта как выполненная", st, "done")
        msgs = [r[0] for r in c.execute("SELECT message FROM pub_release_log WHERE release_id='rel-1'")]
        check("в журнале сказано, что надпись не сверена",
              any("не сверена" in m and "глазами" in m for m in msgs), True)

        print("\nНастоящий брак на втором круге — по-прежнему разбор")
        FakeCover.info = {"status": "completed_partial", "finished_at": NOW, "results_count": 1,
                          "output_files": [f], "fallback_files": [f], "unchecked_files": []}
        c = svc_db()
        try:
            svc.do_cover_ready(c, rel)
            got = "принято"
        except S.ItemFailed as exc:
            got = str(exc)
        check("выпуск уходит в разбор с прежней причиной",
              got, "обложка деградировала дважды: текст наложен поверх картинки")

        print("\nНастоящий брак на первом круге — один перезаказ, как раньше")
        c = svc_db()
        c.execute("UPDATE pub_release_jobs SET error=NULL")
        c.commit()
        FakeCover.created = []
        out = svc.do_cover_ready(c, rel)
        check("перезаказан один раз", (FakeCover.created, out), (["https://youtu.be/aaa"], {"wait": True}))
    finally:
        S.CoverClient, S.get_key = was_client, was_key


def main() -> int:
    print("Проверка: несверенная обложка — не брак")
    test_core_marks_unchecked()
    test_core_batch_reports_unchecked()
    test_timeout_default()
    test_job_and_api()
    test_server_wiring()
    test_degraded_predicate()
    test_studio_accepts_unchecked()
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
