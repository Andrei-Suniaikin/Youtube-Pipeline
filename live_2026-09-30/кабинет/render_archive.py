# -*- coding: utf-8 -*-
"""Готовый ролик уехал в объектное хранилище — но для всех читателей он «на месте».

Задача. На диске VPS 325 ГиБ готовых mp4, и растёт на 13 ГБ в сутки. Унести их в
хранилище мешает то, что файл читают из 58 мест: кабинет, движок публикаций, приёмка
с рендер-ноды, заливщик, наши скрипты волн. Переписывать 58 мест нельзя.

Решение. Рядом с унесённым роликом остаётся указатель — крохотный json того же имени
плюс «.archived». Маска `render/*.mp4` его НЕ видит, поэтому ни один старый читатель
не подсунет вместо унесённого ролика соседний чужой mp4 (движок публикаций берёт
«самый свежий mp4 в каталоге» — это была главная опасность). А тот, кто хочет знать
правду, смотрит наличие одного файла: ни базы, ни ключей от хранилища не нужно.

    было:  render/17654_7acde.mp4            158 МБ
    стало: render/17654_7acde.mp4.archived   200 байт  +  копия в Contabo

Возврат прозрачный: ensure_local() скачивает файл обратно на прежнее место под тем же
именем, сверяя размер и контрольную сумму. Для заливщика это обычная отдача по HTTP —
про хранилище он не знает и ключей не имеет.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time

УКАЗАТЕЛЬ = ".archived"
REMOTE = os.environ.get("VS_ARCHIVE_REMOTE", "contabo:vsgen-archive")
RCLONE = os.environ.get("VS_ARCHIVE_RCLONE", "/usr/bin/rclone")
КУСОК = 1024 * 1024

СХЕМА = """
CREATE TABLE IF NOT EXISTS render_archive(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  abs_path TEXT NOT NULL,
  job_id INTEGER,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  remote_key TEXT NOT NULL,
  state TEXT NOT NULL,
  local_present INTEGER NOT NULL DEFAULT 1,
  lease_owner TEXT, lease_until REAL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created REAL NOT NULL, uploaded REAL, verified REAL,
  deleted_at REAL, restored_at REAL, last_read REAL,
  UNIQUE(tenant_id, project_id, rel_path));
CREATE UNIQUE INDEX IF NOT EXISTS idx_render_archive_key ON render_archive(remote_key);
CREATE INDEX IF NOT EXISTS idx_render_archive_state ON render_archive(state, local_present);
"""


def ensure_schema(db):
    """Таблица заводится и кабинетом при старте, и грузчиком — кто первый."""
    db.executescript(СХЕМА)


# ───────────────────────── указатель ─────────────────────────

def pointer_path(path: str) -> str:
    return str(path) + УКАЗАТЕЛЬ


def read_pointer(path: str):
    """Запись указателя или None. Битый указатель — это None, а не исключение:
    читатель тогда увидит «файла нет» и не подставит чужой ролик."""
    try:
        with open(pointer_path(path), "rb") as fh:
            d = json.loads(fh.read())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("remote_key") and d.get("sha256") else None


def write_pointer(path: str, запись: dict) -> None:
    """Атомарно: временный файл рядом, fsync, os.replace, fsync каталога."""
    каталог = os.path.dirname(path) or "."
    fd, врем = tempfile.mkstemp(dir=каталог, prefix=".archived-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(запись, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(врем, pointer_path(path))
        _fsync_dir(каталог)
    except BaseException:
        try: os.unlink(врем)
        except OSError: pass
        raise


def _fsync_dir(каталог: str) -> None:
    fd = os.open(каталог, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def resolve(path: str):
    """('local', путь) | ('archived', запись) | ('missing', None).

    Порядок важен: сначала реальный файл. Если он на месте — указатель не смотрим,
    иначе гонка «вернули файл, но указатель ещё не убрали» дала бы лишнее скачивание.
    """
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return "local", path
    except OSError:
        pass
    запись = read_pointer(path)
    return ("archived", запись) if запись else ("missing", None)


# ───────────────────────── контрольная сумма ─────────────────────────

def sha256_файла(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            кусок = fh.read(КУСОК)
            if not кусок:
                break
            h.update(кусок)
    return h.hexdigest()


# ───────────────────────── замок на один ролик ─────────────────────────

class _Замок:
    """Два запроса на один унесённый ролик не должны тянуть его дважды."""

    def __init__(self, path):
        self.путь = pointer_path(path) + ".lock"
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.путь, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


# ───────────────────────── возврат из хранилища ─────────────────────────

def _rclone(*args, **kw):
    return subprocess.run([RCLONE, *args], capture_output=True, **kw)


def ensure_local(path: str, timeout: int = 1800) -> str:
    """Вернуть настоящий локальный путь, при необходимости скачав ролик обратно.

    Файл встаёт на своё прежнее место под прежним именем — для всех читателей
    ничего не изменилось. Указатель снимается только после успешной сверки.
    """
    вид, что = resolve(path)
    if вид == "local":
        return path
    if вид == "missing":
        raise FileNotFoundError(path)
    запись = что
    with _Замок(path):
        вид, _ = resolve(path)          # пока ждали замок, мог вернуть сосед
        if вид == "local":
            return path
        каталог = os.path.dirname(path) or "."
        fd, врем = tempfile.mkstemp(dir=каталог, prefix=".restore-")
        os.close(fd)
        try:
            r = _rclone("copyto", f'{REMOTE}/{запись["remote_key"]}', врем,
                        "--contimeout", "30s", "--timeout", "10m", "-q", timeout=timeout)
            if r.returncode != 0:
                raise OSError(f"не скачался из хранилища: {r.stderr.decode('utf-8', 'replace')[:200]}")
            размер = os.path.getsize(врем)
            if размер != int(запись["size"]):
                raise OSError(f"размер не сошёлся: {размер} вместо {запись['size']}")
            сумма = sha256_файла(врем)
            if сумма != запись["sha256"]:
                raise OSError("контрольная сумма не сошлась")
            os.chmod(врем, 0o644)
            os.replace(врем, path)
            _fsync_dir(каталог)
        except BaseException:
            try: os.unlink(врем)
            except OSError: pass
            raise
        try:
            os.unlink(pointer_path(path))
            _fsync_dir(каталог)
        except OSError:
            pass
    return path


def stream(path: str, chunk: int = KB if (KB := 1024 * 512) else 0):
    """Отдать байты ролика сразу, не дожидаясь полной загрузки.

    Заливщик ждёт первый байт ограниченное время, поэтому молча восстанавливать файл
    в сторонке нельзя. Читаем поток из хранилища, одновременно пишем во временный файл,
    и если сумма сошлась — ставим его на место: повторное скачивание пойдёт уже с диска.
    """
    вид, что = resolve(path)
    if вид == "local":
        with open(path, "rb") as fh:
            while True:
                кусок = fh.read(chunk)
                if not кусок:
                    return
                yield кусок
    if вид == "missing":
        raise FileNotFoundError(path)
    запись = что
    каталог = os.path.dirname(path) or "."
    fd, врем = tempfile.mkstemp(dir=каталог, prefix=".restore-")
    h = hashlib.sha256()
    прочитано = 0
    p = subprocess.Popen([RCLONE, "cat", f'{REMOTE}/{запись["remote_key"]}',
                          "--contimeout", "30s", "--timeout", "10m"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with os.fdopen(fd, "wb") as буфер:
            while True:
                кусок = p.stdout.read(chunk)
                if not кусок:
                    break
                h.update(кусок)
                прочитано += len(кусок)
                буфер.write(кусок)
                yield кусок
        p.wait(timeout=60)
        if p.returncode == 0 and прочитано == int(запись["size"]) and h.hexdigest() == запись["sha256"]:
            os.chmod(врем, 0o644)
            os.replace(врем, path)
            _fsync_dir(каталог)
            try:
                os.unlink(pointer_path(path))
                _fsync_dir(каталог)
            except OSError:
                pass
            врем = None
    finally:
        if p.poll() is None:
            p.kill()
        if врем:
            try: os.unlink(врем)
            except OSError: pass
