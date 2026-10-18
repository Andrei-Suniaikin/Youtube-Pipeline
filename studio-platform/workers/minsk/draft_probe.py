#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прочитать список роликов канала в Studio — только чтение (18.10.2026).

Зачем. У выпуска, чья загрузка оборвалась, статус «MP4 completion unproven»: файл мог
дойти до YouTube, а мог и нет, и номера ролика мы не знаем. Возвращать такой выпуск на
новую заливку вслепую нельзя — если черновик на канале всё-таки есть, появится второй.
Скрипт открывает профиль под бронью координатора, читает строки канала тем же кодом, что
и заливщик (yt_studio.read_videos), и печатает их: номер, заголовок, состояние. Ничего не
нажимает, ничего не меняет, браузер закрывает за собой.

Дальше решение простое: нашёлся черновик — его номер записывается заданию и выпуск
доводится по номеру; не нашёлся — выпуск возвращается на чистую заливку.

    ~/dp.sh <профиль> <UC>
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/vova")
sys.path.insert(0, "/home/vova/VideoStudio/src")
import upload_puller as P                      # noqa: E402
import upload_coordination as coord            # noqa: E402

OUT = Path.home() / "draft_probe"


def main() -> int:
    if len(sys.argv) < 3:
        print("нужны профиль и UC канала")
        return 2
    profile, uc = str(sys.argv[1]), str(sys.argv[2])
    OUT.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT, 0o700)
    out_dir = OUT / f"{profile}_{uc}_{time.strftime('%Y%m%dT%H%M%S')}"
    out_dir.mkdir()
    rec = {"profile": profile, "uc": uc, "started": time.time(), "rows": []}

    uid = P.resolve_user_id(profile)
    if P.profile_browser_alive(P.ADSPOWER_API, uid):
        print("профиль уже открыт — не трогаю")
        return 3
    try:
        lease_cm = coord.profile_lease(profile, P.lease_context())
    except coord.ProfileLeaseError as exc:
        print(f"бронь не дана: {str(exc)[:120]}")
        return 3

    driver = None
    owned = False
    original = []
    try:
        with lease_cm as lease:
            try:
                resp = P._ads_get(P.ADSPOWER_API, "/api/v1/browser/start",
                                  {"user_id": uid, "open_tabs": 1, "ip_tab": 0, "headless": 0},
                                  timeout=180)
                if resp.get("code") != 0:
                    raise RuntimeError(f"browser/start: {str(resp.get('msg'))[:80]}")
                owned = True
                data = resp.get("data") or {}
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                opts = Options()
                opts.add_experimental_option("debuggerAddress", (data.get("ws") or {}).get("selenium") or "")
                chromedriver = data.get("webdriver") or ""
                driver = (webdriver.Chrome(service=Service(chromedriver), options=opts)
                          if chromedriver else webdriver.Chrome(options=opts))
                driver.set_page_load_timeout(90)
                driver.set_script_timeout(45)
                original = list(driver.window_handles)
                driver.switch_to.new_window("tab")
                if lease:
                    lease.assert_held()
                import yt_studio as YS
                driver.get(f"https://studio.youtube.com/channel/{uc}/videos/upload")
                time.sleep(10)
                try:
                    YS.wait_videos(driver, timeout=60)
                except Exception as exc:                   # noqa: BLE001
                    rec["wait_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
                rows = YS.read_videos(driver) or []
                for r in rows[:40]:
                    if not isinstance(r, dict):
                        continue
                    rec["rows"].append({"videoId": r.get("videoId"), "title": (r.get("title") or "")[:90],
                                        "state": YS.classify(r)})
                driver.save_screenshot(str(out_dir / "videos.png"))
                rec["result"] = "read"
            except Exception as exc:                       # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            finally:
                try:
                    if driver is not None:
                        for h in list(driver.window_handles):
                            if h not in original:
                                driver.switch_to.window(h)
                                driver.close()
                        driver.service.stop()
                except Exception:                          # noqa: BLE001
                    pass
                if owned:
                    try:
                        P._ads_get(P.ADSPOWER_API, "/api/v1/browser/stop", {"user_id": uid}, timeout=60)
                        for _ in range(10):
                            time.sleep(2)
                            if not P.profile_browser_alive(P.ADSPOWER_API, uid):
                                rec["closed"] = True
                                break
                    except Exception as exc:               # noqa: BLE001
                        rec["close_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
    except coord.ProfileLeaseError as exc:
        print(f"бронь потеряна: {str(exc)[:120]}")
        return 3

    rec["finished"] = time.time()
    (out_dir / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"профиль {profile}, канал {uc}: строк {len(rec['rows'])}, "
          f"итог {rec.get('result') or rec.get('error')}")
    for r in rec["rows"]:
        print(f"   {r['state']:10} {r['videoId']} {r['title']}")
    print("улики:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
