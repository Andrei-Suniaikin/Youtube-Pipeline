#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Забрать у рерайта то, за что уже заплачено, и не заплатить второй раз (19.09.2026).

Дефект из разбора пакета 18.09. Движок выпусков стоит на паузе с 31.08, а рерайт всё
это время доделывал заказанное: в кабинете владельца 146 задач со статусом completed —
текст написан, ОПЛАЧЕН и лежит у соседа, но кабинет его не забрал. Ещё 187 выпусков
ждут в очереди. Пока пауза держит новые траты, штатный тик до этих текстов не дойдёт,
а снять паузу ради них нельзя: вместе с забором готового поехали бы новые заказы.

Проход `pub_services.collect_paid_texts` закрывает ровно эту дыру, и цена ошибки в нём —
вторая оплата. Поэтому проверяется не только «текст забран», но и всё, чего он делать
не должен: не заказывать, не воскрешать пропавшую у соседа задачу новым заказом, не
снимать паузу, не пропускать половинчатый текст мимо приёмки, не ломиться в лежачего
соседа всеми оставшимися сотнями строк.

Сети здесь нет: `RewriteClient` подменён клиентом, который падает на любой попытке
что-нибудь заказать.

    .venv/bin/python tests/test_paid_text_pickup.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls                                                   # noqa: E402
import pub_schema                                                  # noqa: E402
import pub_services                                                # noqa: E402
from pub_engine import ResourceDown                                # noqa: E402
from pub_remote import RemoteJobMissing                            # noqa: E402

NOW = 1_800_000_000.0
FAILS = []

DONOR = "Исходный текст донора. " * 200          # 4600 знаков
GOOD = "П" * 4600                                # полноценный результат
HALF = "П" * int(4600 * 0.46)                    # случай из боя: 46% донора


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────────────── сосед на бумажке ───────────────────────────

class Neighbour:
    """Рерайт, который умеет ровно две вещи: сказать статус и отдать файл.

    Любой заказ — это провал теста, а не «ну вдруг». Проход не имеет права ни
    загружать донора, ни создавать задачу, ни даже спрашивать список моделей:
    всё это либо стоит денег, либо ведёт к ним.
    """

    status = {}          # job_id → что отвечает сосед на GET /jobs/<id>
    text = {}            # job_id → что лежит в файле результата
    asked = []           # порядок обращений: видно, что лишнего не спрашивали
    down_after = None    # с какого по счёту вопроса сосед «лежит»

    @classmethod
    def reset(cls):
        cls.status = {}
        cls.text = {}
        cls.asked = []
        cls.down_after = None

    def __init__(self, *_a, **_k):
        pass

    # ── то, чего проход делать не должен ──

    def resolve_model(self, preset):
        raise AssertionError("проход спросил модель — он ничего не заказывает")

    def upload_text(self, text, filename=""):
        raise AssertionError("проход загрузил донора соседу — это начало заказа")

    def create_job(self, *a, **k):
        raise AssertionError("проход создал задачу — это вторая оплата")

    # ── то, что ему разрешено ──

    def job(self, job_id):
        Neighbour.asked.append(f"job:{job_id}")
        n = len([x for x in Neighbour.asked if x.startswith("job:")])
        if Neighbour.down_after is not None and n > Neighbour.down_after:
            raise ResourceDown("рерайт", "рерайт не отвечает: ConnectionError")
        got = Neighbour.status.get(job_id)
        if got is None:
            raise RemoteJobMissing(
                "рерайт: сохранённая задача отсутствует — она удалена у соседа или "
                "заказана с другого стенда. Автоматическое создание дубля остановлено")
        return got

    def result_code(self, info, lang):
        return lang

    def result_text(self, job_id, code):
        Neighbour.asked.append(f"text:{job_id}")
        return Neighbour.text.get(job_id, GOOD)


# ─────────────────────────── кабинет на бумажке ───────────────────────────

def fresh():
    """Настоящая схема кабинета: иначе тест не заметит забытую колонку."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    pub_calls.ensure_calls_schema(c)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, tenant_id TEXT,
        name TEXT, key_opts TEXT, keys TEXT);
    """)
    c.execute("INSERT INTO admin_state VALUES('pub_key_rewrite','ключ',0)")
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,lang,tz,created,updated) "
              "VALUES(1,'dev','UC0000000000000000000001','Демо','pl','Europe/Warsaw',?,?)",
              (NOW, NOW))
    c.commit()
    return c


def add_release(c, rid: str, *, job_id: str = "", job_status: str = "running",
                donor_chars: int = len(DONOR), phase: str = "created",
                status: str = "pending", call_status: str = "ordered",
                tenant: str = "dev", text_done: bool = False):
    """Выпуск, дошедший до заказа рерайта: строка плана, выпуск, задача, оплата."""
    n = c.execute("SELECT COUNT(*) FROM pub_plan_items").fetchone()[0] + 1
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_title,slot_at,status,created,updated) "
              "VALUES(?,?,1,1,?,?,?,'running',?,?)",
              (n, tenant, f"https://youtu.be/don{n}", f"Донор {n}", NOW + n, NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
              "created,updated) VALUES(?,?,?,1,1,'UC0000000000000000000001',?,?,?,?,?,?)",
              (rid, tenant, n, NOW + n, f"proj-{rid}", status, phase, NOW, NOW))
    if job_id:
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,"
                  "status,payload,created,updated) VALUES(?,'rewrite','job',?,?,?,?,?)",
                  (rid, job_id, job_status,
                   json.dumps({"donor_chars": donor_chars}) if donor_chars else None,
                   NOW, NOW))
    if text_done:
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,status,payload,"
                  "created,updated) VALUES(?,'rewrite','text','done',?,?,?)",
                  (rid, json.dumps({"text": GOOD}), NOW, NOW))
    if call_status:
        c.execute("INSERT INTO provider_calls(tenant_id,project_id,release_id,kind,unit,"
                  "fingerprint,provider,model,remote_order_id,status,created,updated) "
                  "VALUES(?,?,?,'text','pl',?,'rewrite','terra',?,?,?,?)",
                  (tenant, f"proj-{rid}", rid, f"fp-{rid}", job_id or "", call_status,
                   NOW, NOW))
    c.commit()


def release(c, rid):
    return c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone()


def job_row(c, rid):
    """Строка о задаче соседа. Нет строки — пустой словарь, а не падение: проверка
    должна печатать FAIL с понятной разницей, а не трассировку."""
    row = c.execute("SELECT * FROM pub_release_jobs WHERE release_id=? AND "
                    "service='rewrite' AND kind='job'", (rid,)).fetchone()
    return dict(row) if row else {}


def stored_text(c, rid):
    row = c.execute("SELECT payload FROM pub_release_jobs WHERE release_id=? AND "
                    "service='rewrite' AND kind='text'", (rid,)).fetchone()
    return json.loads(row["payload"])["text"] if row else ""


def call_row(c, rid):
    return c.execute("SELECT * FROM provider_calls WHERE release_id=?", (rid,)).fetchone()


def messages(c, rid):
    return [r[0] for r in c.execute("SELECT message FROM pub_release_log "
                                    "WHERE release_id=?", (rid,))]


def phases(c, rid):
    return [r[0] for r in c.execute("SELECT phase FROM pub_release_events "
                                    "WHERE release_id=? ORDER BY seq", (rid,))]


def collect(c, **kw):
    kw.setdefault("now", NOW + 1000)
    return pub_services.collect_paid_texts(c, **kw)


# ─────────────────────────── проверки ───────────────────────────

def part_take():
    print("Оплаченный текст забирается, а фаза двигается")

    c = fresh()
    add_release(c, "r1", job_id="j-1")
    Neighbour.reset()
    Neighbour.status["j-1"] = {"status": "completed", "output_files": {"pl": "f"}}
    Neighbour.text["j-1"] = GOOD

    out = collect(c)
    check("подходящий выпуск найден", out["candidates"], 1)
    check("текст забран", out["taken"], 1)
    check("пропущенных нет", out["skipped"], 0)
    check("итог по строке", out["items"][0]["result"], "taken")
    check("текст лёг в кабинет", stored_text(c, "r1"), GOOD)
    check("фаза двинулась", release(c, "r1")["phase"], "source_ready")
    check("и это видно в журнале фаз", phases(c, "r1"), ["source_ready"])
    check("выпуск снова в работе", release(c, "r1")["status"], "running")
    check("срок фазы выставлен", (release(c, "r1")["deadline"] or 0) > NOW + 1000,
          True)
    check("задача закрыта как выполненная", job_row(c, "r1").get("status"), "done")
    check("оплата закрыта как полученная", call_row(c, "r1")["status"], "delivered")
    check("в оплате записан номер задачи", call_row(c, "r1")["result_path"], "j-1")
    check("у соседа спросили только статус и файл",
          Neighbour.asked, ["job:j-1", "text:j-1"])
    said = [m for m in messages(c, "r1") if "оплачено, но не забрано" in m]
    check("в журнале сказано, откуда текст", len(said), 1)
    check("и что нового заказа не было", "нового заказа не создавал" in said[0], True)
    check("доля донора посчитана", "100.0% донора" in said[0], True)

    print("\n  движок подхватывает забранный текст следующим шагом, а не заново")

    # Ради этого всё и делалось: после прохода выпуск должен выглядеть для движка
    # обычным, дошедшим до текста, — и следующий тик обязан пойти в СЛЕДУЮЩУЮ стадию,
    # а не переспрашивать рерайт.
    import pub_engine

    class Stub:
        called = []

        def do_project_ready(self, c, rel):
            Stub.called.append("do_project_ready")
            return {}

        def do_source_ready(self, c, rel):
            Stub.called.append("do_source_ready")     # сюда попадать нельзя
            return {}

        def release_resources(self, c, rel):
            return None

    rel = [x for x in pub_engine.live_releases(c) if x["release_id"] == "r1"][0]
    step = pub_engine.advance(c, rel, Stub(), NOW + 2000)
    check("движок пошёл дальше, а не за текстом", Stub.called, ["do_project_ready"])
    check("и выпуск встал на следующую фазу", step, "project_ready")

    print("\n  повторный проход не находит ничего и второй раз никуда не ходит")
    Neighbour.asked = []
    again = collect(c)
    check("подходящих больше нет", again["candidates"], 0)
    check("к соседу не ходили", Neighbour.asked, [])


def part_not_ready():
    print("\nНезаконченная задача остаётся ждать, а не считается забранной")

    c = fresh()
    add_release(c, "r2", job_id="j-2")
    Neighbour.reset()
    Neighbour.status["j-2"] = {"status": "processing"}

    out = collect(c)
    check("текст не забран", out["taken"], 0)
    check("строка помечена ожиданием", out["items"][0]["result"], "waiting")
    check("фаза на месте", release(c, "r2")["phase"], "created")
    check("задача по-прежнему в работе", job_row(c, "r2").get("status"), "running")
    check("файл не качали", [x for x in Neighbour.asked if x.startswith("text:")], [])
    check("и на следующем проходе она снова кандидат",
          collect(c)["candidates"], 1)


def part_missing():
    print("\nПропавшая у соседа задача не превращается в новый заказ")

    c = fresh()
    add_release(c, "r3", job_id="j-3")
    Neighbour.reset()          # статуса для j-3 нет — сосед ответит 404

    out = collect(c)
    check("текст не забран", out["taken"], 0)
    check("строка помечена пропажей", out["items"][0]["result"], "missing")
    check("задача помечена missing — повтор не закажет дубль",
          job_row(c, "r3").get("status"), "missing")
    # Строка оплаты остаётся `ordered`: заказ был создан, деньги потрачены, и следующая
    # попытка обязана ПРОВЕРЯТЬ существующий заказ, а не создавать второй.
    check("деньги считаются потраченными", call_row(c, "r3")["status"], "ordered")
    check("и причина записана в строку оплаты", call_row(c, "r3")["error"] != "", True)
    check("фаза не двинута", release(c, "r3")["phase"], "created")
    check("выпуск не уведён в разбор проходом — это решение движка и человека",
          release(c, "r3")["status"], "pending")
    check("в журнале выпуска есть причина",
          any("отсутствует" in m for m in messages(c, "r3")), True)

    print("\n  и второй проход её больше не дёргает")
    Neighbour.asked = []
    out2 = collect(c)
    check("в кандидаты не попала", out2["candidates"], 0)
    check("к соседу не ходили", Neighbour.asked, [])


def part_length():
    print("\nПоловинчатый текст не проходит приёмку и здесь")

    c = fresh()
    add_release(c, "r4", job_id="j-4")
    Neighbour.reset()
    Neighbour.status["j-4"] = {"status": "done", "output_files": {"pl": "f"}}
    Neighbour.text["j-4"] = HALF

    out = collect(c)
    check("текст не принят", out["taken"], 0)
    check("строка помечена отказом приёмки", out["items"][0]["result"], "rejected")
    check("текст НЕ сохранён — озвучка его не увидит", stored_text(c, "r4"), "")
    check("фаза не двинута", release(c, "r4")["phase"], "created")
    check("задача помечена неудачной", job_row(c, "r4").get("status"), "error")
    check("в журнале обе длины",
          any("короче донора" in m and "46.0%" in m for m in messages(c, "r4")), True)

    print("\n  а без длины донора доля не выдумывается")
    c2 = fresh()
    add_release(c2, "r5", job_id="j-5", donor_chars=0)
    Neighbour.reset()
    Neighbour.status["j-5"] = {"status": "done", "output_files": {"pl": "f"}}
    Neighbour.text["j-5"] = HALF
    out2 = collect(c2)
    check("текст принят как раньше", out2["taken"], 1)
    check("и доля в журнале не названа",
          any("% донора" in m for m in messages(c2, "r5")), False)


def part_stop_on_down():
    print("\nЛежачий сосед останавливает проход, а не перебирает остаток")

    c = fresh()
    for i in (1, 2, 3):
        add_release(c, f"d{i}", job_id=f"jd-{i}")
    Neighbour.reset()
    for i in (1, 2, 3):
        Neighbour.status[f"jd-{i}"] = {"status": "done", "output_files": {"pl": "f"}}
    Neighbour.down_after = 1          # первый ответил, дальше сосед лёг

    out = collect(c)
    check("первый текст забран", out["taken"], 1)
    check("проход остановлен", out["stopped"] != "", True)
    check("причина названа словами", "не отвечает" in out["stopped"], True)
    check("спросили ровно двоих, третьего не трогали",
          len([x for x in Neighbour.asked if x.startswith("job:")]), 2)
    check("и в ответе видно, сколько осталось", out["left"], 2)
    check("забранный текст остался забранным", stored_text(c, "d1"), GOOD)
    check("непройденные выпуски не тронуты", release(c, "d3")["phase"], "created")


def part_pause():
    print("\nПауза не мешает забрать оплаченное и не снимается проходом")

    c = fresh()
    c.execute("INSERT INTO admin_state VALUES('queue_pause',?,0)",
              (json.dumps({"all": True, "providers": ["prompt_claude"]}),))
    c.commit()
    add_release(c, "p1", job_id="jp-1")
    Neighbour.reset()
    Neighbour.status["jp-1"] = {"status": "completed", "output_files": {"pl": "f"}}

    out = collect(c)
    check("оплаченный текст забран и на паузе", out["taken"], 1)
    state = json.loads(c.execute("SELECT v FROM admin_state WHERE k='queue_pause'")
                       .fetchone()[0])
    check("пауза осталась как была", state, {"all": True, "providers": ["prompt_claude"]})


def part_scope():
    print("\nПроход берёт только то, за что заплачено и что ещё не забрано")

    c = fresh()
    add_release(c, "s1", job_id="")                       # номера задачи нет вообще
    add_release(c, "s2", job_id="j-s2", job_status="done")     # текст уже забирали
    add_release(c, "s3", job_id="j-s3", job_status="missing")  # уже помечена пропавшей
    add_release(c, "s4", job_id="j-s4", phase="project_ready")  # ушёл дальше
    add_release(c, "s5", job_id="j-s5", status="blocked")      # в разборе
    add_release(c, "s6", job_id="j-s6", text_done=True)        # текст уже лежит
    Neighbour.reset()

    picked = [x["release_id"] for x in pub_services.paid_text_candidates(c)]
    check("ни один из них не кандидат", picked, [])
    check("выпуск в разборе берётся только осознанно",
          [x["release_id"] for x in pub_services.paid_text_candidates(
              c, include_blocked=True)], ["s5"])

    add_release(c, "s7", job_id="j-s7", tenant="other")        # чужой кабинет
    add_release(c, "s8", job_id="j-s8")
    check("а обычные ждущие выпуски — кандидаты",
          [x["release_id"] for x in pub_services.paid_text_candidates(c)], ["s7", "s8"])
    check("отбор по кабинету работает",
          [x["release_id"] for x in pub_services.paid_text_candidates(
              c, tenant_id="dev")], ["s8"])
    check("и у соседнего кабинета своё",
          [x["release_id"] for x in pub_services.paid_text_candidates(
              c, tenant_id="other")], ["s7"])
    check("и по списку выпусков тоже",
          [x["release_id"] for x in pub_services.paid_text_candidates(
              c, release_ids=["s1", "s8"])], ["s8"])


def part_from_calls():
    print("\nНомер задачи из журнала оплат: своя строка потерялась, деньги записаны")

    c = fresh()
    add_release(c, "k1", job_id="")        # нашей строки о задаче нет
    c.execute("UPDATE provider_calls SET remote_order_id='j-k1' WHERE release_id='k1'")
    c.commit()
    Neighbour.reset()
    Neighbour.status["j-k1"] = {"status": "completed", "output_files": {"pl": "f"}}

    found = pub_services.paid_text_candidates(c)
    check("выпуск найден по журналу оплат", [x["release_id"] for x in found], ["k1"])
    check("и видно, что номер взят оттуда", found[0]["src"], "calls")

    out = collect(c)
    check("текст забран", out["taken"], 1)
    check("своя строка о задаче восстановлена", job_row(c, "k1").get("remote_job_id"), "j-k1")
    check("и закрыта как выполненная", job_row(c, "k1").get("status"), "done")
    check("в журнале сказано, что нового заказа не создавали",
          any("нового заказа не создаю" in m for m in messages(c, "k1")), True)
    check("донора не качали заново — его тут неоткуда взять",
          Neighbour.asked, ["job:j-k1", "text:j-k1"])

    print("\n  и даже если задача ещё не готова, своя строка о ней остаётся")
    add_release(c, "k2", job_id="")
    c.execute("UPDATE provider_calls SET remote_order_id='j-k2' WHERE release_id='k2'")
    c.commit()
    Neighbour.status["j-k2"] = {"status": "processing"}
    collect(c)
    check("строка записана", job_row(c, "k2").get("remote_job_id"), "j-k2")
    check("и она в работе, а не закрыта", job_row(c, "k2").get("status"), "running")
    found2 = pub_services.paid_text_candidates(c)
    check("следующий проход ищет её коротким путём",
          [(x["release_id"], x["src"]) for x in found2], [("k2", "job")])


def part_batch():
    print("\nПроход ограничен пачкой: кнопка пульта не висит на 146 задачах")

    c = fresh()
    for i in (1, 2, 3):
        add_release(c, f"b{i}", job_id=f"jb-{i}")
        Neighbour.status[f"jb-{i}"] = {"status": "done", "output_files": {"pl": "f"}}
    Neighbour.reset()
    for i in (1, 2, 3):
        Neighbour.status[f"jb-{i}"] = {"status": "done", "output_files": {"pl": "f"}}

    out = collect(c, limit=2)
    check("взято ровно два", out["taken"], 2)
    check("и сказано, сколько осталось", out["left"], 1)
    check("кандидатов было три", out["candidates"], 3)
    out2 = collect(c, limit=0)
    check("нулевой предел добирает остаток", out2["taken"], 1)
    check("и остатка больше нет", out2["left"], 0)


def part_no_key():
    print("\nБез ключа доступа проход честно говорит, что не работал")

    c = fresh()
    c.execute("DELETE FROM admin_state WHERE k='pub_key_rewrite'")
    c.commit()
    add_release(c, "n1", job_id="j-n1")
    Neighbour.reset()
    out = collect(c)
    check("проход остановлен", "ключ" in out["stopped"], True)
    check("ничего не забрано", out["taken"], 0)
    check("к соседу не ходили", Neighbour.asked, [])


def part_guards():
    print("\nЗаглушки обработчика: через проход стадию не позовёшь")

    svc = pub_services._text_only_services()
    for name in ("create_project", "enqueue", "project_dir"):
        try:
            getattr(svc, name)()
            check(f"{name} падает громко", "не упал", "RuntimeError")
        except RuntimeError:
            check(f"{name} падает громко", "RuntimeError", "RuntimeError")


def part_screen():
    print("\nЭкран «оплачено, но не забрано» открывается без сети")

    c = fresh()
    add_release(c, "w1", job_id="j-w1")
    rows = pub_services.paid_texts_waiting(c, now=NOW + 3600)
    check("строка одна", len(rows), 1)
    check("есть что показать человеку",
          [rows[0]["ch_name"], rows[0]["donor_title"], rows[0]["job_id"]],
          ["Демо", "Донор 1", "j-w1"])
    check("и сколько ждём", rows[0]["waiting_sec"], 3600.0)

    # Экран могут открыть и с соединения без row_factory (консоль, чужой процесс).
    # Падать на этом нельзя: список ждущих денег — не место для сюрпризов.
    plain = sqlite3.connect(":memory:")
    plain.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(plain)
    pub_calls.ensure_calls_schema(plain)
    plain.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,lang,tz,created,updated)"
                  " VALUES(1,'dev','UC0000000000000000000002','Демо','pl','UTC',?,?)",
                  (NOW, NOW))
    plain.commit()
    add_release(plain, "w2", job_id="j-w2")
    plain.row_factory = None
    check("без row_factory список тоже читается",
          [x["release_id"] for x in pub_services.paid_text_candidates(plain)], ["w2"])


def main() -> int:
    saved = pub_services.RewriteClient
    pub_services.RewriteClient = Neighbour
    try:
        part_take()
        part_not_ready()
        part_missing()
        part_length()
        part_stop_on_down()
        part_pause()
        part_scope()
        part_from_calls()
        part_batch()
        part_no_key()
        part_guards()
        part_screen()
    finally:
        pub_services.RewriteClient = saved

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО {len(FAILS)}: " + "; ".join(FAILS))
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
