#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Донор без субтитров — речь распознаётся на минской машине, без людей (03.10.2026).

Владелец: «автор отключил субтитры — без нашего участия сразу на минскую машину; не
вышло — пускай падает, чтобы ребята по новой искали донора». Проверка на боевой машине
03.10: с домашнего адреса Минска свежий yt-dlp качает звук (VPS и выходы получают 403),
видеокарта распознаёт 219 минут звука за 47 с.

Здесь без сети и без Минска — транспорт подменён. Проверяется:
* выпуск (`asr=True`) на «дорожек нет» заказывает распознавание и ЖДЁТ (ResourceDown), а
  не падает; повторный тик ждёт, не ходя в YouTube; готовый текст берётся из кэша;
* не вышло за две попытки — честный провал словами, донора ищут заново;
* скрытый ролик, возрастное ограничение, выключенная настройка — как раньше, сразу провал;
* проверка ссылки распознавание не заказывает, но и не бракует такого донора;
* ответ Минска разбирается, адрес машины наружу не попадает.

    .venv/bin/python tests/test_donor_asr.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

TMP = tempfile.mkdtemp(prefix="donor-asr-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DB"] = DB
os.environ["VS_SUBS_PROBE_PERIOD"] = "0"
os.environ["VS_SUBS_PROXIES"] = "direct"
os.environ["VS_SUBS_COOKIES"] = os.path.join(TMP, "нет-такого-файла.txt")
os.environ["VS_DONOR_ASR_HOST"] = "vova@10.9.8.7"

import donor_asr as A                                             # noqa: E402
import block_reasons                                              # noqa: E402
import pub_remote as PR                                           # noqa: E402
import pub_tags as PT                                             # noqa: E402
from pub_engine import ItemFailed, ResourceDown                   # noqa: E402

FAILS = []
URL = "https://youtu.be/UbEtjUNgcw0"
VID = "UbEtjUNgcw0"
TEXT = "Welcome to History and Sleep, the channel where we dive into history. " * 40


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class TranscriptsDisabled(Exception):
    pass


class VideoUnplayable(Exception):
    pass


class AgeRestricted(Exception):
    pass


class FakeApi:
    def __init__(self, plan):
        self.plan = plan

    def list(self, vid):
        self.plan["seen"] += 1
        raise self.plan["raise"]


def install(plan):
    stub = types.ModuleType("youtube_transcript_api")
    stub.YouTubeTranscriptApi = object
    sys.modules["youtube_transcript_api"] = stub
    PR._transcript_api = lambda proxy: FakeApi(plan)
    PR.SUBS_PROXIES = ["direct"]
    PR._subs_rest_until.clear()


def fresh():
    if os.path.exists(DB):
        os.remove(DB)


# фоновый поток в тесте не нужен: работу делаем руками через work_once
A.ensure_worker = lambda: True


def text_runner(url, cfg):
    return {"text": TEXT, "lang": "en", "seconds": 13140.0}


def broken_runner(url, cfg):
    return {"error": "звук с YouTube не скачался: HTTP Error 403"}


def main() -> int:
    print("Выпуск: дорожек нет — заказываем распознавание и ждём")
    fresh()
    plan = {"raise": TranscriptsDisabled("disabled"), "seen": 0}
    install(plan)
    try:
        PR.donor_subs(URL, asr=True)
        check("выпуск ждёт", "не упало", "ResourceDown")
    except ResourceDown as exc:
        check("выпуск ждёт, а не падает", getattr(exc, "kind", ""), PR.DOWN_ASR)
        check("и сказано, что распознаём на Минске", "распознаём речь донора" in str(exc), True)
    except ItemFailed as exc:
        check("выпуск ждёт", f"ItemFailed: {exc}", "ResourceDown")
    check("распознавание заказано", A.state(VID).get("status"), "queued")

    print("\n  следующий тик ждёт, не ходя в YouTube")
    seen = plan["seen"]
    try:
        PR.donor_subs(URL, asr=True)
    except ResourceDown:
        pass
    check("YouTube второй раз не спрашивали", plan["seen"], seen)

    print("\n  Минск вернул текст — выпуск берёт его из кэша, как субтитры")
    check("работа сделана", A.work_once(runner=text_runner), VID)
    check("строка — done", A.state(VID).get("status"), "done")
    got = PR.donor_subs(URL, asr=True)
    check("текст на месте", got["text"], TEXT.strip())
    check("язык и длина записаны", (got["lang"], got["seconds"]), ("en", 13140.0))
    check("и это кэш, без похода в YouTube", (got["cached"], plan["seen"]), (True, seen))

    print("\nНе вышло дважды — честный провал, донора ищут заново")
    fresh()
    install(plan)
    try:
        PR.donor_subs(URL, asr=True)
    except ResourceDown:
        pass
    check("первая попытка не вышла", A.work_once(runner=broken_runner), VID)
    st = A.state(VID)
    check("после первой — снова в очереди, с паузой", (st["status"], st["attempts"] >= 1,
          (st["not_before"] or 0) > time.time()), ("queued", True, True))
    check("пока пауза — работы нет", A.work_once(runner=broken_runner), "")
    A.work_once(now=time.time() + A.RETRY_PAUSE_SEC + 1, runner=broken_runner)
    check("после второй — failed", A.state(VID).get("status"), "failed")
    try:
        PR.donor_subs(URL, asr=True)
        check("выпуск падает", "не упало", "ItemFailed")
    except ItemFailed as exc:
        check("выпуск падает словами: распознать не вышло",
              "распознать речь на минской машине не вышло" in str(exc), True)
        check("и причина Минска в тексте", "403" in str(exc), True)
        b = block_reasons.explain(str(exc))
        check("экран: проблема в доноре", (b.get("kind"), b.get("title")),
              ("donor", "не распознали речь донора"))
        check("и что делать — другой донор", "другой ролик-донор" in b.get("fix", ""), True)
    except ResourceDown as exc:
        check("выпуск падает", f"ResourceDown: {exc}", "ItemFailed")

    print("\n  служба перезапустилась посреди распознавания — работу берут заново")
    fresh()
    A.request(VID, URL, now=time.time() - 3600)
    c = sqlite3.connect(DB)
    c.execute("UPDATE donor_asr SET status='running', updated=?", (time.time() - A.STALE_SEC - 5,))
    c.commit()
    c.close()
    check("брошенная работа подхвачена", A.work_once(runner=text_runner), VID)

    print("\nЧто распознаванием не лечится — как раньше")
    for name, exc in (("скрытый ролик", VideoUnplayable("gone")),
                      ("возрастное ограничение", AgeRestricted("age"))):
        fresh()
        install({"raise": exc, "seen": 0})
        saved = PR._video_really_gone
        PR._video_really_gone = lambda vid: True
        try:
            PR.donor_subs(URL, asr=True)
            check(f"{name}: провал", "не упало", "ItemFailed")
        except ItemFailed:
            check(f"{name}: сразу провал", True, True)
        except ResourceDown as e:
            check(f"{name}: сразу провал", f"ResourceDown: {e}", "ItemFailed")
        finally:
            PR._video_really_gone = saved
        check(f"{name}: распознавание не заказано", A.state(VID), {})

    fresh()
    install({"raise": TranscriptsDisabled("x"), "seen": 0})
    os.environ["VS_DONOR_ASR_HOST"] = ""
    try:
        PR.donor_subs(URL, asr=True)
        check("без настройки: провал", "не упало", "ItemFailed")
    except ItemFailed:
        check("без настройки Минска — как до 03.10: провал", True, True)
    except ResourceDown as e:
        check("без настройки: провал", f"ResourceDown: {e}", "ItemFailed")
    finally:
        os.environ["VS_DONOR_ASR_HOST"] = "vova@10.9.8.7"
    check("и ничего не заказано", A.state(VID), {})

    print("\nПроверка ссылки: не заказывает, но и не бракует")
    fresh()
    install({"raise": TranscriptsDisabled("x"), "seen": 0})
    facts = PT.check_donor(URL, subs=lambda link, net=True: PR.donor_subs(link, net=net, strict=False),
                           title_fetch=lambda u: "Leonardo da Vinci")
    check("вердикт — предупреждение, а не брак", (facts["verdict"], facts["reason"]), ("warn", "asr"))
    check("ставить можно", facts["can_plan"], True)
    check("распознавание НЕ заказано проверкой", A.state(VID), {})
    os.environ["VS_DONOR_ASR_HOST"] = ""
    try:
        facts = PT.check_donor(URL, subs=lambda link, net=True: PR.donor_subs(link, net=net, strict=False),
                               title_fetch=lambda u: "x")
        check("без настройки Минска — брак, как раньше", facts["reason"], "no_subs")
    finally:
        os.environ["VS_DONOR_ASR_HOST"] = "vova@10.9.8.7"

    print("\nОтвет Минска разбирается, адрес машины не утекает")
    cfg = A.settings()

    def fake_run(out, err="", code=0):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=out, stderr=err)

    saved = subprocess.run
    try:
        subprocess.run = fake_run('шум\n===ТЕКСТ==={"text": "abc", "lang": "en", "seconds": 5}\n')
        check("текст разобран", A.run_remote(URL, cfg).get("text"), "abc")
        subprocess.run = fake_run('===ОШИБКА==={"stage": "download", "error": "HTTP 403 at 10.9.8.7"}\n')
        got = A.run_remote(URL, cfg)
        check("ошибка скачивания — словами", got.get("error", "").startswith("звук с YouTube не скачался"), True)
        check("адрес машины скрыт", "10.9.8.7" in got.get("error", ""), False)
        subprocess.run = fake_run("", "ssh: connect to host 10.9.8.7 port 2222: refused", 255)
        got = A.run_remote(URL, cfg)
        check("машина не ответила — словами и без адреса",
              ("не ответила" in got.get("error", ""), "10.9.8.7" in got.get("error", "")), (True, False))
    finally:
        subprocess.run = saved

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
