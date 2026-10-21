#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отказ фильтра провайдера озвучки лечится смягчением текста блока, а не повтором (17.10.2026).

Живой случай: «Nevjerojatne Priče Rata», задачи 3367 и 3861 — FastGen на блок 6 из 10 дважды
ответил «Request blocked by provider safety filters», воркер заказал тот же текст ещё раз,
движок вернул выпуск ещё дважды. По ТЗ («Отказ фильтра») — до трёх раундов смягчения.

Что проверяется:
  • из журнала задачи вынимается номер блока, который не пропустил фильтр; другой отказ
    (нет связи, лимит) блоком не считается;
  • текст режется на блоки тем же кодом, что воркер, — «блок 6» у нас и у него один;
  • модель просят переписать только этот блок, раунды — по возрастанию силы; отказ модели,
    обрывок и разросшийся ответ не принимаются;
  • движок: озвучка поставлена заново, зеркало этапа помнит раунд (`tts-soften:N`),
    прежний текст сохранён рядом; после трёх раундов — остановка с причиной, где назван
    блок, и block_reasons её объясняет; не-фильтровый отказ падает как раньше.

    .venv/bin/python tests/test_tts_soften.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))
os.environ["FASTGEN_TTS_MAX_CHARS"] = "1000"

import tts_soften as S                                            # noqa: E402
import pub_services                                               # noqa: E402
import pub_engine                                                 # noqa: E402
import block_reasons                                              # noqa: E402

NOW = 1_800_000_000.0
FAILS = []
LOG_3861 = [
    "[08:14:52.091] ⏳ FastGen: блок 6: генерация gen_ea23bea641b0474fa9ac65685f7ecbdd создана (802 слов)",
    "[08:14:57.469] ⚠️ FastGen: блок 6: генерация gen_ea23bea641b0474fa9ac65685f7ecbdd — failed: "
    "Request blocked by provider safety filters — заказываю заново",
    "[08:15:03.600] ⚠️ блок 6 из 10 не готов: блок 6: генерация gen_ea5ce1751fa640d886f87135c01a139d"
    " — failed: Request blocked by provider safety filters",
    "[08:15:03.600] ❌ Озвучка FastGen не завершена: не готово блоков 1 из 10; первая причина — блок 6",
    "❌ ошибка (код 1)",
]
# Задача 5306 (21.10.2026): после первого смягчения блок 6 вернулся обрубком звука дважды —
# фильтр режет запись, а не отказывает словами.
LOG_5306 = [
    "[16:13:08.085] ⏳ FastGen: блок 6: генерация gen_ab096373883148898be0279e976d89c1 создана (805 слов)",
    "[16:13:48.708] ⚠️ FastGen: блок 6: генерация gen_ab096373883148898be0279e976d89c1 — звук не похож "
    "на текст: обрубок: 117.9 с на 805 слов (быстрее 200 сл/мин) — заказываю заново",
    "[16:14:29.427] ⚠️ FastGen: блок 6: генерация gen_b1c86367d380485dbd8a6c1edf6a9408 — звук не похож "
    "на текст: обрубок: 122.3 с на 805 слов (быстрее 200 сл/мин)",
    "[16:14:29.428] ⚠️ блок 6 из 10 не готов: блок 6: генерация gen_b1c86367d380485dbd8a6c1edf6a9408 — "
    "звук не похож на текст: обрубок: 122.3 с на 805 слов (быстрее 200 сл/мин)",
    "[16:14:29.428] ❌ Озвучка FastGen не завершена: не готово блоков 1 из 10; первая причина — блок 6",
    "❌ ошибка (код 1)",
]


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def test_failed_block():
    print("Номер блока — из журнала задачи")
    check("живой журнал 3861 → блок 6", S.failed_block(reversed(LOG_3861)), 6)
    check("журнал с конца тоже читается", S.failed_block(LOG_3861), 6)
    check("отказ не фильтра — не блок",
          S.failed_block(["⚠️ FastGen: блок 3: нет связи: timed out", "❌ ошибка (код 1)"]), None)
    check("пустой журнал", S.failed_block([]), None)
    print("Обрубок звука — отдельный детектор")
    check("журнал 5306: фильтр словами не отказывал", S.failed_block(LOG_5306), None)
    check("журнал 5306: обрубок на блоке 6", S.stub_block(LOG_5306), 6)
    check("журнал 5306 с конца", S.stub_block(reversed(LOG_5306)), 6)
    check("отказ фильтра — не обрубок", S.stub_block(LOG_3861), None)
    check("нет связи — не обрубок", S.stub_block(["⚠️ FastGen: блок 3: нет связи: timed out"]), None)


# ~700 знаков на абзац при пределе 1000 → каждый абзац сам себе блок: блоков 8, блок 6 есть.
PARA = ("Vojnici su krenuli u zoru. " * 26).strip()


def make_project():
    d = tempfile.mkdtemp(prefix="tts-soften-")
    text = "\n".join(f"Odlomak {i}. {PARA}" for i in range(1, 9)) + "\n"
    with open(os.path.join(d, "source.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return d, text


def test_blocks_match_worker():
    print("\nБлоки режутся как у воркера")
    import fastgen_tts_worker as W
    d, text = make_project()
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    ours = S.blocks_of(text, 1000)
    check("границы те же, что у воркера", ours, W.split_text(paragraphs, 1000))
    check("блоков больше одного — есть что смягчать по одному", len(ours) > 1, True)


def test_reply_validation():
    print("\nОтвет модели проверяется")
    original = PARA
    ok = original.replace("Vojnici", "Ljudi")
    check("нормальный ответ принят", S.clean_reply("```\n" + ok + "\n```", original), ok)
    for label, reply in (("отказ модели", "I can't help with rewriting violent content."),
                         ("пустой", "   "),
                         ("обрывок", "Vojnici."),
                         ("разросся вдвое", (ok + " ") * 3)):
        try:
            S.clean_reply(reply, original)
            check(f"{label} — отвергнут", False, True)
        except S.SoftenError:
            check(f"{label} — отвергнут", True, True)
    m = S.messages_for("текст", "hr", 3)
    check("язык канала назван", "хорватский" in m[1]["content"], True)
    check("третий раунд — нейтральный пересказ", "нейтральным" in m[1]["content"], True)
    check("первый раунд бережнее третьего",
          S.messages_for("т", "hr", 1)[1]["content"] != m[1]["content"], True)


def test_soften_project_writes_backup():
    print("\nЗапись: блок заменён, прежний текст рядом")
    d, text = make_project()
    blocks = S.blocks_of(text, 1000)
    target = 2
    seen = {}
    def chat(messages, key, base):
        seen["prompt"] = messages[1]["content"]
        return blocks[target - 1].replace("Vojnici", "Ljudi")
    info = S.soften_project(d, target, "hr", 1, "k", "http://x", max_chars=1000, chat=chat)
    check("в просьбе — только этот блок", blocks[target - 1] in seen["prompt"]
          and blocks[0] not in seen["prompt"], True)
    new = open(os.path.join(d, "source.txt"), encoding="utf-8").read()
    check("блок заменён", "Ljudi" in new, True)
    check("другие блоки целы", new.count("Vojnici") > 0, True)
    check("прежний текст сохранён", open(info["backup"], encoding="utf-8").read(), text)
    check("число блоков названо", info["blocks"], len(blocks))
    new_blocks = S.blocks_of(new, 1000)
    check("новый текст режется без потерь слов",
          " ".join(" ".join(new_blocks).split()) == " ".join(new.split()), True)
    try:
        S.soften_project(d, 99, "hr", 2, "k", "http://x", max_chars=1000, chat=chat)
        check("несуществующий блок — ошибка", False, True)
    except S.SoftenError:
        check("несуществующий блок — ошибка", True, True)
    try:
        S.soften_project(d, 1, "hr", 2, "k", "http://x", max_chars=1000,
                         chat=lambda m, k, b: S.blocks_of(new, 1000)[0])
        check("ответ без изменений — ошибка", False, True)
    except S.SoftenError:
        check("ответ без изменений — ошибка", True, True)


# ─────────────────────────── движок ───────────────────────────

def fresh(mark=None, logs=LOG_3861, settings='{"engine":"fastgen","voice":"algieba"}',
          sibling_template=None):
    """База для движка. `sibling_template` — шаблон Lumean у ПРЕЖНЕГО ролика того же канала
    (19.10.2026: шаблон лежит в настройках конкретного проекта, у соседнего ролика канала
    он может быть, а у этого нет)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
      CREATE TABLE pub_releases(release_id TEXT PRIMARY KEY, channel_id INTEGER,
                                project_id TEXT, slot_at REAL);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT, project_id TEXT,
                               kind TEXT, status TEXT, error TEXT);
      CREATE TABLE job_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, ts REAL, line TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        service TEXT, kind TEXT, remote_job_id TEXT, status TEXT, error TEXT, created REAL,
        updated REAL, UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT, repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
    """)
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.execute("INSERT INTO projects VALUES('proj-1','dev',?)", (settings,))
    c.execute("INSERT INTO pub_releases VALUES('rel-1',7,'proj-1',?)", (NOW,))
    if sibling_template:
        c.execute("INSERT INTO projects VALUES('proj-0','dev',?)",
                  ('{"engine":"fastgen","voice_uuid":"%s"}' % sibling_template,))
        c.execute("INSERT INTO pub_releases VALUES('rel-0',7,'proj-0',?)", (NOW - 86400,))
    c.execute("INSERT INTO queue_tasks VALUES(10,'dev','proj-1','tts','error','rc=1')")
    for line in logs:
        c.execute("INSERT INTO job_logs(job_id,ts,line) VALUES(10,?,?)", (NOW, line))
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,error,"
              "created,updated) VALUES('rel-1','vsgen','tts','10','error',?,?,?)", (mark, NOW, NOW))
    c.commit()
    return c


REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "hr",
       "channel_id": 7, "preset_cover": "", "donor_url": "", "updated": NOW}


def services(placed, pdir):
    return pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda uid, tid, pid, kind, params: (placed.append((kind, params)), 100 + len(placed))[1],
        "project_dir": lambda *a, **k: pdir})


def tts_row(c):
    return dict(c.execute("SELECT remote_job_id, status, error FROM pub_release_jobs WHERE kind='tts'").fetchone())


def last_msg(c):
    r = c.execute("SELECT message FROM pub_release_log ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r else ""


def all_msgs(c):
    return [r[0] for r in c.execute("SELECT message FROM pub_release_log ORDER BY id")]


def test_engine():
    print("\nДвижок: фильтр → смягчение → озвучка заново")
    pub_services.get_key = lambda c, name: "rewrite-key"
    saved = S.chat_once
    calls = []
    def fake_chat(messages, key, base, **kw):
        calls.append((key, base, messages[1]["content"][:60]))
        frag = messages[1]["content"].split("ФРАГМЕНТ:\n", 1)[1]
        return frag.replace("Vojnici", "Ljudi")
    S.chat_once = fake_chat
    try:
        pdir, text = make_project()
        c = fresh()
        placed = []
        out = services(placed, pdir).do_media_ready(c, REL)
        check("движок ждёт новую озвучку", out, {"wait": True})
        # Картинки движок ставит рядом с озвучкой всегда (они друг друга не ждут) —
        # смотрим только на озвучку.
        tts_placed = [p for k, p in placed if k == "tts"]
        check("озвучка поставлена заново", len(tts_placed), 1)
        check("с параметрами пресета", tts_placed[0].get("engine"), "fastgen")
        row = tts_row(c)
        check("зеркало помнит раунд и блок", (row["status"], row["error"]), ("queued", "tts-soften:1:6"))
        check("ключ сервиса текста передан", calls and calls[0][0] == "rewrite-key", True)
        check("текст изменён", "Ljudi" in open(os.path.join(pdir, "source.txt"), encoding="utf-8").read(), True)
        check("прежний текст рядом", os.path.exists(os.path.join(pdir, "source.txt.before-soften-1")), True)
        check("в журнале выпуска сказано, что сделано",
              "смягчён" in last_msg(c) and "блок 6" in last_msg(c), True)

        print("\nТри раунда кончились, у пресета есть шаблон Lumean — озвучка уходит на Lumean")
        c = fresh(mark="tts-soften:3",
                  settings='{"engine":"fastgen","voice":"algieba","voice_uuid":"0b1c2d3e-aaaa-bbbb-cccc-0123456789ab"}')
        placed = []
        out = services(placed, pdir).do_media_ready(c, REL)
        check("движок ждёт озвучку", out, {"wait": True})
        lum = [p for k, p in placed if k == "tts"]
        check("поставлена одна задача озвучки", len(lum), 1)
        check("движок — Lumean, шаблон из пресета",
              (lum[0].get("engine"), lum[0].get("voice_uuid")),
              ("lumean", "0b1c2d3e-aaaa-bbbb-cccc-0123456789ab"))
        check("зеркало помнит переход", tts_row(c)["error"], "tts-lumean:1")
        check("в журнале сказано про Lumean", "Lumean" in last_msg(c), True)

        print("\nLumean тоже упал на фильтре — дальше человек, второго круга нет")
        c = fresh(mark="tts-lumean:1", settings='{"engine":"fastgen","voice_uuid":"0b1c2d3e-aaaa-bbbb-cccc-0123456789ab"}')
        placed = []
        try:
            services(placed, pdir).do_media_ready(c, REL)
            check("остановлен", False, True)
        except pub_engine.ItemFailed as exc:
            check("остановлен", "перехода на Lumean" in str(exc), True)
        check("озвучку не ставили", [k for k, _ in placed if k == "tts"], [])

        print("\nУ этого ролика шаблона нет, но канал уже озвучивался Lumean — берём его шаблон")
        c = fresh(mark="tts-soften:3", sibling_template="9f8e7d6c-1111-2222-3333-cba987654321")
        placed = []
        out = services(placed, pdir).do_media_ready(c, REL)
        check("движок ждёт озвучку", out, {"wait": True})
        lum = [p for k, p in placed if k == "tts"]
        check("поставлена одна задача озвучки", len(lum), 1)
        check("движок — Lumean, шаблон от прежнего ролика канала",
              (lum[0].get("engine"), lum[0].get("voice_uuid")),
              ("lumean", "9f8e7d6c-1111-2222-3333-cba987654321"))
        check("в журнале сказано, откуда голос",
              "прежних роликов этого канала" in last_msg(c), True)

        print("\nТри раунда кончились, шаблона Lumean нет ни здесь, ни у канала — остановка")
        c = fresh(mark="tts-soften:3")
        placed = []
        try:
            services(placed, pdir).do_media_ready(c, REL)
            check("выпуск остановлен", False, True)
        except pub_engine.ItemFailed as exc:
            reason = str(exc)
            check("выпуск остановлен", True, True)
            check("причина называет блок", reason.startswith("озвучка: блок 6 не прошёл фильтр"), True)
            check("это не «временный сбой» для автовозврата",
                  pub_engine.transient_kind(reason), None)
            b = block_reasons.explain(reason)
            check("block_reasons объясняет", b.get("title"), "озвучка: фильтр провайдера не пропустил текст")
            check("и говорит, какой блок править", "блок 6" in b.get("fix", ""), True)
        check("озвучку заново не ставили", [k for k, _ in placed if k == "tts"], [])

        print("\nОбрубок звука на только что смягчённом блоке (21.10.2026) — следующий раунд")
        pdir3, _ = make_project()
        c = fresh(mark="tts-soften:1:6", logs=LOG_5306)
        placed = []
        out = services(placed, pdir3).do_media_ready(c, REL)
        check("движок ждёт новую озвучку", out, {"wait": True})
        check("озвучка поставлена заново", len([p for k, p in placed if k == "tts"]), 1)
        check("зеркало: второй раунд, тот же блок", tts_row(c)["error"], "tts-soften:2:6")
        check("журнал объясняет обрубок",
              any("обрубком" in m for m in all_msgs(c)) and "раунд 2 из 3" in last_msg(c), True)

        print("\nМетка старого вида без номера блока (r_61c7232bb0) — обрубок тоже ведёт на раунд 2")
        pdir4, _ = make_project()
        c = fresh(mark="tts-soften:1", logs=LOG_5306)
        placed = []
        out = services(placed, pdir4).do_media_ready(c, REL)
        check("движок ждёт новую озвучку", out, {"wait": True})
        check("зеркало: второй раунд, блок теперь записан", tts_row(c)["error"], "tts-soften:2:6")

        print("\nОбрубок на другом блоке — битый звук, падает как раньше")
        c = fresh(mark="tts-soften:1:6",
                  logs=[l.replace("блок 6", "блок 3") for l in LOG_5306])
        placed = []
        try:
            services(placed, pdir3).do_media_ready(c, REL)
            check("ItemFailed", False, True)
        except pub_engine.ItemFailed as exc:
            check("ItemFailed", str(exc), "этап «tts» завершился со статусом error")
        check("озвучку не ставили", [k for k, _ in placed if k == "tts"], [])

        print("\nОбрубок без смягчения до него — тоже как раньше")
        c = fresh(logs=LOG_5306)
        try:
            services([], pdir3).do_media_ready(c, REL)
            check("ItemFailed", False, True)
        except pub_engine.ItemFailed as exc:
            check("ItemFailed", str(exc), "этап «tts» завершился со статусом error")

        print("\nДругой отказ — падает как раньше")
        n_calls = len(calls)
        c = fresh(logs=["⚠️ FastGen: блок 2: нет связи: timed out", "❌ ошибка (код 1)"])
        placed = []
        try:
            services(placed, pdir).do_media_ready(c, REL)
            check("ItemFailed", False, True)
        except pub_engine.ItemFailed as exc:
            check("ItemFailed", str(exc), "этап «tts» завершился со статусом error")
        check("модель не звали", len(calls), n_calls)

        print("\nМодель отказалась — падаем как раньше, в журнале сказано почему")
        S.chat_once = lambda m, k, b, **kw: "I can't help with that."
        c = fresh()
        try:
            services([], pdir).do_media_ready(c, REL)
            check("ItemFailed", False, True)
        except pub_engine.ItemFailed as exc:
            check("ItemFailed", str(exc), "этап «tts» завершился со статусом error")
        check("журнал объясняет", "смягчение" in last_msg(c) and "не удалось" in last_msg(c), True)

        print("\nСервис текста не ответил (21.10.2026) — ждём, раунд не потрачен, выпуск не падает")
        timeouts = []
        def slow_chat(m, k, b, **kw):
            timeouts.append(1)
            raise S.SoftenError("сервис текста не ответил: ReadTimeout")
        S.chat_once = slow_chat
        c = fresh()
        placed = []
        try:
            services(placed, pdir).do_media_ready(c, REL)
            check("поднят ResourceDown", False, True)
        except pub_services.ResourceDown as exc:
            check("поднят ResourceDown", exc.resource, "codex")
        row = tts_row(c)
        check("зеркало помнит ожидание (раунд 0)", row["error"].startswith("tts-soften-wait:0:"), True)
        check("озвучку заново не ставили", [k for k, _ in placed if k == "tts"], [])
        check("в журнале сказано «отложено»", "отложено" in last_msg(c), True)
        try:
            services(placed, pdir).do_media_ready(c, REL)
            check("второй круг сразу — снова ждём", False, True)
        except pub_services.ResourceDown as exc:
            check("второй круг сразу — снова ждём", "повтор через" in str(exc), True)
        check("сервис текста второй раз не дёргали", len(timeouts), 1)
        # Прошло больше TTS_SOFTEN_WAIT_SEC — тот же раунд пробуется заново и проходит.
        S.chat_once = fake_chat
        pdir2, _ = make_project()       # текст ещё не смягчён — как у настоящего ролика
        c.execute("UPDATE pub_release_jobs SET error=? WHERE kind='tts'",
                  (f"tts-soften-wait:0:{int(time.time()) - 2000}",))
        c.commit()
        out = services(placed, pdir2).do_media_ready(c, REL)
        check("после паузы смягчение прошло", out, {"wait": True})
        check("раунд считается первым, не вторым", tts_row(c)["error"], "tts-soften:1:6")
    finally:
        S.chat_once = saved


def main():
    test_failed_block(); test_blocks_match_worker(); test_reply_validation()
    test_soften_project_writes_backup(); test_engine()
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
