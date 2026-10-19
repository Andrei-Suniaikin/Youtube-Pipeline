#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Срок хранения и уборка проектов (ТЗ §03/12: «механизма чистки нет вообще»).

Проект весит около 518 МБ, и место кончается само. Но «удалять старое» — это решение
не про технику, а про деньги: внутри проекта лежат кадры, за которые заплачено, и
озвучка, за которую заплачено. Поэтому здесь три отдельные вещи:

  посмотреть  — что лежит, сколько весит, сколько за это заплачено
  решить      — по правилу владельца, а не по нашему вкусу
  сделать     — с проверкой каждого пути, и по умолчанию не делая ничего

**Правила по умолчанию нет.** Пока владелец не назвал сроки, `plan()` возвращает
«оставить» для всего и говорит почему. Придумывать срок хранения за владельца нельзя:
ошибка здесь необратима и стоит денег.

Два уровня уборки, и разница между ними принципиальна:

  strip  удалить промежуточное — кадры, клипы, куски озвучки. Пять артефактов выпуска
         остаются. Это освобождает почти весь объём и оставляет ролик воспроизводимым
         для зрителя, но НЕ воспроизводимым для нас: пересобрать его заново будет
         стоить столько же, сколько он стоил в первый раз.
  drop   удалить проект целиком вместе с готовым mp4. Только когда ролик опубликован
         и подтверждён публичным — то есть существует уже не только у нас.

Условие для `drop` — фаза `published`. Не `done` и не `scheduled`: `done` в этой системе
означает «расписание проверено», а не «ролик вышел» (известная ловушка проекта).

**У `strip` условие мягче, и вот почему (18.10.2026).** Ролик, залитый на YouTube и
ждущий своей даты, лежит у нас полным проектом иногда три недели: план расписан вперёд
на двадцать дней. Таких на боевом сервере оказалось 266 штук на 159 ГБ — больше
половины всего места студии. При этом mp4 уже у Google, а кадры и озвучка нужны были
только чтобы его собрать. Поэтому кадры и озвучку можно убрать раньше публикации — но
не раньше, чем заливка доказана: нужен и номер ролика, и фаза `scheduled`. Готовый mp4
остаётся до публикации, чтобы перезаливка оставалась возможной без новых трат.

Сроки задаются в `.env` — их меняет владелец на своём сервере, без нас и без базы:

    VS_RETENTION_STRIP_DAYS=14   через сколько дней после публикации убрать промежуточное
    VS_RETENTION_UPLOADED_STRIP_DAYS=1
                                 через сколько дней после подтверждённой заливки убрать
                                 кадры и озвучку у ролика, который ещё не вышел (mp4 цел)
    VS_RETENTION_DROP_DAYS=90    через сколько удалить проект целиком
    VS_RETENTION_AUTO=1          уборка идёт сама; без этого — только показывает

`.env` выигрывает у базы намеренно: правило хранения — это решение владельца о его
деньгах и его диске, и оно должно меняться там, куда он имеет доступ, а не кнопкой
в интерфейсе, которую можно нажать случайно. Значение из базы (`dev/retention.py
--policy`) остаётся как запасной путь и работает, только пока `.env` молчит.

Автоуборка по умолчанию ВЫКЛЮЧЕНА, и это не перестраховка: сроки без `VS_RETENTION_AUTO`
дают отчёт «что было бы удалено», по которому видно цену решения до того, как оно
станет необратимым.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import time

STATE_KEY = "retention"
LAST_KEY = "retention_last"        # что сделала последняя уборка — для интерфейса
GB = 1024 ** 3
MB = 1024 ** 2

ENV_STRIP = "VS_RETENTION_STRIP_DAYS"
ENV_UPLOADED_STRIP = "VS_RETENTION_UPLOADED_STRIP_DAYS"
ENV_DROP = "VS_RETENTION_DROP_DAYS"
ENV_AUTO = "VS_RETENTION_AUTO"
ENV_EVERY = "VS_RETENTION_EVERY_SEC"

EVERY_DEFAULT = 3600
EVERY_MIN = 300                    # чаще смысла нет: проекты стареют днями, не минутами

# Что внутри проекта чем является. Порядок важен: первая подходящая категория выигрывает,
# поэтому «готовый mp4» стоит раньше «клипов» — оба .mp4, но лежат в разных папках.
# Шаблоны те же, что у выдачи файлов (main.py `_DL_PATTERNS`), чтобы уборка и скачивание
# смотрели на одни и те же файлы.
KEEP, STRIP = "keep", "strip"

CATEGORIES = (
    # код,        имя,                 судьба, шаблоны (glob от корня проекта, recursive)
    ("final",    "готовый ролик",      KEEP,  ((("render", "*.mp4"), False),)),
    ("subs",     "субтитры",           KEEP,  ((("*.srt",), False), (("**", "*.srt"), True))),
    ("chapters", "тайм-коды",          KEEP,  ((("chapters", "*.*"), False),)),
    ("cover",    "обложка",            KEEP,  ((("cover", "*.*"), False),
                                               (("cover*.jpg",), False),
                                               (("cover*.png",), False))),
    ("clips",    "клипы",              STRIP, ((("googler video", "*.mp4"), False),)),
    ("frames",   "кадры",              STRIP, ((("googler", "*.*"), False),
                                               (("Images", "**", "*.*"), True))),
    ("audio",    "озвучка",            STRIP, ((("**", "*.mp3"), True),)),
)

# Чем оплачен каждый вид промежуточного — колонка `kind` журнала платных вызовов.
PAID_BY = {"frames": "images", "clips": "video", "audio": "tts", "cover": "cover"}

# Фаза, начиная с которой ролик существует не только у нас.
SAFE_PHASE = "published"
# Фаза «файл у Google, ждёт своей даты». Для удаления mp4 её не хватает, для удаления
# кадров и озвучки — хватает: собирать ролик больше не из чего и не для чего.
UPLOADED_PHASE = "scheduled"


class Project:
    __slots__ = ("pid", "tid", "name", "path", "exists", "sizes", "total", "newest",
                 "phase", "release_id", "video_id", "published_at", "uploaded_at",
                 "busy", "paid")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    @property
    def strippable(self) -> int:
        return sum(v for k, v in (self.sizes or {}).items() if _fate(k) == STRIP)

    def as_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__} | {
            "strippable": self.strippable}


def _fate(code: str) -> str:
    for c, _n, fate, _p in CATEGORIES:
        if c == code:
            return fate
    return KEEP          # незнакомое не трогаем никогда


# ─────────────────────────────── посмотреть ───────────────────────────────

def measure(pdir: str) -> tuple:
    """{категория: байты}, самый свежий mtime, и байты, не попавшие ни в одну категорию.

    Пути проверяются `realpath`: симлинк наружу каталога проекта не должен ни попадать
    в подсчёт, ни тем более удаляться. Это тот же приём, что в выдаче файлов.
    """
    root = os.path.realpath(pdir)
    sizes, newest, seen = {}, 0.0, set()
    if not os.path.isdir(root):
        return sizes, newest, 0
    for code, _name, _fate, patterns in CATEGORIES:
        for parts, rec in patterns:
            for f in glob.glob(os.path.join(root, *parts), recursive=rec):
                full = os.path.realpath(f)
                if full in seen or not os.path.isfile(full):
                    continue
                if not full.startswith(root + os.sep):
                    continue          # симлинк наружу — не наше
                seen.add(full)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                sizes[code] = sizes.get(code, 0) + st.st_size
                newest = max(newest, st.st_mtime)
    rest = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.realpath(os.path.join(dirpath, fn))
            if full in seen or not full.startswith(root + os.sep):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            rest += st.st_size
            newest = max(newest, st.st_mtime)
    if rest:
        sizes["other"] = rest
    return sizes, newest, rest


def scan(c, tenants_dir: str, tenant=None) -> list:
    """Все проекты с диска и из базы, с ценой того, что в них лежит."""
    where, args = ((" WHERE tenant_id=?", (tenant,)) if tenant else ("", ()))
    out = []
    for p in c.execute(f"SELECT id, tenant_id, name FROM projects{where} "
                       f"ORDER BY tenant_id, id", args):
        pid, tid = p[0], p[1]
        pdir = os.path.join(tenants_dir, tid, "projects", pid)
        sizes, newest, _rest = measure(pdir)
        rel = c.execute(
            "SELECT release_id, phase, status, video_id, updated FROM pub_releases "
            "WHERE project_id=? ORDER BY updated DESC LIMIT 1", (pid,)).fetchone()
        # Когда заливка этого выпуска доказанно закончилась. Доказательство — не статус
        # задания, а номер ролика: без него «залито» означает только «мы пытались».
        uploaded_at = 0.0
        if rel and rel[3]:
            try:
                row = c.execute(
                    "SELECT MAX(finished) FROM pub_upload_jobs WHERE release_id=? "
                    "AND status='done' AND video_id IS NOT NULL AND video_id<>'' "
                    "AND finished IS NOT NULL", (rel[0],)).fetchone()
                uploaded_at = float(row[0] or 0.0) if row else 0.0
            except sqlite3.Error:
                # Базы без таблицы заданий заливки бывают у старых копий. Молчаливый
                # ноль здесь означает «момент заливки неизвестен» — и уборка такой
                # проект не тронет, а не уберёт на всякий случай.
                uploaded_at = 0.0
        busy = c.execute(
            "SELECT COUNT(*) FROM queue_tasks WHERE project_id=? "
            "AND status IN ('queued','running')", (pid,)).fetchone()[0]
        paid = {}
        for row in c.execute(
                "SELECT kind, COUNT(*) n, SUM(COALESCE(cost,0)) s FROM provider_calls "
                "WHERE project_id=? AND status='delivered' GROUP BY kind", (pid,)):
            paid[row[0]] = {"calls": row[1], "cost": row[2] or 0.0}
        out.append(Project(
            pid=pid, tid=tid, name=p[2] or pid, path=pdir, exists=os.path.isdir(pdir),
            sizes=sizes, total=sum(sizes.values()), newest=newest,
            release_id=(rel[0] if rel else ""), phase=(rel[1] if rel else ""),
            video_id=(rel[3] if rel else ""),
            published_at=(rel[4] if rel and rel[1] == SAFE_PHASE else 0.0),
            uploaded_at=uploaded_at,
            busy=busy, paid=paid))
    return out


# ─────────────────────────────── решить ───────────────────────────────

def db_policy(c) -> dict:
    """Что записано в базу (`dev/retention.py --policy`). Запасной путь, не главный."""
    row = c.execute("SELECT v FROM admin_state WHERE k=?", (STATE_KEY,)).fetchone()
    try:
        data = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def env_policy(env=None) -> dict:
    """Что задано в `.env`. Пустая переменная — это «не задано», а не «ноль дней»."""
    env = os.environ if env is None else env
    out = {}
    for key, name in (("strip_after_days", ENV_STRIP),
                      ("uploaded_strip_after_days", ENV_UPLOADED_STRIP),
                      ("drop_after_days", ENV_DROP)):
        days = _days(env.get(name))
        if days is not None:
            out[key] = days
    return out


def policy(c=None, env=None) -> dict:
    """Действующее правило. `.env` выигрывает у базы; ключ `source` говорит, чьё оно.

    Смешивать источники нельзя: срок очистки из `.env` и срок удаления из базы дали бы
    правило, которого никто не задавал. Поэтому если `.env` сказал хоть слово — база
    молчит целиком, и интерфейс это показывает.
    """
    env_pol = env_policy(env)
    if env_pol:
        return env_pol | {"source": "env"}
    db_pol = db_policy(c) if c is not None else {}
    return (db_pol | {"source": "db"}) if db_pol else {}


def auto_enabled(env=None) -> bool:
    """Убирать ли на самом деле. Всё, кроме явного «да», означает «нет»."""
    env = os.environ if env is None else env
    return str(env.get(ENV_AUTO, "")).strip().lower() in ("1", "yes", "on", "true", "да")


def every_sec(env=None) -> int:
    """Как часто проверять. Ниже пяти минут не опускаемся: проекты стареют днями."""
    env = os.environ if env is None else env
    try:
        n = int(str(env.get(ENV_EVERY, "")).strip())
    except ValueError:
        return EVERY_DEFAULT
    return max(EVERY_MIN, n)


def _uploaded_verdict(p: Project, upload_strip_days, now: float) -> tuple:
    """Что делать с проектом, который ещё не вышел.

    Единственное, что здесь разрешено, — убрать кадры и озвучку у ролика, который уже
    лежит у Google и ждёт своей даты. Три условия, и все обязательны: фаза `scheduled`,
    номер ролика на руках и прошёл заданный владельцем срок после заливки. Нет срока —
    ничего не делаем: это та же осторожность, что и с остальными порогами.

    Готовый mp4 не трогаем ни при каком сроке. До публикации ролик ещё может
    понадобиться перезалить, и файл на диске — разница между «перезалили» и
    «собираем и платим заново».
    """
    if p.phase != UPLOADED_PHASE or not p.video_id:
        return (p, KEEP,
                f"не опубликован (фаза «{p.phase or 'без выпуска'}»): "
                f"ролик существует только у нас", 0)
    if upload_strip_days is None:
        return (p, KEEP, "залит, но срок очистки после заливки владельцем не задан", 0)
    if not p.uploaded_at:
        return (p, KEEP, "залит, но момент заливки не подтверждён заданием", 0)
    age_days = (now - p.uploaded_at) / 86400.0
    if age_days >= upload_strip_days and p.strippable:
        return (p, STRIP,
                f"залит на YouTube {age_days:.0f} дн. назад, ждёт даты выхода "
                f"(порог очистки после заливки {upload_strip_days}); "
                f"готовый ролик остаётся", p.strippable)
    if not p.strippable:
        return (p, KEEP, "залит, убирать уже нечего", 0)
    return (p, KEEP, f"залит {age_days:.0f} дн. назад — рано", 0)


def plan(projects, pol: dict, now=None) -> list:
    """[(проект, действие, почему, сколько освободит)]. Действие: keep | strip | drop.

    Ни одно правило не может пересилить три запрета: занят работой, не опубликован,
    каталога нет. Они проверяются до правил, а не после.
    """
    now = now or time.time()
    strip_days = _days(pol.get("strip_after_days"))
    upload_strip_days = _days(pol.get("uploaded_strip_after_days"))
    drop_days = _days(pol.get("drop_after_days"))
    out = []
    for p in projects:
        if not p.exists:
            out.append((p, KEEP, "каталога нет на диске", 0))
            continue
        if p.busy:
            out.append((p, KEEP, f"идёт работа: активных задач {p.busy}", 0))
            continue
        if p.phase != SAFE_PHASE:
            out.append(_uploaded_verdict(p, upload_strip_days, now))
            continue
        if strip_days is None and drop_days is None:
            out.append((p, KEEP, "срок хранения владельцем не задан", 0))
            continue
        age_days = (now - (p.published_at or now)) / 86400.0
        if drop_days is not None and age_days >= drop_days:
            out.append((p, "drop",
                        f"опубликован {age_days:.0f} дн. назад "
                        f"(порог удаления {drop_days})", p.total))
            continue
        if strip_days is not None and age_days >= strip_days and p.strippable:
            out.append((p, STRIP,
                        f"опубликован {age_days:.0f} дн. назад "
                        f"(порог очистки {strip_days})", p.strippable))
            continue
        out.append((p, KEEP, f"опубликован {age_days:.0f} дн. назад — рано", 0))
    return out


def price_of(p: Project, action: str) -> dict:
    """Что будет потеряно безвозвратно: сколько оплаченных вызовов и на какую сумму.

    Отвечает на вопрос ТЗ «потрачены ли деньги» до нажатия кнопки, а не после.
    """
    codes = [c for c, _n, fate, _pat in CATEGORIES
             if action == "drop" or fate == STRIP]
    calls, cost, kinds = 0, 0.0, []
    for code in codes:
        kind = PAID_BY.get(code)
        rec = (p.paid or {}).get(kind or "")
        if not rec or not p.sizes.get(code):
            continue
        calls += rec["calls"]
        cost += rec["cost"]
        kinds.append(f"{code}: {rec['calls']}")
    return {"calls": calls, "cost": cost, "detail": ", ".join(kinds)}


# ─────────────────────────────── сделать ───────────────────────────────

def apply(c, actions, dry_run=True, log=print) -> dict:
    """Выполнить план. По умолчанию не выполняет — печатает, что сделал бы.

    Строка проекта из базы удаляется только вместе с каталогом (`drop`) и только
    после того, как каталог действительно исчез: иначе получится проект-призрак,
    который в интерфейсе есть, а на диске нет.
    """
    done = {"strip": 0, "drop": 0, "freed": 0, "errors": []}
    for p, action, why, freed in actions:
        if action == KEEP:
            continue
        if dry_run:
            log(f"  [показ] {action:<5} {p.pid}  {_size(freed)}  — {why}")
            done[action] += 1
            done["freed"] += freed
            continue
        try:
            if action == STRIP:
                removed = _strip(p)
                log(f"  очищен {p.pid}: {_size(removed)}")
                done["freed"] += removed
            else:
                root = os.path.realpath(p.path)
                if not _inside(root, p.tid, p.pid):
                    raise ValueError(f"путь не похож на каталог проекта: {root}")
                shutil.rmtree(root)
                if os.path.exists(root):
                    raise OSError("каталог не исчез")
                c.execute("DELETE FROM projects WHERE id=? AND tenant_id=?",
                          (p.pid, p.tid))
                c.commit()
                log(f"  удалён {p.pid}: {_size(p.total)}")
                done["freed"] += p.total
            done[action] += 1
        except Exception as exc:                                   # noqa: BLE001
            done["errors"].append(f"{p.pid}: {type(exc).__name__}: {exc}")
            log(f"  ОШИБКА {p.pid}: {exc}")
    return done


def _strip(p: Project) -> int:
    """Удалить только промежуточное. Каждый путь проверяется отдельно."""
    root = os.path.realpath(p.path)
    freed = 0
    for code, _name, fate, patterns in CATEGORIES:
        if fate != STRIP:
            continue
        for parts, rec in patterns:
            for f in glob.glob(os.path.join(root, *parts), recursive=rec):
                full = os.path.realpath(f)
                if not full.startswith(root + os.sep) or not os.path.isfile(full):
                    continue
                try:
                    size = os.path.getsize(full)
                    os.remove(full)
                    freed += size
                except OSError:
                    continue
    # Пустые каталоги после уборки убираем — но только пустые.
    for dirpath, dirs, files in os.walk(root, topdown=False):
        if dirpath != root and not dirs and not files:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return freed


def _inside(root: str, tid: str, pid: str) -> bool:
    """Каталог проекта выглядит как .../<tid>/projects/<pid> — иначе не наш."""
    parts = root.rstrip(os.sep).split(os.sep)
    return len(parts) >= 3 and parts[-1] == pid and parts[-2] == "projects" \
        and parts[-3] == tid


def sweep(c, tenants_dir: str, tenant=None, now=None, dry_run=None, env=None,
          log=print) -> dict:
    """Один проход автоуборки: посмотреть, решить, сделать — и записать, что вышло.

    `dry_run` по умолчанию берётся из `VS_RETENTION_AUTO`: без него проход только считает.
    Итог кладётся в базу (`admin_state.retention_last`) — чтобы в интерфейсе было видно,
    когда убирали в последний раз и сколько оплаченного при этом исчезло. Без этой записи
    автоуборка была бы единственным местом системы, которое тратит чужое и не отчитывается.
    """
    now = now or time.time()
    auto = auto_enabled(env)
    if dry_run is None:
        dry_run = not auto
    pol = policy(c, env)
    acts = plan(scan(c, tenants_dir, tenant), pol, now)
    lost_calls, lost_cost = 0, 0.0
    for p, action, _why, _freed in acts:
        if action == KEEP:
            continue
        price = price_of(p, action)
        lost_calls += price["calls"]
        lost_cost += price["cost"]
    out = apply(c, acts, dry_run=dry_run, log=log)
    res = {
        "at": now, "dry_run": bool(dry_run), "auto": auto,
        "policy": {k: v for k, v in pol.items() if k != "source"},
        "source": pol.get("source", ""),
        "planned": sum(1 for _p, a, _w, _f in acts if a != KEEP),
        "strip": out["strip"], "drop": out["drop"], "freed": out["freed"],
        "lost_calls": lost_calls, "lost_cost": lost_cost,
        "errors": out["errors"], "tenant": tenant or "",
    }
    res["line"] = sweep_line(res)
    _save_last(c, res)
    return res


def sweep_line(res: dict) -> str:
    """Одна строка для человека — она же уходит в телеграм."""
    if not res.get("policy"):
        return "срок хранения не задан — ничего не убирается"
    if not res["planned"]:
        return "убирать нечего"
    price = (f"; безвозвратно ушло {res['lost_calls']} оплаченных вызовов"
             if res["lost_calls"] else "")
    if res["dry_run"]:
        return (f"под очистку {res['strip']}, под удаление {res['drop']}, "
                f"освободилось бы {_size(res['freed'])}{price}. "
                f"Ничего не тронуто: {ENV_AUTO} выключен")
    return (f"очищено {res['strip']}, удалено {res['drop']}, "
            f"освобождено {_size(res['freed'])}{price}")


def _save_last(c, res: dict) -> None:
    try:
        c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                  (LAST_KEY, json.dumps(res, ensure_ascii=False), res["at"]))
        c.commit()
    except Exception:                                              # noqa: BLE001
        pass          # уборка уже сделана; неудачная запись отчёта её не отменяет


def last_sweep(c) -> dict:
    row = c.execute("SELECT v FROM admin_state WHERE k=?", (LAST_KEY,)).fetchone()
    try:
        data = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def free_space(tenants_dir: str) -> dict:
    probe = os.path.abspath(tenants_dir)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        u = shutil.disk_usage(probe)
    except OSError as exc:
        return {"path": probe, "error": str(exc)}
    return {"path": probe, "total": u.total, "used": u.used, "free": u.free,
            "free_gb": round(u.free / GB, 1)}


def _days(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _size(n) -> str:
    n = int(n or 0)
    if n < MB:
        return f"{n / 1024:.0f} КБ"
    if n < GB:
        return f"{n / MB:.0f} МБ"
    return f"{n / GB:.2f} ГБ"
