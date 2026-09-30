# -*- coding: utf-8 -*-
"""Уборка промежуточных файлов готового ролика (цикл 04, гипотеза Г11).

Гарантии модуля (проверяются tests/test_cleanup.py):
1. Никогда не удаляются: итоговый ролик out/video.mp4, кадры frames/NNNNN.png, озвучка,
   тексты и раскадровка, листы героев и мест, эталоны интерфейса, вердикты судьи,
   квитанции генераций (*.generation.json), истории выбора (*.selected.json),
   состояния пересдач (state.json) и задания, на которые ссылается выбранный кадр,
   крупные планы говорящего frames/NNNNN_speaker.png, врезки frames/NNNNN_extra.png
   и копии оригиналов кадров,
   перерисованных с облаком реплики (круг r30 в frames/.repair).
   Удаляются только файлы из явного перечня категорий ниже, по точному шаблону имени.
2. Путь к файлу проходится без символических ссылок: ссылка в любом звене или сам
   файл-ссылка — отказ с записью в журнал. Выйти за пределы папки проекта нельзя.
3. Проект убирается, только если ролик готов (видео и приёмка на месте, все кадры целы),
   последняя задача завершилась успешно и нет задачи в очереди или в работе. Условие
   проверяется перед каждой категорией, а появление задачи у ролика — перед каждым файлом:
   после нажатия «Пересдать» удаляется не больше одного файла, чья проверка уже прошла.
4. Этап «сразу» удаляет только то, что никто не читает после готовности:
   out/silent.mp4 (видеопоток побайтно тот же, что в out/video.mp4), out/.stale
   (старые версии, отодвинутые пересдачей кадра) и соседние кадры-ссылки законченных
   пересдач из интерфейса.
5. Этап «черновики» удаляет клипы сборки, картинки кандидатов и копии оригиналов
   пересдачи и задания отклонённых кандидатов — только когда с последней задачи проекта
   прошло MANGA_CLEANUP_DRAFT_DAYS суток. По умолчанию черновики не удаляются (never):
   срок выбирает владелец. Копия выбранной картинки удаляется, только если кадр
   побайтно с ней совпадает.
6. Каждое удаление сначала записывается в журнал проекта cleanup_log.jsonl с размером
   и только потом выполняется. Журнал не открылся — не удаляется ничего.
7. Повторный запуск безопасен: удалённое уже не находится, остальное не трогается.
8. Ошибка в одном ролике не останавливает уборку остальных: она пишется в журнал службы,
   отметка этапа не ставится, следующий проход повторяет этап.
9. Остановка службы прерывает уборку между файлами; незаконченный этап не отмечается
   сделанным и доделывается следующим проходом.
10. Этап «после выдачи» (раунд 2 scale20, часть «Диск и выдача») включается только настройкой:
   MANGA_CLEANUP_DELIVERED_HOURS — через столько часов после отметки «скачано» (отметка
   относится к той версии видео, что лежит сейчас), MANGA_CLEANUP_DONE_DAYS — через столько
   суток после готовности, даже без отметки. По умолчанию обе never: без новых настроек
   ничего сверх этапов «сразу» и «черновики» не удаляется.
   Удаляет только картинки и видео-черновики: кадры frames/NNNNN.png и крупные планы
   говорящего, их чистые копии frames/clean, все картинки frames/.repair, картинки пересдач retries/, облачка derived/,
   клипы сборки и всё, что удаляет этап «сразу». Никогда не удаляет: out/video.mp4, обложку
   и прочее в out/ (кроме silent.mp4 и out/.stale), озвучку, тексты, листы героев, места,
   планы, эталоны, исходник, вердикты судьи и ни одного JSON, кроме тех, что убирают и прежние
   этапы (задания клипов сборки, старые версии в out/.stale): квитанции, истории выбора,
   задания кадров и пересдач остаются. Отметка «убран» пишется в базу очереди ДО первого удаления и в той же
   записи, что проверяет отсутствие задач ролика: после неё «Повторить» и пересдача кадра
   отказывают с объяснением и ничего не покупают (web_queue.QueueStore.enqueue).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import time
import uuid

from web_queue import read_json, write_json

IMMEDIATE = "immediate"
DRAFTS = "drafts"
ARCHIVE = "archive"
PHASE_NAMES = {IMMEDIATE: "сразу", DRAFTS: "черновики", ARCHIVE: "после выдачи"}
JOURNAL = "cleanup_log.jsonl"
STATE = "cleanup_state.json"
SWEEP_EVERY = 3600

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_CLIP = re.compile(r"^clip_\d{5}\.mp4(\.input\.json)?$")
_CLIP_VIDEO_PATH = re.compile(r"^clips/clip_\d{5}\.mp4$")
_REPAIR_PNG = re.compile(r"^(r\d+_\d{5}(_[0-9a-f]{16})?|orig_\d{5}(_r\d+_\d+)?)\.png$")
_REPAIR_JOB = re.compile(r"^r\d+_\d{5}(_[0-9a-f]{16})?\.png\.job\.json$")
_FRAME_PNG = re.compile(r"^\d{5}\.png$")
# Черновики обложки: ответы генератора (по 2 МБ), копии «как было до ремонта», копии
# каждого прохода (cover_round_N — из них выбирается лучший), осветлённый проход (cover_lit_N),
# непроверенная вклейка ремонта (*_trial), сетка и вырезки для проверки глазами.
# Сама обложка (cover.png), её превью, cover.json, cover_prompts.json и лист для глаз —
# не черновики и не удаляются никогда. Квитанции (*.generation.json) тоже: по ним
# считается расход единиц, а весят они байты против двух мегабайт картинки. Пока уборка
# их забирала, отчёт о тратах после уборки молча уменьшался.
# Варианты шага «3 варианта + выбор» (cover_var_К_N и их копии с надписью кода) — тоже черновики,
# но только отложенной уборки: пока они есть, оператор может «взять другой» на странице.
_COVER_DRAFT = re.compile(r"^cover_(raw|fix|before_fix|round|lit)(_\d+)?(_trial)?\.png$"
                          r"|^cover_var_\d+_\d+(_final|_fit\.tmp)?\.png$"
                          r"|^cover(_fix_\d+_trial)?_(grid|fan|look|face\d+(?:_donor)?)\.jpg$")
# Перерисовка готового кадра с облаком реплики — круг пересдачи 30 (pipeline.QUOTE_ROUND).
# Копия оригинала без облака — единственный путь назад, черновиком она не считается.
_QUOTE_PROMOTION = re.compile(r"^r30_\d{5}(_[0-9a-f]{16})?\.png\.promotion\.json$")
_SELECTED = re.compile(r"^\d{5}\.png\.selected\.json$")
# Этап «после выдачи»: кадр и крупный план говорящего; в .repair, retries и derived — любая картинка.
_FRAME_MEDIA = re.compile(r"^\d{5}(_speaker|_extra)?\.png$")
_ANY_PNG = re.compile(r"^[A-Za-z0-9_.-]+\.png$")


class Refused(Exception):
    """Файл нельзя удалить безопасно: ссылка, не обычный файл, путь вне проекта."""


def settings(env=None):
    """Настройки уборки из окружения; неверное значение — громкая ошибка при старте."""
    env = os.environ if env is None else env
    enabled = str(env.get("MANGA_CLEANUP", "1")).strip()
    if enabled not in ("0", "1"):
        raise ValueError("MANGA_CLEANUP: укажите 1 (уборка включена) или 0 (выключена)")
    raw = str(env.get("MANGA_CLEANUP_DRAFT_DAYS", "never")).strip().lower()
    if raw == "never":
        days = None
    elif raw.isdigit() and 1 <= int(raw) <= 3650:
        days = int(raw)
    else:
        raise ValueError("MANGA_CLEANUP_DRAFT_DAYS: укажите число суток от 1 до 3650 или never")
    keep = [token for token in re.split(r"[\s,]+", str(env.get("MANGA_CLEANUP_KEEP", ""))) if token]
    wrong = [token for token in keep if not _HEX32.fullmatch(token)]
    if wrong:
        raise ValueError("MANGA_CLEANUP_KEEP: номера роликов через запятую, неверно: " + ", ".join(wrong[:3]))
    return {"enabled": enabled == "1", "draft_days": days, "keep": frozenset(keep)}


def delivery_settings(env=None):
    """Уборка после выдачи и защита диска (раунд 2 scale20); по умолчанию всё выключено."""
    env = os.environ if env is None else env
    done_days = _never_or_int(env, "MANGA_CLEANUP_DONE_DAYS", 1, 3650, "число суток от 1 до 3650 или never")
    delivered_hours = _never_or_int(env, "MANGA_CLEANUP_DELIVERED_HOURS", 0, 8760, "число часов от 0 до 8760 или never")
    raw_disk = str(env.get("MANGA_DISK_MIN_GB", "off")).strip().lower()
    if raw_disk in ("", "off"):
        disk = None
    elif raw_disk.isdigit() and 1 <= int(raw_disk) <= 100000:
        disk = int(raw_disk)
    else:
        raise ValueError("MANGA_DISK_MIN_GB: укажите число ГБ от 1 до 100000 или off")
    return {"done_days": done_days, "delivered_hours": delivered_hours, "disk_min_gb": disk}


def _never_or_int(env, name, low, high, what):
    raw = str(env.get(name, "never")).strip().lower()
    if raw in ("", "never"):
        return None
    if raw.isdigit() and low <= int(raw) <= high:
        return int(raw)
    raise ValueError(f"{name}: укажите {what}")


# ---------- безопасный доступ к путям ----------
def _lstat_rel(root, rel):
    """(stat, None) для пути без ссылок; (None, None) если его нет; (None, причина) если нельзя."""
    parts = rel.split("/")
    current = root
    st = None
    for i, part in enumerate(parts):
        if part in ("", ".", ".."):
            return None, "недопустимое имя в пути"
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return None, None
        if stat.S_ISLNK(st.st_mode):
            return None, "символическая ссылка: " + "/".join(parts[:i + 1])
        if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            return None, "в пути не каталог: " + "/".join(parts[:i + 1])
    return st, None


def _open_parent(root, parts):
    """Дескриптор каталога-родителя, открытого без перехода по ссылкам в любом звене."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in parts[:-1]:
            try:
                following = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise Refused("символическая ссылка или не каталог: " + part) from None
                raise
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def _unlink(root, rel):
    """Удаляет один обычный файл внутри root. Возвращает его stat до удаления."""
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        raise Refused("недопустимое имя в пути")
    fd = _open_parent(root, parts)
    try:
        st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            raise Refused("символическая ссылка")
        if not stat.S_ISREG(st.st_mode):
            raise Refused("не обычный файл")
        os.unlink(parts[-1], dir_fd=fd)
        return st
    finally:
        os.close(fd)


def _read_bytes(root, rel):
    parts = rel.split("/")
    fd = _open_parent(root, parts)
    try:
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused("символическая ссылка") from None
        raise
    finally:
        os.close(fd)
    with os.fdopen(file_fd, "rb") as stream:
        return stream.read()


def _read_json(root, rel):
    """JSON внутри проекта без перехода по ссылкам; None, если файла нет или он нечитаем."""
    st, why = _lstat_rel(root, rel)
    if why or st is None or not stat.S_ISREG(st.st_mode):
        return None
    try:
        return json.loads(_read_bytes(root, rel).decode("utf-8"))
    except (OSError, ValueError, Refused):
        return None


def _sha(root, rel):
    try:
        return hashlib.sha256(_read_bytes(root, rel)).hexdigest()
    except (OSError, Refused):
        return None


# ---------- план ----------
class _Plan:
    def __init__(self, root):
        self.root = root
        self.items, self.refused, self.kept = [], [], []

    def refuse(self, rel, reason):
        self.refused.append({"path": rel, "reason": reason})

    def listing(self, rel_dir):
        """[(имя, entry)] каталога; ссылка вместо каталога — отказ."""
        st, why = _lstat_rel(self.root, rel_dir)
        if why:
            self.refuse(rel_dir, why)
            return []
        if st is None:
            return []
        if not stat.S_ISDIR(st.st_mode):
            self.refuse(rel_dir, "ожидался каталог")
            return []
        with os.scandir(os.path.join(self.root, rel_dir)) as entries:
            return sorted(((entry.name, entry) for entry in entries), key=lambda pair: pair[0])

    def add(self, category, rel):
        st, why = _lstat_rel(self.root, rel)
        if why:
            self.refuse(rel, why)
            return
        if st is None:
            return
        if not stat.S_ISREG(st.st_mode):
            self.refuse(rel, "не обычный файл")
            return
        self.items.append({"category": category, "path": rel, "bytes": st.st_size, "links": st.st_nlink})

    def add_image(self, category, rel, refs):
        ref = refs.get(rel)
        if ref is None:
            return self.add(category, rel)
        st, why = _lstat_rel(self.root, rel)
        if why:
            return self.refuse(rel, why)
        if st is None:
            return None
        expected = ref.get("sha")
        if expected and _sha(self.root, ref["frame"]) == expected == _sha(self.root, rel):
            return self.add(category, rel)
        self.kept.append({"path": rel, "reason": "единственная копия выбранной картинки кадра: кадр с ней не совпадает"})
        return None

    def files_under(self, rel_dir):
        out = []
        for name, entry in self.listing(rel_dir):
            rel = f"{rel_dir}/{name}"
            if entry.is_symlink():
                self.refuse(rel, "символическая ссылка")
            elif entry.is_dir(follow_symlinks=False):
                out.extend(self.files_under(rel))
            else:
                out.append(rel)
        return out

    def done_retries(self):
        for name, entry in self.listing("retries"):
            if not _HEX32.fullmatch(name):
                continue
            if entry.is_symlink():
                self.refuse(f"retries/{name}", "символическая ссылка")
                continue
            state = _read_json(self.root, f"retries/{name}/state.json")
            frame_id = state.get("frame_id") if isinstance(state, dict) else None
            if state and state.get("stage") == "done" and type(frame_id) is int and frame_id >= 1:
                yield name, frame_id

    def quote_originals(self):
        """Копии оригиналов, заменённых кадром с облаком реплики; None — если запись не читается."""
        keep = set()
        for name, _ in self.listing("frames/.repair"):
            if not _QUOTE_PROMOTION.fullmatch(name):
                continue
            rel = f"frames/.repair/{name}"
            data = _read_json(self.root, rel)
            if not isinstance(data, dict):
                self.refuse(rel, "запись перерисовки с облаком не читается — копии оригиналов не трогаю")
                return None
            if isinstance(data.get("original_copy"), str) and data["original_copy"]:
                keep.add(posixpath.normpath(data["original_copy"]))
        return keep

    def selected_refs(self):
        """{путь: {frame, sha}} из истории выбора кадров; None, если историю прочитать нельзя."""
        refs = {}
        before = len(self.refused)
        names = [name for name, _ in self.listing("frames") if _SELECTED.fullmatch(name)]
        if len(self.refused) != before:
            return None
        for name in names:
            rel = f"frames/{name}"
            data = _read_json(self.root, rel)
            if not isinstance(data, dict):
                self.refuse(rel, "история выбора кадра не читается — черновики пересдач не трогаю")
                return None
            for key in ("candidate_path", "job_path"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    refs[posixpath.normpath(value)] = {"frame": f"frames/{name[:9]}", "sha": data.get("image_sha256")}
        return refs


def plan(root, phase):
    """Что удалит этап, ничего не меняя: {items, refused, kept}."""
    if phase not in PHASE_NAMES:
        raise ValueError("Неизвестный этап уборки")
    p = _Plan(root)
    if phase == IMMEDIATE:
        _immediate(p)
    elif phase == ARCHIVE:
        _immediate(p)
        for name, _ in p.listing("clips"):
            if _CLIP.fullmatch(name):
                p.add("clips", f"clips/{name}")
        for name, entry in p.listing("frames"):
            if _FRAME_MEDIA.fullmatch(name) and not entry.is_dir(follow_symlinks=False):
                p.add("frames", f"frames/{name}")
        for name, entry in p.listing("frames/.repair"):
            if _ANY_PNG.fullmatch(name) and not entry.is_dir(follow_symlinks=False):
                p.add("repair_images", f"frames/.repair/{name}")
        # Чистые копии кадров (шаг «Чистка», denoise.py): картинки; их отпечатки (.clean.json) остаются.
        for name, entry in p.listing("frames/clean"):
            if _FRAME_MEDIA.fullmatch(name) and not entry.is_dir(follow_symlinks=False):
                p.add("frames_clean", f"frames/clean/{name}")
        for name, entry in p.listing("retries"):
            if not _HEX32.fullmatch(name):
                continue
            if entry.is_symlink():
                p.refuse(f"retries/{name}", "символическая ссылка")
                continue
            for rel in p.files_under(f"retries/{name}"):
                if _ANY_PNG.fullmatch(rel.rsplit("/", 1)[-1]):
                    p.add("retry_images", rel)
        for rel in p.files_under("derived"):
            if _ANY_PNG.fullmatch(rel.rsplit("/", 1)[-1]):
                p.add("derived", rel)
        # One path may come twice (a neighbour of a finished retry is also a retry image): delete once.
        seen = set()
        p.items = [item for item in p.items if not (item["path"] in seen or seen.add(item["path"]))]
    else:
        for name, _ in p.listing("clips"):
            if _CLIP.fullmatch(name):
                p.add("clips", f"clips/{name}")
        for name, _ in p.listing("out"):
            if _COVER_DRAFT.fullmatch(name):
                p.add("cover_drafts", f"out/{name}")
        for name, _ in p.listing("source"):                 # сетка донора для проверки глазами
            if name == "cover_donor_grid.jpg":
                p.add("cover_drafts", f"source/{name}")
        for name, _ in p.listing("source/cover_locks"):     # вырезки старого способа (до 18.09)
            if name.startswith("lock_") and name.endswith(".png"):
                p.add("cover_drafts", f"source/cover_locks/{name}")
        for name, _ in p.listing("source/cover_heroes"):    # листы героев, собранные для генератора
            if name.endswith(".png"):
                p.add("cover_drafts", f"source/cover_heroes/{name}")
        refs = p.selected_refs()
        quote_originals = p.quote_originals()
        if refs is not None:
            for name, _ in p.listing("frames/.repair"):
                rel = f"frames/.repair/{name}"
                if _REPAIR_PNG.fullmatch(name) and name.startswith("orig_") and (
                        quote_originals is None or rel in quote_originals):
                    p.kept.append({"path": rel, "reason": "оригинал кадра без облака реплики"})
                elif _REPAIR_PNG.fullmatch(name):
                    p.add_image("repair_images", rel, refs)
                elif _REPAIR_JOB.fullmatch(name) and rel not in refs:
                    p.add("repair_jobs", rel)
            for name, frame_id in p.done_retries():
                base = f"retries/{name}"
                candidate = f"{base}/candidates/{frame_id:05d}.png"
                p.add_image("retry_drafts", f"{base}/original.png", refs)
                p.add_image("retry_drafts", candidate, refs)
                if candidate + ".job.json" not in refs:
                    p.add("retry_drafts", candidate + ".job.json")
    return {"items": p.items, "refused": p.refused, "kept": p.kept}


def _immediate(p):
    p.add("silent", "out/silent.mp4")
    for name, entry in p.listing("out/.stale"):
        rel = f"out/.stale/{name}"
        if entry.is_symlink():
            p.refuse(rel, "символическая ссылка")
        elif _HEX32.fullmatch(name) and entry.is_dir(follow_symlinks=False):
            for file_rel in p.files_under(rel):
                p.add("stale", file_rel)
    for name, frame_id in p.done_retries():
        base = f"retries/{name}/candidates"
        for file_name, _ in p.listing(base):
            if _FRAME_PNG.fullmatch(file_name) and int(file_name[:5]) != frame_id:
                p.add("retry_neighbors", f"{base}/{file_name}")


# ---------- условия ----------
def _project_root(store, project_id):
    """Настоящий каталог проекта или причина отказа."""
    resolved = str(store.project_path(project_id))
    lexical = os.path.join(str(store.root), "projects", project_id)
    try:
        st = os.lstat(lexical)
    except FileNotFoundError:
        return None, "папки ролика нет"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return None, "папка ролика — символическая ссылка или не каталог"
    if os.path.realpath(lexical) != resolved:
        return None, "папка ролика указывает за пределы очереди"
    return resolved, None


def _blocker(store, project_id):
    project = store.get_project(project_id)
    if project["status"] != "complete":
        return "у ролика есть задача в очереди, в работе или с ошибкой"
    if not project["video_ready"]:
        return "готового видео нет"
    return None


def _active(store, project_id):
    with store._db() as db:
        return db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('pending','running') LIMIT 1",
                          (project_id,)).fetchone() is not None


def _last_finished(store, project_id):
    with store._db() as db:
        return db.execute("SELECT MAX(finished) FROM jobs WHERE project_id=? AND action != 'title'",
                          (project_id,)).fetchone()[0]


class _Journal:
    def __init__(self, root, run_id):
        fd = os.open(os.path.join(root, JOURNAL), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        self.stream = os.fdopen(fd, "a", encoding="utf-8")
        self.run_id = run_id

    def write(self, **record):
        self.stream.write(json.dumps({"time": round(time.time(), 3), "run": self.run_id, **record},
                                     ensure_ascii=False) + "\n")
        self.stream.flush()

    def close(self):
        try:
            os.fsync(self.stream.fileno())
        finally:
            self.stream.close()


# ---------- выполнение ----------
def clean(store, project_id, phase, config, now=None, log=print, stop=None):
    """Один этап уборки одного ролика. Возвращает итог; причина пропуска — в skipped."""
    now = time.time() if now is None else now
    result = {"project_id": project_id, "phase": phase, "deleted": 0, "freed_bytes": 0,
              "refused": 0, "kept": 0, "errors": 0, "skipped": None, "stopped": None}
    if phase not in PHASE_NAMES:
        raise ValueError("Неизвестный этап уборки")
    if project_id in config["keep"]:
        result["skipped"] = "ролик в списке MANGA_CLEANUP_KEEP"
        return result
    root, why = _project_root(store, project_id)
    if why:
        result["skipped"] = why
        return result
    blocker = _blocker(store, project_id)
    if blocker:
        result["skipped"] = blocker
        return result
    last = _last_finished(store, project_id)
    archive_why = None
    if phase == ARCHIVE:
        archived = store.archive_info(project_id)
        if archived and archived.get("finished"):
            result["skipped"] = "кадры и черновики ролика уже убраны после выдачи"
            return result
        archive_why = archived["reason"] if archived else archive_reason(store, project_id, config, now, last)
        if not archive_why:
            result["skipped"] = "уборка после выдачи ещё не пора или выключена"
            return result
    if phase == DRAFTS:
        if config["draft_days"] is None:
            result["skipped"] = "черновики хранятся бессрочно (MANGA_CLEANUP_DRAFT_DAYS=never)"
            return result
        if last is None or now - last < config["draft_days"] * 86400:
            result["skipped"] = "срок хранения черновиков ещё не прошёл"
            return result
    try:
        journal = _Journal(root, uuid.uuid4().hex)
    except OSError as exc:
        result["errors"] = 1
        result["skipped"] = f"журнал уборки не открылся, ничего не удалено: {type(exc).__name__}: {exc}"
        log(f"уборка {project_id}: {result['skipped']}")
        return result
    if phase == ARCHIVE and not store.begin_archive(project_id, archive_why, now=now):
        # The mark and the «no job» check are one database write: a retry pressed a moment ago wins.
        journal.close()
        result["skipped"] = "у ролика появилась задача"
        return result
    planned = plan(root, phase)
    try:
        journal.write(event="start", phase=phase, planned=len(planned["items"]),
                      planned_bytes=sum(item["bytes"] for item in planned["items"]), last_job_finished=last)
        for refusal in planned["refused"]:
            journal.write(event="refused", **refusal)
        for kept in planned["kept"]:
            journal.write(event="kept", **kept)
        result["refused"], result["kept"] = len(planned["refused"]), len(planned["kept"])
        category = None
        for item in planned["items"]:
            if stop is not None and stop():
                result["stopped"] = "служба останавливается"
            elif item["category"] != category:
                category = item["category"]
                result["stopped"] = _blocker(store, project_id)
            elif _active(store, project_id):
                # One queue read per file (~0.3 ms, a new SQLite connection) instead of a flag in
                # memory: a retry may be queued by another process that shares this queue root.
                result["stopped"] = "у ролика появилась задача"
            if result["stopped"]:
                break
            journal.write(event="delete", **item)
            try:
                st = _unlink(root, item["path"])
            except Refused as exc:
                result["refused"] += 1
                journal.write(event="refused", path=item["path"], reason=str(exc))
                continue
            except FileNotFoundError:
                journal.write(event="absent", path=item["path"])
                continue
            except OSError as exc:
                result["errors"] += 1
                journal.write(event="error", path=item["path"], error=f"{type(exc).__name__}: {exc}")
                continue
            result["deleted"] += 1
            if st.st_nlink <= 1:
                result["freed_bytes"] += st.st_size
        journal.write(event="finish", **{k: v for k, v in result.items() if k != "project_id"})
    finally:
        journal.close()
    if not result["stopped"] and not result["errors"]:
        state = read_json(os.path.join(root, STATE), {})
        state = state if isinstance(state, dict) else {}
        state[phase] = last
        write_json(os.path.join(root, STATE), state)
        if phase == ARCHIVE:
            store.finish_archive(project_id, now=now)
    log(f"уборка {project_id}, этап «{PHASE_NAMES[phase]}»: удалено файлов {result['deleted']}, "
        f"освобождено {result['freed_bytes'] / 1e6:.1f} МБ, отказов {result['refused']}, ошибок {result['errors']}"
        + (f"; остановлена: {result['stopped']}" if result["stopped"] else ""))
    return result


def due(store, config, project_ids=None, now=None):
    """[(ролик, этап)], которым пора на уборку, по базе очереди и отметке прошлой уборки."""
    now = time.time() if now is None else now
    rows = {}
    with store._db() as db:
        for row in db.execute("SELECT project_id,status,finished,action FROM jobs ORDER BY created"):
            info = rows.setdefault(row["project_id"], {"status": None, "active": False, "last": None})
            info["active"] = info["active"] or row["status"] in ("pending", "running")
            if row["action"] == "title":
                # «Название — сделать заново» не меняет состояния ролика (web_queue.get_project): упавший ролик
                # после удачного названия не считается готовым, а срок уборки не сдвигается.
                continue
            info["status"] = row["status"]
            if row["finished"] is not None:
                info["last"] = max(info["last"] or 0, row["finished"])
    wanted = None if project_ids is None else set(project_ids)
    out = []
    for project_id, info in rows.items():
        if (wanted is not None and project_id not in wanted) or project_id in config["keep"]:
            continue
        if info["active"] or info["status"] != "complete" or info["last"] is None:
            continue
        try:
            state = read_json(os.path.join(str(store.project_path(project_id)), STATE), {})
        except ValueError:
            continue
        state = state if isinstance(state, dict) else {}
        if state.get(IMMEDIATE) != info["last"]:
            out.append((project_id, IMMEDIATE))
        archived = store.archive_info(project_id)
        if archived and archived.get("finished"):
            continue   # frames and drafts are gone already; nothing is left for the drafts stage
        if archived or archive_reason(store, project_id, config, now, info["last"]):
            out.append((project_id, ARCHIVE))
            continue   # the stage after delivery removes the drafts too
        days = config["draft_days"]
        if days is not None and now - info["last"] >= days * 86400 and state.get(DRAFTS) != info["last"]:
            out.append((project_id, DRAFTS))
    return out


def archive_reason(store, project_id, config, now, last):
    """Почему ролику пора на этап «после выдачи»; None — не пора или выключено настройкой."""
    hours = config.get("delivered_hours")
    if hours is not None:
        delivery = store.delivery_info(project_id)
        if delivery and delivery["current"] and now - delivery["delivered"] >= hours * 3600:
            return "после отметки «скачано»"
    days = config.get("done_days")
    if days is not None and last is not None and now - last >= days * 86400:
        return f"через {days} сут. после готовности (MANGA_CLEANUP_DONE_DAYS)"
    return None


def run_due(store, config, project_ids=None, now=None, stop=None, log=print):
    results = []
    if not config["enabled"]:
        return results
    for project_id, phase in due(store, config, project_ids, now):
        if stop is not None and stop():
            break
        try:
            results.append(clean(store, project_id, phase, config, now=now, log=log, stop=stop))
        except Exception as exc:
            # One unreadable folder or full disk must not block every younger video.
            why = f"{type(exc).__name__}: {exc}"
            log(f"уборка {project_id}, этап «{PHASE_NAMES[phase]}» не выполнена, повторю в следующий проход: {why}")
            results.append({"project_id": project_id, "phase": phase, "deleted": 0, "freed_bytes": 0, "refused": 0,
                            "kept": 0, "errors": 1, "skipped": "ошибка уборки: " + why, "stopped": None})
    return results


_UNREADABLE = {errno.ENOTDIR: "это не папка", errno.EISDIR: "это папка, а не файл", errno.EACCES: "нет прав на чтение",
               errno.EPERM: "нет прав на чтение", errno.ELOOP: "это символическая ссылка"}


def _clips_deleted(root):
    """Удаляла ли уборка клипы ролика: в журнале есть delete клипа без отказа или ошибки следом."""
    try:
        fd = os.open(os.path.join(root, JOURNAL), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    deleted = set()
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"clips/clip_' not in line or '.mp4"' not in line:  # cheap filter: thousands of lines per page view
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            path = row.get("path") if isinstance(row, dict) else None
            if not isinstance(path, str) or not _CLIP_VIDEO_PATH.fullmatch(path):
                continue
            if row.get("event") == "delete":
                deleted.add(path)
            else:  # refused, absent or error right after the delete line: the file was not removed
                deleted.discard(path)
    return bool(deleted)


def render_cache_removed(root):
    """{missing, total}: сколько частей видео последней сборки нет на диске, если уборка их удаляла;
    {error}: журнал уборки или папку clips прочитать нельзя; None — сказать нечего."""
    root = str(root)
    mapping = read_json(os.path.join(root, "out", "clip_map.json"), None)
    if not isinstance(mapping, dict):
        return None
    wanted = {n for ids in mapping.values() if isinstance(ids, list) for n in ids if type(n) is int}
    if not wanted:
        return None
    try:
        # First render, failed render or interrupted retry also leave parts missing: not the cleanup.
        if not _clips_deleted(root):
            return None
    except OSError as exc:
        return {"error": f"журнал уборки {JOURNAL}: " + _UNREADABLE.get(exc.errno, f"не читается ({type(exc).__name__})")}
    try:
        with os.scandir(os.path.join(root, "clips")) as entries:
            present = {entry.name for entry in entries}
    except FileNotFoundError:
        present = set()
    except OSError as exc:
        return {"error": "папка clips: " + _UNREADABLE.get(exc.errno, f"не читается ({type(exc).__name__})")}
    missing = sum(f"clip_{n:05d}.mp4" not in present for n in wanted)
    return {"missing": missing, "total": len(wanted)} if missing else None


if __name__ == "__main__":
    # Только просмотр: python3 app/cleanup.py <папка ролика> [immediate|drafts] — ничего не удаляет.
    target = os.path.realpath(sys.argv[1])
    phases = sys.argv[2:] or [IMMEDIATE, DRAFTS]
    for name in phases:
        report = plan(target, name)
        by_category = {}
        for entry in report["items"]:
            count, size = by_category.get(entry["category"], (0, 0))
            by_category[entry["category"]] = (count + 1, size + (entry["bytes"] if entry["links"] <= 1 else 0))
        print(json.dumps({"phase": name, "categories": by_category,
                          "total_bytes": sum(size for _, size in by_category.values()),
                          "refused": report["refused"][:20], "kept": len(report["kept"])}, ensure_ascii=False))
