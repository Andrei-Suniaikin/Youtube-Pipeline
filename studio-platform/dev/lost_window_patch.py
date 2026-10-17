#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кнопка «Окно потеряно — залить заново» для выпуска, у которого загрузка начиналась,
но номера ролика нет, а окно заливки на машине закрыто.

17.10.2026, решение владельца. Восемь окон с «Verify it's you» на Минске простояли по
двое суток, Google завершил в них сессию, и файл, залитый внутри мастера без номера,
пропал вместе с окном. Таким выпускам правило «загрузка начиналась — повтор запрещён»
больше не помогает, а вредит: ролика на канале нет и не будет, а выход канала занят.

Что меняется (всё — только добавление):
  • pub_engine.reached_irreversible: отметка «lost_window» в истории фаз обнуляет улики
    до неё;
  • pub_engine.lost_window_refusal + revive(..., lost_window=True): пускает только выпуск
    без номера ролика, с заданием в repair/error, которое машина не держит; пишет отметку
    и строку в журнал; задание аннулируется, комплект собирается заново;
  • web/app.py: POST /ui/releases/{id}/lost_window;
  • web/index.html: кнопка на странице выпуска (blocked, фаза upload_running, без номера);
  • tests/test_lost_window.py.

Защита от второго ролика остаётся двойной: выпуск с номером сюда не пускается
(это «подтвердить руками»), а заливщик перед свежей загрузкой ищет на канале строку с
таким названием и, найдя, доводит её, а не заливает второй раз.

Применить из корня репозитория:  python3 studio-platform/dev/lost_window_patch.py
Проверить:  cd studio-platform && VS_ALERTS=0 .venv/bin/python tests/test_lost_window.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENGINE = os.path.join(ROOT, "server", "pub_engine.py")
APP = os.path.join(ROOT, "web", "app.py")
HTML = os.path.join(ROOT, "web", "index.html")
TEST = os.path.join(ROOT, "tests", "test_lost_window.py")


def replace_once(text, old, new, where):
    if text.count(old) != 1:
        raise SystemExit(f"{where}: якорь не найден или не уникален ({text.count(old)}): {old[:60]!r}")
    return text.replace(old, new)


# ─────────────────────────── pub_engine.py ───────────────────────────

E_OLD_1 = """    for phase in phase_history(c, release_id):
        if PHASE_INDEX.get(phase, -1) >= IRREVERSIBLE_FROM:
            return True
    return False
"""
E_NEW_1 = """    history = phase_history(c, release_id)
    if "lost_window" in history:
        # Человек решил, что окно заливки потеряно и ролика на канале нет (17.10.2026):
        # всё, что было до этой отметки, уликой начатой загрузки больше не считается.
        history = history[len(history) - history[::-1].index("lost_window"):]
    for phase in history:
        if PHASE_INDEX.get(phase, -1) >= IRREVERSIBLE_FROM:
            return True
    return False
"""

E_OLD_2 = "def revive(c, release_id: str, now: float = None, keep_attempts: bool = False) -> dict:\n"
E_NEW_2 = '''def lost_window_refusal(c, release_id: str) -> str:
    """Почему выпуск НЕЛЬЗЯ заливать заново как «окно потеряно». Пусто — можно.

    17.10.2026. Восемь окон на Минске с «Verify it's you» простояли по двое суток, Google
    завершил в них сессию, и файл, залитый внутри мастера без номера, пропал вместе с
    окном. Такому выпуску правило «загрузка начиналась — повтор запрещён» больше не
    помогает, а вредит: ролика на канале нет, и не будет никогда. Человек закрывает окно
    и нажимает «окно потеряно» — выпуск собирает комплект заново и заливает файл ещё раз.

    Защита от второго ролика остаётся двойной: сюда не пускаем выпуск с номером ролика
    (номер — улика, что ролик на канале есть: это «подтвердить руками»), и выпуск, чьё
    задание машина держит прямо сейчас; а сам заливщик перед свежей загрузкой ищет на
    канале строку с таким названием и, найдя, доводит её, а не заливает второй раз.
    """
    job = c.execute("SELECT status, video_id FROM pub_upload_jobs WHERE release_id=?",
                    (release_id,)).fetchone()
    if job is None:
        return "задания заливки нет — это обычный «Вернуть в работу»"
    if job["video_id"]:
        return (f"у ролика есть номер {job['video_id']} — значит, он на канале есть; "
                "окно не потеряно, закрывайте через «Подтвердить руками»")
    if (job["status"] or "") == "running":
        return "машина держит задание прямо сейчас — дождитесь её вердикта"
    if (job["status"] or "") not in ("repair", "error"):
        return f"задание в состоянии «{job['status']}» — окно потерянным не считается"
    return ""


def revive(c, release_id: str, now: float = None, keep_attempts: bool = False,
           lost_window: bool = False) -> dict:
'''

E_OLD_3 = """    if reached_irreversible(c, release_id):
        return {"ok": False,
                "why": "загрузка уже начиналась — повтор запрещён навсегда, "
                       "нужен разбор вручную"}
"""
E_NEW_3 = """    if reached_irreversible(c, release_id):
        if not lost_window:
            return {"ok": False,
                    "why": "загрузка уже начиналась — повтор запрещён навсегда, "
                           "нужен разбор вручную"}
        why = lost_window_refusal(c, release_id)
        if why:
            return {"ok": False, "why": why}
        # Отметка в истории фаз: с неё `reached_irreversible` считает заново.
        log_phase(c, release_id, "lost_window", now,
                  payload={"from_phase": rel["phase"], "reason": rel["blocked_reason"]})
        log_line(c, release_id, "заливка",
                 "окно заливки на машине потеряно, ролика на канале нет — человек разрешил "
                 "новую заливку; комплект соберётся заново, заливщик перед загрузкой сверит "
                 "контент канала по названию", now=now)
"""

E_OLD_4 = """    annul = bool(job and phase in ("upload_pending", "upload_running")
                 and (job["status"] or "") != "running"
                 and not job["claim_token"] and not job["video_id"])
"""
E_NEW_4 = """    annul = bool(job and phase in ("upload_pending", "upload_running")
                 and (job["status"] or "") != "running"
                 and (lost_window or not job["claim_token"]) and not job["video_id"])
"""

# ─────────────────────────── web/app.py ───────────────────────────

A_OLD = """# ─────────────── обложка выпуска глазами человека (04.10.2026) ───────────────
"""
A_NEW = '''@app.post("/ui/releases/{release_id}/lost_window")
def ui_release_lost_window(release_id: str):
    """Окно заливки потеряно, ролика на канале нет — залить заново.

    17.10.2026. Окна с «Verify it's you» на машине заливки простояли двое суток, Google
    завершил в них сессию, и файл без номера пропал вместе с окном. Обычный возврат такому
    выпуску запрещён («загрузка начиналась»), а подтвердить руками нечем — ролика нет.
    Кнопка одиночная, на странице выпуска: человек закрыл окно и видел, что на канале
    ролика нет. Выпуск с номером ролика и выпуск, чьё задание держит машина, сюда не
    пускаются (`pub_engine.lost_window_refusal`).
    """
    c = db()
    try:
        out = pub_engine_revive(c, release_id, lost_window=True)
        if not out["ok"]:
            raise HTTPException(409, out["why"])
        c.commit()
    finally:
        c.close()
    return out


# ─────────────── обложка выпуска глазами человека (04.10.2026) ───────────────
'''

# ─────────────────────────── web/index.html ───────────────────────────

H_OLD_1 = """      && !r.hand_confirmed && ['upload_pending', 'upload_running'].includes(r.phase))
    acts.push(`<button class="ghost" id="byhand">Подтвердить руками</button>`);
  return `<div class="blk ${esc(b.kind)}" id="blk">"""
H_NEW_1 = """      && !r.hand_confirmed && ['upload_pending', 'upload_running'].includes(r.phase))
    acts.push(`<button class="ghost" id="byhand">Подтвердить руками</button>`);
  // Окно заливки потеряно (17.10.2026): загрузка начиналась, но номера ролика нет, а
  // окно на машине закрыто — ролика на канале нет и не будет. Единственный выход —
  // залить заново; заливщик перед загрузкой сверит контент канала по названию.
  if (r.status === 'blocked' && !r.hand_confirmed && r.phase === 'upload_running' && !r.video_id)
    acts.push(`<button class="ghost" id="lostwin">Окно потеряно — залить заново</button>`);
  return `<div class="blk ${esc(b.kind)}" id="blk">"""

H_OLD_2 = """      await post(`/ui/releases/${encodeURIComponent(r.release_id)}/revive`);
      await render();
    }catch(e){ rev.disabled = false; toast(e.message); }
  });
"""
H_NEW_2 = """      await post(`/ui/releases/${encodeURIComponent(r.release_id)}/revive`);
      await render();
    }catch(e){ rev.disabled = false; toast(e.message); }
  });

  const lostwin = head.querySelector('#lostwin');
  if (lostwin) lostwin.onclick = () => confirmInline(lostwin,
      'Окно на машине закрыто и ролика на канале нет? Комплект соберётся заново и файл '
      + 'зальётся ещё раз; перед загрузкой заливщик сверит контент канала по названию.',
      async () => {
    lostwin.disabled = true;
    try{
      await post(`/ui/releases/${encodeURIComponent(r.release_id)}/lost_window`);
      await render();
    }catch(e){ lostwin.disabled = false; toast(e.message); }
  });
"""

# ─────────────────────────── tests/test_lost_window.py ───────────────────────────

TEST_SRC = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Окно потеряно — залить заново» (17.10.2026).

Выпуск, у которого загрузка начиналась, но номера ролика нет, а окно заливки на машине
закрыто: обычный возврат запрещён, подтвердить руками нечем. Проверяем в обе стороны —
что действие работает там, где должно, и НЕ работает там, где за ним второй ролик.

    .venv/bin/python tests/test_lost_window.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.abspath(os.path.join(HERE, "..")), "server"))

import pub_engine                                                  # noqa: E402
import pub_schema                                                  # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\\n        ожидалось: {want!r}\\n        получено:  {got!r}")
        FAILS.append(label)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    return c


def release(c, n, *, job_status="repair", job_video=None, claim="tok"):
    uc = "UC" + str(n) * 22
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) VALUES('t1',?,?,'ru','Europe/Warsaw','{}',1,0,?,?)",
              (uc, f"Канал {n}", NOW, NOW))
    ch = c.execute("SELECT id FROM pub_channels WHERE uc=?", (uc,)).fetchone()[0]
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_video_id,donor_title,slot_at,status,created,updated) "
              "VALUES(?,'t1',?,1,?,?,'Донор',?,'error',?,?)",
              (n, ch, f"https://youtu.be/donor{n}aaaaa", f"donor{n}aaaaa", NOW + 3600, NOW, NOW))
    rid = f"rel-{n}"
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,blocked_reason,created,updated) "
              "VALUES(?,'t1',?,1,?,?,?,?,'blocked','upload_running',"
              "'UPLOAD_IN_PROGRESS_PROFILE_RETAINED: …',?,?)",
              (rid, n, ch, uc, NOW + 3600, f"p{n}", NOW, NOW))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
              "title,video_path,slot_at,status,phase,video_id,claim_token,created,updated) "
              "VALUES(?,'t1',?,?,'http://c','Ролик','/v.mp4',?,?,?,?,?,?,?)",
              (rid, ch, uc, NOW + 3600, job_status,
               json.dumps({"upload_phase_schema_version": 1, "phase": "upload_started", "video_id": job_video or ""}),
               job_video, claim, NOW, NOW))
    for seq, ph in enumerate(pub_engine.PHASES[:pub_engine.PHASE_INDEX["upload_running"] + 1], start=1):
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,created,repeats,last_seen) "
                  "VALUES(?,?,?,?,1,?)", (rid, seq, ph, NOW - 3600, NOW - 3600))
    c.commit()
    return rid


def rel(c, rid):
    return c.execute("SELECT * FROM pub_releases WHERE release_id=?", (rid,)).fetchone()


def main() -> int:
    print("Окно потеряно: загрузка начиналась, номера нет, задание в repair")
    c = fresh()
    rid = release(c, 1)
    check("до кнопки выпуск необратим", pub_engine.reached_irreversible(c, rid), True)
    check("обычный возврат отказывает", pub_engine.revive(c, rid, NOW)["ok"], False)
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("«окно потеряно» принято", out["ok"], True)
    r = rel(c, rid)
    check("выпуск снова в работе", r["status"], "pending")
    check("с фазы «обложка готова» — комплект соберётся заново", r["phase"], "cover_ready")
    check("старое задание убрано", c.execute("SELECT COUNT(*) FROM pub_upload_jobs WHERE release_id=?", (rid,)).fetchone()[0], 0)
    check("после отметки выпуск больше не необратим", pub_engine.reached_irreversible(c, rid), False)
    hist = pub_engine.phase_history(c, rid)
    check("отметка lost_window в истории фаз есть", "lost_window" in hist, True)
    msgs = " | ".join(m[0] for m in c.execute("SELECT message FROM pub_release_log WHERE release_id=?", (rid,)))
    check("в журнале сказано, что человек разрешил новую заливку", "разрешил новую заливку" in msgs, True)

    print("\\nЗащиты: где за кнопкой второй ролик, она не работает")
    c = fresh()
    rid = release(c, 2, job_video="Abc-123_xyz")
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("есть номер ролика — отказ", out["ok"], False)
    check("и сказано идти в «подтвердить руками»", "Подтвердить руками" in out["why"], True)
    check("выпуск не тронут", rel(c, rid)["status"], "blocked")

    c = fresh()
    rid = release(c, 3, job_status="running")
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("машина держит задание — отказ", out["ok"], False)
    check("выпуск не тронут", rel(c, rid)["status"], "blocked")

    c = fresh()
    rid = release(c, 4)
    c.execute("UPDATE pub_releases SET hand_confirmed=? WHERE release_id=?", (NOW, rid)); c.commit()
    out = pub_engine.revive(c, rid, NOW, lost_window=True)
    check("подтверждённый руками — отказ", out["ok"], False)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def main():
    engine = open(ENGINE, encoding="utf-8").read()
    if "lost_window_refusal" in engine:
        print("pub_engine.py: уже применено")
    else:
        engine = replace_once(engine, E_OLD_1, E_NEW_1, "pub_engine reached_irreversible")
        engine = replace_once(engine, E_OLD_2, E_NEW_2, "pub_engine revive signature")
        engine = replace_once(engine, E_OLD_3, E_NEW_3, "pub_engine revive guard")
        engine = replace_once(engine, E_OLD_4, E_NEW_4, "pub_engine revive annul")
        open(ENGINE, "w", encoding="utf-8").write(engine)
        print("pub_engine.py: готово")
    app = open(APP, encoding="utf-8").read()
    if "/lost_window" in app:
        print("web/app.py: уже применено")
    else:
        app = replace_once(app, A_OLD, A_NEW, "web/app.py route")
        open(APP, "w", encoding="utf-8").write(app)
        print("web/app.py: готово")
    html = open(HTML, encoding="utf-8").read()
    if "lostwin" in html:
        print("web/index.html: уже применено")
    else:
        html = replace_once(html, H_OLD_1, H_NEW_1, "index.html button")
        html = replace_once(html, H_OLD_2, H_NEW_2, "index.html handler")
        open(HTML, "w", encoding="utf-8").write(html)
        print("web/index.html: готово")
    if not os.path.exists(TEST):
        open(TEST, "w", encoding="utf-8").write(TEST_SRC)
        print("tests/test_lost_window.py: создан")
    import py_compile
    py_compile.compile(ENGINE, doraise=True)
    py_compile.compile(APP, doraise=True)
    py_compile.compile(TEST, doraise=True)
    print("синтаксис в порядке; дальше: cd studio-platform && VS_ALERTS=0 .venv/bin/python tests/test_lost_window.py")


if __name__ == "__main__":
    sys.exit(main())
