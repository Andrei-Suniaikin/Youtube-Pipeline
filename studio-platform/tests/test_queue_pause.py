#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пауза генерации: адресная, записанная в журнал и видимая словами.

Какой дефект ловится. `admin_state.queue_pause` — единственный тормоз конвейера, и до
этой работы про него нельзя было ответить ни на один человеческий вопрос.

1. **Изменение рубильника не оставляло следа о состоянии.** В `admin_audit` писался
   ЗАПРОС («resume_queue {"target":"all"}»), а не то, что было и что стало. Кто
   поставил паузу 31.08 и что именно она держала, по журналу не восстанавливалось;
   правка мимо панели (14.08, скриптом прямо в `admin_state`) не оставляла вообще
   ничего. 187 выпусков простояли до 18.09.

2. **Пауза снималась вслепую.** Кнопка «Возобновить» отпускала очередь целиком за один
   клик. Августовский залп — ровно это: конвейер ушёл в работу весь сразу, и деньги
   потратились раньше, чем кто-то успел посмотреть на числа.

3. **Кабинет о паузе молчал.** Выпуски стояли «ждём», а что именно остановлено, с
   какого времени и с чьей руки — на экране не было. Хуже того, не было видно самого
   неочевидного правила: пауза ОДНОГО канала текстов (`prompt_*`) останавливает заказ
   новых текстов ВО ВСЕХ профилях (унаследовано из боя, `pub_services`).

Что проверяется здесь: каждая запись рубильника оставляет строку журнала с «было →
стало», правка в обход опознаётся, снятие паузы без `confirm` НИЧЕГО не меняет и
возвращает числа, а состояние читается словами — и на экране кабинета тоже.

Сети нет, стенда нет: поднимается временная база, как в tests/test_render_receipt.py.

    .venv/bin/python tests/test_queue_pause.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="queue-pause-")

os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402
import pub_ui as U                                                # noqa: E402
from starlette.testclient import TestClient                       # noqa: E402

FAILS = []
DAY = 86400.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def admin():
    c = M.db()
    try:
        return c.execute("SELECT * FROM users WHERE role='admin'").fetchone()
    finally:
        c.close()


def tenant_of(u):
    return u["tenant_id"] or str(u["id"])


def reset_state(state=None):
    """Вернуть стенд к чистому рубильнику и пустому журналу."""
    c = M.db()
    try:
        c.execute("DELETE FROM queue_pause_audit")
        if state is None:
            c.execute("DELETE FROM admin_state WHERE k='queue_pause'")
        else:
            c.execute("INSERT INTO admin_state(k,v,updated) VALUES('queue_pause',?,?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
                      (json.dumps(state, ensure_ascii=False), time.time()))
        c.commit()
    finally:
        c.close()
    M._queue_pause_forget_cache()


def seed_queue(u, tid):
    """Две ждущие задачи и один выпуск, остановленный движком из-за паузы."""
    now = time.time()
    c = M.db()
    try:
        c.execute("DELETE FROM queue_tasks")
        c.execute("DELETE FROM pub_releases")
        c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,"
                  "created,uid,provider) VALUES(901,?,?,'prompts','queued',?,?,'prompt_claude')",
                  (tid, "p1", now - 2 * 3600, u["id"]))
        c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,"
                  "created,uid,provider) VALUES(902,?,?,'images','queued',?,?,'fastgen')",
                  (tid, "p1", now - 3600, u["id"]))
        # Так выпуск помечает движок, когда ресурс недоступен: pub_engine.advance
        # пишет blocked_reason = "ждём: <текст ResourceDown>".
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
                  "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
                  "blocked_reason,created,updated) VALUES('r_pause1',?,1,1,1,'UC1',?,"
                  "'p1','pending','created',"
                  "'ждём: генерация текстов и промптов этого профиля приостановлена',?,?)",
                  (tid, now + DAY, now - 19 * DAY, now - 19 * DAY))
        c.commit()
    finally:
        c.close()


def history():
    return M._queue_pause_history(20)


def newest():
    """Верхняя строка журнала или пустышка: сломанный журнал должен давать FAIL,
    а не traceback — иначе непонятно, что именно отвалилось."""
    rows = history()
    return rows[0] if rows else {"actor": "", "origin": "", "action": "",
                                 "summary": "", "holding": "", "when": "", "ago": ""}


# ─────────────────────────────────────────────────────────────────────────────
def test_journal_records_before_and_after():
    print("\nЖурнал рубильника: кто, когда, что было, что стало, с какого экрана")
    u = admin()
    reset_state()
    M._adm_act_pause_queue(u, {"target": "all", "origin": "админ · обслуживание"})
    check("постановка паузы оставила ровно одну строку", len(history()), 1)
    check("в строке записан автор", newest()["actor"], u["name"])
    check("и экран, с которого нажали", newest()["origin"], "админ · обслуживание")
    check("и что изменилось — словами", newest()["summary"], "включена общая пауза")

    c = M.db()
    try:
        raw = c.execute("SELECT state_before, state_after FROM queue_pause_audit "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        c.close()
    check("«было» записано отдельным снимком",
          bool(raw) and json.loads(raw["state_before"])["all"], False)
    check("«стало» — тоже", bool(raw) and json.loads(raw["state_after"])["all"], True)

    # Точечная пауза провайдера поверх глобальной: журнал обязан различать их.
    M._adm_act_pause_queue(u, {"target": "fastgen", "origin": "админ · провайдеры"})
    check("вторая правка — вторая строка", len(history()), 2)
    check("и в ней только то, что изменилось",
          newest()["summary"], "на паузу поставлены провайдеры: fastgen")

    # Переписать то же состояние тем же значением — не изменение.
    state = dict(M._queue_pause_state())
    M._queue_pause_apply(u, "тест", "повтор", state)
    check("перезапись того же состояния названа честно",
          newest()["summary"], "состояние не изменилось")

    # Запись мимо журнала должна быть невозможна, а не «не принято по договорённости»:
    # общая ручка admin_state для рубильника закрыта.
    try:
        M._adm_state_set("queue_pause", {"all": True})
        blocked = False
    except RuntimeError:
        blocked = True
    check("прямая запись рубильника мимо журнала запрещена", blocked, True)


def test_change_outside_the_panel_is_noticed():
    print("\nПравка мимо панели: журнал догоняет её сам, а не делает вид, что всё записано")
    u = admin()
    reset_state()
    M._adm_act_pause_queue(u, {"target": "all", "origin": "админ · обслуживание"})
    # 14.08 ровно так и сделали: скриптом прямо в admin_state, мимо админ-API.
    c = M.db()
    try:
        c.execute("UPDATE admin_state SET v=? WHERE k='queue_pause'",
                  (json.dumps({"all": False, "providers": ["prompt_codex"],
                               "prompt_uids": []}),))
        c.commit()
    finally:
        c.close()
    M._queue_pause_forget_cache()
    last = M._queue_pause_last(force=True)
    check("появилась строка о правке в обход",
          "мимо панели" in (last["origin"] or ""), True)
    check("автор не выдуман", last["actor"], "неизвестно")
    check("и видно, что именно поменялось",
          "на паузу поставлены провайдеры: prompt_codex" in last["summary"], True)
    check("повторное чтение второй такой строки не плодит", len(history()), 2)


def test_dry_run_shows_numbers_and_changes_nothing():
    print("\nСухой прогон: сколько и каких задач стартует немедленно — до, а не после")
    u = admin()
    tid = tenant_of(u)
    reset_state()
    seed_queue(u, tid)
    M._adm_act_pause_queue(u, {"target": "all", "origin": "тест"})
    before = json.dumps(M._queue_pause_state(), sort_keys=True)
    marks = len(history())

    res = M._adm_act_resume_queue(u, {"target": "all"})
    check("снятие без подтверждения вернуло предпросмотр", res.get("preview"), True)
    check("и рубильник не тронут",
          json.dumps(M._queue_pause_state(), sort_keys=True), before)
    check("и в журнал предпросмотр не попал", len(history()), marks)

    text = res["msg"]
    check("названо число задач", "Немедленно стартуют задачи: 2" in text, True)
    check("названы виды работ", "images 1" in text and "prompts 1" in text, True)
    check("названы провайдеры", "Провайдеры:" in text and "fastgen 1" in text, True)
    check("названы профили", "Профили: " + u["name"] in text, True)
    check("названо число выпусков", "Выпусков сдвинется с места: 1" in text, True)
    check("сказано про деньги", "Деньги:" in text, True)

    ok = M._adm_act_resume_queue(u, {"target": "all", "confirm": 1, "origin": "тест"})
    check("с подтверждением пауза снята", ok["msg"], "Очередь возобновлена полностью")
    check("и очередь действительно отпущена", M._queue_paused(), False)
    check("журнал пополнился", len(history()), marks + 1)
    check("и сохранил тот самый прогон, который видел человек",
          "Немедленно стартуют задачи: 2" in newest()["holding"], True)


def test_dry_run_counts_only_what_this_step_releases():
    print("\nСухой прогон считает только то, что отпускает ИМЕННО это снятие")
    u = admin()
    tid = tenant_of(u)
    reset_state()
    seed_queue(u, tid)
    M._adm_act_pause_queue(u, {"target": "all", "origin": "тест"})
    M._adm_act_pause_queue(u, {"target": "fastgen", "origin": "тест"})
    res = M._adm_act_resume_queue(u, {"target": "all",
                                      "preserve_providers": ["fastgen"]})
    text = res["msg"]
    check("задача оставленного провайдера в залп не входит",
          "Немедленно стартуют задачи: 1" in text, True)
    check("и сказано, кто остаётся под паузой",
          "На точечной паузе останутся: fastgen" in text, True)
    check("и сколько задач там остаётся", "Под паузой останется задач: 1" in text, True)

    # Снятие одного провайдера не должно обещать, что поедут выпуски: их держит
    # не он, а пауза текстов.
    reset_state({"all": False, "providers": ["fastgen", "prompt_codex"], "prompt_uids": []})
    only = M._adm_act_resume_queue(u, {"target": "fastgen"})["msg"]
    check("снятие провайдера не обещает движения выпусков",
          "Выпусков сдвинется с места" in only, False)
    check("а честно говорит, что они останутся стоять",
          "Выпуски останутся стоять: 1" in only, True)


def test_money_is_estimated_from_the_call_journal():
    print("\nДеньги: оценка по журналу платных вызовов, а не по выдуманному прайсу")
    u = admin()
    tid = tenant_of(u)
    reset_state()
    seed_queue(u, tid)
    now = time.time()
    c = M.db()
    try:
        for n in range(3):
            pid = f"done{n}"
            c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
                      "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
                      "created,updated) VALUES(?,?,?,?,1,'UC1',?,?,'done','published',?,?)",
                      (f"r_done{n}", tid, 100 + n, 1, now, pid, now, now))
            for unit in range(4):
                c.execute(
                    "INSERT INTO provider_calls(tenant_id,project_id,release_id,kind,unit,"
                    "fingerprint,provider,model,remote_order_id,status,result_path,cost,"
                    "attempts,error,created,updated) VALUES(?,?,?,'images',?,?,'fastgen','m',"
                    "'o','delivered','x',10.0,1,'',?,?)",
                    (tid, pid, f"r_done{n}", str(unit), f"f{n}{unit}", now, now))
        c.commit()
    finally:
        c.close()
    M._queue_pause_forget_cache()
    M._adm_act_pause_queue(u, {"target": "all", "origin": "тест"})
    text = M._adm_act_resume_queue(u, {"target": "all"})["msg"]
    check("медиана считается по завершённым выпускам",
          "медиана — 4 оплаченных вызовов на выпуск" in text, True)
    check("стоимость берётся из журнала, а не из головы", "≈40 кр. за выпуск" in text, True)
    check("и умножается на число выпусков залпа",
          "порядка 4 платных вызовов" in text, True)
    check("и названа оценкой", "Это оценка по истории, а не счёт провайдера." in text, True)


def test_state_reads_as_words():
    print("\nСостояние словами: что остановлено, с какого времени, кем и что держит")
    u = admin()
    tid = tenant_of(u)
    reset_state()
    seed_queue(u, tid)
    M._adm_act_pause_queue(u, {"target": "all", "origin": "админ · полоса паузы"})
    story = M._queue_pause_story(u["id"], tid)
    text = "\n".join(story["lines"])
    check("сказано, что остановлено",
          "Что остановлено: вся очередь студии" in text, True)
    check("сказано, с какого времени", "С какого времени: " in text, True)
    check("сказано, кем", "поставил: " + u["name"] in text, True)
    check("сказано, с какого экрана", "экран: админ · полоса паузы" in text, True)
    check("сказано, что держится прямо сейчас",
          "Держится прямо сейчас: задач в очереди: 2" in text, True)
    check("и сколько выпусков ждут текста",
          "выпусков ждут новый текст: 1" in text, True)
    check("и что идущие задачи не обрываются",
          "Уже идущие задачи пауза не обрывает" in text, True)

    # Самое неочевидное правило рубильника обязано быть на экране словами.
    reset_state({"all": False, "providers": ["prompt_codex"], "prompt_uids": []})
    story = M._queue_pause_story(u["id"], tid)
    text = "\n".join(story["lines"])
    check("правило «один prompt_* останавливает всех» написано прямо",
          "ни в одном профиле" in text, True)
    check("и кабинет знает, что его тексты остановлены",
          story["blocks_my_texts"], True)


def test_cabinet_card():
    print("\nКарточка кабинета: показывается на своей паузе и молчит на чужой")
    u = admin()
    tid = tenant_of(u)
    reset_state()
    seed_queue(u, tid)
    M._adm_act_pause_queue(u, {"target": "all", "origin": "тест"})
    html = U.pause_card(M._queue_pause_story(u["id"], tid))
    check("карточка нарисована", "Конвейер остановлен" in html, True)
    check("и в ней те же слова, что в описании",
          "вся очередь студии" in html, True)
    check("и сказано, что снимать паузу — в админке с прогоном",
          "сухим прогоном" in html, True)
    check("текст экранирован", "<script" not in html, True)

    # Чужая персональная пауза текстов: кабинета не касается — карточки быть не должно.
    reset_state({"all": False, "providers": [], "prompt_uids": [999]})
    other = M._queue_pause_story(u["id"], tid)
    check("чужая пауза текстов кабинет не пугает", U.pause_card(other), "")
    check("но сама пауза при этом считается активной", other["paused"], True)

    reset_state()
    check("без паузы карточки нет", U.pause_card(M._queue_pause_story(u["id"], tid)), "")


def test_screens_serve_the_words():
    print("\nЭкраны: кабинет пишет про паузу словами, админ-панель отдаёт журнал")
    u = admin()
    tid = tenant_of(u)
    reset_state()
    seed_queue(u, tid)
    M._adm_act_pause_queue(u, {"target": "all", "origin": "админ · обслуживание"})
    client = TestClient(M.app, cookies={M.SESSION_COOKIE: M.create_session(u["id"])})
    page = client.get("/channels", follow_redirects=False)
    check("кабинет открылся", page.status_code, 200)
    check("и на самом экране написано, что конвейер остановлен",
          "Конвейер остановлен" in page.text, True)
    check("с именем того, кто поставил", "поставил: " + u["name"] in page.text, True)
    check("и с тем, что держится прямо сейчас",
          "Держится прямо сейчас" in page.text, True)

    state = client.get("/admin/api/state?tabs=audit,pause_history&token=" + u["token"])
    check("админ-панель ответила", state.status_code, 200)
    data = state.json()
    lines = (data.get("pause") or {}).get("lines") or []
    check("и отдала состояние словами",
          any("Что остановлено" in line for line in lines), True)
    check("и журнал рубильника отдельным блоком",
          len(data.get("pause_history") or []), 1)


def test_env_example_documents_previous_wave():
    print("\n.env.example: настройки прошлой волны названы и объяснены")
    env = open(os.path.join(BASE, ".env.example"), encoding="utf-8").read()
    for name in ("VS_SOURCE_RETENTION_IMAGES", "VS_RENDER_INCOMING_MAX_AGE_SEC"):
        check(f"{name} есть в примере", name in env, True)
        # Имя без объяснения бесполезно: рядом должно быть написано, что выбирать.
        block = env.split(name)[0].rsplit("# ───", 1)[-1]
        check(f"{name} снабжён пояснением", len(block.strip()) > 200, True)
    check("у уборки исходников перечислены режимы",
          bool(re.search(r"keep\s+\(умолчание\)", env)), True)
    check("у брошенной доставки объяснено, откуда шесть часов",
          "timeout=1200" in env, True)


def main() -> int:
    try:
        test_journal_records_before_and_after()
        test_change_outside_the_panel_is_noticed()
        test_dry_run_shows_numbers_and_changes_nothing()
        test_dry_run_counts_only_what_this_step_releases()
        test_money_is_estimated_from_the_call_journal()
        test_state_reads_as_words()
        test_cabinet_card()
        test_screens_serve_the_words()
        test_env_example_documents_previous_wave()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
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
