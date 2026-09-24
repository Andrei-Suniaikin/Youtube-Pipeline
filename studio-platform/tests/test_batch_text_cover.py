#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пакетный текст и обложка для отмеченных проектов (`web/app.py`).

Дефект, ради которого это писалось: на экране «Проекты» пачкой ставились только
задачи кабинета (озвучка, картинки, видео, рендер), а текст и обложку приходилось
заказывать по одному. Пакет для них — это платные вызовы сразу на десяти проектах,
то есть самый дорогой способ ошибиться, и проверяется здесь именно это:

  * источник берётся ИЗ ПРОЕКТА — донорская ссылка выпуска, иначе сценарий проекта;
  * у проекта без источника — честный пропуск с причиной, а не чужой источник;
  * повторное нажатие не ставит вторую задачу и не платит второй раз;
  * ОТПЕЧАТОК ПЛАТНОГО ВЫЗОВА У КНОПКИ И У НОЧНОГО ДВИЖКА ОДИН И ТОТ ЖЕ: текст,
    оплаченный ночью движком, утром кнопкой второй раз не оплачивается;
  * бронь, выданная на уже заказанный язык, — это вторая оплата, и её не выдают
    даже тогда, когда предварительный разбор успел решить иначе;
  * строки журнала кто-то закрывает: задача, которую сосед доделал, перестаёт
    держать проект «занятым», а её строка становится оплаченной и полученной;
  * пустой пресет обложек — отказ, а не тихий заказ на умолчаниях соседа;
  * тело заказа обложки проверяется целиком, а не по одному-двум полям;
  * замена источника из карточки уходит ровно туда, куда должна (ссылка — всем,
    вставленный текст — только одному отмеченному проекту), и не спорит молча с
    выбором «что считать источником»;
  * отказ соседа ДО заказа открывает строку журнала заново, а неоднозначный обрыв —
    нет: повтор после обрыва означал бы вторую оплату;
  * беда одного проекта (включая HTTPException) не уносит отчёт по всей пачке.

Соседей и сети здесь нет: `_rw`, `_cover`, `donor_subs` и `rewrite_preset_config`
подменены. База — своя копия `dev/vsgen.dev.db` во временном каталоге, после себя тест
её убирает.

    .venv/bin/python tests/test_batch_text_cover.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
SEED = os.path.join(BASE, "dev", "vsgen.dev.db")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Req:
    """Запрос ровно в том объёме, в каком его читают ручки: кука вошедшего."""

    class state:
        sid = "test-sid"


class Fake:
    """Соседи, записанные на бумажке: что у них просили и что они ответили."""

    def __init__(self):
        self.models = []        # ответ /models: default-модель резолвера (пункт 4 ТЗ)
        self.rw = []            # (путь, тело/файл); путь «/jobs/rewrite» — это ЗАКАЗ
        self.cover = []         # (путь, тело)
        self.subs = {}          # ссылка → расшифровка
        self.rw_fail = None     # (код, текст) на следующий /jobs/rewrite
        self.cover_boom = False  # оборвать следующий заказ обложки
        self.job = 0
        # Чем сосед отвечает на ВОПРОС о задаче (`/jobs/{id}`). Неизвестная задача —
        # «ещё делается»: именно так и выглядит только что поставленная.
        self.rw_jobs = {}
        self.cover_jobs = {}
        self.presets = {}       # имя пресета рерайта → его настройки

    def _rw(self, path, sid, payload=None, method=None, raw=None, ctype=None, extra=None):
        if path == "/models":
            # Список моделей — читает только `_default_text_model` (пункт 4 ТЗ
            # 22.09.2026), когда ни в карточке, ни в пресете модели нет. Пустой список
            # (умолчание `self.models`) ⇒ резолвер сам откатится на TEXT_MODEL_FALLBACK —
            # поведение этого файла по умолчанию не меняется. Отдельный сценарий ниже
            # («модель по умолчанию рерайта») подкладывает сюда флаг `default`, чтобы
            # проверить, что четвёртое место и правда СПРАШИВАЕТ /models, а не просто
            # структурно упоминает резолвер в исходнике. Отдельная ветка ДО общей: иначе
            # список считался бы заказом текста и сдвигал бы «job_id» всех заказов после
            # себя.
            self.rw.append((path, None))
            return 200, self.models
        if path == "/upload":
            body = raw.decode("utf-8", "replace")
            text = body.split("\r\n\r\n", 1)[1].rsplit("\r\n--", 1)[0]
            name = body.split('filename="', 1)[1].split('"', 1)[0]
            self.rw.append(("/upload", {"name": name, "text": text}))
            return 200, {"filename": name}
        if path.startswith("/jobs/") and payload is None:
            # Вопрос о судьбе задачи. Он бесплатен, и отличать его от заказа
            # обязательно: половина проверок ниже про то, что заказа НЕ БЫЛО.
            jid = path.split("/jobs/", 1)[1]
            self.rw.append((path, None))
            info = self.rw_jobs.get(jid)
            if info == 404:
                return 404, "нет такой задачи"
            return 200, info or {"id": jid, "status": "running"}
        self.rw.append((path, {"payload": payload, "key": (extra or {}).get("Idempotency-Key", "")}))
        if self.rw_fail is not None:
            code, text = self.rw_fail
            self.rw_fail = None
            return code, text
        self.job += 1
        return 200, {"job_id": f"rw-{self.job}"}

    def _cover(self, path, sid, payload=None, method=None):
        if path.startswith("/jobs/") and payload is None:
            jid = path.split("/jobs/", 1)[1]
            self.cover.append((path, None))
            info = self.cover_jobs.get(jid)
            if info == 404:
                return 404, "нет такой задачи"
            return 200, info or {"id": jid, "status": "running"}
        self.cover.append((path, payload))
        if self.cover_boom:
            self.cover_boom = False
            raise RuntimeError("сосед оборвал соединение")
        self.job += 1
        return 200, {"job_id": f"cv-{self.job}"}

    def donor_subs(self, url, net=True, strict=True, deadline=None, max_exits=None):
        if url not in self.subs:
            if not net:
                return {}
            raise RuntimeError(f"у ролика нет субтитров: {url}")
        return {"text": self.subs[url], "chars": len(self.subs[url]), "cached": True}

    def preset_config(self, user_id, name):
        """Пресет рерайта глазами соседа — то же, что читает движок."""
        return dict(self.presets.get(name) or {})


def rw_orders(fake):
    """Только ЗАКАЗЫ текста. Вопросы о статусе сюда не попадают: они бесплатны."""
    return [r for r in fake.rw if r[0] == "/jobs/rewrite"]


def rw_asks(fake):
    return [r[0] for r in fake.rw if r[1] is None]


def cover_orders(fake):
    return [r for r in fake.cover if r[0] == "/jobs/cover"]


def make_project(c, pid, name, *, channel=None, donor="", scenario="", tenants="",
                 tenant="dev"):
    """Проект такой, каким его заводит кабинет: строка в базе и папка с `source.txt`."""
    now = time.time()
    c.execute("INSERT INTO projects(id,tenant_id,name,dir_path,status,created,updated) "
              "VALUES(?,?,?,?,?,?,?)", (pid, tenant, name, "", "new", now, now))
    if scenario:
        pdir = os.path.join(tenants, tenant, "projects", pid)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "source.txt"), "w", encoding="utf-8") as f:
            f.write(scenario)
    if channel is not None:
        cur = c.execute(
            "INSERT INTO pub_plan_items(tenant_id,channel_id,version,donor_url,position,"
            "slot_at,overrides,status,created,updated) VALUES(?,?,1,?,0,NULL,'{}','planned',?,?)",
            (tenant, channel, donor, now, now))
        item = cur.lastrowid
        c.execute(
            "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
            "channel_id,uc,slot_at,project_id,status,phase,created,updated) "
            "VALUES(?,?,?,1,?,?,?,?,'pending','created',?,?)",
            (f"rel-{pid}", tenant, item, channel, f"uc-{pid}", now, pid, now, now))
    c.commit()


def make_channel(c, cid, name, lang, preset_cover="", preset_rewrite="", tenant="dev"):
    now = time.time()
    c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,handle,lang,niche,tz,"
              "schedule,horizon_days,preset_rewrite,preset_cover,status,created,updated) "
              "VALUES(?,?,?,?,'',?,'','Europe/Warsaw','{}',3,?,?,'active',?,?)",
              (cid, tenant, f"uc-ch-{cid}", name, lang, preset_rewrite, preset_cover,
               now, now))
    c.commit()


def calls(c, pid, kind):
    return [dict(r) for r in c.execute(
        "SELECT unit, status, remote_order_id FROM provider_calls "
        "WHERE project_id=? AND kind=? ORDER BY id", (pid, kind))]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    if not os.path.exists(SEED):
        print(f"НЕ ПРОВЕРЕНО: нет базы-образца {SEED}")
        return 1
    work = tempfile.mkdtemp(prefix="batch-text-cover-")
    db_path = os.path.join(work, "vsgen.db")
    shutil.copy(SEED, db_path)
    tenants = os.path.join(work, "tenants")
    os.environ["VS_DEV_DB"] = db_path
    os.environ["VS_TENANTS_DIR"] = tenants
    os.environ["VS_DEV_TENANT"] = "dev"
    sys.path.insert(0, BASE)
    sys.path.insert(0, os.path.join(BASE, "server"))
    import web.app as pult                                         # noqa: E402
    import pub_calls                                               # noqa: E402

    fake = Fake()
    pult._rw = fake._rw
    pult._cover = fake._cover
    pult.donor_subs = fake.donor_subs
    pult.rewrite_preset_config = fake.preset_config
    DONOR = "https://youtu.be/AAAAAAAAAAA"
    OTHER = "https://youtu.be/BBBBBBBBBBB"
    NIGHT = "https://youtu.be/CCCCCCCCCCC"
    fake.subs[DONOR] = "расшифровка донора " * 20
    fake.subs[OTHER] = "расшифровка замены " * 20
    fake.subs[NIGHT] = "расшифровка ночного " * 20
    fake.presets["Ночной пресет"] = {"model": "gpt-5"}

    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        make_channel(c, 901, "Канал ру", "ru", preset_cover="Базовая обложка")
        make_channel(c, 902, "Канал cz", "cz")
        # Канал с пресетами — на нём проверяется отпечаток: движок берёт имя пресета
        # рерайта и модель ИЗ ПРЕСЕТА, и кнопка обязана брать то же самое.
        make_channel(c, 903, "Канал с пресетом", "ru", preset_rewrite="Ночной пресет")
        # Проекты одного канала и с разным источником: донор, свой сценарий, пусто.
        # Канал нужен каждому — из него берётся язык, когда в карточке его не выбрали.
        make_project(c, "t-donor", "С донором", channel=901, donor=DONOR, tenants=tenants)
        make_project(c, "t-text", "Со сценарием", channel=901,
                     scenario="свой сценарий проекта", tenants=tenants)
        make_project(c, "t-empty", "Пустой", channel=901, tenants=tenants)
        make_project(c, "t-cz", "Чужой язык", channel=902, donor=OTHER, tenants=tenants)
        # Спорный случай, ради которого и заведена настройка `source_from`: у проекта
        # есть И донор, И свой сценарий. Умолчание — донор (так же выбирает ночной
        # движок), а явное «text» берёт сценарий.
        make_project(c, "t-both", "И то и другое", channel=901, donor=DONOR,
                     scenario="сценарий поверх донора", tenants=tenants)
        make_project(c, "t-repl-1", "Замена 1", channel=901, tenants=tenants)
        make_project(c, "t-repl-2", "Замена 2", channel=901, tenants=tenants)

        # ── 1. Сухой прогон: ничего не заказывает и денег не тратит ──────────
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-donor", "t-text", "t-empty"], dry_run=True), Req())
        by = {i["id"]: i for i in out["projects"]}
        check("сухой прогон: уйдёт в работу два проекта", out["planned"], 2)
        check("сухой прогон: ничего не поставлено", out["ordered"], 0)
        check("сухой прогон: соседа не трогали", fake.rw, [])
        check("источник проекта с выпуском — ссылка донора", by["t-donor"]["source"], "donor")
        check("источник проекта без выпуска — его сценарий", by["t-text"]["source"], "text")
        check("проект без источника пропущен", by["t-empty"]["action"], "skip")
        check("и причина названа словами",
              "нет источника" in by["t-empty"]["why"], True)
        check("язык взят из канала проекта", by["t-donor"]["languages"], ["ru"])
        check("журнал пуст: сухой прогон не бронирует", calls(c, "t-donor", "text"), [])

        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-both"], dry_run=True), Req())
        check("есть и донор, и сценарий — по умолчанию берём донора",
              out["projects"][0]["source"], "donor")
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-both"], dry_run=True, source_from="text"), Req())
        check("явный выбор «text» берёт сценарий проекта",
              out["projects"][0]["source"], "text")

        # ── 2. Настоящая постановка ──────────────────────────────────────────
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-donor", "t-text", "t-empty"]), Req())
        by = {i["id"]: i for i in out["projects"]}
        check("поставлено два проекта", out["ordered"], 2)
        check("проект с донором получил задачу", by["t-donor"]["job_id"], "rw-1")
        uploads = {u[1]["name"].split("-")[1]: u[1]["text"]
                   for u in fake.rw if u[0] == "/upload"}
        check("донору уехала расшифровка ЕГО ссылки",
              fake.subs[DONOR] in "".join(t for t in uploads.values()), True)
        check("проекту со сценарием уехал ЕГО сценарий",
              any("свой сценарий проекта" == t for t in uploads.values()), True)
        check("в журнале платных вызовов строка на язык",
              calls(c, "t-donor", "text"),
              [{"unit": "ru", "status": "ordered", "remote_order_id": "rw-1"}])
        linked = c.execute("SELECT job_id FROM project_service_jobs WHERE project_id=? "
                           "AND service='rewrite'", ("t-donor",)).fetchall()
        check("задача привязана к проекту", [r["job_id"] for r in linked], ["rw-1"])
        check("ключ повтора соседу отправлен",
              all(bool(r[1].get("key")) for r in rw_orders(fake)), True)
        check("модели нет нигде — уехало умолчание экрана текста",
              rw_orders(fake)[0][1]["payload"]["model"], pult.TEXT_MODEL_FALLBACK)

        # ── 2б. Модель по умолчанию рерайта — четвёртое место (_batch_text_plan) ──
        # Отдельно от проверки выше («нет нигде — литерал»): там у /models пустой
        # ответ, и что с резолвером, что без него итог один — TEXT_MODEL_FALLBACK, а
        # значит она НЕ отличила бы резолвер от захардкоженного литерала поведенчески
        # (только структурная проверка в test_prompt_channel.py это ловит). Здесь у
        # /models ЕСТЬ модель с флагом `default`, и заказ обязан унести именно её —
        # это единственная поведенческая проверка того, что сайт 4 и правда СПРАШИВАЕТ
        # /models на настоящем прогоне, а не просто содержит вызов в исходнике.
        # Мутация: в `_batch_text_plan` заменить `_default_text_model(sid) if net else
        # TEXT_MODEL_FALLBACK` на просто `TEXT_MODEL_FALLBACK` — эта проверка ловит.
        make_project(c, "t-model-default", "Модель по умолчанию рерайта", channel=901,
                     donor=DONOR, tenants=tenants)
        fake.models = [{"value": "codex:gpt-5.7-glow", "label": "glow", "default": True}]
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-model-default"]), Req())
        check("модель по умолчанию рерайта — заказ поставлен",
              out["projects"][0]["action"], "ordered")
        check("уехала модель, помеченная /models как default, а не литерал",
              rw_orders(fake)[-1][1]["payload"]["model"], "codex:gpt-5.7-glow")

        # ── 2в. Резолвер модели по умолчанию — один поход на всю пачку, не на проект
        #        (ревью 22.09.2026, Issue 6) ──────────────────────────────────────
        # До правки `_batch_text_plan` спрашивал `_default_text_model(sid)` (⇒ `/models`
        # соседа) на КАЖДОМ проекте без своей модели в пресете — до `BATCH_PULT_MAX`
        # раз за один запрос кнопки. Два проекта без пресета в одном запросе — минимальный
        # случай, который это ловит: было бы два обращения к /models, стало одно.
        # Мутация: в `ui_projects_batch_text` вернуть прямой вызов `_default_text_model(sid)`
        # вместо `default_model_fn=_default_model_once` (или убрать кэш `_default_model_once`
        # внутри него) — эта проверка ловит: /models был бы спрошен дважды, не один раз.
        make_project(c, "t-model-a", "Без пресета, модель A", channel=901,
                     donor=DONOR, tenants=tenants)
        make_project(c, "t-model-b", "Без пресета, модель B", channel=901,
                     donor=DONOR, tenants=tenants)
        fake.models = [{"value": "codex:gpt-5.7-glow", "label": "glow", "default": True}]
        models_calls_before = len([r for r in fake.rw if r[0] == "/models"])
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-model-a", "t-model-b"]), Req())
        models_calls_after = len([r for r in fake.rw if r[0] == "/models"])
        by26 = {i["id"]: i for i in out["projects"]}
        check("оба проекта пачки без своего пресета — заказ поставлен",
              [by26["t-model-a"]["action"], by26["t-model-b"]["action"]],
              ["ordered", "ordered"])
        check("/models за весь запрос пачки спрошен РОВНО один раз, не по разу на проект",
              models_calls_after - models_calls_before, 1)
        check("обоим проектам ушла одна и та же модель по умолчанию",
              {o[1]["payload"]["model"] for o in rw_orders(fake)[-2:]},
              {"codex:gpt-5.7-glow"})

        # Сухой прогон резолвер вообще не зовёт (Edge case «не платить за просмотр»):
        # ни один из проектов без модели в пресете не должен спросить /models.
        make_project(c, "t-model-dry", "Сухой прогон без пресета", channel=901,
                     donor=DONOR, tenants=tenants)
        models_calls_before = len([r for r in fake.rw if r[0] == "/models"])
        pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-model-dry"], dry_run=True), Req())
        models_calls_after = len([r for r in fake.rw if r[0] == "/models"])
        check("сухой прогон не спрашивает /models вовсе",
              models_calls_after - models_calls_before, 0)

        fake.models = []   # вернуть фикстуру к пустому /models для всех проверок ниже

        # ── 3. Повторное нажатие ─────────────────────────────────────────────
        was = len(rw_orders(fake))
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-donor", "t-text"]), Req())
        by = {i["id"]: i for i in out["projects"]}
        check("повтор ничего не поставил", out["ordered"], 0)
        check("и заказов соседу не слал", len(rw_orders(fake)), was)
        check("а про идущую задачу спросил — это бесплатно",
              "/jobs/rw-1" in rw_asks(fake), True)
        check("причина — уже идёт заказ",
              "уже идёт заказ" in by["t-donor"]["why"], True)
        check("вторых строк в журнале нет", len(calls(c, "t-donor", "text")), 1)

        # Снятая защита «по одному за раз» не отменяет защиту по деньгам: тот же текст
        # с теми же настройками — это тот же оплаченный вызов.
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-donor"], one_at_a_time=False), Req())
        item = out["projects"][0]
        check("без «по одному за раз» повтор всё равно не оплачивается",
              item["action"], "skip")
        check("и сказано, что за это уже платили",
              "уже платили" in item["why"], True)
        check("заказов соседу снова не было", len(rw_orders(fake)), was)

        # Сухой прогон ПОСЛЕ постановки обязан видеть, что за это уже платили: иначе
        # экран пообещал бы работу, которой не будет, и человек ждал бы её зря.
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-donor"], dry_run=True, one_at_a_time=False), Req())
        item = out["projects"][0]
        check("сухой прогон видит уже оплаченное", (out["planned"], item["action"]),
              (0, "skip"))
        check("и говорит, что за это уже платили", "уже платили" in item["why"], True)

        # Выбранный в карточке язык сильнее языка канала — иначе поле в карточке
        # было бы украшением, а человек получил бы не тот язык за свои деньги.
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-both"], languages=["en"]), Req())
        check("язык карточки сильнее языка канала",
              out["projects"][0]["languages"], ["en"])
        check("и именно он уехал соседу",
              [x["code"] for x in rw_orders(fake)[-1][1]["payload"]["languages"]], ["en"])
        check("в журнале единица оплаты — этот же язык",
              [r["unit"] for r in calls(c, "t-both", "text")], ["en"])

        # ── 4. Отпечаток: ночь движка и утро кнопки — один платёж ────────────
        # Самая дорогая ошибка из возможных: движок оплатил текст ночью, человек утром
        # нажал кнопку, и тот же текст оплачен второй раз. Единственная защита от неё —
        # одинаковый отпечаток, собранный из одинаковых ключей.
        make_project(c, "t-night", "Ночной", channel=903, donor=NIGHT, tenants=tenants)
        pub_calls.ensure_calls_schema(c)
        engine_fp = pub_calls.fingerprint(text=sha(fake.subs[NIGHT]), lang="ru",
                                          preset="Ночной пресет", model="gpt-5")
        night = pub_calls.reserve(c, tenant_id="dev", project_id="t-night", kind="text",
                                  unit="ru", fp=engine_fp, provider="rewrite",
                                  model="gpt-5", release_id="rel-t-night")
        pub_calls.mark_ordered(c, night["call_id"], "rw-night")
        c.commit()
        fake.rw_jobs["rw-night"] = {"id": "rw-night", "status": "completed"}
        was = len(rw_orders(fake))
        out = pult.ui_projects_batch_text(pult.BatchTextIn(pids=["t-night"]), Req())
        item = out["projects"][0]
        check("ночной заказ движка виден кнопке как идущий",
              (out["ordered"], item["action"]), (0, "skip"))
        check("и назван по номеру задачи", "rw-night" in item["why"], True)
        # А вот и главное: без «по одному за раз» остаётся ровно одна защита —
        # отпечаток. Соберись он из других ключей, кнопка оплатила бы тот же текст
        # второй раз, и узналось бы это только по счёту.
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-night"], one_at_a_time=False), Req())
        item = out["projects"][0]
        check("текст, оплаченный ночью движком, утром не оплачивается заново",
              (out["ordered"], item["action"]), (0, "skip"))
        check("и сказано, что за это уже платили", "уже платили" in item["why"], True)
        check("заказ соседу не ушёл", len(rw_orders(fake)), was)
        check("пресет проекта виден в отчёте", item["preset"], "Ночной пресет")
        check("второй строки в журнале не появилось",
              len(calls(c, "t-night", "text")), 1)
        check("отпечаток кнопки совпал с отпечатком движка",
              c.execute("SELECT fingerprint FROM provider_calls WHERE project_id=? "
                        "AND kind='text'", ("t-night",)).fetchone()["fingerprint"],
              engine_fp)

        # ── 5. Кто закрывает строки журнала ──────────────────────────────────
        # Строку кнопки движок не закроет никогда: он ищет свои по `release_id`, а у
        # наших он пуст. Значит, закрывает её следующее нажатие — иначе проект «занят»
        # навсегда, а журнал копит открытые строки.
        #
        # Но ЧУЖИЕ строки кнопка не закрывает, даже когда сосед уже всё доделал: по
        # строкам движка (`release_id` непустой) работает поиск оплаченных и не
        # забранных текстов, и «получено» вместо «заказано» вычеркнуло бы оплаченный
        # текст из этого поиска раньше, чем его оттуда забрали.
        check("строку движка кнопка не трогает",
              [r["status"] for r in calls(c, "t-night", "text")], ["ordered"])

        make_project(c, "t-done", "Доделанный", channel=901, donor=OTHER, tenants=tenants)
        out = pult.ui_projects_batch_text(pult.BatchTextIn(pids=["t-done"]), Req())
        job = out["projects"][0]["job_id"]
        check("задача поставлена", out["ordered"], 1)
        check("строка журнала открыта, пока задача идёт",
              [r["status"] for r in calls(c, "t-done", "text")], ["ordered"])
        fake.rw_jobs[job] = {"id": job, "status": "completed"}
        out = pult.ui_projects_batch_text(pult.BatchTextIn(pids=["t-done"]), Req())
        check("сосед доделал — строка журнала закрыта",
              [r["status"] for r in calls(c, "t-done", "text")], ["delivered"])
        check("и проект больше не «занят»: причина теперь про деньги, а не про очередь",
              ("уже платили" in out["projects"][0]["why"],
               "уже идёт" in out["projects"][0]["why"]), (True, False))

        make_project(c, "t-lost", "Пропавший", channel=901, donor=OTHER,
                     scenario="сценарий пропавшего", tenants=tenants)
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-lost"], source_from="text"), Req())
        job = out["projects"][0]["job_id"]
        fake.rw_jobs[job] = {"id": job, "status": "failed", "error": "кончились токены"}
        was = len(rw_orders(fake))
        out = pult.ui_projects_batch_text(pult.BatchTextIn(pids=["t-lost"]), Req())
        item = out["projects"][0]
        check("задача кончилась ничем — строка остаётся открытой, а не «failed»",
              [r["status"] for r in calls(c, "t-lost", "text")], ["ordered"])
        check("нового заказа за те же деньги не ставим", len(rw_orders(fake)), was)
        check("и человеку сказано, что деньги ушли, а нужен разбор",
              ("кончился ничем" in item["why"], "разбор" in item["why"]), (True, True))

        # ── 6. Бронь на уже заказанный язык не выдаётся ──────────────────────
        # Эта проверка стоит прямо на защите: `_batch_langs_left(reserve=True)` обязан
        # считать свежей ТОЛЬКО бронь «new». Разрешить сюда ещё и «verify» (заказ уже
        # создавался, судьба неизвестна) — значит заказать и оплатить второй раз.
        parts = {"text": sha("страж"), "preset": "", "model": ""}
        guard_fp = pub_calls.fingerprint(lang="ru", **parts)
        g = pub_calls.reserve(c, tenant_id="dev", project_id="t-guard", kind="text",
                              unit="ru", fp=guard_fp, provider="rewrite", model="")
        pub_calls.mark_ordered(c, g["call_id"], "rw-guard")
        c.commit()
        item = pult._batch_item("t-guard", "Страж")
        fresh = pult._batch_langs_left(c, "t-guard", "text", ["ru"], parts, item,
                                       reserve=True, provider="rewrite", model="")
        check("на уже заказанный язык бронь не выдаётся", fresh, [])
        check("язык ушёл в пропущенные с номером задачи",
              [(x["code"], x["job_id"]) for x in item["skipped_languages"]],
              [("ru", "rw-guard")])
        pub_calls.mark_delivered(c, g["call_id"], "rw-guard")
        c.commit()
        item = pult._batch_item("t-guard", "Страж")
        fresh = pult._batch_langs_left(c, "t-guard", "text", ["ru"], parts, item,
                                       reserve=True, provider="rewrite", model="")
        check("на уже полученный — тем более", fresh, [])
        check("и причина другая: получено, а не заказано",
              item["skipped_languages"][0]["why"], "уже оплачено и получено")

        # ── 7. Модель: пустое поле карточки = модель пресета проекта ─────────
        make_project(c, "t-model", "По пресету", channel=903, donor=DONOR, tenants=tenants)
        out = pult.ui_projects_batch_text(pult.BatchTextIn(pids=["t-model"]), Req())
        check("поставлено", out["projects"][0]["action"], "ordered")
        check("соседу уехала модель ПРЕСЕТА, а не наше умолчание",
              rw_orders(fake)[-1][1]["payload"]["model"], "gpt-5")
        check("и отпечаток собран по ней же",
              c.execute("SELECT fingerprint FROM provider_calls WHERE project_id=? "
                        "AND kind='text'", ("t-model",)).fetchone()["fingerprint"],
              pub_calls.fingerprint(text=sha(fake.subs[DONOR]), lang="ru",
                                    preset="Ночной пресет", model="gpt-5"))

        # ── 8. Замена источника ──────────────────────────────────────────────
        try:
            pult.ui_projects_batch_text(
                pult.BatchTextIn(pids=["t-repl-1", "t-repl-2"], source_text="один сценарий"),
                Req())
            check("вставленный текст на пачку — отказ", "поставилось", "отказ")
        except pult.HTTPException as exc:
            check("вставленный текст на пачку — отказ", exc.status_code, 400)
            check("и объяснено, почему", "одному проекту" in str(exc.detail), True)

        # Выбор источника и замена спорят друг с другом: замена проверяется первой и
        # молча отменяла выбор. Теперь это отказ ДО постановки — денег ещё не потрачено.
        try:
            pult.ui_projects_batch_text(
                pult.BatchTextIn(pids=["t-repl-1"], source_from="text", source_url=OTHER),
                Req())
            check("выбор источника против замены — отказ", "поставилось", "отказ")
        except pult.HTTPException as exc:
            check("выбор источника против замены — отказ", exc.status_code, 400)
            check("и сказано, что замена сильнее",
                  "замена сильнее" in str(exc.detail), True)

        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-repl-1", "t-repl-2"], source_url=OTHER), Req())
        by = {i["id"]: i for i in out["projects"]}
        check("ссылка карточки раздаётся всем отмеченным", out["ordered"], 2)
        check("и помечена как замена", [by[p]["replaced"] for p in by], [True, True])
        tail = [u[1]["text"] for u in fake.rw if u[0] == "/upload"][-2:]
        check("обоим уехал текст замены, а не их собственный",
              [t == fake.subs[OTHER] for t in tail], [True, True])

        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-empty"], source_text="ручной сценарий"), Req())
        item = out["projects"][0]
        check("одному проекту вставленный текст уходит", item["action"], "ordered")
        check("и именно он", [u[1]["text"] for u in fake.rw if u[0] == "/upload"][-1],
              "ручной сценарий")
        check("замена видна в ответе", (item["source"], item["replaced"]),
              ("replacement-text", True))

        # ── 9. Отказ соседа: деньги решают, можно ли повторять ───────────────
        make_project(c, "t-fail", "Отказ", channel=901, donor=DONOR, tenants=tenants)
        fake.rw_fail = (422, "модель не принята")
        out = pult.ui_projects_batch_text(pult.BatchTextIn(pids=["t-fail"]), Req())
        check("отказ соседа показан как ошибка проекта",
              out["projects"][0]["action"], "error")
        check("до заказа не дошло — строка открыта заново",
              [r["status"] for r in calls(c, "t-fail", "text")], ["failed"])
        fake.rw_fail = (502, "рерайт не отвечает: TimeoutError")
        out = pult.ui_projects_batch_text(pult.BatchTextIn(pids=["t-fail"]), Req())
        # Строка остаётся ОТКРЫТОЙ (`reserved`), а не уходит в `failed`: судьба заказа
        # неизвестна, и следующее нажатие обязано пойти разбираться, а не платить.
        check("обрыв неоднозначен — строка журнала остаётся открытой",
              [r["status"] for r in calls(c, "t-fail", "text")], ["reserved"])
        check("и человеку сказано не нажимать второй раз",
              "не нажимайте второй раз" in out["projects"][0]["why"], True)
        out = pult.ui_projects_batch_text(
            pult.BatchTextIn(pids=["t-fail"], one_at_a_time=False), Req())
        item = out["projects"][0]
        check("следующее нажатие после обрыва не заказывает заново",
              item["action"], "skip")
        check("а отправляет разбираться",
              "нужен разбор" in json.dumps(item, ensure_ascii=False), True)

        # ── 10. Беда одного проекта не уносит отчёт по пачке ─────────────────
        # HTTPException изнутри цикла уносила ВЕСЬ ответ — вместе со строками проектов,
        # за которые уже заплачено: человек не узнал бы, за что именно.
        make_project(c, "t-boo", "С отказом внутри", channel=901,
                     scenario="сценарий проекта", tenants=tenants)
        make_project(c, "t-ok", "Обычный", channel=901, donor=DONOR, tenants=tenants)
        real_scenario = pult._project_scenario

        def boom_scenario(mod, pid):
            if pid == "t-boo":
                raise pult.HTTPException(503, "сценарий прочитать нечем")
            return real_scenario(mod, pid)

        pult._project_scenario = boom_scenario
        try:
            out = pult.ui_projects_batch_text(
                pult.BatchTextIn(pids=["t-boo", "t-ok"]), Req())
        except pult.HTTPException as exc:
            out = {"projects": [], "ordered": -1, "boom": exc.status_code}
        finally:
            pult._project_scenario = real_scenario
        by = {i["id"]: i for i in out["projects"]}
        check("отказ внутри цикла не уносит ответ", sorted(by), ["t-boo", "t-ok"])
        check("проект с отказом — строка ошибки с кодом",
              (by.get("t-boo", {}).get("action"),
               "503" in by.get("t-boo", {}).get("why", "")), ("error", True))
        check("а соседний проект в том же ответе поставлен",
              (out["ordered"], by.get("t-ok", {}).get("action")), (1, "ordered"))

        # ── 11. Обложки ──────────────────────────────────────────────────────
        out = pult.ui_projects_batch_cover(
            pult.BatchCoverIn(pids=["t-donor", "t-empty", "t-cz"], dry_run=True), Req())
        by = {i["id"]: i for i in out["projects"]}
        check("сухой прогон обложек: уйдёт один", out["planned"], 1)
        check("обложке источник — превью донора", by["t-donor"]["source"], "donor")
        check("язык канала переведён в название, которое понимает Cover",
              by["t-donor"]["languages"], ["Русский"])
        check("пресет обложек взят у канала", by["t-donor"]["preset"], "Базовая обложка")
        check("проект без донора пропущен",
              "нет донорской ссылки" in by["t-empty"]["why"], True)
        check("неизвестный обложкам язык канала — отказ, а не молча «Русский»",
              "обложкам неизвестен" in by["t-cz"]["why"], True)
        check("сухой прогон обложек соседа не трогал", fake.cover, [])

        out = pult.ui_projects_batch_cover(
            pult.BatchCoverIn(pids=["t-donor", "t-empty"]), Req())
        by = {i["id"]: i for i in out["projects"]}
        check("обложка поставлена одному проекту", out["ordered"], 1)
        # Тело проверяем ЦЕЛИКОМ: выпади из него пресет, провайдер или режим — сосед
        # нарисовал бы что-то своё, и узналось бы это по счёту, а не по тесту.
        check("тело заказа обложки целиком", cover_orders(fake)[0][1],
              {"languages": ["Русский"], "mode": "url",
               "preset_name": "Базовая обложка",
               "preset_data": {"aspect_ratio": "16:9", "simple_mode": False},
               "urls": [DONOR]})
        check("в журнале строка обложки",
              [r["status"] for r in calls(c, "t-donor", "cover")], ["ordered"])

        make_project(c, "t-cov-full", "Обложка целиком", channel=901, donor=OTHER,
                     tenants=tenants)
        out = pult.ui_projects_batch_cover(
            pult.BatchCoverIn(pids=["t-cov-full"], preset="Пресет карточки",
                              provider="openai", model="gpt-image-1",
                              edit_preview=True, languages=["Английский"]), Req())
        check("поставлено", out["projects"][0]["action"], "ordered")
        check("тело заказа с провайдером и редактированием превью — целиком",
              cover_orders(fake)[-1][1],
              {"languages": ["Английский"], "mode": "url",
               "preset_name": "Пресет карточки",
               "preset_data": {"aspect_ratio": "16:9", "simple_mode": True,
                               "generation_provider": "openai",
                               "generation_model": "gpt-image-1"},
               "urls": [OTHER]})

        was_cover = len(cover_orders(fake))
        out = pult.ui_projects_batch_cover(pult.BatchCoverIn(pids=["t-donor"]), Req())
        check("повтор обложки задачу не плодит", out["ordered"], 0)
        check("и заказов соседу не слал", len(cover_orders(fake)), was_cover)

        out = pult.ui_projects_batch_cover(
            pult.BatchCoverIn(pids=["t-empty"], reference_url=OTHER,
                              languages=["Русский"]), Req())
        item = out["projects"][0]
        check("референс карточки спасает проект без донора", item["action"], "ordered")
        check("и помечен как замена", (item["source"], item["replaced"]),
              ("replacement-url", True))
        check("референс уехал ссылкой заказа", cover_orders(fake)[-1][1]["urls"], [OTHER])

        # Пустой пресет — отказ, а не тихий заказ: сосед нарисовал бы обложку на своих
        # умолчаниях, то есть платно и не ту.
        make_project(c, "t-nopreset", "Без пресета", channel=903, donor=DONOR,
                     tenants=tenants)
        was_cover = len(cover_orders(fake))
        out = pult.ui_projects_batch_cover(
            pult.BatchCoverIn(pids=["t-nopreset"], languages=["Русский"]), Req())
        item = out["projects"][0]
        check("пресета нет ни в карточке, ни у канала — отказ по проекту",
              (out["ordered"], item["action"]), (0, "skip"))
        check("и причина названа словами",
              "не выбран пресет обложек" in item["why"], True)
        check("заказ соседу не ушёл", len(cover_orders(fake)), was_cover)
        check("и строки в журнале не появилось", calls(c, "t-nopreset", "cover"), [])
        out = pult.ui_projects_batch_cover(
            pult.BatchCoverIn(pids=["t-nopreset"], preset="Руками из карточки",
                              languages=["Русский"]), Req())
        check("а с пресетом из карточки — уезжает", out["ordered"], 1)

        # Сбой на одном проекте не уносит отчёт по пачке — и не разрешает платить
        # второй раз: судьба оборванного заказа неизвестна, строка остаётся открытой.
        make_project(c, "t-boom", "Обрыв", channel=901, donor=DONOR, tenants=tenants)
        fake.cover_boom = True
        out = pult.ui_projects_batch_cover(
            pult.BatchCoverIn(pids=["t-boom", "t-empty"]), Req())
        by = {i["id"]: i for i in out["projects"]}
        check("сбой на проекте показан строкой, а не падением всей пачки",
              by["t-boom"]["action"], "error")
        check("остальные проекты в отчёте остались", len(out["projects"]), 2)
        check("оборванный заказ остаётся открытым в журнале",
              [r["status"] for r in calls(c, "t-boom", "cover")], ["reserved"])
        out = pult.ui_projects_batch_cover(pult.BatchCoverIn(pids=["t-boom"]), Req())
        check("и повтор идёт разбираться, а не заказывать",
              out["projects"][0]["action"], "skip")

        # Обложка, которую сосед доделал, тоже закрывает свою строку журнала.
        job = c.execute("SELECT remote_order_id FROM provider_calls WHERE project_id=? "
                        "AND kind='cover'", ("t-donor",)).fetchone()["remote_order_id"]
        fake.cover_jobs[job] = {"id": job, "status": "completed", "results_count": 1}
        out = pult.ui_projects_batch_cover(pult.BatchCoverIn(pids=["t-donor"]), Req())
        check("готовая обложка закрывает строку журнала",
              [r["status"] for r in calls(c, "t-donor", "cover")], ["delivered"])
        check("и повтор отказывает по деньгам, а не по очереди",
              "уже платили" in out["projects"][0]["why"], True)

        # ── 12. Потолок пачки ────────────────────────────────────────────────
        try:
            pult.ui_projects_batch_text(
                pult.BatchTextIn(pids=[f"p{n}" for n in range(pult.BATCH_PULT_MAX + 1)]),
                Req())
            check("пачка сверх потолка — отказ", "поставилось", "отказ")
        except pult.HTTPException as exc:
            check("пачка сверх потолка — отказ", exc.status_code, 400)
        # Экран обязан знать потолок ЗАРАНЕЕ, а не узнавать его отказом на нажатие.
        fields = pult.ui_projects_batch_fields()
        check("потолок пачки отдан экрану вместе с карточками",
              [fields["pult"][k].get("max") for k in ("text", "cover")],
              [pult.BATCH_PULT_MAX, pult.BATCH_PULT_MAX])
        check("и карточки на месте",
              [fields["pult"][k]["post"] for k in ("text", "cover")],
              ["/ui/projects/batch/text", "/ui/projects/batch/cover"])
    finally:
        c.close()
        shutil.rmtree(work, ignore_errors=True)

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
