#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Уборка исходников проекта — только по квитанции рендера и только под замком.

ЗАЧЕМ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Прежняя уборка (`main._sweep_project_sources`) сносила
`Images`, `.thumbs`, `googler video` и все `_FINAL_AUDIO_*` целиком, как только в
каталоге `render/` находился ЛЮБОЙ файл `*.mp4`. Этот mp4 мог остаться от прошлого
рендера, быть оборванным на половине или вообще чужим — проверки не было никакой.
В бою ровно эта версия стёрла 761 МБ исходников, которые нечем восстановить: кадры
оплачены поштучно, озвучка — посимвольно, сцены — отдельным платным планом.

ЧТО ЗДЕСЬ ДРУГОГО. Удаление разрешает не наличие файла, а КВИТАНЦИЯ: строка задачи
рендера в статусе `done`, у которой заполнены `output_rel`, `render_sha256` и
`render_size` (их пишет приём результата). Перед удалением финальный mp4 читается
целиком и сверяется с квитанцией, а каждый исходник удаляется по точному совпадению
`(dev, ino, size, mtime_ns)` через `dir_fd` с `O_NOFOLLOW` — то есть «снести каталог»
превращается в «снести ровно те файлы, которые я проверил секунду назад».

ГЛАВНОЕ УМОЛЧАНИЕ. Нет квитанции — нет уборки. Проект, отрендеренный до появления
колонок квитанции, не будет убран НИКОГДА: `latest_render` вернёт
`'legacy render lacks exact output receipt'`. Это осознанный выбор — место на старых
проектах само не освободится, но и второго «минус 761 МБ» не случится. Разовый
backfill квитанций остаётся отдельным явным действием владельца.

Английские тексты причин отказа (`'no completed render'`, `'source path changed'`, …)
оставлены ДОСЛОВНО как в боевом `source_retention.py`: по ним дёшево диффать нашу
версию против production. Русские объяснения живут в комментариях, а не в строках.

Что не трогаем никогда: `render/` (готовый mp4 — ради него всё), `_RENDER_MEDIA/`
(материалы юзера, их неоткуда взять заново), `_ASSETS/`, `_style_refs/`, `source.txt`
и любые `*.json` (внутри `googler/` лежат `scenes.json` и `scenes.meta.json` — план
сцен, самое дорогое в стеке). Сюда же по умолчанию добавился `Images/` — это ручные
материалы пользователя, и отличить их от выхлопа старого Bing-режима нечем; подробный
разбор с привязкой к строкам кабинета — у `IMAGES_DIR` ниже.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import uuid

from pub_calls import open_calls

# Задача считается «не активной» только в терминальном статусе. `canceled` с одной `l`
# в нашей схеме не встречается, но оставлен: строка могла приехать из чужого снимка БД,
# а ошибиться здесь — значит счесть живую задачу мёртвой и убрать её вход.
TERMINAL = ('done', 'error', 'cancelled', 'canceled')

# Расширения, которые вообще могут быть кандидатами на удаление. Всё остальное
# (json, txt, srt) — не медиа и переживает уборку независимо от того, где лежит.
MEDIA = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif',
         '.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac',
         '.mp4', '.mov', '.webm'}
IMAGE = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}

# Каталоги-кандидаты. `googler/` разбирается отдельно (там же лежит scenes.json),
# остальные берутся целиком, включая вложенные папки.
#
# `Images/` В ЭТОМ СПИСКЕ НЕТ, хотя в боевом `source_retention.py` он есть
# (`_RETENTION_DIRS = ("googler video", "Images", ".thumbs")`, live 18.09 main.py:11633).
# Причина: в `Images/**` лежат РУЧНЫЕ материалы пользователя. Кабинет читает их как
# полноправный источник картинок наравне с генерацией — `_materials_ready`
# (main.py ≈2948), `_video_ready` (≈3471), `_DL_PATTERNS['images']` (≈5636),
# `_gallery_image_ok` (≈9419), «зеркалим googler/ + Images/» в `_build_cmd` (≈7917);
# у `_images_ratio_ok` (≈3007) прямо написано «ручные материалы, где image-job никогда
# не было». Ни одна ручка кабинета их не создаёт и, значит, не восстановит: ни у нас,
# ни в боевом main.py нет ни одного места, которое пишет в `Images/`, кроме
# `image_worker.py` (IMAGE_DIR_NAME='Images') в СТАРОМ режиме тегов Bing — и тот
# кладёт строго в `Images/<номер тега>/` (`download_images_for_tag`, img_folder =
# base_img_dir/str(index), image_worker.py:295). Всё остальное в `Images/` попало туда
# руками, и доказать обратное по коду нечем.
#
# Отсюда умолчание `keep` — «не доказано, что моё, значит не трогаю». Спорность в том,
# что место на проектах со старым Bing-режимом так не освободится, поэтому выбор
# вынесен в настройку, а не зашит:
#   keep      (умолчание) — `Images/` не трогаем вообще;
#   generated — удаляем только `Images/<цифры>/**`, ровно ту раскладку, которую
#               создаёт image_worker в режиме тегов. Ослабление сознательное: папку
#               с именем из одних цифр может сделать и человек, нумеруя кадры;
#   all       — как в бою: весь `Images/**` считается выхлопом генерации.
IMAGES_DIR = 'Images'
IMAGES_MODES = ('keep', 'generated', 'all')
IMAGES_ENV = 'VS_SOURCE_RETENTION_IMAGES'
SOURCE_DIRS = ('.thumbs', 'googler video')
SOURCE_DIR_PREFIX = '_FINAL_AUDIO_'

# Виды задач очереди, которые НЕ являются «новой генерацией»: они не пишут ни одного
# файла в каталоги-кандидаты, поэтому после них исходники всё ещё описывают тот самый
# отрендеренный ролик. Список закрытый и обоснован построчно — любой НЕназванный вид
# (в том числе будущий, ещё не написанный) по-прежнему запрещает уборку:
#   chapters — читает готовое медиа, пишет chapters/*.txt|json (main.py `_build_cmd`
#              ≈7608: воркеру отдаётся только --project_dir и пороги);
#   cards    — карточка загрузки. Зависит только от source.txt, а пишет строго в
#              `_cards/v1/job-<id>/*.json` (card_jobs.artifact_rel:378). Кабинет уже
#              считает её не-входом рендера: `_project_has_active_work` (main.py ≈3439)
#              исключает 'cards' из задач, которых рендер обязан дождаться.
# Без этого списка любая карточка загрузки, сделанная после рендера, дисквалифицировала
# бы проект НАВСЕГДА: строка `cards` остаётся в очереди вечно, и гейт «новая генерация
# после последнего рендера» срабатывал бы на каждом проходе уборки до скончания века.
NON_SOURCE_KINDS = ('chapters', 'cards')

MARKER = '_sources_purged.json'
LOCK_PREFIX = 'storage_cleanup:'
CURSOR_KEY = 'source_retention_cursor'


class Retained(ValueError):
    """Проект оставлен нетронутым. Текст исключения — причина отказа, она же ключ отчёта."""


@contextmanager
def connection(database):
    """Короткое соединение на одну транзакцию.

    Отдельное соединение, а не общее с сервером: уборка берёт писательскую блокировку
    на секунды и не должна держать её в чужом соединении, которое кто-то переиспользует.
    `with db` коммитит при выходе и откатывает при исключении — любой `Retained` внутри
    транзакции не оставляет после себя половину записи.
    """
    db = sqlite3.connect(database, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def held(value, now=None):
    """Занят ли проект уборкой прямо сейчас.

    Любая неразборчивая запись считается ЗАНЯТО (возврат True). Это единственный
    безопасный ответ: перепутать «замок сломан» с «замка нет» — значит начать
    удаление под чужой уборкой. Цена ошибки в другую сторону — временный отказ.
    """
    try:
        value = json.loads(value) if isinstance(value, str) else value
        expiry = float(value['expires_at'])
        # Без владельца запись бессмысленна, а бесконечность (inf/nan) — признак
        # порчи: такую аренду не снять никогда, поэтому считаем её занятой, но
        # пишем как занятую, а не как вечную (снимет только владелец или админ).
        return (not value.get('owner')
                or not math.isfinite(expiry)
                or expiry > (time.time() if now is None else now))
    except (ValueError, TypeError, KeyError, AttributeError):
        return True


def sha(path):
    """sha256 файла по мегабайту. Читается целиком — это и есть доказательство,
    что на диске лежит именно тот mp4, про который отчиталась нода."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            digest.update(block)
    return digest.hexdigest()


def inside(path, root):
    """Путь обязан быть настоящим файлом внутри каталога проекта.

    Симлинк отвергается ДО resolve(): иначе «исходник» мог бы указывать на чужой
    файл вне проекта, и уборка удалила бы его, пройдя все остальные проверки.
    """
    path = Path(path)
    root = Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise Retained('source path changed')
    return path.resolve()


def atomic_json(path, value):
    """Записать маркер так, чтобы он либо был целым, либо его не было вовсе.

    Маркер — АУДИТ («здесь убирали, освободили столько»), а не разрешение на
    следующую уборку: гейтом он был в прежней версии, и из-за этого проект,
    перегенерировавший материалы, не убирался уже никогда.
    """
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.source-retention-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        # fsync каталога: без него переименование может не пережить внезапную
        # перезагрузку, и файлы окажутся удалены, а следа об этом не останется.
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def latest_render(db, project_id, cutoff):
    """Найти рендер, по квитанции которого можно убирать. Иначе — причина отказа.

    Порядок проверок важен: сначала «проект вообще спит», потом «есть завершённый
    рендер», и только в конце «у него есть квитанция». Так причина в отчёте
    называет первое настоящее препятствие, а не последнее.
    """
    rows = [dict(r) for r in db.execute('SELECT * FROM queue_tasks WHERE project_id=?', (project_id,))]
    if any(r.get('status') not in TERMINAL for r in rows):
        raise Retained('project has active work')
    renders = [r for r in rows if r.get('kind') == 'render' and r.get('status') == 'done']
    if not renders:
        raise Retained('no completed render')
    latest = max(renders, key=lambda r: float(r.get('finished') or r.get('created') or 0))
    stamp = float(latest.get('finished') or latest.get('created') or 0)
    if stamp <= 0 or stamp >= cutoff:
        raise Retained('latest render younger than retention')
    # Этап, не переделывающий исходники (NON_SOURCE_KINDS), уборке не мешает. Любой
    # другой более поздний этап (в том числе неизвестный будущий) означает, что
    # материалы переделывали, и уборка молчит.
    if any(r.get('kind') not in NON_SOURCE_KINDS and r['job_id'] != latest['job_id']
           and max(float(r.get('created') or 0), float(r.get('finished') or 0)) > stamp
           for r in rows):
        raise Retained('new generation after latest render')
    # Вот он, главный гейт задачи. Без квитанции мы знаем только, что mp4 когда-то
    # существовал, — ровно то знание, которым прежняя уборка стёрла 761 МБ.
    if not latest.get('render_sha256') or not latest.get('render_size') or not latest.get('output_rel'):
        raise Retained('legacy render lacks exact output receipt')
    latest['source_cutoff'] = stamp
    return latest


def remote_settled(database, project_id):
    """Нет ли у проекта оплаченных вызовов с невыясненной судьбой.

    В бою незавершённые операции лежат файлами в `.remote_jobs/*.json`; у нас такой
    папки нет — журнал платных вызовов живёт в таблице `provider_calls`. Замена
    один в один по смыслу: `open_calls` отдаёт строки в `reserved` и `ordered`, то
    есть «деньги, возможно, потрачены, а результата на диске ещё нет». Удалить
    исходники под такой строкой — значит выбросить кадр, за который уже заплачено,
    и лишить повтор возможности узнать, что заказ вообще был.
    """
    with connection(database) as db:
        if open_calls(db, project_id):
            raise Retained('remote operation unresolved')


def images_mode(value=None):
    """Режим уборки `Images/`: настройка владельца, а не догадка кода.

    Неизвестное значение (опечатка в .env) трактуется как `keep`, а не как ошибка:
    уборка удаляет невосстановимое, и падать ей тут нельзя, но и разрешать удаление
    по непонятой настройке — тем более. Смотри комментарий у `IMAGES_DIR`.
    """
    raw = (os.environ.get(IMAGES_ENV, '') or '') if value is None else value
    raw = str(raw).strip().lower()
    return raw if raw in IMAGES_MODES else 'keep'


def _generated_by_tag_mode(path, images_root):
    """Лежит ли файл в `Images/<цифры>/…` — единственной раскладке, которую создаёт
    сам кабинет (`image_worker.download_images_for_tag`, старый режим тегов Bing)."""
    parts = path.relative_to(images_root).parts
    return len(parts) > 1 and parts[0].isascii() and parts[0].isdigit()


def _tree_media(directory, keep=None):
    """Медиа-файлы всего дерева. `keep` — дополнительный фильтр по пути."""
    found = []
    for walk, dirs, files in os.walk(directory, followlinks=False):
        # Симлинк-каталог внутри дерева — повод остановиться целиком, а не
        # пропустить его: непонятно, что ещё в этом проекте подменили.
        if any((Path(walk) / d).is_symlink() for d in dirs):
            raise Retained('source directory changed')
        for name in files:
            if Path(name).suffix.lower() not in MEDIA:
                continue
            path = Path(walk) / name
            if keep is None or keep(path):
                found.append(path)
    return found


def source_files(root, stamp, images=None):
    """Список кандидатов на удаление: путь + его точная личность на диске.

    Возвращается кортеж `(path, dev, ino, size, mtime_ns)` — по нему потом сверяется
    файл непосредственно перед `unlink`. Пересоздание файла между проверкой и
    удалением меняет ino/mtime, и удаления не будет.

    `images` — режим для `Images/`; по умолчанию берётся из настройки. Отдельным
    аргументом, а не только из окружения, чтобы тест проверял все три режима без
    переустановки переменных на живом процессе.
    """
    images = images_mode(images)
    candidates = []
    googler = root / 'googler'
    if googler.is_symlink():
        raise Retained('source directory changed')
    if googler.is_dir():
        # Только картинки и только верхним уровнем: рядом лежат scenes.json и
        # scenes.meta.json — план сцен, который переживает уборку всегда.
        candidates.extend(p for p in googler.iterdir() if p.suffix.lower() in IMAGE)
    if images != 'keep':
        pics = root / IMAGES_DIR
        if pics.is_symlink():
            raise Retained('source directory changed')
        if pics.is_dir():
            candidates.extend(_tree_media(
                pics, None if images == 'all'
                else lambda path: _generated_by_tag_mode(path, pics)))
    for directory in root.iterdir():
        if directory.name not in SOURCE_DIRS and not directory.name.startswith(SOURCE_DIR_PREFIX):
            continue
        if directory.is_symlink():
            raise Retained('source directory changed')
        if directory.is_dir():
            candidates.extend(_tree_media(directory))
    result = []
    for path in candidates:
        path = inside(path, root)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise Retained('source is not regular')
        # Файл новее рендера — значит он в этот ролик не попал; возможно, человек
        # уже готовит следующий. Удалять чужую работу по чужой квитанции нельзя.
        if info.st_mtime > stamp:
            raise Retained('source edited after latest render')
        result.append((path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return result


def _unlink_exact(root, entry):
    """Удалить ровно тот файл, который был проверен, — и никакой другой.

    Спуск по каталогам идёт через `dir_fd` и `O_NOFOLLOW`, поэтому подменённый на
    симлинк промежуточный каталог не уведёт удаление наружу. Личность файла
    сверяется в последний момент, уже по открытому каталогу: между `source_files`
    и этой строкой проект мог начать новую генерацию.
    """
    path, device, inode, size, mtime = entry
    relative = path.relative_to(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        info = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (device, inode, size, mtime):
            raise Retained('source changed during retention')
        os.unlink(relative.name, dir_fd=fd)
    finally:
        os.close(fd)


def purge_project(database, projects_root, project_id, cutoff, deadline,
                  lease_seconds=120, batch_size=64, dry_run=False, images=None):
    """Убрать исходники одного проекта. Любой отказ — исключение `Retained` с причиной.

    `dry_run=True` проходит все проверки, но ничего не удаляет, не пишет маркер И НЕ
    БЕРЁТ ЗАМОК. Замок — не диагностика, а запрет: пока он стоит, проект не получает
    ни новой задачи (`_enqueue` → 409), ни выдачи рендера, ни удаления. Брать его в
    режиме, который заведомо ничего не удалит, значит мешать работе ВЫКЛЮЧЕННОЙ
    уборкой — а выключена она по умолчанию. Чужой замок отчёт по-прежнему уважает:
    читать его дёшево и правильно, это ответ «проект сейчас занят».

    Сверка финального mp4 (полное чтение) в сухом режиме ОСТАЁТСЯ: отчёт отвечает на
    вопрос «убрался бы этот проект», и без квитанции он отвечал бы на него неправдой.
    Стоимость ограничена бюджетом прохода.

    `images` — режим уборки `Images/` (см. `images_mode`); None = настройка владельца.
    """
    key = LOCK_PREFIX + str(project_id)
    owner = uuid.uuid4().hex
    acquired = False
    freed = 0
    removed = 0
    try:
        with connection(database) as db:
            # BEGIN IMMEDIATE: проверка замка и его взятие — одна транзакция, иначе
            # два тика планировщика (или тик и ручной запуск) возьмут один проект.
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT v FROM admin_state WHERE k=?', (key,)).fetchone()
            if old and held(old['v']):
                raise Retained('project has maintenance owner')
            row = db.execute('SELECT tenant_id, dir_path FROM projects WHERE id=?', (project_id,)).fetchone()
            if not row:
                raise Retained('project missing')
            # `dir_path` пишется при создании проекта и равен раскладке
            # `<tenants>/<tenant>/projects/<pid>`. Пустая колонка встречается у
            # строк, приехавших из старых снимков, — для них берём раскладку.
            # Обе ветки всё равно обязаны оказаться ВНУТРИ projects_root.
            dir_path = row['dir_path'] or os.path.join(
                projects_root, row['tenant_id'] or '', 'projects', str(project_id))
            root = inside(dir_path, projects_root)
            latest = latest_render(db, project_id, cutoff)
            # Замок ставит только боевой проход: см. докстроку про dry_run.
            if not dry_run:
                db.execute('INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)',
                           (key, json.dumps({'owner': owner,
                                             'expires_at': time.time() + lease_seconds}),
                            time.time()))
                acquired = True
        # Пустой набор кандидатов не требует сверки квитанции: удалять нечего, а
        # чтение многогигабайтного mp4 на каждом проходе уборки — заметная нагрузка
        # на диск. Здесь же оказываются уже убранные проекты.
        entries = source_files(root, latest['source_cutoff'], images)
        if not entries:
            return {'state': 'no_sources', 'freed_bytes': 0, 'removed': 0}
        remote_settled(database, project_id)
        final = inside(root / latest['output_rel'], root)
        final_info = final.stat()
        if (not stat.S_ISREG(final_info.st_mode)
                or final_info.st_size != latest['render_size']
                or sha(final) != latest['render_sha256']):
            raise Retained('final video differs from render receipt')
        after_hash = final.stat()
        # Файл могли переписать ПОКА мы его читали: тогда посчитанный sha относится
        # к смеси старого и нового, и доказательством он не является.
        if (after_hash.st_dev, after_hash.st_ino, after_hash.st_size, after_hash.st_mtime_ns) != (
                final_info.st_dev, final_info.st_ino, final_info.st_size, final_info.st_mtime_ns):
            raise Retained('final video changed during verification')
        for start in range(0, len(entries), batch_size):
            if time.monotonic() >= deadline:
                raise Retained('retention time budget exhausted')
            with connection(database) as db:
                db.execute('BEGIN IMMEDIATE')
                if not dry_run:
                    lease = db.execute('SELECT v FROM admin_state WHERE k=?', (key,)).fetchone()
                    try:
                        lease = json.loads(lease['v']) if lease else {}
                    except (TypeError, ValueError):
                        lease = {}
                    expiry = float(lease.get('expires_at', 0))
                    # Аренда могла истечь, пока мы читали mp4: тогда проект уже мог
                    # получить новую задачу, и продолжать удаление нельзя. В сухом
                    # режиме аренды нет вовсе — проверять нечего и не нужно: ни одна
                    # строка ниже ничего не удалит.
                    if (lease.get('owner') != owner or not math.isfinite(expiry)
                            or expiry <= time.time()):
                        raise Retained('retention lease expired')
                current = latest_render(db, project_id, cutoff)
                if current['job_id'] != latest['job_id']:
                    raise Retained('render receipt changed')
                info = final.stat()
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                        final_info.st_dev, final_info.st_ino, final_info.st_size, final_info.st_mtime_ns):
                    raise Retained('final video changed during retention')
                remote_settled(database, project_id)
                for entry in entries[start:start + batch_size]:
                    if time.monotonic() >= deadline:
                        raise Retained('retention time budget exhausted')
                    if not dry_run:
                        _unlink_exact(root, entry)
                    freed += entry[3]
                    removed += 1
        if dry_run:
            # Ни файла, ни маркера: отчётный проход обязан быть неотличим от
            # «ничего не делали» по следам на диске.
            return {'state': 'would_purge', 'freed_bytes': freed, 'removed': removed}
        atomic_json(root / MARKER, {'purged_at': time.time(), 'freed_bytes': freed,
                                    'render_job_id': latest['job_id'],
                                    'render_sha256': latest['render_sha256'], 'removed': removed})
        return {'state': 'purged', 'freed_bytes': freed, 'removed': removed}
    except Retained as exc:
        # Частично убранный проект — нормальный исход (кончился бюджет, истекла
        # аренда). Сколько успели освободить, отчёт обязан знать.
        exc.freed_bytes = freed
        exc.removed = removed
        raise
    finally:
        if acquired:
            with connection(database) as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT v FROM admin_state WHERE k=?', (key,)).fetchone()
                try:
                    same = old and json.loads(old['v']).get('owner') == owner
                except (ValueError, TypeError):
                    same = False
                # Снимаем ТОЛЬКО свою аренду: чужую (перехваченную после истечения)
                # трогать нельзя, иначе двое разом решат, что проект свободен.
                if same:
                    db.execute('DELETE FROM admin_state WHERE k=?', (key,))


def sweep(database, projects_root, retention_days=7, budget_seconds=20, dry_run=False,
          images=None):
    """Обойти проекты, чей последний рендер старше срока хранения. Отчёт — словарь.

    Бюджет времени обязателен: уборка живёт в тике планировщика, и обход сотен
    каталогов не должен останавливать раздачу задач.

    Режим `Images/` попадает в отчёт: по умолчанию ручные материалы не трогаются, и
    читающий отчёт обязан видеть, какой уборке принадлежат цифры (см. `images_mode`).
    """
    images = images_mode(images)
    started = time.monotonic()
    deadline = started + max(0, float(budget_seconds))
    cutoff = time.time() - float(retention_days) * 86400
    with connection(database) as db:
        ids = [r[0] for r in db.execute(
            "SELECT project_id FROM queue_tasks WHERE kind='render' AND status='done' "
            "GROUP BY project_id HAVING MAX(COALESCE(finished,created))<? ORDER BY project_id",
            (cutoff,))]
        row = db.execute('SELECT v FROM admin_state WHERE k=?', (CURSOR_KEY,)).fetchone()
        try:
            cursor = json.loads(row['v']).get('last_project_id', '') if row else ''
        except (ValueError, TypeError, AttributeError):
            cursor = ''
        if not isinstance(cursor, str):
            cursor = ''
    # Курсор меняет только порядок обхода; все гейты по каждому проекту остаются на
    # месте. Нужен он ради одного случая: первый по алфавиту тяжёлый проект не должен
    # после каждого рестарта съедать весь бюджет и душить остальных.
    ids = [pid for pid in ids if pid > cursor] + [pid for pid in ids if pid <= cursor]
    report = {'projects': 0, 'freed_bytes': 0, 'retained': {}, 'images': images}
    if dry_run:
        report['dry_run'] = True
        report['would_purge'] = 0
        report['would_free_bytes'] = 0
    for project_id in ids:
        if time.monotonic() >= deadline:
            break
        try:
            result = purge_project(database, projects_root, project_id, cutoff, deadline,
                                   dry_run=dry_run, images=images)
            if result['state'] == 'purged':
                report['projects'] += 1
                report['freed_bytes'] += result['freed_bytes']
            elif result['state'] == 'would_purge':
                report['would_purge'] += 1
                report['would_free_bytes'] += result['freed_bytes']
        except (Retained, OSError, sqlite3.Error, ValueError) as exc:
            # Причина отказа — ключ отчёта. Неожиданная ошибка (ENOSPC, битая БД)
            # называется типом: она тоже означает «проект оставлен», а не «убран».
            partial = getattr(exc, 'freed_bytes', 0)
            if dry_run:
                report['would_free_bytes'] += partial
            else:
                report['freed_bytes'] += partial
            reason = str(exc) if isinstance(exc, Retained) else type(exc).__name__
            report['retained'][reason] = report['retained'].get(reason, 0) + 1
        finally:
            # Курсор двигается и после отказа — иначе один вечно непригодный проект
            # навсегда закрыл бы очередь всем остальным.
            with connection(database) as db:
                db.execute('INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)',
                           (CURSOR_KEY, json.dumps({'last_project_id': project_id}), time.time()))
    report['elapsed_seconds'] = time.monotonic() - started
    return report
