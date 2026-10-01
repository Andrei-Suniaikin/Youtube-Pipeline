#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Подтвердить руками» — единственный выход из тупика выпуска (01.10.2026).

Дефект Д28, в разборе названный самым опасным местом: у выпуска, у которого загрузка
уже начиналась, не было НИ ОДНОГО доступного действия. Вернуть в работу нечем (повтор
заливки запрещён навсегда), снять строку плана нельзя, переставить нельзя, а выход
канала занят насовсем. Единственным выходом оставалась правка базы руками. При этом
ролик на канале чаще всего есть — не получено лишь machine-readable подтверждение.

Решение владельца, п. 6: «подтвердить руками» считается выполнением плана, пометка
«подтверждено человеком» остаётся навсегда.

Любая ошибка здесь даёт либо второй ролик на канале, либо потерянный навсегда выход.
Поэтому проверяется в обе стороны — и что действие работает, и что оно НЕ работает там,
где работать не должно:

1. **Без номера ролика не подтверждаем.** «Не знаем» не равно «подтверждено». Номер
   берётся у ноды, если она его прислала, иначе его вписывает человек.
2. **Расхождение номеров — отказ.** Нода прислала один, человек вписал другой: закрывать
   такое нельзя, это разбор.
3. **Сорвавшееся до отправки файла не закрываем.** Такой выпуск возвращают в работу, и
   подмена одного другим означала бы, что ролика на канале не будет никогда.
4. **Выход канала не освобождается.** Освободив его, мы отдали бы то же время второму
   ролику.
5. **Пометка не снимается ничем.** Возврат в работу подтверждённому руками отказывает.
6. **Машинным подтверждением это не становится.** В сводке такие выходы названы отдельно:
   за `published` стоит ответ YouTube Data API, здесь — человек.
7. **Сорвавшуюся ссылку нельзя вставить заново.** Строка плана в `error` держит выход, а
   дубль её раньше не видел: та же ссылка заводила второй выпуск того же ролика.

    .venv/bin/python tests/test_hand_confirm.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine                                                  # noqa: E402
import pub_schema                                                  # noqa: E402
import pub_tags                                                    # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_has(label, text, piece):
    if piece.lower() in str(text or "").lower():
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        искали: {piece!r}\n        в:      {text!r}")
        FAILS.append(label)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    return c


def release(c, n=1, phase="upload_running", status="blocked", video_id=None,
            job_video=None, job_status="error", job_phase=None, item_status="error"):
    """Канал, строка плана, выпуск и задание заливки в заданном состоянии."""
    uc = "UC" + str(n) * 22
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) "
              "VALUES('t1',?,?,'ru','Europe/Warsaw','{}',1,0,?,?)",
              (uc, f"Канал {n}", NOW, NOW))
    ch = c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_video_id,donor_title,slot_at,status,created,updated) "
              "VALUES(?,'t1',?,1,?,?,'Донор',?,?,?,?)",
              (n, ch, f"https://youtu.be/donor{n}aaaaa", f"donor{n}aaaaa",
               NOW - 3600, item_status, NOW, NOW))
    rid = f"rel-{n}"
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
              "blocked_reason,video_id,created,updated) "
              "VALUES(?,'t1',?,1,?,?,?,?,?,?,'браузер умер на последнем шаге',?,?,?)",
              (rid, n, ch, uc, NOW - 3600, f"p{n}", status, phase, video_id, NOW, NOW))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,"
              "channel_url,title,video_path,slot_at,status,phase,video_id,created,updated)"
              " VALUES(?,'t1',?,?,'http://c','Ролик','/v.mp4',?,?,?,?,?,?)",
              (rid, ch, uc, NOW - 3600, job_status, job_phase, job_video, NOW, NOW))
    # Журнал фаз — он же защита от повторной заливки: именно по нему
    # `reached_irreversible` решает, уходил ли файл. В бою его пишет движок на каждом
    # шаге (`log_phase`); без него проверка «уходил ли файл» отвечала бы «нет» на
    # выпуске, который давно в YouTube.
    путь = list(pub_engine.PHASES[:pub_engine.PHASE_INDEX.get(phase, 0) + 1])
    for seq, ph in enumerate(путь, start=1):
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,created,repeats,"
                  "last_seen) VALUES(?,?,?,?,1,?)", (rid, seq, ph, NOW - 3600, NOW - 3600))
    c.commit()
    return rid, ch


def rel_of(c, rid):
    return c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone()


def журнал(c, rid):
    return " | ".join(r[0] for r in c.execute(
        "SELECT message FROM pub_release_log WHERE release_id=? ORDER BY id", (rid,)))


def main() -> int:
    print("Подтверждать нечем: номера ролика не знает никто")
    c = fresh()
    rid, _ = release(c, 1, job_phase=json.dumps({"phase": "upload_running"}))
    out = pub_engine.confirm_by_hand(c, rid, who="хозяин", now=NOW)
    check("отказ", out["ok"], False)
    check_has("и сказано, что делать", out["why"], "скопируйте ссылку")
    check("выпуск не тронут", rel_of(c, rid)["status"], "blocked")

    print("\nЧеловек вписал ссылку — принимаем")
    out = pub_engine.confirm_by_hand(c, rid, who="хозяин",
                                    video_id="https://youtu.be/abcDEF12345",
                                    note="ролик виден на канале", now=NOW)
    check("подтверждено", out["ok"], True)
    check("номер разобран из ссылки", out["video_id"], "abcDEF12345")
    r = rel_of(c, rid)
    check("выпуск закрыт как выполненный", r["status"], "done")
    check("номер ролика записан", r["video_id"], "abcDEF12345")
    check("пометка «подтверждено человеком» стоит", r["hand_confirmed"], NOW)
    check("и кто подтвердил", r["hand_by"], "хозяин")
    check("и заметка", r["hand_note"], "ролик виден на канале")
    check("строка плана выполнена",
          c.execute("SELECT status FROM pub_plan_items WHERE id=1").fetchone()[0], "done")
    job = c.execute("SELECT status, video_id FROM pub_upload_jobs WHERE release_id=?",
                    (rid,)).fetchone()
    check("задание закрыто", (job["status"], job["video_id"]), ("done", "abcDEF12345"))
    check_has("в журнале сказано, кто и что", журнал(c, rid), "подтверждено руками (хозяин)")
    check_has("и что заливка не повторится", журнал(c, rid), "не повторится")
    check("событие в журнале фаз есть",
          bool(c.execute("SELECT 1 FROM pub_release_events WHERE release_id=? "
                         "AND phase='hand_confirmed'", (rid,)).fetchone()), True)

    print("\nВыход канала остаётся занятым")
    # Освободив выход, мы отдали бы то же время второму ролику: первый на канале уже есть.
    ch = c.execute("SELECT channel_id FROM pub_releases WHERE release_id=?",
                   (rid,)).fetchone()[0]
    check("слот по-прежнему занят", len(pub_tags.taken_slots(c, ch)), 1)

    print("\nПовторное подтверждение отвергается")
    out = pub_engine.confirm_by_hand(c, rid, who="другой", now=NOW + 10)
    check("отказ", out["ok"], False)
    check_has("и сказано почему", out["why"], "уже подтверждён руками")
    check("кто подтвердил — не перезаписан", rel_of(c, rid)["hand_by"], "хозяин")

    print("\nВозврат в работу подтверждённому руками отказывает")
    # Пометка остаётся НАВСЕГДА (решение владельца, п. 6). Возврат означал бы второй
    # ролик на канале, поэтому проверяется и отказ, и то, что колонки целы.
    c.execute("UPDATE pub_releases SET status='blocked' WHERE release_id=?", (rid,))
    c.commit()
    out = pub_engine.revive(c, rid, now=NOW + 20)
    check("отказ", out["ok"], False)
    check_has("и сказано почему", out["why"], "подтверждён руками")
    r = rel_of(c, rid)
    check("пометка цела", (r["hand_confirmed"], r["hand_by"], r["hand_video_id"]),
          (NOW, "хозяин", "abcDEF12345"))

    print("\nНода прислала номер — вписывать не нужно")
    c2 = fresh()
    rid2, _ = release(c2, 2, job_video="nodeAAAA111",
                      job_phase=json.dumps({"phase": "upload_running"}))
    out = pub_engine.confirm_by_hand(c2, rid2, who="оператор", now=NOW)
    check("подтверждено", out["ok"], True)
    check("номер взят у ноды", out["video_id"], "nodeAAAA111")

    print("\nНомера разошлись — это разбор, а не подтверждение")
    c3 = fresh()
    rid3, _ = release(c3, 3, job_video="nodeAAAA111",
                      job_phase=json.dumps({"phase": "upload_running"}))
    out = pub_engine.confirm_by_hand(c3, rid3, who="оператор",
                                    video_id="otherBBB222", now=NOW)
    check("отказ", out["ok"], False)
    check_has("названы оба номера", out["why"], "nodeAAAA111")
    check("выпуск не тронут", rel_of(c3, rid3)["status"], "blocked")

    print("\nМусор в поле номера не принимается")
    c4 = fresh()
    rid4, _ = release(c4, 4, job_phase=json.dumps({"phase": "upload_running"}))
    for мусор in ("посмотрю потом", "да", "https://example.com/видео"):
        out = pub_engine.confirm_by_hand(c4, rid4, video_id=мусор, now=NOW)
        check(f"«{мусор}» отвергнут", out["ok"], False)
        # Ответ про вписанное, а не общий «номера не знаем»: человек уже что-то вписал,
        # и отсылать его вписать ссылку значит просить сделать то, что он сделал.
        check_has(f"и в отказе названо вписанное («{мусор[:12]}…»)", out["why"], мусор[:12])
    check("выпуск не тронут", rel_of(c4, rid4)["status"], "blocked")

    print("\nФайл в YouTube не уходил — такой выпуск возвращают в работу")
    # Главный предохранитель. Подтвердив сорвавшийся до отправки выпуск, мы закрыли бы
    # его как вышедший — и ролика на канале не было бы никогда.
    c5 = fresh()
    rid5, _ = release(c5, 5, phase="upload_pending", job_status="error", job_phase=None)
    out = pub_engine.confirm_by_hand(c5, rid5, video_id="abcDEF12345", now=NOW)
    check("отказ", out["ok"], False)
    check_has("и сказано, что делать вместо этого", out["why"], "возвращают в работу")
    check("выпуск не тронут", rel_of(c5, rid5)["status"], "blocked")
    # А возврат в работу по нему проходит — тупика нет.
    out = pub_engine.revive(c5, rid5, now=NOW)
    check("возврат в работу проходит", out["ok"], True)

    print("\nПодтверждают только выпуск в разборе")
    c6 = fresh()
    rid6, _ = release(c6, 6, status="running", job_video="nodeAAAA111")
    out = pub_engine.confirm_by_hand(c6, rid6, now=NOW)
    check("отказ", out["ok"], False)
    check_has("и сказано почему", out["why"], "только выпуск в разборе")

    print("\nСорвавшуюся ссылку нельзя вставить заново")
    # Строка плана в `error` держит выход канала, а дубль её раньше не видел: та же
    # донорская ссылка заводила вторую строку и второй выпуск того же ролика.
    c7 = fresh()
    _, ch7 = release(c7, 7, item_status="error")
    канал = c7.execute("SELECT * FROM pub_channels WHERE id=?", (ch7,)).fetchone()
    out = pub_tags.add_links(c7, "t1", канал, ["https://youtu.be/donor7aaaaa"], NOW)
    check("ссылка пропущена как дубль", out["skipped"], ["https://youtu.be/donor7aaaaa"])
    check("второй строки плана не появилось",
          c7.execute("SELECT COUNT(*) FROM pub_plan_items").fetchone()[0], 1)
    check("и та же ссылка в другом написании тоже",
          pub_tags.add_links(c7, "t1", канал,
                             ["https://www.youtube.com/watch?v=donor7aaaaa&t=30"],
                             NOW)["skipped"],
          ["https://www.youtube.com/watch?v=donor7aaaaa&t=30"])
    check("`error` входит в набор дубля", "error" in pub_tags.DUP_STATUSES, True)
    # А снятую человеком ссылку вернуть можно: он убрал её сам, и запирать канал на
    # ролике, от которого отказались, нельзя.
    c7.execute("UPDATE pub_plan_items SET status='cancelled' WHERE id=7")
    c7.commit()
    out = pub_tags.add_links(c7, "t1", канал, ["https://youtu.be/donor7aaaaa"], NOW)
    check("снятую ссылку вставить можно", out["skipped"], [])
    check("`cancelled` в набор дубля не входит",
          "cancelled" in pub_tags.DUP_STATUSES, False)

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
