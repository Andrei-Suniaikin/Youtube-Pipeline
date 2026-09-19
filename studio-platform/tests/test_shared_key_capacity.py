#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Один физический ключ — один счёт мест, а не три.

ДЕФЕКТ. Один и тот же ключ владелец вписывает в кабинет, в рерайт и в обложки, и
каждый сервис считает его ёмкость сам: кабинет — `_ServerCredentialPool` и
`_provider_call_budget`, рерайт — свой `ServerCredentialPool`, обложки — `_KeyBudget`.
Провайдер при этом считает ОДИН ключ. Три независимых потолка складываются, ключ
получает втрое больше одновременных вызовов, чем разрешено, и отвечает 429 — уже
после того, как вызовы оплачены.

Хуже того, сложить их «просто по имени семейства» нельзя: у одного вендора имена
РАЗНЫЕ. FastGen кабинет зовёт `googler` (семейство) и `recrafter` (провайдер),
обложки — `fastgen`, `flow`, `grok`; подписку ChatGPT кабинет зовёт `codex_home`,
рерайт — `codex`. Хэш берётся от имени, поэтому без таблицы вендоров один ключ
остаётся двумя-тремя разными.

ЧТО ПРОВЕРЯЕТСЯ:
  1. опознание ключа: разные локальные имена одного вендора дают ОДИН отпечаток;
  2. счёт: тринадцатый вызов при потолке 12 не проходит, кем бы он ни был сделан;
  3. ЕДИНИЦА СЧЁТА — одновременный вызов, а не задача: задача с тремя потоками берёт
     три места, и вторая такая же в тот же ключ уже не идёт (рерайт на этих же замках
     считает вызовы, и складывать с ним задачи значит складывать несравнимое);
  4. кабинет через этот счёт проведён, и при отказе он ОТДАЁТ свой локальный слот —
     и на серверной дорожке, и на byok с двумя своими ключами (иначе лечение перебора
     обернулось бы простоем, а byok-слот терялся бы навсегда);
  5. занятое место — это отложка со словами и цифрами, а не ошибка задачи, и строка
     про неё ОДНА на ожидание: проверяется боевой последовательностью (тик → задача
     спрятана своей отложкой → отложка истекла), а не снятием retry_after руками;
  6. ожидание чужого ключа считается СВОИМ счётчиком и не тратит бюджет возвратов byok,
     но ограничено по суммарному времени;
  7. расхождение «выдано потоков больше, чем весь потолок ключа» доходит до журнала
     задачи, в том числе на своём ключе владельца с ОДНИМ ключом;
  8. «не смог посчитать» (занят .gate) не выдаётся за «мест нет»;
  9. умолчание ничего не ужесточает: в режиме off кабинет ведёт себя как до B8.

    .venv/bin/python tests/test_shared_key_capacity.py
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "shared"))
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []
TMP = tempfile.mkdtemp(prefix="b8-shared-key-")

# Ключ-образец. Значение видно только тесту; в отпечаток уходит его хэш.
KEY = "sk-b8-shared-CAPACITY-0000000001"
CODEX_ACCOUNT = "acc-b8-0001"

# Каталог замков обязан быть приватным (0700) — модуль это проверяет и иначе
# отказывается работать. Это не придирка: мировой каталог позволил бы соседу по
# машине занять чужие места.
LOCKS = os.path.join(TMP, "locks")
os.makedirs(LOCKS, mode=0o700, exist_ok=True)
os.chmod(LOCKS, 0o700)
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS
# Маленькие потолки — чтобы тест был быстрым и читаемым. Сами числа проверяет
# tests/test_credential_budget.py, здесь важна МЕХАНИКА, а не значение.
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_OPENAI"] = "3"
os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_FASTGEN"] = "3"

# Кабинет поднимается без фона и на КОПИИ базы: тест не имеет права трогать стенд.
KEYS_PATH = os.path.join(TMP, "video_studio_api_keys.json")
# Потоки канала промптов пиним ЯВНО. Единица общего счёта — не задача, а одновременный
# вызов (main._key_budget_units), поэтому «одна задача = одно место» в тесте — это не
# умолчание платформы, а НАСТРОЙКА: на боевых умолчаниях та же задача берёт 10 мест.
# Отдельная проверка ниже («Мест берётся столько, сколько будет вызовов») гоняет именно
# боевой случай, когда задача одна, а вызовов несколько.
json.dump({"openai": KEY, "_concurrency": {"prompt_openai": {"threads": 1}}},
          open(KEYS_PATH, "w"))


def set_prompt_threads(n):
    """Переписать «потоков на проект» канала промптов и сбросить кэш настроек (10 с)."""
    json.dump({"openai": KEY, "_concurrency": {"prompt_openai": {"threads": int(n)}}},
              open(KEYS_PATH, "w"))
    main._invalidate_keys_cache()
    main._CONC_CACHE["ts"] = 0.0


DEV_DB = os.path.join(BASE, "dev", "vsgen.dev.db")
DB_COPY = os.path.join(TMP, "vsgen.copy.db")
if os.path.exists(DEV_DB):
    shutil.copy(DEV_DB, DB_COPY)
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_KEYS_PATH"] = KEYS_PATH
os.environ["VS_SERVER_DB"] = DB_COPY
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_KEY_BUDGET_MODE"] = "off"

import credential_budget as budget                                  # noqa: E402
import main                                                         # noqa: E402

# Канал промптов кабинета. «Одна задача = один вызов» здесь ВЕРНО только потому, что
# выше в keys-файле явно выставлены «потоков на проект» = 1: на боевых умолчаниях одна
# задача этого канала делает 10 одновременных вызовов (codex — 20).
PROVIDER = "prompt_openai"
OWN_KEY = "sk-b8-OWN-KEY-00000000000000001"


class FakeUser(dict):
    """users-строка так, как её читает кабинет: доступ как у sqlite3.Row."""

    def keys(self):                      # noqa: A003 — имя как у sqlite3.Row
        return list(super().keys())


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def free_all(leases):
    for lease in leases:
        if lease is not None:
            lease.release()


JOB_ID = 990001


def seed_queued_task():
    """Одна ждущая задача канала промптов на КОПИИ базы. Очередь чистим целиком:
    чужие строки стенда шедулер стал бы разбирать наравне с нашей."""
    c = main.db()
    try:
        uid = c.execute("SELECT id, tenant_id FROM users ORDER BY id").fetchone()
        c.execute("DELETE FROM queue_tasks")
        c.execute("DELETE FROM job_logs WHERE job_id=?", (JOB_ID,))
        c.execute("DELETE FROM admin_state WHERE k='queue_pause'")
        c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,"
                  "created,uid,provider,params) VALUES(?,?,?,'prompts','queued',?,?,?,'{}')",
                  (JOB_ID, uid["tenant_id"] or str(uid["id"]), "p-b8",
                   main._now() - 60, uid["id"], PROVIDER))
        c.commit()
    finally:
        c.close()
    main._queue_pause_forget_cache()
    return JOB_ID


def task_row(jid):
    c = main.db()
    try:
        return c.execute("SELECT * FROM queue_tasks WHERE job_id=?", (jid,)).fetchone()
    finally:
        c.close()


def mark_running(jid, token):
    """Задача в работе с этим claim-токеном — состояние, в котором её застаёт _run_task."""
    c = main.db()
    try:
        c.execute("UPDATE queue_tasks SET status='running', claimed=?, started=?, "
                  "claim_token=?, retry_after=NULL WHERE job_id=?",
                  (main._now(), main._now(), token, jid))
        c.commit()
    finally:
        c.close()


def clear_retry(jid):
    c = main.db()
    try:
        c.execute("UPDATE queue_tasks SET retry_after=NULL WHERE job_id=?", (jid,))
        c.commit()
    finally:
        c.close()


def job_log(jid):
    c = main.db()
    try:
        return [r["line"] for r in c.execute(
            "SELECT line FROM job_logs WHERE job_id=? ORDER BY id", (jid,))]
    finally:
        c.close()


def main_test() -> int:
    print("Один ключ опознаётся одинаково всеми тремя сервисами")
    ids = {name: budget.credential_identity(name, KEY)
           for name in ("googler", "fastgen", "recrafter", "flow", "grok")}
    check("FastGen под пятью именами — один отпечаток", len(set(ids.values())), 1)
    ids_openai = {name: budget.credential_identity(name, KEY)
                  for name in ("openai", "prompt_openai", "gpt_image2")}
    check("OpenAI кабинета, рерайта и обложек — один отпечаток",
          len(set(ids_openai.values())), 1)
    check("а FastGen и OpenAI — всё-таки разные ключи",
          ids["fastgen"] == ids_openai["openai"], False)
    check("отпечаток — это хэш, а не ключ", KEY in ids["fastgen"], False)

    # Подписка ChatGPT: если в auth.json лежит обычный OPENAI_API_KEY, это ТОТ ЖЕ
    # физический ключ, и деньги те же. Правило взято из боевого модуля дословно.
    codex_home = os.path.join(TMP, "codex")
    os.makedirs(codex_home, exist_ok=True)
    json.dump({"OPENAI_API_KEY": KEY}, open(os.path.join(codex_home, "auth.json"), "w"))
    check("codex_home с OPENAI_API_KEY считается ключом OpenAI, а не отдельным",
          budget.credential_identity("codex_home", codex_home), ids_openai["openai"])
    json.dump({"tokens": {"account_id": CODEX_ACCOUNT}},
              open(os.path.join(codex_home, "auth.json"), "w"))
    # Ждём отпечаток ИМЕННО от аккаунта: два кабинета с разными путями к одной и той
    # же подписке обязаны считаться одним ключом, иначе подписку посчитают дважды.
    by_account = hashlib.sha256(
        ("codex\0account:" + CODEX_ACCOUNT).encode()).hexdigest()
    check("подписка без ключа считается по аккаунту, а не по пути к папке",
          budget.credential_identity("codex_home", codex_home), by_account)
    json.dump({"nothing": 1}, open(os.path.join(codex_home, "auth.json"), "w"))
    try:
        budget.credential_identity("codex_home", codex_home)
        got = "пустил"
    except budget.BudgetConfigurationError:
        got = "ошибка настройки"
    check("подписка без ключа и без аккаунта — ошибка, а не молчаливый новый счёт",
          got, "ошибка настройки")
    check("незнакомое имя семейства не становится молча новым вендором",
          budget.vendor_of("нет-такого"), None)
    check("семейство с одним потребителем в общем счёте не участвует",
          budget.vendor_of("lumean"), None)

    print("\nСчёт ОБЩИЙ: трое зовут его под разными именами, потолок один")
    # Кабинет, рерайт и обложки — три РАЗНЫХ имени одного ключа. Потолок 3.
    held = [budget.try_acquire("googler", KEY, 1),       # кабинет
            budget.try_acquire("fastgen", KEY, 1),       # обложки
            budget.try_acquire("flow", KEY, 1)]          # обложки, другой провайдер
    check("три места выданы", [h is not None for h in held], [True, True, True])
    # ВОТ ОН, дефект из задачи: сегодня четвёртый вызов прошёл бы, потому что считал бы
    # его другой сервис своим счётчиком.
    check("четвёртый не проходит — ключ-то один",
          budget.try_acquire("recrafter", KEY, 1), None)
    held[0].release()
    fourth = budget.try_acquire("recrafter", KEY, 1)
    check("освободилось место — вызов снова проходит", fourth is not None, True)
    free_all(held[1:] + [fourth])
    check("после освобождения ключ снова пуст",
          budget.usage("fastgen", KEY), {"used": 0, "capacity": 3})

    print("\nУмолчание ничего не ужесточает: режим off — это сегодняшнее поведение")
    # Переменную именно УБИРАЕМ: проверяется поведение НЕНАСТРОЕННОЙ платформы. Общий
    # счёт превращает сумму трёх потолков в один, то есть кому-то обязательно станет
    # теснее вчерашнего, и такое решение принимает владелец, а не релиз.
    os.environ.pop("VS_KEY_BUDGET_MODE", None)
    check("без настройки счёт выключен", main._key_budget_mode(), "off")
    check("мусор в настройке тоже не включает его молча",
          (os.environ.__setitem__("VS_KEY_BUDGET_MODE", "ага"),
           main._key_budget_mode())[1], "off")
    os.environ["VS_KEY_BUDGET_MODE"] = "off"
    hold, note = main._key_budget_take(PROVIDER, KEY, 1)
    check("в режиме off место не занимается", (hold, note), (None, None))
    check("и общий счёт этого не видит", budget.usage("openai", KEY)["used"], 0)

    print("\nРежим enforce: кабинет ходит через общий счёт")
    os.environ["VS_KEY_BUDGET_MODE"] = "enforce"
    leases = [main._reserve_task_credential(PROVIDER, "prompts", {}) for _ in range(3)]
    check("три задачи кабинета взяли три места",
          [l is not None for l in leases], [True, True, True])
    check("и это видно в общем счёте, а не только у кабинета",
          budget.usage("openai", KEY), {"used": 3, "capacity": 3})
    note = {}
    denied = main._reserve_task_credential(PROVIDER, "prompts", {}, note=note)
    check("четвёртая задача места не получила", denied, None)
    check("причина названа словами", note.get("busy"), True)
    check("и с цифрами: сколько занято и из скольких",
          "занято 3 из 3" in note.get("line", ""), True)
    check("сказано, что счёт НАШ, а не отказ провайдера",
          "не отказ провайдера" in note.get("line", ""), True)
    check("в строку журнала не попадает сам ключ", KEY in note.get("line", ""), False)

    # Слот кабинета при отказе обязан вернуться в пул: иначе задача, которой ключ
    # достался бы честно, встала бы за несуществующей.
    loads = main.SERVER_CREDENTIAL_POOL.lanes().get(PROVIDER, {})
    check("локальный слот отказавшей задачи отдан обратно",
          sum(loads.values()), 3)

    free_all(leases)
    check("освобождение задачи отпускает и общее место",
          budget.usage("openai", KEY), {"used": 0, "capacity": 3})
    check("и локальный слот вместе с ним",
          sum(main.SERVER_CREDENTIAL_POOL.lanes().get(PROVIDER, {}).values()), 0)
    again = main._reserve_task_credential(PROVIDER, "prompts", {})
    check("после освобождения задача снова стартует", again is not None, True)
    free_all([again])

    print("\nРежим observe: цифры есть, отказа нет")
    os.environ["VS_KEY_BUDGET_MODE"] = "observe"
    busy = [budget.try_acquire("openai", KEY, 1) for _ in range(3)]
    note = {}
    watched = main._reserve_task_credential(PROVIDER, "prompts", {}, note=note)
    check("задача не задержана", watched is not None, True)
    check("но занятость записана", note.get("busy"), True)
    check("и сказано, что это наблюдение",
          "НЕ задержана" in note.get("line", ""), True)
    free_all(busy + [watched])

    print("\nЗанятый ключ — отложка со строкой в журнале, а не ошибка задачи")
    os.environ["VS_KEY_BUDGET_MODE"] = "enforce"
    busy = [budget.try_acquire("openai", KEY, 1) for _ in range(3)]
    jid = seed_queued_task()
    c = main.db()
    try:
        launched = main._claim_fair_tasks(c)
    finally:
        c.close()
    check("задача не запущена", [t["job_id"] for t in launched], [])
    row = task_row(jid)
    check("и не сломана: статус прежний", row["status"], "queued")
    check("ошибки в задаче нет", row["error"], None)
    waiting = float(row["retry_after"] or 0) - main._now()
    check("она отложена, а не пропущена молча", 30 < waiting <= 60, True)
    lines = job_log(jid)
    check("в журнале ОДНА строка про ключ", len(lines), 1)
    said = lines[0] if lines else ""      # пусто — это тоже ответ, а не повод падать
    check("строка называет ключ отпечатком, а не значением", KEY in said, False)
    check("и объясняет, что происходит", "занято 3 из 3" in said, True)

    # Строка одна на ОЖИДАНИЕ, а не одна на подход. Воспроизводим БОЕВУЮ
    # последовательность, а не удобную: тик 1 уже написал строку и отложил задачу;
    # тик 2 идёт, пока задача спрятана своей же отложкой (шедулер её не выбирает —
    # `AND (retry_after IS NULL OR retry_after<=?)`); потом отложка истекает, и тик 3
    # снова видит тот же занятый ключ. Раньше тик 2 стирал отметку «уже сказано»
    # (_purge_pause_wait_logs не видел спрятанную задачу), и тик 3 печатал дубль —
    # то есть ~60 одинаковых строк за час ожидания.
    c = main.db()
    try:
        main._claim_fair_tasks(c)          # задача спрятана отложкой, её тут нет
    finally:
        c.close()
    check("пока задача отложена, отметка про ключ жива",
          any(int(k[0]) == jid for k in main._PAUSE_WAIT_LOGGED), True)
    clear_retry(jid)                       # отложка истекла — задача снова видна
    c = main.db()
    try:
        main._claim_fair_tasks(c)
    finally:
        c.close()
    check("час ожидания не превращается в сотню строк", len(job_log(jid)), 1)
    free_all(busy)

    # А когда место освободилось, задача стартует сама — отложка её не хоронит.
    clear_retry(jid)
    c = main.db()
    try:
        launched = main._claim_fair_tasks(c)
    finally:
        c.close()
    check("освободился ключ — задача взята в работу",
          [t["job_id"] for t in launched], [jid])
    free_all([t.get("_credential_lease") for t in launched])

    print("\nСвой ключ владельца считается тем же счётом")
    # Свой ключ может быть ФИЗИЧЕСКИ тем же, что вписан в рерайт или в обложки, —
    # тогда это один кошелёк, а не два. Потоки на своём ключе при этом не зажимаются
    # (_own_key_threads): чужие деньги считает их владелец.
    os.environ["VS_KEY_BUDGET_MODE"] = "enforce"
    own = FakeUser(id=1, name="dev", role="user", tenant_id="dev", daily_quota=500,
                   keys=json.dumps({"openai": OWN_KEY}), key_opts="{}",
                   disabled=0, token="t", email="", password_hash="")
    credential, own_lease = main._own_credential_with_pool(own, PROVIDER, "prompts", {})
    check("задача идёт на свой ключ", credential, OWN_KEY)
    check("и место на нём занято в общем счёте",
          budget.usage("openai", OWN_KEY)["used"], 1)
    check("серверный ключ при этом не тронут",
          budget.usage("openai", KEY)["used"], 0)
    own_lease.release()
    check("освобождение отпускает и его",
          budget.usage("openai", OWN_KEY)["used"], 0)

    print("\nЗадаче выдано больше потоков, чем держит ключ — это слышно")
    # Настоящий случай: у ключа FastGen владельца замерено 25 потоков, а общий потолок
    # боевого счёта — 18. Молча ужать нельзя: воркер всё равно пойдёт своими 25, и учёт
    # станет украшением. Берём ключ целиком и говорим вслух.
    os.environ["VS_KEY_BUDGET_MODE"] = "enforce"
    hold, note = main._key_budget_take(PROVIDER, KEY, 7)      # потолок здесь 3
    check("место всё-таки выдано — задача не встаёт", hold is not None, True)
    check("ключ занят целиком", budget.usage("openai", KEY)["used"], 3)
    check("расхождение названо числами",
          "выдано 7 потоков" in note.get("line", "")
          and "потолок ключа — 3" in note.get("line", ""), True)
    check("и это не объявлено занятостью", note.get("busy"), False)
    free_all([hold])

    print("\nЗанятый gate — это «не смог посчитать», а не «мест нет»")
    # try_acquire возвращал None в ДВУХ разных случаях: мест нет и .gate держит чужая
    # выдача. Кабинет говорил про оба одинаково — «мест нет (нужно N)», хотя ключ мог
    # быть свободен, а занятость длиться доли секунды. Человеку это разные слова и
    # разное ожидание.
    os.environ["VS_KEY_BUDGET_MODE"] = "enforce"
    identity = budget.credential_identity("openai", KEY)
    gate = budget._open(budget._root() / (identity + ".gate"))
    fcntl.flock(gate, fcntl.LOCK_EX)         # «сосед ровно сейчас выдаёт места»
    try:
        hold_g, note_g = main._key_budget_take(PROVIDER, KEY, 1)
        lease_g, why_g = budget.try_acquire_ex("openai", KEY, 1)
    finally:
        budget._close(gate)
    check("место не выдано", hold_g, None)
    check("и причина названа своим именем", why_g, budget.BUSY_GATE)
    check("в журнале не написано «мест нет»",
          "мест нет" in note_g.get("line", ""), False)
    check("а написано, что посчитать не удалось",
          "состояние неизвестно" in note_g.get("line", ""), True)
    check("ключ при этом свободен — счёт его не занял",
          budget.usage("openai", KEY), {"used": 0, "capacity": 3})
    free_all([lease_g])

    print("\nМест берётся столько, сколько будет ВЫЗОВОВ, а не задач")
    # Главное утверждение задачи. Рерайт на этих же замках берёт место НА КАЖДЫЙ вызов,
    # поэтому считать кабинетом «одна задача = одно место» значит складывать
    # несравнимые величины: на боевых умолчаниях одна задача канала codex — это 20
    # одновременных вызовов подписки с потолком 1.
    os.environ["VS_KEY_BUDGET_MODE"] = "enforce"
    set_prompt_threads(3)                 # «потоков на проект» = вызовов в ключ
    check("кабинет считает вызовы, а не задачи",
          main._key_budget_units("prompts", {}, PROVIDER), 3)
    one = main._reserve_task_credential(PROVIDER, "prompts", {})
    check("одна задача заняла все три места", budget.usage("openai", KEY),
          {"used": 3, "capacity": 3})
    note = {}
    second = main._reserve_task_credential(PROVIDER, "prompts", {}, note=note)
    check("вторая задача в тот же ключ уже не идёт", second, None)
    check("и сказано почему", "занято 3 из 3" in note.get("line", ""), True)
    free_all([one])
    set_prompt_threads(1)
    check("настройка вернулась", main._key_budget_units("prompts", {}, PROVIDER), 1)

    print("\nСвой ключ: расхождение чисел СЛЫШНО и на одном ключе")
    # Мотивирующий случай правки — ключ ВЛАДЕЛЬЦА: 25 потоков против потолка 18. На
    # серверном ключе units зажаты ёмкостью слота, поэтому единственное место, где
    # расхождение вообще может прозвучать, — свой ключ. Ветка «ключ один» возвращалась
    # СРАЗУ, не заполнив note, и строка не доходила до журнала никогда.
    set_prompt_threads(7)                 # 7 вызовов против потолка 3
    own_loud = FakeUser(id=1, name="dev", role="user", tenant_id="dev", daily_quota=500,
                        keys=json.dumps({"openai": OWN_KEY}), key_opts="{}",
                        disabled=0, token="t", email="", password_hash="")
    note = {}
    cred, lease_loud = main._own_credential_with_pool(
        own_loud, PROVIDER, "prompts", {}, note=note)
    check("задача всё равно идёт", cred, OWN_KEY)
    check("но расхождение названо числами",
          "выдано 7 потоков" in note.get("line", "")
          and "потолок ключа — 3" in note.get("line", ""), True)
    check("и это не объявлено занятостью", note.get("busy"), False)
    # И эта строка должна ДОЙТИ до журнала задачи. Раньше _run_task печатал строку
    # только под условием key_note['busy'], а у squeeze busy=False — то есть
    # расхождение не звучало нигде.
    jid_loud = seed_queued_task()
    main._PAUSE_WAIT_LOGGED.clear()
    check("строка ушла в журнал задачи",
          main._announce_key_budget_note(jid_loud, note), True)
    said_loud = job_log(jid_loud)
    check("и она там ровно одна", len(said_loud), 1)
    check("с теми же числами",
          bool(said_loud) and "потолок ключа — 3" in said_loud[0], True)
    free_all([lease_loud])
    set_prompt_threads(1)

    print("\nДва своих ключа: при отказе общего счёта локальный слот возвращается")
    # Ветка двух и более своих ключей берёт slot у раздатчика, а потом просит место в
    # общем счёте. Забыть отдать slot при отказе — значит потерять byok-ёмкость
    # НАВСЕГДА: слот не освободит никто, задача-то не стартовала.
    own_two = FakeUser(id=1, name="dev", role="user", tenant_id="dev", daily_quota=500,
                       keys=json.dumps({"openai": [OWN_KEY, OWN_KEY + "-second"]}),
                       key_opts="{}", disabled=0, token="t", email="", password_hash="")
    hog = [budget.try_acquire("openai", OWN_KEY, 1) for _ in range(3)]
    hog += [budget.try_acquire("openai", OWN_KEY + "-second", 1) for _ in range(3)]
    check("оба своих ключа заняты соседом", [h is not None for h in hog],
          [True] * 6)
    note = {}
    cred2, lease2 = main._own_credential_with_pool(
        own_two, PROVIDER, "prompts", {}, note=note)
    check("slot не выдан: мест на ключе нет", lease2, None)
    check("причина названа", note.get("busy"), True)
    byok_lane = main.SERVER_CREDENTIAL_POOL.lanes().get(f"byok:{PROVIDER}", {})
    check("локальный слот byok-дорожки отдан обратно", sum(byok_lane.values()), 0)
    free_all(hog)
    after = main._own_credential_with_pool(own_two, PROVIDER, "prompts", {})
    check("освободилось — задача снова получает ключ", after[1] is not None, True)
    free_all([after[1]])

    print("\nОжидание чужого ключа не тратит бюджет возвратов byok")
    # BYOK_REQUEUE_MAX=5 придуман против вечного цикла «свой ключ исчез до старта».
    # Занятый соседом ключ — состояние нормальное; потратив на него пять возвратов,
    # enforce через пять минут молча переставал быть enforce.
    # Проверяем ПУТЬ, а не хелпер: ровно эту функцию зовёт _run_task обеими ветками.
    jid = seed_queued_task()
    main._BYOK_REQUEUES.pop(str(jid), None)
    main._KEY_BUDGET_WAITS.pop(str(jid), None)
    main._PAUSE_WAIT_LOGGED.clear()      # прошлый раздел уже говорил про этот ключ
    note = {"busy": True, "signature": f"key:openai:{main._cred_mask(KEY)}",
            "line": "⏳ ключ занят соседом"}
    deferred = []
    for _ in range(main.BYOK_REQUEUE_MAX + 3):
        mark_running(jid, "tok-b8")               # так задачу видит _run_task
        deferred.append(main._defer_task_for_busy_key(jid, "tok-b8", note))
    check("ждать можно дольше пяти подходов", deferred, [True] * len(deferred))
    check("бюджет возвратов byok при этом не тронут",
          main._BYOK_REQUEUES.get(str(jid)), None)
    check("а в журнале всё те же цифры: одна строка на ожидание", len(job_log(jid)), 1)
    row = task_row(jid)
    check("и задача жива, просто отложена", row["status"], "queued")
    check("ошибки ей не приписали", row["error"], None)
    # А время всё-таки ограничено: ключ, занятый сутки, — это уже не «сосед работает».
    main._KEY_BUDGET_WAITS[str(jid)] = main._now() - main.VS_KEY_BUDGET_WAIT_MAX_SEC - 1
    mark_running(jid, "tok-b8")
    check("но не дольше отведённого времени",
          main._defer_task_for_busy_key(jid, "tok-b8", note), False)
    main._key_budget_wait_clear(jid)

    print("\nСломанная настройка учёта не роняет оплачиваемую задачу")
    os.environ["VS_KEY_BUDGET_MODE"] = "enforce"
    os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = os.path.join(TMP, "нет-такого/х")
    open(os.path.join(TMP, "нет-такого"), "w").close()      # не каталог — mkdir упадёт
    lease = main._reserve_task_credential(PROVIDER, "prompts", {})
    check("задача идёт как до B8", lease is not None, True)
    free_all([lease])
    os.environ["VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"] = LOCKS

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
