#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_selectors — контракт-тест селекторов YouTube Studio.

Отвечает на вопрос «Studio ещё не переехал?» за ~2 минуты, вместо того чтобы
узнавать это по 40 роликам, ушедшим в черновики после 20-минутной заливки.

Ничего не заливает и не публикует. Читает список видео канала и (если на канале
есть черновик) открывает его мастер, проверяя каждый селектор из yt_studio.SEL
и каждое поле состояния, на котором держится логика заливки.

Запуск:
  python3 tools/verify_selectors.py --profile 9001 --channel https://studio.youtube.com/channel/UCFAKE...
  python3 tools/verify_selectors.py --attach          # использовать сессию ytdom.py
  python3 tools/verify_selectors.py --attach --list-only   # не открывать мастер

Выход: 0 — всё живо, 1 — что-то отвалилось (см. ❌ в отчёте).
"""
import os
import re
import sys
import json
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.expanduser("~/VideoStudio/src"))

import requests  # noqa: E402
import yt_studio as YS  # noqa: E402

ADSPOWER_API = os.getenv("ADSPOWER_API", "http://localhost:50325")
SESSION_FILE = os.path.expanduser("~/.ytdom_session.json")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return ok


def _driver_from_session():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    if not os.path.exists(SESSION_FILE):
        sys.exit("❌ Нет сессии ytdom. Запусти: python3 tools/ytdom.py open --profile <N>")
    s = json.load(open(SESSION_FILE))
    o = Options()
    o.add_experimental_option("debuggerAddress", s["addr"])
    cd = s.get("chromedriver") or ""
    d = webdriver.Chrome(service=Service(cd), options=o) if cd else webdriver.Chrome(options=o)
    d.set_page_load_timeout(180)
    d.set_script_timeout(120)
    return d, s.get("profile")


def _driver_from_profile(profile):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    pid = str(profile)
    params = {"serial_number": pid} if pid.isdigit() else {"user_id": pid}
    addr = cd = None
    for att in range(1, 5):
        time.sleep(1.5)
        r = requests.get(f"{ADSPOWER_API}/api/v1/browser/start",
                         params={**params, "open_tabs": 1, "ip_tab": 0}, timeout=120).json()
        if r.get("code") == 0:
            addr = r["data"]["ws"]["selenium"]
            cd = r["data"].get("webdriver", "")
            break
        print(f"  … старт профиля: {r.get('msg')} (попытка {att})")
        time.sleep(3 * att)
    if not addr:
        sys.exit("❌ Профиль не стартовал (частая причина: Check Proxy Fail)")
    o = Options()
    o.add_experimental_option("debuggerAddress", addr)
    d = webdriver.Chrome(service=Service(cd), options=o) if cd else webdriver.Chrome(options=o)
    d.set_page_load_timeout(180)
    d.set_script_timeout(120)
    return d, pid


def _present(driver, selector, visible_only=False):
    """Есть ли элемент (сквозь shadow DOM)."""
    try:
        return YS._js(driver, """
            var el = deepFind(arguments[0], arguments[1]);
            return !!el;
        """, selector, visible_only)
    except Exception:
        return False


def verify_list_page(driver, channel_url):
    print("\n── Страница списка видео ──")
    uc = re.search(r"(UC[\w-]{20,})", channel_url or "")
    if not uc:
        # канал не задан — узнаём канал профиля через редирект Studio
        driver.get("https://studio.youtube.com/")
        time.sleep(8)
        uc = re.search(r"(UC[\w-]{20,})", driver.current_url)
    if uc:
        driver.get(f"https://studio.youtube.com/channel/{uc.group(1)}/videos/upload")
        time.sleep(8)

    check("страница Studio загружена", "studio.youtube.com" in driver.current_url,
          driver.current_url[:70])

    vids = YS.read_videos(driver)
    check("ytcp-video-row отдаёт Polymer-состояние", len(vids) > 0, f"{len(vids)} строк")
    if not vids:
        return None

    v = vids[0]
    for field in ("videoId", "title", "draftStatus", "privacy", "status"):
        check(f"поле video.{field}", bool(v.get(field) or field == "draftStatus"),
              str(v.get(field))[:40])
    check("video.visibility.effectiveStatus", bool(v.get("effectiveStatus")),
          str(v.get("effectiveStatus"))[:45])
    check("videoPrechecks читается", isinstance(v.get("prechecksDone"), bool),
          str(v.get("prechecksDone")))
    check("alteredContentSettings читается", isinstance(v.get("alteredDisclosed"), bool),
          str(v.get("alteredDisclosed")))

    kinds = {}
    for x in vids:
        k = YS.classify(x)
        kinds[k] = kinds.get(k, 0) + 1
    check("классификация статусов работает", "UNKNOWN" not in kinds or kinds.get("UNKNOWN", 0) == 0,
          json.dumps(kinds, ensure_ascii=False))

    # поиск устойчив к NFD (Studio отдаёт диакритику в NFD, наши строки в NFC)
    import unicodedata
    t = v.get("title") or ""
    if t:
        f1, _ = YS.find_video(driver, t, videos=vids)
        f2, _ = YS.find_video(driver, unicodedata.normalize("NFD", t), videos=vids)
        check("find_video: NFC и NFD дают один результат",
              f1 and f2 and f1["videoId"] == f2["videoId"],
              f1["videoId"] if f1 else "нет")

    drafts = [x for x in vids if YS.classify(x) == "DRAFT"]
    print(f"     (черновиков на канале: {len(drafts)})")
    return drafts[0] if drafts else None


def verify_wizard(driver, draft):
    print("\n── Мастер загрузки (на существующем черновике, без заливки) ──")
    if not draft:
        print("  ⚠️  черновиков нет — шаги мастера не проверены")
        print("      (создай черновик или запусти с каналом, где он есть)")
        return

    ok = YS.open_draft_wizard(driver, draft["videoId"])
    if not check(f"открытие мастера черновика ({SEL_HINT})", ok, draft["videoId"]):
        return

    w = YS.read_wizard(driver)
    check("ytcp-uploads-dialog отдаёт состояние", w.get("present"))
    check("workflowStep читается", bool(w.get("workflowStep")), str(w.get("workflowStep")))
    check("isVideoUploaded читается", w.get("isVideoUploaded") is not None,
          str(w.get("isVideoUploaded")))
    check("isCurrentStepInvalid читается", w.get("isCurrentStepInvalid") is not None,
          str(w.get("isCurrentStepInvalid")))
    check("shouldShowMonetizationStep читается", w.get("shouldShowMonetizationStep") is not None,
          str(w.get("shouldShowMonetizationStep")))
    check("errorSectionStatus читается", w.get("errorSectionStatus") is not None,
          str(w.get("errorSectionStatus")))

    # шаг Details
    check("поле заголовка (#textbox[aria-label])", _present(driver, YS.SEL["textbox"], True))
    check("радио «не для детей»", _present(driver, YS.SEL["kids_no"]))

    # переход на последний шаг — с ожиданием, а не «кликнул и надеюсь»
    w = YS.goto_step_and_wait(driver, YS.STEP_REVIEW, timeout=90) or YS.read_wizard(driver)
    check(f"переход на последний шаг (={YS.STEP_REVIEW})",
          w.get("workflowStep") == YS.STEP_REVIEW, str(w.get("workflowStep")))
    check("радио видимости отрисованы", (w.get("visibilityRadiosVisible") or 0) >= 3,
          f"{w.get('visibilityRadiosVisible')} шт")
    for name in ("radio_public", "radio_private", "radio_unlisted"):
        check(f"селектор {YS.SEL[name]}", _present(driver, YS.SEL[name], True))
    check("компонент видимости (availabilityRadioSelection)",
          w.get("availability") is not None, str(w.get("availability")))
    check("кнопка #done-button", bool(w.get("doneButton")),
          (w.get("doneButton") or {}).get("text", ""))

    # отложка — самая хрупкая часть
    print("\n── Отложка ──")
    exp = YS._js(driver, YS._SCHEDULE_EXPAND_JS)
    check("секция Schedule раскрывается (#second-container-expand-button)",
          exp in ("expanded", "already-open"), str(exp))
    time.sleep(3)
    check("ytcp-datetime-picker присутствует", _present(driver, YS.SEL["datetime_picker"], True))
    check("поле времени (#time-of-day-container input)",
          _present(driver, YS.SEL["time_input"], True))
    if _present(driver, YS.SEL["date_trigger"], True):
        check("триггер даты (#datepicker-trigger)", True)
        YS.click(driver, YS.SEL["date_trigger"])
        time.sleep(2.5)
        check("диалог календаря (ytcp-date-picker #dialog)",
              _present(driver, YS.SEL["date_dialog"], True))
        check("поле ввода даты в диалоге", _present(driver, YS.SEL["date_input"], True))
        try:
            driver.execute_script(
                "var d=document.querySelector('%s'); if(d&&d.close) d.close();" % YS.SEL["date_dialog"])
        except Exception:
            pass
    else:
        check("триггер даты (#datepicker-trigger)", False, "не найден")


def verify_collaboration(driver, videos):
    """Проверка блока коллабораций. Приглашение НЕ отправляется: доходим до
    списка результатов поиска и жмём Cancel."""
    print("\n── Коллаборации (без отправки приглашения) ──")
    target = next((v for v in videos if YS.classify(v) in ("PUBLIC", "UNLISTED")), None)
    if not target:
        print("  ⚠️  нет опубликованного видео — блок не проверен")
        return
    ok, msg = YS.set_collaborator(driver, target["videoId"], "ExampleChannel", save=False)
    check("блок коллабораций работает (поиск канала)", ok, str(msg)[:70])
    # страница не должна остаться с несохранёнными изменениями
    dirty = YS._js(driver, """
        var b = deepFind('ytcp-button#save', true);
        return b ? b.getAttribute('aria-disabled') : null;
    """)
    check("после Cancel несохранённых изменений нет", dirty == "true", f"save aria-disabled={dirty}")


SEL_HINT = "/video/{id}/edit → ytcp-button#action-1"


def main():
    p = argparse.ArgumentParser(description="Контракт-тест селекторов YouTube Studio")
    p.add_argument("--profile", help="AdsPower serial_number или user_id")
    p.add_argument("--attach", action="store_true", help="использовать сессию ytdom.py")
    p.add_argument("--channel", default="", help="URL канала (иначе — текущая страница)")
    p.add_argument("--list-only", action="store_true", help="не открывать мастер")
    p.add_argument("--keep-open", action="store_true", help="не гасить профиль в конце")
    a = p.parse_args()

    if a.attach:
        driver, profile = _driver_from_session()
    elif a.profile:
        driver, profile = _driver_from_profile(a.profile)
    else:
        sys.exit("❌ Укажи --profile <N> или --attach")

    print("═" * 62)
    print(f"  Контракт-тест селекторов Studio | профиль {profile}")
    print("═" * 62)

    try:
        draft = verify_list_page(driver, a.channel)
        if not a.list_only:
            verify_wizard(driver, draft)
            try:
                verify_collaboration(driver, YS.read_videos(driver))
            except Exception as e:
                check("блок коллабораций", False, str(e)[:70])
    finally:
        if a.profile and not a.keep_open and not a.attach:
            try:
                pid = str(profile)
                params = {"serial_number": pid} if pid.isdigit() else {"user_id": pid}
                requests.get(f"{ADSPOWER_API}/api/v1/browser/stop", params=params, timeout=30)
            except Exception:
                pass

    total = len(RESULTS)
    bad = [r for r in RESULTS if not r[1]]
    print("\n" + "═" * 62)
    print(f"  ИТОГ: {total - len(bad)}/{total} живо")
    if bad:
        print("\n  Отвалилось — Studio изменился, чинить здесь:")
        for name, _, detail in bad:
            print(f"    ❌ {name}  {detail}")
        print("\n  Дальше: python3 tools/ytdom.py open --profile <N> --url <страница>")
        print("          python3 tools/ytdom.py dump --grep <что искали>")
        print("  и обновить yt_studio.SEL по РЕАЛЬНОМУ дампу.")
    print("═" * 62)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
