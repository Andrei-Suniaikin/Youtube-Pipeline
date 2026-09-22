#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Уборка исходников удаляет только по квитанции рендера — и молчит, когда квитанции нет.

Дефект: прежняя уборка считала проект убираемым по условию «в каталоге `render/` есть
ЛЮБОЙ файл *.mp4». Этот mp4 мог остаться от предыдущего рендера, быть оборванным на
половине или вообще чужим; проверки не было никакой, каталоги сносились целиком через
`rmtree(ignore_errors=True)`. В бою ровно эта версия стёрла 761 МБ исходников, которые
нечем восстановить: кадры оплачены поштучно, озвучка — посимвольно.

Главный инвариант, ради которого написан файл: у проекта, отрендеренного ДО появления
колонок квитанции, `render_sha256` пуст — и уборка обязана просто НЕ СРАБОТАТЬ, а не
удалить «на всякий случай». Проверяется первым же сценарием и по каждой колонке
отдельно: отсутствие любой из трёх (`output_rel`, `render_sha256`, `render_size`)
запрещает удаление.

Второй инвариант: пока не выяснена судьба оплаченного вызова (`provider_calls` в
`reserved`/`ordered`), исходники не трогаем — иначе выбросим кадр, за который уже
заплачено, и лишим повтор возможности узнать, что заказ вообще был.

Кабинет здесь настоящий: база, схема и каталог проектов — те, что создаёт main.py,
подменены только пути и авторизация. Дерево файлов настоящее, потому что проверять
надо поведение с диском: симлинк, подменённый файл, чужой каталог рядом.

    .venv/bin/python tests/test_source_retention.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="source-retention-")

os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["PULLER_TOKEN"] = "test-puller-token"
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402
import source_retention as R                                      # noqa: E402
from starlette.exceptions import HTTPException                    # noqa: E402
from starlette.testclient import TestClient                       # noqa: E402

FAILS = []
TID = "dev"
TOKEN = os.environ["PULLER_TOKEN"]
DAY = 86400.0
NOW = time.time()
CUTOFF = NOW - 7 * DAY                 # срок хранения по умолчанию — 7 дней
RENDERED = NOW - 30 * DAY              # «рендер давно готов»

# Медиа-исходники: их можно сгенерировать заново, они и есть кандидаты на удаление.
SOURCES = ["googler/001.jpg", "googler/002.jpg",
           ".thumbs/t.jpg", "googler video/clip.mp4", "_FINAL_AUDIO_EDGETTS/001.mp3"]
# Неприкосновенное: план сцен (оплачен промптами), материалы юзера, текст.
# `Images/**` здесь, а не в SOURCES: это РУЧНЫЕ материалы человека. Кабинет читает их
# наравне с генерацией (`_materials_ready`, `_DL_PATTERNS['images']`, галерея), но не
# создаёт: единственный писатель в `Images/` во всём коде — image_worker в старом
# режиме тегов, и он кладёт строго в `Images/<номер>/`. Всё остальное попало туда
# руками, восстановить это нечем, и по умолчанию уборка туда не заходит.
KEEP = ["googler/scenes.json", "googler/scenes.meta.json", "_RENDER_MEDIA/intro.mp4",
        "_ASSETS/logo.png", "_style_refs/ref.png", "source.txt", "subtitles.srt",
        "chapters/chapters.txt", "Images/сцены/pic.png", "Images/своё.jpg"]

_seq = [0]


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def put(path, blob=b"x" * 2048, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(blob)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def exists(pid, rel):
    return os.path.exists(os.path.join(M._project_dir(TID, pid), rel))


def make_project(pid, *, sources=None, receipt=True, rendered_at=RENDERED,
                 status="done", output_rel=None, sha256=None, size=None, extra=()):
    """Проект на диске + строка рендера в очереди. Возвращает job_id рендера.

    `receipt=False` — старый проект: mp4 лежит, а колонок квитанции нет. Ровно с таким
    проектом прежняя уборка удаляла исходники, и ради него написан этот файл.
    """
    _seq[0] += 1
    jid = _seq[0] * 100
    d = M._project_dir(TID, pid)
    shutil.rmtree(d, ignore_errors=True)
    for rel in (SOURCES if sources is None else sources):
        put(os.path.join(d, rel), mtime=rendered_at - 60)
    for rel in KEEP:
        put(os.path.join(d, rel), mtime=rendered_at - 60)
    body = b"mp4-" + pid.encode() + b"\0" * 4096
    rel_mp4 = output_rel or f"render/{jid}.mp4"
    put(os.path.join(d, rel_mp4), body, mtime=rendered_at)
    c = M.db()
    c.execute("DELETE FROM projects WHERE id=?", (pid,))
    c.execute("DELETE FROM queue_tasks WHERE project_id=?", (pid,))
    c.execute("DELETE FROM provider_calls WHERE project_id=?", (pid,))
    c.execute("DELETE FROM admin_state WHERE k=?", ("storage_cleanup:" + pid,))
    c.execute("INSERT INTO projects(id,tenant_id,name,dir_path,status,created,updated) "
              "VALUES(?,?,?,?,?,?,?)", (pid, TID, pid, d, "draft", rendered_at, rendered_at))
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,finished,"
              "output_rel,render_sha256,render_size) VALUES(?,?,?,'render',?,?,?,?,?,?)",
              (jid, TID, pid, status, rendered_at - 600, rendered_at, rel_mp4,
               (sha256 if sha256 is not None else hashlib.sha256(body).hexdigest()) if receipt else None,
               (size if size is not None else len(body)) if receipt else None))
    for n, (kind, st, ts) in enumerate(extra, start=1):
        c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,finished)"
                  " VALUES(?,?,?,?,?,?,?)", (jid + n, TID, pid, kind, st, ts, ts))
    c.commit()
    c.close()
    return jid


def purge(pid, *, cutoff=CUTOFF, budget=30.0, **kw):
    """Результат уборки одного проекта: словарь при удаче, текст причины при отказе.

    Неожиданная ошибка ввода-вывода называется типом — ровно так её записывает в
    отчёт `sweep`. Для уборки это тоже отказ: проект остался нетронутым.
    """
    try:
        return R.purge_project(M.DB, M.TENANTS_DIR, pid, cutoff,
                               time.monotonic() + budget, **kw)
    except R.Retained as exc:
        return str(exc)
    except OSError as exc:
        return type(exc).__name__


def field(got, key):
    """Поле результата, а при отказе — сам текст причины. Нужен, чтобы проваленная
    проверка называла причину отказа, а не роняла весь прогон на индексе строки."""
    return got[key] if isinstance(got, dict) else got


def state(pid, **kw):
    return field(purge(pid, **kw), "state")


def sources_alive(pid, sources=None):
    return sorted(rel for rel in (SOURCES if sources is None else sources) if exists(pid, rel))


def keep_alive(pid):
    return sorted(rel for rel in KEEP if exists(pid, rel))


def lock(pid, seconds=120.0, owner="другой-уборщик"):
    c = M.db()
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              ("storage_cleanup:" + pid,
               json.dumps({"owner": owner, "expires_at": time.time() + seconds}), time.time()))
    c.commit()
    c.close()


def unlock(pid):
    c = M.db()
    c.execute("DELETE FROM admin_state WHERE k=?", ("storage_cleanup:" + pid,))
    c.commit()
    c.close()


def cursor():
    c = M.db()
    r = c.execute("SELECT v FROM admin_state WHERE k='source_retention_cursor'").fetchone()
    c.close()
    return json.loads(r["v"])["last_project_id"] if r else None


def call(pid, status):
    """Строка журнала платных вызовов. UNIQUE по (проект, вид, единица, отпечаток),
    поэтому у каждой строки свой номер сцены."""
    _seq[0] += 1
    c = M.db()
    c.execute("INSERT INTO provider_calls(tenant_id,project_id,kind,unit,fingerprint,status,"
              "created,updated) VALUES(?,?,'images',?,?,?,?,?)",
              (TID, pid, str(_seq[0]), "fp%d" % _seq[0], status, time.time(), time.time()))
    c.commit()
    c.close()


def main() -> int:
    client = TestClient(M.app)
    saved = (M.web_user, M._get_project, M._psettings, M._render_inputs_ready)
    try:
        print("Старый проект без квитанции: уборка НЕ срабатывает (главный инвариант)")

        # Колонки квитанции появились вместе с приёмом результата. У всего, что
        # отрендерено раньше, они пусты — и это единственное, что отличает такой
        # проект от убираемого. Прежняя уборка разницы не видела вовсе.
        make_project("p_legacy", receipt=False)
        check("отказ назван по-английски, как в бою",
              state("p_legacy"), "legacy render lacks exact output receipt")
        check("все исходники на месте", sources_alive("p_legacy"), sorted(SOURCES))
        check("маркер уборки не написан", exists("p_legacy", "_sources_purged.json"), False)

        # Каждая колонка по отдельности: половина квитанции — не квитанция.
        for column in ("render_sha256", "render_size", "output_rel"):
            make_project("p_half")
            c = M.db()
            c.execute(f"UPDATE queue_tasks SET {column}=NULL WHERE project_id='p_half'")
            c.commit(); c.close()
            check(f"пустая колонка {column} запрещает уборку",
                  state("p_half"), "legacy render lacks exact output receipt")
            check(f"исходники целы при пустой {column}",
                  sources_alive("p_half"), sorted(SOURCES))

        print("\nПроект ещё работает или рендер свежий — не наше дело")

        make_project("p_busy", extra=[("images", "queued", NOW)])
        check("есть нетерминальная задача", state("p_busy"), "project has active work")
        check("исходники целы", sources_alive("p_busy"), sorted(SOURCES))

        make_project("p_young", rendered_at=NOW - 2 * DAY)
        check("рендер моложе срока хранения",
              state("p_young"), "latest render younger than retention")
        check("исходники целы", sources_alive("p_young"), sorted(SOURCES))

        make_project("p_regen", extra=[("images", "done", RENDERED + 3600)])
        check("после рендера была генерация",
              state("p_regen"), "new generation after latest render")
        check("исходники целы", sources_alive("p_regen"), sorted(SOURCES))

        # Главы читают готовое медиа и пишут текст — после них исходники всё ещё
        # описывают тот самый ролик, поэтому уборке они не мешают.
        make_project("p_chapters", extra=[("chapters", "done", RENDERED + 3600)])
        check("главы после рендера уборке не мешают", state("p_chapters"), "purged")

        # Карточка загрузки — тот же случай (Issue 2 ревью): она зависит только от
        # source.txt и пишет в `_cards/v1/job-<id>/*.json`, то есть ни одного файла в
        # каталогах-кандидатах. Пока `chapters` был единственным исключением, ЛЮБАЯ
        # карточка, сделанная после рендера, дисквалифицировала проект навсегда:
        # строка остаётся в очереди вечно, и гейт срабатывал бы на каждом проходе.
        make_project("p_cards", extra=[("cards", "done", RENDERED + 3600)])
        check("карточка загрузки уборке не мешает", state("p_cards"), "purged")

        # Список исключений закрытый: всё, что переделывает материалы, и всё, чего мы
        # ещё не знаем, по-прежнему запрещает уборку.
        make_project("p_tts_after", extra=[("tts", "done", RENDERED + 3600)])
        check("переозвучка после рендера — запрет",
              state("p_tts_after"), "new generation after latest render")
        make_project("p_future", extra=[("невиданный_этап", "done", RENDERED + 3600)])
        check("неизвестный будущий вид — тоже запрет",
              state("p_future"), "new generation after latest render")

        print("\nИсходник правили после рендера — значит он не из этого ролика")

        pid = "p_touched"
        make_project(pid)
        os.utime(os.path.join(M._project_dir(TID, pid), "googler/001.jpg"),
                 (RENDERED + 600, RENDERED + 600))
        check("отказ по mtime", state(pid), "source edited after latest render")
        check("ни один файл не удалён", sources_alive(pid), sorted(SOURCES))

        print("\nСимлинк в исходниках: остановка целиком, а не «пропустим этот файл»")

        pid = "p_symlink"
        make_project(pid)
        outside = os.path.join(TMP, "чужое.png")
        put(outside)
        os.symlink(outside, os.path.join(M._project_dir(TID, pid), ".thumbs/ссылка.jpg"))
        check("файл-симлинк", state(pid), "source path changed")
        check("чужой файл цел", os.path.exists(outside), True)
        check("ни один исходник не удалён", sources_alive(pid), sorted(SOURCES))

        # Симлинк ВНУТРЬ проекта опаснее наружного: путь остаётся в границах
        # каталога, и ловит его только проверка «это не симлинк». Через такую
        # ссылку уборка считала бы неприкосновенный _RENDER_MEDIA своим исходником.
        pid = "p_symlink_in"
        make_project(pid)
        os.symlink(os.path.join(M._project_dir(TID, pid), "_RENDER_MEDIA/intro.mp4"),
                   os.path.join(M._project_dir(TID, pid), "googler video/свой.mp4"))
        check("симлинк внутрь проекта", state(pid), "source path changed")
        check("материалы юзера целы", exists(pid, "_RENDER_MEDIA/intro.mp4"), True)
        check("ни один исходник не удалён", sources_alive(pid), sorted(SOURCES))

        pid = "p_symdir"
        make_project(pid)
        os.symlink(os.path.join(TMP, "tenants"),
                   os.path.join(M._project_dir(TID, pid), ".thumbs/наружу"))
        check("каталог-симлинк", state(pid), "source directory changed")
        check("ни один исходник не удалён", sources_alive(pid), sorted(SOURCES))

        print("\nФинальный mp4 не тот, что в квитанции")

        pid = "p_othersha"
        jid = make_project(pid)
        put(os.path.join(M._project_dir(TID, pid), f"render/{jid}.mp4"), b"other-bytes" + b"\0" * 4096)
        check("sha не совпал", state(pid), "final video differs from render receipt")
        check("исходники целы", sources_alive(pid), sorted(SOURCES))

        pid = "p_othersize"
        make_project(pid, size=999)
        check("размер не совпал", state(pid), "final video differs from render receipt")
        check("исходники целы", sources_alive(pid), sorted(SOURCES))

        # Прежнее условие звучало «в render/ есть ЛЮБОЙ mp4» — вот оно во плоти:
        # рядом лежит посторонний mp4, а квитанционного файла нет.
        pid = "p_strayonly"
        jid = make_project(pid)
        os.remove(os.path.join(M._project_dir(TID, pid), f"render/{jid}.mp4"))
        put(os.path.join(M._project_dir(TID, pid), "render/старый.mp4"))
        check("чужой mp4 в render/ уборку не разрешает", state(pid), "FileNotFoundError")
        check("исходники целы", sources_alive(pid), sorted(SOURCES))

        print("\nОплаченный вызов с невыясненной судьбой держит исходники")

        for status in ("reserved", "ordered"):
            pid = "p_calls"
            make_project(pid)
            call(pid, status)
            check(f"строка журнала в {status}", state(pid), "remote operation unresolved")
            check("исходники целы", sources_alive(pid), sorted(SOURCES))

        # delivered/failed — судьба выяснена, держать нечего.
        pid = "p_calls_done"
        make_project(pid)
        call(pid, "delivered")
        call(pid, "failed")
        check("закрытые вызовы уборке не мешают", state(pid), "purged")

        print("\nУспешная уборка: уходит только перегенерируемое медиа")

        pid = "p_ok"
        jid = make_project(pid)
        got = purge(pid)
        check("состояние", field(got, "state"), "purged")
        check("удалено файлов", field(got, "removed"), len(SOURCES))
        check("медиа-исходников не осталось", sources_alive(pid), [])
        check("всё дорогое на месте", keep_alive(pid), sorted(KEEP))
        check("готовый mp4 на месте", exists(pid, f"render/{jid}.mp4"), True)
        check("маркер-аудит записан", exists(pid, "_sources_purged.json"), True)
        try:
            marker = json.load(open(os.path.join(M._project_dir(TID, pid), "_sources_purged.json")))
        except OSError:
            marker = {}                                  # сломанная реализация не роняет прогон
        check("в маркере записан рендер, по которому убирали", marker.get("render_job_id"), jid)
        check("и его sha", len(marker.get("render_sha256") or ""), 64)
        # Пустые каталоги остаются: их удаление ничего не освобождает, зато
        # сломало бы код, который проверяет «папка озвучки существует».
        check("каталог озвучки не снесён целиком",
              os.path.isdir(os.path.join(M._project_dir(TID, pid), "_FINAL_AUDIO_EDGETTS")), True)
        # Второй проход по уже убранному проекту: удалять нечего, маркер не
        # переписывается — история прошлой уборки сохраняется.
        def marker_stamp():
            try:
                return os.stat(os.path.join(M._project_dir(TID, pid), "_sources_purged.json")).st_mtime_ns
            except OSError:
                return None

        before = marker_stamp()
        check("повтор ничего не делает", state(pid), "no_sources")
        check("маркер не переписан", marker_stamp(), before)

        print("\nРучные материалы пользователя переживают уборку (Issue 1 ревью)")

        # Прежняя версия (как и боевая) считала весь `Images/**` выхлопом генерации.
        # В `Images/` лежат картинки, которые человек принёс сам: кабинет их читает
        # (`_materials_ready`, галерея, архив «скачать картинки»), но нигде не создаёт
        # и, значит, не восстановит. Удалить их вместе с выхлопом — потеря навсегда.
        pid = "p_manual"
        make_project(pid)
        check("уборка прошла", state(pid), "purged")
        check("ручная картинка в подпапке цела", exists(pid, "Images/сцены/pic.png"), True)
        check("и ручная картинка в корне Images цела", exists(pid, "Images/своё.jpg"), True)
        check("а выхлоп генерации убран", sources_alive(pid), [])
        check("всё дорогое на месте", keep_alive(pid), sorted(KEEP))

        print("\nРежим Images — настройка владельца, а не догадка кода")

        check("умолчание — не трогать", R.images_mode(), "keep")
        check("регистр значения не важен", R.images_mode("GENERATED"), "generated")
        # Непонятое значение трактуется как `keep`, а не как ошибка: падать уборке
        # нельзя, но и удалять по непонятой настройке — тем более.
        check("опечатка в .env = keep", R.images_mode("вклю"), "keep")

        pics = ["Images/1/001.jpg", "Images/2/002.jpg"]
        pid = "p_images_generated"
        make_project(pid, sources=SOURCES + pics)
        check("режим generated убирает", state(pid, images="generated"), "purged")
        check("выхлоп старого режима тегов ушёл",
              sources_alive(pid, pics), [])
        check("а ручные материалы остались", exists(pid, "Images/сцены/pic.png"), True)

        pid = "p_images_keep"
        make_project(pid, sources=SOURCES + pics)
        check("умолчание убирает всё прочее", state(pid), "purged")
        check("но в Images не заходит вовсе", sources_alive(pid, pics), sorted(pics))

        pid = "p_images_all"
        make_project(pid, sources=SOURCES + pics)
        check("режим all — как в бою", state(pid, images="all"), "purged")
        check("уходит и выхлоп тегов", sources_alive(pid, pics), [])
        check("и ручное вместе с ним", exists(pid, "Images/сцены/pic.png"), False)

        # Симлинк вместо самого каталога Images — остановка целиком, как и у прочих
        # каталогов-кандидатов. В режиме keep до него дело не доходит: не смотрим.
        pid = "p_images_symdir"
        make_project(pid, sources=SOURCES)
        shutil.rmtree(os.path.join(M._project_dir(TID, pid), "Images"))
        os.symlink(os.path.join(TMP, "tenants"),
                   os.path.join(M._project_dir(TID, pid), "Images"))
        check("Images-симлинк останавливает уборку",
              state(pid, images="generated"), "source directory changed")
        check("а в режиме keep он просто не рассматривается", state(pid), "purged")

        # Маркер — аудит, а НЕ гейт: прежняя версия по нему решала «здесь уже убирали»,
        # и проект, перегенерировавший материалы, не убирался уже никогда.
        pid = "p_marked"
        make_project(pid)
        put(os.path.join(M._project_dir(TID, pid), "_sources_purged.json"), b"{}",
            mtime=RENDERED - 60)
        check("старый маркер уборку не отменяет", state(pid), "purged")

        print("\nФайл подменили между проверкой и удалением")

        pid = "p_swap"
        make_project(pid, sources=["_FINAL_AUDIO_EDGETTS/001.mp3"])
        entries = R.source_files(R.inside(M._project_dir(TID, pid), M.TENANTS_DIR), RENDERED)
        check("кандидат ровно один", len(entries), 1)
        os.utime(entries[0][0], (RENDERED - 5, RENDERED - 5))     # переозвучили под руками
        try:
            R._unlink_exact(R.inside(M._project_dir(TID, pid), M.TENANTS_DIR), entries[0])
            got = "удалил"
        except R.Retained as exc:
            got = str(exc)
        check("подменённый файл не удаляется", got, "source changed during retention")
        check("файл на месте", exists(pid, "_FINAL_AUDIO_EDGETTS/001.mp3"), True)

        print("\nЗамок: занятый проект не получает ни задачи, ни удаления")

        pid = "p_locked"
        make_project(pid, extra=[("render", "queued", NOW)])
        M.web_user = lambda request: {"id": 1, "tenant_id": TID, "name": "тест", "role": "admin"}
        M._get_project = lambda tid, p: {"name": p, "settings": "{}"}
        M._psettings = lambda p: {}
        M._render_inputs_ready = lambda tid, p: True
        jid_queued = _seq[0] * 100 + 1

        lock(pid)
        check("чужая аренда видна кабинету", M._storage_project_held(M.db(), pid), True)
        got = client.post("/api/render/claim", headers={"X-Puller-Token": TOKEN}, json={}).json()
        check("рендер не выдан", got.get("job"), None)
        check("задача осталась в очереди", M.db().execute(
            "SELECT status FROM queue_tasks WHERE job_id=?", (jid_queued,)).fetchone()[0], "queued")

        # follow_redirects=False: удачное удаление отвечает 302 на список проектов,
        # и без этого «удалил» было бы не отличить от «отказал» по коду ответа.
        r = client.post(f"/api/projects/{pid}/delete", follow_redirects=False)
        check("удаление проекта отказано", r.status_code, 409)
        check("человеку сказано, почему", "архивируется" in r.text, True)
        check("каталог проекта цел", os.path.isdir(M._project_dir(TID, pid)), True)
        check("строка проекта цела", bool(M.db().execute(
            "SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone()), True)

        try:
            M._enqueue(1, TID, pid, "tts", {})
            got = "поставлена"
        except HTTPException as exc:
            got = exc.status_code
        check("новая задача не ставится", got, 409)

        # Issue 4 ревью. Карточка загрузки идёт МИМО транзакции `_enqueue`: тот отдаёт
        # её `_enqueue_card_task` ещё до `BEGIN IMMEDIATE` и до проверки замка. Пока
        # своей проверки там не было, карточка оставалась единственным видом задачи,
        # который мог лечь в очередь посреди прохода уборки — гейт «ни одной
        # нетерминальной задачи» к тому моменту уже пройден.
        card_context = {
            "external_key": "retention-card", "channel": "Канал", "brand": "SHADOW",
            "language_code": "PL", "source_title": "Заголовок источника",
            "examples": [{"title": "Пример заголовка",
                          "description": "Пример описания достаточной длины для проверки"}],
        }
        try:
            M._enqueue(1, TID, pid, "cards", {"context": dict(card_context)})
            got = "поставлена"
        except HTTPException as exc:
            got = exc.status_code
        check("карточка загрузки тоже не ставится", got, 409)
        check("и строки в очереди не появилось", M.db().execute(
            "SELECT COUNT(*) FROM queue_tasks WHERE project_id=? AND kind='cards'",
            (pid,)).fetchone()[0], 0)
        check("и в card_jobs тоже", M.db().execute(
            "SELECT COUNT(*) FROM card_jobs WHERE project_id=?", (pid,)).fetchone()[0], 0)

        unlock(pid)
        got = client.post("/api/render/claim", headers={"X-Puller-Token": TOKEN}, json={}).json()
        check("после снятия замка задача выдана", (got.get("job") or {}).get("job_id"), jid_queued)

        # Отказ был именно про замок, а не про негодный контракт карточки: без замка
        # та же самая карточка ставится.
        check("без замка карточка ставится",
              bool(M._enqueue(1, TID, pid, "cards", {"context": dict(card_context)})), True)

        # Истёкшая аренда не держит проект вечно: зависший уборщик сам отпускает.
        lock(pid, seconds=-1)
        check("протухшая аренда не держит", M._storage_project_held(M.db(), pid), False)
        # А неразборчивая запись держит: «замок сломан» и «замка нет» — не одно и то же.
        c = M.db()
        c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                  ("storage_cleanup:" + pid, "не json", time.time()))
        c.commit(); c.close()
        check("битая запись считается занятой", M._storage_project_held(M.db(), pid), True)
        check("и уборка к такому проекту не подходит", state(pid), "project has maintenance owner")
        unlock(pid)

        print("\nСухой прогон замок НЕ берёт — выключенная уборка не мешает работе")

        # Issue 3 ревью. Замок — не диагностика, а запрет: пока он стоит, проект не
        # получает ни новой задачи (`_enqueue` → 409), ни выдачи рендера, ни удаления;
        # все трое спрашивают ровно `_storage_project_held` (проверено выше в этом же
        # файле). Держать его ради прохода, который заведомо ничего не удалит, — значит
        # мешать работой ВЫКЛЮЧЕННОЙ уборки, а выключена она по умолчанию.
        #
        # Смотрим в самый долгий момент прохода — полное чтение финального mp4.
        def during_sha(pid, **kw):
            """Что видно про замок ровно в момент сверки квитанции."""
            seen = {}
            real = R.sha

            def spy(path):
                seen["held"] = M._storage_project_held(M.db(), pid)
                return real(path)

            R.sha = spy
            try:
                seen["state"] = state(pid, **kw)
            finally:
                R.sha = real
            return seen

        make_project("p_dry_lock")
        seen = during_sha("p_dry_lock", dry_run=True)
        check("сухой проход доходит до сверки квитанции", seen["state"], "would_purge")
        check("и во время чтения mp4 проект НЕ заперт", seen.get("held"), False)
        check("исходники, разумеется, целы", sources_alive("p_dry_lock"), sorted(SOURCES))
        check("замка не осталось и после",
              M._storage_project_held(M.db(), "p_dry_lock"), False)

        # Парная проверка: боевой проход замок держит именно там, иначе первая половина
        # доказывала бы лишь то, что сверка не вызывается.
        make_project("p_wet_lock")
        seen = during_sha("p_wet_lock")
        check("боевой проход убирает", seen["state"], "purged")
        check("и во время чтения mp4 проект заперт", seen.get("held"), True)
        check("после прохода замок снят",
              M._storage_project_held(M.db(), "p_wet_lock"), False)

        print("\nПроход уборки: курсор, отчёт и выключатель")

        # Чистая площадка: в прошлых сценариях осталось десятка два проектов.
        c = M.db()
        c.execute("DELETE FROM queue_tasks")
        c.execute("DELETE FROM projects")
        c.execute("DELETE FROM provider_calls")
        c.execute("DELETE FROM admin_state")
        c.commit(); c.close()

        make_project("p_sweep1", receipt=False)
        check("курсор пуст", cursor(), None)
        report = R.sweep(M.DB, M.TENANTS_DIR, 7, 30, dry_run=False)
        check("проект отказал", report["retained"],
              {"legacy render lacks exact output receipt": 1})
        check("курсор сдвинулся даже после отказа", cursor(), "p_sweep1")
        check("исходники целы", sources_alive("p_sweep1"), sorted(SOURCES))

        # Порядок обхода начинается ПОСЛЕ курсора: иначе первый тяжёлый проект
        # после каждого рестарта съедал бы весь бюджет и душил остальных.
        make_project("p_sweep2", receipt=False)
        seen = []
        real_purge = R.purge_project

        def spy(database, root, project_id, *a, **kw):
            seen.append(project_id)
            return real_purge(database, root, project_id, *a, **kw)

        R.purge_project = spy
        try:
            R.sweep(M.DB, M.TENANTS_DIR, 7, 30, dry_run=False)
        finally:
            R.purge_project = real_purge
        check("после курсора идёт следующий, а не первый", seen, ["p_sweep2", "p_sweep1"])

        # Выключатель: по умолчанию уборка только отчитывается.
        make_project("p_dry")
        report = R.sweep(M.DB, M.TENANTS_DIR, 7, 30, dry_run=True)
        check("режим назван в отчёте", report.get("dry_run"), True)
        check("удалено ничего", report["projects"], 0)
        check("сказано, сколько убрал бы", report["would_purge"], 1)
        check("и сколько освободил бы", report["would_free_bytes"] > 0, True)
        check("исходники на диске целы", sources_alive("p_dry"), sorted(SOURCES))
        check("маркер не написан", exists("p_dry", "_sources_purged.json"), False)
        check("замок снят", M._storage_project_held(M.db(), "p_dry"), False)
        check("режим Images назван в отчёте", report.get("images"), "keep")

        check("по умолчанию автоуборка выключена", M.RETENTION_AUTO, False)
        M._sweep_project_sources()
        check("тик планировщика ничего не удалил", sources_alive("p_dry"), sorted(SOURCES))

        # И тот же проход с включённым выключателем — чтобы было видно, что
        # «ничего не удалено» выше даёт именно режим, а не непригодность проекта.
        report = R.sweep(M.DB, M.TENANTS_DIR, 7, 30, dry_run=False)
        check("с включённой уборкой проект убран", report["projects"], 1)
        check("медиа-исходников не осталось", sources_alive("p_dry"), [])
    finally:
        (M.web_user, M._get_project, M._psettings, M._render_inputs_ready) = saved
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
