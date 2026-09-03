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
  stdout: monotonic "__UPLOAD_PHASE__|<schema-v1-json>" records and exactly one
          "__UPLOAD_RESULT__|<schema-v2-json>", followed by
          "__UPLOAD_SUCCESS__|<video_url>" OR "__UPLOAD_ERROR__|<message>"
"""

import os
import sys
import re
import json
import difflib
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime


def _load_sibling_module(name):
    try:
        return __import__(name)
    except ImportError:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


upload_contract = _load_sibling_module("upload_contract")
upload_coordination = _load_sibling_module("upload_coordination")


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
    FIELDS = ("name", "channel_url", "profile_id", "language", "niche", "collaborator")

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
            # @handle канала-соавтора: если задан, после публикации ему уходит
            # приглашение на коллаборацию. ТОЛЬКО точный handle — по названию
            # поиск возвращает чужие каналы-однофамильцы.
            "collaborator": str(data.get("collaborator", "")).strip(),
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
    _THREAD_LOCK = threading.RLock()

    @classmethod
    @contextmanager
    def _exclusive(cls):
        """Serialize the complete read/modify/write on Unix and Windows."""
        lock_path = cls._PATH + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        with cls._THREAD_LOCK:
            with open(lock_path, "a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _load_unlocked(cls):
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
    def load(cls):
        with cls._exclusive():
            return cls._load_unlocked()

    @classmethod
    def _save_unlocked(cls, data):
        tmp = f"{cls._PATH}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, cls._PATH)
            try:
                directory_fd = os.open(os.path.dirname(os.path.abspath(cls._PATH)), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    @classmethod
    def save(cls, data):
        with cls._exclusive():
            return cls._save_unlocked(data)

    @staticmethod
    def _today():
        return datetime.now().strftime("%Y-%m-%d")

    @classmethod
    def record(cls, channel_name, channel_url, project, url):
        with cls._exclusive():
            data = cls._load_unlocked()
            data.setdefault("uploads", []).append({
                "channel": str(channel_name or "").strip(),
                "channel_url": str(channel_url or "").strip(),
                "project": str(project or ""),
                "url": str(url or ""),
                "date": cls._today(),
                "at": datetime.now().isoformat(),
            })
            return cls._save_unlocked(data)

    @classmethod
    def uploaded_today(cls, channel_name, channel_url=""):
        """Возвращает запись СЕГОДНЯШНЕЙ заливки на этот канал (по имени ИЛИ url) или None."""
        name = str(channel_name or "").strip()
        url = str(channel_url or "").strip()
        today = cls._today()
        with cls._exclusive():
            for rec in reversed(cls._load_unlocked().get("uploads", [])):
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
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, 'x', encoding='utf-8', errors='replace') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except (OSError, UnboundLocalError):
            pass
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
_RESULT_MARKER = upload_contract.RESULT_MARKER
_PHASE_MARKER = upload_contract.PHASE_MARKER
_RECEIPT_ENV_KEYS = (
    "VIDEO_OPS_RECEIPT_CONTEXT_SCHEMA_VERSION",
    "VIDEO_OPS_RECEIPT_OUTBOX",
    "VIDEO_OPS_RECEIPT_MANIFEST_SHA256",
    "VIDEO_OPS_RECEIPT_MANIFEST_NAME",
    "VIDEO_OPS_RECEIPT_OPERATIONAL_DATE",
    "VIDEO_OPS_RECEIPT_EXPECTED_SCHEDULED_AT",
    "VIDEO_OPS_RECEIPT_SOURCE_HOST",
    "VIDEO_OPS_RECEIPT_SSH_TARGET",
    "VIDEO_OPS_RECEIPT_SSH_TIMEOUT",
    "VIDEO_OPS_RECEIPT_IO",
    "VIDEO_OPS_RECEIPT_EXISTING_UPLOAD_POLICY",
)


def _worker_path():
    """Путь к upload_worker.py рядом с этим модулем."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_worker.py")


def _normalize_receipt_context(value):
    """Delegate producer validation to the canonical receipt module."""
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise ValueError("receipt_context must be an object")
    raw_receipt_io_path = value.get("receipt_io_path")
    if not isinstance(raw_receipt_io_path, str):
        raise ValueError("receipt_context.receipt_io_path must be a string")
    if raw_receipt_io_path and not os.path.isabs(raw_receipt_io_path):
        raise ValueError("receipt_context.receipt_io_path must be absolute")
    candidate = raw_receipt_io_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools", "video_ops_watchdog", "receipt_io.py",
    )
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("video_ops_receipt_context", candidate)
        if spec is None or spec.loader is None:
            raise ValueError(f"receipt_io не найден: {candidate}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.normalize_producer_context(value)
    except (OSError, ImportError, AttributeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc

def _validate_structured_result(candidate):
    return upload_contract.validate_result(candidate)


def _structured_failure(message, structured_result=None, phase_event=None):
    """Preserve known Studio identity/evidence while refusing an unsafe success."""
    event = phase_event
    if event is None and isinstance(structured_result, dict):
        try:
            phase = str(structured_result.get("phase") or "preflight")
            video_id = (
                str(structured_result.get("video_id") or "")
                if phase == "video_id_known"
                else ""
            )
            event = upload_contract.advance_phase_event(
                upload_contract.initial_phase_event(), phase, video_id
            )
        except ValueError:
            event = None
    event = event or upload_contract.initial_phase_event()
    return upload_contract.failure_result(
        message,
        event,
        structured_result,
    )


def _protocol_failure(message, structured_result=None, *, phase_event=None):
    return _structured_failure(message, structured_result, phase_event)


def _invoke_worker(argv, worker_env, log, *, lease=None, phase_callback=None):
    """Run one worker and recover retry safety from its last durable phase."""
    result_line = None
    terminal_result_count = 0
    structured_result = None
    structured_result_count = 0
    structured_result_error = ""
    phase_event = upload_contract.initial_phase_event()
    phase_error = ""
    worker_hard_timeout = 45 * 60
    killed = {"reason": ""}
    kill_lock = threading.Lock()

    try:
        durable_ack = phase_callback is not None
        child_env = dict(worker_env)
        if durable_ack:
            child_env["VIDEO_UPLOAD_PHASE_ACK"] = "1"
        else:
            child_env.pop("VIDEO_UPLOAD_PHASE_ACK", None)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if durable_ack else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=child_env,
        )

        def kill_worker(reason):
            with kill_lock:
                if killed["reason"]:
                    return
                killed["reason"] = reason
                try:
                    proc.kill()
                except Exception:
                    pass

        def timeout_watchdog():
            try:
                proc.wait(timeout=worker_hard_timeout)
            except subprocess.TimeoutExpired:
                kill_worker(
                    f"worker timeout after {worker_hard_timeout // 60} minutes"
                )

        threading.Thread(target=timeout_watchdog, daemon=True).start()
        if lease is not None:
            def lease_watchdog():
                lease.lost_event.wait()
                if lease.lost_event.is_set():
                    kill_worker(
                        "cross-host profile lease lost: "
                        + (lease.lost_error or "unknown")
                    )

            threading.Thread(target=lease_watchdog, daemon=True).start()

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            log(line)
            if line.startswith(_PHASE_MARKER):
                try:
                    candidate = upload_contract.decode_phase_marker(line)
                    phase_event = upload_contract.advance_phase_event(
                        phase_event,
                        candidate["phase"],
                        candidate["video_id"],
                    )
                    if phase_callback is not None:
                        try:
                            phase_callback(dict(phase_event))
                        except Exception as exc:
                            kill_worker(f"durable phase callback failed: {exc}")
                            continue
                    if durable_ack and phase_event["phase"] == "upload_started":
                        try:
                            proc.stdin.write(upload_contract.encode_phase_ack(phase_event) + "\n")
                            proc.stdin.flush()
                        except (AttributeError, BrokenPipeError, OSError) as exc:
                            kill_worker(f"durable phase ACK failed: {exc}")
                except ValueError as exc:
                    phase_error = str(exc)
                    kill_worker(f"invalid upload phase protocol: {exc}")
            if line.startswith(_RESULT_MARKER):
                structured_result_count += 1
                try:
                    candidate = json.loads(line[len(_RESULT_MARKER):])
                    structured_result = _validate_structured_result(candidate)
                    result_video_id = structured_result.get("video_id") or ""
                    phase_event = upload_contract.advance_phase_event(
                        phase_event,
                        structured_result["phase"],
                        result_video_id,
                    )
                except (ValueError, json.JSONDecodeError) as exc:
                    structured_result_error = str(exc)
            if line.startswith(_SUCCESS_MARKER) or line.startswith(_ERROR_MARKER):
                terminal_result_count += 1
                result_line = line
        return_code = proc.wait()
    except Exception as exc:
        message = f"Сбой запуска/чтения воркера: {exc}"
        log(message)
        return _protocol_failure(
            message,
            structured_result if structured_result_count == 1 and not structured_result_error else None,
            phase_event=phase_event,
        )

    if killed["reason"]:
        message = killed["reason"]
        log(message)
        return _protocol_failure(
            message,
            structured_result if structured_result_count == 1 and not structured_result_error else None,
            phase_event=phase_event,
        )
    if phase_error:
        message = f"Некорректный протокол upload_worker: {phase_error}."
        log(message)
        return _protocol_failure(message, structured_result, phase_event=phase_event)
    if structured_result_count != 1 or structured_result is None or structured_result_error:
        message = (
            "Некорректный протокол upload_worker: ожидался ровно один "
            f"__UPLOAD_RESULT__ schema=2 (count={structured_result_count}, "
            f"error={structured_result_error or 'none'})."
        )
        log(message)
        return _protocol_failure(message, structured_result, phase_event=phase_event)
    if terminal_result_count != 1:
        message = (
            "Некорректный протокол upload_worker: ожидался ровно один terminal marker "
            f"(__UPLOAD_SUCCESS__/__UPLOAD_ERROR__), count={terminal_result_count}."
        )
        log(message)
        return _protocol_failure(message, structured_result, phase_event=phase_event)

    if result_line and result_line.startswith(_SUCCESS_MARKER):
        url = result_line[len(_SUCCESS_MARKER):].strip()
        if return_code != 0:
            message = f"Некорректный протокол upload_worker: success-маркер при exit={return_code}."
        elif not structured_result.get("success"):
            message = "Некорректный протокол upload_worker: success-маркер противоречит JSON-результату."
        elif str(structured_result.get("url") or "") != url:
            message = "Некорректный протокол upload_worker: URL success-маркера не совпал с JSON."
        else:
            return structured_result
        log(message)
        return _protocol_failure(message, structured_result, phase_event=phase_event)

    if result_line and result_line.startswith(_ERROR_MARKER):
        error = result_line[len(_ERROR_MARKER):].strip()
        log(f"Ошибка загрузки: {error}")
        if (
            return_code == 0
            or structured_result.get("success")
            or structured_result.get("error") != error
        ):
            message = (
                "Некорректный протокол upload_worker: error-маркер противоречит "
                f"JSON/exit={return_code}."
            )
            log(message)
            return _protocol_failure(message, structured_result, phase_event=phase_event)
        return _structured_failure(error, structured_result, phase_event)

    message = "Воркер не вернул маркер результата (__UPLOAD_SUCCESS__/__UPLOAD_ERROR__)."
    log(message)
    return _protocol_failure(message, structured_result, phase_event=phase_event)


def run_upload(project_dir, video_path, title, description, channel_entry,
               schedule_iso=None, thumbnail='', log_callback=None, dedup=True,
               one_per_day=True, finalize_draft=False, upload_only=False,
               search_title='', monetize=False, ab_thumbnails=None, visibility='public',
               collaborator=None, receipt_context=None, profile_lease_context=None,
               profile_guard=None, profile_inactive_guard=None, phase_callback=None,
               known_upload_url=''):
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
        receipt_context: explicit frozen manifest/date/source/outbox/push settings.
        profile_lease_context: versioned shared-coordinator lease settings.
        profile_guard: read-only dual-host check called while the lease is held.
        profile_inactive_guard: bounded dual-host inactive proof after the worker,
            still under the same lease.  Failure retains the lease until TTL.
        phase_callback: durable observer called for each validated worker phase.
        known_upload_url: exact receipt-derived URL, allowed only in reconcile mode.

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

    try:
        receipt_context = _normalize_receipt_context(receipt_context)
    except ValueError as exc:
        msg = f"Некорректный receipt_context: {exc}"
        _log(msg)
        return _protocol_failure(msg)
    try:
        profile_lease_context = upload_coordination.normalize_lease_context(
            profile_lease_context
        )
    except (ValueError, upload_coordination.ProfileLeaseError) as exc:
        msg = f"Некорректный profile_lease_context: {exc}"
        _log(msg)
        return _protocol_failure(msg)
    try:
        parsed_schedule = upload_contract.parse_absolute_schedule(schedule_iso)
    except ValueError as exc:
        msg = f"Некорректный schedule_iso: {exc}"
        _log(msg)
        return _protocol_failure(msg)
    if receipt_context and parsed_schedule is None:
        msg = "receipt_context требует абсолютный schedule_iso с UTC offset"
        _log(msg)
        return _protocol_failure(msg)
    if receipt_context and (dedup is not True or one_per_day is not True):
        msg = "receipt_context требует включённые dedup и one_per_day"
        _log(msg)
        return _protocol_failure(msg)
    if receipt_context:
        expected_receipt_schedule = upload_contract.parse_absolute_schedule(
            receipt_context["expected_scheduled_at"]
        )
        if int(expected_receipt_schedule.timestamp()) != int(parsed_schedule.timestamp()):
            msg = "receipt_context expected_scheduled_at не совпадает с schedule_iso"
            _log(msg)
            return _protocol_failure(msg)

    channel_entry = channel_entry if isinstance(channel_entry, dict) else {}
    channel_url = str(channel_entry.get("channel_url", "")).strip()
    profile_id = str(channel_entry.get("profile_id", "")).strip()
    channel_name = str(channel_entry.get("name", "")).strip() or channel_url
    if profile_lease_context is not None and not callable(profile_guard):
        msg = "profile_lease_context требует dual-host profile_guard"
        _log(msg)
        return _protocol_failure(msg)
    if phase_callback is not None and not callable(phase_callback):
        msg = "phase_callback должен быть callable"
        _log(msg)
        return _protocol_failure(msg)
    if profile_inactive_guard is not None and not callable(profile_inactive_guard):
        msg = "profile_inactive_guard должен быть callable"
        _log(msg)
        return _protocol_failure(msg)

    # --- dedup: уже загружено? ---
    project_target = load_project_target(project_dir)
    existing = str(project_target.get("uploaded_url") or "").strip()
    existing_policy = (
        receipt_context.get("existing_upload_policy")
        if receipt_context
        else "skip"
    )
    durable_known = str(known_upload_url or "").strip()
    if durable_known:
        if not receipt_context or existing_policy != "reconcile_exact":
            msg = "known_upload_url разрешён только receipt reconcile_exact"
            _log(msg)
            return _protocol_failure(msg)
        if not upload_contract.video_id_from_url(durable_known):
            msg = "known_upload_url не является точным https://youtu.be/<video_id>"
            _log(msg)
            return _protocol_failure(msg)
        if existing and existing != durable_known:
            msg = "upload_target и canonical receipt указывают разные video URL"
            _log(msg)
            return _protocol_failure(msg)
        existing = durable_known
    reconcile_video_id = ""
    if dedup and existing:
        if receipt_context and existing_policy == "reconcile_exact":
            reconcile_video_id = upload_contract.video_id_from_url(existing)
            if not reconcile_video_id:
                msg = "Dedup: uploaded_url не является точным https://youtu.be/<video_id>"
                _log(msg)
                return _protocol_failure(msg)
            _log(
                "Dedup: локальный URL найден; выполняю exact Studio reconciliation "
                f"в reconcile-only режиме → {existing}"
            )
        else:
            _log(f"Пропуск: проект уже загружен → {existing}")
            return {"success": True, "url": existing, "skipped": True}

    project_name = os.path.basename(str(project_dir).rstrip("/\\"))

    # --- правило «1 ролик в день на канал» ---
    if one_per_day and (channel_name or channel_url):
        today_rec = UploadLog.uploaded_today(channel_name, channel_url)
        same_reconciliation = bool(
            today_rec
            and receipt_context
            and existing_policy == "reconcile_exact"
            and existing
            and str(today_rec.get("project") or "") == project_name
            and str(today_rec.get("url") or "") == existing
        )
        if today_rec and not same_reconciliation:
            msg = (f"Пропуск: на канал «{channel_name}» уже залит ролик сегодня "
                   f"({today_rec.get('date')}) → {today_rec.get('url', '')}")
            _log(msg)
            return {"success": True, "url": today_rec.get("url", ""),
                    "skipped": True, "reason": "one_per_day"}
        if same_reconciliation:
            _log("One-per-day: разрешена только идемпотентная exact reconciliation того же проекта/URL")

    if receipt_context and profile_lease_context is None:
        msg = "receipt_context требует cross-host profile_lease_context"
        _log(msg)
        return _protocol_failure(msg)
    if receipt_context and profile_inactive_guard is None:
        msg = "receipt_context требует post-worker dual-host inactive guard"
        _log(msg)
        return _protocol_failure(msg)

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
    if search_title:
        argv += ["--search-title", str(search_title)]
    if monetize:
        argv += ["--monetize"]
    if visibility and visibility != "public":
        argv += ["--visibility", str(visibility)]
    if ab_thumbnails:
        argv += ["--ab-thumbs", ",".join(str(p) for p in ab_thumbnails)]
    # соавтор: явный аргумент важнее записи канала (None = взять из адресной книги)
    _collab = collaborator if collaborator is not None else str(channel_entry.get("collaborator", "")).strip()
    if _collab:
        argv += ["--collaborator", str(_collab)]
    if reconcile_video_id:
        argv += ["--reconcile-video-id", reconcile_video_id]
    if receipt_context:
        argv += [
            "--receipt-context-schema-version", str(receipt_context["schema_version"]),
            "--receipt-outbox", receipt_context["outbox_dir"],
            "--receipt-manifest-sha256", receipt_context["manifest_sha256"],
            "--receipt-manifest-name", receipt_context["manifest_name"],
            "--receipt-operational-date", receipt_context["operational_date"],
            "--receipt-expected-scheduled-at", receipt_context["expected_scheduled_at"],
            "--receipt-source-host", receipt_context["source_host"],
            "--receipt-existing-upload-policy", receipt_context["existing_upload_policy"],
        ]
        if receipt_context["receipt_io_path"]:
            argv += ["--receipt-io-path", receipt_context["receipt_io_path"]]
        if receipt_context["push"]["enabled"]:
            argv += [
                "--receipt-ssh-target", receipt_context["push"]["ssh_target"],
                "--receipt-ssh-timeout", str(receipt_context["push"]["timeout_seconds"]),
            ]

    _log(f"Запуск загрузки: {video_path}")

    worker_env = os.environ.copy()
    for key in _RECEIPT_ENV_KEYS:
        worker_env.pop(key, None)
    if receipt_context:
        worker_env.update(
            {
                "VIDEO_OPS_RECEIPT_CONTEXT_SCHEMA_VERSION": str(receipt_context["schema_version"]),
                "VIDEO_OPS_RECEIPT_OUTBOX": receipt_context["outbox_dir"],
                "VIDEO_OPS_RECEIPT_MANIFEST_SHA256": receipt_context["manifest_sha256"],
                "VIDEO_OPS_RECEIPT_MANIFEST_NAME": receipt_context["manifest_name"],
                "VIDEO_OPS_RECEIPT_OPERATIONAL_DATE": receipt_context["operational_date"],
                "VIDEO_OPS_RECEIPT_EXPECTED_SCHEDULED_AT": receipt_context["expected_scheduled_at"],
                "VIDEO_OPS_RECEIPT_SOURCE_HOST": receipt_context["source_host"],
                "VIDEO_OPS_RECEIPT_SSH_TARGET": receipt_context["push"].get("ssh_target", ""),
                "VIDEO_OPS_RECEIPT_SSH_TIMEOUT": str(
                    receipt_context["push"].get("timeout_seconds", "")
                ),
                "VIDEO_OPS_RECEIPT_IO": receipt_context["receipt_io_path"],
                "VIDEO_OPS_RECEIPT_EXISTING_UPLOAD_POLICY": receipt_context["existing_upload_policy"],
            }
        )

    post_worker_guard_error = ""
    response = _protocol_failure("worker did not start")
    lease = None
    try:
        with upload_coordination.profile_lease(
            profile_id, profile_lease_context
        ) as lease:
            if lease is not None:
                # Lease first, then query both hosts, then start.  No other
                # cooperative host can enter the check→start window.
                profile_guard()
                lease.assert_held()
            response = _invoke_worker(
                argv,
                worker_env,
                _log,
                lease=lease,
                phase_callback=phase_callback,
            )
            if lease is not None and profile_inactive_guard is not None:
                try:
                    profile_inactive_guard()
                except Exception as exc:
                    post_worker_guard_error = str(exc)
                    lease.retain_until_expiry(post_worker_guard_error)
        if lease is not None and lease.release_error:
            _log(
                "Profile lease release не подтверждён; coordinator TTL остаётся fail-safe: "
                + lease.release_error
            )
    except (upload_coordination.ProfileLeaseError, OSError, RuntimeError) as exc:
        msg = f"Профиль не зарезервирован/не проверен: {exc}"
        _log(msg)
        return _protocol_failure(msg)

    if post_worker_guard_error:
        msg = (
            "Post-worker dual-host inactive proof failed; profile lease retained "
            "until TTL: " + post_worker_guard_error
        )
        _log(msg)
        return _structured_failure(msg, response)

    if not response.get("success"):
        return response
    url = str(response.get("url") or "")
    if upload_only or url == "DRAFT_UPLOADED":
        _log("Готово (черновик залит, планирование — Фаза B)")
        return {**response, "draft_uploaded": True}
    if not save_project_target(
        project_dir,
        uploaded_url=url,
        uploaded_at=datetime.now().isoformat(),
    ):
        msg = "upload_target.json не сохранён; video_id известен, повторно не заливать"
        _log(msg)
        return _structured_failure(msg, response)
    if not UploadLog.record(channel_name, channel_url, project_name, url):
        _log("Предупреждение: UploadLog не сохранён; exact worker result остаётся источником retry safety")
    _log(f"Готово: {url}")
    return response


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
        "collaborator": "Соавтор (@):",
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
