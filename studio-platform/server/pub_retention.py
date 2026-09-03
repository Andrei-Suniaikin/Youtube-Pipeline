#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Срок хранения и уборка проектов (ТЗ §03/12: «механизма чистки нет вообще»).

Проект весит около 518 МБ, и место кончается само. Но «удалять старое» — это решение
не про технику, а про деньги: внутри проекта лежат кадры, за которые заплачено, и
озвучка, за которую заплачено. Поэтому здесь три отдельные вещи:

  посмотреть  — что лежит, сколько весит, сколько за это заплачено
  решить      — по правилу владельца, а не по нашему вкусу
  сделать     — с проверкой каждого пути, и по умолчанию не делая ничего

**Правила по умолчанию нет.** Пока владелец не назвал сроки (`admin_state.retention`),
`plan()` возвращает «оставить» для всего и говорит почему. Придумывать срок хранения
за владельца нельзя: ошибка здесь необратима и стоит денег.

Два уровня уборки, и разница между ними принципиальна:

  strip  удалить промежуточное — кадры, клипы, куски озвучки. Пять артефактов выпуска
         остаются. Это освобождает почти весь объём и оставляет ролик воспроизводимым
         для зрителя, но НЕ воспроизводимым для нас: пересобрать его заново будет
         стоить столько же, сколько он стоил в первый раз.
  drop   удалить проект целиком вместе с готовым mp4. Только когда ролик опубликован
         и подтверждён публичным — то есть существует уже не только у нас.

Условие для обоих — фаза `published`. Не `done` и не `scheduled`: `done` в этой системе
означает «расписание проверено», а не «ролик вышел» (известная ловушка проекта).
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import time

STATE_KEY = "retention"
GB = 1024 ** 3
MB = 1024 ** 2

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


class Project:
    __slots__ = ("pid", "tid", "name", "path", "exists", "sizes", "total", "newest",
                 "phase", "release_id", "video_id", "published_at", "busy", "paid")

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
            busy=busy, paid=paid))
    return out


# ─────────────────────────────── решить ───────────────────────────────

def policy(c) -> dict:
    """Правило владельца. Пустое — значит правила нет, и это не то же самое, что ноль."""
    row = c.execute("SELECT v FROM admin_state WHERE k=?", (STATE_KEY,)).fetchone()
    try:
        data = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def plan(projects, pol: dict, now=None) -> list:
    """[(проект, действие, почему, сколько освободит)]. Действие: keep | strip | drop.

    Ни одно правило не может пересилить три запрета: занят работой, не опубликован,
    каталога нет. Они проверяются до правил, а не после.
    """
    now = now or time.time()
    strip_days = _days(pol.get("strip_after_days"))
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
            out.append((p, KEEP,
                        f"не опубликован (фаза «{p.phase or 'без выпуска'}»): "
                        f"ролик существует только у нас", 0))
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
