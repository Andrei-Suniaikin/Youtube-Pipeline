#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ресурс ключа FastGen (пункт 8 ТЗ, батч 16.09.2026, фаза 2): снимок в базу и его показ.

Дополняет `tests/test_alerts.py` (поводы 9/10 читают снимок) и
`tests/test_preset_binding.py` (статус моделей в подписи, пункт 2.1) — здесь проверяется
то, что кладёт снимок и то, что его показывает:

  §3.1 `server/main.py:save_fastgen_usage` — форма JSON и кто её зовёт
       (`fastgen_status` при успехе, `dev/alerts.py` перед каждым тиком, сеть падает —
       снимок просто не обновляется);
  §3.3 `web/app.py:/ui/keys` — блок «FastGen»: данных нет → «данных пока нет».

`main.py` — 17 тыс. строк и сеет тестового пользователя на импорте, поэтому часть
проверок идёт отдельным процессом со своей временной базой и своим файлом ключей
(тот же приём, что в `tests/test_gpt_image_25.py`), а не импортом в этот процесс.

Часть, проверяющая `/ui/keys`, требует поднятого стенда (см. `dev/run.sh` или раздел
«Проверка результата» в task-spec.md) и своей копии базы:

    .venv/bin/python tests/test_fastgen_usage.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8080/studio")
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")

# Тот же сырой ответ, что задокументирован в task-spec.md как живой
# GET https://googler.fast-gen.ai/api/v6/usage.
USAGE_RAW = {
    "usage_window": "per_hour",
    "activation_date": 1788337851.0,
    "expiration_date": 1791016251.0,
    "account_limits": {
        "img_gen_per_hour_limit": 6000,
        "img_generation_threads_allowed": 25,
        "prompt_tokens_per_hour_limit": 200000,
        "speech_threads_allowed": 20,
    },
    "current_usage": {
        "hourly_usage": {"image_generation": 123, "video_generation": 4},
        "active_threads": {"image_threads": 3, "speech_threads": 1},
    },
}

FAILS = []
# Пропуск из-за отсутствия стенда раньше не влиял на код выхода: «проверено» и «не
# проверялось» выглядели одинаково. Теперь пропуск честно даёт 2, а осознанный объявляется
# переменной — тем же приёмом, что в tests/test_pult_wave3_browser.py.
SKIPPED = []
NO_STAND_OK = os.environ.get("VS_PULT_TEST_NO_STAND", "") not in ("", "0")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────── snapshot-логика отдельным процессом ────────────────────

_SCRIPT = r"""
import json, os, sys

BASE = %(base)r
sys.path[:0] = [os.path.join(BASE, "server"), os.path.join(BASE, "workers"),
                os.path.join(BASE, "dev")]
import main                                                        # noqa: E402
import alerts as A_dev                                             # noqa: E402

USAGE_RAW = %(usage)r
NOW1 = 1_800_000_000.0
R = {}

# §3.1 — форма JSON и upsert (один ключ, не строка на снимок)
c = main.db()
main.save_fastgen_usage(c, USAGE_RAW, NOW1)
row = c.execute("SELECT v, updated FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["snap1"] = json.loads(row[0])
R["snap1_updated"] = row[1]

usage2 = dict(USAGE_RAW)
usage2["current_usage"] = {"hourly_usage": {"image_generation": 999}, "active_threads": {}}
main.save_fastgen_usage(c, usage2, NOW1 + 100)
row2 = c.execute("SELECT v, updated FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["snap2"] = json.loads(row2[0])
R["rowcount_after_upsert"] = c.execute(
    "SELECT COUNT(*) FROM admin_state WHERE k='fastgen_usage'").fetchone()[0]

# Обрывок с провайдера ("account_limits" не словарь) не должен ронять запись —
# то же правило "нет данных, а не выдумка", просто на уровне формы ответа.
main.save_fastgen_usage(c, {"account_limits": "not-a-dict"}, NOW1 + 101)
row3 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["snap3"] = json.loads(row3[0])
c.close()

# §3.1 — fastgen_status кладёт снимок при каждом успешном обновлении кэша
c = main.db()
c.execute("DELETE FROM admin_state WHERE k='fastgen_usage'")
c.commit()
c.close()


def fake_fg_get_with_usage(path, key):
    if path == "/api/v6/models":
        return []
    if path == "/api/stats/error-rate":
        return {"healthScores": {}, "data": []}
    if path == "/api/v6/usage":
        return USAGE_RAW
    raise AssertionError(path)


main._fg_get = fake_fg_get_with_usage
st = main.fastgen_status("fake-key", force=True)
R["status_usage_present"] = st.get("usage") is not None
c = main.db()
row4 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["status_triggered_snapshot"] = json.loads(row4[0]) if row4 else None
c.close()

# Ответ провайдера без /api/v6/usage — статус моделей всё равно считается,
# но снимок расхода писать нечем: молчим, а не пишем пустышку.
c = main.db()
c.execute("DELETE FROM admin_state WHERE k='fastgen_usage'")
c.commit()
c.close()


def fake_fg_get_no_usage(path, key):
    if path == "/api/v6/models":
        return []
    if path == "/api/stats/error-rate":
        return {"healthScores": {}, "data": []}
    if path == "/api/v6/usage":
        return None
    raise AssertionError(path)


main._fg_get = fake_fg_get_no_usage
main.fastgen_status("fake-key", force=True)
c = main.db()
row5 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["status_no_usage_snapshot"] = row5[0] if row5 else None
c.close()

# §3.1 — dev/alerts.py: ключ googler читается ЧЕРЕЗ main.keys()+main._first_credential
# (review issue 3, 16.09.2026) — тот же разбор, что и в кабинете, а не свой; это
# единственный способ увидеть скаляр, список И `googler_pool`. Файл — main.KEYS_PATH
# (тот же VS_SERVER_KEYS_PATH), кэш main.keys() инвалидируется по mtime/размеру файла,
# так что переписывать содержимое того же пути между сценариями достаточно.


def write_keys(obj):
    json.dump(obj, open(main.KEYS_PATH, "w"))


def googler_key():
    return main._first_credential(main.keys(), "googler")


try:
    os.remove(main.KEYS_PATH)
except FileNotFoundError:
    pass
R["googler_key_no_file"] = googler_key()

write_keys({"googler": "fg_live_string_key"})
R["googler_key_string"] = googler_key()

write_keys({"googler": ["fg_live_list_key", "second"]})
R["googler_key_list"] = googler_key()

write_keys({"other": "x"})
R["googler_key_missing"] = googler_key()

# Пул без скалярного "googler" — до фикса issue 3 свой разбор dev/alerts.py его не
# видел вовсе, и снимок молча не обновлялся.
write_keys({"googler_pool": ["fg_pool_key", "second"]})
R["googler_key_pool"] = googler_key()


# §3.1 — dev/alerts.py:refresh_fastgen_usage перед каждым тиком
class FakeResp:
    def __init__(self, ok=True, payload=None):
        self.ok = ok
        self._payload = payload or {}

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("boom")

    def json(self):
        return self._payload


network_calls = []


def fake_get_ok(url, headers=None, timeout=None):
    network_calls.append((url, dict(headers or {})))
    return FakeResp(True, USAGE_RAW)


def fake_get_fail(url, headers=None, timeout=None):
    network_calls.append((url, dict(headers or {})))
    raise RuntimeError("сеть недоступна")


# без ключа — вообще не ходим в сеть
try:
    os.remove(main.KEYS_PATH)
except FileNotFoundError:
    pass
network_calls.clear()
A_dev.requests.get = fake_get_ok
c = main.db()
c.execute("DELETE FROM admin_state WHERE k='fastgen_usage'")
c.commit()
A_dev.refresh_fastgen_usage(c, NOW1 + 500)
R["refresh_no_key_network_calls"] = len(network_calls)
row6 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["refresh_no_key_snapshot"] = row6[0] if row6 else None
c.close()

# ключ есть, сеть недоступна — не падает и не пишет снимок
write_keys({"googler": "fg_live_test_key"})
network_calls.clear()
A_dev.requests.get = fake_get_fail
c = main.db()
c.execute("DELETE FROM admin_state WHERE k='fastgen_usage'")
c.commit()
try:
    A_dev.refresh_fastgen_usage(c, NOW1 + 600)
    R["refresh_network_fail_raised"] = False
except Exception:
    R["refresh_network_fail_raised"] = True
row7 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["refresh_network_fail_snapshot"] = row7[0] if row7 else None
R["refresh_network_fail_called"] = len(network_calls)
c.close()

# ключ есть, сеть отвечает — снимок пишется через ту же save_fastgen_usage
network_calls.clear()
A_dev.requests.get = fake_get_ok
c = main.db()
c.execute("DELETE FROM admin_state WHERE k='fastgen_usage'")
c.commit()
A_dev.refresh_fastgen_usage(c, NOW1 + 700)
row8 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["refresh_success_snapshot"] = json.loads(row8[0]) if row8 else None
R["refresh_success_header"] = network_calls[0][1] if network_calls else None
R["refresh_success_url"] = network_calls[0][0] if network_calls else None
c.close()

# ключ доступен ТОЛЬКО через googler_pool — review issue 3: до фикса это молчало
write_keys({"googler_pool": ["fg_pool_only_key"]})
network_calls.clear()
A_dev.requests.get = fake_get_ok
c = main.db()
c.execute("DELETE FROM admin_state WHERE k='fastgen_usage'")
c.commit()
A_dev.refresh_fastgen_usage(c, NOW1 + 800)
row9 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
R["refresh_pool_only_snapshot"] = json.loads(row9[0]) if row9 else None
R["refresh_pool_only_header"] = network_calls[0][1] if network_calls else None
c.close()

# §11 (21.09.2026) — провайдер ОТВЕТИЛ и ключ не принял. До этой правки 401 и обрыв
# связи обрабатывались одинаково: снимок просто не обновлялся, и мёртвый ключ выглядел
# ровно как отсутствие интернета. Отличает их наличие `response` у исключения.
class FakeHTTPError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.response = type("Resp", (), {"status_code": code})()


def fake_get_401(url, headers=None, timeout=None):
    network_calls.append((url, dict(headers or {})))
    raise FakeHTTPError(401)


write_keys({"googler": "fg_live_test_key"})
A_dev.requests.get = fake_get_401
c = main.db()
c.execute("DELETE FROM admin_state WHERE k IN ('fastgen_usage','fastgen_key_refused')")
c.commit()
A_dev.refresh_fastgen_usage(c, NOW1 + 900)
row10 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_key_refused'").fetchone()
R["refused_after_one"] = json.loads(row10[0]) if row10 else None
A_dev.refresh_fastgen_usage(c, NOW1 + 960)
row11 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_key_refused'").fetchone()
R["refused_after_two"] = json.loads(row11[0]) if row11 else None
# Обрыв связи заявку не заводит и не трогает: ответа провайдера не было вовсе.
A_dev.requests.get = fake_get_fail
A_dev.refresh_fastgen_usage(c, NOW1 + 1020)
row12 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_key_refused'").fetchone()
R["refused_after_outage"] = json.loads(row12[0]) if row12 else None
# Принятый запрос — единственное доказательство того, что ключ заменили.
A_dev.requests.get = fake_get_ok
A_dev.refresh_fastgen_usage(c, NOW1 + 1080)
row13 = c.execute("SELECT v FROM admin_state WHERE k='fastgen_key_refused'").fetchone()
R["refused_after_success"] = json.loads(row13[0]) if row13 else None
c.close()

print(json.dumps(R, ensure_ascii=False))
"""


def run_snapshot_checks() -> dict:
    """Гоняет `_SCRIPT` отдельным процессом со своей временной базой и HOME —
    как `main.py`, так и `dev/alerts.py` на импорте трогают файлы (сеют пользователя,
    читают `.env`), и подмешивать это в тестовый процесс не нужно."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HOME"] = os.path.join(BASE, "dev", "home")
        env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
        env["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        # Путь к файлу ключей главного кабинета — свой во временном каталоге; тест
        # сам пишет в него разные сценарии через main.KEYS_PATH (тот же путь,
        # `main.keys()` перечитывает при изменении файла), реальный
        # ~/video_studio_api_keys.json трогать нельзя.
        env["VS_SERVER_KEYS_PATH"] = os.path.join(tmp, "keys.json")
        script = _SCRIPT % {"base": BASE, "usage": USAGE_RAW}
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, env=env, timeout=180)
    if out.returncode != 0:
        raise SystemExit("подпроцесс упал:\n" + out.stderr[-4000:])
    return json.loads((out.stdout.strip().splitlines() or ["{}"])[-1])


# ───────────────────────── /ui/keys — блок «FastGen» ─────────────────────────

class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, payload=None, method=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(PULT + urllib.parse.quote(path, safe="/?&="),
                                     data=data,
                                     method=method or ("POST" if data is not None else "GET"))
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except ValueError:
                return exc.code, raw[:200]
        except urllib.error.URLError as exc:
            raise SystemExit(f"пульт {PULT} не отвечает: {exc.reason}. Сначала: dev/run.sh")


def db():
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def set_admin_state(key, value) -> None:
    c = db()
    try:
        c.execute("INSERT INTO admin_state(k,v,updated) VALUES(?,?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
                  (key, json.dumps(value, ensure_ascii=False) if value is not None else None,
                   0))
        if value is None:
            c.execute("DELETE FROM admin_state WHERE k=?", (key,))
        c.commit()
    finally:
        c.close()


def main() -> int:
    print("§3.1 — save_fastgen_usage и кто его зовёт")
    r = run_snapshot_checks()

    check("форма снимка — ровно нужные ключи",
          sorted(r["snap1"]), ["checked", "expires", "hourly", "limits", "threads", "window"])
    check("checked — время записи, а не из ответа провайдера", r["snap1"]["checked"], 1_800_000_000.0)
    check("expires — из expiration_date", r["snap1"]["expires"], 1791016251.0)
    check("window — из usage_window", r["snap1"]["window"], "per_hour")
    check("limits — из account_limits как есть",
          r["snap1"]["limits"]["img_gen_per_hour_limit"], 6000)
    check("hourly — из current_usage.hourly_usage",
          r["snap1"]["hourly"]["image_generation"], 123)
    check("threads — из current_usage.active_threads",
          r["snap1"]["threads"]["image_threads"], 3)

    check("повторная запись — upsert, а не вторая строка", r["rowcount_after_upsert"], 1)
    check("upsert заменяет цифры", r["snap2"]["hourly"]["image_generation"], 999)

    check("обрывок ответа провайдера не роняет запись (лимиты — «нет данных», не крах)",
          r["snap3"]["limits"], {})
    check("и остальные поля снимка тоже честно пустые", r["snap3"]["hourly"], {})

    print("\n  fastgen_status кладёт снимок при каждом успешном обновлении кэша")
    check("статус моделей увидел usage", r["status_usage_present"], True)
    check("и снимок появился в admin_state сам, без похода на /ui/keys",
          r["status_triggered_snapshot"] is not None, True)
    check("снимок от fastgen_status — тот же лимит, что вернул провайдер",
          r["status_triggered_snapshot"]["limits"]["img_gen_per_hour_limit"], 6000)

    check("ответ без /api/v6/usage — статус моделей не выдумывает снимок расхода",
          r["status_no_usage_snapshot"], None)

    print("\n  dev/alerts.py: ключ googler — ТЕМ ЖЕ разбором, что и кабинет "
          "(main.keys()+_first_credential, review issue 3)")
    check("строковый ключ", r["googler_key_string"], "fg_live_string_key")
    check("список ключей — берётся первый", r["googler_key_list"], "fg_live_list_key")
    check("файл есть, ключа googler нет", r["googler_key_missing"], "")
    check("файла ключей нет вовсе", r["googler_key_no_file"], "")
    check("ключ только в googler_pool — тоже подхватывается (issue 3, раньше молчало)",
          r["googler_key_pool"], "fg_pool_key")

    print("\n  dev/alerts.py:refresh_fastgen_usage перед каждым тиком")
    check("ключа нет — в сеть вообще не ходили", r["refresh_no_key_network_calls"], 0)
    check("и снимка не появилось", r["refresh_no_key_snapshot"], None)

    check("сеть недоступна — тик не падает (правило «сбой сети — не падать»)",
          r["refresh_network_fail_raised"], False)
    check("сходили один раз", r["refresh_network_fail_called"], 1)
    check("но снимок не обновился", r["refresh_network_fail_snapshot"], None)

    check("сеть отвечает — снимок лёг через save_fastgen_usage",
          r["refresh_success_snapshot"]["limits"]["img_gen_per_hour_limit"], 6000)
    check("запрос ушёл на /api/v6/usage",
          r["refresh_success_url"], "https://googler.fast-gen.ai/api/v6/usage")
    check("ключ передан заголовком X-API-Key, как у fastgen_status",
          r["refresh_success_header"].get("X-API-Key"), "fg_live_test_key")

    check("ключ только в пуле — снимок обновился и по нему (issue 3)",
          r["refresh_pool_only_snapshot"]["limits"]["img_gen_per_hour_limit"], 6000)
    check("и заголовок ушёл с ключом из пула",
          r["refresh_pool_only_header"].get("X-API-Key"), "fg_pool_only_key")

    print("\n  §11 — отказ провайдера отличается от обрыва связи (21.09.2026)")
    check("первый 401 записан заявкой",
          ((r["refused_after_one"] or {}).get("googler") or {}).get("seen"), 1)
    check("второй отказ подряд — подтверждение, вот теперь это повод",
          ((r["refused_after_two"] or {}).get("googler") or {}).get("seen"), 2)
    check("код ответа сохранён — в тревоге будет видно, что сказал провайдер",
          ((r["refused_after_two"] or {}).get("googler") or {}).get("code"), 401)
    check("в заявке маска, а не значение ключа",
          "fg_live_test_key" in json.dumps(r["refused_after_two"], ensure_ascii=False),
          False)
    check("обрыв связи заявку не трогает — ответа провайдера не было",
          ((r["refused_after_outage"] or {}).get("googler") or {}).get("seen"), 2)
    check("провайдер принял ключ — заявка снята, тревога погаснет",
          r["refused_after_success"], {})

    print("\nШапка модуля и строка «ни один из N поводов» обновлены (пункт §3.2)")
    pub_alerts_src = open(os.path.join(BASE, "server", "pub_alerts.py"), encoding="utf-8").read()
    check("докстрока pub_alerts.py говорит про одиннадцать поводов",
          "одиннадцать поводов" in pub_alerts_src, True)
    dev_alerts_src = open(os.path.join(BASE, "dev", "alerts.py"), encoding="utf-8").read()
    check("dev/alerts.py:show() считает одиннадцать поводов",
          "ни один из одиннадцати поводов" in dev_alerts_src, True)
    index_html_src = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    check("web/index.html:viewAdmin() тоже про одиннадцать поводов",
          "Ни один из одиннадцати поводов не выполнен." in index_html_src, True)
    test_alerts_src = open(os.path.join(BASE, "tests", "test_alerts.py"), encoding="utf-8").read()
    check("докстрока tests/test_alerts.py тоже обновлена",
          "одиннадцать поводов" in test_alerts_src, True)

    print("\nReview issue 2 — экран ключей не подставляет 0 вместо неизвестного расхода")
    check("images_used больше не подставляет 0 по умолчанию",
          "fg.images_used ?? 0" in index_html_src, False)
    check("threads_used больше не подставляет 0 по умолчанию",
          "fg.threads_used ?? 0" in index_html_src, False)
    check("вместо этого — явное «нет данных», когда числа нет",
          "fg.images_used != null ? fg.images_used : 'нет данных'" in index_html_src, True)
    check("то же самое для потоков",
          "fg.threads_used != null ? fg.threads_used : 'нет данных'" in index_html_src, True)
    check("пульт помечает, когда цифры сняты (§3.3 — «с пометкой, когда сняты»), "
          "а не просто держит поле в API без показа",
          # esc() вокруг when(...) — доработка ревьюера (дополнение 16.09, п.3): числа
          # и дату на экране ключей выводить через esc(), не только сырым значением.
          "снято ${esc(when(fg.checked))}" in index_html_src, True)
    # Доработка 3 (дополнение 16.09) — не только дата: расход картинок и занятые
    # потоки на этом же экране тоже должны идти через esc(), а не сырым значением.
    check("израсходовано картинок — через esc()",
          "${esc(fg.images_used != null ? fg.images_used : 'нет данных')}"
          in index_html_src, True)
    check("часовой лимит картинок — через esc()",
          "из ${esc(fg.images_limit)} картинок" in index_html_src, True)
    check("занято потоков — через esc()",
          "${esc(fg.threads_used != null ? fg.threads_used : 'нет данных')}"
          in index_html_src, True)
    check("лимит потоков — через esc()",
          "из ${esc(fg.threads_limit)}" in index_html_src, True)
    check("блок называется «FastGen» на экране ключей (§3.3)",
          '<div class="sec">FastGen</div>' in index_html_src, True)

    print("\n§3.3 — /ui/keys: блок «FastGen» (нужен поднятый стенд, dev/run.sh)")
    try:
        anon = Client()
        code, _ = anon.go("/ui/keys")
        if code == 401:
            me = Client()
            code, body = me.go("/ui/login", {"login": LOGIN, "password": PASSWORD})
            if code != 200:
                raise SystemExit(f"вход не удался ({code}): {body}")
        else:
            me = anon
    except SystemExit as exc:
        SKIPPED.append(f"экран ключей по HTTP: {exc}")
        print(f"  ПРОПУЩЕНО: {exc}")
        me = None

    if me is not None:
        set_admin_state("fastgen_usage", None)
        code, body = me.go("/ui/keys")
        check("экран ключей отвечает", code, 200)
        check("данных пока нет — так и сказано, а не нулями", body.get("fastgen"), None)

        now = 1_800_000_000.0
        snap = {
            "checked": now, "window": "per_hour", "expires": now + 3 * 86400,
            "limits": {"img_gen_per_hour_limit": 6000, "img_generation_threads_allowed": 25},
            "hourly": {"image_generation": 5400},
            "threads": {"image_threads": 4},
        }
        set_admin_state("fastgen_usage", snap)
        code, body = me.go("/ui/keys")
        fg = body.get("fastgen") or {}
        check("снимок есть — отдан как есть", code, 200)
        check("expires передан как есть", fg.get("expires"), snap["expires"])
        check("остаток посчитан на сервере (серверные часы — источник правды)",
              isinstance(fg.get("expires_in_sec"), int), True)
        check("израсходовано картинок — из hourly.image_generation", fg.get("images_used"), 5400)
        check("лимит картинок — из limits.img_gen_per_hour_limit", fg.get("images_limit"), 6000)
        check("занято потоков — из threads.image_threads", fg.get("threads_used"), 4)
        check("лимит потоков — из limits.img_generation_threads_allowed",
              fg.get("threads_limit"), 25)
        check("checked передан как есть — пульт обязан пометить, когда сняты цифры (§3.3)",
              fg.get("checked"), snap["checked"])

        set_admin_state("fastgen_usage", None)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    if SKIPPED and not NO_STAND_OK:
        print("СТЕНД НЕ ПОДНЯТ — проверки не выполнены:")
        for s_ in SKIPPED:
            print("  -", s_)
        print("Это не успех: без стенда не проверено, что блок FastGen на экране ключей"
              " показывает снимок, а не выдумывает цифры. Поднимите стенд либо объявите"
              " пропуск осознанно: VS_PULT_TEST_NO_STAND=1.")
        return 2
    print("все проверки прошли" + (" (стенд пропущен осознанно)" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
