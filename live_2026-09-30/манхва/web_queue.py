"""Durable queue and read-only project views for the web application.

Ownership guarantees of a running job:
1. claim() gives a pending job to exactly one owner (worker launch id) and stamps its heartbeat.
2. While a worker runs a job it holds that job's lock file (JobLock); the kernel frees the lock
   when the process ends in any way. recover() never resets a job whose lock is held, however
   old its heartbeat or wrong the clock; a job whose lock file exists but is free lost its process
   and is reset at once. Only without a lock file (old rows, tools, a claim not run yet) the
   heartbeat decides: older than the lease means orphaned. A lock that cannot be read (no rights,
   too many open files, job_locks not a folder) proves nothing: the job is never reset on it; while
   its heartbeat is old it carries a QUEUE_PAUSED note with the reason, cleared once the check works.
   A job whose lock cannot be taken goes back to pending with such a note; nothing is closed as an error.
3. The worker writes its claimed job (heartbeat, step state, finish) only under the condition
   status='running' AND the same owner; a late owner changes nothing and learns it (False).
   finish() without owner (tools, tests) still requires status='running'.
4. Existing queue.sqlite3 files are migrated in place; old running rows have no heartbeat
   and are treated as orphans, exactly as before the migration.
The queue root must be a local disk: SQLite and flock give these guarantees only there.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import resource
import shutil
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    from . import source as SRC
    from . import languages as LG
    from . import claude_pool as CP
except ImportError:
    import source as SRC
    import languages as LG
    import claude_pool as CP

# A worker marks its job alive every HEARTBEAT_SECONDS. Without a job lock to ask, only a
# mark older than LEASE_SECONDS lets another worker take the job back: a false expiry would
# run paid steps twice, a late one only delays a restart, so the lease is long.
HEARTBEAT_SECONDS = 15.0
LEASE_SECONDS = 180.0

log = logging.getLogger("manga.queue")

# Start of a job note that means "the queue cannot start or check this job"; the interface
# (web.error_view) and /health recognise it. Such a job waits, it has nothing to retry by hand.
QUEUE_PAUSED = "Очередь приостановлена"
_LOCK_CAUSES = {errno.EACCES: "нет прав на папку замков", errno.EPERM: "нет прав на папку замков",
                errno.ENOTDIR: "вместо папки замков лежит файл", errno.EEXIST: "вместо папки замков лежит файл",
                errno.EMFILE: "у службы кончился лимит открытых файлов",
                errno.ENFILE: "в системе кончился лимит открытых файлов",
                errno.ENOSPC: "на диске нет места", errno.EROFS: "диск доступен только для чтения",
                errno.ENOLCK: "система не выдаёт блокировки файлов"}


def lock_problem(exc, folder):
    """Operator-facing reason of a job lock failure; stable text, so repeats are not logged again."""
    cause = _LOCK_CAUSES.get(exc.errno, "ошибка файла-замка")
    return f"{QUEUE_PAUSED}: {cause} ({folder}: {exc.strerror or exc})."


_QUEUE_CAUSES = {errno.ENOSPC: "на диске нет места", errno.EDQUOT: "на диске нет места",
                 errno.EMFILE: "у службы кончился лимит открытых файлов",
                 errno.ENFILE: "в системе кончился лимит открытых файлов",
                 errno.EROFS: "диск доступен только для чтения", errno.EIO: "ошибка чтения или записи диска",
                 errno.EACCES: "нет прав на папку очереди", errno.EPERM: "нет прав на папку очереди",
                 errno.ENOLCK: "система не выдаёт блокировки файлов"}


def queue_problem(exc, root):
    """Operator-facing reason the queue loop cannot go on (OSError or SQLite failure); stable text while the cause lasts."""
    root = Path(root)
    if isinstance(exc, OSError):
        cause, where = _QUEUE_CAUSES.get(exc.errno, "служба не может открыть файл очереди"), exc.filename or root
        detail = exc.strerror or exc
    else:
        # SQLite reports only "unable to open database file" or "disk I/O error"; measure the usual system causes.
        # Descriptors are counted, not probed: the failed run has already closed a few by now.
        cause, where, detail = "база очереди недоступна", root / "queue.sqlite3", exc
        try:
            limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            if shutil.disk_usage(root).free < 1024 * 1024:
                cause = "на диске нет места"
            elif limit != resource.RLIM_INFINITY and len(os.listdir("/dev/fd")) >= limit - 16:
                cause = _QUEUE_CAUSES[errno.EMFILE]
        except OSError as probe:
            cause = _QUEUE_CAUSES.get(probe.errno, cause)
    return (f"{QUEUE_PAUSED}: {cause} ({where}: {detail}). "
            "Ролики не потеряны. Студия повторяет попытку сама и продолжит после устранения причины.")


class QueueDatabaseError(sqlite3.OperationalError):
    """The queue database itself failed (full disk, no descriptors, no rights, locked); never a fault of one video."""


def new_owner():
    """Unique per worker launch: host, process and a random launch mark (PIDs are reused)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


class JobLock:
    """Held by the process that runs a job; the kernel releases it when that process dies."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The visible name appears already locked, so no one can read it as a dead owner.
            os.replace(tmp, self.path)
        except BaseException:
            os.close(fd)
            tmp.unlink(missing_ok=True)
            raise
        self._fd = fd

    def release(self):
        # Name first: once the lock is free, an existing name would read as a crashed owner.
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self._fd)


def _lock_state(path):
    """True: a live process holds the lock. False: the file is there but its process is gone.
    None: no lock file, nothing to prove either way."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


# «Название» (title) ролик не держит: сбой виден у шага, остальные шаги идут дальше (web_worker, publish.py).
STEPS = [("source", "Исходник"), ("skeleton", "Скелет"), ("expand", "Текст"), ("title", "Название"),
         ("scenes", "Паспорта сцен"),
         ("storyboard", "Раскадровка"), ("sheets", "Листы героев"),
         ("images", "Картинки"), ("judge", "Судья"), ("repair", "Пересдача"),
         ("fix", "Герои"), ("clean", "Чистка"), ("voice", "Озвучка"), ("align", "Тайминг"), ("render", "Сборка"),
         # «Обложка» идёт ПОСЛЕДНЕЙ. Ей нужен только паспорт наших героев словами (он
         # готов уже после «Листов»), а с «Картинками» она не связана ни одним файлом.
         # Очередь идёт по одному шагу: стой обложка в середине, её 2-10 минут задерживали
         # бы сам ролик, а её отказ задерживал бы его целиком. Ролик важнее обложки.
         ("cover", "Обложка")]
VOICES = {key: tuple(code for code, _ in spec["voices"]) for key, spec in LG.LANGS.items()}
_ID = re.compile(r"^[a-f0-9]{32}$")


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _calls(n: int) -> str:
    """1 вызов, 2 вызова, 5 вызовов."""
    word = ("вызов" if n % 10 == 1 and n % 100 != 11 else
            "вызова" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "вызовов")
    return f"{n} {word}"


def claude_report(journal, text_done=True) -> dict:
    """«Claude: токенов N, эквивалент $X, по шагам» из журнала вызовов ролика."""
    s = CP.summary(journal)
    cx = codex_report((s or {}).get("codex"))
    if not s or not s["calls"]:
        if s and s["codex"]["calls"]:
            note = "Вызовов Claude у этого ролика не было: текст шёл через Codex (итог Codex — ниже)."
        else:
            note = ("Вызовов Claude у этого ролика не записано (текст шёл через Codex или ролик сделан до счётчика)."
                    if text_done else "Вызовов Claude пока не было.")
        return dict(claude_tokens=None, claude_cost=None, claude_steps=None, claude_calls=0, claude_note=note, **cx)
    labels = {**dict(STEPS), "cover": "Обложка"}
    parts = []
    for key, st in s["steps"].items():
        name = labels.get(key, key or "без шага")
        parts.append(f"{name}: {_thousands(st['tokens'])} ток. + кэш {_thousands(st['cache'])}, ${st['cost_usd']:.2f}"
                     + (f" (расход неизвестен: {_calls(st['unknown'])})" if st["unknown"] else ""))
    routes = s.get("routes") or {}
    alt, sub = routes.get("alt", 0), s["calls"] - routes.get("alt", 0)
    if alt and sub:
        via = f"Вызовы Claude шли через альт-ключ ({_calls(alt)}) и через подписку Claude ({_calls(sub)}). "
    elif alt:
        via = "Вызовы Claude шли через альт-ключ (сторонний прокси), не через подписку Claude. "
    else:
        via = "Вызовы Claude шли через подписку Claude. "
    lean = s.get("lean", 0)
    cache_why = ("служебный промпт CLI снят лёгким режимом" if lean == s["calls"] else
                 "в основном служебный промпт CLI, ~23–42 тыс. на вызов"
                 + (f"; в лёгком режиме — {_calls(lean)}" if lean else ""))
    note = (via + f"Вызовов: {s['calls']}. Токены — вход без кэша {_thousands(s['input'])} + выход {_thousands(s['output'])}. "
            f"Отдельно кэш: чтение {_thousands(s['cache_read'])}, запись {_thousands(s['cache_write'])} "
            f"({cache_why}). "
            "Эквивалент — цена всех этих токенов, с кэшем и побочными вызовами CLI, по прайсу API; "
            + ("альт-ключ списывает токены своего остатка." if alt and not sub else
               "по подписке платится фиксированно, а тратится лимит окон 5 ч и 7 дней."))
    if s.get("cut"):
        note += f" Оборвано по длине: {_calls(s['cut'])}."
    if s["unknown"]:
        note += f" Расход неизвестен: {_calls(s['unknown'])} (CLI не отдал JSON) — итог занижен."
    return dict(claude_tokens=_thousands(s["tokens"]), claude_cost=f"${s['cost_usd']:.2f}",
                claude_steps=" · ".join(parts), claude_calls=s["calls"], claude_note=note, **cx)


def codex_report(c) -> dict:
    """«Codex: вызовов N, токенов M, по учёткам и шагам» — отдельно от Claude. Учётки — по имени."""
    if not c or not c.get("calls"):
        return dict(codex_calls=0, codex_tokens=None, codex_note="")
    labels = {**dict(STEPS), "cover": "Обложка"}
    known = c["calls"] - c["unknown"]
    tokens = c["input"] - c["cache_read"] + c["output"]
    alt = (c.get("routes") or {}).get("alt", 0)
    accounts = ", ".join(f"«{name}» {n}" for name, n in sorted(c["accounts"].items(), key=lambda kv: -kv[1])
                         if name != "alt")
    via = ("через альт-ключ" if alt == c["calls"] else
           f"через подписки ChatGPT (учётки: {accounts})" + (f", через альт-ключ {_calls(alt)}" if alt else ""))
    steps = " · ".join(f"{labels.get(k, k or 'без шага')}: {_calls(v['calls'])}"
                       + (f", {_thousands(v['tokens'])} ток." if v["calls"] > v["unknown"] else "")
                       for k, v in c["steps"].items())
    note = f"Codex: {_calls(c['calls'])} {via}, всего {round(c['seconds'] / 60)} мин работы CLI. "
    if known:
        note += (f"Токены — вход без кэша {_thousands(c['input'] - c['cache_read'])} + выход {_thousands(c['output'])}; "
                 f"отдельно вход из кэша {_thousands(c['cache_read'])}. ")
    if c["unknown"]:
        note += f"Расход токенов нет данных: {_calls(c['unknown'])} (CLI не отдал usage). "
    if c["failed"]:
        note += f"Без ответа: {_calls(c['failed'])}. "
    note += "По подписке платится фиксированно, тратятся окна 5 ч и неделя. По шагам: " + steps + "."
    return dict(codex_calls=c["calls"], codex_tokens=_thousands(tokens) if known else None, codex_note=note)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    tmp.replace(path)


def _safe_error(error):
    # Error pages must never echo provider request bodies or credentials.
    value = str(error)
    value = re.sub(r"data:image/[^\s\"']+", "[изображение]", value)
    # «Bearer sk-...» прячем ПЕРВЫМ: правило про authorization съедало слово Bearer и
    # оставляло сам ключ в тексте, который потом уходит на страницу ролика и в журнал.
    value = re.sub(r"(?i)bearer\s+\S+", "Bearer [скрыто]", value)
    value = re.sub(r"(?i)(api[-_ ]?key|authorization|token|secret)([\s\"']*[:=][\s\"']*)[^\s,}\"']+",
                   r"\1\2[скрыто]", value)
    return value[:700]


def video_stamp(path):
    """Версия файла видео: «размер:время изменения». None — файла нет."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_size}:{st.st_mtime_ns}"


def _when(ts):
    return time.strftime("%d.%m в %H:%M", time.localtime(ts))


def archived_refusal(archive):
    """Честный отказ «Повторить»/пересдачи у ролика, чьи кадры убраны после выдачи."""
    state = "удалены" if archive.get("finished") else "удаляются"
    return (f"Кадры этого ролика {state} уборкой после выдачи ({_when(archive['started'])}, {archive['reason']}). "
            "На месте видео, обложка и название с описанием (если студия их сделала), озвучка, тексты, листы героев и квитанции. "
            "Пересдать или принять кадр и собрать видео заново уже нельзя: для этого пришлось бы заново покупать "
            "все картинки. Студия ничего не заказала. Нужна другая версия — закажите ролик заново.")


class QueueStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "projects").mkdir(exist_ok=True)
        self.db_path = self.root / "queue.sqlite3"
        with self._db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS project_requests (
              token TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, project_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, action TEXT NOT NULL,
              frame_id INTEGER, correction TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
              error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS steps (
              project_id TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
              started REAL, finished REAL, seconds REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(project_id, key));
            CREATE TABLE IF NOT EXISTS manual_accepts (
              project_id TEXT NOT NULL, frame_id INTEGER NOT NULL, accepted REAL NOT NULL,
              original TEXT NOT NULL, image_stamp TEXT NOT NULL,
              PRIMARY KEY(project_id, frame_id));
            CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created);
            CREATE TABLE IF NOT EXISTS deliveries (
              project_id TEXT PRIMARY KEY, delivered REAL NOT NULL, how TEXT NOT NULL, video_stamp TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS archives (
              project_id TEXT PRIMARY KEY, started REAL NOT NULL, reason TEXT NOT NULL, finished REAL);
            """)
            # Two processes may open an old file at once; check and alter under one write lock.
            db.execute("BEGIN IMMEDIATE")
            if "note" not in {r[1] for r in db.execute("PRAGMA table_info(steps)")}:
                db.execute("ALTER TABLE steps ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            job_columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "owner" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            if "heartbeat" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN heartbeat REAL")
            # Автоповтор временного сбоя (watch.py): задача не выдаётся раньше not_before; auto_reason — чем был сбой.
            if "not_before" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN not_before REAL")
            if "auto_reason" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN auto_reason TEXT NOT NULL DEFAULT ''")
            # Шаг «Исходник» появился 17.09.2026: у роликов до него исходником был вставленный текст — шаг готов.
            db.execute("INSERT OR IGNORE INTO steps(project_id,key,status) SELECT id,'source','complete' FROM projects")

    @contextmanager
    def _db(self):
        try:
            db = sqlite3.connect(self.db_path, timeout=30)
        except sqlite3.OperationalError as exc:
            raise QueueDatabaseError(*exc.args) from exc
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        except sqlite3.OperationalError as exc:
            if isinstance(exc, QueueDatabaseError):
                raise
            raise QueueDatabaseError(*exc.args) from exc
        finally:
            db.close()

    def project_path(self, project_id):
        if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
            raise ValueError("Неверный номер ролика")
        path = (self.root / "projects" / project_id).resolve()
        path.relative_to(self.root / "projects")
        return path

    def has_project(self, project_id):
        """Check registration without loading frame metadata or filesystem media."""
        if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
            return False
        with self._db() as db:
            return db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is not None

    def _data(self, project_id, db=None):
        self.project_path(project_id)
        if db is None:
            with self._db() as connection:
                return self._data(project_id, connection)
        row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return json.loads(row[0])

    # Что берём у ролика-эталона, когда делаем тот же сюжет на другом языке. Текст, доли и
    # картинки НЕ берём: текст переводится, картинки рисуются заново своим зерном.
    TRANSLATE_FILES = ("text/skeleton.json", "text/scenes.json", "text/storyboard.json",
                       "text/memory.json", "text/locks.json", "text/locks_text.json",
                       "text/style.json", "text/sfx_fallback.json", "text/heights.json")
    TRANSLATE_DIRS = ("sheets", "places", "plans")

    def create_project(self, title, source_text, minutes, style, language, voice, frame_limit=None,
                       idempotency_key=None, source_url=None, channel=None, source_project=None):
        title = str(title or "").strip()
        source_text = str(source_text or "").strip()
        link = {}
        if source_url:
            # Текст ролика по ссылке получает шаг «Исходник», а не этот запрос.
            vid = SRC.video_id(source_url)
            if not vid:
                raise ValueError("Ссылка не похожа на ролик YouTube")
            if source_text:
                raise ValueError("Оставьте один исходник: ссылку или текст")
            link = dict(source_url=SRC.canonical_url(vid), video_id=vid, title_from_source=not title)
            title = title or f"Ролик YouTube {vid}"
        if not title or len(title) > 160:
            raise ValueError("Название: от 1 до 160 символов")
        base = None
        if source_project:
            base = self.project_path(str(source_project))
            if not (base / "text" / "final.txt").exists() or not (base / "text" / "storyboard.json").exists():
                raise ValueError("Ролик-эталон не найден или у него ещё нет текста и раскадровки")
            source_text = ""
        if not link and not base and (len(source_text) < 20 or len(source_text) > 2_000_000):
            raise ValueError("Вставьте текст: от 20 до 2 000 000 символов")
        if not link and not base and len(source_text) > SRC.max_chars():
            # Тот же предел, что у шага «Исходник»: отказ сразу в форме, а не падение шага в очереди.
            raise ValueError(str(SRC._too_long(len(source_text), SRC.max_chars(), "Текст")))
        try:
            parsed_minutes = float(minutes)
        except (ValueError, TypeError):
            raise ValueError("Длительность должна быть числом") from None
        if not parsed_minutes.is_integer() or not 1 <= parsed_minutes <= 180:
            raise ValueError("Длительность: целое число от 1 до 180 минут")
        if style not in ("S0", "S1", "S2", "S3", "S4"):
            raise ValueError("Выберите стиль S0–S4")
        if language not in VOICES or voice not in VOICES[language]:
            raise ValueError("Голос должен соответствовать языку")
        if frame_limit is not None and (type(frame_limit) is not int or not 1 <= frame_limit <= 12):
            raise ValueError("Проверочный предел: от 1 до 12 кадров")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or
                                            not 1 <= len(idempotency_key) <= 160):
            raise ValueError("Неверный токен создания ролика")
        # Канал (пресет из channels.json): ключ для фильтра и название на карточке — как было в момент заказа.
        mark = {}
        if channel is not None:
            key, name = (channel.get("key"), channel.get("name")) if isinstance(channel, dict) else (None, None)
            if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,59}", key) \
                    or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                raise ValueError("Неверный канал ролика")
            mark = dict(channel=key, channel_name=name.strip())
        params = dict(title=title, minutes=int(parsed_minutes), style=style, language=language,
                      voice=voice, frame_limit=frame_limit, **link, **mark)
        if base is not None:
            params["source_project"] = str(source_project)
        fingerprint = hashlib.sha256(json.dumps({**params, "source_text": source_text},
                                               sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM project_requests WHERE token=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("Этот токен уже использован с другими параметрами ролика")
                # reused: ролик уже был (повторная отправка или гонка двух одинаковых пачек).
                return {**self.get_project(existing["project_id"]), "reused": True}
            pid, now = uuid.uuid4().hex, time.time()
            data = dict(id=pid, **params, created_at=now)
            folder = self.project_path(pid)
            (folder / "text").mkdir(parents=True)
            if base is not None:
                # Тот же сюжет на другом языке: берём у эталона разбор истории, раскадровку и
                # листы героев. Текст переведёт шаг «Текст», картинки будут свои.
                for rel in self.TRANSLATE_FILES:
                    src = base / rel
                    if src.exists():
                        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, folder / rel)
                for name in self.TRANSLATE_DIRS:
                    if (base / name).is_dir():
                        shutil.copytree(base / name, folder / name, dirs_exist_ok=True)
                write_json(folder / "text" / "source_project.json",
                           {"project": str(source_project), "at": now})
                (folder / "text" / "source.txt").write_text(
                    "Тот же сюжет, что у ролика " + str(source_project)[:8] + ", на другом языке.",
                    encoding="utf-8")
            elif not link:
                (folder / "text" / "source.txt").write_text(source_text, encoding="utf-8")
            write_json(folder / "project.json", data)
            db.execute("INSERT INTO projects VALUES(?,?,?)", (pid, json.dumps(data), now))
            db.executemany("INSERT INTO steps(project_id,key,status) VALUES(?,?,'pending')",
                           [(pid, key) for key, _ in STEPS])
            db.execute("INSERT INTO jobs(id,project_id,action,status,created) VALUES(?,?,'run','pending',?)",
                       (uuid.uuid4().hex, pid, now))
            if idempotency_key is not None:
                db.execute("INSERT INTO project_requests VALUES(?,?,?)", (idempotency_key, fingerprint, pid))
        return self.get_project(pid)

    def project_for_request(self, token):
        """Ролик, уже созданный запросом с этим токеном (пачка ищет так дубли); None — такого не было."""
        with self._db() as db:
            row = db.execute("SELECT project_id FROM project_requests WHERE token=?", (token,)).fetchone()
        return row[0] if row else None

    def set_source_title(self, project_id, title, job=None):
        """Название оригинала для ролика по ссылке без своего названия; своё название владельца не трогается."""
        title = str(title or "").strip()[:160]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if job is not None and not db.execute("SELECT 1 FROM jobs WHERE id=? AND owner=? AND status='running'",
                                                  (job["id"], job["owner"])).fetchone():
                return False
            data = self._data(project_id, db)
            if not title or not data.get("title_from_source") or data.get("title") == title:
                return True
            data["title"] = title
            write_json(self.project_path(project_id) / "project.json", data)
            db.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(data), project_id))
            return True

    def enqueue(self, project_id, action="run", frame_id=None, correction=""):
        # title — сделать заново только название и описание (кнопка на странице ролика).
        if action not in ("run", "retry", "frame_retry", "title"):
            raise ValueError("Неизвестное действие")
        if action == "frame_retry":
            if type(frame_id) is not int or frame_id < 1:
                raise ValueError("Неверный номер кадра")
            self.get_frame(project_id, frame_id)
        elif frame_id is not None:
            raise ValueError("Номер кадра подходит только для пересдачи кадра")
        correction = str(correction or "").strip()
        if len(correction) > 4000:
            raise ValueError("Правка должна быть короче 4000 символов")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._data(project_id, db)
            archive = self._archive(db, project_id)
            # «Название — сделать заново» читает только тексты (text/), уборка их не трогает и картинок он не покупает:
            # у убранного ролика кнопка работает. Остальные действия — честный отказ.
            if archive and action != "title":
                # Same write lock as begin_archive: either the cleanup sees this job or this sees the mark.
                raise ValueError(archived_refusal(archive))
            # Full run and per-frame operations must not race or duplicate one another.
            active = db.execute("SELECT * FROM jobs WHERE project_id=? AND status IN ('pending','running') ORDER BY created",
                                (project_id,)).fetchall()
            for row in active:
                # Ждущее «название» не поглощает кнопки ролика: они встают в очередь следом (claim даёт ролику
                # одну задачу за раз). Кнопка «название» при любой идущей задаче ролика возвращает её.
                if row["action"] == "title" and action != "title":
                    continue
                if action != "frame_retry" or row["action"] != "frame_retry" or row["frame_id"] == frame_id:
                    return dict(row)
            if action == "retry":
                last = db.execute("SELECT * FROM jobs WHERE project_id=? AND action != 'title' ORDER BY created DESC LIMIT 1",
                                  (project_id,)).fetchone()
                if last and last["action"] == "frame_retry" and last["status"] == "error":
                    # Resume the same accepted candidate and durable seed; a new
                    # explicit frame_retry button still creates a new candidate.
                    db.execute("UPDATE jobs SET status='pending',finished=NULL,error='' WHERE id=?", (last["id"],))
                    return dict(db.execute("SELECT * FROM jobs WHERE id=?", (last["id"],)).fetchone())
            jid, now = uuid.uuid4().hex, time.time()
            db.execute("INSERT INTO jobs(id,project_id,action,frame_id,correction,status,created) VALUES(?,?,?,?,?,'pending',?)",
                       (jid, project_id, action, frame_id, correction, now))
            if action == "title":
                # Сбросить только ошибку самого шага: ошибки шагов ролика остаются видны оператору.
                db.execute("UPDATE steps SET status='pending',error='' WHERE project_id=? AND key='title' AND status='error'",
                           (project_id,))
            elif action != "frame_retry":
                db.execute("UPDATE steps SET status='pending',error='' WHERE project_id=? AND status='error'", (project_id,))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def list_projects(self):
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM projects ORDER BY created DESC")]
        return [self.get_project(pid) for pid in ids]

    # ---------- выдача и уборка после выдачи ----------
    @staticmethod
    def _archive(db, project_id):
        row = db.execute("SELECT * FROM archives WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def archive_info(self, project_id):
        """{started, reason, finished} — кадры ролика убираются или убраны после выдачи; None — нет."""
        with self._db() as db:
            return self._archive(db, project_id)

    def begin_archive(self, project_id, reason, now=None):
        """Отметка «убирается после выдачи» ДО первого удаления. False — у ролика есть задача: не трогать."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('pending','running') LIMIT 1",
                          (project_id,)).fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO archives(project_id,started,reason) VALUES(?,?,?)",
                       (project_id, time.time() if now is None else now, str(reason)[:200]))
            return True

    def finish_archive(self, project_id, now=None):
        with self._db() as db:
            db.execute("UPDATE archives SET finished=? WHERE project_id=? AND finished IS NULL",
                       (time.time() if now is None else now, project_id))

    def _delivery(self, db, project_id):
        row = db.execute("SELECT * FROM deliveries WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            return None
        current = video_stamp(self.project_path(project_id) / "out" / "video.mp4")
        return {"delivered": row["delivered"], "how": row["how"], "current": row["video_stamp"] == current}

    def delivery_info(self, project_id):
        """{delivered, how, current}: когда ролик отмечен скачанным; current — отметка про то видео, что лежит сейчас."""
        with self._db() as db:
            return self._delivery(db, project_id)

    def mark_delivered(self, project_id, how, stamp=None):
        """how: button — отметил оператор; download — сервер отдал видео целиком. Повтор той же версии
        не сдвигает время отметки (от него считается уборка после выдачи)."""
        if how not in ("button", "download"):
            raise ValueError("Неверный способ отметки")
        project = self.get_project(project_id)
        if not project["video_ready"]:
            raise ValueError("Видео ещё не готово — отмечать скачанным пока нечего.")
        current = video_stamp(self.project_path(project_id) / "out" / "video.mp4")
        stamp = stamp or current
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT video_stamp FROM deliveries WHERE project_id=?", (project_id,)).fetchone()
            if row and row[0] == stamp:
                return self._delivery(db, project_id)
            db.execute("INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?)", (project_id, time.time(), how, stamp))
            return self._delivery(db, project_id)

    def unmark_delivered(self, project_id):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._data(project_id, db)
            if self._archive(db, project_id):
                raise ValueError("Отметку уже не снять: кадры ролика убраны после выдачи.")
            db.execute("DELETE FROM deliveries WHERE project_id=?", (project_id,))

    def get_project(self, project_id):
        data = self._data(project_id)
        with self._db() as db:
            saved = {r["key"]: dict(r) for r in db.execute("SELECT * FROM steps WHERE project_id=?", (project_id,))}
            jobs = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC", (project_id,))]
            # Автоповтор, ждущий паузы, места в очереди не занимает: у него плашка «начнётся сам через N мин».
            pending = [r[0] for r in db.execute("SELECT id FROM jobs WHERE status='pending' "
                                                "AND (not_before IS NULL OR not_before<=?) ORDER BY created", (time.time(),))]
            archive = self._archive(db, project_id)
            delivery = self._delivery(db, project_id)
        # Задача «сделать название заново» не меняет состояния ролика: его показывает последняя другая задача,
        # идёт ли название или уже закончилось.
        main = [j for j in jobs if j["action"] != "title"]
        active_main = next((j for j in reversed(main) if j["status"] in ("running", "pending")), None)
        title_active = next((j for j in reversed(jobs) if j["action"] == "title" and j["status"] in ("running", "pending")), None)
        active = active_main or title_active
        latest = active_main or (main[0] if main else title_active)
        # Ролик, собранный до шага «Название» (18.09.2026): строки шага нет, а «Сборка» уже готова.
        # Строки остальных шагов заводятся при создании ролика, поэтому их наличие ничего не значит:
        # несобранный старый ролик шаг «Название» ждёт и получает его в цепочке.
        old_video = "title" not in saved and saved.get("render", {}).get("status") == "complete"
        # Ролик, собранный до шага «Чистка» (19.09.2026): строки шага нет, а «Сборка» уже готова.
        old_clean = "clean" not in saved and saved.get("render", {}).get("status") == "complete"
        frames = self.list_frames(project_id, include_details=False)
        path = self.project_path(project_id)
        video = path / "out" / "video.mp4"
        acceptance = read_json(path / "out" / "acceptance.json", {})
        # Frames removed by the cleanup after delivery do not make a delivered video unready.
        ready = bool(frames) and (bool(archive) or all(f["image"] for f in frames)) and video.is_file() \
            and video.stat().st_size > 0 and acceptance.get("ok") is True
        steps = []
        for key, label in STEPS:
            s = saved.get(key, {})
            if key == "title" and old_video:
                s = dict(status="complete", note="Пропущено: ролик собран до шага «Название». "
                                                 "Название и описание можно сделать кнопкой на странице ролика.")
            if key == "clean" and old_clean:
                s = dict(status="complete", note="Пропущено: ролик собран до шага «Чистка».")
            seconds = s.get("seconds", 0)
            if s.get("status") == "running" and s.get("started"):
                seconds += time.time() - s["started"]
            status, note = s.get("status", "pending"), s.get("note", "")
            if not s and ready and not any(j["status"] in ("running", "pending") for j in jobs):
                # Сданный ролик, сделанный ДО появления этого шага: строки о шаге в базе
                # нет вовсе. Показывать его вечно «ждёт» — значит держать у готового ролика
                # незакрытый пункт, которого никто не делал и делать не собирался.
                status = "skipped"
                # Обложку старому ролику можно заказать кнопкой — и это покупка (4-12 единиц),
                # поэтому заметка говорит об этом прямо, а не «не делалась» и только.
                note = ("Шаг появился позже этого ролика: он не делался, а кадры и черновики ролика уже убраны — "
                        "обложку не доделать." if archive else
                        "Шаг появился позже этого ролика: он не делался. «Повторить незавершённое» "
                        "сделает обложку (если ролик собран по ссылке или это перевод такого ролика) и потратит генерации.")
            steps.append(dict(key=key, label=label, status=status,
                              started_at=s.get("started"), finished_at=s.get("finished"),
                              seconds=round(seconds, 2), error=s.get("error", ""), note=note))
        status = latest["status"] if latest else "pending"
        if status == "complete" and not ready:
            status = "error"
        error = _safe_error(latest.get("error", "")) if latest else ""
        if status == "error" and not error:
            error = "Готовый ролик или часть кадров отсутствуют"
        # «Название» идёт рядом с цепочкой ролика: текущим показывается шаг самого ролика, если он есть.
        running = [s["key"] for s in steps if s["status"] == "running"]
        current = next((k for k in running if k != "title"), running[0] if running else None)
        # Сбой «Названия» ролик не останавливает: в сводку и Telegram идёт шаг самого ролика.
        failed_step = next((s["label"] for s in steps if s["status"] == "error" and s["key"] != "title"), "")
        auto = None
        if latest and latest.get("auto_reason"):
            # Автоповтор после временного сбоя (watch.py): видно на странице ролика и в сводке.
            auto = dict(reason=_safe_error(latest["auto_reason"]), created=latest["created"],
                        not_before=latest.get("not_before"), status=latest["status"])
        if active and active["action"] == "frame_retry":
            current = "frame_retry"
        return {**data, "status": status, "steps": steps, "current_step": current,
                "title_job": title_active["status"] if title_active else None,
                "retry_frame_id": active.get("frame_id") if active and active["action"] == "frame_retry" else None,
                "finished_at": latest.get("finished") if latest else None, "failed_step": failed_step, "auto_retry": auto,
                "error": error, "error_hint": "Нажмите «Повторить незавершённое». Готовые файлы сохранятся." if error else "",
                "frame_count": len(frames), "ready_frames": sum(bool(f["image"]) for f in frames),
                # Ролик готов уже тогда, когда недоделана только «Обложка»: она стоит ПОСЛЕ
                # «Сборки» и видео не трогает. Это и шаг в работе, и «Повторить
                # незавершённое» ради одной обложки, которое ждёт в очереди часами.
                "video_ready": ready and (not active_main or (
                    active_main["action"] != "frame_retry"
                    # «Название» ролик не держит (как failed_step и current_step): его сбой не прячет видео.
                    and all(s["status"] in ("complete", "skipped") for s in steps if s["key"] not in ("cover", "title")))),
                "queue_position": pending.index(active["id"]) + 1 if active and active["id"] in pending else None,
                "archived": archive, "delivery": delivery,
                # Никогда не начинался: защита диска (MANGA_DISK_MIN_GB) не выдаёт такие ролики.
                "never_started": bool(active_main) and active_main["action"] == "run" and all(s["status"] == "pending" for s in steps)}

    def list_frames(self, project_id, *, include_details=True):
        """Read current visible state; load selected prompts/refs only for detail views."""
        self._data(project_id)
        path = self.project_path(project_id)
        sb = read_json(path / "text" / "storyboard.json", {})
        verdicts = read_json(path / "judge" / "verdicts.json", {})
        prompts = read_json(path / "frames" / "_jobs.json", {}) if include_details else {}
        with self._db() as db:
            overrides = {r["frame_id"]: dict(r) for r in db.execute("SELECT * FROM manual_accepts WHERE project_id=?", (project_id,))}
            busy = {r[0] for r in db.execute("SELECT frame_id FROM jobs WHERE project_id=? AND action='frame_retry' AND status IN ('pending','running')", (project_id,))}
            full_busy = bool(db.execute("SELECT 1 FROM jobs WHERE project_id=? AND action != 'frame_retry' AND status IN ('pending','running')", (project_id,)).fetchone())
        output = []
        for fd in sb.get("frames", []):
            idx = int(fd["idx"])
            image = path / "frames" / f"{idx:05d}.png"
            valid = image.is_file() and image.stat().st_size > 4096
            stamp = f"{image.stat().st_size}:{image.stat().st_mtime_ns}" if valid else ""
            manual = idx in overrides and overrides[idx]["image_stamp"] == stamp
            v = verdicts.get(str(idx), {})
            prompt = prompts.get(str(idx), {})
            selected = read_json(str(image) + ".selected.json", {}) if include_details else {}
            if selected.get("job_path"):
                try:
                    selected_job_path = (path / selected["job_path"]).resolve()
                    selected_job_path.relative_to(path)
                    selected_job = read_json(selected_job_path, {})
                    prompt = {**prompt, "prompt": selected_job.get("prompt", prompt.get("prompt", ""))}
                except (ValueError, TypeError):
                    pass
            reasons = list((v.get("row") or {}).get("notes") or [])
            if v.get("error"):
                reasons.append(_safe_error(v["error"]))
            if not valid:
                reasons.insert(0, "Картинка ещё не готова")
            refs = []
            for ref in prompt.get("references", []):
                rp = ref.get("path")
                try:
                    resolved = (path / rp).resolve() if rp else None
                    if resolved:
                        resolved.relative_to(path)
                    safe = str(resolved.relative_to(path)) if resolved and resolved.is_file() else None
                except (ValueError, OSError, TypeError):
                    safe = None
                refs.append({"name": str(ref.get("name", "Эталон")) + (" — эталон недоступен" if safe is None else ""), "path": safe})
            frame = dict(idx=idx, scene_num=fd.get("scene_num", 0), verdict=v.get("verdict", "unknown"),
                         reasons=reasons, judge_row=v.get("row"), image=f"frames/{idx:05d}.png" if valid else None)
            if include_details:
                frame.update(prompt=prompt.get("prompt", fd.get("prompt_en", "")), references=refs, blank=fd)
            frame.update(manual_accepted=manual, busy=full_busy or idx in busy)
            output.append(frame)
        return output

    def get_frame(self, project_id, frame_id):
        for frame in self.list_frames(project_id):
            if frame["idx"] == frame_id:
                return frame
        raise KeyError(frame_id)

    def accept_frame(self, project_id, frame_id):
        archive = self.archive_info(project_id)
        if archive:
            raise ValueError(archived_refusal(archive))
        frame = self.get_frame(project_id, frame_id)
        if not frame["image"]:
            raise ValueError("Сначала дождитесь картинки")
        if frame["busy"]:
            raise ValueError("Дождитесь завершения пересдачи")
        path = self.project_path(project_id)
        image = path / frame["image"]
        stamp = f"{image.stat().st_size}:{image.stat().st_mtime_ns}"
        original = read_json(path / "judge" / "verdicts.json", {}).get(str(frame_id), {})
        now = time.time()
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO manual_accepts VALUES(?,?,?,?,?)",
                       (project_id, frame_id, now, json.dumps(original), stamp))
        return {**frame, "manual_accepted": True, "accepted_at": now, "original_verdict": original}

    def report(self, project_id):
        project = self.get_project(project_id)
        frames = self.list_frames(project_id, include_details=False)
        path = self.project_path(project_id)
        repairs = read_json(path / "frames" / "_repair.json", [])
        identity_fix = read_json(path / "frames" / "_fix_identity.json", {})
        web_repairs = [read_json(p, {}) for p in (path / "retries").glob("*/state.json")]
        receipts = {}
        for receipt_path in path.rglob("*.generation.json"):
            receipt = read_json(receipt_path, {})
            for attempt in receipt.get("attempts", []):
                if attempt.get("id"):
                    receipts[attempt["id"]] = attempt
        known_units = [a.get("quota_units") for a in receipts.values()]
        units = sum(known_units) if known_units and all(type(v) in (int, float) for v in known_units) else None
        note = ("По принятым запросам и тарифу модели; возвраты провайдера не измерены. Общая квота других сервисов не учитывается."
                if units is not None else "Точный расход недоступен: нет полной истории принятых запросов. Общая квота других сервисов не учитывается.")
        text_done = any(st["key"] == "expand" and st["status"] == "complete" for st in project.get("steps", []))
        return dict(**claude_report(path / "text" / "llm_usage.jsonl", text_done=text_done),
                    accepted=sum(f["verdict"] == "accept" or f["manual_accepted"] for f in frames),
                    warn=sum(f["verdict"] == "warn" and not f["manual_accepted"] for f in frames),
                    reject=sum(f["verdict"] == "reject" and not f["manual_accepted"] for f in frames),
                    retried=sum(len(r.get("frames", [])) for r in repairs) + int(identity_fix.get("sent", 0)) + len(web_repairs),
                    returned_original=sum(r.get("returned_original", 0) for r in repairs) + int(identity_fix.get("returned_original", 0)) + sum(r.get("outcome") == "returned_original" for r in web_repairs),
                    fastgen_units=units,
                    fastgen_units_note=note,
                    steps=project["steps"], video_ready=project["video_ready"],
                    preview=bool(project.get("frame_limit")),
                    auto_repair_tested=not project.get("frame_limit") and all(
                        s["status"] == "complete" for s in project["steps"] if s["key"] in ("repair", "fix")))

    # Worker-only state transitions. SQLite owns the invariants: every change of a
    # running job is a compare-and-set on (status, owner); see the module header.
    def job_lock_path(self, job):
        owner = hashlib.sha256(str(job["owner"]).encode()).hexdigest()[:16]
        return self.root / "job_locks" / f"{job['id']}.{owner}.lock"

    def hold_job(self, job):
        """Taken by the worker for the whole run of a claimed job; see guarantee 2."""
        return JobLock(self.job_lock_path(job))

    def recover(self, lease_seconds=LEASE_SECONDS):
        """Return orphaned running jobs to pending; a job whose process is alive stays untouched."""
        now = time.time()
        recovered, dead_locks = {}, []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,project_id,owner,heartbeat,error FROM jobs WHERE status='running'").fetchall():
                path = self.job_lock_path(row)
                try:
                    held, unreadable = _lock_state(path), None
                except OSError as exc:
                    held, unreadable = None, exc
                noted = row["error"].startswith(QUEUE_PAUSED)
                if held or (held is None and row["heartbeat"] is not None and row["heartbeat"] >= now - lease_seconds):
                    if noted:
                        self._note_running(db, row, "")
                        log.warning("Ролик %s (проект %s): пауза снята, его обработчик жив", row["id"], row["project_id"])
                    continue
                if unreadable is not None:
                    # Whether the owner lives is unknown, and a second run would buy its steps twice: never reset on a guess.
                    note = _safe_error(lock_problem(unreadable, path.parent) + (
                        " Нельзя проверить, жив ли обработчик этого ролика, поэтому ролик не сброшен."
                        " Пока он числится в работе, новые ролики не выдаются."
                        " Студия проверяет снова сама и продолжит после устранения причины."))
                    if row["error"] != note:
                        self._note_running(db, row, note)
                        log.error("Ролик %s (проект %s): %s", row["id"], row["project_id"], note)
                    continue
                # owner/heartbeat stay as a record of the last claim; they matter only while running.
                changed = db.execute("UPDATE jobs SET status='pending',started=NULL,error='' "
                                     "WHERE id=? AND status='running' AND owner=? AND heartbeat IS ?",
                                     (row["id"], row["owner"], row["heartbeat"])).rowcount
                if changed == 1:
                    recovered[row["id"]] = row
                if held is False:
                    dead_locks.append(path)
            # A running step belongs to its project's running job; reset it only when none is left.
            for row in db.execute("SELECT project_id,key,started,seconds FROM steps WHERE status='running' AND NOT EXISTS "
                                  "(SELECT 1 FROM jobs WHERE jobs.project_id=steps.project_id AND jobs.status='running')").fetchall():
                # Count work up to the owner's last sign of life, not the time it lay dead.
                last_alive = max((r["heartbeat"] for r in recovered.values()
                                  if r["project_id"] == row["project_id"] and r["heartbeat"] is not None), default=now)
                elapsed = max(0, min(now, last_alive) - (row["started"] or now))
                db.execute("UPDATE steps SET status='pending',started=NULL,seconds=? WHERE project_id=? AND key=? AND status='running'",
                           (row["seconds"] + elapsed, row["project_id"], row["key"]))
        for path in dead_locks:
            # The owner of this exact (job, launch) pair is dead and never comes back.
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                # The job is already back in the queue; a new claim locks another name, this empty file is never read.
                log.warning("Ролик возвращён в очередь, но файл замка умершего обработчика не удалён: %s", exc)
        return list(recovered)

    @staticmethod
    def _note_running(db, row, note):
        db.execute("UPDATE jobs SET error=? WHERE id=? AND status='running' AND owner=?", (note, row["id"], row["owner"]))

    def unclaim(self, job, note):
        """Give back a claimed job whose run could not start: pending again, steps untouched, the reason visible."""
        with self._db() as db:
            return db.execute("UPDATE jobs SET status='pending',started=NULL,error=? WHERE id=? AND status='running' AND owner=?",
                              (_safe_error(note), job["id"], job["owner"])).rowcount == 1

    def paused_reason(self):
        """The reason the queue cannot start or check a waiting job (see QUEUE_PAUSED); None while it flows."""
        with self._db() as db:
            row = db.execute("SELECT error FROM jobs WHERE status IN ('pending','running') AND substr(error,1,?)=? "
                             "ORDER BY created LIMIT 1", (len(QUEUE_PAUSED), QUEUE_PAUSED)).fetchone()
        return row[0] if row else None

    def claim(self, owner=None, limit=1, new_videos=True):
        """limit — сколько задач очереди могут идти одновременно (MANGA_PARALLEL_VIDEOS); 1 — как раньше.
        Ролик никогда не выдаётся второму исполнителю, пока его задача идёт: одна задача на ролик.
        new_videos=False (мало места на диске): не выдаются ролики, у которых не начат ни один шаг;
        начатые ролики и пересдачи кадров идут дальше."""
        owner = owner or new_owner()
        fresh = "" if new_videos else (
            " AND NOT (action='run' AND NOT EXISTS (SELECT 1 FROM steps WHERE steps.project_id=jobs.project_id"
            " AND steps.status != 'pending'))")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            # not_before: автоповтор ждёт своей паузы (watch.py); у обычных задач его нет.
            free = ("(not_before IS NULL OR not_before<=?) "
                    "AND project_id NOT IN (SELECT project_id FROM jobs WHERE status='running')")
            # Кнопка «Название — сделать заново»: один короткий вызов модели. Идёт первой и сверх лимита
            # параллели (одна такая задача за раз), чтобы не ждать часами за роликами пачки и не занимать их место.
            row = None
            if not db.execute("SELECT 1 FROM jobs WHERE status='running' AND action='title'").fetchone():
                row = db.execute(f"SELECT * FROM jobs WHERE status='pending' AND action='title' AND {free} "
                                 "ORDER BY created LIMIT 1", (now,)).fetchone()
            if row is None:
                if db.execute("SELECT COUNT(*) FROM jobs WHERE status='running' AND action != 'title'"
                              ).fetchone()[0] >= max(1, int(limit)):
                    return None
                # fresh: мало места на диске (MANGA_DISK_MIN_GB) — не начатые ролики не выдаются.
                row = db.execute(f"SELECT * FROM jobs WHERE status='pending' AND {free}" + fresh +
                                 " ORDER BY created LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            # error='' drops the note of an earlier start that could not take the job lock.
            if db.execute("UPDATE jobs SET status='running',started=?,owner=?,heartbeat=?,error='' WHERE id=? AND status='pending'",
                          (now, owner, now, row["id"])).rowcount != 1:
                return None
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def others_waiting(self, project_id) -> bool:
        """Ждёт ли в очереди задача ДРУГОГО ролика."""
        with self._db() as db:
            return bool(db.execute("SELECT 1 FROM jobs WHERE status='pending' AND project_id!=? LIMIT 1",
                                   (project_id,)).fetchone())

    def heartbeat(self, job_id, owner):
        """Mark the owner's job alive. False: the job is no longer this owner's running job."""
        with self._db() as db:
            return db.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND owner=? AND status='running'",
                              (time.time(), job_id, owner)).rowcount == 1

    def step(self, project_id, key, status, error="", note="", job=None):
        """With job (a claimed row), the write happens only while that owner still runs it."""
        if key not in dict(STEPS) or status not in ("pending", "running", "complete", "error"):
            raise ValueError("Неверное состояние шага")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if job is not None and not db.execute("SELECT 1 FROM jobs WHERE id=? AND owner=? AND status='running'",
                                                  (job["id"], job["owner"])).fetchone():
                return False
            old = db.execute("SELECT * FROM steps WHERE project_id=? AND key=?", (project_id, key)).fetchone()
            if old is None:
                # У роликов до шага «Название» (18.09.2026) и «Обложка» строки шага нет: заводится при первой
                # записи. OR IGNORE: раньше здесь падал TypeError, исполнитель умирал, и вставала вся очередь.
                db.execute("INSERT OR IGNORE INTO steps(project_id,key,status) VALUES(?,?,'pending')", (project_id, key))
                old = db.execute("SELECT * FROM steps WHERE project_id=? AND key=?", (project_id, key)).fetchone()
            now = time.time()
            seconds = old["seconds"]
            if old["status"] == "running" and old["started"]:
                seconds += max(0, now - old["started"])
            db.execute("UPDATE steps SET status=?,started=?,finished=?,seconds=?,error=?,note=? WHERE project_id=? AND key=?",
                       (status, now if status == "running" else old["started"], now if status in ("complete", "error") else None,
                        seconds, _safe_error(error), str(note)[:700], project_id, key))
            return True

    def fail_and_retry(self, job, error, pause_seconds, reason):
        """Закрыть задачу ошибкой и в той же записи поставить ОДИН автоповтор через паузу (watch.py).
        Как «Повторить незавершённое»: готовые шаги сохраняются, упавший начнётся заново. False — задача не наша."""
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE jobs SET status='error',finished=?,error=? WHERE id=? AND status='running' AND owner=?",
                          (now, _safe_error(error), job["id"], job["owner"])).rowcount != 1:
                return False
            db.execute("INSERT INTO jobs(id,project_id,action,status,created,not_before,auto_reason) "
                       "VALUES(?,?,'retry','pending',?,?,?)",
                       (uuid.uuid4().hex, job["project_id"], now, now + max(0.0, float(pause_seconds)),
                        _safe_error(reason)[:300] or "временный сбой"))
            db.execute("UPDATE steps SET status='pending',error='' WHERE project_id=? AND status='error'", (job["project_id"],))
            return True

    def finish(self, job_id, error="", paused=False, owner=None, then_retry=False):
        """Close a running job. With owner, only that owner may close it; returns whether it did.

        then_retry: в ТОЙ ЖЕ транзакции поставить «retry» этого ролика в конец очереди (отложенная
        обложка). Раньше retry ставился отдельным шагом после закрытия задачи, и падение службы
        между ними оставляло обложку «ждёт» навсегда."""
        guard, params = ("", ()) if owner is None else (" AND owner=?", (owner,))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            done = db.execute("UPDATE jobs SET status=?,finished=?,error=? WHERE id=? AND status='running'" + guard,
                              ("pending" if paused else "error" if error else "complete", None if paused else time.time(),
                               _safe_error(error), job_id, *params)).rowcount == 1
            if done and then_retry and not paused and not error:
                row = db.execute("SELECT project_id FROM jobs WHERE id=?", (job_id,)).fetchone()
                busy = db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('pending','running')",
                                  (row["project_id"],)).fetchone()
                if not busy:
                    db.execute("INSERT INTO jobs(id,project_id,action,frame_id,correction,status,created) "
                               "VALUES(?,?,'retry',NULL,'','pending',?)", (uuid.uuid4().hex, row["project_id"], time.time()))
                    db.execute("UPDATE steps SET status='pending',error='' WHERE project_id=? AND status='error'",
                               (row["project_id"],))
            return done
