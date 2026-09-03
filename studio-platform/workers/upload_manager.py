# -*- coding: utf-8 -*-
"""
upload_manager.py — management layer for YouTube auto-upload.

SELF-CONTAINED: uses only stdlib + tkinter. Does NOT import app_main.

Three pieces:
  1) Channels       — channel address-book persisted to ~/video_studio_channels.json
                      (mirrors VoiceSets: atomic tmp+os.replace, ensure_ascii=False).
  2) Per-project    — load_project_target / save_project_target read/write
     binding          upload_target.json inside the project dir
                      (mirrors _load/_save_media_scale_offset_file).
  3) run_upload     — orchestrates one upload by invoking upload_worker.py as a
                      subprocess and parsing __UPLOAD_SUCCESS__|/__UPLOAD_ERROR__|.
  4) build_channels_editor(parent) — Tkinter Toplevel editor for the channels store.

upload_worker.py subprocess contract (sibling module, may not exist yet):
  <python> <dir>/upload_worker.py --video <mp4> --title <str> --description <str>
           --profile <id> --channel-url <url> [--schedule <ISO8601>] [--thumbnail <path>]
  stdout: "__UPLOAD_SUCCESS__|<video_url>"  OR  "__UPLOAD_ERROR__|<message>"
"""

import os
import sys
import re
import json
import difflib
import subprocess
from datetime import datetime


# ---------------------------------------------------------------------------
# 1) Channels address-book — ~/video_studio_channels.json
# ---------------------------------------------------------------------------
class Channels:
    """Глобальная адресная книга каналов YouTube.

    Файл ~/video_studio_channels.json.
    Схема: {"channels": {"<name>": {name, channel_url, profile_id, language, niche}}}
    niche — опционально, дефолт "".
    Зеркалит VoiceSets: атомарная запись (tmp + os.replace), ensure_ascii=False.
    """
    _PATH = os.path.join(os.path.expanduser("~"), "video_studio_channels.json")

    # порядок и набор полей одной записи канала
    FIELDS = ("name", "channel_url", "profile_id", "language", "niche")

    @classmethod
    def load(cls):
        try:
            if os.path.exists(cls._PATH):
                with open(cls._PATH, 'r', encoding='utf-8', errors='replace') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get("channels"), dict):
                        return data
        except Exception:
            pass
        return {"channels": {}}

    @classmethod
    def save(cls, data):
        """Атомарная запись (tmp + replace)."""
        try:
            tmp = cls._PATH + ".tmp"
            with open(tmp, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cls._PATH)
            return True
        except Exception:
            return False

    @classmethod
    def list_names(cls):
        return sorted(cls.load().get("channels", {}).keys())

    @classmethod
    def get(cls, name):
        """Возвращает запись канала {name, channel_url, profile_id, language, niche} или None."""
        return cls.load().get("channels", {}).get(name)

    @classmethod
    def upsert(cls, name, data):
        """Создаёт/обновляет канал. Нормализует поля по схеме, niche дефолтит на ''."""
        name = str(name).strip()
        if not name:
            return False
        data = data if isinstance(data, dict) else {}
        entry = {
            "name": name,
            "channel_url": str(data.get("channel_url", "")).strip(),
            "profile_id": str(data.get("profile_id", "")).strip(),
            "language": str(data.get("language", "")).strip(),
            "niche": str(data.get("niche", "")).strip(),
        }
        store = cls.load()
        store.setdefault("channels", {})[name] = entry
        return cls.save(store)

    @classmethod
    def delete(cls, name):
        store = cls.load()
        if name in store.get("channels", {}):
            del store["channels"][name]
            return cls.save(store)
        return False

    @classmethod
    def for_language(cls, lang):
        """Возвращает список записей каналов, у которых language == lang."""
        lang = str(lang).strip()
        out = []
        for entry in cls.load().get("channels", {}).values():
            if isinstance(entry, dict) and str(entry.get("language", "")).strip() == lang:
                out.append(entry)
        return out


# ---------------------------------------------------------------------------
# 1b) UploadLog — журнал заливок для правила «1 ролик в день на канал»
# ---------------------------------------------------------------------------
class UploadLog:
    """Журнал заливок: ~/video_studio_upload_log.json.
    Схема: {"uploads": [{channel, channel_url, project, url, date 'YYYY-MM-DD' (локальная), at ISO}]}.
    Дата — локальная (часовой пояс машины-оператора). Атомарная запись (tmp + replace)."""
    _PATH = os.path.join(os.path.expanduser("~"), "video_studio_upload_log.json")

    @classmethod
    def load(cls):
        try:
            if os.path.exists(cls._PATH):
                with open(cls._PATH, 'r', encoding='utf-8', errors='replace') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get("uploads"), list):
                        return data
        except Exception:
            pass
        return {"uploads": []}

    @classmethod
    def save(cls, data):
        try:
            tmp = cls._PATH + ".tmp"
            with open(tmp, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cls._PATH)
            return True
        except Exception:
            return False

    @staticmethod
    def _today():
        return datetime.now().strftime("%Y-%m-%d")

    @classmethod
    def record(cls, channel_name, channel_url, project, url):
        data = cls.load()
        data.setdefault("uploads", []).append({
            "channel": str(channel_name or "").strip(),
            "channel_url": str(channel_url or "").strip(),
            "project": str(project or ""),
            "url": str(url or ""),
            "date": cls._today(),
            "at": datetime.now().isoformat(),
        })
        return cls.save(data)

    @classmethod
    def uploaded_today(cls, channel_name, channel_url=""):
        """Возвращает запись СЕГОДНЯШНЕЙ заливки на этот канал (по имени ИЛИ url) или None."""
        name = str(channel_name or "").strip()
        url = str(channel_url or "").strip()
        today = cls._today()
        for rec in reversed(cls.load().get("uploads", [])):
            if rec.get("date") != today:
                continue
            if (name and rec.get("channel", "").strip() == name) or \
               (url and rec.get("channel_url", "").strip() == url):
                return rec
        return None


# ---------------------------------------------------------------------------
# 2) Per-project binding — upload_target.json inside the project dir
# ---------------------------------------------------------------------------
def load_project_target(project_dir):
    """Загружает upload_target.json из папки проекта.

    Схема: {"channel_name", "uploaded_url", "uploaded_at"}.
    Зеркалит _load_media_scale_offset_file (try/except, json).
    """
    path = os.path.join(project_dir, "upload_target.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_project_target(project_dir, **fields):
    """Сохраняет/обновляет upload_target.json в папке проекта (merge с существующим).

    Допустимые поля: channel_name, uploaded_url, uploaded_at.
    Зеркалит _save_media_scale_offset_file (try/except, json, ensure_ascii=False).
    """
    path = os.path.join(project_dir, "upload_target.json")
    data = load_project_target(project_dir)
    for key in ("channel_name", "uploaded_url", "uploaded_at"):
        if key in fields:
            data[key] = fields[key]
    try:
        with open(path, 'w', encoding='utf-8', errors='replace') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 2b) Thumbnail resolver — найти обложку в папке ПО НАЗВАНИЮ проекта
# ---------------------------------------------------------------------------
THUMBNAILS_DIR = os.path.join(os.path.expanduser("~"), "video_studio_thumbnails")
_THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _norm_title(name):
    """Снимает языковой префикс (de_/ru_/pl_/…) + нормализует строку для матча обложки по названию."""
    s = re.sub(r'^[a-zA-Z]{2}_', '', str(name or '').strip())
    return re.sub(r'[^a-z0-9]', '', s.lower())


def find_thumbnail(project_name, thumb_dir=None):
    """Ищет обложку в папке thumb_dir ПО НАЗВАНИЮ проекта (язык-агностично).
    Имя файла обложки ≈ заголовок видео → одна обложка на тему подходит на все языки.
    Точное совпадение / вхождение / похожесть >=0.82. Возвращает путь или '' (нет совпадения)."""
    d = thumb_dir or THUMBNAILS_DIR
    if not d or not os.path.isdir(d):
        return ""
    key = _norm_title(project_name)
    if not key:
        return ""
    try:
        files = os.listdir(d)
    except Exception:
        return ""
    best, best_r = "", 0.0
    for f in files:
        stem, ext = os.path.splitext(f)
        if ext.lower() not in _THUMB_EXTS:
            continue
        fk = _norm_title(stem)
        if not fk:
            continue
        if fk == key or key in fk or fk in key:
            return os.path.join(d, f)
        r = difflib.SequenceMatcher(None, key, fk).ratio()
        if r > best_r:
            best_r, best = r, os.path.join(d, f)
    return best if best_r >= 0.82 else ""


# ---------------------------------------------------------------------------
# 3) Orchestration — run one upload via upload_worker.py subprocess
# ---------------------------------------------------------------------------
_SUCCESS_MARKER = "__UPLOAD_SUCCESS__|"
_ERROR_MARKER = "__UPLOAD_ERROR__|"


def _worker_path():
    """Путь к upload_worker.py рядом с этим модулем."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_worker.py")


def run_upload(project_dir, video_path, title, description, channel_entry,
               schedule_iso=None, thumbnail='', log_callback=None, dedup=True,
               one_per_day=True, finalize_draft=False, upload_only=False):
    """Оркеструет одну загрузку видео на YouTube через upload_worker.py.

    Args:
        project_dir:   папка проекта (для upload_target.json и dedup).
        video_path:    путь к mp4.
        title:         заголовок ролика.
        description:   описание ролика.
        channel_entry: dict записи канала (channel_url, profile_id, ...) — см. Channels.get().
        schedule_iso:  ISO8601-строка для отложенной публикации (опционально).
        thumbnail:     путь к превью (опционально).
        log_callback:  callable(str) — построчный лог из stdout воркера (опционально).
        dedup:         если True и в проекте уже есть непустой uploaded_url — пропустить.

    Returns:
        {"success": True,  "url": <str>}                      — успех
        {"success": True,  "url": <str>, "skipped": True}     — пропуск (dedup)
        {"success": False, "error": <str>}                    — ошибка
    """
    def _log(msg):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    # --- dedup: уже загружено? ---
    if dedup:
        existing = load_project_target(project_dir).get("uploaded_url", "")
        if existing and str(existing).strip():
            _log(f"Пропуск: проект уже загружен → {existing}")
            return {"success": True, "url": existing, "skipped": True}

    channel_entry = channel_entry if isinstance(channel_entry, dict) else {}
    channel_url = str(channel_entry.get("channel_url", "")).strip()
    profile_id = str(channel_entry.get("profile_id", "")).strip()
    channel_name = str(channel_entry.get("name", "")).strip() or channel_url

    # --- правило «1 ролик в день на канал» ---
    if one_per_day and (channel_name or channel_url):
        today_rec = UploadLog.uploaded_today(channel_name, channel_url)
        if today_rec:
            msg = (f"Пропуск: на канал «{channel_name}» уже залит ролик сегодня "
                   f"({today_rec.get('date')}) → {today_rec.get('url', '')}")
            _log(msg)
            return {"success": True, "url": today_rec.get("url", ""),
                    "skipped": True, "reason": "one_per_day"}

    worker = _worker_path()
    if not os.path.isfile(worker):
        msg = f"upload_worker.py не найден: {worker}"
        _log(msg)
        return {"success": False, "error": msg}

    # --- argv по контракту ---
    argv = [
        sys.executable, worker,
        "--video", str(video_path),
        "--title", str(title),
        "--description", str(description),
        "--profile", profile_id,
        "--channel-url", channel_url,
    ]
    if schedule_iso:
        argv += ["--schedule", str(schedule_iso)]
    if thumbnail:
        argv += ["--thumbnail", str(thumbnail)]
    if finalize_draft:
        argv += ["--finalize-draft"]
    if upload_only:
        argv += ["--upload-only"]

    _log(f"Запуск загрузки: {video_path}")

    result_line = None
    WORKER_HARD_TIMEOUT = 45 * 60  # макс 45 мин на ОДИН ролик (аплоад до 760МБ + флоу + запас).
    # Watchdog убивает зависший воркер (живой-но-замёрзший Chrome: крышка Mac/half-open сокет),
    # иначе `for line in proc.stdout` блокируется навечно и весь батч стоит на одном ролике.
    killed = {"v": False}
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace',
            bufsize=1,
        )
        import threading as _thr
        def _watchdog():
            try:
                proc.wait(timeout=WORKER_HARD_TIMEOUT)
            except subprocess.TimeoutExpired:
                killed["v"] = True
                try:
                    proc.kill()
                except Exception:
                    pass
        _thr.Thread(target=_watchdog, daemon=True).start()
        for line in proc.stdout:
            line = line.rstrip("\n")
            _log(line)
            if line.startswith(_SUCCESS_MARKER) or line.startswith(_ERROR_MARKER):
                result_line = line
        proc.wait()
        if killed["v"]:
            msg = f"воркер убит по таймауту {WORKER_HARD_TIMEOUT // 60} мин (зависший Chrome) — помечаю провалом, батч продолжается"
            _log(msg)
            return {"success": False, "error": msg}
    except Exception as e:
        msg = f"Сбой запуска воркера: {e}"
        _log(msg)
        return {"success": False, "error": msg}

    # --- разбор маркеров ---
    if result_line and result_line.startswith(_SUCCESS_MARKER):
        url = result_line[len(_SUCCESS_MARKER):].strip()
        # Фаза A (upload_only): ролик ещё ЧЕРНОВИК (не запланирован) — НЕ помечаем проект залитым,
        # иначе Фаза B/повтор посчитает его готовым. Просто рапортуем успех заливки файла.
        if upload_only or url == "DRAFT_UPLOADED":
            _log("Готово (черновик залит, планирование — Фаза B)")
            return {"success": True, "url": url, "draft_uploaded": True}
        save_project_target(
            project_dir,
            uploaded_url=url,
            uploaded_at=datetime.now().isoformat(),
        )
        try:
            UploadLog.record(channel_name, channel_url,
                             os.path.basename(str(project_dir).rstrip("/\\")), url)
        except Exception:
            pass
        _log(f"Готово: {url}")
        return {"success": True, "url": url}

    if result_line and result_line.startswith(_ERROR_MARKER):
        err = result_line[len(_ERROR_MARKER):].strip()
        _log(f"Ошибка загрузки: {err}")
        return {"success": False, "error": err}

    msg = "Воркер не вернул маркер результата (__UPLOAD_SUCCESS__/__UPLOAD_ERROR__)."
    _log(msg)
    return {"success": False, "error": msg}


# ---------------------------------------------------------------------------
# 4) Channels editor — Tkinter Toplevel (lazy tkinter import)
# ---------------------------------------------------------------------------
def build_channels_editor(parent):
    """Открывает Toplevel-редактор каналов: Listbox имён + поля + Save/Delete/Close.

    Бэкенд — Channels store. tkinter импортируется лениво (модуль остаётся
    импортируемым в headless-окружении).
    """
    from tkinter import (Toplevel, Frame, Label, Entry, Listbox, StringVar,
                         Button, ttk, messagebox, END, SINGLE)

    dialog = Toplevel(parent)
    dialog.title("Каналы YouTube")
    dialog.geometry("640x420")
    try:
        dialog.transient(parent)
    except Exception:
        pass

    # --- слева: список имён каналов ---
    left = Frame(dialog)
    left.pack(side="left", fill="y", padx=(12, 6), pady=12)
    Label(left, text="Каналы:", anchor="w").pack(fill="x")
    listbox = Listbox(left, width=26, height=18, selectmode=SINGLE,
                      exportselection=False)
    listbox.pack(fill="y", expand=True)

    # --- справа: поля редактирования ---
    right = Frame(dialog)
    right.pack(side="left", fill="both", expand=True, padx=(6, 12), pady=12)

    entry_vars = {f: StringVar(value="") for f in Channels.FIELDS}
    labels = {
        "name": "Имя:",
        "channel_url": "URL канала:",
        "profile_id": "Profile ID:",
        "language": "Язык (код):",
        "niche": "Ниша:",
    }
    for i, f in enumerate(Channels.FIELDS):
        Label(right, text=labels[f], width=12, anchor="w").grid(
            row=i, column=0, sticky="w", pady=3)
        Entry(right, textvariable=entry_vars[f], width=40).grid(
            row=i, column=1, sticky="ew", padx=5, pady=3)
    right.columnconfigure(1, weight=1)

    def _refresh_list(select_name=None):
        listbox.delete(0, END)
        names = Channels.list_names()
        for nm in names:
            listbox.insert(END, nm)
        if select_name and select_name in names:
            idx = names.index(select_name)
            listbox.selection_clear(0, END)
            listbox.selection_set(idx)
            listbox.see(idx)

    def _clear_fields():
        for var in entry_vars.values():
            var.set("")

    def _load_selected(_evt=None):
        sel = listbox.curselection()
        if not sel:
            return
        nm = listbox.get(sel[0])
        entry = Channels.get(nm) or {}
        for f, var in entry_vars.items():
            var.set(str(entry.get(f, "")))
    listbox.bind("<<ListboxSelect>>", _load_selected)

    def _do_save():
        nm = entry_vars["name"].get().strip()
        if not nm:
            messagebox.showwarning("Каналы", "Укажи имя канала.", parent=dialog)
            return
        data = {f: entry_vars[f].get().strip() for f in Channels.FIELDS}
        if Channels.upsert(nm, data):
            _refresh_list(select_name=nm)
            messagebox.showinfo("Каналы", f"Канал «{nm}» сохранён.", parent=dialog)
        else:
            messagebox.showerror("Каналы", "Не удалось сохранить.", parent=dialog)

    def _do_delete():
        nm = entry_vars["name"].get().strip()
        sel = listbox.curselection()
        if not nm and sel:
            nm = listbox.get(sel[0])
        if not nm:
            return
        if messagebox.askyesno("Каналы", f"Удалить канал «{nm}»?", parent=dialog):
            Channels.delete(nm)
            _clear_fields()
            _refresh_list()
            messagebox.showinfo("Каналы", "Удалён.", parent=dialog)

    # --- низ: кнопки ---
    btns = Frame(right)
    btns.grid(row=len(Channels.FIELDS), column=0, columnspan=2, sticky="ew", pady=(12, 0))
    Button(btns, text="➕ Новый", command=_clear_fields, width=12).pack(side="left", padx=3)
    Button(btns, text="💾 Сохранить", command=_do_save, width=14).pack(side="left", padx=3)
    Button(btns, text="🗑 Удалить", command=_do_delete, width=12).pack(side="left", padx=3)
    Button(btns, text="Закрыть", command=dialog.destroy, width=12).pack(side="right", padx=3)

    _refresh_list()
    return dialog
