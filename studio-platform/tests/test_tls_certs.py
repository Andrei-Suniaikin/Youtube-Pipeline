#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стенд поднимается со связкой сертификатов — иначе всё «сеть недоступна».

20.09.2026 у владельца трижды подряд падал заказ текста: рерайт писал «Ошибка: сеть
недоступна (E-NET)», а `curl` к тому же провайдеру в том же терминале отвечал 200. В сыром
логе задачи нашлась настоящая причина:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

Python с python.org не берёт сертификаты из системной связки macOS: `ssl.get_default_verify_paths()`
отдаёт пустоту, и падает КАЖДЫЙ https-вызов — у рерайта, обложек, картинок, субтитров. Ошибка
при этом маскируется под сетевую, то есть человек чинит интернет вместо сертификатов.

Тест сторожит две вещи: связка ставится в `dev/run.sh` и она рабочая (рукопожатие с
провайдером проходит). Второе требует сети, поэтому при её отсутствии проверка честно
пропускается ненулевым кодом, а не выдаёт себя за успех.

    .venv/bin/python tests/test_tls_certs.py
"""
from __future__ import annotations

import os
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

FAILS = []
SKIPPED = []
NO_NET_OK = os.environ.get("VS_TEST_NO_NET", "") not in ("", "0")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    print("Запуск стенда задаёт связку сертификатов")
    run_sh = open(os.path.join(BASE, "dev", "run.sh"), encoding="utf-8").read()
    check("SSL_CERT_FILE выставляется", "SSL_CERT_FILE" in run_sh, True)
    check("и берётся из certifi, а не зашит путём",
          "certifi" in run_sh, True)
    check("заданное снаружи значение не перетирается",
          'if [ -z "$SSL_CERT_FILE" ]' in run_sh, True)
    # REQUESTS_CA_BUNDLE нужен отдельно: requests (а через него — соседи и воркеры)
    # смотрит на свою переменную и ssl-умолчание не читает.
    check("requests тоже получает связку", "REQUESTS_CA_BUNDLE" in run_sh, True)

    print("\nКлючи доходят до обложек")
    # Cover Studio с 18.09 не принимает ключ в запросе вовсе («используются только
    # серверные ключи») и читает СВОИ переменные COVER_*. Стенд не задавал ни одной, и
    # анализ превью донора оставался без ключа, хотя тот же ключ лежал в общем файле и
    # кабинетом использовался. Ключ должен жить в ОДНОМ месте: две копии расходятся
    # молча и всегда на платном вызове.
    for var, family in (("COVER_OPENAI_KEY", "openai"), ("COVER_FAST_GEN_KEY", "googler"),
                        ("COVER_VEO_KEY", "veo")):
        check(f"{var} берётся из общего файла ключей ({family})",
              var in run_sh and family in run_sh, True)
    check("заданное снаружи не перетирается",
          'eval "printf' in run_sh or "-z" in run_sh, True)
    check("ключ не печатается в вывод запуска",
          "echo \"$COVER_OPENAI_KEY" not in run_sh and "echo $_val" not in run_sh, True)

    print("\nСвязка в .venv есть и она рабочая")
    try:
        import certifi
    except ImportError:
        check("certifi установлен в .venv", "нет", "есть")
        certifi = None
    if certifi:
        path = certifi.where()
        check("файл связки на месте", os.path.exists(path), True)
        check("и он непустой", os.path.getsize(path) > 100_000, True)

        # Голый ssl на macOS-сборке python.org связки не видит — это и есть корень.
        # Проверка не требует сети: спрашиваем только, что именно видит ssl.
        print("\nБез связки ssl действительно слеп (корень поломки)")
        check("у голого ssl файла связки нет",
              bool(ssl.get_default_verify_paths().cafile), False)

        print("\nС этой связкой рукопожатие с провайдером проходит")
        ctx = ssl.create_default_context(cafile=path)
        try:
            urllib.request.urlopen("https://api.openai.com/v1/models",
                                   timeout=20, context=ctx)
            check("сервер ответил", "200 без ключа", "401")
        except urllib.error.HTTPError as exc:
            # 401 — успех: TLS состоялся, сервер отказал по ключу, а не по сертификату.
            check("TLS состоялся (отказ по ключу, а не по сертификату)",
                  exc.code in (401, 403), True)
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                check("TLS состоялся", f"сертификат не принят: {reason[:60]}", "рукопожатие")
            else:
                SKIPPED.append(f"сети нет: {reason[:70]}")
                print(f"  ПРОПУЩЕНО: сети нет ({reason[:70]})")

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    if SKIPPED and not NO_NET_OK:
        print("СЕТИ НЕТ — проверка рукопожатия не выполнена:")
        for s in SKIPPED:
            print("  -", s)
        print("Это не успех: именно она ловит поломку, из-за которой всё выглядит как"
              " «сеть недоступна». Объявите пропуск осознанно: VS_TEST_NO_NET=1.")
        return 2
    print("все проверки прошли" + (" (сеть пропущена осознанно)" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
