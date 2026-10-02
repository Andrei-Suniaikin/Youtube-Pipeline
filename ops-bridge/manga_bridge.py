# -*- coding: utf-8 -*-
"""Мост манхвы в кабинет: ПРИНЯТЫЙ ролик → задание на заливку.

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

Что изменилось 01.10.2026. Прежде мост брал в работу ЛЮБОЙ существующий mp4: что нашлось
по `projects/*/out/video.mp4`, то и уехало. Теперь перед очередью стоят ворота приёмки
(`manga_gate.py`): прошла ли сборка, есть ли название, есть ли обложка там, где она
обязательна, тот ли это канал, разрешено ли на него лить. Все ворота закрываются ДО
обращения к платной модели названий — прежде единственным, что держало брак, был её
отказ 402, то есть пустой счёт. При допуске запоминается паспорт принятой версии; если
ролик потом пересобрали, мост останавливает выпуск, а не делает вид, что всё в порядке.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/VideoStudio/server")
import manga_gate as gate                                            # noqa: E402
import manga_title                                                   # noqa: E402
import pub_publish                                                   # noqa: E402

DB = "/root/VideoStudio/server/vsgen.db"
PROJECTS = "/root/manga_rewrite/projects_web/projects"
SEEN = "/root/ops_bridge/manga_seen.json"
# Паспорта принятых версий и настройки ворот — рядом с мостом, не в базе кабинета:
# база чужая, а эти две вещи нужны одному мосту (почему именно так — в manga_gate.py).
PASSPORTS = "/root/ops_bridge/manga_passports.json"
SETTINGS = "/root/ops_bridge/manga_gate.json"
KEYS = os.path.expanduser("~/video_studio_api_keys.json")

# Сколько запланированных роликов держать на канале. Столько же стоит в deferred_limit
# самих каналов: смысл один — не выкладывать месяц вперёд с одного захода.
CHANNEL_QUEUE_LIMIT = 5

# Имя канала в project.json → UC. Запасная связь: сначала мост ищет канал по UC из
# паспорта проекта, потом по хендлу, и только потом по имени через эту таблицу
# (manga_gate.resolve_channel). Имена в проектах манхвы писались руками, поэтому имя —
# подпись, а не идентификатор: совпало больше одного канала — отказ, а не первый из них.
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
        with open(KEYS, encoding="utf-8") as f:
            k = json.load(f)
    except Exception:
        return {}
    key = k.get("alt_llm") or ""
    if isinstance(key, list):
        key = key[0] if key else ""
    return {"base": str(k.get("alt_llm_base") or ""), "key": str(key),
            "model": str(k.get("pub_title_model") or "")}


def ready_projects() -> list:
    """КАНДИДАТЫ на заливку: (pid, паспорт проекта, путь к mp4, путь к обложке).

    Здесь только «файл на диске есть и паспорт проекта читается». Приёмку это не
    означает: прошла ли сборка, есть ли название и тот ли канал — решают ворота
    (manga_gate.admit). Раньше этот список и был допуском, из-за чего в очередь
    уезжало всё подряд.

    Паспорт, который не читается, приходит как None: что с ним делать, решает main —
    отказом словами, а не пропуском. Пропуск здесь неотличим от «ролика ещё нет».
    """
    out = []
    for video in glob.glob(PROJECTS + "/*/out/video.mp4"):
        pid = video.split("/")[-3]
        # Кодировка названа явно. С locale C (а cron ходит без LANG) json.load(open(...))
        # читал бы файл как ascii и падал на любом названии по-русски — а падение здесь
        # раньше означало «такого ролика нет вовсе»: ролик тихо исчезал из прогона.
        try:
            with open(os.path.join(PROJECTS, pid, "project.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError, UnicodeDecodeError):
            meta = None          # паспорт не читается — это отказ, а не «ролика нет»
        cover = os.path.join(PROJECTS, pid, "out", "cover.png")
        out.append((pid, meta, video, cover if os.path.exists(cover) else ""))
    out.sort(key=lambda r: os.path.getmtime(r[2]))
    return out


def donor_url(pid: str, meta: dict) -> str:
    """Ссылка на донора — для строки плана. Нет паспорта обложки: хватит и id проекта."""
    try:
        with open(os.path.join(PROJECTS, pid, "out", "cover.json"), encoding="utf-8") as f:
            cover = json.load(f)
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


def pair(c, release_id: str):
    """Выпуск и его задание одной парой: судьбу выпуска решают только оба вместе."""
    rel = c.execute("SELECT * FROM pub_releases WHERE release_id=?",
                    (release_id,)).fetchone()
    job = c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?",
                    (release_id,)).fetchone()
    return rel, job


def sent_already(rel, job) -> bool:
    """Уходил ли файл этого выпуска в YouTube. Да — повтор дал бы второй ролик.

    Улик четыре, и верим любой: номер ролика у выпуска, номер у задания, вердикт
    заливщика и его последняя фаза. Пустота — «не уходил», но только когда пусты все.
    """
    if (rel["video_id"] or "") or (job["video_id"] or ""):
        return True
    if (job["verdict"] or "") in ("scheduled", "published"):
        return True
    said = str(job["phase"] or "")
    return "upload_started" in said or "video_id_known" in said


def revive(c, release_id: str, now: float, saved, fresh) -> bool:
    """Оживить выпуск, который встал, хотя ролик в YouTube не уходил.

    Заливка срывается и по причинам, которые к ролику отношения не имеют: залипший
    AdsPower, мёртвый браузер, обрыв сети. Попытки исчерпываются, выпуск встаёт — и
    готовый ролик манхвы не поедет уже никогда: номер выпуска занят, и мост обойдёт его
    как «уже в кабинете». Поэтому такой выпуск возвращаем в очередь.

    Возвращаем ТОЛЬКО когда обе улики согласны, что файла в YouTube нет: пустой номер
    ролика и у выпуска, и у задания. Если файл уходил, повтор дал бы второй ролик.

    И только когда на диске лежит ТА САМАЯ принятая версия. Иначе оживление тихо
    заливало бы ролик, которого никто не принимал: выпуск, остановленный из-за
    пересборки, стоит ровно в том же blocked, что и выпуск, которому не повезло
    с браузером. Сверка здесь дублирует сверку в main намеренно — порядок проверок
    в main никто не обязан держать в голове вечно.
    """
    rel, job = pair(c, release_id)
    if rel is None or job is None:
        return False
    if (rel["status"] or "") != "blocked":
        return False
    if sent_already(rel, job):
        return False
    if not os.path.exists(job["video_path"] or ""):
        return False
    if gate.mismatch(saved, fresh):
        return False
    c.execute("UPDATE pub_upload_jobs SET status='queued', verdict=NULL,"
              " error=NULL, phase=NULL,"
              " claim_token=NULL, host=NULL, worker=NULL, claimed=NULL, lease_until=NULL,"
              " heartbeat=NULL, evidence=NULL, finished=NULL, updated=? WHERE release_id=?",
              (now, release_id))
    c.execute("UPDATE pub_releases SET status='running', phase='upload_pending',"
              " blocked_reason=NULL, deadline=?, updated=? WHERE release_id=?",
              (now + 7200, now, release_id))
    return True


def hold(c, release_id: str, now: float, why: str) -> str:
    """Остановить выпуск, ролик которого после приёмки стал другим. Строка человеку.

    Тихо пропустить нельзя: задание уже лежит в очереди, и нода зальёт то, что найдёт
    по пути, — а найдёт она версию, которую никто не принимал. Создать второе задание
    тоже нельзя: это второй ролик на канале. Поэтому выпуск уводится в разбор тем же
    способом, каким это делает движок кабинета (pub_engine.quarantine): ещё не взятое
    задание гасится, выпуск встаёт в blocked с причиной словами, дальше решает человек.

    Почему мост не перепринимает ролик сам: название, описание и время выхода делались
    для той версии, которую приняли, а новая приёмка — это ещё один платный вызов
    модели на каждый круг пересборки. Пересборка готового ролика — событие редкое и
    обычно чья-то ошибка; её должен увидеть человек, а не замести мост.
    """
    rel, job = pair(c, release_id)
    if rel is None or job is None:
        return why + ". Выпуск или задание в кабинете не найдены — останавливать нечего"
    if sent_already(rel, job):
        return (why + ". Файл в YouTube уже уходил: отзывать нечего, а повторять нельзя — "
                "вышел бы второй ролик. Разбирать руками")
    if (job["status"] or "") == "running":
        return (why + ". Задание уже забрала машина %s: файл качается или заливается "
                "прямо сейчас, остановить можно только на машине"
                % (job["host"] or "неизвестно"))
    c.execute("UPDATE pub_upload_jobs SET status='cancelled', error=?, updated=? "
              "WHERE release_id=? AND status='queued'", (why[:500], now, release_id))
    c.execute("UPDATE pub_releases SET status='blocked', blocked_reason=?, updated=? "
              "WHERE release_id=? AND status IN ('pending','running')",
              (why[:500], now, release_id))
    return why + ". Задание снято до выдачи, выпуск убран в разбор: нужна новая приёмка"


def put(c, ch, pid, meta, video, cover, llm, now) -> str:
    """Три строки: план (замороженный), выпуск сразу на заливке, задание.

    Возвращает готовую строку с названием и временем выхода — для печати человеку.
    """
    release_id = "m_" + pid[:14]
    slot = next_slot(c, ch, now)
    if not slot:
        raise RuntimeError("свободного выхода у канала нет на 90 дней вперёд")
    # Заголовок делает Кодекс подписки, если он настроен, иначе прокси — как раньше
    # (разрешение заказчика 02.10.2026, после того как ключ прокси встал на лимите).
    title = manga_title.make_title(meta.get("title") or "", ch["lang"] or "en", llm)
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

    # Видимость остаётся public: «режим публикации разрешён» из ТЗ — это разрешение на
    # КАНАЛ (его проверяет manga_gate.channel_allowed), а не видимость самого ролика.
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
              "host_id,channel_url,title,description,schedule_iso,visibility,collaborator,"
              "video_path,cover_path,slot_at,created,updated) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (release_id, ch["tenant_id"], ch["id"], ch["uc"], ch["profile_id"],
               ch["host_id"] or "", "https://studio.youtube.com/channel/%s/" % ch["uc"],
               title, description, schedule, "public", ch["collaborator"] or "",
               video, cover, slot, now, now))
    return "%s   выход %s" % (title, schedule or "сразу")


def report(counts: dict, refused: dict, made: int, applied: bool) -> None:
    """Итог прогона. Отказы — списком причин со счётчиком: человеку нужна причина."""
    print("\n%s: %d" % ("поставлено" if applied else "к постановке", made))
    for k, v in counts.items():
        if v:
            print("   %s: %d" % (k, v))
    if refused:
        print("   не допущено: %d" % sum(len(v) for v in refused.values()))
        for why, pids in sorted(refused.items(), key=lambda kv: -len(kv[1])):
            print("      %d × %s" % (len(pids), why))
            print("         роликов: " + ", ".join(p[:8] for p in pids[:6])
                  + (" и ещё %d" % (len(pids) - 6) if len(pids) > 6 else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    seen = {}
    if os.path.exists(SEEN):
        with open(SEEN, encoding="utf-8") as f:
            seen = json.load(f)
    try:
        settings = gate.load_settings(SETTINGS)
        passports = gate.load_store(PASSPORTS)
    except gate.GateError as exc:
        # Битые настройки не читаются как пустые: пустые выключили бы ворота обложки,
        # и опечатка в json тихо вернула бы мост к поведению до 01.10.2026.
        print("мост остановлен: %s" % exc)
        print("почините файл и запустите снова — в очередь ничего не добавлено")
        sys.exit(2)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    now = time.time()
    llm = title_llm()
    if not manga_title.ready() and (not llm.get("base") or not llm.get("key")):
        print("названия писать нечем: Кодекс не настроен и прокси без ключа"); sys.exit(2)

    made = legacy = 0
    counts = {"залит раньше": 0, "залит раньше, версия неизвестна": 0,
              "помечен не перезаливать": 0, "уже в кабинете": 0,
              "пересобран после приёмки": 0, "очередь канала полна": 0,
              # Выпуски прежней версии моста: паспорта нет ни у одного. Отдельный
              # счётчик, а не «пересобран»: это разные ответы, и выдавать незнание за
              # поломку нельзя.
              "версия не записана — на разбор": 0}
    refused = {}
    for pid, meta, video, cover in ready_projects():
        if args.limit and made >= args.limit:
            break
        if meta is None:
            refused.setdefault("паспорт проекта (project.json) не читается: починить "
                               "файл или убрать папку ролика", []).append(pid)
            continue
        name = meta.get("channel_name") or "канал не указан"
        was = seen.get(pid) or {}
        if was.get("status") == "ok":
            # Решение владельца, пункт 7: 32 ролика залиты прежней версией моста, и
            # какую именно версию файла тогда приняли, не записывал никто. Пустота не
            # должна читаться как «проверено», поэтому она помечается словами.
            if pid not in passports:
                passports[pid] = gate.unknown_version(now)
                legacy += 1
            counts["залит раньше" if gate.is_accepted(passports[pid])
                   else "залит раньше, версия неизвестна"] += 1
            continue
        # «не перезаливать» ставил прежний заливщик, когда файл в YouTube уже ушёл:
        # ролик лежит там черновиком или отбит навсегда. Повтор дал бы второй ролик
        # на канале. Разбирается такое только руками — мост их не трогает.
        if was.get("no_retry"):
            counts["помечен не перезаливать"] += 1; continue
        release_id = "m_" + pid[:14]
        folder = os.path.join(PROJECTS, pid)
        # Паспорт нынешнего состояния ролика: он же идёт и в ворота, и в сверку.
        fresh = gate.passport(folder, video, cover)
        if c.execute("SELECT 1 FROM pub_releases WHERE release_id=?",
                     (release_id,)).fetchone():
            # Повтор допуска второй работы не создаёт: release_id считается от id
            # проекта манхвы, а поле уникально. Но ролик на диске мог за это время
            # стать другим — это проверяется ДО всякой мысли об оживлении.
            why = gate.mismatch(passports.get(pid), fresh)
            # «Версия не записана» — не «пересобран», и в карантин за это нельзя
            # (разбор 01.10.2026). Выпуски прежней версии моста паспорта не имеют все
            # до одного; объявив их пересобранными, мы разом остановили бы работающую
            # очередь и приписали бы роликам то, чего не проверяли. Говорим как есть:
            # подтвердить нечем, нужен человек. Возврат в очередь при этом не
            # произойдёт — `revive` ту же причину считает достаточной, чтобы не трогать.
            if why == gate.НЕ_ЗНАЕМ:
                counts["версия не записана — на разбор"] += 1
                print("  НЕ ПОДТВЕРДИТЬ: %-17s %s — %s" % (name, pid[:8], why))
                continue
            if why:
                counts["пересобран после приёмки"] += 1
                if args.apply:
                    said = hold(c, release_id, now, why)
                    c.commit()
                    print("  ОСТАНОВИЛ: %-20s %s — %s" % (name, pid[:8], said))
                else:
                    print("  остановил бы: %-17s %s — %s" % (name, pid[:8], why))
                continue
            if args.apply and revive(c, release_id, now, passports.get(pid), fresh):
                c.commit()
                print("  вернул в очередь: %-20s %s (срывалось не по вине ролика)"
                      % (name, pid[:8]))
                made += 1
            else:
                counts["уже в кабинете"] += 1
            continue

        # Ворота приёмки. Все до единой — раньше make_title: платный вызов на
        # непринятый ролик сжигает деньги и ничего не ловит (решение владельца, п. 1).
        ch, why = gate.admit(c, meta, fresh, settings, NAME_TO_UC)
        if why:
            refused.setdefault(why, []).append(pid)
            continue
        if queued_ahead(c, ch["id"], now) >= CHANNEL_QUEUE_LIMIT:
            counts["очередь канала полна"] += 1; continue

        if not args.apply:
            print("  поставил бы: %-22s %s" % (name, pid[:8]))
            made += 1
            continue
        try:
            # Паспорт пишется РАНЬШЕ задания. Умрём между ними — останется паспорт без
            # задания, и следующий прогон просто примет ролик заново, перезаписав его.
            # Обратный порядок давал бы задание без паспорта: версию такого ролика
            # подтвердить уже нечем, а это и есть то, что мы здесь чиним.
            passports[pid] = gate.taken(release_id, ch["uc"], fresh, now)
            gate.save_store(PASSPORTS, passports)
            line = put(c, ch, pid, meta, video, cover, llm, now)
            c.commit()
        except Exception as exc:
            c.rollback()
            print("  НЕ СМОГ: %s %s — %s" % (name, pid[:8], str(exc)[:160]))
            continue
        print("  поставил: %-22s %s" % (name, line))
        made += 1

    if args.apply and legacy:
        gate.save_store(PASSPORTS, passports)
        print("  пометил «версия неизвестна»: %d (залиты прежней версией моста)" % legacy)
    report(counts, refused, made, args.apply)
    c.close()


if __name__ == "__main__":
    main()
