#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настоящий лог задачи соседа в пульте: причина, а не её код.

Повод (20.09.2026, владелец): «можем добавить лог?». В тот день он полтора часа чинил
интернет, которого не ломал. Заказ текста трижды падал с «Ошибка: сеть недоступна
(E-NET)», а настоящая причина лежала в СЫРОМ логе задачи рерайта:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local
    issuer certificate

Публичный фильтр рерайта (`rewrite/log_public.py`, `_classify_error`) по правилу
default-deny меняет текст исключения на код: в сообщении есть слово «сеть» — наружу
уходит `E-NET`, и человек идёт чинить сеть. Сама поломка починена (`dev/run.sh` даёт
связку certifi), а вот видимость причины закрывает `GET /ui/jobs/{service}/{id}/log`.

Что здесь проверяется:

  * у рерайта отдаётся СЫРОЙ лог с диска, а не публичный: в ответе видно
    `CERTIFICATE_VERIFY_FAILED`, а не «E-NET». Иначе ручки нет вовсе — она бы
    пересказывала ровно то, от чего её завели;
  * у Cover сырого файла не существует, зато `GET /jobs/{id}` отдаёт весь лог
    целиком — берётся он;
  * рерайт на другой машине: файла нет → отдаётся публичный лог, но НАЗВАННЫЙ
    публичным, с путём, где лежит настоящий. Молча подменить один другим значит
    воспроизвести исходную ловушку;
  * секреты режутся дважды — точной редактурой кабинета (`_redact_worker_output`
    через `_redact_calls_error`) и сеткой по образцу, которая работает и без
    поднятого кабинета;
  * хвост с потолком, и обрезка названа обрезкой;
  * ошибка чтения — внятный ответ, а не пустота;
  * права спрашиваются у соседа, а не сочиняются пультом;
  * имя задачи из URL не выводит за каталог логов;
  * ручка ничего не пишет.

Ни сети, ни соседей, ни стенда: сосед подменён, лог — временный файл.

    .venv/bin/python tests/test_job_log.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_in(label, needle, haystack):
    if needle in (haystack or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали:   {needle!r}\n        в строке: {haystack!r}")
        FAILS.append(label)


def check_not_in(label, needle, haystack):
    if needle not in (haystack or ""):
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        не должно быть: {needle!r}\n"
              f"        а строка:       {haystack!r}")
        FAILS.append(label)


CHILD = r'''
# -*- coding: utf-8 -*-
"""Поднимает пульт, подменяет соседей и снимает поведение ручки лога."""
import json, os, sys, urllib.parse

LOGS = os.environ["VS_TEST_LOGS"]

# Тот самый вечер, строка в строку. Ключ и пароль в логе — не украшение: сырой лог
# пишет пайплайн, и в него попадает всё, что сказал провайдер.
KEY_EXACT = "vs-secret-value-known-to-cabinet-0001"
KEY_SHAPED = "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF"
RAW_LOG = (
    "[19:41:02] 🌍 ru: начинаю (1 частей)\n"
    "[19:41:02] POST https://api.openai.com/v1/responses key=" + KEY_EXACT + "\n"
    "[19:41:09] ❌ ru: сетевая ошибка при обращении к провайдеру: "
    "HTTPSConnectionPool(host='api.openai.com', port=443): Max retries exceeded "
    "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: "
    "CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local "
    "issuer certificate (_ssl.c:1000)')))\n"
    "[19:41:09] Authorization: Bearer " + KEY_SHAPED + "\n"
)
# А это то, что видел человек. Ровно то же событие, переведённое публичным фильтром.
PUBLIC_LOG = (
    "[19:41:02] Задача принята\n"
    "[19:41:02] ru: старт — 1 частей\n"
    "[19:41:09] Ошибка: сеть недоступна (E-NET)\n"
)

JOB_RW = "0a6c6716dbd1"
JOB_GONE = "5a6a1f2eb6ba"          # задача рерайта, чей файл лежит на другой машине
JOB_EMPTY = "761806d9262d"
JOB_CV = "cover-1"
UID = "dev"

os.makedirs(os.path.join(LOGS, UID), exist_ok=True)
with open(os.path.join(LOGS, UID, JOB_RW + ".log"), "w", encoding="utf-8") as fh:
    fh.write(RAW_LOG)
# Длинный лог — на нём проверяется хвост: первая строка заведомо не влезет.
LONG = os.path.join(LOGS, UID, "long.log")
with open(LONG, "w", encoding="utf-8") as fh:
    fh.write("ПЕРВАЯ СТРОКА, которой в хвосте быть не должно\n")
    for i in range(4000):
        fh.write(f"[19:4{i % 10}:00] строка номер {i} " + "х" * 60 + "\n")
    fh.write("[19:59:59] ❌ последняя строка: вот она причина\n")

sys.path[:0] = [os.path.join(os.environ["VS_BASE"], "web"),
                os.path.join(os.environ["VS_BASE"], "server"),
                os.path.join(os.environ["VS_BASE"], "workers")]
import app
from fastapi import HTTPException

app.REWRITE_LOG_DIR = LOGS


class Req:
    class State:
        sid = "sid-test"
    state = State()


# ── сосед ────────────────────────────────────────────────────────────────────
# Отвечает ровно тем, чем отвечают настоящие: рерайт — своей задачей с ПУБЛИЧНЫМ
# логом в поле `log` (rewrite/server.py get_job отдаёт колонку из базы, а в базе с
# PATCH-2026-08-01-publiclog лежит прогресс), Cover — своей, где `log` полный.
ASKED = []


def fake_proxy_get(base, path, sid):
    ASKED.append((base, path, sid))
    jid = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    if base == app.COVER_BASE:
        if jid == JOB_CV:
            return 200, {"id": jid, "status": "failed", "error": "нет ключа провайдера",
                         "log": "Обложка: старт\nTraceback: KeyError('api_key')\n"}
        return 404, "job not found"
    if jid == JOB_RW:
        return 200, {"job_id": jid, "user_id": UID, "status": "failed",
                     "error": "1 из 1 задач не выполнено", "log": PUBLIC_LOG}
    if jid == JOB_GONE:
        return 200, {"job_id": jid, "user_id": UID, "status": "failed",
                     "error": "1 из 1 задач не выполнено", "log": PUBLIC_LOG}
    if jid == JOB_EMPTY:
        return 200, {"job_id": jid, "user_id": UID, "status": "pending",
                     "error": "", "log": ""}
    if jid == "long":
        return 200, {"job_id": jid, "user_id": UID, "status": "running",
                     "error": "", "log": PUBLIC_LOG}
    if jid.startswith(".."):
        # Задача с таким номером у соседа «есть»: проверяется не отказ, а то, что
        # имя из URL не превращается в путь наружу каталога логов.
        return 200, {"job_id": jid, "user_id": UID, "status": "failed", "log": ""}
    if jid == "alien":
        return 403, "access denied"
    if jid == "dots":
        # Сосед назвал владельцем «..» — путь к файлу это выводить из каталога не должно.
        return 200, {"job_id": jid, "user_id": "..", "status": "failed", "log": ""}
    return 404, "job not found"


app._proxy_get = fake_proxy_get


class FakeCabinet:
    """Кабинет знает ЗНАЧЕНИЯ ключей — сетка по образцу их не ловит."""
    @staticmethod
    def _redact_calls_error(text):
        return str(text or "").replace(KEY_EXACT, "[REDACTED]")


app._cabinet = lambda: FakeCabinet


out = {}

# ── 1. Рерайт: сырой лог с диска, а не публичный ─────────────────────────────
before = open(os.path.join(LOGS, UID, JOB_RW + ".log"), "rb").read()
r = app.ui_job_log("rewrite", JOB_RW, Req())
out["raw"] = r
out["file_untouched"] = (
    open(os.path.join(LOGS, UID, JOB_RW + ".log"), "rb").read() == before)

# ── 2. Кабинет не поднят: точной редактуры нет, сетка по образцу есть ───────
def no_cabinet():
    raise ImportError("кабинет отдельным процессом не поднят")


app._cabinet = no_cabinet
out["no_cabinet"] = app.ui_job_log("rewrite", JOB_RW, Req())
app._cabinet = lambda: FakeCabinet

# ── 3. Хвост с потолком ─────────────────────────────────────────────────────
out["tail"] = app.ui_job_log("rewrite", "long", Req(), tail=8192)
out["tail_capped"] = app.ui_job_log("rewrite", "long", Req(), tail=99999999)

# ── 4. Рерайт на другой машине: файла нет ───────────────────────────────────
out["gone"] = app.ui_job_log("rewrite", JOB_GONE, Req())

# ── 5. Файл есть, но не читается ────────────────────────────────────────────
real_tail = app._tail_file


def boom(path, limit):
    raise PermissionError("Permission denied")


app._tail_file = boom
out["unreadable"] = app.ui_job_log("rewrite", JOB_RW, Req())
app._tail_file = real_tail

# ── 6. Cover: файла у него нет, зато лог полный по HTTP ─────────────────────
out["cover"] = app.ui_job_log("cover", JOB_CV, Req())

# ── 7. Пустой лог — это ответ, а не пустота ─────────────────────────────────
out["empty"] = app.ui_job_log("rewrite", JOB_EMPTY, Req())

# ── 8. Права спрашивает сосед ───────────────────────────────────────────────
for name, service, jid in (("чужая", "rewrite", "alien"),
                           ("нет такой", "rewrite", "nosuchjob"),
                           ("нет сервиса", "render", JOB_RW)):
    try:
        app.ui_job_log(service, jid, Req())
        out.setdefault("refusals", {})[name] = "отказа не было"
    except HTTPException as exc:
        out.setdefault("refusals", {})[name] = f"{exc.status_code}: {exc.detail}"
out["asked"] = [p for _b, p, _s in ASKED]

# ── 9. Имя из URL за каталог логов не выводит ───────────────────────────────
out["path_escape"] = app.ui_job_log("rewrite", "../../../etc/passwd", Req())["path"]
out["path_dots_uid"] = app.ui_job_log("rewrite", "dots", Req())["path"]
out["logs_dir"] = LOGS

print("<<<JSON>>>" + json.dumps(out, ensure_ascii=False, default=str))
'''


def run_child():
    env = dict(os.environ)
    env["HOME"] = os.path.join(BASE, "dev", "home")
    env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
    env["VS_BASE"] = BASE
    env["VS_DEV_TENANT"] = "dev"
    with tempfile.TemporaryDirectory() as tmp:
        env["VS_DEV_DB"] = os.path.join(tmp, "joblog.db")
        env["VS_SERVER_DB"] = os.path.join(tmp, "joblog.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        env["VS_TEST_LOGS"] = os.path.join(tmp, "job_logs")
        script = os.path.join(tmp, "child.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(CHILD))
        done = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              env=env, timeout=300)
    if "<<<JSON>>>" not in done.stdout:
        raise SystemExit("пульт не поднялся:\n" + (done.stderr or done.stdout)[-3000:])
    return json.loads(done.stdout.split("<<<JSON>>>", 1)[1].splitlines()[0])


KEY_EXACT = "vs-secret-value-known-to-cabinet-0001"
KEY_SHAPED = "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF"
REAL_CAUSE = "CERTIFICATE_VERIFY_FAILED"


def main() -> int:
    got = run_child()
    raw = got["raw"]

    print("Рерайт: отдаётся сырой лог с диска, а не публичный пересказ")
    check("источник назван своим именем", raw["source"], "raw")
    check_in("настоящая причина видна", REAL_CAUSE, raw["text"])
    check_in("и та её половина, которую ищут глазами",
             "unable to get local issuer certificate", raw["text"])
    check_not_in("а код вместо причины в ответ не попал", "E-NET", raw["text"])
    check_in("сказано, что это за лог", "сырой лог задачи с диска", raw["why"])
    check("и где он лежит", raw["path"].endswith("dev/0a6c6716dbd1.log"), True)
    check("жалоб нет", raw["problem"], "")
    check("хвост не обрезан — лог короткий", raw["truncated"], False)
    check("ручка ничего не записала", got["file_untouched"], True)

    print("\nСекреты режутся, и сказано чем")
    check_not_in("ключ, известный кабинету, вырезан", KEY_EXACT, raw["text"])
    check_not_in("и похожий на ключ — тоже", KEY_SHAPED, raw["text"])
    check("редактура названа полной", raw["redacted"], "точная и по образцу")
    nc = got["no_cabinet"]
    check("без кабинета ручка всё равно отвечает", nc["source"], "raw")
    check_not_in("сетка по образцу режет и без него", KEY_SHAPED, nc["text"])
    check("и об этом сказано прямо", nc["redacted"], "только по образцу")
    check_in("а причина по-прежнему видна", REAL_CAUSE, nc["text"])

    print("\nХвост с потолком, и обрезка названа обрезкой")
    tail = got["tail"]
    check("обрезку признали", tail["truncated"], True)
    check("размер всего лога назван", tail["total_bytes"] > 200000, True)
    check("отдан хвост, а не начало", tail["bytes"] <= 8192, True)
    check("байты считаются байтами, а не знаками — лог русский",
          (tail["bytes"] > tail["chars"], tail["chars"] > 0), (True, True))
    check_not_in("начало лога в хвост не попало", "ПЕРВАЯ СТРОКА", tail["text"])
    check_in("а конец — попал", "вот она причина", tail["text"])
    check("обрезано по границе строки", tail["text"].startswith("["), True)
    check("потолок не обойти запросом",
          got["tail_capped"]["bytes"] <= 512 * 1024, True)

    print("\nРерайт на другой машине: публичный лог, но названный публичным")
    gone = got["gone"]
    check("источник назван публичным", gone["source"], "public")
    check_in("и сказано, что причина подменена кодом", "ПУБЛИЧНЫЙ лог", gone["why"])
    check_in("и где искать настоящий", "на машине рерайта", gone["why"])
    check_in("почему файла тут нет", "на другой машине", gone["problem"])
    check_in("сам публичный лог отдан", "E-NET", gone["text"])
    check("и размер назван по целому логу, а не по хвосту",
          gone["total_bytes"], gone["bytes"])

    print("\nОшибка чтения — внятный ответ, а не пустота")
    bad = got["unreadable"]
    check_in("названа причиной", "не прочитан (PermissionError)", bad["problem"])
    check("и вместо пустоты отдан публичный лог", bad["source"], "public")
    check_in("который тоже подписан", "ПУБЛИЧНЫЙ лог", bad["why"])

    print("\nCover: своего файла нет, но его лог и так полный")
    cover = got["cover"]
    check("источник — сам сервис", cover["source"], "service")
    check_in("лог отдан целиком", "KeyError('api_key')", cover["text"])
    check("пути к файлу у Cover нет", cover["path"], "")
    check_in("и сказано, почему так", "он у неё один и не фильтруется", cover["why"])

    print("\nПустой лог — это ответ")
    check("и он не притворяется логом", got["empty"]["text"], "")
    check_in("а говорит, в чём дело", "лог пуст", got["empty"]["problem"])

    print("\nПрава спрашивает сосед, пульт их не сочиняет")
    check_in("чужая задача — отказ соседа как есть", "403", got["refusals"]["чужая"])
    check_in("несуществующая — 404", "404", got["refusals"]["нет такой"])
    check_in("несуществующий сервис — 404 пульта", "нет сервиса",
             got["refusals"]["нет сервиса"])
    check("у несуществующего сервиса соседа не спрашивали",
          (any("nosuchjob" in p for p in got["asked"]),
           any("render" in p for p in got["asked"])), (True, False))

    print("\nИмя задачи из URL за каталог логов не выводит")
    check("точки и слэши обезврежены",
          got["path_escape"].startswith(got["logs_dir"] + os.sep), True)
    check("и ни один кусок пути не «..»",
          [x for x in got["path_escape"].split(os.sep) if x == ".."], [])
    check("нормализация пути ничего не меняет — идти вверх неоткуда",
          os.path.normpath(got["path_escape"]), got["path_escape"])
    check("владелец «..» превращается в anon",
          got["path_dots_uid"].startswith(
              os.path.join(got["logs_dir"], "anon") + os.sep), True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО {len(FAILS)}: " + "; ".join(FAILS))
        return 1
    print("всё сходится")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
