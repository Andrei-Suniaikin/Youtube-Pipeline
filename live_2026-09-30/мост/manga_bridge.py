# -*- coding: utf-8 -*-
"""Мост манхвы в кабинет: готовый ролик → задание на заливку.

Зачем. Манхву делает своя служба (manga_rewrite), кабинет о ней ничего не знал, и заливка
запускалась руками с Минска. Из-за этого: ролики копились без заливки (на 25.09 готовых 64,
залит 21), профиль 21 делили вслепую с ВВ2, а вход в аккаунт в ручном скрипте был выключен.

Что делает мост. Ровно то же, что кабинет делает для ВВ2, но без производства: берёт уже
готовый mp4 манхвы и кладёт задание в общую очередь заливки. Дальше работает обычный путь —
бронь профиля, заливщик с автовходом, вердикт по состоянию строки.

Как устроено. На каждый ролик три строки:
  pub_plan_items  — сразу frozen, иначе движок начнёт производить ролик заново;
  pub_releases    — сразу в фазе upload_pending: производственные фазы пропускаются,
                    do_upload_pending увидит готовое задание и просто двинет фазу дальше;
  pub_upload_jobs — само задание с путями к файлам манхвы (файлы остаются на VPS,
                    Минск качает их через /api/upload/{release_id}/file).

Повтор исключён видом release_id: он считается от id проекта манхвы, а поле уникально.

Запуск:  python3 manga_bridge.py            — только показать
         python3 manga_bridge.py --apply    — поставить задания
         --limit N                          — не больше N роликов за прогон
"""
import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, "/root/VideoStudio/server")
import pub_publish                                                   # noqa: E402

DB = "/root/VideoStudio/server/vsgen.db"
PROJECTS = "/root/manga_rewrite/projects_web/projects"
SEEN = "/root/ops_bridge/manga_seen.json"
KEYS = os.path.expanduser("~/video_studio_api_keys.json")

# Сколько запланированных роликов держать на канале. Столько же стоит в deferred_limit
# самих каналов: смысл один — не выкладывать месяц вперёд с одного захода.
CHANNEL_QUEUE_LIMIT = 5

# Имя канала в project.json → UC. Своего справочника у манхвы нет, имена в её проектах
# писались руками и в кабинете могут быть записаны иначе, поэтому связь идёт по UC.
NAME_TO_UC = {
    "Хару Манхва": "UCVREAS1oT4aFf6BT-ynTWFg",
    "Кофе и Манхва": "UC-65TlNg-KKVRerxznVXTxw",
    "Реванш Манхва": "UCrx3aBHIaQLj9gqHVlCPbig",
    "S-Ранг Манхва": "UCOVvbQ7WpDjkSJRauTDTqaQ",
    "La Rivincita Manhwa": "UCHTVsWoaACKlafEY48WJeHw",
    "Il Risveglio Manhwa": "UCqFMoZJCKj6YTaL83mbu-fg",
    "Cuori di Manhwa": "UCTH4S4cOW03YVg56m5xG4Hw",
    "Amor en Viñetas": "UCjKcIgA61QZHHGfXf_DkMbQ",
    "Renacer Manhwa": "UCAR9BruIUfXwB230MR1kZPQ",
    "Mazmorra Cero": "UC-Btib4_4i4HQMXERjNM2zg",
    "Manhwa a Dois": "UCE0xGgS6YjxQMBfsiGCr0MA",
    "Rank S Narrado": "UCznnds10NhwEMe3W9ZIXnkQ",
    "Drugie Życie Manhwy": "UCafmKg_VBZA0EBBRbVZ9G5A",
    "Dragoste în Manhwa": "UCfxG2eB-GJkBW1cRsyNaJiw",
    "Brama Potworów": "UCrKKIu7pcDH8NwqMzKuksQQ",
    "Manhwa o Lásce": "UCKvI6_TOAzo_zMojM4an4fg",
    "Návrat Nejsilnějšího": "UCDsbUuGOsWpKyT44hZ9Vukw",
    "Randka z Manhwą": "UCmSvl9Zb4iFqdvPMD-0U2Cw",
    "Renăscut Mai Puternic": "UC4dHbJ9cbajBD6FnKKACs4g",
    "Volta por Cima Manhwa": "UCx061eEgBSogxrmWxQwtIIA",
}


def title_llm() -> dict:
    """Чем писать название — тот же прокси, что у движка кабинета (см. main._pub_title_llm)."""
    try:
        k = json.load(open(KEYS))
    except Exception:
        return {}
    key = k.get("alt_llm") or ""
    if isinstance(key, list):
        key = key[0] if key else ""
    return {"base": str(k.get("alt_llm_base") or ""), "key": str(key),
            "model": str(k.get("pub_title_model") or "")}


def ready_projects() -> list:
    """Готовые ролики манхвы: (pid, паспорт проекта, путь к mp4, путь к обложке)."""
    out = []
    for video in glob.glob(PROJECTS + "/*/out/video.mp4"):
        pid = video.split("/")[-3]
        try:
            meta = json.load(open(os.path.join(PROJECTS, pid, "project.json")))
        except Exception:
            continue
        cover = os.path.join(PROJECTS, pid, "out", "cover.png")
        out.append((pid, meta, video, cover if os.path.exists(cover) else ""))
    out.sort(key=lambda r: os.path.getmtime(r[2]))
    return out


def donor_url(pid: str, meta: dict) -> str:
    """Ссылка на донора — для строки плана. Нет паспорта обложки: хватит и id проекта."""
    try:
        cover = json.load(open(os.path.join(PROJECTS, pid, "out", "cover.json")))
        url = ((cover.get("donor") or {}).get("url") or "").strip()
        if url:
            return url
    except Exception:
        pass
    return "manga:" + pid


def next_slot(c, ch, now: float) -> float:
    """Ближайший свободный выход канала: 10:00 по его поясу, один ролик в день.

    Занятым считается день, на который у канала уже стоит выпуск. Иначе два ролика
    получили бы одно время, и YouTube выложил бы их подряд.
    """
    tz = ZoneInfo(ch["tz"] or "Europe/Warsaw")
    busy = set()
    for row in c.execute("SELECT slot_at FROM pub_releases WHERE channel_id=? "
                         "AND status NOT IN ('cancelled','error')", (ch["id"],)):
        busy.add(dt.datetime.fromtimestamp(row[0], tz).date())
    hour, minute = 10, 0
    try:
        times = (json.loads(ch["schedule"] or "{}") or {}).get("*") or []
        if times:
            hour, minute = (int(x) for x in str(times[0]).split(":")[:2])
    except Exception:
        pass
    day = dt.datetime.fromtimestamp(now, tz).date() + dt.timedelta(days=1)
    for _ in range(90):
        if day not in busy:
            moment = dt.datetime.combine(day, dt.time(hour, minute), tz)
            if moment.timestamp() > now + 3600:
                return moment.timestamp()
        day += dt.timedelta(days=1)
    return 0.0


def queued_ahead(c, channel_id: int, now: float) -> int:
    """Сколько роликов канала уже ждут своего выхода — чтобы не набивать месяц вперёд."""
    return c.execute(
        "SELECT COUNT(*) FROM pub_releases WHERE channel_id=? AND slot_at > ? "
        "AND status NOT IN ('cancelled','error','done')", (channel_id, now)).fetchone()[0]


def revive(c, release_id: str, now: float) -> bool:
    """Оживить выпуск, который встал, хотя ролик в YouTube не уходил.

    Заливка срывается и по причинам, которые к ролику отношения не имеют: залипший
    AdsPower, мёртвый браузер, обрыв сети. Попытки исчерпываются, выпуск встаёт — и
    готовый ролик манхвы не поедет уже никогда: номер выпуска занят, и мост обойдёт его
    как «уже в кабинете». Поэтому такой выпуск возвращаем в очередь.

    Возвращаем ТОЛЬКО когда обе улики согласны, что файла в YouTube нет: пустой номер
    ролика и у выпуска, и у задания. Если файл уходил, повтор дал бы второй ролик.
    """
    rel = c.execute("SELECT * FROM pub_releases WHERE release_id=?", (release_id,)).fetchone()
    job = c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?", (release_id,)).fetchone()
    if rel is None or job is None:
        return False
    if (rel["status"] or "") != "blocked":
        return False
    if (rel["video_id"] or "") or (job["video_id"] or ""):
        return False
    if (job["verdict"] or "") in ("scheduled", "published"):
        return False
    said = str(job["phase"] or "")
    if "upload_started" in said or "video_id_known" in said:
        return False
    if not os.path.exists(job["video_path"] or ""):
        return False
    c.execute("UPDATE pub_upload_jobs SET status='queued', verdict=NULL, error=NULL, phase=NULL,"
              " claim_token=NULL, host=NULL, worker=NULL, claimed=NULL, lease_until=NULL,"
              " heartbeat=NULL, evidence=NULL, finished=NULL, updated=? WHERE release_id=?",
              (now, release_id))
    c.execute("UPDATE pub_releases SET status='running', phase='upload_pending',"
              " blocked_reason=NULL, deadline=?, updated=? WHERE release_id=?",
              (now + 7200, now, release_id))
    return True


def put(c, ch, pid, meta, video, cover, llm, now) -> str:
    """Три строки: план (замороженный), выпуск сразу на заливке, задание. Возвращает название."""
    release_id = "m_" + pid[:14]
    slot = next_slot(c, ch, now)
    if not slot:
        raise RuntimeError("свободного выхода у канала нет на 90 дней вперёд")
    title = pub_publish.make_title(meta.get("title") or "", ch["lang"] or "en", llm)
    description = pub_publish.make_description(title)
    schedule = pub_publish.schedule_iso(slot, ch["tz"])

    c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,version,donor_url,position,"
              "slot_mode,overrides,status,frozen,created,updated,slot_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (ch["tenant_id"], ch["id"], 1, donor_url(pid, meta), slot, "auto",
               json.dumps({"источник": "манхва", "проект": pid}, ensure_ascii=False),
               "frozen", 1, now, now, slot))
    item_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,deadline,created,updated) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (release_id, ch["tenant_id"], item_id, 1, ch["id"], ch["uc"], slot, "",
               "running", "upload_pending", now + 7200, now, now))

    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
              "host_id,channel_url,title,description,schedule_iso,visibility,collaborator,"
              "video_path,cover_path,slot_at,created,updated) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (release_id, ch["tenant_id"], ch["id"], ch["uc"], ch["profile_id"],
               ch["host_id"] or "", "https://studio.youtube.com/channel/%s/" % ch["uc"],
               title, description, schedule, "public", ch["collaborator"] or "",
               video, cover, slot, now, now))
    return "%s   выход %s" % (title, schedule or "сразу")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    seen = {}
    if os.path.exists(SEEN):
        seen = json.load(open(SEEN))
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    now = time.time()
    llm = title_llm()
    if not llm.get("base") or not llm.get("key"):
        print("нет настроек прокси названий — названия писать нечем"); sys.exit(2)

    made = 0
    counts = {"залит раньше": 0, "помечен не перезаливать": 0,
              "уже в кабинете": 0, "канал на паузе": 0,
              "канала нет в кабинете": 0, "очередь канала полна": 0}
    for pid, meta, video, cover in ready_projects():
        if args.limit and made >= args.limit:
            break
        was = seen.get(pid) or {}
        if was.get("status") == "ok":
            counts["залит раньше"] += 1; continue
        # «не перезаливать» ставил прежний заливщик, когда файл в YouTube уже ушёл:
        # ролик лежит там черновиком или отбит навсегда. Повтор дал бы второй ролик
        # на канале. Разбирается такое только руками — мост их не трогает.
        if was.get("no_retry"):
            counts["помечен не перезаливать"] += 1; continue
        release_id = "m_" + pid[:14]
        if c.execute("SELECT 1 FROM pub_releases WHERE release_id=?",
                     (release_id,)).fetchone():
            if args.apply and revive(c, release_id, now):
                c.commit()
                print("  вернул в очередь: %-20s %s (срывалось не по вине ролика)"
                      % (meta.get("channel_name"), pid[:8]))
                made += 1
            else:
                counts["уже в кабинете"] += 1
            continue
        uc = NAME_TO_UC.get(meta.get("channel_name") or "")
        ch = c.execute("SELECT * FROM pub_channels WHERE uc=?", (uc,)).fetchone() if uc else None
        if ch is None:
            counts["канала нет в кабинете"] += 1; continue
        if (ch["status"] or "active") != "active":
            counts["канал на паузе"] += 1; continue
        if queued_ahead(c, ch["id"], now) >= CHANNEL_QUEUE_LIMIT:
            counts["очередь канала полна"] += 1; continue

        if not args.apply:
            print("  поставил бы: %-22s %s" % (meta.get("channel_name"), pid[:8]))
            made += 1
            continue
        try:
            line = put(c, ch, pid, meta, video, cover, llm, now)
            c.commit()
        except Exception as exc:
            c.rollback()
            print("  НЕ СМОГ: %s %s — %s" % (meta.get("channel_name"), pid[:8], str(exc)[:160]))
            continue
        print("  поставил: %-22s %s" % (meta.get("channel_name"), line))
        made += 1

    print("\n%s: %d" % ("поставлено" if args.apply else "к постановке", made))
    for k, v in counts.items():
        if v:
            print("   %s: %d" % (k, v))
    c.close()


if __name__ == "__main__":
    main()
