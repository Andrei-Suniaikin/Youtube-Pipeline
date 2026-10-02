#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выходы для субтитров из прокси профилей AdsPower (02.10.2026).

Зачем. Субтитры снимались через ОДИН туннель, и YouTube ограничивал его за минуты: 60
выпусков разом упирались в квоту адреса и ждали по полчаса. Владелец (02.10.2026) дал
добро брать прокси профилей AdsPower — у каждого канала свой адрес, и это десятки
выходов вместо одного.

Почему тонким инструментом, а не прямо в студии. Прокси профиля — это удостоверение
канала: с него заходят в аккаунт и льют видео. Читать такие вещи на каждом тике, держать
в памяти процесса и писать в журналы — лишний риск на ровном месте. Здесь они читаются
РАЗОВО, складываются в строку настройки и больше нигде не живут.

Что наружу НЕ печатается: ни логин, ни пароль прокси. В консоль идут только хост, порт и
внешний адрес, который этот выход показывает миру, — по ним владелец и решает.

    .venv/bin/python dev/subs_exits_from_adspower.py --adspower http://ХОСТ:50325
    .venv/bin/python dev/subs_exits_from_adspower.py --adspower ... --check --out exits.env
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

PAGE = 100
PAUSE = 1.1          # AdsPower не любит чаще раза в секунду — тот же темп, что у описи


def _call(base: str, path: str, params: dict) -> dict:
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace") or "{}")
    if data.get("code") not in (0, None):
        raise SystemExit("AdsPower ответил отказом: %s" % str(data.get("msg"))[:200])
    return data.get("data") or {}


def профили(base: str) -> list:
    """Все профили постранично. Останавливаемся на первой неполной странице."""
    out, page = [], 1
    while page <= 50:
        # Пауза и ПЕРЕД первым запросом тоже: AdsPower считает частоту по себе, а не по
        # нашему процессу, и два прогона подряд отбиваются с «Too many request per
        # second» (проверено на боевой машине 02.10.2026).
        time.sleep(PAUSE)
        кусок = _call(base, "/api/v1/user/list", {"page": page, "page_size": PAGE})
        список = кусок.get("list") or []
        out.extend(список)
        if len(список) < PAGE:
            break
        page += 1
    return out


def выход(профиль: dict) -> dict:
    """Профиль → один выход. Пусто, если прокси не задан.

    Берём ТОЛЬКО поля прокси и серийный номер. Остальное из ответа AdsPower (логин и
    пароль аккаунта, группа, заметки) не читается вовсе — его здесь незачем знать.
    """
    cfg = профиль.get("user_proxy_config") or {}
    host = str(cfg.get("proxy_host") or "").strip()
    port = str(cfg.get("proxy_port") or "").strip()
    if not host or not port:
        return {}
    тип = (str(cfg.get("proxy_type") or "http").strip().lower() or "http")
    # `socks5h` просим явно: имя должен разрешать сам выход, иначе YouTube увидит, что
    # DNS-запрос пришёл не оттуда, откуда соединение.
    схема = {"socks5": "socks5h", "socks5h": "socks5h", "socks4": "socks4",
             "http": "http", "https": "http"}.get(тип, "http")
    user = str(cfg.get("proxy_user") or "")
    пароль = str(cfg.get("proxy_password") or "")
    доступ = ""
    if user:
        доступ = "%s:%s@" % (urllib.parse.quote(user, safe=""),
                             urllib.parse.quote(пароль, safe=""))
    return {"url": "%s://%s%s:%s" % (схема, доступ, host, port),
            "host": host, "port": port, "схема": схема,
            "serial": str(профиль.get("serial_number") or "")}


def проверить(url: str, timeout: float = 20.0) -> str:
    """Какой адрес показывает этот выход миру. Строка с «—» — выход не ответил.

    Проверяем через `requests`, а не через `urllib`: `urllib.request.ProxyHandler` не
    умеет SOCKS ВООБЩЕ, и первая версия этой проверки объявила «не ответившими» все
    socks-выходы разом — то есть почти все. Ошибка инструмента выглядела как приговор
    инфраструктуре.
    """
    try:
        import requests                                            # noqa: PLC0415
    except ImportError:
        return "— нет requests"
    try:
        r = requests.get("https://api.ipify.org",
                         proxies={"http": url, "https": url}, timeout=timeout)
        if r.status_code != 200:
            return "— HTTP %s" % r.status_code
        return r.text.strip()[:40]
    except Exception as exc:                                       # noqa: BLE001
        причина = type(exc).__name__
        if "Missing dependencies for SOCKS" in str(exc):
            причина = "нет PySocks — поставьте requests[socks]"
        return "— %s" % причина


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--adspower", required=True, help="адрес местного API AdsPower")
    p.add_argument("--check", action="store_true", help="проверить каждый выход живым запросом")
    p.add_argument("--limit", type=int, default=0, help="взять не больше стольких выходов")
    p.add_argument("--out", default="", help="куда записать строку настройки (права 600)")
    a = p.parse_args()

    все = профили(a.adspower)
    выходы, видели = [], set()
    без_прокси = 0
    for пр in все:
        в = выход(пр)
        if not в:
            без_прокси += 1
            continue
        ключ = (в["host"], в["port"])
        if ключ in видели:          # один прокси на несколько профилей — берём один раз
            continue
        видели.add(ключ)
        выходы.append(в)
    print("профилей в AdsPower: %d | с прокси: %d | без прокси: %d | разных выходов: %d"
          % (len(все), len(все) - без_прокси, без_прокси, len(выходы)))

    if a.limit:
        выходы = выходы[:a.limit]
        print("взято по ограничению: %d" % len(выходы))

    живые = []
    for в in выходы:
        строка = "   %-6s %-16s :%-6s %s" % (в["схема"], в["host"], в["port"], "")
        if a.check:
            адрес = проверить(в["url"])
            годен = not адрес.startswith("—")
            print(строка + ("виден как %s" % адрес if годен else "НЕ ОТВЕТИЛ %s" % адрес))
            if годен:
                живые.append(в)
        else:
            print(строка)
            живые.append(в)

    if a.check:
        print("живых выходов: %d из %d" % (len(живые), len(выходы)))
    строка = "VS_SUBS_PROXIES=" + ",".join(в["url"] for в in живые)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(строка + "\n")
        os.chmod(a.out, 0o600)
        print("строка настройки записана в %s (права 600). В консоль она не выводится: "
              "в ней логины и пароли прокси." % a.out)
    else:
        print("строка настройки готова (%d выходов). Укажите --out ФАЙЛ, чтобы записать "
              "её: в консоль не печатаю — в ней логины и пароли прокси." % len(живые))
    return 0 if живые else 1


if __name__ == "__main__":
    sys.exit(main())
