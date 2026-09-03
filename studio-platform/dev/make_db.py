#!/usr/bin/env python3
"""Локальная база кабинета из боевого DDL + синтетические справочники.

Схема берётся дословно из запечатанного пакета — никакой выдумки. Данных в пакете нет
(во всех пяти DDL ноль INSERT), поэтому справочники и один выпуск засеваются здесь,
чтобы экран выпуска было на чём проверять без доступа к бою.

    python3 dev/make_db.py [путь_к_базе]     # по умолчанию dev/vsgen.dev.db
"""
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SCHEMA = os.path.join(ROOT, "Studio_Full_Handoff_2026-08-31",
                     "contracts", "schema", "vsgen.schema.sql")

# sqlite ведёт эту таблицу сам и отвергает попытку её создать.
SKIP = ("sqlite_sequence",)

TENANT = "dev"
NOW = time.time()
DAY = 86400.0


def build_schema(c) -> int:
    raw = open(SCHEMA, encoding="utf-8").read()
    made = 0
    for stmt in raw.split(";"):
        s = stmt.strip()
        if not s:
            continue
        m = re.match(r'CREATE (TABLE|INDEX)\s+(\w+)', s, re.I)
        if m and m.group(2) in SKIP:
            continue
        c.execute(s)
        if m and m.group(1).upper() == "TABLE":
            made += 1
    # Журнал платных вызовов появился после снятия пакета, поэтому его нет в DDL
    # архива. Создаём тем же кодом, что и кабинет при старте, — второй, расходящийся
    # вариант схемы завёлся бы в тот же день.
    sys.path.insert(0, os.path.join(ROOT, "studio-platform", "server"))
    from pub_calls import ensure_calls_schema
    ensure_calls_schema(c)
    made += 1
    return made


def seed(c) -> dict:
    """Минимум, на котором собирается экран выпуска: канал → строка плана → выпуск."""
    c.execute("INSERT INTO tenants(id,name,created,is_active,daily_quota) VALUES(?,?,?,1,500)",
              (TENANT, "Локальная разработка", str(int(NOW))))
    c.execute("INSERT INTO users(id,name,token,role,created,daily_quota,keys,email,"
              "password_hash,tenant_id,key_opts,disabled) "
              "VALUES(1,'dev','dev-token','admin',?,500,'{}','dev@local','',?,'{}',0)",
              (str(int(NOW)), TENANT))

    c.execute("INSERT INTO pub_hosts(tenant_id,code,title,adspower_url,status,created) "
              "VALUES(?,'minsk','Минская машина','http://127.0.0.1:50325','active',?)",
              (TENANT, NOW))
    host_id = c.execute("SELECT id FROM pub_hosts WHERE code='minsk'").fetchone()[0]
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,status,created) "
              "VALUES(?,'46','Профиль 46',?,'active',?)", (TENANT, host_id, NOW))
    profile_id = c.execute("SELECT id FROM pub_profiles WHERE serial='46'").fetchone()[0]

    c.execute("INSERT INTO presets(tenant_id,name,settings,created) VALUES(?,?,?,?)",
              (TENANT, "Базовый", '{"tts_provider":"lumean","image_model":"NARWHAL"}', NOW))
    preset_id = c.execute("SELECT id FROM presets WHERE name='Базовый'").fetchone()[0]

    c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,handle,lang,niche,profile_id,host_id,"
        "tz,schedule,horizon_days,preset_rewrite,preset_vsgen_id,preset_cover,video_mode,"
        "t2v_first_n,status,created,updated,lead_days,deferred,deferred_limit) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT, "UCdev0000000000000000001", "Демо-канал", "@demo", "pl", "история",
         profile_id, host_id, "Europe/Warsaw", '{"mon":["18:00"],"thu":["18:00"]}', 3,
         "Базовый рерайт", preset_id, "Базовая обложка", "slideshow", 0,
         "active", NOW, NOW, 2, 1, 5))
    ch_id = c.execute("SELECT id FROM pub_channels WHERE handle='@demo'").fetchone()[0]

    # Строки плана ставим РОВНО в слоты расписания канала, иначе календарь покажет их
    # пропусками: он сопоставляет план со слотами по округлённому моменту, как это
    # делает pub_tags.coverage(). Слоты считаем тем же кодом, что и кабинет.
    sys.path.insert(0, os.path.join(ROOT, "studio-platform", "server"))
    from channel_slots import upcoming_slots           # noqa: E402
    import datetime as _dt

    sched = {"mon": ["18:00"], "thu": ["18:00"]}
    tz = "Europe/Warsaw"
    now_dt = _dt.datetime.fromtimestamp(NOW, _dt.timezone.utc)
    ahead = upcoming_slots(sched, tz, 7, now_dt)
    # Прошлые слоты — то же расписание, отмотанное назад (зеркало upcoming_slots).
    from channel_slots import normalize_schedule, _localize, _zone, DAY_KEYS, ANY_DAY
    plan_days = normalize_schedule(sched)
    zone = _zone(tz)
    moment = now_dt.astimezone(zone)
    behind = []
    for off in range(14, -1, -1):
        day = (moment - _dt.timedelta(days=off)).date()
        key = ANY_DAY if ANY_DAY in plan_days else DAY_KEYS[day.weekday()]
        for hh, mm in plan_days.get(key, []):
            pt = _localize(day, hh, mm, zone)
            if pt is not None and pt <= moment:
                behind.append(pt)
    behind.sort()

    def add_item(slot_dt, url, title, status="planned"):
        c.execute(
            "INSERT INTO pub_plan_items(tenant_id,channel_id,version,donor_url,position,"
            "slot_at,slot_mode,overrides,status,frozen,created,updated,donor_title) "
            "VALUES(?,?,1,?,0,?,'auto','{}',?,?,?,?,?)",
            (TENANT, ch_id, url, slot_dt.timestamp(), status,
             1 if status != "planned" else 0, NOW, NOW, title))
        return c.execute("SELECT id FROM pub_plan_items ORDER BY id DESC LIMIT 1").fetchone()[0]

    # Прошлое: вышедший ролик, остановленный и один пропуск (слот без строки вовсе).
    done_item = add_item(behind[-3], "https://www.youtube.com/watch?v=dev0000done",
                         "Вышедший ролик", "done") if len(behind) >= 3 else None
    blocked_item = add_item(behind[-1], "https://www.youtube.com/watch?v=dev000stop",
                            "Остановленный выпуск", "frozen") if behind else None
    # behind[-2] намеренно оставлен пустым — это и есть видимый пропуск.

    # Ближайший слот: выпуск в работе, ради которого и заведён проект.
    item_id = add_item(ahead[0], "https://www.youtube.com/watch?v=dev0000001",
                       "Донорский ролик для разработки", "frozen")

    # Сам проект VideoStudio: страница /p/{id} без этой строки отдаёт заглушку.
    c.execute("INSERT INTO projects(id,tenant_id,name,dir_path,status,created,updated,settings) "
              "VALUES(?,?,?,?,?,?,?,?)",
              ("proj-dev-0001", TENANT, "Демо-проект", "projects/dev/proj-dev-0001",
               "active", NOW, NOW,
               '{"model":"NARWHAL","sync_audio":"1","transitions":"fast",'
               '"min_image_duration":"2.0","max_image_duration":"30.0","seed":""}'))

    rid = "rel-dev-0001"
    c.execute(
        "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
        "channel_id,uc,slot_at,project_id,status,phase,heartbeat,deadline,attempts,"
        "created,updated) VALUES(?,?,?,1,?,?,?,?,'running','media_ready',?,?,0,?,?)",
        (rid, TENANT, item_id, ch_id, "UCdev0000000000000000001", ahead[0].timestamp(),
         "proj-dev-0001", NOW, NOW + 6 * 3600, NOW - 3600, NOW))

    if done_item:
        c.execute(
            "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
            "channel_id,uc,slot_at,project_id,status,phase,created,updated) "
            "VALUES('rel-dev-done',?,?,1,?,?,?,'proj-dev-done','done','scheduled',?,?)",
            (TENANT, done_item, ch_id, "UCdev0000000000000000001",
             behind[-3].timestamp(), NOW, NOW))
    if blocked_item:
        c.execute(
            "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
            "channel_id,uc,slot_at,status,phase,blocked_reason,created,updated) "
            "VALUES('rel-dev-stop',?,?,1,?,?,?,'blocked','cover_ready',"
            "'у канала не выбран пресет обложек',?,?)",
            (TENANT, blocked_item, ch_id, "UCdev0000000000000000001",
             behind[-1].timestamp(), NOW, NOW))

    # Лента фаз: как её пишет log_phase() в pub_engine.
    for i, ph in enumerate(("created", "source_ready", "project_ready")):
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,payload,created) "
                  "VALUES(?,?,?,'{}',?)", (rid, i + 1, ph, NOW - (3 - i) * 900))

    # Этапы: два готовы, один идёт, два ещё не поставлены — как в pub_release_jobs.
    for service, kind, status, job in (("rewrite", "job", "done", "rw-1001"),
                                       ("vsgen", "tts", "done", "41801"),
                                       ("vsgen", "images", "running", "41802")):
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,"
                  "status,payload,created,updated) VALUES(?,?,?,?,?,'{}',?,?)",
                  (rid, service, kind, job, status, NOW, NOW))

    for kind, status, progress, stage, provider in (
            ("tts", "done", 1.0, "готово", "lumean"),
            ("images", "running", 0.63, "38 из 60", "googler")):
        c.execute("INSERT INTO queue_tasks(tenant_id,project_id,kind,status,created,params,"
                  "uid,progress,stage,provider) VALUES(?,?,?,?,?,'{}',1,?,?,?)",
                  (TENANT, "proj-dev-0001", kind, status, NOW, progress, stage, provider))

    # Ключи и кулдауны — чтобы экрану «Ключи» было что показывать. Значения выдуманные,
    # формат — тот, что кабинет реально пишет в admin_state.credential_cooldown.
    c.execute("UPDATE users SET keys=? WHERE id=1", (json.dumps({
        "googler": ["own-key-AAAAAAAAAAAAAAAAAAAA", "own-key-BBBBBBBBBBBBBBBBBBBB"],
        "openai": "own-openai-CCCCCCCCCCCCCCCC"}),))
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES('credential_cooldown',?,?)",
              (json.dumps({
                  "googler:1": {"family": "googler", "mask": "fg_live_aaaa…b3gt",
                                "until": NOW + 1800,
                                "why": "HTTP 429 — часовой лимит картинок исчерпан"},
                  "openai:1": {"family": "openai", "mask": "sk-proj-cccc…9f2a",
                               "until": NOW + 240, "shared": True,
                               "why": "лимит заметил сосед (rewrite): окно подписки исчерпано"},
              }), NOW))
    c.execute("INSERT OR REPLACE INTO provider_rr_cursor(provider,last_uid,updated) "
              "VALUES('recrafter',1,?)", (NOW,))
    # Очередь с ожиданием: старейшая задача ждёт два часа — видно на экране «Ключи».
    for kind, prov, st, age in (("images", "recrafter", "queued", 7200),
                                ("images", "recrafter", "queued", 900),
                                ("tts", "lumean", "queued", 120)):
        c.execute("INSERT INTO queue_tasks(tenant_id,project_id,kind,status,created,params,"
                  "uid,provider) VALUES(?,?,?,?,?,'{}',1,?)",
                  (TENANT, "proj-dev-0001", kind, st, NOW - age, prov))

    c.execute("INSERT INTO pub_release_log(release_id,ts,level,step,message) "
              "VALUES(?,?,'info','кадры','готово 38 из 60, две сцены в добивке')",
              (rid, NOW - 300))
    return {"release_id": rid, "channel_id": ch_id, "plan_item_id": item_id}


# Размеры файлов взяты близкими к настоящим (проект ≈ 518 МБ), но сами файлы —
# разрежённые: `st_size` показывает нужные мегабайты, а на диске занято несколько
# килобайт. Иначе локальная песочница отъедала бы полтора гигабайта ради картинки
# на экране. Уборка меряет ровно `st_size`, поэтому отчёт получается настоящим.
DISK_LAYOUT = (
    ("googler/{i:03d}.jpg", 155, 300 * 1024),
    ("googler video/{i:03d}.mp4", 12, 4 * 1024 * 1024),
    ("voice_final.mp3", 1, 25 * 1024 * 1024),
)
DISK_ARTIFACTS = (
    ("render/out.mp4", 420 * 1024 * 1024),
    ("subtitles.srt", 40 * 1024),
    ("chapters/chapters.txt", 4 * 1024),
)


def _sparse(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Пустой файл + truncate: дыра получается целиком, и на диске файл занимает ноль.
    # Вариант «seek к концу и записать байт» на APFS резервирует место целиком для
    # мелких файлов — 155 кадров превращались в настоящие 45 МБ.
    open(path, "wb").close()
    if size > 0:
        os.truncate(path, size)


def seed_disk(c, tenants_dir: str) -> int:
    """Каталоги проектов на диске — иначе экрану «Диск» нечего показывать.

    Три состояния, ради которых экран и нужен: работающий проект (трогать нельзя),
    давно опубликованный (кандидат на очистку) и вчерашний (рано).
    """
    made = 0
    plan = (
        # id проекта,       артефакты?, фаза,          когда опубликован
        ("proj-dev-0001",   False,      "media_ready",  0),
        ("proj-dev-0002",   True,       "published",    NOW - 40 * DAY),
        ("proj-dev-0003",   True,       "published",    NOW - 3 * DAY),
    )
    for pid, with_artifacts, phase, published in plan:
        pdir = os.path.join(tenants_dir, TENANT, "projects", pid)
        for tmpl, count, size in DISK_LAYOUT:
            for i in range(count):
                _sparse(os.path.join(pdir, tmpl.format(i=i)), size)
        if with_artifacts:
            for rel, size in DISK_ARTIFACTS:
                _sparse(os.path.join(pdir, rel), size)
        if pid != "proj-dev-0001":
            c.execute("INSERT OR REPLACE INTO projects(id,tenant_id,name,dir_path,status,"
                      "created,updated,settings) VALUES(?,?,?,?,?,?,?,'{}')",
                      (pid, TENANT, f"Проект {pid[-4:]}", f"projects/{TENANT}/{pid}",
                       "done", published - 2 * DAY, published))
            c.execute(
                "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
                "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
                "video_id,attempts,created,updated) VALUES(?,?,?,1,?,?,?,?,?,?,?,0,?,?)",
                (f"rel-{pid}", TENANT, -abs(hash(pid)) % 10 ** 6, 1,
                 "UCdev0000000000000000001", published, pid, "done", phase,
                 "yt-" + pid[-4:], published - 2 * DAY, published))
        # Журнал платных вызовов: без него уборка не сможет сказать, сколько денег
        # выбрасывается вместе с кадрами.
        for i in range(155):
            c.execute(
                "INSERT OR IGNORE INTO provider_calls(tenant_id,project_id,release_id,"
                "kind,unit,fingerprint,provider,model,status,result_path,cost,attempts,"
                "created,updated) VALUES(?,?,?,'images',?,?,'recrafter','NARWHAL',"
                "'delivered',?,4.0,1,?,?)",
                (TENANT, pid, f"rel-{pid}", f"{i:03d}", f"fp-{pid}-{i}",
                 f"googler/{i:03d}.jpg", NOW - 10 * DAY, NOW - 10 * DAY))
        made += 1
    return made


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "vsgen.dev.db")
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    try:
        made = build_schema(c)
        ids = seed(c)
        tenants = os.environ.get("VS_TENANTS_DIR") or os.path.join(HERE, "data", "tenants")
        projects = seed_disk(c, tenants)
        c.commit()
        tables = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        c.close()
    print(f"база:     {path}")
    print(f"таблиц:   {made} создано из DDL, {tables} всего в файле")
    print(f"выпуск:   {ids['release_id']} — фаза media_ready, канал {ids['channel_id']}")
    print(f"на диске: {projects} проекта в {tenants} (файлы разрежённые: "
          f"размер настоящий, места почти не занимают)")


if __name__ == "__main__":
    main()
