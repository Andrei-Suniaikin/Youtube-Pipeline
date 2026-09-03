"""
Render Worker v4.4.11 - Автонастройка по железу

🚀 НОВОЕ в v4.4.11:
- auto_tune: определение CPU, RAM, GPU и подбор оптимальных параметров
- render_settings: max_pool_workers, video_batch_size, image_convert_workers
- Параллельная конвертация картинок (PIL) при 8+ изображениях

v4.4.10:
- Цикл стоков: повторяем ВЕСЬ ordered (видео+картинки), сохраняя чередование до конца
- duration_per_image: учёт fade overlap — d = (remaining + (n-1)*fade) / n
- images.txt: уникальное имя на основе output_name (seg_timeline_NNNN_list.txt)
- copy2 → rename/move для видео-сегментов (экономия I/O)

v4.4.9:
- Единый пайплайн для ВСЕХ режимов material_order (video_first, images_first, alternate, alphabetical, manual, random)
- Исправлен stock_video_cycle: цикл видео теперь корректно работает во всех режимах
- Исправлен stock_video_limit: фильтрация до построения timeline, нет "дыр" в порядке
- Исправлен video_path_to_processed: индексный маппинг вместо path (дубликаты не перетираются)
- Исправлен remaining_time: гарантированный бюджет для картинок при cycle
- xfade между КАЖДЫМ элементом (видео и картинками), а не между блоками
- Уникальные имена temp-файлов (seg_timeline_NNNN) — нет конфликтов при параллельной обработке

v4.4.8:
- FPS: -r и -vsync cfr во всех выходах (слайдшоу, xfade, видео-батчи)
- fps filter в _create_fade_batch для стабильного framerate
- Длительности картинок: min_image_duration..max_image_duration (по умолчанию 3..5 сек)

v4.4.7:
- Картинка→картинка: fade минимум 1с (0.5с было почти моментально)
- fade_duration_images для слайдшоу и финальной склейки

v4.4.6:
- Xfade батчинг при 20+ сегментах — избегает "Cannot allocate memory" (140+ входов в один FFmpeg)

v4.4.5:
- Xfade: параллельное получение длительностей (10+ сегментов), filter_complex_script при 30+
- Videotoolbox: 2 потока в едином пуле (как NVENC)

v4.4.4:
- Fade (xfade) между ВСЕМИ сегментами: видео↔видео, видео↔картинка, картинка↔картинка
- _concat_parts_with_xfade: финальная склейка с переходами вместо concat при enable_fade

v4.4.3:
- Единый пул: видео-батчи и батчи картинок рендерятся параллельно

v4.4.2:
- setpts=PTS-STARTPTS перед concat — устраняет повторения кадров на стыках сегментов
- fps:round=near — равномерное распределение кадров, плавное воспроизведение (zero давал лаги)
- -vsync cfr при финальной склейке — постоянный frame rate
- scale:flags=bilinear — лучшее качество масштабирования
- maxrate 8 Mbps по умолчанию (было 6)

v4.4.1:
- Финальная склейка: перекодирование вместо stream copy — устраняет дёрганье на стыках
- NVENC: maxrate 8 Mbps по умолчанию — 5 мин ~300 MB (max_bitrate_mbps в render_settings)

v4.4.0:
- Нормализация FPS (target_fps=30) во всех батчах — устраняет dup/drop кадров при concat
- Дедупликация входов: один файл → один -i (важно при stock_video_cycle)
- Stream copy для финальной склейки (мгновенно вместо перекодирования)
- batch_size=10 по умолчанию (снижение нагрузки на CPU)
- -hwaccel auto при GPU, таймаут 10 мин на батч
- _apply_random_metadata: только метаданные (stream copy), без перекодирования

v4.3.0:
- Лимит стоковых видео: берём N роликов, рандомный порядок, цикл при исчерпании
- validate_fragment: проверка валидности видео после -c copy
- Fallback re-encode при битых фрагментах (вместо зависания FFmpeg)

v4.2.0:
- Исправлена проблема с длительностью видео на YouTube
- Добавлен -movflags +faststart для совместимости

v4.1.0:
- Батчинг для больших проектов (300+ картинок)
- Разбивка на группы по 20 картинок для экономии RAM

v4.0.0:
- Fade переходы между картинками
- Ken Burns эффект
"""

__VERSION__ = "4.4.11"

import os
import glob
import math
import shutil
import subprocess
import sys
import tempfile
import argparse
import json
import re
import random
import platform
import time
import uuid
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

# Скрытие окна консоли при запуске FFmpeg из exe (Windows)
_SUBPROCESS_HIDE_WINDOW = {}
if sys.platform == 'win32':
    _SUBPROCESS_HIDE_WINDOW = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}

# Исправление кодировки для Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        import io
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Импортируем модуль для определения путей portable Python
try:
    import portable_paths  # type: ignore[reportMissingImports]
    FFMPEG_EXE = portable_paths.get_ffmpeg_executable()
except ImportError:
    # Если модуль не найден - используем системный ffmpeg
    FFMPEG_EXE = "ffmpeg"

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False


# Потолок параллельных ffmpeg на ПАЧКАХ КАРТИНОК (не на Ken Burns — у того свой, см.
# _process_timeline). Замер 03.08.2026, 40 фото 1080p, 20 ядер: пул=1 → 11.20с, 4 → 11.67с,
# 8 → +9.6%, 16 → +10.5%. Пачка уже насыщает машину одним ffmpeg, поэтому число НЕ выводим
# из RAM/CPU — иначе на жирном боксе пул раздувается и рендер картинок становится медленнее.
_IMAGE_POOL_CAP = 4


def _real_cpu_count():
    """Реальное число ДОСТУПНЫХ ядер. В контейнере (RunPod и пр.) os.cpu_count() возвращает ядра
    ХОСТА (напр. 256), а лимит задан cgroup CPU-квотой → читаем её, иначе auto_tune ставит
    threads=256 и плодит оверсабскрайбинг. Фолбэк: affinity → os.cpu_count(). На Маке/Win cgroup
    нет → корректный фолбэк."""
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()
            if quota != "max":
                n = int(float(quota) / float(period))
                if n >= 1:
                    return n
    except Exception:
        pass
    # cgroup v1
    try:
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read().strip())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read().strip())
        if q > 0 and p > 0:
            n = int(q / p)
            if n >= 1:
                return n
    except Exception:
        pass
    # affinity (учитывает taskset/cpuset)
    try:
        n = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
        if n >= 1:
            return n
    except Exception:
        pass
    return os.cpu_count() or 4


def _get_system_ram_gb():
    """Определяет объём RAM в ГБ (доступная или общая). Кроссплатформенно."""
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except ImportError:
        pass
    try:
        if sys.platform == 'win32':
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return round(m.ullTotalPhys / (1024 ** 3), 1)
        elif sys.platform == 'darwin':
            r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=2)
            if r.returncode == 0:
                return round(int(r.stdout.strip()) / (1024 ** 3), 1)
        else:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 * 1024), 1)
    except Exception:
        pass
    return 8.0  # fallback

# Импорт системы логирования
try:
    from logger_config import print_user, print_debug, print_progress, print_error_user_friendly
except ImportError:
    # Если модуль не найден, используем функции-заглушки
    def print_user(msg, msg_type="INFO"):
        emoji_map = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌", "PROCESS": "⏳"}
        emoji = emoji_map.get(msg_type.upper(), "ℹ️")
        print(f"{emoji} {msg}", flush=True)
    def print_debug(msg):
        pass
    def print_progress(current, total, item_name="элементов", show_bar=True):
        percentage = int((current / total) * 100) if total > 0 else 0
        if show_bar and total > 0:
            bar_len = 20
            filled = int(bar_len * current / total)
            bar = "█" * filled + "░" * (bar_len - filled)
            print_user(f"[{bar}] {percentage}% — {item_name}: {current}/{total}", "PROCESS")
        else:
            print_user(f"{item_name}: {current}/{total} ({percentage}%)", "PROCESS")
    def print_error_user_friendly(error_msg, show_debug_hint=True):
        print_user(error_msg, "ERROR")

IMAGE_DIR_NAME = "Images"
# OUTPUT_FILENAME больше не используется - используется имя проекта
SUPPORTED_IMAGE_FORMATS = ['.jpg', '.jpeg', '.jfif', '.png', '.webp', '.bmp', '.tiff', '.tif', '.ico', '.gif']
FORMATS_TO_CONVERT = ['.webp', '.bmp', '.tiff', '.tif', '.ico', '.gif']
SUPPORTED_VIDEO_FORMATS = ['.mp4', '.mov', '.avi', '.mkv', '.webm']

LANGUAGE_CODES = {
    'Afrikaans': 'af', 'Albanian': 'sq', 'Amharic': 'am', 'Arabic': 'ar',
    'Armenian': 'hy', 'Azerbaijani': 'az', 'Bangla': 'bn', 'Bosnian': 'bs',
    'Bulgarian': 'bg', 'Burmese': 'my', 'Catalan': 'ca', 'Chinese': 'zh',
    'Croatian': 'hr', 'Czech': 'cs', 'Danish': 'da', 'Dutch': 'nl',
    'English': 'en', 'Estonian': 'et', 'Filipino': 'fil', 'Finnish': 'fi',
    'French': 'fr', 'Galician': 'gl', 'Georgian': 'ka', 'German': 'de',
    'Greek': 'el', 'Gujarati': 'gu', 'Hebrew': 'he', 'Hindi': 'hi',
    'Hungarian': 'hu', 'Icelandic': 'is', 'Indonesian': 'id', 'Irish': 'ga',
    'Italian': 'it', 'Japanese': 'ja', 'Javanese': 'jv', 'Kannada': 'kn',
    'Kazakh': 'kk', 'Khmer': 'km', 'Korean': 'ko', 'Lao': 'lo',
    'Latvian': 'lv', 'Lithuanian': 'lt', 'Macedonian': 'mk', 'Malay': 'ms',
    'Malayalam': 'ml', 'Maltese': 'mt', 'Marathi': 'mr', 'Mongolian': 'mn',
    'Nepali': 'ne', 'Norwegian': 'nb', 'Pashto': 'ps', 'Persian': 'fa',
    'Polish': 'pl', 'Portuguese': 'pt', 'Romanian': 'ro', 'Russian': 'ru',
    'Serbian': 'sr', 'Sinhala': 'si', 'Slovak': 'sk', 'Slovenian': 'sl',
    'Somali': 'so', 'Spanish': 'es', 'Sundanese': 'su', 'Swahili': 'sw',
    'Swedish': 'sv', 'Tamil': 'ta', 'Telugu': 'te', 'Thai': 'th',
    'Turkish': 'tr', 'Ukrainian': 'uk', 'Urdu': 'ur', 'Uzbek': 'uz',
    'Vietnamese': 'vi', 'Welsh': 'cy', 'Zulu': 'zu'
}


def _escape_concat_path(path):
    """Экранирует путь для FFmpeg concat demuxer: одинарные кавычки в пути ломают парсинг."""
    normalized = (path or "").replace('\\', '/')
    return normalized.replace("'", "'\\''")


# ======================================================================
# СИНХРОН картинка↔озвучка: чистые функции (тестируются без whisper/ffmpeg)
# ======================================================================
import re as _re_sync

_SYNC_PUNCT_RE = _re_sync.compile(r"[^\w\s]", _re_sync.UNICODE)
_SYNC_WS_RE = _re_sync.compile(r"\s+", _re_sync.UNICODE)


def _scene_index_from_filename(path):
    """Извлекает 0-based индекс сцены из имени файла картинки.
    googler_worker сохраняет сцену i (0-based) в файл f"{i+1:03d}.jpg" → имя 1-based.
    '047.jpg' и '047_video.webm' → сцена 46. Сценовый префикс — ровно три
    цифры, после которых идёт ``_`` или конец stem; год вроде ``1945 Berlin``
    не считается номером сцены.
    Возвращает int (0-based) или None, если строгого префикса нет.
    """
    if not path:
        return None
    base = os.path.basename(str(path))
    stem = os.path.splitext(base)[0]
    m = _re_sync.match(r"^(\d{3})(?:_|$)", stem)
    if not m:
        return None
    num = int(m.group(1))
    # Имена 1-based (001 = первая сцена). 0 на всякий случай не уводим в -1.
    return num - 1 if num >= 1 else 0


def _partition_scene_mapped_videos(videos):
    """Делит видео на привязанные к сцене (ведущий NNN) и обычные стоки."""
    scene_mapped = []
    generic_stock = []
    for path in videos or []:
        target = scene_mapped if _scene_index_from_filename(path) is not None else generic_stock
        target.append(path)
    return scene_mapped, generic_stock


def _apply_stock_video_limit(videos, limit, sample_fn=None):
    """Лимитирует только generic stock; NNN-видео сцен всегда сохраняются.

    Порядок входного списка сохраняется. ``sample_fn`` инъецируется только для
    детерминированного unit-теста; в рабочем коде используется random.sample.
    """
    videos = list(videos or [])
    if limit <= 0:
        return videos

    scene_mapped, generic_stock = _partition_scene_mapped_videos(videos)
    if len(generic_stock) <= limit:
        return videos

    sampler = sample_fn or random.sample
    selected_generic = set(sampler(generic_stock, limit))
    scene_mapped_set = set(scene_mapped)
    return [path for path in videos if path in scene_mapped_set or path in selected_generic]


def _place_scene_mapped_videos(ordered, videos_set, images_set):
    """Собирает NNN-медиа в сценовый блок и заменяет картинку NNN её видео.

    ``material_order=video_first`` исторически группирует все видео в начале. Для
    ``NNN_parser.mp4`` это неверно: такой клип уже привязан к сцене и должен стоять
    между соседними ``NNN``-картинками. Обычные (без ведущего номера) материалы
    сохраняют своё положение относительно сценового блока.
    """
    ordered = list(ordered or [])
    videos_set = set(videos_set or [])
    images_set = set(images_set or [])

    scene_videos = {}
    scene_images = {}
    scene_paths = set()
    image_positions = []
    video_positions = []

    for pos, path in enumerate(ordered):
        scene_idx = _scene_index_from_filename(path)
        if scene_idx is None:
            continue
        if path in videos_set:
            scene_videos.setdefault(scene_idx, []).append(path)
            scene_paths.add(path)
            video_positions.append(pos)
        elif path in images_set:
            scene_images.setdefault(scene_idx, []).append(path)
            scene_paths.add(path)
            image_positions.append(pos)

    if not scene_videos:
        return ordered

    # Якоримся на блоке картинок: в video_first это оставляет generic-видео
    # впереди, но не позволяет сценовым NNN-видео уехать туда же.
    anchor = min(image_positions) if image_positions else min(video_positions)
    scene_block = []
    for scene_idx in sorted(set(scene_images) | set(scene_videos)):
        if scene_idx in scene_videos:
            # Один активный материал на сцену: видео заменяет image/placeholder.
            scene_block.append(scene_videos[scene_idx][0])
        else:
            scene_block.extend(scene_images[scene_idx])

    before = [path for pos, path in enumerate(ordered) if pos < anchor and path not in scene_paths]
    after = [path for pos, path in enumerate(ordered) if pos >= anchor and path not in scene_paths]
    return before + scene_block + after


def _normalize_text_for_align(s):
    """Нормализация для сравнения: lower, без пунктуации, схлопнутые пробелы."""
    if not s:
        return ""
    s = str(s).lower()
    s = _SYNC_PUNCT_RE.sub(" ", s)
    s = _SYNC_WS_RE.sub(" ", s)
    return s.strip()


def _align_durations(words, scene_texts, audio_duration=0.0):
    """Выравнивает длительности по сценам из whisper word-timestamps.

    words: [{'start','end','word'}, ...] в порядке речи.
    scene_texts: список текстов сцен (порядок = порядок сцен).
    Возвращает d[] длиной len(scene_texts); d[i] = max(0.5, end-start) сцены i.

    Алгоритм: идём по словам по порядку, накапливаем в текущую сцену; сцена закрыта,
    когда накопленный текст ПОКРЫЛ текст сцены (накопл. число слов >= числу слов в сцене
    ИЛИ накопл. символов >= длине нормализованного текста сцены). start сцены = start
    первого её слова, end = end последнего.

    РОБАСТНОСТЬ: слова кончились раньше / матчинг поплыл → оставшимся сценам раздаём
    остаток аудио ПРОПОРЦИОНАЛЬНО длине их текста (fallback). Никогда не падаем.
    """
    n = len(scene_texts)
    if n == 0:
        return []
    # Целевые метрики по каждой сцене (норм. текст)
    norm_scene = [_normalize_text_for_align(t) for t in scene_texts]
    scene_word_cnt = [max(1, len(ns.split())) for ns in norm_scene]
    scene_char_cnt = [max(1, len(ns.replace(" ", ""))) for ns in norm_scene]

    # Очищаем слова: только непустые с валидными таймкодами
    clean = []
    for w in (words or []):
        try:
            txt = _normalize_text_for_align(w.get('word', ''))
            if not txt:
                continue
            st = float(w.get('start', 0.0))
            en = float(w.get('end', st))
            if en < st:
                en = st
            clean.append((st, en, txt))
        except Exception:
            continue

    durations = [None] * n
    try:
        total_audio = float(audio_duration) if audio_duration else 0.0
    except Exception:
        total_audio = 0.0
    if total_audio <= 0 and clean:
        total_audio = clean[-1][1]

    wi = 0
    nwords = len(clean)
    last_end = 0.0
    for si in range(n):
        if wi >= nwords:
            break  # слова кончились — оставшиеся сцены уйдут в fallback ниже
        acc_words = 0
        acc_chars = 0
        seg_start = clean[wi][0]
        seg_end = clean[wi][1]
        need_w = scene_word_cnt[si]
        need_c = scene_char_cnt[si]
        # Последняя сцена забирает все оставшиеся слова (хвост не теряем)
        is_last = (si == n - 1)
        while wi < nwords:
            st, en, txt = clean[wi]
            acc_words += 1
            acc_chars += len(txt.replace(" ", ""))
            seg_end = en
            wi += 1
            if not is_last and (acc_words >= need_w or acc_chars >= need_c):
                break
        durations[si] = max(0.5, seg_end - seg_start)
        last_end = seg_end

    # FALLBACK для незаполненных сцен: остаток аудио пропорционально длине текста
    missing = [i for i in range(n) if durations[i] is None]
    if missing:
        remaining = max(0.0, total_audio - last_end)
        weight_sum = sum(scene_char_cnt[i] for i in missing)
        if remaining <= 0 or weight_sum <= 0:
            # Нет остатка/веса — раздаём минимум, чтобы не падать
            for i in missing:
                durations[i] = 0.5
        else:
            for i in missing:
                share = remaining * (scene_char_cnt[i] / weight_sum)
                durations[i] = max(0.5, share)
    return durations


def _align_durations_v2(words, scene_texts, audio_duration=0.0):
    """ТЕКСТ-ЯКОРНЫЙ алайн (v2): выравнивает ИЗВЕСТНЫЙ текст сцен на слова whisper через
    sequence-matching (difflib), а НЕ подсчётом слов → граница каждой сцены ставится по её
    РЕАЛЬНЫМ словам, дрейф не копится. Несматченные сцены — линейная интерполяция между якорями.
    Реюзает тот же whisper-проход (0 доп. времени рендера; difflib по ~сотням слов = миллисекунды).
    Возвращает d[] длиной len(scene_texts)."""
    import difflib
    n = len(scene_texts)
    if n == 0:
        return []
    clean = []
    for w in (words or []):
        try:
            t = _normalize_text_for_align(w.get('word', ''))
            if not t:
                continue
            st = float(w.get('start', 0.0)); en = float(w.get('end', st))
            if en < st:
                en = st
            clean.append((st, en, t))
        except Exception:
            continue
    try:
        total = float(audio_duration) if audio_duration else 0.0
    except Exception:
        total = 0.0
    if total <= 0 and clean:
        total = clean[-1][1]
    # токены текста сцен с тегом сцены
    script = []
    for si, txt in enumerate(scene_texts):
        for tok in _normalize_text_for_align(txt).split():
            script.append((si, tok))
    if not clean or not script:
        # фолбэк: пропорционально длине текста сцены
        cc = [max(1, len(_normalize_text_for_align(t).replace(' ', ''))) for t in scene_texts]
        tot = sum(cc) or 1
        T = total if total > 0 else float(n)
        return [max(0.5, T * c / tot) for c in cc]
    wtok = [c[2] for c in clean]
    stok = [s[1] for s in script]
    sm = difflib.SequenceMatcher(None, wtok, stok, autojunk=False)
    w_scene = [None] * len(clean)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            w_scene[a + k] = script[b + k][0]
    # carry-forward несматченных whisper-слов на последнюю известную сцену
    last = 0
    for i in range(len(w_scene)):
        if w_scene[i] is None:
            w_scene[i] = last
        else:
            last = w_scene[i]
    # старт сцены = время её ПЕРВОГО whisper-слова
    sfirst = [None] * n
    for i, (st, en, _t) in enumerate(clean):
        si = w_scene[i]
        if 0 <= si < n and sfirst[si] is None:
            sfirst[si] = st
    if sfirst[0] is None:
        sfirst[0] = clean[0][0]
    # интерполяция None между якорями
    known = [i for i in range(n) if sfirst[i] is not None]
    for idx in range(len(known) - 1):
        a, b = known[idx], known[idx + 1]
        sa, sb = sfirst[a], sfirst[b]
        for j in range(1, b - a):
            sfirst[a + j] = sa + (sb - sa) * j / (b - a)
    lastk = known[-1]
    if lastk < n - 1:
        sa = sfirst[lastk]; remain = max(0.0, total - sa)
        for j in range(1, n - lastk):
            sfirst[lastk + j] = sa + remain * j / (n - lastk)
    # монотонность стартов
    for i in range(1, n):
        if sfirst[i] is None or sfirst[i] < sfirst[i - 1]:
            sfirst[i] = sfirst[i - 1]
    # длительность сцены = старт следующей − старт текущей
    durations = []
    for i in range(n):
        nxt = sfirst[i + 1] if i + 1 < n else total
        durations.append(max(0.5, nxt - sfirst[i]))
    return durations


def _collapse_missing_images(scene_present, durations):
    """Схлопывает длительности сцен без картинок в ПРЕДЫДУЩУЮ существующую картинку.

    scene_present[i]: True если у сцены i есть файл картинки.
    durations[i]: длительность сцены i (для ВСЕХ сцен).
    Возвращает dict {scene_idx_с_картинкой: суммарная_длительность}.

    Правила:
    - сцена с картинкой → её длительность = своя d[i].
    - сцена без картинки → её d[i] прибавляется к длительности предыдущей существующей.
    - ведущие сцены без картинок (до первой существующей) → их d копится и прибавляется
      к ПЕРВОЙ существующей картинке.
    Сумма всех значений = сумме durations (точный синхрон по времени).
    """
    n = len(durations)
    result = {}
    leading = 0.0          # накопленные d ведущих сцен без картинки
    last_present = None     # индекс последней существующей картинки
    for i in range(n):
        d = max(0.5, float(durations[i])) if durations[i] is not None else 0.5
        if scene_present[i]:
            result[i] = d + leading
            leading = 0.0
            last_present = i
        else:
            if last_present is None:
                leading += d        # ещё не было картинок — копим для первой
            else:
                result[last_present] += d
    return result


def sanitize_filename(name, max_length=150):
    """
    Очищает имя файла от недопустимых символов и ограничивает длину.
    
    Args:
        name: Исходное имя
        max_length: Максимальная длина имени (по умолчанию 150 символов)
    
    Returns:
        Очищенное имя файла
    """
    # Недопустимые символы в Windows: < > : " / \ | ? *
    invalid_chars = '<>:"/\\|?*'
    
    # Заменяем недопустимые символы на подчеркивание
    for char in invalid_chars:
        name = name.replace(char, '_')
    
    # Удаляем ведущие и завершающие пробелы и точки
    name = name.strip(' .')
    
    # Ограничиваем длину (оставляем место для расширения .mp4)
    if len(name) > max_length:
        name = name[:max_length]
    
    # Если имя пустое после очистки, используем дефолтное
    if not name:
        name = "video"
    
    return name


# F. Рендер-фильтр (17.07): единый grade поверх СОБРАННОГО видеоряда (трюк SLEEP —
# разнородные кадры выравниваются одним пресетом и смотрятся однородной хроникой).
# Дефолт ВЫКЛ (config['color_grade'] отсутствует/пуст) — старый путь рендера не
# трогается ни одной командой. Применяется одним финальным проходом по video_only,
# а НЕ по-сегментно: гарантирует одинаковый вид у картинок, клипов и интро.
COLOR_GRADE_PRESETS = {
    # «Архивная» хроника: мягкие выцветшие кривые + тёплые света/холодные тени + зерно.
    # Зерно слабее vintage_effect (45): grade — выравнивание, а не стилизация под руину.
    "archive_grade": (
        "curves=preset=vintage,"
        "colorbalance=rs=0.04:gs=0.01:bs=-0.05:rm=0.02:bm=-0.04,"
        "noise=alls=7:allf=t+u"
    ),
}


def get_color_grade_filter(preset):
    """Возвращает строку ffmpeg-фильтра для пресета grade или None (= выключено).

    Пустое/None имя — выключено. Неизвестное имя — тоже None (вызывающий логирует
    предупреждение, рендер продолжается без grade: recovery > avoidance).
    """
    if not preset or not str(preset).strip():
        return None
    return COLOR_GRADE_PRESETS.get(str(preset).strip().lower())


class VideoRenderer:
    def __init__(self, project_dir, config):
        self.project_dir = project_dir
        # Кэшируем путь к FFmpeg при инициализации
        self._cached_ffmpeg_path = None
        self.config = config
        self.quality = config.get('quality', '720p')
        self.tts_engine = config.get('tts_engine', 'edge')
        self.add_subtitles = config.get('add_subtitles', False)
        self.sync_audio = config.get('sync_audio', True)
        self.pause_duration = config.get('pause_duration', 0.0)
        self.pause_interval = config.get('pause_interval', 0)
        self.min_image_duration = float(config.get('min_image_duration', 3.0))  # Мин время на картинку (сек)
        self.max_image_duration = float(config.get('max_image_duration', 5.0))  # Макс время на картинку (сек)
        self.distribute_evenly = config.get('distribute_evenly', True)
        self.source_text_path = config.get('source_text_path', None)
        self.subtitle_language = config.get('subtitle_language', 'Russian')
        self.vintage_effect = config.get('vintage_effect', False)
        # F. Единый grade поверх собранного видеоряда: имя пресета из COLOR_GRADE_PRESETS
        # (например 'archive_grade'). Дефолт None = ВЫКЛ, поведение рендера прежнее.
        self.color_grade = config.get('color_grade', None)
        self.background_audio_file = config.get('background_audio_file', None)
        self.main_audio_volume = config.get('main_audio_volume', 1.0)
        self.background_audio_volume = config.get('background_audio_volume', 0.2)
        # 🔉 «Звук из видео»: подмешивать аудио-дорожки ведущих видео-клипов в финальный микс
        self.use_video_audio = bool(config.get('use_video_audio', False))
        try:
            self.video_audio_volume = float(config.get('video_audio_volume', 1.0) or 1.0)
        except (TypeError, ValueError):
            self.video_audio_volume = 1.0
        
        # Интро-видео (список, всегда первыми в ролике)
        intro_files = config.get('intro_video_files', None)
        if intro_files is None and config.get('intro_video_file'):
            intro_files = [config.get('intro_video_file')]
        self.intro_video_files = [p for p in (intro_files or []) if p and isinstance(p, str)]
        # НОВОЕ: Трек в конце ролика
        self.end_track_file = config.get('end_track_file', None)
        self.end_track_volume = config.get('end_track_volume', 1.0)
        
        # НОВОЕ: Переходы
        self.enable_fade = config.get('enable_fade', True)
        self.fade_duration = config.get('fade_duration', 0.5)
        # Для картинка→картинка: макс 0.7с (1с съедал всё время), адаптивно уменьшается при нехватке
        self.fade_duration_images = min(max(self.fade_duration, 0.5), 0.7)
        self.min_visible_image_time = float(config.get('min_visible_image_time', 1.2))  # Мин. сек картинки без fade
        self.enable_kenburns = config.get('enable_kenburns', False)
        self.kenburns_zoom = config.get('kenburns_zoom', 1.1)
        # Порядок материалов: video_first, images_first, random, alternate, alphabetical
        self.material_order = config.get('material_order', 'video_first')
        
        # Рандомизация метаданных для уникальности каждого ролика
        self.randomize_metadata = config.get('randomize_metadata', False)
        
        # Масштаб и смещение картинок/видео: глобально и по элементам
        self.media_scale_pct = float(config.get('media_scale_pct', 100))
        self.media_offset_x = int(config.get('media_offset_x', 0))
        self.media_offset_y = int(config.get('media_offset_y', 0))
        self.media_per_item = config.get('media_per_item', {})  # normpath -> {scale_pct, offset_x, offset_y}
        
        # Папка дополнительных материалов
        self.extra_materials_dir = config.get('extra_materials_dir', None)
        
        # Лимит стоковых видео: 0 = без лимита, >0 = взять N роликов, рандом, цикл при исчерпании
        self.stock_video_limit = int(config.get('stock_video_limit', 0))
        self.stock_video_cycle = config.get('stock_video_cycle', True)  # Циклить когда стоки закончились
        
        # Оверлеи (наложения поверх видео)
        self.overlays = config.get('overlays', [])

        # Относительные пути медиа → от project_dir (кабинет шлёт относительные: на рендер-ноде
        # абсолютные пути сервера не существуют; ассеты приезжают в tar внутрь project_dir, 2026-07-10)
        def _abs_media(p):
            if not p or not isinstance(p, str) or os.path.isabs(p):
                return p
            return os.path.join(self.project_dir, p)
        self.background_audio_file = _abs_media(self.background_audio_file)
        self.end_track_file = _abs_media(self.end_track_file)
        self.intro_video_files = [_abs_media(p) for p in self.intro_video_files]
        self.extra_materials_dir = _abs_media(self.extra_materials_dir)   # кабинет шлёт "_RENDER_MEDIA/stock"
        for _ov in self.overlays:
            if isinstance(_ov, dict) and _ov.get('path'):
                _ov['path'] = _abs_media(_ov['path'])

        # Настройки рендера для оптимизации
        # render_settings может содержать:
        #   - force_cpu: bool - принудительно использовать CPU (по умолчанию False)
        #   - force_encoder: str - принудительно указать кодек ('libx264', 'h264_nvenc', 'h264_videotoolbox', 'h264_vaapi')
        #   - crf: int - качество видео (0-51, меньше = лучше качество, по умолчанию 23)
        #   - threads: int - количество потоков для CPU кодировщика (0 = авто, по умолчанию 0)
        #   - cpu_preset: str - пресет для CPU ('ultrafast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'veryslow', по умолчанию 'fast')
        #   - nvenc_preset: str - пресет для NVENC ('p1'-'p7', где p4 = default, по умолчанию 'p4')
        #   - videotoolbox_preset: str - пресет для VideoToolbox ('fast', 'medium', 'slow', по умолчанию 'fast')
        #   - vaapi_preset: str - пресет для VAAPI ('fast', 'medium', 'slow', по умолчанию 'fast')
        #   - vaapi_device: str - путь к VAAPI устройству (например '/dev/dri/renderD128', по умолчанию None)
        #   - timeout_seconds: int - таймаут для FFmpeg команд в секундах (по умолчанию 14400 = 4 часа)
        #   - process_priority: str - приоритет процесса ('low', 'normal', 'high', по умолчанию 'normal')
        #   - batch_size_small: int - размер батча для малых проектов (по умолчанию 20)
        #   - batch_size_medium: int - размер батча для средних проектов (по умолчанию 18)
        #   - batch_size_large: int - размер батча для больших проектов (по умолчанию 15)
        #   - max_bitrate_mbps: int - макс. битрейт для NVENC (по умолчанию 8, ~300MB на 5 мин)
        #   - auto_tune: bool - автонастройка по железу (по умолчанию True)
        #   - max_pool_workers: int - макс. параллельных FFmpeg (0 = авто по CPU)
        #   - video_batch_size: int - размер батча видео для CPU (0 = авто)
        #   - image_convert_workers: int - потоков для конвертации картинок (0 = авто)
        self.render_settings = config.get('render_settings', {})
        
        # Целевой FPS для нормализации (30 — универсальный стандарт, минимизирует dup/drop кадров)
        self.target_fps = int(config.get('target_fps', 30))
        
        self.gpu_available = self._check_gpu()
        self._pc_specs = None  # кэш: {cpu_cores, ram_gb, gpu}
        self._apply_auto_tune()
        self.whisper_model = None
        self.converted_images = []
        # Кэш для длительностей файлов (избегаем повторных вызовов ffprobe)
        self._duration_cache = {}
        
    def log(self, message):
        """Legacy метод логирования (используйте print_user/print_debug)"""
        # Определяем тип сообщения по префиксу
        if message.startswith("✅") or "успешно" in message.lower() or "готово" in message.lower() or "__RENDER_SUCCESS__" in message:
            print_user(message.replace("✅", "").replace("__RENDER_SUCCESS__|", "").strip(), "SUCCESS")
        elif message.startswith("❌") or "ошибка" in message.lower() or "error" in message.lower() or "__RENDER_ERROR__" in message:
            print_user(message.replace("❌", "").replace("__RENDER_ERROR__|", "").strip(), "ERROR")
        elif message.startswith("⚠️") or "предупреждение" in message.lower() or "warning" in message.lower():
            print_user(message.replace("⚠️", "").strip(), "WARNING")
        elif message.startswith("⏳") or "обработка" in message.lower() or "processing" in message.lower():
            print_user(message.replace("⏳", "").strip(), "PROCESS")
        else:
            # Технические сообщения идут в debug
            print_debug(message)
        
    def _check_gpu(self):
        """Проверяет доступность GPU ускорения для разных платформ"""
        try:
            # Проверяем, не принудительно ли отключен GPU через настройки
            if self.render_settings.get('force_cpu', False):
                return False
            
            # Находим FFmpeg для проверки encoders
            ffmpeg_path = self._get_ffmpeg_path()
            
            try:
                result = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"], 
                                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
                                       **_SUBPROCESS_HIDE_WINDOW)
                if result.returncode != 0:
                    return False
                encoder_list = result.stdout
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                return False
            
            # Windows/Linux: NVIDIA GPU
            if 'h264_nvenc' in encoder_list:
                # Проверяем наличие CUDA через FFmpeg (более надежно чем через torch)
                try:
                    # Проверяем, что NVENC доступен (не требует torch)
                    # Если кодек есть в списке - значит GPU доступен
                    return True
                except:
                    pass
            
            # Mac: VideoToolbox (Apple Silicon или Intel с Quick Sync)
            if 'h264_videotoolbox' in encoder_list:
                return True
            
            # Linux: VAAPI (Intel/AMD)
            if 'h264_vaapi' in encoder_list:
                return True
            
            return False
        except Exception as e:
            print_debug(f"Ошибка при проверке GPU: {e}")
            return False
    
    def _detect_pc_specs(self):
        """Определяет характеристики ПК: CPU cores, RAM (ГБ), GPU."""
        if self._pc_specs is not None:
            return self._pc_specs
        cpu_cores = _real_cpu_count()
        ram_gb = _get_system_ram_gb()
        self._pc_specs = {
            "cpu_cores": cpu_cores,
            "ram_gb": ram_gb,
            "gpu": self.gpu_available,
        }
        return self._pc_specs
    
    def _apply_auto_tune(self):
        """
        Настраивает render_settings по железу для максимальной скорости.
        Применяет только ключи, не заданные явно в config.
        """
        if not self.render_settings.get('auto_tune', True):
            return
        specs = self._detect_pc_specs()
        cpu = specs["cpu_cores"]
        ram = specs["ram_gb"]
        gpu = specs["gpu"]
        
        # Ключи, которые можно переопределить вручную (не трогаем если уже заданы)
        def _set_if_missing(key, value):
            if key not in self.render_settings or self.render_settings[key] is None:
                self.render_settings[key] = value
        
        # threads на один ffmpeg ставим НИЖЕ, после расчёта max_pool_workers (балансируем по воркерам)
        # Батчи картинок: больше при много RAM
        if ram >= 32:
            _set_if_missing('batch_size_small', 28)
            _set_if_missing('batch_size_medium', 24)
            _set_if_missing('batch_size_large', 20)
        elif ram >= 16:
            _set_if_missing('batch_size_small', 24)
            _set_if_missing('batch_size_medium', 20)
            _set_if_missing('batch_size_large', 16)
        else:
            _set_if_missing('batch_size_small', 20)
            _set_if_missing('batch_size_medium', 18)
            _set_if_missing('batch_size_large', 15)
        
        # max_pool_workers: параллельные FFmpeg. КЛЮЧЕВОЕ: считать по РЕАЛЬНОМУ энкодеру, а не по
        # наличию GPU. На RunPod GPU есть, но NVENC мёртв (драйвер без video-cap) → force_encoder=
        # libx264 (CPU). Раньше gpu=True давал 6 воркеров → на 27-ядерном боксе крутилось 6 ffmpeg
        # (zoompan/scale фильтры single-thread ≈ 6 ядер занято), 21 ядро простаивало → рендер ×3-4 дольше.
        _enc = str(self.render_settings.get('force_encoder', '')).lower()
        _is_hw = ('nvenc' in _enc) or ('videotoolbox' in _enc) or (not _enc and gpu)
        if _is_hw:
            _set_if_missing('max_pool_workers', 6)  # HW-сессии (NVENC/videotoolbox)
        else:
            # софт-энкод (libx264): фильтры single-thread → больше воркеров = больше ядер занято
            _set_if_missing('max_pool_workers', min(max(4, cpu - 2), 24))
        # Memory-aware потолок (ЛЮБАЯ платформа, только понижает): N параллельных рендеров
        # с xfade жрут ~2-3GB RSS каждый. На 24GB unified / low-RAM Windows 6 воркеров свопят,
        # а своп хуже serial. Измерено на M5 Pro 24GB: N=3 чисто (swap=0, 367 vid/h), N≥6 — своп.
        per_render_gb = 3
        safe_workers = max(1, int((ram * 0.4) / per_render_gb))  # 24GB → 3, 8GB → 1
        self.render_settings['max_pool_workers'] = min(
            int(self.render_settings.get('max_pool_workers', safe_workers)), safe_workers)

        # threads на ОДИН ffmpeg = cpu/воркеры → threads×workers ≈ cpu (без оверсабскрайбинга).
        # Раньше str(cpu) ставился на КАЖДЫЙ ffmpeg → N_workers × cpu потоков = трэшинг
        # (в контейнере с врущим cpu=256 — катастрофа). Топ-фикс ревью пайплайна.
        _wk = max(1, int(self.render_settings.get('max_pool_workers', 1)))
        _set_if_missing('threads', str(max(2, min(16, cpu // _wk))) if cpu else '0')

        # video_batch_size для CPU (GPU = 2 жёстко)
        if not gpu:
            if ram >= 16 and cpu >= 8:
                _set_if_missing('video_batch_size', 15)
            elif ram >= 8 and cpu >= 4:
                _set_if_missing('video_batch_size', 12)
            else:
                _set_if_missing('video_batch_size', 10)
        
        # image_convert_workers: конвертация PIL (детект ядер теперь верный → не зажимаем 6)
        _set_if_missing('image_convert_workers', min(16, max(2, cpu // 2)))
        
        # Пресеты для скорости (если не заданы вручную)
        # veryfast+crf24 идут ПАРОЙ (замер 03.08.2026, 40 фото 1080p): faster/crf26 →
        # veryfast/crf24 = −14.4% без переходов, −13.9% с переходами, −17.8% на Ken Burns
        # при +2.1…+4.4% размера и неупавшем SSIM. crf по умолчанию — ниже, в _get_ffmpeg_args.
        _set_if_missing('cpu_preset', 'veryfast')
        _set_if_missing('nvenc_preset', 'p2')     # p2 быстрее p4, p1 — макс. скорость
        
        print_debug(f"Автонастройка: {cpu} ядер, {ram} ГБ RAM, GPU={gpu}")
    
    def _validate_fragment(self, fragment_path):
        """
        Проверяет что видеофрагмент валидный: имеет видеопоток, декодируется, длительность > 0.
        Возвращает True если файл валидный. Используется для проверки после -c copy.
        """
        if not fragment_path or not os.path.exists(fragment_path):
            return False
        if os.path.getsize(fragment_path) < 1024:
            return False
        try:
            ffprobe_exe = self._find_ffprobe()
            r = subprocess.run([
                ffprobe_exe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                str(fragment_path)
            ], capture_output=True, text=True, timeout=10,
               encoding="utf-8", errors="replace", **_SUBPROCESS_HIDE_WINDOW)
            if r.returncode != 0:
                print_debug(f"validate_fragment ffprobe error: {r.stderr[:200] if r.stderr else 'no stderr'}")
                return False
            data = json.loads(r.stdout)
            streams = data.get("streams", [])
            if not streams:
                return False
            s = streams[0]
            w, h = int(s.get("width", 0)), int(s.get("height", 0))
            if w <= 0 or h <= 0:
                return False
            ffmpeg_path = self._get_ffmpeg_path()
            r2 = subprocess.run([
                ffmpeg_path, "-v", "error",
                "-i", str(fragment_path),
                "-vframes", "2",
                "-f", "null", "-"
            ], capture_output=True, text=True, timeout=15,
               encoding="utf-8", errors="replace", **_SUBPROCESS_HIDE_WINDOW)
            if r2.returncode != 0:
                print_debug(f"validate_fragment decode failed: {r2.stderr[:200] if r2.stderr else ''}")
                return False
            return True
        except subprocess.TimeoutExpired:
            print_debug(f"validate_fragment timeout: {fragment_path}")
            return False
        except Exception as e:
            print_debug(f"validate_fragment error: {e}")
            return False
    
    def _safe_loop_trim(self, input_path, duration_sec, output_path, ffmpeg_args=None):
        """
        Зацикливает видео и обрезает до duration_sec. Пробует -c copy, при битом выводе — re-encode.
        """
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        ffmpeg_path = self._get_ffmpeg_path()
        # 1. Пробуем -c copy (быстро)
        try:
            r = subprocess.run([
                ffmpeg_path, "-y",
                "-stream_loop", "-1", "-i", input_path,
                "-t", str(duration_sec),
                "-c", "copy", "-y", output_path
            ], capture_output=True, text=True, timeout=self.render_settings.get('timeout_seconds', 14400),
               encoding="utf-8", errors="replace", **_SUBPROCESS_HIDE_WINDOW)
            if r.returncode == 0 and os.path.exists(output_path) and self._validate_fragment(output_path):
                return True
        except Exception as e:
            print_debug(f"_safe_loop_trim copy failed: {e}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        # 2. Fallback: re-encode (гарантированно валидный)
        self.log("   ⚠️ Copy-cut дал битый фрагмент, перекодирую...")
        try:
            enc_params = self._get_encoder_params(ffmpeg_args or self._get_ffmpeg_args())
            cmd = [ffmpeg_path, "-y", "-stream_loop", "-1", "-i", input_path, "-t", str(duration_sec)]
            cmd.extend(["-c:v", enc_params.get("encoder", "libx264")])
            if "preset" in enc_params:
                cmd.extend(["-preset", enc_params["preset"]])
            cmd.extend(["-crf", str(enc_params.get("crf", 18)), "-pix_fmt", "yuv420p", "-an", "-y", output_path])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.render_settings.get('timeout_seconds', 14400),
                              encoding="utf-8", errors="replace", **_SUBPROCESS_HIDE_WINDOW)
            if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                return True
        except Exception as e:
            self.log(f"   ❌ Re-encode fallback failed: {e}")
        return False
    
    def convert_image_to_png(self, image_path, output_dir):
        if not PIL_AVAILABLE:
            return image_path
        file_ext = os.path.splitext(image_path)[1].lower()
        if file_ext not in FORMATS_TO_CONVERT:
            return image_path
        try:
            base_name = os.path.splitext(os.path.basename(image_path))[0]
            output_path = os.path.join(output_dir, f"{base_name}_converted.png")
            with Image.open(image_path) as img:
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    mask = img.split()[-1] if img.mode in ('RGBA', 'LA') else None
                    background.paste(img, mask=mask)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(output_path, 'PNG')
            self.converted_images.append(output_path)
            return output_path
        except:
            return image_path
    
    def cleanup_converted_images(self):
        for img_path in self.converted_images:
            try:
                if os.path.exists(img_path):
                    os.remove(img_path)
            except:
                pass
        self.converted_images.clear()
    
    def _project_is_image_dominated(self):
        """True если в проекте картинок значительно больше видео (по расширениям, без ffprobe).
        Для таких таймлайнов libx264 veryfast быстрее HW-энкодера на статике."""
        IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
        VID_EXT = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
        img_n = vid_n = 0
        roots = [self.project_dir]
        if self.extra_materials_dir:
            roots.append(self.extra_materials_dir)
        for root_dir in roots:
            if not root_dir or not os.path.isdir(root_dir):
                continue
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [d for d in dirs if not d.startswith('.') and 'temp' not in d.lower() and d != '_ASSETS']
                for fn in files:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in IMG_EXT:
                        img_n += 1
                    elif ext in VID_EXT:
                        vid_n += 1
        total = img_n + vid_n
        if total == 0:
            return False
        return (img_n / total) >= 0.7  # ≥70% материалов — картинки

    def _get_ffmpeg_args(self):
        """Определяет оптимальные параметры FFmpeg в зависимости от платформы и GPU"""
        import platform
        
        quality_settings = {
            '144p': {'size': '256x144', 'crf': 28},
            '240p': {'size': '426x240', 'crf': 26},
            '360p': {'size': '640x360', 'crf': 25},
            '480p': {'size': '854x480', 'crf': 24},
            '720p': {'size': '1280x720', 'crf': 23},
            '1080p': {'size': '1920x1080', 'crf': 23}
        }
        settings = quality_settings.get(self.quality, quality_settings['720p'])
        
        # Используем CRF из настроек рендера, если указан
        crf = self.render_settings.get('crf', settings['crf'])
        threads = str(self.render_settings.get('threads', 0))
        
        # Проверяем, не принудительно ли указан кодек через настройки
        forced_encoder = self.render_settings.get('force_encoder', None)
        if forced_encoder:
            # Используем принудительно указанный кодек
            preset = self.render_settings.get('cpu_preset', 'fast')
            if forced_encoder == 'h264_nvenc':
                preset = self.render_settings.get('nvenc_preset', 'p4')
            elif forced_encoder == 'h264_videotoolbox':
                preset = self.render_settings.get('videotoolbox_preset', 'fast')
            elif forced_encoder == 'h264_vaapi':
                preset = self.render_settings.get('vaapi_preset', 'fast')
            
            return {
                'size': settings['size'],
                'encoder': forced_encoder,
                'preset': preset,
                'crf': crf,
                'threads': threads
            }

        # Умный авто-выбор: для image-доминированных таймлайнов libx264 veryfast БЫСТРЕЕ
        # HW-энкодера на статике (HW упёрт ~470fps; x264 на near-identical кадрах ~2600fps)
        # + файл меньше. Для video-тяжёлых оставляем HW (лучше для motion).
        # Отключается render_settings['disable_smart_encoder']=True.
        if not self.render_settings.get('disable_smart_encoder', False) and self._project_is_image_dominated():
            print_debug("Smart encoder: image-доминированный таймлайн → libx264 veryfast")
            return {
                'size': settings['size'],
                'encoder': 'libx264',
                'preset': self.render_settings.get('cpu_preset', 'veryfast'),
                # crf 26 → 24: обязательная половина пары с veryfast (см. _apply_auto_tune).
                # Без неё смена пресета — просто потеря качества, а не ускорение.
                'crf': self.render_settings.get('crf', 24),
                'threads': threads,
            }

        if self.gpu_available:
            # Определяем какой GPU энкодер использовать
            try:
                ffmpeg_path = self._get_ffmpeg_path()
                
                result = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"], 
                                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
                                       **_SUBPROCESS_HIDE_WINDOW)
                if result.returncode != 0:
                    raise Exception("FFmpeg не доступен")
                encoder_list = result.stdout
                
                # Mac: VideoToolbox (самый быстрый для Mac)
                if 'h264_videotoolbox' in encoder_list:
                    preset = self.render_settings.get('videotoolbox_preset', 'fast')
                    return {
                        'size': settings['size'], 
                        'encoder': 'h264_videotoolbox', 
                        'preset': preset, 
                        'crf': crf, 
                        'threads': threads
                    }
                
                # Windows/Linux: NVIDIA
                if 'h264_nvenc' in encoder_list:
                    preset = self.render_settings.get('nvenc_preset', 'p4')
                    return {
                        'size': settings['size'], 
                        'encoder': 'h264_nvenc', 
                        'preset': preset, 
                        'crf': crf, 
                        'threads': threads
                    }
                
                # Linux: VAAPI
                if 'h264_vaapi' in encoder_list:
                    preset = self.render_settings.get('vaapi_preset', 'fast')
                    vaapi_device = self.render_settings.get('vaapi_device', None)
                    return {
                        'size': settings['size'], 
                        'encoder': 'h264_vaapi', 
                        'preset': preset, 
                        'crf': crf, 
                        'threads': threads,
                        'vaapi_device': vaapi_device  # Передаем устройство через ffmpeg_args
                    }
            except Exception as e:
                print_debug(f"Ошибка при определении GPU кодировщика: {e}, используем CPU")
        
        # CPU fallback - используем настройки из конфига
        preset = self.render_settings.get('cpu_preset', 'fast')
        
        return {
            'size': settings['size'], 
            'encoder': 'libx264', 
            'preset': preset, 
            'crf': crf, 
            'threads': threads
        }
    
    def _get_encoder_params(self, ffmpeg_args):
        """
        Формирует параметры кодировщика для FFmpeg команды.
        Возвращает список аргументов для -c:v и связанных параметров.
        """
        encoder = ffmpeg_args.get('encoder', 'libx264')
        preset = ffmpeg_args.get('preset', 'fast')
        crf = ffmpeg_args.get('crf', 23)
        threads = ffmpeg_args.get('threads', '0')
        
        params = ["-c:v", encoder]
        
        if encoder == 'h264_nvenc':
            # NVIDIA NVENC параметры
            nvenc_preset_map = {
                'ultrafast': 'p1', 'veryfast': 'p2', 'faster': 'p3',
                'fast': 'p4', 'medium': 'p5', 'slow': 'p6', 'veryslow': 'p7'
            }
            nvenc_preset = nvenc_preset_map.get(preset, 'p4')
            params.extend(["-preset", nvenc_preset])
            cq_value = max(0, min(51, int(crf)))
            params.extend(["-rc", "vbr"])
            params.extend(["-cq", str(cq_value)])
            params.extend(["-b:v", "0"])
            # Ограничение битрейта: без maxrate 5-мин ролик может весить 500MB+
            # 6 Mbps → ~225 MB для 5 мин. 0 = без лимита (старое поведение)
            maxrate_mbps = self.render_settings.get('max_bitrate_mbps', 8)
            if maxrate_mbps and int(maxrate_mbps) > 0:
                m = int(maxrate_mbps)
                params.extend(["-maxrate", f"{m}M", "-bufsize", f"{m*2}M"])
            params.extend(["-spatial-aq", "1"])
            params.extend(["-temporal-aq", "1"])
        elif encoder in ('h264_videotoolbox', 'hevc_videotoolbox'):
            # Apple VideoToolbox параметры (H.264 и HEVC)
            rs = self.render_settings
            # allow_sw: по умолчанию 1 (надёжность); на Mac можно 0 для гарантии HW-энкода
            allow_sw = "1" if rs.get('vt_allow_sw', True) else "0"
            params.extend(["-allow_sw", allow_sw])
            # vt_quality: constant-quality 0..100 (выше = лучше/больше). Если задан — используем -q:v,
            # иначе legacy-эвристика bitrate из CRF (обратная совместимость).
            vt_q = rs.get('vt_quality', None)
            if vt_q is not None:
                params.extend(["-q:v", str(int(vt_q))])
            else:
                base_bitrate = 5000
                if crf < 20:
                    bitrate = int(base_bitrate * 1.5)
                elif crf > 26:
                    bitrate = int(base_bitrate * 0.5)
                else:
                    bitrate = int(base_bitrate * (1.0 - (crf - 23) * 0.1))
                params.extend(["-b:v", f"{bitrate}k"])
            params.extend(["-bf", str(rs.get('vt_bframes', 0))])
            if encoder == 'hevc_videotoolbox':
                params.extend(["-tag:v", "hvc1"])
        elif encoder == 'h264_vaapi':
            # VAAPI параметры (Linux)
            # VAAPI требует указания устройства через -vaapi_device
            # Получаем устройство из ffmpeg_args (передается из _get_ffmpeg_args)
            vaapi_device = ffmpeg_args.get('vaapi_device', None)
            if vaapi_device:
                params.insert(0, "-vaapi_device")
                params.insert(1, vaapi_device)
            
            params.extend(["-qp", str(int(crf))])
            # VAAPI preset: fast, medium, slow
            vaapi_preset_map = {
                'ultrafast': 'fast', 'veryfast': 'fast', 'faster': 'fast',
                'fast': 'fast', 'medium': 'medium', 'slow': 'slow', 'veryslow': 'slow'
            }
            vaapi_preset = vaapi_preset_map.get(preset, 'fast')
            params.extend(["-quality", vaapi_preset])
        else:
            # libx264 (CPU) параметры
            params.extend(["-preset", preset])
            params.extend(["-crf", str(crf)])
            if threads != '0':
                params.extend(["-threads", threads])
        
        return params
    
    def _get_ffmpeg_path(self):
        """Получает путь к FFmpeg (кэширует результат)
        Поддерживает работу в exe (PyInstaller) через проверку _MEIPASS и bin рядом с exe
        """
        if self._cached_ffmpeg_path:
            return self._cached_ffmpeg_path
        
        # Проверка 1: прямой путь (может быть из portable_paths)
        if os.path.exists(FFMPEG_EXE):
            self._cached_ffmpeg_path = FFMPEG_EXE
            return self._cached_ffmpeg_path
        
        # Проверка 2: для PyInstaller - в _MEIPASS/bin
        try:
            if hasattr(sys, '_MEIPASS'):
                meipass_bin = os.path.join(sys._MEIPASS, 'bin', 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
                if os.path.exists(meipass_bin):
                    self._cached_ffmpeg_path = meipass_bin
                    return self._cached_ffmpeg_path
        except Exception:
            pass
        
        # Проверка 3: bin рядом с exe (для портативных сборок)
        try:
            # Определяем директорию exe или скрипта
            is_frozen = getattr(sys, 'frozen', False) or getattr(sys, '_MEIPASS', None) is not None
            if is_frozen and hasattr(sys, 'executable'):
                exe_dir = os.path.dirname(sys.executable)
            else:
                try:
                    exe_dir = os.path.dirname(os.path.abspath(__file__))
                except:
                    exe_dir = os.getcwd()
            
            if sys.platform == 'win32':
                # На Windows: рядом с exe
                exe_bin = os.path.join(exe_dir, 'bin', 'ffmpeg.exe')
                if os.path.exists(exe_bin):
                    self._cached_ffmpeg_path = exe_bin
                    return self._cached_ffmpeg_path
            else:
                # На Linux/Mac: рядом с exe или скриптом
                exe_bin = os.path.join(exe_dir, 'bin', 'ffmpeg')
                if os.path.exists(exe_bin):
                    self._cached_ffmpeg_path = exe_bin
                    return self._cached_ffmpeg_path
        except Exception:
            pass
        
        # Проверка 4: в PATH
        ffmpeg_in_path = shutil.which(FFMPEG_EXE)
        if ffmpeg_in_path:
            self._cached_ffmpeg_path = ffmpeg_in_path
            return self._cached_ffmpeg_path
        
        # Проверка 5: через imageio-ffmpeg
        try:
            import imageio_ffmpeg  # type: ignore
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
            if ffmpeg_path and os.path.exists(ffmpeg_path):
                self._cached_ffmpeg_path = ffmpeg_path
                return self._cached_ffmpeg_path
        except ImportError:
            pass
        except Exception:
            pass
        
        # Если не найден - возвращаем исходный путь (может быть в PATH)
        self._cached_ffmpeg_path = FFMPEG_EXE
        return self._cached_ffmpeg_path
    
    def run_ffmpeg_command(self, command, timeout=None, show_processing_message=True, processing_message=None):
        """Выполняет FFmpeg команду с улучшенной обработкой ошибок.
        timeout: опционально, секунды (если None — из render_settings).
        show_processing_message: если False — не печатать сообщение.
        processing_message: если задано — использовать вместо «Обработка видео... Подождите»."""
        # Заменяем первый элемент команды на найденный путь к FFmpeg, если это ffmpeg
        if command and len(command) > 0:
            if command[0] == FFMPEG_EXE or command[0] == "ffmpeg":
                ffmpeg_path = self._get_ffmpeg_path()
                if ffmpeg_path != command[0]:
                    command = [ffmpeg_path] + command[1:]
                    print_debug(f"Заменен FFmpeg в команде на: {ffmpeg_path}")
        
        try:
            # Используем переданный таймаут или из настроек рендера
            if timeout is None:
                timeout = self.render_settings.get('timeout_seconds', 14400)
            
            # Применяем приоритет процесса если указан
            import platform
            process_priority = self.render_settings.get('process_priority', 'normal')
            
            # Применяем приоритет процесса (упрощенная версия)
            # На Unix используем nice, на Windows приоритет устанавливается системой
            if platform.system() != 'Windows':
                # Unix: используем nice для изменения приоритета
                if process_priority == 'low':
                    # nice +10 (низкий приоритет)
                    command = ['nice', '-n', '10'] + command
                elif process_priority == 'high':
                    # nice -10 (высокий приоритет, требует root)
                    command = ['nice', '-n', '-10'] + command
            
            # Нормализуем все пути в команде перед запуском
            # Фильтруем None и преобразуем все в строки
            # ВАЖНО: не нормализуем аргументы, которые являются FFmpeg фильтрами/выражениями,
            # иначе os.path.normpath() уничтожит '/' в выражениях вроде (ow-iw)/2 или on/125
            normalized_command = []
            # Аргументы-ключи FFmpeg, после которых идёт НЕ-путь (фильтры, форматы и т.д.)
            ffmpeg_non_path_flags = {
                '-vf', '-af', '-filter_complex', '-filter:v', '-filter:a',
                '-c:v', '-c:a', '-c', '-codec', '-f', '-preset', '-crf', '-b:v', '-b:a',
                '-r', '-s', '-ar', '-ac', '-pix_fmt', '-map', '-movflags',
                '-rc', '-cq', '-qp', '-quality', '-maxrate', '-bufsize',
                '-spatial-aq', '-temporal-aq', '-allow_sw', '-bf',
                '-vaapi_device', '-threads', '-t', '-ss', '-to',
                '-stream_loop', '-safe', '-v',
                '-filter:a', '-n', '-hide_banner',
                '-metadata', '-metadata:s:v', '-metadata:s:a', '-map_metadata',
                '-shortest', '-an', '-profile:v', '-g', '-vsync',
            }
            skip_normalize_next = False
            prev_arg = None
            for arg in command:
                if arg is None:
                    continue  # Пропускаем None
                # Преобразуем в строку и фильтруем пустые
                arg_str = str(arg).strip()
                if not arg_str:
                    continue  # Пропускаем пустые строки
                
                if isinstance(arg, str):
                    # Пропускаем нормализацию для значений после ключей фильтров
                    # и для самих ключей FFmpeg (начинаются с -)
                    is_ffmpeg_flag = arg_str.startswith('-')
                    is_filter_value = prev_arg in ffmpeg_non_path_flags
                    # Также проверяем наличие FFmpeg выражений: =, (, ), [, ]
                    has_filter_syntax = any(ch in arg_str for ch in '=()[]')
                    
                    if is_ffmpeg_flag or is_filter_value or has_filter_syntax:
                        # Это ключ/значение FFmpeg или фильтр — НЕ нормализуем как путь
                        normalized_command.append(arg)
                    elif os.path.sep in arg or (sys.platform == 'win32' and ':' in arg and len(arg) > 2):
                        # Похоже на путь к файлу — нормализуем
                        normalized_arg = os.path.normpath(arg)
                        # На Windows проверяем длину пути
                        if sys.platform == 'win32' and len(normalized_arg) > 260:
                            try:
                                import win32api  # type: ignore
                                normalized_arg = win32api.GetShortPathName(normalized_arg)
                            except:
                                pass
                        normalized_command.append(normalized_arg)
                    else:
                        normalized_command.append(arg)
                else:
                    # Для не-строк преобразуем в строку
                    normalized_command.append(arg_str)
                prev_arg = arg_str
            
            # Логируем команду для отладки
            if show_processing_message:
                msg = processing_message if processing_message else "Обработка видео... Подождите"
                print_user(msg, "PROCESS")
            print_debug(f"Выполняю команду FFmpeg ({len(normalized_command)} аргументов):")
            # Показываем ВСЮ команду для отладки (каждый аргумент отдельно)
            for idx_arg, nc_arg in enumerate(normalized_command):
                print_debug(f"   [{idx_arg}] {nc_arg}")
            print_debug(f"   Существует FFmpeg: {os.path.exists(normalized_command[0]) if normalized_command and isinstance(normalized_command[0], str) else 'N/A'}")
            
            # Проверяем, что FFmpeg существует
            if normalized_command and isinstance(normalized_command[0], str):
                if not os.path.exists(normalized_command[0]) and not shutil.which(normalized_command[0]):
                    raise Exception(f"FFmpeg не найден: {normalized_command[0]}")
            
            # Запускаем команду через Popen + ЖИВОЙ стрим stderr (CREATE_NO_WINDOW на Windows — без окна консоли).
            # КОНТРАКТ СОХРАНЁН 1:1: те же аргументы/кодеки → тот же mp4; возврат None при успехе,
            # Exception при ошибке/таймауте, тот же текст ошибок. Прогресс — ТОЛЬКО сверху, ничего не меняет в выходе.
            import time as _time
            import threading as _threading
            import re as _re

            try:
                proc = subprocess.Popen(
                    normalized_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='replace', bufsize=1,
                    **_SUBPROCESS_HIDE_WINDOW
                )
            except FileNotFoundError as e:
                print_error_user_friendly("Ошибка при запуске FFmpeg: файл не найден")
                print_debug(f"Команда: {normalized_command}")
                print_debug(f"Первый аргумент: {normalized_command[0] if normalized_command else 'N/A'}")
                print_debug(f"Ошибка: {e}")
                import traceback
                print_debug(traceback.format_exc())
                raise Exception(f"FFmpeg не найден: {normalized_command[0] if normalized_command else 'unknown'}")
            except Exception as e:
                print_error_user_friendly(f"Ошибка при запуске FFmpeg: {e}")
                print_debug(f"Команда: {normalized_command}")
                import traceback
                print_debug(traceback.format_exc())
                raise

            # Watchdog таймаута: эквивалент subprocess.run(timeout=...). Убивает зависший ffmpeg.
            _timed_out = {'flag': False}
            def _kill_on_timeout():
                _timed_out['flag'] = True
                try:
                    proc.kill()
                except Exception:
                    pass
            _timer = _threading.Timer(timeout, _kill_on_timeout) if timeout else None
            if _timer:
                _timer.daemon = True
                _timer.start()

            # Живой прогресс: ОБЩУЮ длительность берём из самого ffmpeg ("Duration:"),
            # текущую позицию — из строк прогресса ("time="). Throttle 2с, чтобы не спамить лог.
            _total_dur = None
            _last_emit = 0.0
            _start_ts = _time.monotonic()
            _dur_re = _re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')
            _time_re = _re.compile(r'\btime=\s*(\d+):(\d+):(\d+(?:\.\d+)?)')
            _speed_re = _re.compile(r'\bspeed=\s*([\d.]+)x')

            def _fmt_t(sec):
                sec = int(max(0, sec))
                h, rem = divmod(sec, 3600)
                m, s = divmod(rem, 60)
                return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

            stderr_lines = []
            try:
                if proc.stderr is not None:
                    for raw in proc.stderr:
                        line = raw.rstrip('\r\n')
                        if not line:
                            continue
                        # Строки прогресса (frame=/size=/bitrate= + time=) НЕ копим в лог —
                        # иначе «хвост» ошибки забьётся прогрессом. Но time= из них парсим.
                        is_progress = ('time=' in line and ('frame=' in line or 'size=' in line or 'bitrate=' in line))
                        if not is_progress:
                            stderr_lines.append(line)
                            if len(stderr_lines) > 600:
                                del stderr_lines[:300]
                        if _total_dur is None:
                            md = _dur_re.search(line)
                            if md:
                                _total_dur = int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))
                        mt = _time_re.search(line)
                        if mt:
                            cur = int(mt.group(1)) * 3600 + int(mt.group(2)) * 60 + float(mt.group(3))
                            now = _time.monotonic()
                            if now - _last_emit >= 2.0:
                                _last_emit = now
                                if _total_dur and _total_dur > 0:
                                    pct = max(0.0, min(100.0, cur / _total_dur * 100.0))
                                    ms = _speed_re.search(line)
                                    speed = float(ms.group(1)) if ms else 0.0
                                    if speed <= 0:
                                        elapsed = max(0.001, now - _start_ts)
                                        speed = cur / elapsed if cur > 0 else 0.0
                                    eta = (_total_dur - cur) / speed if speed > 0 else 0.0
                                    print_user(
                                        f"🎬 Рендер: {_fmt_t(cur)}/{_fmt_t(_total_dur)} · {pct:.0f}% · ~{_fmt_t(eta)} осталось",
                                        "PROCESS"
                                    )
                                else:
                                    print_user(f"🎬 Рендер: {_fmt_t(cur)} (идёт кодирование)", "PROCESS")
                # Дочитываем stdout (для энкода обычно пуст — вывод идёт в файл, не в pipe).
                stdout_data = proc.stdout.read() if proc.stdout is not None else ""
                proc.wait()
            finally:
                if _timer:
                    _timer.cancel()

            if _timed_out['flag']:
                raise Exception("FFmpeg таймаут")

            returncode = proc.returncode
            stderr_text = "\n".join(stderr_lines)

            if returncode != 0:
                rc = returncode
                if rc is not None and rc > 0x7FFFFFFF:
                    rc_signed = rc - 0x100000000
                else:
                    rc_signed = rc
                error_msg = f"FFmpeg завершился с ошибкой (код {rc_signed})"
                print_debug(error_msg)
                stderr_tail = ""
                if stderr_text:
                    lines = stderr_text.strip().split('\n')[-15:]
                    stderr_tail = "\n".join(lines)
                    print_debug(f"Stderr (последние строки):\n{stderr_tail}")
                    print_user("Последние строки FFmpeg:\n" + "\n".join(lines[-5:]), "ERROR")
                if stdout_data:
                    print_debug(f"Stdout (последние 10 строк):")
                    for line in stdout_data.strip().split('\n')[-10:]:
                        print_debug(f"   {line}")
                raise Exception(f"FFmpeg error: {error_msg}. {stderr_tail[:300]}" if stderr_tail else f"FFmpeg error: {error_msg}")
        except subprocess.TimeoutExpired:
            raise Exception("FFmpeg таймаут")

    def _find_ffprobe(self):
        """Находит путь к ffprobe (бандл _MEIPASS, bin рядом с exe, PATH).
        Поддерживает работу в exe (PyInstaller)
        """
        try:
            import portable_paths  # type: ignore[reportMissingImports]
            exe = portable_paths.get_ffprobe_executable()
            if exe and (os.path.exists(exe) or shutil.which(exe)):
                return exe
        except Exception:
            pass
        
        # Проверка 1: для PyInstaller - в _MEIPASS/bin
        try:
            if hasattr(sys, '_MEIPASS'):
                meipass_bin = os.path.join(sys._MEIPASS, 'bin', 'ffprobe.exe' if sys.platform == 'win32' else 'ffprobe')
                if os.path.exists(meipass_bin):
                    return meipass_bin
        except Exception:
            pass
        
        # Проверка 2: bin рядом с exe (для портативных сборок)
        try:
            # Определяем директорию exe или скрипта
            is_frozen = getattr(sys, 'frozen', False) or getattr(sys, '_MEIPASS', None) is not None
            if is_frozen and hasattr(sys, 'executable'):
                exe_dir = os.path.dirname(sys.executable)
            else:
                try:
                    exe_dir = os.path.dirname(os.path.abspath(__file__))
                except:
                    exe_dir = os.getcwd()
            
            if sys.platform == 'win32':
                exe_bin = os.path.join(exe_dir, 'bin', 'ffprobe.exe')
                if os.path.exists(exe_bin):
                    return exe_bin
            else:
                exe_bin = os.path.join(exe_dir, 'bin', 'ffprobe')
                if os.path.exists(exe_bin):
                    return exe_bin
        except Exception:
            pass
        
        # Проверка 3: рядом с FFmpeg
        if os.path.exists(FFMPEG_EXE) and "bin" in FFMPEG_EXE:
            ffprobe_path = FFMPEG_EXE.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
            if os.path.exists(ffprobe_path):
                return ffprobe_path
        
        # Проверка 4: через imageio-ffmpeg
        try:
            import imageio_ffmpeg  # type: ignore
            ffmpeg_io = imageio_ffmpeg.get_ffmpeg_exe()
            if ffmpeg_io and os.path.exists(ffmpeg_io):
                ffmpeg_dir = os.path.dirname(ffmpeg_io)
                ffprobe_io = os.path.join(ffmpeg_dir, "ffprobe.exe" if sys.platform == 'win32' else "ffprobe")
                if os.path.exists(ffprobe_io):
                    return ffprobe_io
        except Exception:
            pass
        
        # Проверка 5: в PATH
        ffprobe_in_path = shutil.which("ffprobe")
        if ffprobe_in_path:
            return ffprobe_in_path

        # Не найден нигде — громко предупреждаем оператора (один раз), а не молча
        # отдаём строку "ffprobe", которая потом тихо упадёт в 0.0 длительности.
        if not getattr(self, '_ffprobe_missing_warned', False):
            self._ffprobe_missing_warned = True
            print_user(
                "❌ ffprobe не найден — положи его в src/bin/ или установи ffmpeg в PATH "
                "(длительности будут определяться менее точно через ffmpeg)",
                "ERROR"
            )
        return "ffprobe"
    
    def get_audio_duration(self, audio_path, use_cache=True):
        """
        Определяет длительность аудио/видео файла.
        Использует кэш для избежания повторных вызовов ffprobe/ffmpeg.
        """
        try:
            # Нормализуем путь перед использованием
            normalized_path = os.path.normpath(os.path.abspath(audio_path))
            
            # Проверяем кэш
            if use_cache and normalized_path in self._duration_cache:
                return self._duration_cache[normalized_path]
            
            # Проверяем существование файла
            if not os.path.exists(normalized_path):
                print_debug(f"Файл не найден для определения длительности: {normalized_path}")
                # Пробуем альтернативные пути
                alt_paths = [audio_path, os.path.abspath(audio_path)]
                for alt_path in alt_paths:
                    if os.path.exists(alt_path):
                        normalized_path = os.path.normpath(alt_path)
                        print_debug(f"Найден альтернативный путь: {normalized_path}")
                        break
                else:
                    return 0.0
            
            if not os.path.isfile(normalized_path):
                print_debug(f"Путь не является файлом: {normalized_path}")
                return 0.0
            
            file_size = os.path.getsize(normalized_path)
            if file_size == 0:
                print_debug(f"Файл пуст: {normalized_path}")
                return 0.0
            
            # Проверяем кэш еще раз после нормализации
            if use_cache and normalized_path in self._duration_cache:
                return self._duration_cache[normalized_path]
            
            # Находим ffprobe или используем ffmpeg как альтернативу
            ffprobe_exe = self._find_ffprobe()
            abs_path = os.path.abspath(normalized_path)
            
            # Проверяем, что ffprobe существует
            if not os.path.exists(ffprobe_exe) and not shutil.which(ffprobe_exe):
                # Альтернативный метод: используем ffmpeg для определения длительности
                print_debug(f"FFprobe не найден ({ffprobe_exe}), используем ffmpeg для определения длительности")
                try:
                    # Находим ffmpeg (аналогично поиску в объединении аудио)
                    ffmpeg_found = False
                    ffmpeg_path = FFMPEG_EXE
                    
                    if os.path.exists(FFMPEG_EXE):
                        ffmpeg_found = True
                        ffmpeg_path = FFMPEG_EXE
                    elif shutil.which(FFMPEG_EXE):
                        ffmpeg_found = True
                        ffmpeg_path = shutil.which(FFMPEG_EXE)
                    else:
                        try:
                            import imageio_ffmpeg
                            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                            if ffmpeg_path and os.path.exists(ffmpeg_path):
                                ffmpeg_found = True
                        except ImportError:
                            pass
                    
                    if ffmpeg_found and ffmpeg_path:
                        # Используем ffmpeg для определения длительности
                        result = subprocess.run(
                            [ffmpeg_path, "-i", abs_path, "-f", "null", "-"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            encoding='utf-8',
                            errors='replace',
                            **_SUBPROCESS_HIDE_WINDOW
                        )
                        # Парсим длительность из stderr (ffmpeg выводит длительность в stderr)
                        import re
                        duration_match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2})\.(\d{2})', result.stderr)
                        if duration_match:
                            hours, minutes, seconds, centiseconds = map(int, duration_match.groups())
                            duration = hours * 3600 + minutes * 60 + seconds + centiseconds / 100.0
                            if use_cache and duration > 0:
                                self._duration_cache[normalized_path] = duration
                            print_debug(f"Длительность определена через ffmpeg: {duration:.2f}с")
                            return duration
                        else:
                            print_debug(f"Не удалось найти длительность в выводе ffmpeg")
                            return 0.0
                    else:
                        print_debug(f"FFmpeg также не найден для альтернативного метода")
                        if not getattr(self, '_audio_dur_tool_warned', False):
                            self._audio_dur_tool_warned = True
                            print_user(
                                "⚠️ Не удалось измерить длительность аудио: ни ffprobe, ни ffmpeg не найдены. "
                                "Длительность принята за 0 — тайминги видео могут быть неверными. "
                                "Положи ffmpeg/ffprobe в src/bin/ или установи в PATH.",
                                "WARNING"
                            )
                        return 0.0
                except Exception as e:
                    print_debug(f"Ошибка альтернативного метода определения длительности: {e}")
                    import traceback
                    print_debug(traceback.format_exc())
                    return 0.0
            
            # Используем ffprobe если найден
            # Фильтруем None и преобразуем все в строки
            ffprobe_command = [ffprobe_exe, "-v", "error", "-show_entries", 
                 "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", 
                 abs_path]
            ffprobe_command = [str(arg) for arg in ffprobe_command if arg is not None]
            
            result = subprocess.run(
                ffprobe_command, 
                capture_output=True, 
                text=True, 
                timeout=10,
                encoding='utf-8',
                errors='replace',
                **_SUBPROCESS_HIDE_WINDOW
            )
            
            if result.returncode != 0:
                print_debug(f"Ошибка определения длительности через ffprobe для {abs_path}:")
                print_debug(f"   Команда: {ffprobe_exe}")
                print_debug(f"   stderr: {result.stderr[:300]}")
                print_debug(f"   stdout: {result.stdout[:300]}")
                return 0.0
            
            duration_str = result.stdout.strip()
            if not duration_str:
                print_debug(f"Пустой ответ от ffprobe для {abs_path}")
                return 0.0
            
            try:
                duration = float(duration_str)
                if duration <= 0:
                    print_debug(f"Некорректная длительность: {duration}")
                    return 0.0
                # Сохраняем в кэш
                if use_cache:
                    self._duration_cache[normalized_path] = duration
                return duration
            except ValueError:
                print_debug(f"Не удалось преобразовать длительность '{duration_str}' в число")
                return 0.0
        except subprocess.TimeoutExpired:
            print_debug(f"Таймаут при определении длительности: {audio_path}")
            return 0.0
        except Exception as e:
            print_debug(f"Исключение при определении длительности: {e}")
            import traceback
            print_debug(traceback.format_exc())
            return 0.0
    
    def get_scale_filter_for(self, width, height, path=None, fill=False):
        """
        Строит фильтр scale+pad с учётом масштаба (%) и смещения (x,y).
        path: если задан и есть в media_per_item — используются персональные значения.
        fill=True: заполнять кадр целиком (cover, центр-обрезка) вместо чёрных полей —
          для видео, чтобы не было letterbox на не-16:9 источниках (grok-видео).
        ВАЖНО: Не используем скобки () в pad-выражениях (ow*0.5-iw*0.5 вместо (ow-iw)*0.5),
        так как на Windows скобки могут ломать парсинг FFmpeg фильтров.
        """
        scale_pct = self.media_scale_pct
        offset_x = int(self.media_offset_x)
        offset_y = int(self.media_offset_y)
        if path and self.media_per_item:
            norm = os.path.normpath(os.path.abspath(path))
            per = self.media_per_item.get(norm) or self.media_per_item.get(path)
            if isinstance(per, dict):
                scale_pct = float(per.get('scale_pct', scale_pct))
                offset_x = int(per.get('offset_x', offset_x))
                offset_y = int(per.get('offset_y', offset_y))
        
        w, h = int(width), int(height)
        
        if abs(float(scale_pct) - 100.0) < 0.01 and offset_x == 0 and offset_y == 0:
            # ВАЖНО: НЕ используем скобки () в pad/crop-выражениях — Windows ломает парсинг FFmpeg
            # Вместо (ow-iw)*0.5 используем ow*0.5-iw*0.5 (математически эквивалентно)
            if fill:
                # Заполнить кадр целиком (cover): увеличить до покрытия + центр-обрезка, без чёрных полей
                return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=bilinear,crop={w}:{h}:iw*0.5-ow*0.5:ih*0.5-oh*0.5,setsar=1"
            # Простой случай: scale + pad по центру (вписать, чёрные поля если не совпадает аспект)
            return f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bilinear,pad={w}:{h}:ow*0.5-iw*0.5:oh*0.5-ih*0.5:black,setsar=1"
        
        scale_mult = max(0.01, min(5.0, float(scale_pct) / 100.0))
        
        if scale_mult > 1.001:
            # Zoom in (scale > 100%): увеличиваем картинку, потом центральная обрезка до целевого размера
            # force_original_aspect_ratio=increase гарантирует покрытие целевой области
            scaled_w = max(2, int(round(w * scale_mult / 2)) * 2)
            scaled_h = max(2, int(round(h * scale_mult / 2)) * 2)
            if offset_x == 0 and offset_y == 0:
                crop_x = "iw*0.5-ow*0.5"
                crop_y = "ih*0.5-oh*0.5"
            else:
                crop_x = f"iw*0.5-ow*0.5+{offset_x}"
                crop_y = f"ih*0.5-oh*0.5+{offset_y}"
            base = f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase:flags=bilinear,crop={w}:{h}:{crop_x}:{crop_y},setsar=1"
        elif scale_mult < 0.999:
            # Zoom out (scale < 100%): уменьшаем, pad чёрным
            scaled_w = max(2, int(round(w * scale_mult / 2)) * 2)
            scaled_h = max(2, int(round(h * scale_mult / 2)) * 2)
            if offset_x == 0 and offset_y == 0:
                pad_x = "ow*0.5-iw*0.5"
                pad_y = "oh*0.5-ih*0.5"
            else:
                pad_x = f"ow*0.5-iw*0.5+{offset_x}"
                pad_y = f"oh*0.5-ih*0.5+{offset_y}"
            base = f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=decrease:flags=bilinear,pad={w}:{h}:{pad_x}:{pad_y}:black,setsar=1"
        else:
            # Scale ~100%, только смещение
            if offset_x == 0 and offset_y == 0:
                pad_x = "ow*0.5-iw*0.5"
                pad_y = "oh*0.5-ih*0.5"
            else:
                pad_x = f"ow*0.5-iw*0.5+{offset_x}"
                pad_y = f"oh*0.5-ih*0.5+{offset_y}"
            base = f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bilinear,pad={w}:{h}:{pad_x}:{pad_y}:black,setsar=1"
        print_debug(f"get_scale_filter_for: scale_pct={scale_pct}, offset=({offset_x},{offset_y}), filter={base}")
        return base
    
    def get_vintage_filter(self):
        return "noise=alls=45:allf=t+u,eq=contrast=0.80:brightness=-0.15,hue=s=0"

    def _apply_color_grade(self, video_path, temp_dir, ffmpeg_args):
        """F. Единый grade поверх собранного видеоряда (пресет self.color_grade).

        Дефолт ВЫКЛ: без пресета выходим ДО любых команд — старый путь рендера
        байт-в-байт. С пресетом — один проход ffmpeg по video_path, результат
        атомарно заменяет исходник. Провал grade НЕ роняет рендер: остаёмся на
        неграйженном видеоряде с warning (recovery > avoidance).
        Возвращает video_path (файл заменён на месте либо оставлен как был).
        """
        vf = get_color_grade_filter(self.color_grade)
        if vf is None:
            if self.color_grade and str(self.color_grade).strip():
                self.log(f"⚠️ Неизвестный grade-пресет '{self.color_grade}' — рендер без grade "
                         f"(доступны: {', '.join(sorted(COLOR_GRADE_PRESETS))})")
            return video_path
        self.log(f"🎞 Grade-пресет '{str(self.color_grade).strip()}': единый проход по видеоряду")
        graded = os.path.join(temp_dir, "video_graded.mp4")
        cmd = [FFMPEG_EXE, "-i", video_path, "-vf", vf, "-an"]
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr",
                    "-movflags", "+faststart", "-y", graded])
        try:
            self.run_ffmpeg_command(cmd)
            if self._validate_fragment(graded):
                os.replace(graded, video_path)
                self.log("🎞 Grade применён")
            else:
                self.log("⚠️ Grade-проход не прошёл валидацию — оставляю видеоряд без grade")
        except Exception as e:
            print_debug(f"Grade-проход упал: {e}")
            self.log("⚠️ Grade-проход упал — оставляю видеоряд без grade")
        return video_path
    
    def apply_vintage_overlay(self, input_video, output_video, overlay_png, video_size):
        if not os.path.exists(overlay_png):
            shutil.copy2(input_video, output_video)
            return
        width, height = video_size.split('x')
        cmd = [FFMPEG_EXE, "-i", input_video, "-i", overlay_png,
               "-filter_complex", f"[1:v]scale={width}:{height}[png];[0:v][png]overlay=(W-w)*0.5:(H-h)*0.5",
               "-c:a", "copy", "-y", output_video]
        self.run_ffmpeg_command(cmd)
    
    def apply_overlays(self, input_video, output_video, video_size, ffmpeg_args):
        """
        Применяет оверлеи (наложения) поверх видео.
        Оверлеи накладываются в порядке списка: первый — ниже, последний — сверху.
        Каждый оверлей масштабируется до размера видео и имеет свою прозрачность.
        """
        valid_overlays = [ov for ov in self.overlays if ov.get('path') and os.path.exists(ov['path'])]
        if not valid_overlays:
            shutil.copy2(input_video, output_video)
            return
        
        width, height = video_size.split('x')
        
        inputs = ["-i", input_video]
        for ov in valid_overlays:
            ext = os.path.splitext(ov['path'])[1].lower()
            if ext in SUPPORTED_VIDEO_FORMATS:
                inputs.extend(["-stream_loop", "-1", "-i", ov['path']])
            else:
                inputs.extend(["-i", ov['path']])
        
        filter_parts = []
        current_stream = "[0:v]"
        
        for i, ov in enumerate(valid_overlays):
            input_idx = i + 1
            opacity = float(ov.get('opacity', 0.5))
            opacity = max(0.0, min(1.0, opacity))
            
            ov_scaled = f"[{input_idx}:v]scale={width}:{height},format=rgba,colorchannelmixer=aa={opacity:.2f}[ov{i}]"
            filter_parts.append(ov_scaled)
            
            out_tag = f"[out{i}]" if i < len(valid_overlays) - 1 else "[vout]"
            overlay_cmd = f"{current_stream}[ov{i}]overlay=0:0:format=auto:shortest=1{out_tag}"
            filter_parts.append(overlay_cmd)
            
            current_stream = out_tag
        
        filter_complex = ";".join(filter_parts)
        
        cmd = [FFMPEG_EXE] + inputs + [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "0:a?",
            "-c:a", "copy"
        ]
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", output_video])
        
        self.log(f"🎭 Применение {len(valid_overlays)} оверлеев...")
        for i, ov in enumerate(valid_overlays):
            self.log(f"   [{i+1}] {os.path.basename(ov['path'])} — {int(float(ov.get('opacity', 0.5))*100)}%")
        
        self.run_ffmpeg_command(cmd)
    
    # ==================================================================
    # СИНХРОН картинка↔озвучка (whisper word-timestamps → длительности сцен)
    # ==================================================================
    def _media_has_audio(self, path):
        """True если у файла есть аудио-дорожка (ffprobe)."""
        try:
            ffprobe_exe = self._find_ffprobe()
            if not ffprobe_exe:
                return False
            r = subprocess.run([ffprobe_exe, "-v", "error", "-select_streams", "a:0",
                                "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                               capture_output=True, text=True, timeout=20)
            return "audio" in (r.stdout or "")
        except Exception:
            return False

    def _compute_synced_image_durations(self, audio_file, timeline, audio_duration):
        """Главный оркестратор синхрона. Читает scenes.json, гоняет whisper (timestamps),
        выравнивает длительности по сценам, схлопывает длительности пропавших картинок
        в предыдущую существующую и возвращает НОВЫЙ timeline с переменными длительностями
        у image-сегментов. При любой проблеме возвращает None (→ откат на uniform).

        Ведущие video-сегменты, чьё имя маппится на сцену (NNN_video.mp4), ТОЖЕ синкаются под
        озвучку: им ставится целевая длина T + флаг retime (пре-пасс _retime_synced_videos потом
        приводит клип к T: T<L обрезка, T>L замедление). Video без маппинга — натуральной длины.
        Маппинг сцена→картинка — по ИНДЕКСУ ИМЕНИ файла (NNN.jpg = сцена NNN, 1-based),
        НЕ по позиции в списке.
        """
        try:
            if not timeline:
                return None
            # 1. scenes.json
            scenes_path = os.path.join(self.project_dir, "googler", "scenes.json")
            if not os.path.isfile(scenes_path):
                self.log(f"   ⚠️ Синхрон откат: scenes.json не найден ({scenes_path}) → uniform")
                return None
            try:
                with open(scenes_path, 'r', encoding='utf-8', errors='replace') as f:
                    scenes = json.load(f)
            except Exception as e:
                self.log(f"   ⚠️ Синхрон откат: не прочитать scenes.json ({e}) → uniform")
                return None
            if not isinstance(scenes, list) or not scenes:
                print_debug("Синхрон: scenes.json пуст/не список → uniform")
                return None
            scene_texts = []
            for sc in scenes:
                if isinstance(sc, dict):
                    scene_texts.append(str(sc.get('text_segment', '') or ''))
                else:
                    scene_texts.append(str(sc or ''))

            # 2. Сопоставляем сегменты timeline со сценами по индексу имени файла.
            #    Картинки (NNN.jpg) И ведущие видео-клипы (NNN_video.mp4 → сцена NNN). Видео,
            #    смапленное на сцену, синкается под озвучку наравне с картинкой (ретайм при рендере,
            #    см. synced_video_pos + _retime_synced_videos). Видео без маппинга остаётся
            #    натуральной длины (его время вычитается из аудио ниже).
            n_scenes = len(scene_texts)
            scene_to_tl = {}
            synced_video_pos = set()      # позиции video-сегментов timeline, смапленных на сцену
            for ti, seg in enumerate(timeline):
                _t = seg.get('type')
                if _t not in ('image', 'video'):
                    continue
                ref = seg.get('orig_path') or seg.get('source') or ''
                sidx = _scene_index_from_filename(ref)
                if sidx is not None and 0 <= sidx < n_scenes and sidx not in scene_to_tl:
                    scene_to_tl[sidx] = ti
                    if _t == 'video':
                        synced_video_pos.add(ti)
            if not scene_to_tl:
                # Сегменты не маппятся на сцены (другой проект/именование) → откат.
                self.log("   ⚠️ Синхрон откат: ни один сегмент не сопоставлен со сценой по имени → uniform")
                return None

            # 3. whisper-таймкоды (даже если субтитры выключены).
            #    Язык АУДИО определяем по тексту сцен (он в языке озвучки), а НЕ по UI
            #    subtitle_language (она часто "Russian" даже для румынского/чешского аудио →
            #    whisper форсился в 'ru' → транскрипция-мусор → синхрон ломался на ~4с).
            _sync_lang = None
            try:
                from langdetect import detect as _ld_detect
                _joined = " ".join(t for t in scene_texts if t).strip()[:3000]
                if _joined:
                    _sync_lang = _ld_detect(_joined)
                    self.log(f"   🎯 Синхрон: язык аудио по тексту сцен = '{_sync_lang}'")
            except Exception as _le:
                print_debug(f"langdetect сцен не удался ({_le}) → whisper авто-детект")
            words = self._whisper_words(audio_file, language=_sync_lang)
            if not words:
                self.log("   ⚠️ Синхрон откат: нет whisper-слов → uniform")
                return None

            # 4. Длительности по КАЖДОЙ сцене
            try:
                durations = _align_durations_v2(words, scene_texts, audio_duration)
            except Exception as _ae:
                print_debug(f"align v2 упал ({_ae}) → откат на v1 count-based")
                durations = _align_durations(words, scene_texts, audio_duration)
            if not durations or len(durations) != n_scenes:
                print_debug("Синхрон: выравниватель вернул некорректный результат → uniform")
                return None

            # 5. Схлопывание пропавших картинок: scene_present[i] = есть ли image-сегмент
            scene_present = [i in scene_to_tl for i in range(n_scenes)]
            if not any(scene_present):
                return None
            # collapsed[scene_idx с картинкой] = суммарная длительность (своя + пропавшие соседи)
            collapsed = _collapse_missing_images(scene_present, durations)

            # 5b. Масштаб к длине аудио ЗА ВЫЧЕТОМ видео-сегментов (у видео своя длительность) —
            # картинки заполняют остаток звука. Выравниватель даёт сумму по речи (паузы между
            # фразами и тишина в конце не учтены), поэтому растягиваем пропорционально: синхрон
            # сохраняется, а видеоряд покрывает ВЕСЬ звук (не обрывается раньше аудио).
            # Вычитаем натуральную длину ТОЛЬКО НЕсмапленных видео (они остаются native).
            # Смапленные видео теперь распределяются по сценам наравне с картинками.
            _video_time = sum(s.get('duration', 0) for ti, s in enumerate(timeline)
                              if s.get('type') == 'video' and ti not in synced_video_pos)
            _target = max(0.0, audio_duration - _video_time)
            _csum = sum(collapsed.values())
            if _csum > 0 and _target > 0:
                _factor = _target / _csum
                collapsed = {k: max(0.5, v * _factor) for k, v in collapsed.items()}

            # 6. Применяем длительности к копии timeline (image — по collapsed, video — как есть)
            new_timeline = [dict(seg) for seg in timeline]
            applied = 0
            for sidx, dur in collapsed.items():
                ti = scene_to_tl.get(sidx)
                if ti is None:
                    continue
                seg = new_timeline[ti]
                if ti in synced_video_pos:
                    # видео: сохраняем натуральную длину L до затирания + флаг для пре-пасса ретайма
                    seg['native_duration'] = float(timeline[ti].get('duration', 0) or 0)
                    seg['retime'] = True
                seg['duration'] = max(0.5, float(dur))
                seg['exact'] = True  # рендер возьмёт РОВНО эту длительность (синхрон), не распределяет
                applied += 1
            if applied == 0:
                return None

            img_total = sum(s['duration'] for s in new_timeline if s.get('type') == 'image')
            _nvid = len(synced_video_pos)
            self.log(f"   🎯 Синхрон: {applied} сегментов выровнены по озвучке"
                     f"{f' (в т.ч. {_nvid} видео-клипов)' if _nvid else ''} "
                     f"({n_scenes} сцен, картинок-сумма {img_total:.1f}с / аудио {audio_duration:.1f}с)")
            return new_timeline
        except Exception as e:
            print_debug(f"Синхрон: непредвиденная ошибка ({e}) → uniform")
            import traceback
            print_debug(traceback.format_exc())
            return None

    def _retime_synced_videos(self, timeline, temp_dir, ffmpeg_args):
        """Пре-пасс: ведущие видео-клипы, синканные под озвучку (флаг retime от синхрона),
        приводим к целевой длине сцены T. T<L → обрезка; T>L → замедление (setpts=(T/L)*PTS),
        затем trim до точного T. Меняем source сегмента на ретаймленный файл (натуральное
        разрешение сохраняем — масштаб накладывает основной пайплайн позже). Любая ошибка по
        клипу → оставляем оригинал (рендер не ломается). Сегменты без retime не трогаем.
        Изолировано: горячий путь рендера не меняется."""
        if not timeline:
            return timeline
        target_fps = getattr(self, 'target_fps', 30)
        done = 0
        for idx, seg in enumerate(timeline):
            if not seg.get('retime') or seg.get('type') != 'video':
                continue
            try:
                T = float(seg.get('duration') or 0)
                L = float(seg.get('native_duration') or 0)
                src = seg.get('source')
                if not src or not os.path.isfile(src) or T <= 0 or L <= 0:
                    seg.pop('retime', None)
                    continue
                if abs(T - L) < 0.1:           # уже совпадают — ретайм не нужен
                    seg.pop('retime', None)
                    continue
                out_path = os.path.join(temp_dir, f"retimed_v_{idx:04d}.mp4")
                if T > L:
                    factor = T / L             # >1 → замедление до T
                    vf = (f"setpts={factor:.6f}*PTS,trim=duration={T:.6f},"
                          f"fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS")
                else:
                    vf = (f"trim=duration={T:.6f},"
                          f"fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS")
                # Промежуточный проход — near-lossless libx264 crf18 (НЕ финальный энкодер):
                # клип ещё раз перекодируется основным пайплайном (scale+fps) → гасим двойную
                # потерю качества на ведущих клипах (crf18→crf26 вместо crf26→crf26).
                cmd = [FFMPEG_EXE, "-i", src, "-an", "-vf", vf,
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                       "-pix_fmt", "yuv420p", "-r", str(target_fps), "-vsync", "cfr", "-y", out_path]
                self.run_ffmpeg_command(cmd)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                    seg['audio_source'] = src    # оригинал со звуком: ретайм кодирует -an (для «звука из видео»)
                    seg['source'] = out_path
                    seg['orig_path'] = out_path   # дальше сегмент уже нужной длины
                    seg.pop('retime', None)
                    done += 1
                    _mode = f"замедлен {T/L:.2f}×" if T > L else "обрезан"
                    print_debug(f"   ретайм видео сцены: {os.path.basename(src)} {L:.1f}с→{T:.1f}с ({_mode})")
                else:
                    # ffmpeg не дал файл → откат к натуральной длине L (консистентно: иначе при T>L
                    # downstream-trim не удлинит клип, а timeline думает T → молчаливый десинк хвоста)
                    seg['duration'] = L
                    seg.pop('exact', None)
                    seg.pop('retime', None)
                    self.log(f"   ⚠️ Ретайм видео не дал файл → клип натуральной длиной {L:.1f}с: {os.path.basename(src)}")
            except Exception as e:
                _L = seg.get('native_duration')
                if _L:
                    seg['duration'] = float(_L)
                    seg.pop('exact', None)
                seg.pop('retime', None)
                self.log(f"   ⚠️ Ретайм видео упал ({e}) → клип натуральной длиной")
        if done:
            self.log(f"   🎬 Видео-клипов синканы под озвучку (ретайм): {done}")
        return timeline

    def _whisper_words(self, audio_file, language=None):
        """Прогоняет whisper с word_timestamps и возвращает плоский список слов
        [{'start','end','word'}, ...] в порядке речи. Кэширует результат по audio_file
        (чтобы и субтитры, и синхрон-картинок брали один прогон). None при ошибке/недоступности.

        language: ISO-код языка АУДИО ('ro','cs','ru',...). Если None — whisper авто-детект.
        НЕ форсим self.subtitle_language (UI-настройка) — она часто не совпадает с языком аудио
        (voicegencore-проекты остаются 'Russian' → не-русское аудио транскрибилось мусором →
        синхрон по предложениям ломался). Язык берём по тексту сцен (см. вызов в синхроне)."""
        if not WHISPER_AVAILABLE:
            return None
        try:
            cache = getattr(self, '_whisper_words_cache', None)
            if cache is not None and cache.get('audio') == audio_file:
                return cache.get('words')
            # device по РЕАЛЬНОМУ наличию CUDA, а не self.gpu_available — он True и для
            # ffmpeg-VideoToolbox на Mac, где CUDA нет → whisper.load_model("base", device="cuda")
            # падал мгновенно → синхрон И субтитры молча не работали на Mac.
            try:
                import torch
                _cuda = bool(torch.cuda.is_available())
            except Exception:
                _cuda = False
            # triton>=3.3 (идёт с torch 2.7) ломает CUDA-ядра word_timestamps: кидает
            # AttributeError («Cannot set attribute 'src'»), а whisper/timing.py ловит только
            # RuntimeError → ошибка пролетала в наш except → синхрон МОЛЧА откатывался в uniform
            # (найдено на минском боксе 15.07). Глушим triton-ядра функцией, кидающей RuntimeError,
            # который whisper штатно ловит и уходит на свои torch/cpu-фолбэки.
            # Цена фолбэка замерена: 70-мин аудио = ~105с на RTX 4070S (base, fp16).
            if _cuda:
                def _no_triton(*_a, **_k):
                    raise RuntimeError("triton kernels disabled (triton>=3.3 incompatible with whisper)")
                import sys as _sys, types as _types
                _stub = _types.ModuleType("whisper.triton_ops")
                _stub.median_filter_cuda = _no_triton
                _stub.dtw_cuda = _no_triton
                _sys.modules["whisper.triton_ops"] = _stub  # перехват in-function import
                try:
                    import whisper.timing as _wtiming  # перехват module-level import
                    for _fn in ("median_filter_cuda", "dtw_cuda"):
                        if hasattr(_wtiming, _fn):
                            setattr(_wtiming, _fn, _no_triton)
                except Exception:
                    pass
            if self.whisper_model is None:
                self.whisper_model = whisper.load_model("base", device=("cuda" if _cuda else "cpu"))
            # Язык: явно переданный (определён по тексту сцен) → иначе авто-детект whisper.
            _tr_kwargs = dict(word_timestamps=True, fp16=_cuda)
            if language:
                _tr_kwargs['language'] = language
            print_debug(f"whisper transcribe: language={language or 'auto-detect'}")
            result = self.whisper_model.transcribe(audio_file, **_tr_kwargs)
            words = []
            for seg in result.get('segments', []):
                if isinstance(seg, dict) and 'words' in seg:
                    w = seg.get('words', [])
                    if w:
                        words.extend(w)
            self._whisper_words_cache = {'audio': audio_file, 'words': words}
            return words
        except Exception as e:
            print_debug(f"_whisper_words: ошибка whisper-транскрипции: {e}")
            import traceback
            print_debug(traceback.format_exc())
            return None

    def create_subtitle_file_from_audio(self, audio_file):
        if not WHISPER_AVAILABLE:
            return None
        try:
            print_user("Создание субтитров...", "PROCESS")
            segments = self._whisper_words(audio_file)
            if segments is None:
                raise Exception("whisper не вернул результат")

            subtitle_path = os.path.join(self.project_dir, "subtitles.srt")

            with open(subtitle_path, 'w', encoding='utf-8', errors='replace') as f:
                for i, word in enumerate(segments, 1):
                    start = word.get('start', 0)
                    end = word.get('end', start + 0.5)
                    text = word.get('word', '').strip()
                    if text:
                        f.write(f"{i}\n{self._format_timestamp(start)} --> {self._format_timestamp(end)}\n{text}\n\n")
            
            print_user(f"Субтитры созданы: {len(segments)} слов", "SUCCESS")
            return subtitle_path
        except Exception as e:
            print_error_user_friendly("Ошибка при создании субтитров")
            print_debug(f"Детали: {e}")
            import traceback
            print_debug(traceback.format_exc())
            return None
    
    def _format_timestamp(self, seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    
    def get_kenburns_filter(self, width, height, duration, direction='random', image_path=None, frames=None):
        """
        Плавный Ken Burns эффект с линейной интерполяцией масштаба.

        Решение проблемы дёргания через апскейл и линейную интерполяцию zoom.
        Апскейл до 7680px по длинной стороне для максимально плавного зума без артефактов.
        frames: точное число кадров (frame-lock) — если задано, используем его вместо int(dur*fps)
          (убирает floor-смещение, копившее дрейф синхрона). round вместо int — на случай float-epsilon.
        """
        target_w, target_h = int(width), int(height)
        fps = getattr(self, 'target_fps', 30)
        total_frames = int(frames) if frames else max(round(duration * fps), 1)
        
        zoom_factor = float(self.kenburns_zoom)
        if zoom_factor <= 1.0 or zoom_factor > 3.0:
            zoom_factor = 1.2
        
        # Апскейл по длинной стороне для плавного зума. История капа: 7680 (8K, часы CPU) → 3840
        # (4K, 04.07) → 2560 (15.07, замер на 4070S: zoompan −28%, весь KB-рендер ~−20-25%).
        # Качество: видимое окно при zoom=1.2 = 2560/1.2 ≈ 2133px > 1920 → резкость 1080p не
        # страдает вплоть до zoom≈1.33. Риск: на очень медленных зумах (<1.08) и сценах >10с
        # шаг пана может падать <1px/кадр (лёгкий степпинг) — если глаз заметит, вернуть 3840.
        KENBURNS_UPSCALE_PX = 2560
        if target_w >= target_h:
            upscale_w = KENBURNS_UPSCALE_PX
            aspect = target_h / target_w
            upscale_h = int(round(KENBURNS_UPSCALE_PX * aspect))
            upscale_h = upscale_h if upscale_h % 2 == 0 else upscale_h + 1
        else:
            upscale_h = KENBURNS_UPSCALE_PX
            aspect = target_w / target_h
            upscale_w = int(round(KENBURNS_UPSCALE_PX * aspect))
            upscale_w = upscale_w if upscale_w % 2 == 0 else upscale_w + 1
        
        up_w = upscale_w
        up_h = upscale_h
        
        # Доступные направления
        all_directions = [
            'zoom_in_center', 'zoom_out_center',
            'pan_left', 'pan_right', 'pan_up', 'pan_down',
            'zoom_in_pan_right', 'zoom_in_pan_left',
            'zoom_out_pan_right', 'zoom_out_pan_left',
        ]
        if direction == 'random':
            direction = random.choice(all_directions)
        
        # Линейная интерполяция масштаба через dz (прирост zoom за кадр)
        # dz = (end_zoom - 1.0) / frames - это прирост на каждый кадр
        denom = max(total_frames - 1, 1)
        dz = (zoom_factor - 1.0) / total_frames  # Прирост zoom за кадр (линейная интерполяция)
        
        # Центральная точка для центрированного зума
        # iw/ih здесь = размеры после апскейла и crop
        x_center = "iw/2-(iw/zoom/2)"
        y_center = "ih/2-(ih/zoom/2)"
        
        if direction == 'zoom_in_center':
            # Линейная интерполяция: zoom = 1.0 + dz * (on-1), где on - номер кадра (начинается с 1)
            z_expr = f"1.0+{dz:.8f}*(on-1)"
            x_expr = x_center
            y_expr = y_center
            
        elif direction == 'zoom_out_center':
            # Zoom начинается с zoom_factor и уменьшается на dz каждый кадр
            z_expr = f"{zoom_factor}-{dz:.8f}*(on-1)"
            x_expr = x_center
            y_expr = y_center
            
        elif direction == 'pan_left':
            z_expr = f"{zoom_factor}"
            x_expr = f"(iw-iw/zoom)*(1-(on-1)/{denom})"
            y_expr = "(ih-ih/zoom)/2"
            
        elif direction == 'pan_right':
            z_expr = f"{zoom_factor}"
            x_expr = f"(iw-iw/zoom)*(on-1)/{denom}"
            y_expr = "(ih-ih/zoom)/2"
            
        elif direction == 'pan_up':
            z_expr = f"{zoom_factor}"
            x_expr = "(iw-iw/zoom)/2"
            y_expr = f"(ih-ih/zoom)*(1-(on-1)/{denom})"
            
        elif direction == 'pan_down':
            z_expr = f"{zoom_factor}"
            x_expr = "(iw-iw/zoom)/2"
            y_expr = f"(ih-ih/zoom)*(on-1)/{denom}"
            
        elif direction == 'zoom_in_pan_right':
            z_expr = f"1.0+{dz:.8f}*(on-1)"
            x_expr = f"(iw-iw/zoom)*(on-1)/{denom}"
            y_expr = "(ih-ih/zoom)/2"
            
        elif direction == 'zoom_in_pan_left':
            z_expr = f"1.0+{dz:.8f}*(on-1)"
            x_expr = f"(iw-iw/zoom)*(1-(on-1)/{denom})"
            y_expr = "(ih-ih/zoom)/2"
            
        elif direction == 'zoom_out_pan_right':
            z_expr = f"{zoom_factor}-{dz:.8f}*(on-1)"
            x_expr = f"(iw-iw/zoom)*(on-1)/{denom}"
            y_expr = "(ih-ih/zoom)/2"
            
        elif direction == 'zoom_out_pan_left':
            z_expr = f"{zoom_factor}-{dz:.8f}*(on-1)"
            x_expr = f"(iw-iw/zoom)*(1-(on-1)/{denom})"
            y_expr = "(ih-ih/zoom)/2"
        
        else:
            z_expr = f"1.0+{dz:.8f}*(on-1)"
            x_expr = x_center
            y_expr = y_center
        
        # КЛЮЧЕВАЯ цепочка фильтров с апскейлом до 7680px для плавности:
        #
        # 1. scale с force_original_aspect_ratio=increase — масштабирует картинку
        #    чтобы она ПОКРЫВАЛА целевой размер (без чёрных полос, сохраняя пропорции)
        # 2. crop — обрезает до точного target_w x target_h (центральная обрезка)
        # 3. scale — АПСКЕЙЛ (3840px по длинной стороне) с lanczos для плавности
        # 4. zoompan — линейная интерполяция zoom, x/y на высоком разрешении
        # 5. scale — даунскейл до целевого разрешения с lanczos (anti-aliasing)
        # 6. format — yuv420p для совместимости с кодировщиками
        
        # Апскейл перед zoompan для плавности (до 7680px по длинной стороне)
        scale_before_zoom = f"scale={up_w}:{up_h}:flags=bilinear"
        zoompan_s = f"{up_w}x{up_h}"
        
        # 1. Масштабирование с учетом media_scale_pct (центрированное, пропорциональное)
        # Получаем настройки масштабирования для этой конкретной картинки
        scale_pct = self.media_scale_pct
        offset_x = int(self.media_offset_x)
        offset_y = int(self.media_offset_y)
        if image_path and self.media_per_item:
            norm = os.path.normpath(os.path.abspath(image_path))
            per = self.media_per_item.get(norm) or self.media_per_item.get(image_path)
            if isinstance(per, dict):
                scale_pct = float(per.get('scale_pct', scale_pct))
                offset_x = int(per.get('offset_x', offset_x))
                offset_y = int(per.get('offset_y', offset_y))
        
        scale_mult = max(0.01, min(5.0, float(scale_pct) / 100.0))
        if scale_mult > 1.001:
            # Zoom in (scale > 100%): увеличиваем, потом центральная обрезка
            # force_original_aspect_ratio=increase гарантирует покрытие целевой области
            scaled_w = max(2, int(round(target_w * scale_mult / 2)) * 2)
            scaled_h = max(2, int(round(target_h * scale_mult / 2)) * 2)
            if offset_x == 0 and offset_y == 0:
                crop_x = "iw*0.5-ow*0.5"
                crop_y = "ih*0.5-oh*0.5"
            else:
                crop_x = f"iw*0.5-ow*0.5+{offset_x}"
                crop_y = f"ih*0.5-oh*0.5+{offset_y}"
            scale_step = f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase:flags=bilinear,crop={target_w}:{target_h}:{crop_x}:{crop_y}"
        elif scale_mult < 0.999:
            # Zoom out (scale < 100%): уменьшаем, pad чёрным
            scaled_w = max(2, int(round(target_w * scale_mult / 2)) * 2)
            scaled_h = max(2, int(round(target_h * scale_mult / 2)) * 2)
            if offset_x == 0 and offset_y == 0:
                pad_x = "ow*0.5-iw*0.5"
                pad_y = "oh*0.5-ih*0.5"
            else:
                pad_x = f"ow*0.5-iw*0.5+{offset_x}"
                pad_y = f"oh*0.5-ih*0.5+{offset_y}"
            scale_step = f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=decrease:flags=bilinear,pad={target_w}:{target_h}:{pad_x}:{pad_y}:black"
        else:
            # Scale ~100%: подгоняем под размер, pad по центру
            if offset_x == 0 and offset_y == 0:
                pad_x = "ow*0.5-iw*0.5"
                pad_y = "oh*0.5-ih*0.5"
            else:
                pad_x = f"ow*0.5-iw*0.5+{offset_x}"
                pad_y = f"oh*0.5-ih*0.5+{offset_y}"
            scale_step = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:flags=bilinear,pad={target_w}:{target_h}:{pad_x}:{pad_y}:black"
        
        filter_str = (
            f"{scale_step},"
            f"setsar=1,"
            f"{scale_before_zoom},"
            f"zoompan=z={z_expr}"
            f":x={x_expr}:y={y_expr}"
            f":d={total_frames}:fps={fps}"
            f":s={zoompan_s},"
            f"scale={target_w}:{target_h}:flags=bilinear,"
            f"setsar=1,"
            f"format=yuv420p"
        )
        self.log(f"📷 Ken Burns фильтр ({direction}): zoom={zoom_factor:.2f}, апскейл={up_w}x{up_h}, кадров={total_frames}, dz={dz:.8f}")
        print_debug(f"Ken Burns ({direction}): input→scale→crop→upscale→zoompan→scale {target_w}x{target_h}")
        print_debug(f"  zoom: {zoom_factor}, frames: {total_frames}, dz: {dz:.8f}")
        print_debug(f"  Filter: {filter_str[:200]}...")
        return filter_str
    
    def create_slideshow_with_transitions(self, image_paths, duration, temp_dir, ffmpeg_args, output_name=None, exact_per_image=None, exact_frames=None):
        """
        Создаёт слайдшоу. output_name: уникальное имя при поштучной обработке (избегает перезаписи).
        exact_per_image: если задано (float или список длин на картинку) — длительность(и)
          берутся КАК ЕСТЬ (синхрон по озвучке), без клампа в [min..max]. Только пол 0.5с.
          Применяется к равномерному (один пер-имэдж) случаю; для синхрона картинки приходят
          по одной на вызов, поэтому достаточно скалярного значения.
        exact_frames: точное число кадров на одиночную картинку (frame-lock, фикс дрейфа синхрона).
          При одной картинке энкод идёт через -frames:v вместо -t → покадрово точно, без floor.
        Оптимизировано:
        - Без эффектов: быстрый concat (1 вызов FFmpeg)
        - С Ken Burns: отдельные видео + concat
        - С Fade: ОДИН filter_complex для всех xfade (вместо N-1 вызовов)
        """
        width, height = ffmpeg_args['size'].split('x')
        num_images = len(image_paths)
        # frame-lock: точное число кадров для ОДИНОЧНОЙ картинки (синхрон приходит по 1) → -frames:v
        _lock_frames = None
        if num_images == 1 and exact_frames:
            try:
                _lock_frames = max(1, int(round(float(exact_frames))))
            except Exception:
                _lock_frames = None
        # Синхрон: точная длительность на картинку (без клампа min/max).
        _exact_dpi = None
        if exact_per_image is not None:
            try:
                if isinstance(exact_per_image, (list, tuple)) and exact_per_image:
                    _exact_dpi = max(0.5, float(exact_per_image[0]))
                else:
                    _exact_dpi = max(0.5, float(exact_per_image))
            except Exception:
                _exact_dpi = None

        if num_images == 0:
            return None
        
        # Защита от некорректной длительности
        if duration <= 0:
            print_debug(f"Некорректная длительность {duration}, использую 10 сек на картинку")
            duration = num_images * 10
        
        # Рассчитываем длительность на картинку (в диапазоне min..max)
        budget = duration / num_images
        if _exact_dpi is not None:
            # Синхрон по озвучке: точная длительность, без клампа в [min..max].
            duration_per_image = _exact_dpi
        else:
            duration_per_image = max(self.min_image_duration, min(self.max_image_duration, budget))
        duration_per_image = max(duration_per_image, 0.5)  # Абсолютный минимум 0.5 сек

        # Проверка: хватит ли картинок для заполнения времени?
        actual_duration = duration_per_image * num_images
        # При синхроне длительность задана точно — зацикливать не нужно.
        needs_loop = (_exact_dpi is None) and (actual_duration < duration * 0.95)
        
        if needs_loop:
            print_debug(f"Картинок ({num_images}) хватает на {actual_duration:.0f}с, нужно {duration:.0f}с")
            print_user("Мало картинок, создаю зацикленное слайдшоу...", "INFO")
        
        # === БЫСТРЫЙ ПУТЬ: без эффектов - один вызов FFmpeg ===
        if not self.enable_kenburns and not self.enable_fade:
            print_debug(f"Быстрое слайдшоу: {num_images} картинок, {duration_per_image:.2f}с каждая")
            
            video_filter = self.get_scale_filter_for(width, height)
            if self.vintage_effect:
                video_filter = f"{video_filter},{self.get_vintage_filter()}"
            
            output = os.path.join(temp_dir, output_name or "slideshow.mp4")
            total_slideshow_duration = duration_per_image * num_images
            
            # Для 1 картинки: concat demuxer даёт 1 кадр (баг FFmpeg #6128). Используем -loop 1.
            if num_images == 1:
                img_path = image_paths[0]
                normalized_path = img_path.replace('\\', '/')
                cmd = [FFMPEG_EXE, "-loop", "1", "-i", normalized_path,
                       "-vf", video_filter]
                # frame-lock: -frames:v (точно) вместо -t (даёт floor(dur*fps))
                cmd.extend((["-frames:v", str(_lock_frames)] if _lock_frames else ["-t", str(duration_per_image)]))
                cmd.extend(self._get_encoder_params(ffmpeg_args))
                cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr", "-y", output])
                self.run_ffmpeg_command(cmd)
            else:
                concat_base = (output_name or "slideshow").replace(".mp4", "").replace(".", "_")
                concat_list = os.path.join(temp_dir, f"{concat_base}_list.txt")
                with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
                    for img_path in image_paths:
                        escaped = _escape_concat_path(img_path)
                        f.write(f"file '{escaped}'\n")
                        f.write(f"duration {duration_per_image}\n")
                    escaped = _escape_concat_path(image_paths[-1])
                    f.write(f"file '{escaped}'\n")
                    f.write(f"duration 0.04\n")
                
                print_debug(f"Видео фильтр: {video_filter}")
                if "max(" in video_filter and "pad" in video_filter:
                    print_debug(f"⚠️ ВНИМАНИЕ: Обнаружен max() в pad фильтре! Фильтр: {video_filter}")
                
                cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list,
                       "-vf", video_filter]
                cmd.extend(self._get_encoder_params(ffmpeg_args))
                cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr",
                           "-t", str(total_slideshow_duration), "-y", output])
                self.run_ffmpeg_command(cmd)
            
            # Если нужно зациклить - делаем это с готовым видео
            if needs_loop:
                looped_output = os.path.join(temp_dir, "slideshow_looped_temp.mp4")
                print_debug(f"Зацикливаю видео до {duration:.0f}с...")
                
                # Убеждаемся, что входной и выходной файлы разные
                if os.path.normpath(output) == os.path.normpath(looped_output):
                    looped_output = os.path.join(temp_dir, "slideshow_looped_final.mp4")
                
                if self._safe_loop_trim(output, duration, looped_output, ffmpeg_args):
                    output = looped_output
                else:
                    raise Exception("Не удалось зациклить слайдшоу (битый фрагмент после нарезки)")
            
            print_debug(f"Слайдшоу готово")
            return output
        
        # === С ЭФФЕКТАМИ ===
        fade_dur = self.fade_duration if self.enable_fade else 0
        fade_dur_images = self.fade_duration_images if self.enable_fade else 0
        
        # Корректируем длительность для fade overlap
        if self.enable_fade and num_images > 1:
            total_overlap = fade_dur_images * (num_images - 1)
            # Видимое время в диапазоне min..max, итого = visible + fade
            min_per_slot = self.min_image_duration + fade_dur_images
            max_per_slot = self.max_image_duration + fade_dur_images
            budget = (duration + total_overlap) / num_images
            duration_per_image = max(min_per_slot, min(max_per_slot, budget))
            
            # Проверка для fade: хватит ли картинок?
            actual_duration = duration_per_image * num_images - total_overlap
            if actual_duration < duration * 0.95:
                # Для fade зацикливаем картинки, но с лимитом
                MAX_IMAGES_WITH_FADE = 100  # Лимит чтобы не убить RAM
                cycles_needed = min(int(duration / actual_duration) + 1, MAX_IMAGES_WITH_FADE // num_images + 1)
                new_count = min(num_images * cycles_needed, MAX_IMAGES_WITH_FADE)
                
                print_debug(f"Мало картинок для fade ({num_images}), зацикливаю до {new_count}...")
                image_paths = (list(image_paths) * cycles_needed)[:new_count]
                num_images = len(image_paths)
                # Пересчитываем
                total_overlap = fade_dur_images * (num_images - 1)
                budget = (duration + total_overlap) / num_images
                duration_per_image = max(min_per_slot, min(max_per_slot, budget))
        
        # Минимум для xfade offset (offset = duration - fade)
        if self.enable_fade and num_images > 1:
            duration_per_image = max(duration_per_image, fade_dur_images + 0.2)
        
        self.log(f"🎞️ Слайдшоу с эффектами: {num_images} картинок, {duration_per_image:.2f}с")
        if self.enable_kenburns:
            self.log(f"   📷 Ken Burns: zoom {self.kenburns_zoom}")
        if self.enable_fade:
            self.log(f"   🔀 Fade: {self.fade_duration}с (картинки: {fade_dur_images}с)")
        
        if self.media_per_item:
            scale_filters = [self.get_scale_filter_for(width, height, p) for p in image_paths]
        else:
            scale_filters = self.get_scale_filter_for(width, height)
        
        # === ТОЛЬКО FADE без Ken Burns: один проход через картинки ===
        if self.enable_fade and not self.enable_kenburns:
            return self._create_fade_slideshow_fast(image_paths, duration_per_image, fade_dur_images, temp_dir, ffmpeg_args, scale_filters, output_name)
        
        # === С KEN BURNS: создаём клипы только для уникальных картинок, повторные переиспользуем ===
        seen_norm = set()
        unique_ordered = []  # list of (norm_path, first_img_path)
        for p in image_paths:
            n = os.path.normpath(os.path.abspath(p))
            if n not in seen_norm:
                seen_norm.add(n)
                unique_ordered.append((n, p))
        num_unique = len(unique_ordered)
        image_videos = []
        self.log(f"   📷 Ken Burns: {num_unique} уникальных картинок → {num_images} слотов (повторы не рендерятся)")
        kb_cache = {}
        # Уникальный префикс клипов: чанки _process_timeline идут ПАРАЛЛЕЛЬНО по пулу —
        # фиксированное slide_NNNN.mp4 давало гонку (потоки перезаписывают файлы друг друга).
        # output_name уникален per-чанк (seg_timeline_NNNN / seg_img_batch_NNNN).
        _slide_base = (output_name or "slideshow").replace(".mp4", "").replace(".", "_")
        for idx, (norm_path, img_path) in enumerate(unique_ordered):
            output_video = os.path.join(temp_dir, f"{_slide_base}_slide_{idx:04d}.mp4")
            
            kb_filter = self.get_kenburns_filter(width, height, duration_per_image, direction='random', image_path=img_path, frames=_lock_frames)
            if self.vintage_effect:
                kb_filter = f"{kb_filter},{self.get_vintage_filter()}"

            try:
                cmd = [FFMPEG_EXE, "-loop", "1", "-i", img_path,
                       "-filter_complex", kb_filter]
                # frame-lock: -frames:v (совпадает с zoompan d=total_frames) вместо -t
                cmd.extend((["-frames:v", str(_lock_frames)] if _lock_frames else ["-t", str(duration_per_image)]))
                cmd.extend(self._get_encoder_params(ffmpeg_args))
                cmd.extend(["-profile:v", "high",
                           "-pix_fmt", "yuv420p", "-r", str(self.target_fps),
                           "-g", "50", "-bf", "2", "-vsync", "cfr",
                           "-y", output_video])
                self.run_ffmpeg_command(cmd)
            except Exception as e:
                self.log(f"   ⏭️ Пропуск битой картинки: {os.path.basename(img_path)}")
                continue
            
            if not os.path.exists(output_video):
                self.log(f"⚠️ Клип для {os.path.basename(img_path)} не создан, пропускаем")
                continue
            if os.path.getsize(output_video) < 1000:
                self.log(f"⚠️ Клип для {os.path.basename(img_path)} битый, пропускаем")
                continue
            kb_cache[norm_path] = output_video
            print_progress(idx + 1, num_unique, "Ken Burns клипов")
        
        # Собираем порядок слотов из кэша (повторные картинки — тот же клип)
        for p in image_paths:
            n = os.path.normpath(os.path.abspath(p))
            if n in kb_cache:
                image_videos.append(kb_cache[n])
            else:
                self.log(f"⚠️ Нет кэша для {p}, пропуск слота")
        
        if not image_videos:
            self.log("❌ Не создано ни одного клипа Ken Burns!")
            return None
        
        self.log(f"   ✅ Готово: {len(image_videos)} слотов из {num_unique} уникальных клипов")
        
        if len(image_videos) == 1:
            return image_videos[0]
        
        # Ken Burns без fade - быстрый concat
        if not self.enable_fade:
            concat_list = os.path.join(temp_dir, f"{_slide_base}_clips.txt")
            with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
                for v in image_videos:
                    escaped = _escape_concat_path(v)
                    f.write(f"file '{escaped}'\n")
            
            output = os.path.join(temp_dir, f"{_slide_base}_kb.mp4")
            cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list]
            cmd.extend(self._get_encoder_params(ffmpeg_args))
            cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr", "-y", output])
            self.run_ffmpeg_command(cmd)
            self.log(f"✅ Слайдшоу с Ken Burns готово")
            return output
        
        # Ken Burns + Fade: один xfade filter_complex
        return self._merge_clips_with_xfade(image_videos, duration_per_image, fade_dur_images, temp_dir, ffmpeg_args)
    
    def _create_fade_slideshow_fast(self, image_paths, duration_per_image, fade_dur, temp_dir, ffmpeg_args, scale_filter, output_name=None):
        """
        Создаёт слайдшоу с fade. output_name: при поштучной обработке — уникальное имя (избегает перезаписи).
        Использует адаптивный батчинг для больших проектов чтобы не перегружать RAM.
        """
        num_images = len(image_paths)
        # Используем размеры батча из настроек рендера
        if num_images > 1000:
            BATCH_SIZE = self.render_settings.get('batch_size_large', 15)
        elif num_images > 500:
            BATCH_SIZE = self.render_settings.get('batch_size_medium', 18)
        else:
            BATCH_SIZE = self.render_settings.get('batch_size_small', 20)
        
        if self.vintage_effect:
            scale_filter = f"{scale_filter},{self.get_vintage_filter()}"
        
        out_file = output_name if output_name else "slideshow_fade.mp4"
        # Маленький проект - один вызов
        if num_images <= BATCH_SIZE:
            self.log(f"   ⚡ Быстрый режим: {num_images} картинок за 1 вызов")
            return self._create_fade_batch(image_paths, duration_per_image, fade_dur, temp_dir, ffmpeg_args, scale_filter, out_file)
        
        # Большой проект - батчинг
        num_batches = (num_images + BATCH_SIZE - 1) // BATCH_SIZE
        self.log(f"   📦 Батчинг: {num_images} картинок → {num_batches} групп по ~{BATCH_SIZE}")
        
        batch_videos = []
        for batch_idx in range(num_batches):
            start = batch_idx * BATCH_SIZE
            end = min(start + BATCH_SIZE, num_images)
            batch_images = image_paths[start:end]
            
            batch_output = f"batch_{batch_idx:03d}.mp4"
            print_progress(batch_idx + 1, num_batches, "fade-групп")
            
            batch_video = self._create_fade_batch(batch_images, duration_per_image, fade_dur, temp_dir, ffmpeg_args, scale_filter, batch_output)
            batch_videos.append(batch_video)
        
        # Склеиваем батчи простым concat (без fade между ними - компромисс для RAM)
        self.log(f"   🔗 Склеивание {len(batch_videos)} сегментов...")
        concat_list = os.path.join(temp_dir, "batches.txt")
        with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
            for v in batch_videos:
                escaped = _escape_concat_path(v)
                f.write(f"file '{escaped}'\n")
        
        output = os.path.join(temp_dir, "slideshow_fade.mp4")
        cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list]
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr", "-y", output])
        self.run_ffmpeg_command(cmd)
        
        self.log(f"✅ Слайдшоу с fade готово ({num_batches} батчей)")
        return output
    
    def _create_fade_batch(self, image_paths, duration_per_image, fade_dur, temp_dir, ffmpeg_args, scale_filter, output_name):
        """Создаёт один батч fade слайдшоу. scale_filter: строка или список строк (по одному на картинку)."""
        num_images = len(image_paths)
        width, height = ffmpeg_args['size'].split('x')
        filter_parts = []
        fps_str = str(getattr(self, 'target_fps', 30))
        for i in range(num_images):
            sf = scale_filter[i] if isinstance(scale_filter, list) else scale_filter
            filter_parts.append(f"[{i}:v]{sf},fps=fps={fps_str}:round=near,setpts=PTS-STARTPTS,trim=duration={duration_per_image}[v{i}]")
        
        if num_images == 1:
            filter_parts.append(f"[v0]null[vout]")
        else:
            current = "v0"
            offset = duration_per_image - fade_dur
            for i in range(1, num_images):
                out_label = "vout" if i == num_images - 1 else f"x{i}"
                filter_parts.append(f"[{current}][v{i}]xfade=transition=fade:duration={fade_dur}:offset={offset}[{out_label}]")
                current = out_label
                offset += duration_per_image - fade_dur
        
        filter_complex = ";".join(filter_parts)
        
        cmd = [FFMPEG_EXE]
        for img_path in image_paths:
            cmd.extend(["-loop", "1", "-t", str(duration_per_image + 1), "-i", img_path])
        
        cmd.extend(["-filter_complex", filter_complex, "-map", "[vout]"])
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr",
                   "-y", os.path.join(temp_dir, output_name)])
        
        self.run_ffmpeg_command(cmd)
        return os.path.join(temp_dir, output_name)
    
    def _merge_clips_with_xfade(self, video_clips, duration_per_image, fade_dur, temp_dir, ffmpeg_args):
        """
        Объединяет видео клипы с xfade.
        Использует адаптивный батчинг для больших проектов.
        """
        num_clips = len(video_clips)
        # Используем размеры батча из настроек рендера
        if num_clips > 1000:
            BATCH_SIZE = self.render_settings.get('batch_size_large', 15)
        elif num_clips > 500:
            BATCH_SIZE = self.render_settings.get('batch_size_medium', 18)
        else:
            BATCH_SIZE = self.render_settings.get('batch_size_small', 20)
        
        # Маленький проект - один вызов
        if num_clips <= BATCH_SIZE:
            self.log(f"   ⚡ Быстрый xfade: {num_clips} клипов за 1 вызов")
            return self._merge_xfade_batch(video_clips, duration_per_image, fade_dur, temp_dir, ffmpeg_args, "slideshow_kb_fade.mp4")
        
        # Большой проект - батчинг
        num_batches = (num_clips + BATCH_SIZE - 1) // BATCH_SIZE
        self.log(f"   📦 Батчинг xfade: {num_clips} клипов → {num_batches} групп")
        
        batch_videos = []
        for batch_idx in range(num_batches):
            start = batch_idx * BATCH_SIZE
            end = min(start + BATCH_SIZE, num_clips)
            batch_clips = video_clips[start:end]
            
            batch_output = f"kb_batch_{batch_idx:03d}.mp4"
            self.log(f"   🔄 Группа {batch_idx + 1}/{num_batches}: клипы {start + 1}-{end}")
            
            batch_video = self._merge_xfade_batch(batch_clips, duration_per_image, fade_dur, temp_dir, ffmpeg_args, batch_output)
            batch_videos.append(batch_video)
        
        # Склеиваем батчи
        self.log(f"   🔗 Склеивание {len(batch_videos)} сегментов...")
        concat_list = os.path.join(temp_dir, "kb_batches.txt")
        with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
            for v in batch_videos:
                escaped = _escape_concat_path(v)
                f.write(f"file '{escaped}'\n")
        
        output = os.path.join(temp_dir, "slideshow_kb_fade.mp4")
        cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list]
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr", "-y", output])
        self.run_ffmpeg_command(cmd)
        
        self.log(f"✅ Слайдшоу с Ken Burns + fade готово ({num_batches} батчей)")
        return output
    
    def _concat_parts_with_xfade(self, all_parts, fade_dur, temp_dir, ffmpeg_args, output_path):
        """
        Склеивает все сегменты (видео + слайдшоу) с xfade между КАЖДОЙ парой.
        Fade применяется везде: видео↔видео, видео↔картинка, картинка↔картинка.
        При 20+ сегментах — батчинг (избегает Cannot allocate memory).
        """
        n_parts = len(all_parts)
        if n_parts < 2:
            if n_parts == 1:
                shutil.copy2(all_parts[0], output_path)
            return
        # Батчинг при 20+ сегментах — иначе FFmpeg падает с "Cannot allocate memory"
        XFADE_BATCH_SIZE = 20
        if n_parts > XFADE_BATCH_SIZE:
            batches = [all_parts[i:i + XFADE_BATCH_SIZE] for i in range(0, n_parts, XFADE_BATCH_SIZE)]
            # Имя батча: база output_path (уникальна между параллельными вызовами) + n_parts
            # (строго убывает на каждом уровне рекурсии → уровни не пересекаются). Без этого при
            # >400 сегментах рекурсия уровня 2 писала бы xfade_batch_000... поверх собственных входов.
            _uniq_b = os.path.splitext(os.path.basename(output_path))[0]
            batch_outputs = [os.path.join(temp_dir, f"{_uniq_b}_xfb{n_parts}_{k:03d}.mp4") for k in range(len(batches))]
            # ПАРАЛЛЕЛЬНО: батчи независимы (каждый — свой ffmpeg). Раньше шли по одному → главное
            # горлышко на многоядерных боксах. Порядок держим по индексу (batch_outputs[k]).
            try:
                xf_workers = int(self.render_settings.get('xfade_batch_workers',
                                                          self._get_unified_pool_workers(ffmpeg_args)))
            except Exception:
                xf_workers = 4
            xf_workers = max(1, min(len(batches), xf_workers))
            self.log(f"   📦 Xfade батчинг: {n_parts} сегментов → {len(batches)} групп по {XFADE_BATCH_SIZE} "
                     f"(параллельно ×{xf_workers})")
            if xf_workers > 1 and len(batches) > 1:
                with ThreadPoolExecutor(max_workers=xf_workers) as ex:
                    futs = {ex.submit(self._concat_parts_with_xfade, b, fade_dur, temp_dir, ffmpeg_args,
                                      batch_outputs[k]): k for k, b in enumerate(batches)}
                    for f in as_completed(futs):
                        f.result()  # пробрасываем исключение из любого батча
            else:
                for k, b in enumerate(batches):
                    self._concat_parts_with_xfade(b, fade_dur, temp_dir, ffmpeg_args, batch_outputs[k])
            if self.render_settings.get('fast_batch_stitch', True):
                self.log(f"   🔗 Быстрая склейка {len(batch_outputs)} групп (stream-copy, переходы ВНУТРИ групп сохранены, на 8 стыках — резкий рез)...")
                self._concat_streamcopy(batch_outputs, temp_dir, output_path)
            else:
                self.log(f"   🔗 Склеивание {len(batch_outputs)} групп с xfade...")
                self._concat_parts_with_xfade(batch_outputs, fade_dur, temp_dir, ffmpeg_args, output_path)
            return
        # Длительности для offset (параллельно при многих сегментах)
        if n_parts > 10:
            with ThreadPoolExecutor(max_workers=min(8, n_parts)) as ex:
                futures = {ex.submit(self.get_audio_duration, p, True): i for i, p in enumerate(all_parts)}
                durations = [0.0] * n_parts
                for f in as_completed(futures):
                    i = futures[f]
                    try:
                        d = f.result()
                        durations[i] = d if d > 0 else fade_dur * 2
                    except Exception:
                        durations[i] = fade_dur * 2
        else:
            durations = []
            for p in all_parts:
                d = self.get_audio_duration(p, use_cache=True)
                durations.append(d if d > 0 else fade_dur * 2)
        # Проверка: все клипы должны быть достаточно длинными для fade (минимум fade_duration_images)
        min_fade = max(fade_dur, getattr(self, 'fade_duration_images', fade_dur))
        if any(d < min_fade for d in durations):
            self.log(f"   ⚠️ Некоторые клипы короче fade ({min_fade}с), используем concat без переходов")
            _uniq = os.path.splitext(os.path.basename(output_path))[0]  # уникально на вызов → нет гонки при параллели
            concat_list = os.path.join(temp_dir, f"concat_xfade_fb_{_uniq}.txt")
            with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
                for p in all_parts:
                    escaped = _escape_concat_path(p)
                    f.write(f"file '{escaped}'\n")
            cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list]
            cmd.extend(self._get_encoder_params(ffmpeg_args))
            cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr", "-movflags", "+faststart", "-y", output_path])
            self.run_ffmpeg_command(cmd, processing_message="Склейка финального видео... Подождите")
            return
        # Округляем длительности до границ кадров (устраняет наложение/повтор кадров из-за погрешности)
        fps = self.target_fps
        durations = [round(d * fps) / fps for d in durations]
        
        # xfade цепочка: trim каждого входа до точной длительности — устраняет налезание/повтор кадров
        def _is_image_segment(path):
            return 'seg_img' in os.path.basename(path)
        filter_parts = []
        # Первый шаг: trim каждого входа до точной длительности
        for i in range(n_parts):
            filter_parts.append(f"[{i}:v]trim=duration={durations[i]:.6f},setpts=PTS-STARTPTS,fps=fps={fps}:round=near[v{i}]")
        cum_dur = durations[0]
        current = "v0"
        for i in range(1, len(all_parts)):
            fd = self.fade_duration_images if (_is_image_segment(all_parts[i - 1]) and _is_image_segment(all_parts[i])) else fade_dur
            offset = cum_dur - fd
            offset = max(0, offset)
            out_label = "[vout]" if i == len(all_parts) - 1 else f"[x{i}]"
            filter_parts.append(f"[{current}][v{i}]xfade=transition=fade:duration={fd}:offset={offset:.6f}{out_label}")
            cum_dur = cum_dur + durations[i] - fd
            current = f"x{i}" if i < len(all_parts) - 1 else "vout"
        filter_complex = ";".join(filter_parts)
        cmd = [FFMPEG_EXE]
        for p in all_parts:
            cmd.extend(["-i", p])
        # При 30+ сегментах — filter_complex_script (обход лимита длины командной строки на Windows)
        if n_parts > 30:
            _uniq2 = os.path.splitext(os.path.basename(output_path))[0]  # уникально на вызов → нет гонки при параллели
            script_path = os.path.join(temp_dir, f"xfade_filter_{_uniq2}.txt")
            with open(script_path, 'w', encoding='utf-8', errors='replace') as sf:
                sf.write(filter_complex)
            cmd.extend(["-filter_complex_script", script_path])
        else:
            cmd.extend(["-filter_complex", filter_complex])
        cmd.extend(["-map", "[vout]"])
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr", "-movflags", "+faststart", "-y", output_path])
        self.run_ffmpeg_command(cmd, processing_message="Склейка финального видео... Подождите")

    def _concat_streamcopy(self, parts, temp_dir, output_path):
        """Склейка готовых сегментов через concat demuxer БЕЗ пере-кодирования (-c copy).
        Переходы МЕЖДУ этими сегментами = резкий рез (внутри сегментов xfade уже впечён).
        Огромный выигрыш на CPU-боксах: НЕ пере-кодируем весь ролик в финале (была главная статья времени)."""
        if not parts:
            return
        if len(parts) == 1:
            shutil.copy2(parts[0], output_path)
            return
        _uniq = os.path.splitext(os.path.basename(output_path))[0]
        list_path = os.path.join(temp_dir, f"streamcopy_{_uniq}.txt")
        with open(list_path, 'w', encoding='utf-8', errors='replace') as f:
            for p in parts:
                f.write(f"file '{_escape_concat_path(p)}'\n")
        cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", list_path,
               "-c", "copy", "-movflags", "+faststart", "-y", output_path]
        self.run_ffmpeg_command(cmd, processing_message="Быстрая склейка групп (stream-copy)... Подождите")

    def _merge_xfade_batch(self, video_clips, duration_per_image, fade_dur, temp_dir, ffmpeg_args, output_name):
        """Объединяет один батч клипов с xfade. Trim до точной длительности устраняет наложение кадров."""
        num_clips = len(video_clips)
        
        if num_clips == 1:
            # Один клип - просто копируем
            output = os.path.join(temp_dir, output_name)
            shutil.copy(video_clips[0], output)
            return output
        
        fps = self.target_fps
        dur = round(duration_per_image * fps) / fps
        filter_parts = []
        for i in range(num_clips):
            filter_parts.append(f"[{i}:v]trim=duration={dur:.6f},setpts=PTS-STARTPTS,fps=fps={fps}:round=near[v{i}]")
        current = "v0"
        offset = dur - fade_dur
        for i in range(1, num_clips):
            out_label = "[vout]" if i == num_clips - 1 else f"[x{i}]"
            filter_parts.append(f"[{current}][v{i}]xfade=transition=fade:duration={fade_dur}:offset={offset:.6f}{out_label}")
            current = f"x{i}" if i < num_clips - 1 else "vout"
            offset += dur - fade_dur
        filter_complex = ";".join(filter_parts)
        
        cmd = [FFMPEG_EXE]
        for clip in video_clips:
            cmd.extend(["-i", clip])
        
        cmd.extend(["-filter_complex", filter_complex, "-map", "[vout]"])
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr",
                   "-y", os.path.join(temp_dir, output_name)])
        
        self.run_ffmpeg_command(cmd)
        return os.path.join(temp_dir, output_name)
    
    def _get_video_batch_size(self, ffmpeg_args):
        """NVENC с 2024: до 8 сессий. CPU: из render_settings.video_batch_size или авто."""
        if ffmpeg_args.get('encoder') in ('h264_nvenc', 'h264_videotoolbox') and self.gpu_available:
            return 4  # Было 2 — увеличиваем загрузку GPU
        v = self.render_settings.get('video_batch_size', 0)
        return int(v) if v and int(v) > 0 else 10

    def _get_unified_pool_workers(self, ffmpeg_args):
        """Количество параллельных FFmpeg. Из render_settings.max_pool_workers или авто."""
        custom = self.render_settings.get('max_pool_workers', 0)
        if custom and custom > 0:
            return int(custom)
        enc = ffmpeg_args.get('encoder')
        if enc in ('h264_nvenc', 'h264_videotoolbox') and self.gpu_available:
            return 6  # Было 2 — NVENC поддерживает до 8 сессий
        return min(12, max(4, _real_cpu_count()))

    def _process_single_video_batch(self, batch_start, batch, get_video_filter, temp_dir, ffmpeg_args, durations=None):
        """
        Обрабатывает один батч видео. Возвращает dict {global_idx: path} для успешных.
        durations: опциональные длительности для trim (когда timeline ограничивает длительность).
        """
        result = {}
        if not batch:
            return result
        durations = durations or [None] * len(batch)
        encoder_params = self._get_encoder_params(ffmpeg_args)
        target_fps = getattr(self, 'target_fps', 30)
        batch_timeout = min(self.render_settings.get('timeout_seconds', 14400), 600)
        use_hwaccel = self.gpu_available and ffmpeg_args.get('encoder') in ('h264_nvenc', 'h264_videotoolbox')
        unique_files = []
        file_to_input_idx = {}
        for vp in batch:
            norm = os.path.normpath(os.path.abspath(vp))
            if norm not in file_to_input_idx:
                file_to_input_idx[norm] = len(unique_files)
                unique_files.append(vp)
        inputs = []
        for uf in unique_files:
            if use_hwaccel:
                inputs.extend(["-hwaccel", "auto", "-i", uf])
            else:
                inputs.extend(["-i", uf])
        filter_parts = []
        for i, vp in enumerate(batch):
            norm = os.path.normpath(os.path.abspath(vp))
            input_idx = file_to_input_idx[norm]
            vf = get_video_filter(vp) if callable(get_video_filter) else get_video_filter
            d = durations[i] if i < len(durations) and durations[i] is not None else None
            trim_part = f"trim=duration={d:.6f}," if d is not None and d > 0 else ""
            filter_parts.append(f"[{input_idx}:v]{vf},{trim_part}fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS[v{i}]")
        n = len(batch)
        cmd = [FFMPEG_EXE] + inputs + ["-filter_complex", ";".join(filter_parts)]
        fps_opts = ["-r", str(target_fps), "-vsync", "cfr"]
        for i in range(n):
            out_path = os.path.join(temp_dir, f"seg_v_{batch_start + i:04d}.mp4")
            cmd.extend(["-map", f"[v{i}]"] + encoder_params + ["-pix_fmt", "yuv420p", "-an"] + fps_opts + ["-y", out_path])
        try:
            self.run_ffmpeg_command(cmd, timeout=batch_timeout)
            for i in range(n):
                out_path = os.path.join(temp_dir, f"seg_v_{batch_start + i:04d}.mp4")
                if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                    result[batch_start + i] = out_path
        except Exception as e:
            self.log(f"   ⚠️ Батч видео упал: {e}, fallback по одному")
            for i, vp in enumerate(batch):
                try:
                    out_path = os.path.join(temp_dir, f"seg_v_{batch_start + i:04d}.mp4")
                    vf = get_video_filter(vp) if callable(get_video_filter) else get_video_filter
                    d = durations[i] if i < len(durations) and durations[i] is not None else None
                    vf_extra = f"trim=duration={d:.6f}," if d is not None and d > 0 else ""
                    c = [FFMPEG_EXE, "-i", vp, "-vf", f"{vf},{vf_extra}fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS", "-an"] + encoder_params + ["-pix_fmt", "yuv420p", "-r", str(target_fps), "-vsync", "cfr", "-y", out_path]
                    self.run_ffmpeg_command(c, timeout=batch_timeout)
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                        result[batch_start + i] = out_path
                except Exception:
                    self.log(f"   ⏭️ Пропуск: {os.path.basename(vp)}")
        return result

    def _process_videos_batch_individual(self, video_paths, get_video_filter, temp_dir, ffmpeg_args, batch_size=None):
        """
        Обрабатывает много видео батчами: один FFmpeg вызов = batch_size выходных файлов (scale каждого входа).
        Возвращает список путей 1:1 с video_paths. Ускоряет в ~batch_size раз при чередовании (alternate).
        Дедупликация: один файл → один -i вход (важно при stock_video_cycle).
        """
        if not video_paths:
            return []
        batch_size = batch_size or self._get_video_batch_size(ffmpeg_args)
        results = [None] * len(video_paths)
        encoder_params = self._get_encoder_params(ffmpeg_args)
        target_fps = getattr(self, 'target_fps', 30)
        batch_timeout = min(self.render_settings.get('timeout_seconds', 14400), 600)
        use_hwaccel = self.gpu_available and ffmpeg_args.get('encoder') in ('h264_nvenc', 'h264_videotoolbox')
        for batch_start in range(0, len(video_paths), batch_size):
            batch = video_paths[batch_start:batch_start + batch_size]
            # Дедупликация: один файл → один -i вход
            unique_files = []
            file_to_input_idx = {}
            for vp in batch:
                norm = os.path.normpath(os.path.abspath(vp))
                if norm not in file_to_input_idx:
                    file_to_input_idx[norm] = len(unique_files)
                    unique_files.append(vp)
            inputs = []
            for uf in unique_files:
                if use_hwaccel:
                    inputs.extend(["-hwaccel", "auto", "-i", uf])
                else:
                    inputs.extend(["-i", uf])
            filter_parts = []
            for i, vp in enumerate(batch):
                norm = os.path.normpath(os.path.abspath(vp))
                input_idx = file_to_input_idx[norm]
                vf = get_video_filter(vp) if callable(get_video_filter) else get_video_filter
                filter_parts.append(f"[{input_idx}:v]{vf},fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS[v{i}]")
            n = len(batch)
            # Один FFmpeg: N входов -> N выходов (каждый -map [v{i}] -> отдельный файл)
            fps_opts = ["-r", str(target_fps), "-vsync", "cfr"]
            cmd = [FFMPEG_EXE] + inputs + ["-filter_complex", ";".join(filter_parts)]
            for i in range(n):
                out_path = os.path.join(temp_dir, f"seg_v_{batch_start + i:04d}.mp4")
                cmd.extend(["-map", f"[v{i}]"] + encoder_params + ["-pix_fmt", "yuv420p", "-an"] + fps_opts + ["-y", out_path])
            try:
                self.run_ffmpeg_command(cmd, timeout=batch_timeout)
                for i in range(n):
                    out_path = os.path.join(temp_dir, f"seg_v_{batch_start + i:04d}.mp4")
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                        results[batch_start + i] = out_path
            except Exception as e:
                self.log(f"   ⚠️ Батч упал: {e}, fallback по одному")
                for i, vp in enumerate(batch):
                    try:
                        out_path = os.path.join(temp_dir, f"seg_v_{batch_start + i:04d}.mp4")
                        vf = get_video_filter(vp) if callable(get_video_filter) else get_video_filter
                        c = [FFMPEG_EXE, "-i", vp, "-vf", f"{vf},fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS", "-an"] + encoder_params + ["-pix_fmt", "yuv420p", "-r", str(target_fps), "-vsync", "cfr", "-y", out_path]
                        self.run_ffmpeg_command(c, timeout=batch_timeout)
                        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                            results[batch_start + i] = out_path
                    except Exception as e2:
                        self.log(f"   ⏭️ Пропуск: {os.path.basename(vp)}")
            print_progress(min(batch_start + n, len(video_paths)), len(video_paths), "видео")
        return results  # Сохраняем 1:1 с video_paths (None = пропущен)
    
    def _process_videos_batch(self, video_paths, get_video_filter, temp_dir, ffmpeg_args, batch_size=None):
        """
        Обрабатывает много видео одним вызовом FFmpeg (scale+concat) вместо N перекодирований.
        При ошибке (битый файл в батче) — fallback на поштучную обработку с пропуском битых.
        Дедупликация: один файл → один -i вход (важно при stock_video_cycle).
        Нормализация FPS для устранения dup/drop кадров при concat.
        """
        if not video_paths:
            return []
        batch_size = batch_size or self._get_video_batch_size(ffmpeg_args)
        results = []
        target_fps = getattr(self, 'target_fps', 30)
        batch_timeout = min(self.render_settings.get('timeout_seconds', 14400), 600)
        use_hwaccel = self.gpu_available and ffmpeg_args.get('encoder') in ('h264_nvenc', 'h264_videotoolbox')
        for batch_start in range(0, len(video_paths), batch_size):
            batch = video_paths[batch_start:batch_start + batch_size]
            out_path = os.path.join(temp_dir, f"batch_v_{batch_start:04d}.mp4")
            # Дедупликация: один файл → один -i вход
            unique_files = []
            file_to_input_idx = {}
            for vp in batch:
                norm = os.path.normpath(os.path.abspath(vp))
                if norm not in file_to_input_idx:
                    file_to_input_idx[norm] = len(unique_files)
                    unique_files.append(vp)
            inputs = []
            for uf in unique_files:
                if use_hwaccel:
                    inputs.extend(["-hwaccel", "auto", "-i", uf])
                else:
                    inputs.extend(["-i", uf])
            filter_parts = []
            for i, vp in enumerate(batch):
                norm = os.path.normpath(os.path.abspath(vp))
                input_idx = file_to_input_idx[norm]
                vf = get_video_filter(vp) if callable(get_video_filter) else get_video_filter
                # fps:round=near — равномерное распределение кадров, меньше лагов
                # setpts=PTS-STARTPTS — сброс таймстемпов перед concat, устраняет повторения на стыках
                filter_parts.append(f"[{input_idx}:v]{vf},fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS[v{i}]")
            n = len(batch)
            concat_inputs = "".join(f"[v{i}]" for i in range(n))
            filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[outv]")
            filter_complex = ";".join(filter_parts)
            cmd = [FFMPEG_EXE] + inputs + ["-filter_complex", filter_complex, "-map", "[outv]", "-an"]
            cmd.extend(self._get_encoder_params(ffmpeg_args))
            cmd.extend(["-pix_fmt", "yuv420p", "-r", str(target_fps), "-vsync", "cfr", "-y", out_path])
            try:
                self.run_ffmpeg_command(cmd, timeout=batch_timeout)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                    results.append(out_path)
            except Exception as e:
                self.log(f"   ⚠️ Батч упал (возможно битый файл): {e}")
                self.log(f"   Fallback: обработка по одному, битые пропускаются")
                for vp in batch:
                    try:
                        processed = os.path.join(temp_dir, f"seg_v_{len(results):04d}.mp4")
                        vf = get_video_filter(vp) if callable(get_video_filter) else get_video_filter
                        c = [FFMPEG_EXE, "-i", vp, "-vf", f"{vf},fps=fps={target_fps}:round=near,setpts=PTS-STARTPTS", "-an"]
                        c.extend(self._get_encoder_params(ffmpeg_args))
                        c.extend(["-pix_fmt", "yuv420p", "-r", str(target_fps), "-vsync", "cfr", "-y", processed])
                        self.run_ffmpeg_command(c, timeout=batch_timeout)
                        if os.path.exists(processed) and os.path.getsize(processed) > 1024:
                            results.append(processed)
                    except Exception as e2:
                        self.log(f"   ⏭️ Пропуск битого видео: {os.path.basename(vp)}")
        return results
    
    def _interleave(self, list_a, list_b):
        """Чередует два списка: [a0, b0, a1, b1, ...]. Оставшиеся элементы дописываются в конец."""
        result = []
        i, j = 0, 0
        while i < len(list_a) or j < len(list_b):
            if i < len(list_a):
                result.append(list_a[i])
                i += 1
            if j < len(list_b):
                result.append(list_b[j])
                j += 1
        return result

    def _build_timeline(self, videos, images, safe_images, image_to_safe, all_media,
                        audio_duration, videos_set, images_set):
        """
        Строит единый таймлайн сегментов для ВСЕХ режимов material_order.
        Учитывает stock_video_limit (фильтрация ДО), stock_video_cycle (расширение ВНУТРИ).
        Возвращает: list[dict] — [{type:'video'|'image', source:path, duration:float, orig_path:path}, ...]
        """
        if not image_to_safe:
            image_to_safe = {img: sp for img, sp in zip(images, safe_images) if img and sp} if images and safe_images else {}
        
        def get_dur(p):
            return self.get_audio_duration(p, use_cache=True)
        
        # Предзаполнение кэша длительностей для видео (параллельно при 5+)
        unique_videos = list(videos_set)
        if len(unique_videos) >= 5:
            with ThreadPoolExecutor(max_workers=min(8, len(unique_videos))) as ex:
                list(ex.map(lambda v: self.get_audio_duration(v, use_cache=True), unique_videos))
        
        # Базовый порядок по material_order (только videos_set и images_set — уже отфильтрованы)
        videos_ordered = [p for p in all_media if p in videos_set]
        images_ordered = [p for p in all_media if p in images_set and p in image_to_safe]
        
        if self.material_order == 'alternate':
            ordered = self._interleave(videos_ordered, images_ordered)
        elif self.material_order == 'video_first':
            ordered = videos_ordered + images_ordered
        elif self.material_order == 'images_first':
            ordered = images_ordered + videos_ordered
        elif self.material_order == 'random':
            ordered = list(all_media)
            ordered = [p for p in ordered if p in videos_set or (p in images_set and p in image_to_safe)]
        elif self.material_order == 'manual':
            ordered = [p for p in all_media if p in videos_set or (p in images_set and p in image_to_safe)]
        else:  # alphabetical
            ordered = sorted(videos_ordered + images_ordered, key=lambda x: os.path.basename(x).lower())

        # NNN_parser.mp4 — не обычный stock, а материал конкретной сцены. Он
        # заменяет NNN-картинку и остаётся между соседними сценами при любом
        # material_order (в частности, не уезжает в начало при video_first).
        ordered = _place_scene_mapped_videos(ordered, videos_set, images_set)
        
        # НЕ расширяем timeline дублированием — это давало 287×28=8036 лишних рендеров.
        # Если материалов не хватает — зацикливание делаем в конце через stream_loop (без перерендера).
        
        # Формируем timeline: ВСЕ материалы по одному разу (повторение только через stream_loop когда всё использовано)
        num_images = sum(1 for p in ordered if p in images_set and p in image_to_safe)
        num_videos = sum(1 for p in ordered if p in videos_set)
        total_video_time = sum(get_dur(p) for p in ordered if p in videos_set)
        
        fade_dur_img = self.fade_duration_images if self.enable_fade else 0
        min_per_slot = self.min_image_duration + fade_dur_img
        max_per_slot = self.max_image_duration + fade_dur_img
        
        # Бюджеты: при наличии и видео и картинок — картинки заполняют ВСЁ время, не занятое видео.
        # (Раньше тут был кап min(num*min_dur, audio*0.6) → картинки душились до ~min даже при
        #  коротких видео, остаток аудио заполнялся зацикливанием. Теперь — видео своей длиной,
        #  картинки делят остаток аудио и растягиваются в пределах [min..max].)
        has_both = num_videos > 0 and num_images > 0
        if has_both:
            image_budget = max(num_images * 0.5, audio_duration - total_video_time)
            video_budget = total_video_time
        else:
            video_budget = 0 if num_videos == 0 else float('inf')
            image_budget = 0 if num_images == 0 else float('inf')
        
        # Длительность на картинку (в пределах бюджета; при нехватке — ниже min, но не менее 0.5с)
        # ФИКС: раньше при enable_fade бюджет раздувался на (n-1)*fade — «на перекрытия xfade».
        # Перекрытий МЕЖДУ КАРТИНКАМИ в этом пайплайне НЕТ: смежные картинки уходят пачкой в
        # _create_image_batch (concat-демуксер без xfade), а одиночная картинка перекрывать
        # нечего. Итог был Σ картинок = remaining + (n-1)*fade → хвост резался финальным
        # -t audio_duration (при 60 картинках и fade 0.5с последние 5 не показывались вообще).
        # Стык между ЧАСТЯМИ (их xfade реально съедает) компенсируется в _process_timeline.
        if num_images > 0:
            if has_both:
                budget = image_budget / num_images
            else:
                remaining = max(0, audio_duration - total_video_time)
                budget = remaining / num_images
            duration_per_image = max(0.5, min(max_per_slot, budget))
        else:
            duration_per_image = max_per_slot
        duration_per_image = max(duration_per_image, 0.5)
        
        # Видео идут своей реальной длиной; лишнее (если суммарно > аудио) отрежет финальный -t audio_duration
        max_video_dur = None
        
        timeline = []
        for p in ordered:
            if p in videos_set:
                d = get_dur(p)
                if d > 0:
                    if max_video_dur is not None:
                        d = min(d, max_video_dur)
                        d = max(d, 0.5)
                    timeline.append({'type': 'video', 'source': p, 'duration': d, 'orig_path': p})
            elif p in images_set and p in image_to_safe:
                timeline.append({'type': 'image', 'source': image_to_safe[p], 'duration': duration_per_image, 'orig_path': p})
        
        return timeline

    def _create_image_batch(self, image_paths, duration_per_image, temp_dir, ffmpeg_args, output_name, durations=None, frames=None):
        """Кодирует ПАЧКУ картинок одним ffmpeg (concat-demuxer) — без спавна на каждую.
        durations: список длительностей НА КАЖДУЮ картинку (синхрон по предложениям) — пишем
          per-image `duration` в concat-лист → точное попадание в предложения ОДНИМ проходом
          (×3 быстрее, чем энкод по картинке). Если None — все по duration_per_image (равномерно).
        frames: список кадров НА КАЖДУЮ картинку (frame-lock, фикс дрейфа синхрона) — если задан,
          длительности и -frames:v берутся с единой сетки кадров → нулевой остаток на границе пачки.
        Frame-exact через -frames:v (last-repeat без duration). Возвращает путь или None."""
        n = len(image_paths)
        if n == 0:
            return None
        width, height = ffmpeg_args['size'].split('x')
        video_filter = self.get_scale_filter_for(width, height)
        if self.vintage_effect:
            video_filter = f"{video_filter},{self.get_vintage_filter()}"
        fps = self.target_fps
        use_frames = bool(frames) and len(frames) == n
        if use_frames:
            # frame-lock: длительности = кадры/fps (frame-grid), -frames:v = Σ кадров (0 остатка)
            fr = [max(1, int(round(x))) for x in frames]
            durs = [f / float(fps) for f in fr]
            exact_frames = sum(fr)
        else:
            use_per = bool(durations) and len(durations) == n
            durs = [max(0.04, float(d)) for d in durations] if use_per else [float(duration_per_image)] * n
            exact_frames = sum(max(1, round(d * fps)) for d in durs)
        base = output_name.replace(".mp4", "").replace(".", "_")
        concat_list = os.path.join(temp_dir, f"{base}_list.txt")
        with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
            for p, d in zip(image_paths, durs):
                f.write(f"file '{_escape_concat_path(p)}'\n")
                f.write(f"duration {d}\n")
            # last-repeat без duration — даёт frame-exact выход с -frames:v
            f.write(f"file '{_escape_concat_path(image_paths[-1])}'\n")
        out = os.path.join(temp_dir, output_name)
        cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list, "-vf", video_filter]
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(fps), "-vsync", "cfr",
                    "-frames:v", str(exact_frames), "-y", out])
        try:
            self.run_ffmpeg_command(cmd)
        except Exception as e:
            print_debug(f"_create_image_batch ({n} картинок) исключение: {e}")
            return None
        return out if self._validate_fragment(out) else None

    def _create_xfade_batch(self, image_paths, frames, temp_dir, ffmpeg_args, output_name):
        """Кодирует ПАЧКУ картинок одним ffmpeg с НАСТОЯЩИМ кроссфейдом между соседними
        картинками (раньше смежные картинки шли concat-демуксером встык — переходов между
        ними не было вовсе, xfade случался только на стыках ЧАСТЕЙ, т.е. единицы раз на ролик).

        frames: плановые кадры на каждую картинку (frame-lock, единая сетка таймлайна).

        ТАЙМИНГ (главное). Каждый xfade съедает fdf кадров, поэтому входы удлиняются ровно
        на съедаемое, а длина ВЫХОДА остаётся = Σframes — пачка занимает на таймлайне ровно
        столько же, сколько занимала бы без переходов, дрейф не появляется:
            fdf   — длина перехода в кадрах, a = fdf//2, b = fdf - a  (a + b = fdf)
            L[0]   = f[0]  + b      первая картинка гаснет только справа
            L[i]   = f[i]  + fdf    средние гаснут с обеих сторон
            L[-1]  = f[-1] + a      последняя гаснет только слева
            offset_k = C_k - a,  где C_k = f[0]+…+f[k-1] — ПЛАНОВАЯ граница картинок
        Тогда ΣL − (n−1)·fdf = Σf (длина сходится), а k-й переход занимает выходные кадры
        [C_k − a, C_k + b) — то есть отцентрован на плановой границе (±0.5 кадра при нечётном
        fdf). Работает одинаково при равномерном распределении и при синхроне по предложениям:
        f[] уже несёт синканные длительности.

        Под Ken Burns вход = zoompan на L[i] кадров (по одному на картинку в том же
        filter_complex) — KB больше не теряется на пачках, как при concat-демуксере.
        Возвращает путь или None (тогда вызывающий откатывается на обычный батч)."""
        n = len(image_paths)
        if n == 0 or not frames or len(frames) != n:
            return None
        try:
            fps = float(self.target_fps)
            f = [max(1, int(round(x))) for x in frames]
        except Exception:
            return None
        if fps <= 0:
            return None
        total_frames = sum(f)
        fdf = int(round(float(self.fade_duration_images or 0) * fps))
        if n > 1:
            # переход не может быть длиннее самой короткой картинки пачки (иначе соседние
            # переходы наложились бы друг на друга и xfade поехал бы по offset'ам)
            fdf = min(fdf, min(f) - 1)
            if fdf < 2:
                print_debug(f"_create_xfade_batch: переход вырождается (fdf={fdf}) → обычный батч")
                return None
        a = fdf // 2
        b = fdf - a

        width, height = ffmpeg_args['size'].split('x')
        if self.media_per_item:
            scale_filters = [self.get_scale_filter_for(width, height, p) for p in image_paths]
        else:
            _sf = self.get_scale_filter_for(width, height)
            scale_filters = [_sf] * n
        # Винтаж — ПОСЛЕ loop (в нём noise=allf=t, шум обязан меняться по кадрам; применённый
        # до loop он размножился бы одним замороженным паттерном)
        _vint_after_loop = (f",{self.get_vintage_filter()}"
                            if (self.vintage_effect and not self.enable_kenburns) else "")

        # Длины входов в кадрах (см. формулу в docstring)
        if n == 1:
            L = [f[0]]
        else:
            L = [f[0] + b] + [f[i] + fdf for i in range(1, n - 1)] + [f[n - 1] + a]

        # Порядок фильтров важен: trim СБРАСЫВАЕТ метаданные частоты кадров (становится 1/0),
        # а xfade требует constant frame rate («The inputs needs to be a constant frame rate»)
        # → fps ставим ПОСЛЕ trim. Считать кадры trim'у это не мешает: вход уже на target_fps
        # (-framerate у картинки / fps= у zoompan).
        fps_s = str(self.target_fps)
        # xfade-цепочка — единственное место, где filter_complex реально тяжёлый. Дефолтная
        # эвристика ffmpeg разводит граф на столько потоков, сколько ядер, и на коротких
        # звеньях цепочки уходит в синхронизацию. Замер 03.08.2026 (40 фото 1080p, пачки
        # с переходами): -filter_complex_threads 4 = −3.6%, выход ПОБАЙТОВО тот же (md5 совпал).
        cmd = [FFMPEG_EXE, "-filter_complex_threads",
               str(self.render_settings.get('filter_complex_threads', 4))]
        filter_parts = []
        for i, p in enumerate(image_paths):
            if self.enable_kenburns:
                # одна входная картинка: zoompan сам размножает её на d кадров. Просим d=L+1,
                # а trim режет до L — иначе последний кадр упирается в EOF без известной
                # длительности и фильтр fps его выбрасывает (пачка выходила на кадр короче).
                cmd.extend(["-i", p])
                kb = self.get_kenburns_filter(width, height, (L[i] + 1) / fps, direction='random',
                                              image_path=p, frames=L[i] + 1)
                if self.vintage_effect:
                    kb = f"{kb},{self.get_vintage_filter()}"
                filter_parts.append(f"[{i}:v]{kb},trim=end_frame={L[i]},setpts=PTS-STARTPTS,"
                                    f"fps=fps={fps_s}:round=near[v{i}]")
            else:
                # ОДИН декод на картинку вместо L[i] (фикс цены переходов).
                # Было `-loop 1 -t …`: image2-демуксер перечитывал файл и ПЕРЕДЕКОДИРОВАЛ jpeg
                # на КАЖДЫЙ выходной кадр, а scale отрабатывал 30 раз в секунду вместо одного
                # раза на картинку — на этом уходило ~75% времени пачки. loop-фильтр повторяет
                # уже отмасштабированный кадр (тот же буфер, refcount) — цена нулевая.
                # Замер (10 фото 1080p, 1500 кадров): 14.7с → 6.1с, выход ПОБИТОВО тот же
                # (framemd5 совпал покадрово).
                cmd.extend(["-framerate", fps_s, "-i", p])
                filter_parts.append(f"[{i}:v]{scale_filters[i]},"
                                    f"loop=loop={L[i] + 1}:size=1:start=0{_vint_after_loop},"
                                    f"trim=end_frame={L[i]},"
                                    f"setpts=PTS-STARTPTS,fps=fps={fps_s}:round=near[v{i}]")

        if n == 1:
            filter_parts.append("[v0]null[vout]")
        else:
            cur = "v0"
            cum = 0
            for i in range(1, n):
                cum += f[i - 1]                      # C_i — плановая граница перед картинкой i
                off = max(0, cum - a)
                lbl = "vout" if i == n - 1 else f"x{i}"
                filter_parts.append(f"[{cur}][v{i}]xfade=transition=fade:"
                                    f"duration={fdf / fps:.6f}:offset={off / fps:.6f}[{lbl}]")
                cur = lbl

        filter_complex = ";".join(filter_parts)
        base = output_name.replace(".mp4", "").replace(".", "_")
        out = os.path.join(temp_dir, output_name)
        # Длинный filter_complex — через script (обход лимита длины командной строки на Windows)
        if len(filter_complex) > 3000:
            script_path = os.path.join(temp_dir, f"{base}_xfade.txt")
            with open(script_path, 'w', encoding='utf-8', errors='replace') as sf:
                sf.write(filter_complex)
            cmd.extend(["-filter_complex_script", script_path])
        else:
            cmd.extend(["-filter_complex", filter_complex])
        cmd.extend(["-map", "[vout]"])
        cmd.extend(self._get_encoder_params(ffmpeg_args))
        cmd.extend(["-pix_fmt", "yuv420p", "-r", fps_s, "-vsync", "cfr",
                    "-frames:v", str(total_frames), "-y", out])
        try:
            self.run_ffmpeg_command(cmd)
        except Exception as e:
            print_debug(f"_create_xfade_batch ({n} картинок) исключение: {e}")
            return None
        return out if self._validate_fragment(out) else None

    def _lock_frames_to_timeline(self, timeline):
        """FRAME-LOCK против накопления дрейфа синхрона (фикс 04.07).
        Проблема: секундная длительность каждой сцены квантовалась в кадры ПО-СЕГМЕНТНО
        (round в _create_image_batch на границе пачки, floor в Ken Burns) без сквозного
        якоря, а финал = stream-copy (нечему пере-таймить) → остаток копился, к концу ~0.5с.
        Решение: идём по таймлайну с running cum (сек), кадры сцены =
        round(cum_end*fps) − round(cum_start*fps). Σ = round(total*fps) ТОЧНО → дрейф не копится
        (мгновенно ±0.5 кадра, к концу 0). Ставим seg['frames'] и переписываем seg['duration']
        на frame-grid (frames/fps), чтобы ВСЕ пути (батч/KB/fast-path) читали согласованную сетку.
        cum несём и через video-сегменты (их кадры — из своего энкода), меняем только image —
        image-границы после видео держатся в пределах ±1 кадра (bounded, не копится)."""
        try:
            fps = float(self.target_fps)
            if fps <= 0:
                return timeline
        except Exception:
            return timeline
        cum = 0.0
        cum_frame = 0  # = round(cum*fps)
        for seg in timeline:
            try:
                d = max(0.0, float(seg.get('duration') or 0))
            except Exception:
                d = 0.0
            nxt = cum + d
            end_frame = round(nxt * fps)
            if seg.get('type') == 'image':
                f = end_frame - cum_frame
                if f <= 0:
                    # деградация (сцена < 1 кадра) — не молчим, не ломаем сумму: минимум 1 кадр,
                    # но такое почти невозможно (upstream кламп ≥0.5с = 15 кадров @30fps)
                    self.log(f"   ⚠️ frame-lock: сцена <1 кадра (d={d:.4f}с) — ставлю 1 кадр")
                    f = 1
                    end_frame = cum_frame + 1
                seg['frames'] = int(f)
                seg['duration'] = f / fps  # frame-grid: делает round() в батче точным (0 остатка)
            cum = nxt
            cum_frame = end_frame
        return timeline

    def _process_timeline(self, timeline, get_video_filter, temp_dir, ffmpeg_args):
        """
        Обрабатывает каждый сегмент таймлайна. Каждый сегмент = уникальный выходной файл (seg_timeline_NNNN.mp4).
        Индексный маппинг — дубликаты (cycle) не перетираются.
        """
        if not timeline:
            return []
        
        width, height = ffmpeg_args['size'].split('x')
        processed = [None] * len(timeline)
        video_indices = [i for i, seg in enumerate(timeline) if seg['type'] == 'video']
        image_indices = [i for i, seg in enumerate(timeline) if seg['type'] == 'image']
        
        batch_size = self._get_video_batch_size(ffmpeg_args) if len(video_indices) >= 3 else 1
        
        # Обработка видео: батчами. _process_single_video_batch возвращает {batch_start+i: path}
        # Маппим на timeline indices: batch_indices[i] -> timeline index
        for batch_start in range(0, len(video_indices), batch_size):
            batch_indices = video_indices[batch_start:batch_start + batch_size]
            batch_paths = [timeline[i]['source'] for i in batch_indices]
            batch_durations = [timeline[i]['duration'] for i in batch_indices]
            base_idx = batch_indices[0]
            
            idx_to_path = self._process_single_video_batch(
                base_idx, batch_paths, get_video_filter, temp_dir, ffmpeg_args, durations=batch_durations
            )
            for i, timeline_idx in enumerate(batch_indices):
                key = base_idx + i
                if key in idx_to_path:
                    out_path = idx_to_path[key]
                    new_path = os.path.join(temp_dir, f"seg_timeline_{timeline_idx:04d}.mp4")
                    if out_path != new_path:
                        try:
                            os.rename(out_path, new_path)
                        except OSError:
                            shutil.move(out_path, new_path)
                        processed[timeline_idx] = new_path
                    else:
                        processed[timeline_idx] = out_path
        
        # === КАРТИНКИ: батчинг — смежные картинки одной длительности кодируются ОДНИМ ffmpeg ===
        # (вместо спавна на каждую). Чанки параллелятся по пулу. Фолбэк — поштучно.
        img_workers = self._get_unified_pool_workers(ffmpeg_args)
        n_img = len(image_indices)
        # ПОТОЛОК ПУЛА РАЗВЯЗАН ПО ПУТЯМ (замер 03.08.2026, 40 фото 1080p, 20 ядер):
        #  • обычные пачки картинок: пул=1 → 11.20с, 4 → 11.67с, 8 → +9.6%, 16 → +10.5%.
        #    Один ffmpeg на пачке уже выбирает машину, лишние процессы только дерутся за ядра →
        #    потолок ФИКСИРОВАННЫЙ и НЕ зависит от RAM. Раньше он считался формулой safe_workers
        #    (ram*0.4/3), и на боксе с 64-128 ГБ она молча дала бы 8-17 воркеров, т.е. −10%
        #    скорости на ровном месте.
        #  • Ken Burns без переходов: batch_max=1, каждая картинка — свой ffmpeg с zoompan
        #    (фильтр однопоточный, машина недогружена) → там больше процессов ПОМОГАЕТ: 4 → 8 = −5.7%.
        if self.enable_kenburns and not self.enable_fade and n_img:
            # RAM-страховка: zoompan держит апскейленный кадр, считаем ~1.5 ГБ на процесс
            _kb_ram_cap = max(1, int((self._detect_pc_specs()["ram_gb"] * 0.4) / 1.5))
            img_workers = max(img_workers, min(8, _real_cpu_count(), _kb_ram_cap))
        else:
            img_workers = min(img_workers, _IMAGE_POOL_CAP)
        # Размер пачки: столько, чтобы было ≥ ~3 пачек на воркер (параллелизм), но ≤100
        batch_max = max(10, min(100, math.ceil(n_img / max(img_workers * 3, 1)))) if n_img else 1
        # KEN BURNS: пачка кодируется одним concat-проходом БЕЗ zoompan → KB молча терялся на
        # мультикартиночных чанках (статика). KB требует per-image zoompan (свой d=frames и
        # направление на КАЖДУЮ картинку) → под KB каждая картинка = отдельный чанк через
        # проверенный одиночный путь create_slideshow_with_transitions (frame-lock сохраняется:
        # exact_frames → -frames:v + zoompan d=frames).
        if self.enable_kenburns and not self.enable_fade and n_img:
            batch_max = 1
        elif self.enable_kenburns and self.enable_fade and n_img:
            # KB + fade: пачка идёт через _create_xfade_batch — per-image zoompan ВНУТРИ одного
            # filter_complex, то есть KB сохраняется и переходы между картинками настоящие.
            # Пачку режем: zoompan держит апскейленный кадр на КАЖДЫЙ вход (RAM).
            batch_max = max(1, min(batch_max, 6))
        # Строим чанки: смежные image-индексы с РАВНОЙ длительностью, размер ≤ batch_max
        chunks = []  # {'start': idx, 'indices': [...], 'paths': [...], 'dur': d}
        i = 0
        while i < len(timeline):
            if timeline[i]['type'] == 'image':
                # Группируем СМЕЖНЫЕ картинки в один проход НЕЗАВИСИМО от длительности —
                # per-image длительности уходят в concat-лист (синхрон по предложениям сохраняется),
                # пачка кодируется ОДНИМ ffmpeg вместо энкода-на-картинку (×3 на синхроне).
                run = []
                while (i < len(timeline) and timeline[i]['type'] == 'image'
                       and len(run) < batch_max):
                    run.append(i)
                    i += 1
                chunks.append({'start': run[0], 'indices': run,
                               'paths': [timeline[k]['source'] for k in run],
                               'durs': [timeline[k]['duration'] for k in run],
                               'frames': [timeline[k].get('frames') for k in run],  # frame-lock
                               'dur': timeline[run[0]]['duration'],
                               'exact': bool(timeline[run[0]].get('exact'))})
            else:
                i += 1

        # === КОМПЕНСАЦИЯ XFADE НА СТЫКАХ ЧАСТЕЙ ===
        # При enable_fade и НЕ fast_batch_stitch финал собирает _concat_parts_with_xfade:
        # на каждом стыке частей cum = cum + dur − fd, то есть ролик короче суммы частей на
        # (частей−1)*fd, и ничем это не компенсировалось → хвост уезжал под финальный -t.
        # Лечим на входе: на каждый стык добавляем ровно fd кадров — половину в конец левой
        # части, половину в начало правой. Тогда xfade съедает ровно добавку (длина сходится),
        # переход центрируется на плановой границе, а все прочие картинки остаются на сетке.
        # Добавка идёт и в секунды, и в кадры frame-lock (единая сетка сохраняется).
        # Видео-часть тянуть нельзя (готовый перекодированный клип) → её половину берёт соседняя
        # часть-картинки; стык видео↔видео остаётся некомпенсированным (как было).
        # fd берём self.fade_duration: в _concat_parts_with_xfade fade_duration_images
        # применяется только к именам с 'seg_img' (легаси-путь), наши части — seg_batch/seg_timeline.
        if (chunks and self.enable_fade
                and not self.render_settings.get('fast_batch_stitch', True)):
            try:
                _fd = float(self.fade_duration or 0)
                _fps = float(self.target_fps or 0)
            except Exception:
                _fd, _fps = 0.0, 0.0
            if _fd > 0 and _fps > 0:
                # Части в порядке timeline: каждое видео = своя часть (None), каждый чанк = часть
                _chunk_by_start = {ch['start']: ch for ch in chunks}
                _parts = []
                for k in range(len(timeline)):
                    if timeline[k]['type'] == 'video':
                        _parts.append(None)
                    elif k in _chunk_by_start:
                        _parts.append(_chunk_by_start[k])
                _add_f = max(1, int(round(_fd * _fps)))
                _n_comp = 0
                for _j in range(len(_parts) - 1):
                    _left, _right = _parts[_j], _parts[_j + 1]
                    if _left is None and _right is None:
                        continue  # стык видео↔видео — тянуть нечего
                    if _left is None:
                        _f_left, _f_right = 0, _add_f
                    elif _right is None:
                        _f_left, _f_right = _add_f, 0
                    else:
                        _f_left = _add_f // 2
                        _f_right = _add_f - _f_left
                    for _ch, _pos, _f in ((_left, -1, _f_left), (_right, 0, _f_right)):
                        if not _ch or not _f:
                            continue
                        _ch['durs'][_pos] = float(_ch['durs'][_pos]) + _f / _fps
                        if _ch['frames'] and _ch['frames'][_pos]:
                            _ch['frames'][_pos] = int(_ch['frames'][_pos]) + _f
                        if len(_ch['paths']) == 1:
                            _ch['dur'] = _ch['durs'][_pos]
                    _n_comp += 1
                if _n_comp:
                    self.log(f"   🔗 Компенсация xfade на стыках частей: +{_add_f / _fps:.3f}с × {_n_comp}")

        def _process_chunk(ch):
            start = ch['start']
            try:
                # ch['dur'] — авторитетная длительность из timeline (uniform-кламп уже применён
                # выше, либо точный синхрон). Передаём как exact_per_image, чтобы слайдшоу НЕ
                # переклампило слот — иначе ломается синхрон и offset'ы concat/xfade.
                # frame-lock: при синхроне (exact) — как было; под Ken Burns — ВСЕГДА (иначе
                # uniform-клип получил бы -t с клампом [min..max] слайдшоу и разъехался с
                # таймлайном; -frames:v = seg['frames'] держит сетку кадров в обоих режимах)
                _lock = (ch.get('exact') or self.enable_kenburns) and ch.get('frames') and all(x for x in ch['frames'])
                # === РЕАЛЬНЫЙ КРОССФЕЙД МЕЖДУ КАРТИНКАМИ ===
                # При enable_fade вся пачка собирается xfade-цепочкой в _create_xfade_batch.
                # Длина пачки при этом НЕ меняется (= Σ frames) → тайминг сохраняется в обоих
                # режимах склейки, а компенсация стыков ЧАСТЕЙ (выше) работает поверх, на уже
                # скорректированных ch['frames'].
                if self.enable_fade and ch.get('frames') and all(x for x in ch['frames']):
                    _xf = self._create_xfade_batch(ch['paths'], ch['frames'], temp_dir, ffmpeg_args,
                                                   f"seg_batch_{start:04d}.mp4")
                    if _xf and os.path.exists(_xf) and os.path.getsize(_xf) > 1024:
                        return (start, _xf)
                    self.log(f"   ⚠️ Кроссфейд-пачка @{start} не собралась → склейка встык")
                if len(ch['paths']) == 1:
                    out = self.create_slideshow_with_transitions(
                        ch['paths'], ch['dur'], temp_dir, ffmpeg_args,
                        output_name=f"seg_timeline_{start:04d}.mp4", exact_per_image=(ch['dur'] if ch.get('exact') else None),
                        exact_frames=(ch['frames'][0] if _lock else None))
                else:
                    out = self._create_image_batch(ch['paths'], ch['dur'], temp_dir, ffmpeg_args,
                                                   f"seg_batch_{start:04d}.mp4", durations=ch.get('durs'),
                                                   frames=(ch['frames'] if _lock else None))
                    if not out:
                        # ФОЛБЭК: по одной картинке → concat-copy в один чанк (per-image длительности!)
                        print_debug(f"Батч @{start} упал → фолбэк поштучно")
                        parts = []
                        for idx in ch['indices']:
                            _d_idx = timeline[idx]['duration']
                            p = self.create_slideshow_with_transitions(
                                [timeline[idx]['source']], _d_idx, temp_dir, ffmpeg_args,
                                output_name=f"seg_timeline_{idx:04d}.mp4", exact_per_image=(_d_idx if ch.get('exact') else None),
                                exact_frames=(timeline[idx].get('frames') if ((ch.get('exact') or self.enable_kenburns) and timeline[idx].get('frames')) else None))
                            if p and os.path.exists(p):
                                parts.append(p)
                        if len(parts) == 1:
                            out = parts[0]
                        elif len(parts) > 1:
                            cl = os.path.join(temp_dir, f"chunk_fb_{start}.txt")
                            with open(cl, 'w', encoding='utf-8', errors='replace') as f:
                                for pp in parts:
                                    f.write(f"file '{_escape_concat_path(pp)}'\n")
                            out = os.path.join(temp_dir, f"seg_batch_{start:04d}.mp4")
                            self.run_ffmpeg_command([FFMPEG_EXE, "-f", "concat", "-safe", "0",
                                                     "-i", cl, "-c", "copy", "-y", out])
                            if not self._validate_fragment(out):
                                out = None
                if out and os.path.exists(out) and os.path.getsize(out) > 1024:
                    return (start, out)
            except Exception as e:
                self.log(f"   ⚠️ Пачка картинок @{start} упала: {e}")
            return (start, None)

        chunk_results = {}
        if chunks:
            self.log(f"   ⚡ Батчинг {n_img} картинок → {len(chunks)} пачек ({img_workers} потоков, ≤{batch_max}/пачку)")
            if len(chunks) >= 2 and img_workers > 1:
                done = 0
                with ThreadPoolExecutor(max_workers=img_workers) as ex:
                    futures = {ex.submit(_process_chunk, ch): ch['start'] for ch in chunks}
                    for fut in as_completed(futures):
                        start, out = fut.result()
                        if out:
                            chunk_results[start] = out
                        done += 1
                        print_progress(done, len(chunks), "пачек")
            else:
                for ci, ch in enumerate(chunks):
                    start, out = _process_chunk(ch)
                    if out:
                        chunk_results[start] = out
                    print_progress(ci + 1, len(chunks), "пачек")

        # === Сборка в порядке timeline: видео по индексу, картинки по чанкам ===
        chunk_by_start = {ch['start']: ch for ch in chunks}
        result = []
        for i in range(len(timeline)):
            if timeline[i]['type'] == 'video':
                p = processed[i]
                if p and os.path.exists(p) and os.path.getsize(p) > 1024:
                    result.append(p)
            elif i in chunk_by_start:
                out = chunk_results.get(i)
                if out and os.path.exists(out) and os.path.getsize(out) > 1024:
                    result.append(out)
            # image-индексы не на старте чанка уже внутри пачки — пропускаем
        return result

    def _build_parts_by_segments(self, videos, images, safe_images, image_to_safe, all_media,
                                  remaining_time, total_video_time, audio_duration,
                                  get_video_filter, temp_dir, ffmpeg_args):
        """[LEGACY] Собирает all_parts в порядке alternate, alphabetical, manual или random. Используется как fallback."""
        if image_to_safe is None:
            image_to_safe = {img: sp for img, sp in zip(images, safe_images) if img and sp}
        segments = []
        if self.material_order == 'alternate':
            vi, ii = 0, 0
            while vi < len(videos) or ii < len(safe_images):
                if vi < len(videos):
                    segments.append(('video', videos[vi]))
                    vi += 1
                if ii < len(safe_images):
                    segments.append(('image', safe_images[ii]))
                    ii += 1
        else:
            for path in all_media:
                if path in videos:
                    segments.append(('video', path))
                elif path in images and path in image_to_safe:
                    segments.append(('image', image_to_safe[path]))
        
        # Предварительная batch-обработка видео + батчи картинок
        num_image_slots = sum(1 for t, _ in segments if t == 'image')
        fade_dur_img = self.fade_duration_images if self.enable_fade else 0
        # Картинка = видимое время (min..max) + длина перехода
        min_per_slot = self.min_image_duration + fade_dur_img
        max_per_slot = self.max_image_duration + fade_dur_img
        if num_image_slots > 0:
            # С учётом overlap: total = n*d - (n-1)*fade => d = (remaining + (n-1)*fade)/n
            budget_per_slot = (remaining_time + (num_image_slots - 1) * fade_dur_img) / num_image_slots if self.enable_fade else remaining_time / num_image_slots
            duration_per_image = max(min_per_slot, min(max_per_slot, budget_per_slot))
        else:
            duration_per_image = max_per_slot
        duration_per_image = max(duration_per_image, 0.5)
        
        # Собираем все runs (video/image) в порядке
        runs = []  # [(typ, paths), ...]
        seg_idx = 0
        while seg_idx < len(segments):
            typ = segments[seg_idx][0]
            paths = []
            while seg_idx < len(segments) and segments[seg_idx][0] == typ:
                paths.append(segments[seg_idx][1])
                seg_idx += 1
            runs.append((typ, paths))
        
        total_segments = len(segments)
        image_runs = [(i, r[1]) for i, r in enumerate(runs) if r[0] == 'image' and r[1]]
        video_path_to_processed = {}
        
        # Единый пул: видео-батчи + батчи картинок параллельно (NVENC: 2 потока, CPU: до 6)
        bs = self._get_video_batch_size(ffmpeg_args) if len(videos) >= 3 else 0
        video_batch_jobs = []
        if len(videos) >= 3:
            for batch_start in range(0, len(videos), bs):
                batch = videos[batch_start:batch_start + bs]
                video_batch_jobs.append(('video', batch_start, batch))
        
        def _process_image_run(args):
            run_idx, paths = args
            total_dur = len(paths) * duration_per_image
            unique_out = f"seg_img_batch_{run_idx:04d}.mp4"
            try:
                out = self.create_slideshow_with_transitions(
                    paths, total_dur, temp_dir, ffmpeg_args, output_name=unique_out
                )
                if out and os.path.exists(out) and os.path.getsize(out) > 1024:
                    return ('image', run_idx, out)
            except Exception as e:
                self.log(f"   ⚠️ Батч {len(paths)} картинок упал: {e}")
            return ('image', run_idx, None)
        
        def _run_video_batch(args):
            _, batch_start, batch = args
            res = self._process_single_video_batch(batch_start, batch, get_video_filter, temp_dir, ffmpeg_args)
            return ('video', res)
        
        all_jobs = []
        for j in video_batch_jobs:
            all_jobs.append(('video', j))
        for run_idx, paths in image_runs:
            all_jobs.append(('image', (run_idx, paths)))
        
        max_workers = self._get_unified_pool_workers(ffmpeg_args)
        use_unified = len(all_jobs) > 1 and (len(video_batch_jobs) > 0 or len(image_runs) > 1)
        
        if use_unified:
            total_jobs = len(all_jobs)
            self.log(f"   ⚡ Единый пул: {len(video_batch_jobs)} видео-батчей + {len(image_runs)} батчей картинок ({max_workers} потоков)")
            image_results = {}
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {}
                for typ, payload in all_jobs:
                    if typ == 'video':
                        f = ex.submit(_run_video_batch, payload)
                    else:
                        f = ex.submit(_process_image_run, payload)
                    futures[f] = typ
                done_count = 0
                for future in as_completed(futures):
                    typ = futures[future]
                    try:
                        result = future.result()
                        if typ == 'video':
                            _, idx_to_path = result
                            for idx, path in idx_to_path.items():
                                if idx < len(videos):
                                    video_path_to_processed[videos[idx]] = path
                        else:
                            _, run_idx, path = result
                            image_results[run_idx] = path
                    except Exception as e:
                        self.log(f"   ⚠️ Задача упала: {e}")
                    done_count += 1
                    print_progress(done_count, total_jobs, "задач")
        else:
            # Последовательный режим: сначала видео, потом картинки
            if len(videos) >= 3:
                self.log(f"   ⚡ Предварительная batch-обработка {len(videos)} видео")
                batch_results = self._process_videos_batch_individual(videos, get_video_filter, temp_dir, ffmpeg_args)
                for i, vp in enumerate(videos):
                    if i < len(batch_results):
                        video_path_to_processed[vp] = batch_results[i]
            image_results = {}
            img_workers = self._get_unified_pool_workers(ffmpeg_args)
            if len(image_runs) > 1 and img_workers > 1:
                self.log(f"   ⚡ Параллельная обработка {len(image_runs)} батчей картинок ({img_workers} потоков)")
                with ThreadPoolExecutor(max_workers=img_workers) as ex:
                    futures = {ex.submit(_process_image_run, (i, p)): i for i, p in image_runs}
                    for future in as_completed(futures):
                        _, run_idx, path = future.result()
                        image_results[run_idx] = path
                        print_progress(len(image_results), len(image_runs), "батчей картинок")
            else:
                for run_idx, paths in image_runs:
                    _, _, path = _process_image_run((run_idx, paths))
                    image_results[run_idx] = path
                    print_progress(len(image_results), len(image_runs), "батчей картинок")
        
        # Собираем all_parts в порядке runs
        all_parts = []
        for run_idx, (typ, paths) in enumerate(runs):
            if typ == 'video':
                if video_path_to_processed:
                    for vp in paths:
                        p = video_path_to_processed.get(vp)
                        if p and os.path.exists(p) and os.path.getsize(p) > 1024:
                            all_parts.append(p)
                        elif p is None:
                            self.log(f"   ⏭️ Пропущен (битое): {os.path.basename(vp)}")
                elif len(paths) >= 3:
                    batch_parts = self._process_videos_batch(paths, get_video_filter, temp_dir, ffmpeg_args)
                    all_parts.extend(batch_parts)
                else:
                    for vp in paths:
                        try:
                            processed = os.path.join(temp_dir, f"seg_v_{len(all_parts):04d}.mp4")
                            vf = get_video_filter(vp) if callable(get_video_filter) else get_video_filter
                            cmd = [FFMPEG_EXE, "-i", vp, "-vf", f"{vf},fps=fps={self.target_fps}:round=near", "-an"]
                            cmd.extend(self._get_encoder_params(ffmpeg_args))
                            cmd.extend(["-pix_fmt", "yuv420p", "-y", processed])
                            self.run_ffmpeg_command(cmd, show_processing_message=False)
                            if os.path.exists(processed) and os.path.getsize(processed) > 1024:
                                all_parts.append(processed)
                        except Exception as e:
                            self.log(f"   ⏭️ Пропуск битого видео: {os.path.basename(vp)}")
            else:
                p = image_results.get(run_idx)
                if p:
                    all_parts.append(p)
            print_progress(len(all_parts), total_segments, "сегментов")
        # Гарантируем отображение 100% перед финальной склейкой
        print_progress(len(all_parts), total_segments, "сегментов")
        return all_parts
    
    def _apply_random_metadata(self, output_file, temp_dir, has_audio):
        """
        Рандомизирует метаданные видео для уникальности каждого ролика.
        Использует stream copy (без перекодирования) — быстро.
        Изменяет: creation_time, handler_name, comment, encoder.
        """
        uid = uuid.uuid4().hex[:12]
        
        # Случайная дата создания за последние 365 дней
        now_ts = time.time()
        random_offset = random.randint(0, 365 * 24 * 3600)
        random_date = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now_ts - random_offset))
        
        # Случайный encoder-подобный тег
        fake_encoders = [
            f"Lavf{random.randint(58,61)}.{random.randint(10,99)}.{random.randint(100,999)}",
            f"HandBrake {random.randint(1,2)}.{random.randint(0,9)}.{random.randint(0,9)}",
            f"x264 core {random.randint(155,165)} r{random.randint(2900,3200)}",
            f"Adobe Premiere Pro {random.randint(22,26)}.{random.randint(0,9)}",
            f"DaVinci Resolve {random.randint(17,19)}.{random.randint(0,9)}",
        ]
        fake_encoder = random.choice(fake_encoders)
        
        randomized_output = os.path.join(temp_dir, f"randomized_{uid}.mp4")
        
        # Только метаданные — stream copy, без перекодирования (экономия 30-60 мин)
        cmd = [
            FFMPEG_EXE, "-i", output_file,
            "-c", "copy",
            "-map_metadata", "-1",
            "-metadata", f"creation_time={random_date}",
            "-metadata", f"handler_name={fake_encoder}",
            "-metadata", f"comment={hashlib.md5(uid.encode()).hexdigest()[:16]}",
            "-metadata:s:v", f"encoder={fake_encoder}",
            "-movflags", "+faststart",
            "-y", randomized_output
        ]
        
        self.run_ffmpeg_command(cmd)
        
        if os.path.exists(randomized_output) and os.path.getsize(randomized_output) > 1000:
            # Заменяем оригинальный файл
            try:
                os.replace(randomized_output, output_file)
                self.log(f"   ✅ Метаданные рандомизированы")
                self.log(f"   📅 Дата: {random_date}")
                self.log(f"   🔧 Encoder: {fake_encoder}")
            except Exception as e:
                self.log(f"   ⚠️ Не удалось заменить файл: {e}")
                output_file = randomized_output
        else:
            self.log(f"   ⚠️ Рандомизация не удалась, используем оригинал")
        
        return output_file
    
    def render_video(self):
        # Создаем временную директорию на том же диске, где находится проект
        # Это важно для Windows, чтобы избежать проблем с путями между разными дисками
        # Временная папка — ВНУТРИ проекта, а не в системном /tmp.
        #
        # Причина: при отмене задачи пуллер шлёт SIGTERM всей группе процессов
        # (render_puller._stop_process_group). У Python нет обработчика SIGTERM,
        # процесс умирает мгновенно, и блок finally ниже НЕ выполняется. Каталог
        # в /tmp оставался навсегда: /tmp не tmpfs и между ребутами не чистится.
        # Внутри project_dir его убирает ВТОРОЙ, независимый владелец — пуллер
        # делает rmtree(projdir) и в своём finally, и перед каждым клеймом того же
        # jid. Даже SIGKILL не оставит хвоста.
        #
        # Имя ОБЯЗАНО содержать «temp»: сборщики материалов (три os.walk по
        # project_dir) отсеивают папки по условию 'temp' not in d.lower(). Назови
        # иначе — и временные кадры image_*.jpg молча уедут в готовый ролик.
        #
        # Заодно снимается старая задача Windows-ветки (temp обязан лежать на том
        # же диске, что проект): внутри project_dir это выполняется само собой.
        temp_base = os.path.join(self.project_dir, "_render_temp")
        os.makedirs(temp_base, exist_ok=True)
        temp_dir = tempfile.mkdtemp(dir=temp_base, prefix="render_")
        self.log(f"📁 Временная директория: {temp_dir}")
        
        try:
            mode = "VINTAGE" if self.vintage_effect else "ОБЫЧНЫЙ"
            self.log("\n" + "="*70)
            self.log(f"🎬 РЕНДЕРИНГ v{__VERSION__} ({mode})")
            self.log(f"📁 {self.project_dir}")
            if self.enable_fade:
                self.log(f"🔀 Fade переходы: {self.fade_duration}с")
            if self.enable_kenburns:
                self.log(f"📷 Ken Burns: zoom {self.kenburns_zoom}")
            self.log("="*70)
            
            ffmpeg_args = self._get_ffmpeg_args()
            self.log(f"⚙️ {ffmpeg_args['encoder']}, GPU: {self.gpu_available}")

            # --- Тайминг фаз рендера ---
            _t_render = time.time()
            _phase_t = [time.time()]
            def _phase(name):
                now = time.time()
                print_user(f"⏱ {name}: {now - _phase_t[0]:.1f}с", "INFO")
                _phase_t[0] = now

            # ШАГ 1: АУДИО
            print_progress(1, 4, "этапов рендера")
            self.log("\n--- 🎵 ШАГ 1/4: Аудио ---")
            
            audio_files = []
            # Нормализуем путь проекта
            project_dir_normalized = os.path.normpath(os.path.abspath(self.project_dir))
            
            self.log(f"📁 Директория проекта: {project_dir_normalized}")
            self.log(f"   Существует: {os.path.exists(project_dir_normalized)}")
            
            # Ищем аудио файлы рекурсивно по всей папке проекта
            self.log(f"🔍 Ищу аудио файлы рекурсивно по всей папке проекта...")
            
            # Служебные папки для исключения
            assets_dir = os.path.join(project_dir_normalized, "_ASSETS")
            assets_dir_normalized = os.path.normpath(assets_dir)
            
            # Поддерживаемые форматы аудио
            SUPPORTED_AUDIO_FORMATS = ['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac']
            
            try:
                for root, dirs, files in os.walk(project_dir_normalized):
                    # Пропускаем служебные папки
                    root_normalized = os.path.normpath(root)
                    if root_normalized == assets_dir_normalized:
                        dirs[:] = []  # Не заходим в _ASSETS
                        continue
                    
                    # Исключаем служебные папки из дальнейшего обхода
                    dirs[:] = [d for d in dirs if not d.startswith('.')
                               and 'temp' not in d.lower() and 'tmp' not in d.lower()
                               and d != '_ASSETS']
                    
                    for f in files:
                        # `tmp` наравне с `temp`: ускорение озвучки пишет
                        # `<имя>.spd.tmp.mp3`, и осиротевший файл приклеивался к дорожке
                        _fl = f.lower()
                        if (f.startswith('.') or 'temp' in _fl or 'tmp' in _fl
                                or 'final_video' in _fl):
                            continue
                        ext = os.path.splitext(f)[1].lower()
                        if ext in SUPPORTED_AUDIO_FORMATS:
                            full = os.path.normpath(os.path.join(root, f))
                            # Проверяем существование файла
                            if os.path.exists(full) and os.path.isfile(full):
                                file_size = os.path.getsize(full)
                                if file_size > 0:
                                    # Проверяем, что это не фоновое аудио
                                    skip = False
                                    if self.background_audio_file and os.path.exists(self.background_audio_file):
                                        try:
                                            if os.path.samefile(full, self.background_audio_file):
                                                skip = True
                                        except OSError:
                                            # Если samefile не работает (разные диски), сравниваем пути
                                            if os.path.normpath(full) == os.path.normpath(self.background_audio_file):
                                                skip = True
                                    
                                    # Проверяем, что это не трек в конце
                                    if not skip and self.end_track_file and os.path.exists(self.end_track_file):
                                        try:
                                            if os.path.samefile(full, self.end_track_file):
                                                skip = True
                                        except OSError:
                                            if os.path.normpath(full) == os.path.normpath(self.end_track_file):
                                                skip = True
                                    
                                    if not skip:
                                        audio_files.append(full)
                                        rel_path = os.path.relpath(full, project_dir_normalized)
                                        self.log(f"   ✅ Найден: {rel_path} ({file_size} байт)")
                                else:
                                    rel_path = os.path.relpath(full, project_dir_normalized)
                                    self.log(f"   ⚠️ Пропущен (пустой): {rel_path}")
            except (OSError, PermissionError) as e:
                self.log(f"   ❌ Ошибка при поиске аудио: {e}")
            
            # Сортируем по номеру в имени файла
            def get_file_number(path):
                basename = os.path.basename(path)
                numbers = re.findall(r'\d+', basename)
                if numbers:
                    try:
                        return int(numbers[0])
                    except ValueError:
                        return 0
                return 0
            
            audio_files.sort(key=get_file_number)
            
            audio_duration = 0.0
            final_audio = os.path.join(temp_dir, "final_audio.mp3")
            
            if audio_files:
                self.log(f"✅ Найдено: {len(audio_files)} аудио")
                
                safe_audio = []
                self.log(f"📋 Начинаю копирование {len(audio_files)} аудио файлов...")
                
                for i, af in enumerate(audio_files, 1):
                    print_progress(i, len(audio_files), "аудио файлов")
                    # Нормализуем путь к исходному файлу
                    af_normalized = os.path.normpath(os.path.abspath(af))
                    
                    # Проверяем существование файла перед копированием
                    if not os.path.exists(af_normalized):
                        self.log(f"❌ ОШИБКА [{i}/{len(audio_files)}]: Файл не найден: {af_normalized}")
                        # Пробуем найти файл с разными вариантами пути
                        alt_paths = [
                            af,
                            os.path.abspath(af),
                            os.path.realpath(af) if hasattr(os.path, 'realpath') else af,
                        ]
                        found = False
                        for alt_path in alt_paths:
                            if os.path.exists(alt_path):
                                af_normalized = os.path.normpath(alt_path)
                                self.log(f"   ✅ Найден альтернативный путь: {af_normalized}")
                                found = True
                                break
                        if not found:
                            continue
                    
                    if not os.path.isfile(af_normalized):
                        self.log(f"❌ ОШИБКА [{i}/{len(audio_files)}]: Путь не является файлом: {af_normalized}")
                        continue
                    
                    file_size = os.path.getsize(af_normalized)
                    if file_size == 0:
                        self.log(f"⚠️ [{i}/{len(audio_files)}]: Файл пуст, пропускаем: {os.path.basename(af_normalized)}")
                        continue
                    
                    safe_name = f"audio_{i:04d}.mp3"
                    safe_path = os.path.normpath(os.path.join(temp_dir, safe_name))
                    
                    try:
                        self.log(f"   📋 [{i}/{len(audio_files)}] Копирую: {os.path.basename(af_normalized)} ({file_size} байт)...")
                        
                        # Копируем файл с обработкой ошибок
                        shutil.copy2(af_normalized, safe_path)
                        
                        # Проверяем, что файл скопирован
                        if os.path.exists(safe_path):
                            copied_size = os.path.getsize(safe_path)
                            if copied_size > 0:
                                if copied_size == file_size:
                                    safe_audio.append(safe_path)
                                    self.log(f"   ✅ [{i}/{len(audio_files)}] Скопирован: {safe_name} ({copied_size} байт)")
                                else:
                                    self.log(f"   ⚠️ [{i}/{len(audio_files)}] Размер не совпадает: {copied_size} != {file_size}")
                                    # Пробуем использовать файл, если он не пустой
                                    if copied_size > 0:
                                        safe_audio.append(safe_path)
                            else:
                                self.log(f"   ❌ [{i}/{len(audio_files)}] Файл скопирован, но пуст: {safe_path}")
                                try:
                                    os.remove(safe_path)
                                except:
                                    pass
                        else:
                            self.log(f"   ❌ [{i}/{len(audio_files)}] Файл не создан после копирования: {safe_path}")
                    except (OSError, IOError, PermissionError, shutil.Error) as e:
                        self.log(f"   ❌ [{i}/{len(audio_files)}] ОШИБКА копирования: {e}")
                        self.log(f"      Исходный файл: {af_normalized}")
                        self.log(f"      Целевой файл: {safe_path}")
                        import traceback
                        self.log(f"      Детали: {traceback.format_exc()[:300]}")
                        continue
                
                self.log(f"📊 Итого скопировано: {len(safe_audio)}/{len(audio_files)} файлов")
                
                if not safe_audio:
                    error_msg = f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось скопировать ни одного аудио файла из {len(audio_files)}!"
                    self.log(f"❌ {error_msg}")
                    self.log(f"   Проверьте:")
                    self.log(f"   - Существуют ли файлы: {audio_files[:3]}...")
                    self.log(f"   - Права доступа к файлам")
                    self.log(f"   - Свободное место на диске")
                    raise Exception(error_msg)
                
                concat_list = os.path.join(temp_dir, "audio_concat.txt")
                self.log(f"📝 Создаю concat файл: {concat_list}")
                
                # Проверяем, что все файлы существуют перед созданием concat списка
                missing_files = []
                for af in safe_audio:
                    if not os.path.exists(af):
                        missing_files.append(af)
                
                if missing_files:
                    self.log(f"❌ ОШИБКА: Некоторые файлы не найдены после копирования:")
                    for mf in missing_files:
                        self.log(f"   - {mf}")
                    raise Exception(f"Файлы не найдены: {missing_files}")
                
                try:
                    # Создаем список строк для concat файла
                    concat_lines = []
                    
                    self.log(f"   📋 Начинаю формирование concat файла из {len(safe_audio)} файлов...")
                    self.log(f"   📁 Файлы для обработки:")
                    for i, af in enumerate(safe_audio[:5], 1):
                        exists = os.path.exists(af)
                        size = os.path.getsize(af) if exists else 0
                        self.log(f"      {i}. {af} - существует: {exists}, размер: {size} байт")
                    if len(safe_audio) > 5:
                        self.log(f"      ... и еще {len(safe_audio) - 5} файлов")
                    
                    # Подготовка файла паузы (если нужно)
                    pause_path = None
                    if self.pause_interval > 0 and self.pause_duration > 0:
                        pause_file = os.path.normpath(os.path.join(temp_dir, "pause.mp3"))
                        self.log(f"   Создаю файл паузы: {pause_file}")
                        
                        # Проверяем, что temp_dir существует
                        if not os.path.exists(temp_dir):
                            os.makedirs(temp_dir, exist_ok=True)
                        
                        self.run_ffmpeg_command([
                            FFMPEG_EXE, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                            "-t", str(self.pause_duration), "-c:a", "libmp3lame", "-b:a", "128k", "-y", pause_file
                        ])
                        
                        # Проверяем, что pause файл создан
                        if not os.path.exists(pause_file):
                            self.log(f"⚠️ Файл паузы не создан, пропускаем паузы")
                        else:
                            pause_abs = os.path.abspath(pause_file)
                            pause_path = pause_abs.replace('\\', '/')
                            self.log(f"   ✅ Файл паузы создан: {pause_path}")
                    
                    # Обрабатываем файлы (общий блок для всех случаев)
                    for i, af in enumerate(safe_audio, 1):
                        self.log(f"   🔍 Обрабатываю файл {i}/{len(safe_audio)}: {af}")
                                
                        # Проверяем, что файл существует перед записью в concat
                        if not os.path.exists(af):
                            self.log(f"      ❌ Файл не найден, пропускаем: {af}")
                            continue
                        
                        file_size = os.path.getsize(af)
                        if file_size == 0:
                            self.log(f"      ⚠️ Файл пуст, пропускаем: {af}")
                            continue
                        dur = self.get_audio_duration(af, use_cache=True)
                        if dur <= 0:
                            self.log(f"      ⚠️ Битый/пустой аудио (duration=0), пропускаем: {os.path.basename(af)}")
                            continue
                        
                        self.log(f"      ✅ Файл существует, размер: {file_size} байт")
                        
                        # Получаем реальный путь к файлу используя pathlib для надежности
                        from pathlib import Path as PathLib
                        
                        try:
                            # Используем pathlib для получения абсолютного пути
                            file_path_obj = PathLib(af).resolve()
                            normalized_win = str(file_path_obj)
                            
                            self.log(f"      Pathlib resolve: {normalized_win}")
                            
                            # Проверяем, что путь правильный
                            if not file_path_obj.exists():
                                self.log(f"      ❌ Путь не найден через pathlib, пропускаем: {normalized_win}")
                                continue
                            
                            # Для FFmpeg concat нужно использовать прямые слеши
                            # Преобразуем путь в формат для FFmpeg (используем as_posix)
                            normalized_path = file_path_obj.as_posix()
                            
                            self.log(f"      Для FFmpeg: {normalized_path}")
                            
                            # Проверяем, что путь не содержит проблемных символов
                            if "'" in normalized_path:
                                # Экранируем одинарные кавычки в пути
                                normalized_path = normalized_path.replace("'", "'\\''")
                                self.log(f"      Экранированы кавычки: {normalized_path}")
                            
                            # Формируем строку для concat файла
                            # Используем простой формат без лишних манипуляций
                            line = f"file '{normalized_path}'\n"
                            
                            # Проверяем, что строка формируется правильно
                            if len(line) < 10 or not line.startswith("file '"):
                                self.log(f"      ❌ Неправильный формат строки, пропускаем")
                                self.log(f"         Строка: {repr(line)}")
                                continue
                            
                            # Проверяем на известные искажения в строке
                            if 'Loca1' in line or 'Iemp' in line or 'aud1o' in line:
                                self.log(f"      ❌ Строка содержит искаженные символы, пропускаем!")
                                self.log(f"         Строка: {repr(line)}")
                                continue
                            
                            concat_lines.append(line)
                            self.log(f"      ✅ Добавлен в concat (всего: {len(concat_lines)})")
                            
                            # Добавляем паузу если нужно
                            if self.pause_interval > 0 and self.pause_duration > 0 and pause_path and i % self.pause_interval == 0 and i < len(safe_audio):
                                pause_line = f"file '{pause_path}'\n"
                                concat_lines.append(pause_line)
                                self.log(f"      Добавлена пауза после файла {i}")
                        except Exception as e:
                            self.log(f"      ❌ Ошибка обработки пути {af}: {e}")
                            import traceback
                            self.log(f"         Детали: {traceback.format_exc()[:300]}")
                            continue
                    
                    self.log(f"   📊 Итого подготовлено строк для concat: {len(concat_lines)}")
                    
                    if not concat_lines:
                        error_msg = f"КРИТИЧЕСКАЯ ОШИБКА: Нет файлов для записи в concat файл!"
                        self.log(f"   ❌ {error_msg}")
                        self.log(f"   Проверьте:")
                        self.log(f"   - Количество скопированных файлов: {len(safe_audio)}")
                        self.log(f"   - Существуют ли файлы:")
                        for i, af in enumerate(safe_audio[:5], 1):
                            exists = os.path.exists(af)
                            size = os.path.getsize(af) if exists else 0
                            self.log(f"      {i}. {af} - существует: {exists}, размер: {size}")
                        if len(safe_audio) > 5:
                            self.log(f"      ... и еще {len(safe_audio) - 5} файлов")
                        raise Exception(error_msg)
                    
                    # Проверяем строки перед записью на наличие искажений
                    self.log(f"   🔍 Проверка строк перед записью ({len(concat_lines)} строк):")
                    suspicious_lines = []
                    for i, line in enumerate(concat_lines, 1):
                        line_stripped = line.rstrip()
                        # Проверяем на известные искажения
                        if 'Loca1' in line_stripped or 'Iemp' in line_stripped or 'aud1o' in line_stripped:
                            suspicious_lines.append((i, line_stripped))
                            self.log(f"      ⚠️ Строка {i} содержит подозрительные символы: {repr(line_stripped)}")
                            # Показываем байтовое представление
                            try:
                                line_bytes = line_stripped.encode('utf-8')
                                self.log(f"         Байты: {line_bytes[:200]}")
                            except:
                                pass
                        
                        # Проверяем, что путь извлекается правильно из строки
                        if line_stripped.startswith("file '"):
                            path_start = line_stripped.find("'") + 1
                            path_end = line_stripped.rfind("'")
                            if path_start > 0 and path_end > path_start:
                                extracted_path = line_stripped[path_start:path_end]
                                extracted_path = extracted_path.replace("'\\''", "'")
                                extracted_path_win = extracted_path.replace('/', '\\') if os.name == 'nt' else extracted_path
                                
                                # Проверяем существование файла
                                if not os.path.exists(extracted_path_win):
                                    self.log(f"      ⚠️ Строка {i}: путь не найден: {extracted_path_win}")
                                    self.log(f"         Полная строка: {repr(line_stripped)}")
                                    # Показываем байтовое представление строки
                                    try:
                                        line_bytes = line_stripped.encode('utf-8')
                                        self.log(f"         Байты строки: {line_bytes[:200]}")
                                    except:
                                        pass
                    
                    if suspicious_lines:
                        self.log(f"   ❌ КРИТИЧЕСКАЯ ОШИБКА: Обнаружены подозрительные строки ПЕРЕД записью в файл!")
                        self.log(f"      Это означает, что искажение происходит при формировании строк, а не при записи.")
                        for line_num, line_content in suspicious_lines:
                            self.log(f"      Строка {line_num}: {repr(line_content)}")
                        raise Exception(f"Строки искажены ДО записи в файл. Проверьте формирование путей. Проблемные строки: {len(suspicious_lines)}")
                    
                    # Записываем файл простым способом (как video_concat)
                    # Используем текстовый режим с UTF-8
                    try:
                        with open(concat_list, 'w', encoding='utf-8', newline='\n') as f:
                            for line in concat_lines:
                                # Записываем строку как есть
                                f.write(line)
                                # Проверяем, что строка записалась правильно (для отладки)
                                # Но не делаем это для каждой строки, чтобы не замедлять
                    except Exception as e:
                        raise Exception(f"Ошибка записи concat файла: {e}")
                    
                    # Проверяем, что файл записан правильно
                    # Читаем файл обратно и сравниваем с оригиналом
                    try:
                        with open(concat_list, 'r', encoding='utf-8', errors='replace') as f:
                            read_lines = f.readlines()
                        
                        self.log(f"   📖 Прочитано строк из файла: {len(read_lines)}")
                        self.log(f"   📝 Записано строк: {len(concat_lines)}")
                        
                        # Сравниваем записанные и прочитанные строки
                        verification_errors = []
                        for line_num, (original_line, read_line) in enumerate(zip(concat_lines, read_lines), 1):
                            original_stripped = original_line.rstrip()
                            read_stripped = read_line.rstrip()
                            
                            # Проверяем, что строки совпадают
                            if original_stripped != read_stripped:
                                self.log(f"      ⚠️ Строка {line_num} изменилась при записи:")
                                self.log(f"         Оригинал: {repr(original_stripped)}")
                                self.log(f"         Прочитано: {repr(read_stripped)}")
                            
                            # Проверяем путь из прочитанной строки
                            if read_stripped.startswith("file '"):
                                path_start = read_stripped.find("'") + 1
                                path_end = read_stripped.rfind("'")
                                if path_start > 0 and path_end > path_start:
                                    file_path = read_stripped[path_start:path_end]
                                    file_path = file_path.replace("'\\''", "'")
                                    file_path_win = file_path.replace('/', '\\') if os.name == 'nt' else file_path
                                    
                                    # Проверяем существование файла
                                    if not os.path.exists(file_path_win):
                                        verification_errors.append((line_num, file_path_win, read_stripped))
                                        
                                        # Пробуем найти файл с разными вариантами пути
                                        from pathlib import Path as PathLib
                                        alt_paths = [
                                            file_path_win,
                                            os.path.normpath(file_path_win),
                                            os.path.abspath(file_path_win),
                                        ]
                                        
                                        # Пробуем найти через pathlib
                                        try:
                                            alt_paths.append(str(PathLib(file_path_win).resolve()))
                                        except:
                                            pass
                                        
                                        found = False
                                        for alt_path in alt_paths:
                                            if os.path.exists(alt_path):
                                                self.log(f"      ✅ Найден альтернативный путь: {alt_path}")
                                                found = True
                                                break
                                        
                                        if not found:
                                            self.log(f"      ❌ Файл не найден ни по одному пути")
                                            # Показываем байтовое представление для отладки
                                            try:
                                                path_bytes = file_path_win.encode('utf-8')
                                                self.log(f"      Байты пути: {repr(path_bytes)}")
                                            except:
                                                pass
                        
                        if verification_errors:
                            self.log(f"   ❌ ОШИБКА: После записи concat файла некоторые файлы не найдены:")
                            for line_num, missing_path, original_line in verification_errors:
                                self.log(f"      Строка {line_num}: {missing_path}")
                                self.log(f"      Оригинальная строка: {original_line}")
                            
                            # Показываем полное содержимое файла для отладки
                            self.log(f"   📄 Полное содержимое concat файла (repr):")
                            with open(concat_list, 'r', encoding='utf-8', errors='replace') as f:
                                for i, line in enumerate(f, 1):
                                    self.log(f"      {i}: {repr(line)}")
                            
                            # Показываем байтовое содержимое файла
                            self.log(f"   📄 Байтовое содержимое concat файла:")
                            with open(concat_list, 'rb') as f:
                                content = f.read()
                                self.log(f"      Размер: {len(content)} байт")
                                # Показываем первые 500 байт (преобразуем в строку для безопасного вывода)
                                try:
                                    content_str = content[:500].decode('utf-8', errors='replace')
                                    self.log(f"      Первые 500 байт: {content_str}")
                                except Exception:
                                    self.log(f"      Первые 500 байт: {repr(content[:500])}")
                            
                            raise Exception(f"Файлы из concat не найдены после записи: {[p for _, p, _ in verification_errors]}")
                    except Exception as e:
                        self.log(f"   ⚠️ Ошибка при проверке concat файла: {e}")
                        import traceback
                        self.log(f"   Детали: {traceback.format_exc()[:500]}")
                        # Не прерываем выполнение, но логируем
                    
                    # Проверяем, что concat файл создан
                    if not os.path.exists(concat_list):
                        raise Exception(f"Concat файл не создан: {concat_list}")
                    
                    concat_size = os.path.getsize(concat_list)
                    self.log(f"✅ Concat файл создан: {concat_list} ({concat_size} байт)")
                    
                    # Показываем содержимое concat файла для отладки и проверяем пути
                    with open(concat_list, 'r', encoding='utf-8', errors='replace') as f:
                        lines = f.readlines()
                        self.log(f"   Строк в concat файле: {len(lines)}")
                        
                        # Проверяем, что все файлы из concat существуют
                        missing_in_concat = []
                        for line_num, line in enumerate(lines, 1):
                            line = line.strip()
                            if line.startswith("file '"):
                                # Извлекаем путь из строки
                                path_start = line.find("'") + 1
                                path_end = line.rfind("'")
                                if path_start > 0 and path_end > path_start:
                                    file_path = line[path_start:path_end]
                                    # Убираем экранирование кавычек
                                    file_path = file_path.replace("'\\''", "'")
                                    # Заменяем прямые слеши обратно на обратные для Windows
                                    file_path_win = file_path.replace('/', '\\') if os.name == 'nt' else file_path
                                    if not os.path.exists(file_path_win):
                                        missing_in_concat.append((line_num, file_path_win))
                        
                        if missing_in_concat:
                            self.log(f"   ❌ ОШИБКА: Файлы из concat не найдены:")
                            for line_num, missing_path in missing_in_concat:
                                self.log(f"      Строка {line_num}: {missing_path}")
                            raise Exception(f"Файлы из concat не найдены: {[p for _, p in missing_in_concat]}")
                        
                        if len(lines) <= 5:
                            for line in lines:
                                self.log(f"   {line.strip()}")
                        else:
                            for line in lines[:3]:
                                self.log(f"   {line.strip()}")
                            self.log(f"   ... и еще {len(lines) - 3} строк")
                    
                except Exception as e:
                    self.log(f"❌ ОШИБКА при создании concat файла: {e}")
                    raise
                
                # Нормализуем пути перед передачей в ffmpeg
                concat_list_normalized = os.path.normpath(os.path.abspath(concat_list))
                final_audio_normalized = os.path.normpath(os.path.abspath(final_audio))
                
                # Проверяем, что concat файл существует
                if not os.path.exists(concat_list_normalized):
                    raise Exception(f"Concat файл не найден: {concat_list_normalized}")
                
                # Проверяем, что temp_dir существует для выходного файла
                if not os.path.exists(temp_dir):
                    os.makedirs(temp_dir, exist_ok=True)
                
                self.log(f"🎵 Объединяю аудио файлы...")
                self.log(f"   Concat файл: {concat_list_normalized}")
                self.log(f"   Существует: {os.path.exists(concat_list_normalized)}")
                self.log(f"   Размер: {os.path.getsize(concat_list_normalized)} байт")
                self.log(f"   Выходной файл: {final_audio_normalized}")
                
                # Проверяем, что FFmpeg существует (с поддержкой imageio-ffmpeg)
                ffmpeg_found = False
                ffmpeg_path = FFMPEG_EXE
                
                # Проверка 1: прямой путь
                if os.path.exists(FFMPEG_EXE):
                    ffmpeg_found = True
                    ffmpeg_path = FFMPEG_EXE
                # Проверка 2: в PATH
                elif shutil.which(FFMPEG_EXE):
                    ffmpeg_found = True
                    ffmpeg_path = shutil.which(FFMPEG_EXE)
                # Проверка 3: через imageio-ffmpeg
                else:
                    try:
                        import imageio_ffmpeg
                        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                        if ffmpeg_path and os.path.exists(ffmpeg_path):
                            ffmpeg_found = True
                            self.log(f"   ✅ FFmpeg найден через imageio-ffmpeg: {ffmpeg_path}")
                    except ImportError:
                        pass
                    except Exception as e:
                        self.log(f"   ⚠️ Ошибка при проверке imageio-ffmpeg: {e}")
                
                if not ffmpeg_found:
                    error_msg = (
                        f"FFmpeg не найден!\n\n"
                        f"Проверенные пути:\n"
                        f"  - {FFMPEG_EXE}\n"
                        f"  - В системном PATH\n"
                        f"  - Через imageio-ffmpeg\n\n"
                        f"Установите FFmpeg одним из способов:\n"
                        f"  1. Скачайте с https://ffmpeg.org/download.html\n"
                        f"  2. Или установите: pip install imageio-ffmpeg\n"
                        f"  3. Или поместите ffmpeg.exe в папку bin рядом с программой"
                    )
                    raise Exception(error_msg)
                
                # Обновляем FFMPEG_EXE для использования в команде
                if ffmpeg_path and ffmpeg_path != FFMPEG_EXE:
                    self.log(f"   🔄 Используется FFmpeg: {ffmpeg_path}")
                    # Заменяем первый элемент команды на найденный путь
                    ffmpeg_command = [ffmpeg_path] + [
                        "-f", "concat", "-safe", "0", "-i", concat_list_normalized,
                        "-c", "copy", "-y", final_audio_normalized
                    ]
                else:
                    ffmpeg_path = FFMPEG_EXE
                    ffmpeg_command = [
                        FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list_normalized,
                        "-c", "copy", "-y", final_audio_normalized
                    ]
                
                self.log(f"   FFmpeg: {ffmpeg_path}")
                self.log(f"   FFmpeg существует: {os.path.exists(ffmpeg_path)}")
                
                # Проверяем, что все файлы из concat существуют перед запуском ffmpeg
                with open(concat_list_normalized, 'r', encoding='utf-8', errors='replace') as f:
                    concat_lines = f.readlines()
                    for line in concat_lines:
                        line = line.strip()
                        if line.startswith("file '"):
                            path_start = line.find("'") + 1
                            path_end = line.rfind("'")
                            if path_start > 0 and path_end > path_start:
                                file_path = line[path_start:path_end]
                                file_path = file_path.replace("'\\''", "'")
                                file_path_win = file_path.replace('/', '\\') if os.name == 'nt' else file_path
                                if not os.path.exists(file_path_win):
                                    raise Exception(f"Файл из concat не найден: {file_path_win}")
                
                self.log(f"   ✅ Все файлы из concat существуют, запускаю FFmpeg...")
                self.run_ffmpeg_command(ffmpeg_command)
                
                # Проверяем, что выходной файл создан
                if not os.path.exists(final_audio_normalized):
                    raise Exception(f"Выходной аудио файл не создан: {final_audio_normalized}")
                
                final_size = os.path.getsize(final_audio_normalized)
                if final_size == 0:
                    raise Exception(f"Выходной аудио файл пуст: {final_audio_normalized}")
                
                self.log(f"✅ Финальное аудио создано: {final_audio_normalized} ({final_size} байт)")
                
                # Обновляем путь к финальному аудио
                final_audio = final_audio_normalized
                
                # ОПТИМИЗАЦИЯ: кэшируем длительность финального аудио
                audio_duration = self.get_audio_duration(final_audio, use_cache=True)
                self.log(f"✅ Аудио: {audio_duration:.2f}с ({audio_duration/60:.1f} мин)")
            else:
                self.log("⚠️ Аудио не найдено")
            
            _phase("ШАГ 1 Аудио")
            # ШАГ 2: СУБТИТРЫ
            print_progress(2, 4, "этапов рендера")
            subtitle_file = None
            if self.add_subtitles and audio_duration > 0:
                self.log("\n--- 📝 ШАГ 2/4: Субтитры ---")
                subtitle_file = self.create_subtitle_file_from_audio(final_audio)
            
            _phase("ШАГ 2 Субтитры")
            # ШАГ 3: ВИДЕОРЯД
            print_progress(3, 4, "этапов рендера")
            self.log("\n--- 🎞️ ШАГ 3/4: Видеоряд ---")
            
            # Загружаем дополнительные материалы (дополняют проект, порядок material_order — на всё)
            extra_files = []
            extra_image_count = 0
            extra_video_count = 0
            extra_materials_dir = (self.extra_materials_dir or '').strip() if self.extra_materials_dir else None
            if extra_materials_dir:
                extra_materials_dir = os.path.normpath(os.path.abspath(extra_materials_dir))
                if not os.path.isdir(extra_materials_dir):
                    self.log(f"⚠️ Папка доп. материалов не найдена: {extra_materials_dir}")
                else:
                    self.log(f"📂 Загрузка дополнительных материалов (картинки + видео): {extra_materials_dir}")
                    try:
                        for root, dirs, files in os.walk(extra_materials_dir):
                            dirs[:] = sorted([d for d in dirs if not d.startswith('.')])
                            for f in sorted(files):
                                if f.startswith('.'):
                                    continue
                                ext = os.path.splitext(f)[1].lower()
                                if ext in SUPPORTED_IMAGE_FORMATS or ext in SUPPORTED_VIDEO_FORMATS:
                                    full_path = os.path.join(root, f)
                                    extra_files.append(full_path)
                                    if ext in SUPPORTED_IMAGE_FORMATS:
                                        extra_image_count += 1
                                    else:
                                        extra_video_count += 1
                        parts = []
                        if extra_image_count:
                            parts.append(f"{extra_image_count} картинок")
                        if extra_video_count:
                            parts.append(f"{extra_video_count} видео")
                        self.log(f"   ✅ Добавлено {len(extra_files)} файлов из доп. папки ({', '.join(parts)})")
                    except Exception as e:
                        self.log(f"   ⚠️ Ошибка при чтении доп. папки: {e}")
            
            # Ищем медиафайлы в папке проекта
            self.log("🔍 Ищу изображения и видео рекурсивно по всей папке проекта...")
            all_media = []
            assets_dir = os.path.join(self.project_dir, "_ASSETS")
            assets_dir_normalized = os.path.normpath(assets_dir)
            project_dir_normalized = os.path.normpath(os.path.abspath(self.project_dir))
            
            _proj_name = os.path.basename(self.project_dir)
            _safe_fn = sanitize_filename(_proj_name, max_length=150)
            _output_file_norm = os.path.normpath(os.path.join(project_dir_normalized, f"{_safe_fn}.mp4"))
            
            try:
                all_media_set = set()
                for root, dirs, files in os.walk(project_dir_normalized):
                    root_normalized = os.path.normpath(root)
                    if root_normalized == assets_dir_normalized:
                        dirs[:] = []
                        continue
                    # Исключаем служебные папки
                    dirs[:] = [d for d in dirs if not d.startswith('.')
                               and 'temp' not in d.lower() and 'tmp' not in d.lower()
                               and d != '_ASSETS']
                    
                    for f in files:
                        # `tmp` наравне с `temp`: ускорение озвучки пишет
                        # `<имя>.spd.tmp.mp3`, и осиротевший файл приклеивался к дорожке
                        _fl = f.lower()
                        if (f.startswith('.') or 'temp' in _fl or 'tmp' in _fl
                                or 'final_video' in _fl):
                            continue
                        ext = os.path.splitext(f)[1].lower()
                        if ext in SUPPORTED_IMAGE_FORMATS or ext in SUPPORTED_VIDEO_FORMATS:
                            full_path = os.path.join(root, f)
                            full_norm = os.path.normpath(full_path)
                            if full_norm == _output_file_norm:
                                print_debug(f"Исключён предыдущий рендер: {f}")
                                continue
                            if full_norm not in all_media_set:
                                all_media_set.add(full_norm)
                                all_media.append(full_path)
            except Exception as e:
                self.log(f"⚠️ Ошибка при поиске медиафайлов: {e}")
            
            # Добавляем стоки в all_media (без дубликатов, нормализация путей)
            all_media_norm = {os.path.normpath(os.path.abspath(x)) for x in all_media}
            for p in extra_files:
                pn = os.path.normpath(os.path.abspath(p))
                if pn not in all_media_norm:
                    all_media.append(p)
                    all_media_norm.add(pn)
            
            if self.material_order == 'manual':
                manual_file = os.path.join(self.project_dir, "manual_media_order.json")
                if os.path.isfile(manual_file):
                    try:
                        with open(manual_file, 'r', encoding='utf-8', errors='replace') as f:
                            manual_list = json.load(f)
                        if isinstance(manual_list, list) and manual_list:
                            norm_to_path = {os.path.normpath(os.path.abspath(x)): x for x in all_media}
                            ordered = []
                            for p in manual_list:
                                pn = os.path.normpath(os.path.abspath(p))
                                match = norm_to_path.get(pn)
                                if match:
                                    ordered.append(match)
                            if ordered:
                                ordered_set = {os.path.normpath(os.path.abspath(p)) for p in ordered}
                                for x in all_media:
                                    if os.path.normpath(os.path.abspath(x)) not in ordered_set:
                                        ordered.append(x)
                                        ordered_set.add(os.path.normpath(os.path.abspath(x)))
                                all_media = ordered
                                self.log(f"📋 Загружен ручной порядок: {len(all_media)} файлов")
                    except Exception as e:
                        self.log(f"⚠️ Не удалось загрузить ручной порядок: {e}")
                else:
                    self.log("⚠️ Ручной порядок выбран, но файл manual_media_order.json не найден — использую алфавитный порядок")
                    all_media.sort()
            else:
                all_media.sort()
            
            # Полный рандом: перемешиваем всё, чтобы не было "сначала доп папка, потом проект"
            if self.material_order == 'random':
                # random.sample создаёт новую последовательность — надёжнее, чем shuffle in-place
                all_media = random.sample(all_media, len(all_media))
                self.log("   🎲 Порядок: полный рандом (проект + доп. папка перемешаны)")
                # Debug: паттерн типов для проверки (V=видео, I=картинка)
                if len(all_media) >= 20:
                    def _seg_type(p):
                        ext = os.path.splitext(p)[1].lower()
                        return 'V' if ext in SUPPORTED_VIDEO_FORMATS else 'I'
                    pattern = ''.join(_seg_type(p) for p in all_media[:40])
                    print_debug(f"   Паттерн рандома (первые 40): {pattern}")
            
            # Исключаем референсы из списка изображений
            # 1. Исключаем папку _ASSETS полностью (там хранятся референсы)
            assets_dir = os.path.join(self.project_dir, "_ASSETS")
            assets_dir_normalized = os.path.normpath(os.path.abspath(assets_dir))
            
            # 2. Загружаем список изображений, использованных для генерации видео
            video_refs_file = os.path.join(self.project_dir, "googler_video_references.json")
            video_reference_images = set()
            if os.path.exists(video_refs_file):
                try:
                    with open(video_refs_file, 'r', encoding='utf-8', errors='replace') as f:
                        video_refs = json.load(f)
                        # Нормализуем пути для сравнения (поддержка относительных путей)
                        video_reference_images = set()
                        for p in video_refs:
                            if isinstance(p, str) and p.strip():
                                pn = os.path.normpath(os.path.abspath(os.path.join(self.project_dir, p)) if not os.path.isabs(p) else p)
                                video_reference_images.add(pn)
                    self.log(f"📎 Загружено {len(video_reference_images)} изображений-референсов для видео")
                except Exception as e:
                    self.log(f"⚠️ Не удалось загрузить список референсов видео: {e}")
            
            images = [p for p in all_media if os.path.splitext(p)[1].lower() in SUPPORTED_IMAGE_FORMATS]
            images_filtered = []
            for img_path in images:
                img_path_normalized = os.path.normpath(os.path.abspath(img_path))
                # Пропускаем изображения из папки _ASSETS (там хранятся референсы)
                if img_path_normalized.startswith(assets_dir_normalized + os.sep) or img_path_normalized == assets_dir_normalized:
                    self.log(f"⏭️ Пропущен референс из _ASSETS: {os.path.basename(img_path)}")
                    continue
                # Пропускаем изображения, использованные для генерации видео
                if img_path_normalized in video_reference_images:
                    self.log(f"⏭️ Пропущен референс для видео: {os.path.basename(img_path)}")
                    continue
                images_filtered.append(img_path)
            
            images = images_filtered  # Используем отфильтрованный список
            videos_raw = [p for p in all_media if os.path.splitext(p)[1].lower() in SUPPORTED_VIDEO_FORMATS]
            # Отсекаем битые видео (duration=0 или ffprobe). Параллельно получаем длительности при 5+
            videos = []
            if len(videos_raw) >= 5:
                def _check_video(vp):
                    if not os.path.exists(vp) or os.path.getsize(vp) < 1024:
                        return (vp, 0.0, "файл пуст/не найден")
                    dur = self.get_audio_duration(vp, use_cache=True)
                    if dur <= 0:
                        return (vp, 0.0, "duration=0")
                    return (vp, dur, None)
                with ThreadPoolExecutor(max_workers=min(8, len(videos_raw))) as ex:
                    results = list(ex.map(_check_video, videos_raw))
                for vp, dur, reason in results:
                    if reason:
                        self.log(f"   ⏭️ Пропуск битого видео ({reason}): {os.path.basename(vp)}")
                        continue
                    videos.append(vp)
            else:
                for vp in videos_raw:
                    if not os.path.exists(vp) or os.path.getsize(vp) < 1024:
                        self.log(f"   ⏭️ Пропуск битого видео (файл пуст/не найден): {os.path.basename(vp)}")
                        continue
                    dur = self.get_audio_duration(vp, use_cache=True)
                    if dur <= 0:
                        self.log(f"   ⏭️ Пропуск битого видео (duration=0): {os.path.basename(vp)}")
                        continue
                    videos.append(vp)
            if len(videos) < len(videos_raw):
                self.log(f"   ⚠️ Отфильтровано {len(videos_raw) - len(videos)} битых видео из {len(videos_raw)}")
            
            # Доп. папка = ДОПОЛНЕНИЕ к проекту. Порядок (material_order) распространяется на ВСЁ (проект + доп).
            # Лимит применяется только к generic stock. Видео с ведущим NNN уже привязаны
            # к конкретной сцене и не должны случайно исчезать из таймлайна.
            if self.stock_video_limit > 0:
                scene_videos, generic_videos = _partition_scene_mapped_videos(videos)
                if len(generic_videos) > self.stock_video_limit:
                    videos = _apply_stock_video_limit(videos, self.stock_video_limit)
                    self.log(
                        f"   📹 Стоки: взято {self.stock_video_limit} из {len(generic_videos)} generic "
                        f"(лимит {self.stock_video_limit}); сценовые NNN вне лимита: {len(scene_videos)}"
                    )
            
            self.log(f"🖼️ Найдено: {len(images)} картинок, {len(videos)} видео (референсы исключены)")
            order_names = {'video_first': 'видео вначало', 'images_first': 'картинки вначало', 'random': 'рандом', 'alternate': 'чередование', 'alphabetical': 'по алфавиту', 'manual': 'ручной порядок'}
            self.log(f"📋 Порядок материалов: {order_names.get(self.material_order, self.material_order)}")
            
            # Обработка картинок (image_to_safe: путь -> безопасный путь, битые не попадают)
            safe_images = []
            image_to_safe = {}
            if images:
                self.log(f"   🖼️ Подготовка {len(images)} картинок...")
                def _convert_one(args):
                    i, img = args
                    try:
                        if not os.path.exists(img) or os.path.getsize(img) < 100:
                            return (i, img, None)
                        converted = self.convert_image_to_png(img, temp_dir)
                        ext = os.path.splitext(converted)[1]
                        safe_name = f"image_{i:04d}{ext}"
                        safe_path = os.path.join(temp_dir, safe_name)
                        if converted != img and os.path.dirname(converted) == temp_dir:
                            os.rename(converted, safe_path)
                        else:
                            shutil.copy2(converted, safe_path)
                        if os.path.exists(safe_path) and os.path.getsize(safe_path) > 100:
                            return (i, img, safe_path)
                    except Exception:
                        pass
                    return (i, img, None)
                workers = int(self.render_settings.get('image_convert_workers', 0)) or 1
                if workers > 1 and len(images) >= 8:
                    self.log(f"   ⚡ Параллельная конвертация: {workers} потоков")
                    done = 0
                    with ThreadPoolExecutor(max_workers=workers) as ex:
                        futures = {ex.submit(_convert_one, (i, img)): i for i, img in enumerate(images)}
                        for future in as_completed(futures):
                            i, img, safe_path = future.result()
                            if safe_path:
                                image_to_safe[img] = safe_path
                            done += 1
                            if done % 5 == 0 or done == len(images):
                                print_progress(done, len(images), "картинок")
                else:
                    for i, img in enumerate(images):
                        _, _, safe_path = _convert_one((i, img))
                        if safe_path:
                            image_to_safe[img] = safe_path
                        if (i + 1) % 5 == 0 or i + 1 == len(images):
                            print_progress(i + 1, len(images), "картинок")
                safe_images = [image_to_safe[img] for img in images if img in image_to_safe]
            else:
                safe_images = []
            
            width, height = ffmpeg_args['size'].split('x')
            def _base_video_filter(p=None):
                # fill=True: видео заполняют кадр (cover) — без чёрных полей на не-16:9 источниках
                vf = self.get_scale_filter_for(width, height, p, fill=True)
                if self.vintage_effect:
                    vf = f"{vf},{self.get_vintage_filter()}"
                return vf
            
            # Единый пайплайн для ВСЕХ режимов material_order
            # videos: после валидации и stock_video_limit (цикл применяется внутри _build_timeline)
            # all_media: синхронизируем — только видео из videos + изображения (избегаем "дыр" при limit)
            videos_set = set(videos)
            images_set = set(images)
            all_media_filtered = [p for p in all_media if p in videos_set or (p in images_set and p in image_to_safe)]
            all_media = all_media_filtered
            
            if videos or safe_images:
                self.log(f"   📋 Единый пайплайн: {len(videos)} видео, {len(safe_images)} картинок")
                _t_tl = time.time()
                timeline = self._build_timeline(videos, images, safe_images, image_to_safe, all_media,
                                                audio_duration, videos_set, images_set)
                print_user(f"⏱   build_timeline: {time.time() - _t_tl:.1f}с ({len(timeline) if timeline else 0} сегментов)", "INFO")
                # СИНХРОН картинка↔озвучка: длительности картинок по whisper-таймкодам сцен.
                if not self.sync_audio:
                    self.log("   🎯 Синхрон с озвучкой: ВЫКЛ (галка) → равномерное распределение")
                elif timeline and audio_duration > 0:
                    try:
                        _t_sync = time.time()
                        synced = self._compute_synced_image_durations(final_audio, timeline, audio_duration)
                        if synced:
                            timeline = synced
                            self.log(f"   🎯 Синхрон ПРИМЕНЁН за {time.time() - _t_sync:.1f}с")
                        else:
                            self.log("   🎯 Синхрон НЕ применён (нет таймкодов/маппинга сцен) → равномерное")
                    except Exception as _e_sync:
                        self.log(f"   🎯 Синхрон упал ({_e_sync}) → равномерное")
                # Ретайм ведущих видео-клипов под синканную длину сцены (T<L обрезка / T>L замедление)
                if timeline:
                    timeline = self._retime_synced_videos(timeline, temp_dir, ffmpeg_args)
                # FRAME-LOCK: длительности картинок → сетку кадров со сквозным якорём (фикс дрейфа
                # синхрона). После ретайма (длительности финальные), до сбора video-audio и рендера.
                if timeline:
                    timeline = self._lock_frames_to_timeline(timeline)
                # 🔉 «Звук из видео»: позиции video-сегментов в ролике (собираем ПОСЛЕ ретайма —
                # длительности финальные; звук берём из audio_source: ретаймленный файл без звука).
                # Позиция сегмента в готовом ролике = его позиция в таймлайне: перекрытий между
                # сегментами нет (пачки картинок клеятся concat-демуксером), а съедание fd на стыках
                # ЧАСТЕЙ компенсируется добавкой кадров в _process_timeline. Прежняя поправка
                # −k*fade_dur исходила из перекрытия КАЖДОГО сегмента: её не было чем оправдать даже
                # раньше (при fast_batch_stitch xfade не выполняется вовсе), и она уводила звук клипа
                # вперёд на k*0.5с. Интро-видео сдвигают всё — эта поправка осталась в блоке микса.
                self._video_audio_events = []
                if timeline and getattr(self, 'use_video_audio', False):
                    try:
                        _off = 0.0
                        for _seg in timeline:
                            _d = float(_seg.get('duration') or 0)
                            if _seg.get('type') == 'video':
                                _asrc = _seg.get('audio_source') or _seg.get('source')
                                if _asrc:
                                    self._video_audio_events.append(
                                        {'offset': max(0.0, _off), 'source': _asrc, 'duration': _d})
                            _off += _d
                    except Exception as _vae:
                        print_debug(f"video_audio events: {_vae}")
                        self._video_audio_events = []
                if timeline:
                    _t_seg = time.time()
                    all_parts = self._process_timeline(timeline, _base_video_filter, temp_dir, ffmpeg_args)
                    _seg_dt = time.time() - _t_seg
                    _per = (_seg_dt / len(timeline)) if timeline else 0
                    print_user(f"⏱   рендер сегментов: {_seg_dt:.1f}с ({len(all_parts)}/{len(timeline)}, ~{_per*1000:.0f}мс/сегмент)", "INFO")
                    print_progress(len(all_parts), len(timeline), "сегментов")
                else:
                    all_parts = []
            else:
                all_parts = []
            
            # Интро-видео (список, всегда первыми, вне зависимости от порядка материалов)
            intro_parts_processed = []
            for i, intro_path in enumerate(self.intro_video_files):
                intro_path = (intro_path or '').strip()
                if not intro_path or not os.path.isfile(intro_path) or os.path.getsize(intro_path) < 1024:
                    continue
                try:
                    width, height = ffmpeg_args['size'].split('x')
                    vf = self.get_scale_filter_for(width, height, intro_path)
                    if self.vintage_effect:
                        vf = f"{vf},{self.get_vintage_filter()}"
                    intro_processed = os.path.join(temp_dir, f"intro_video_{i:02d}.mp4")
                    cmd = [FFMPEG_EXE, "-i", intro_path, "-vf", f"{vf},fps=fps={self.target_fps}:round=near", "-an"]
                    cmd.extend(self._get_encoder_params(ffmpeg_args))
                    cmd.extend(["-pix_fmt", "yuv420p", "-y", intro_processed])
                    self.run_ffmpeg_command(cmd)
                    if os.path.exists(intro_processed) and os.path.getsize(intro_processed) > 1024:
                        intro_parts_processed.append(intro_processed)
                except Exception as e:
                    self.log(f"⚠️ Не удалось обработать интро {os.path.basename(intro_path)}: {e}")
            main_parts = all_parts  # контент без интро (для раздельной обработки при зацикливании)
            if intro_parts_processed:
                all_parts = intro_parts_processed + all_parts
                self.log(f"🎬 Интро: добавлено {len(intro_parts_processed)} файлов первыми")
            
            # Склейка всех частей
            video_only = os.path.join(temp_dir, "video_only.mp4")
            
            def _concat_to_file(parts, out_path):
                """Склеивает parts в out_path (xfade или concat)."""
                if not parts:
                    return
                if len(parts) == 1:
                    shutil.copy2(parts[0], out_path)
                    return
                # fast_batch_stitch: даже при fade склеиваем сегменты stream-copy (переходы ВНУТРИ
                # сегментов сохранены, теряем только межсегментные ~стыки). Убирает пере-кодирование
                # всего ролика в финале = главную статью времени на CPU-боксах (NVENC недоступен).
                if self.enable_fade and not self.render_settings.get('fast_batch_stitch', True):
                    self._concat_parts_with_xfade(parts, self.fade_duration, temp_dir, ffmpeg_args, out_path)
                else:
                    concat_list = os.path.join(temp_dir, f"video_concat_{id(parts)}.txt")
                    with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
                        for p in parts:
                            escaped = _escape_concat_path(p)
                            f.write(f"file '{escaped}'\n")
                    # ОПТИМИЗАЦИЯ: все сегменты уже в одном кодеке/fps/yuv420p (созданы одним
                    # ffmpeg_args) → stream-copy вместо переэнкода всего ролика (мгновенно вместо
                    # минут). Валидируем выход; при провале — фолбэк на переэнкод (recovery > avoidance).
                    copy_ok = False
                    try:
                        self.run_ffmpeg_command([
                            FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list,
                            "-c", "copy", "-movflags", "+faststart", "-y", out_path
                        ])
                        copy_ok = self._validate_fragment(out_path)
                    except Exception as e:
                        print_debug(f"Концат stream-copy исключение: {e}")
                    if copy_ok:
                        print_user("⏱   концат: stream-copy (без переэнкода)", "INFO")
                    else:
                        print_debug("Концат: stream-copy не прошёл валидацию → переэнкод")
                        cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list]
                        cmd.extend(self._get_encoder_params(ffmpeg_args))
                        cmd.extend(["-pix_fmt", "yuv420p", "-r", str(self.target_fps), "-vsync", "cfr", "-movflags", "+faststart", "-y", out_path])
                        self.run_ffmpeg_command(cmd)
            
            if not all_parts:
                if audio_duration > 0:
                    self.log("⚠️ Создаю чёрный фон...")
                    black_bg_args = ffmpeg_args.copy()
                    black_bg_args['preset'] = 'ultrafast'
                    cmd = [FFMPEG_EXE, "-f", "lavfi", "-i", f"color=c=black:s={ffmpeg_args['size']}:r={self.target_fps}",
                           "-t", str(audio_duration)]
                    cmd.extend(self._get_encoder_params(black_bg_args))
                    cmd.extend(["-pix_fmt", "yuv420p", "-y", video_only])
                    self.run_ffmpeg_command(cmd)
                else:
                    raise Exception("Нет контента для рендеринга")
            elif intro_parts_processed and main_parts:
                # Интро есть и есть основной контент: зацикливаем ТОЛЬКО основной контент, интро — один раз
                intro_video_path = os.path.join(temp_dir, "intro_only.mp4")
                _concat_to_file(intro_parts_processed, intro_video_path)
                intro_dur = self.get_audio_duration(intro_video_path, use_cache=True)
                main_video_path = os.path.join(temp_dir, "main_content.mp4")
                if self.enable_fade:
                    self.log(f"   🔀 Xfade между {len(main_parts)} сегментами ({self.fade_duration}с)")
                _concat_to_file(main_parts, main_video_path)
                main_dur = self.get_audio_duration(main_video_path, use_cache=True)
                needed_main = max(0, audio_duration - intro_dur)
                if main_dur > 0 and main_dur < needed_main * 0.99:
                    self.log(f"   🔁 Зацикливаю основной контент ({main_dur:.0f}с → {needed_main:.0f}с), интро не повторяется")
                    looped_main = os.path.join(temp_dir, "main_looped.mp4")
                    self.run_ffmpeg_command([
                        FFMPEG_EXE, "-stream_loop", "-1", "-i", main_video_path,
                        "-t", str(needed_main), "-c", "copy", "-y", looped_main
                    ])
                    if os.path.exists(looped_main) and os.path.getsize(looped_main) > 1024:
                        main_for_final = looped_main
                    else:
                        main_for_final = main_video_path
                else:
                    main_for_final = main_video_path
                # Склейка intro + main через concat copy (оба файла уже перекодированы)
                self.log(f"   📎 Склейка интро и основного контента (concat copy)")
                concat_list_final = os.path.join(temp_dir, "intro_main_concat.txt")
                with open(concat_list_final, 'w', encoding='utf-8', errors='replace') as f:
                    f.write(f"file '{_escape_concat_path(intro_video_path)}'\n")
                    f.write(f"file '{_escape_concat_path(main_for_final)}'\n")
                self.run_ffmpeg_command([
                    FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list_final,
                    "-c", "copy", "-movflags", "+faststart", "-y", video_only
                ])
            elif intro_parts_processed and not main_parts:
                # Только интро — без зацикливания
                _concat_to_file(intro_parts_processed, video_only)
            else:
                # Без интро: обычная склейка
                if len(all_parts) == 1:
                    shutil.copy2(all_parts[0], video_only)
                else:
                    if self.enable_fade:
                        self.log(f"   🔀 Xfade между {len(all_parts)} сегментами ({self.fade_duration}с)")
                    _concat_to_file(all_parts, video_only)
                # Зацикливание если видео короче аудио (только когда нет интро)
                if audio_duration > 0 and os.path.exists(video_only):
                    video_dur = self.get_audio_duration(video_only, use_cache=True)
                    if video_dur > 0 and video_dur < audio_duration * 0.99:
                        self.log(f"   🔁 Зацикливаю видео ({video_dur:.0f}с → {audio_duration:.0f}с, без перерендера)")
                        looped = os.path.join(temp_dir, "video_looped.mp4")
                        self.run_ffmpeg_command([
                            FFMPEG_EXE, "-stream_loop", "-1", "-i", video_only,
                            "-t", str(audio_duration), "-c", "copy", "-y", looped
                        ])
                        if os.path.exists(looped) and os.path.getsize(looped) > 1024:
                            try:
                                os.remove(video_only)
                            except OSError:
                                pass
                            shutil.move(looped, video_only)

            # F. Опциональный единый grade поверх ВСЕГО собранного видеоряда
            # (пресет color_grade, напр. 'archive_grade'). Дефолт ВЫКЛ — no-op.
            if os.path.exists(video_only):
                self._apply_color_grade(video_only, temp_dir, ffmpeg_args)

            _phase("ШАГ 3 Видеоряд")
            # ШАГ 4: ФИНАЛЬНАЯ СБОРКА
            print_progress(4, 4, "этапов рендера")
            self.log("\n--- ✨ ШАГ 4/4: Финальная сборка ---")
            
            project_name = os.path.basename(self.project_dir)
            # Очищаем имя проекта для использования в имени файла
            safe_filename = sanitize_filename(project_name, max_length=150)
            output_file = os.path.join(self.project_dir, f"{safe_filename}.mp4")
            
            # Обработка аудио (фон + громкость)
            mixed_audio = os.path.join(temp_dir, "mixed_audio.mp3")
            final_audio_to_use = final_audio
            
            if audio_duration > 0:
                # Проверяем наличие фонового аудио
                bg_audio_path = self.background_audio_file
                
                # Логируем для отладки
                self.log(f"🔍 Проверка фонового аудио...")
                self.log(f"   Исходное значение: '{bg_audio_path}' (тип: {type(bg_audio_path).__name__})")
                
                # Проверяем что путь не пустой и не None
                if bg_audio_path and str(bg_audio_path).strip():
                    # Нормализуем путь
                    bg_audio_path = os.path.normpath(os.path.abspath(str(bg_audio_path).strip()))
                    self.log(f"   Нормализованный путь: {bg_audio_path}")
                    self.log(f"   Файл существует: {os.path.exists(bg_audio_path)}")
                    
                    if os.path.exists(bg_audio_path):
                        self.log(f"🎵 Микширование с фоном...")
                        self.log(f"   📁 Фоновое аудио: {os.path.basename(bg_audio_path)}")
                        self.log(f"   🔊 Громкость основного: {self.main_audio_volume*100:.0f}%")
                        self.log(f"   🔊 Громкость фона: {self.background_audio_volume*100:.0f}%")
                        
                        # ОПТИМИЗАЦИЯ: используем уже известную длительность из кэша
                        main_dur = self._duration_cache.get(
                            os.path.normpath(os.path.abspath(final_audio)),
                            self.get_audio_duration(final_audio, use_cache=True)
                        )
                        
                        # Выполняем микширование
                        mix_cmd = [
                            FFMPEG_EXE, "-i", final_audio, 
                            "-stream_loop", "-1", "-i", bg_audio_path,
                            "-filter_complex", f"[0:a]volume={self.main_audio_volume}[main];[1:a]volume={self.background_audio_volume}[bg];[main][bg]amix=inputs=2:duration=first:dropout_transition=2",
                            "-t", str(main_dur), "-c:a", "libmp3lame", "-b:a", "128k", "-y", mixed_audio
                        ]
                        
                        # Логируем команду для отладки
                        self.log(f"   🔧 Команда микширования: {' '.join(mix_cmd[:5])}...")
                        
                        # Выполняем команду микширования
                        try:
                            self.log(f"   ⏳ Выполняю микширование...")
                            self.run_ffmpeg_command(mix_cmd)
                            
                            # Небольшая задержка для завершения записи файла
                            time.sleep(0.5)
                            
                            # Проверяем что файл создан и не пустой
                            if os.path.exists(mixed_audio):
                                file_size = os.path.getsize(mixed_audio)
                                if file_size > 0:
                                    # Проверяем что файл действительно аудио (минимальный размер для MP3 ~100 байт)
                                    if file_size > 100:
                                        final_audio_to_use = mixed_audio
                                        self.log(f"✅ Фоновое аудио успешно добавлено! (размер: {file_size} байт)")
                                    else:
                                        self.log(f"⚠️ Ошибка: файл микширования слишком мал ({file_size} байт), возможно поврежден")
                                        self.log(f"   Используем основное аудио")
                                        final_audio_to_use = final_audio
                                else:
                                    self.log(f"⚠️ Ошибка: файл микширования пуст (0 байт), используем основное аудио")
                                    final_audio_to_use = final_audio
                            else:
                                self.log(f"⚠️ Ошибка: файл микширования не создан: {mixed_audio}")
                                self.log(f"   Проверьте логи FFmpeg выше для деталей")
                                self.log(f"   Убедитесь что:")
                                self.log(f"   - Фоновое аудио существует: {bg_audio_path}")
                                self.log(f"   - Основное аудио существует: {final_audio}")
                                final_audio_to_use = final_audio
                        except Exception as e:
                            self.log(f"⚠️ Ошибка при микшировании: {e}")
                            import traceback
                            self.log(f"   Детали: {traceback.format_exc()[:500]}")
                            self.log(f"   Используем основное аудио без фона")
                            final_audio_to_use = final_audio
                    else:
                        if bg_audio_path:
                            self.log(f"⚠️ Фоновое аудио не найдено: {bg_audio_path}")
                        else:
                            self.log(f"⚠️ Путь к фоновому аудио не указан")
                elif self.main_audio_volume != 1.0:
                    adjusted = os.path.join(temp_dir, "adjusted.mp3")
                    self.run_ffmpeg_command([
                        FFMPEG_EXE, "-i", final_audio, "-filter:a", f"volume={self.main_audio_volume}",
                        "-c:a", "libmp3lame", "-b:a", "128k", "-y", adjusted
                    ])
                    final_audio_to_use = adjusted

            # 🔉 «Звук из видео»: подмешиваем аудио ведущих клипов в их позициях таймлайна.
            # amix normalize=0 — озвучка НЕ приглушается добавлением входов. Интро-видео идут
            # первыми в ролике → все события сдвигаются на их суммарную длину (минус xfade-стыки).
            if audio_duration > 0 and getattr(self, 'use_video_audio', False):
                _events = [e for e in (getattr(self, '_video_audio_events', None) or [])
                           if e.get('source') and os.path.isfile(e['source'])
                           and self._media_has_audio(e['source'])][:24]
                if _events:
                    _intro_files = [p for p in (self.intro_video_files or []) if p and os.path.isfile(p)]
                    _intro_shift = sum(self.get_audio_duration(p, use_cache=True) for p in _intro_files)
                    if self.enable_fade and _intro_files:
                        _intro_shift -= len(_intro_files) * self.fade_duration_images
                    vmix = os.path.join(temp_dir, "video_audio_mix.mp3")
                    _cmd = [FFMPEG_EXE, "-i", final_audio_to_use]
                    _fparts, _tags = [], []
                    for _i, _ev in enumerate(_events, start=1):
                        _cmd.extend(["-i", _ev['source']])
                        _ms = max(0, int((_ev['offset'] + max(0.0, _intro_shift)) * 1000))
                        _fparts.append(f"[{_i}:a]atrim=duration={max(0.2, _ev['duration']):.3f},"
                                       f"adelay={_ms}|{_ms},volume={self.video_audio_volume}[c{_i}]")
                        _tags.append(f"[c{_i}]")
                    _fparts.append("[0:a]" + "".join(_tags) +
                                   f"amix=inputs={len(_events) + 1}:duration=first:dropout_transition=0:normalize=0")
                    _cmd.extend(["-filter_complex", ";".join(_fparts), "-c:a", "libmp3lame", "-b:a", "128k", "-y", vmix])
                    try:
                        self.run_ffmpeg_command(_cmd)
                        if os.path.exists(vmix) and os.path.getsize(vmix) > 100:
                            final_audio_to_use = vmix
                            self.log(f"🔉 Звук видео-клипов подмешан: {len(_events)} клип(ов), громкость {self.video_audio_volume*100:.0f}%")
                        else:
                            self.log("⚠️ Микс звука клипов не создался — рендер без него")
                    except Exception as _vme:
                        self.log(f"⚠️ Микс звука клипов упал ({_vme}) — рендер без него")
                else:
                    self.log("🔉 Звук видео: аудио-дорожек у клипов не нашлось — пропущено")

            temp_output = os.path.join(temp_dir, "temp_video.mp4")
            
            # Сборка видео + аудио
            cmd = [FFMPEG_EXE, "-i", video_only]
            if audio_duration > 0:
                # Логируем какой аудио файл используется
                if final_audio_to_use != final_audio:
                    self.log(f"🎵 Используется микшированное аудио: {os.path.basename(final_audio_to_use)}")
                else:
                    self.log(f"🎵 Используется основное аудио: {os.path.basename(final_audio_to_use)}")
                
                cmd.extend(["-i", final_audio_to_use])
                cmd.extend(["-c:a", "aac", "-b:a", "128k"])
            else:
                cmd.append("-an")
            cmd.extend(["-c:v", "copy"])
            # ОПТИМИЗАЦИЯ: используем уже известную длительность вместо повторного вызова
            # -t в конце чтобы ограничить OUTPUT, а не input
            if audio_duration > 0:
                # Используем длительность из кэша или уже вычисленную
                final_audio_dur = self._duration_cache.get(
                    os.path.normpath(os.path.abspath(final_audio_to_use)), 
                    audio_duration
                )
                cmd.extend(["-t", str(final_audio_dur)])
            # -movflags для YouTube совместимости
            cmd.extend(["-movflags", "+faststart", "-y", temp_output])
            self.run_ffmpeg_command(cmd)
            
            # Vintage overlay
            if self.vintage_effect:
                _overlay_base = getattr(sys, '_MEIPASS', None)
                if not _overlay_base:
                    is_frozen = getattr(sys, 'frozen', False)
                    if is_frozen and hasattr(sys, 'executable'):
                        _overlay_base = os.path.dirname(sys.executable)
                    else:
                        try:
                            _overlay_base = os.path.dirname(os.path.abspath(__file__))
                        except:
                            _overlay_base = os.getcwd()
                
                overlay_png = os.path.join(_overlay_base, "vintage_overlay.png")
                if os.path.exists(overlay_png):
                    overlay_output = os.path.join(temp_dir, "with_overlay.mp4")
                    self.apply_vintage_overlay(temp_output, overlay_output, overlay_png, ffmpeg_args['size'])
                    temp_output = overlay_output
            
            # Пользовательские оверлеи
            if self.overlays:
                valid_overlays = [ov for ov in self.overlays if ov.get('path') and os.path.exists(ov['path'])]
                if valid_overlays:
                    overlays_output = os.path.join(temp_dir, "with_overlays.mp4")
                    self.apply_overlays(temp_output, overlays_output, ffmpeg_args['size'], ffmpeg_args)
                    temp_output = overlays_output
            
            # Добавление трека в конце на черном фоне
            if self.end_track_file and os.path.exists(self.end_track_file):
                self.log("🎵 Добавление трека в конце ролика...")
                try:
                    # Получаем длительность трека
                    end_track_duration = self.get_audio_duration(self.end_track_file, use_cache=True)
                    if end_track_duration > 0:
                        # Создаем черный фон на длительность трека
                        black_screen = os.path.join(temp_dir, "black_screen.mp4")
                        cmd = [FFMPEG_EXE, "-f", "lavfi", "-i", f"color=c=black:s={ffmpeg_args['size']}:r={self.target_fps}",
                               "-t", str(end_track_duration)]
                        cmd.extend(self._get_encoder_params(ffmpeg_args))
                        cmd.extend(["-pix_fmt", "yuv420p", "-y", black_screen])
                        self.run_ffmpeg_command(cmd)
                        
                        # Создаем аудио с нужной громкостью
                        end_track_audio = os.path.join(temp_dir, "end_track_audio.mp3")
                        self.run_ffmpeg_command([
                            FFMPEG_EXE, "-i", self.end_track_file,
                            "-filter:a", f"volume={self.end_track_volume}",
                            "-c:a", "libmp3lame", "-b:a", "128k", "-t", str(end_track_duration),
                            "-y", end_track_audio
                        ])
                        
                        # Создаем видео с треком на черном фоне
                        end_track_video = os.path.join(temp_dir, "end_track_video.mp4")
                        self.run_ffmpeg_command([
                            FFMPEG_EXE, "-i", black_screen, "-i", end_track_audio,
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                            "-shortest", "-movflags", "+faststart",
                            "-y", end_track_video
                        ])
                        
                        # Склеиваем основное видео и трек в конце
                        concat_list = os.path.join(temp_dir, "final_concat.txt")
                        with open(concat_list, 'w', encoding='utf-8', errors='replace') as f:
                            escaped_main = _escape_concat_path(temp_output)
                            escaped_end = _escape_concat_path(end_track_video)
                            f.write(f"file '{escaped_main}'\n")
                            f.write(f"file '{escaped_end}'\n")
                        
                        # Склеиваем с перекодированием для корректных timestamps
                        final_with_track = os.path.join(temp_dir, "final_with_track.mp4")
                        cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0", "-i", concat_list]
                        cmd.extend(self._get_encoder_params(ffmpeg_args))
                        cmd.extend(["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                                   "-movflags", "+faststart", "-y", final_with_track])
                        self.run_ffmpeg_command(cmd)
                        temp_output = final_with_track
                        self.log(f"✅ Трек добавлен в конец ({end_track_duration:.2f} сек)")
                    else:
                        self.log("⚠️ Не удалось определить длительность трека, пропускаем")
                except Exception as e:
                    self.log(f"⚠️ Ошибка при добавлении трека в конце: {e}")
            
            # Субтитры
            if subtitle_file and os.path.exists(subtitle_file):
                self.log("📝 Наложение субтитров...")
                safe_subs = os.path.join(temp_dir, "subtitles.srt")
                shutil.copy2(subtitle_file, safe_subs)
                subs_path = safe_subs.replace('\\', '/').replace(':', '\\:')
                
                cmd = [FFMPEG_EXE, "-i", temp_output, "-vf", f"subtitles={subs_path}:force_style='FontName=Arial,FontSize=28,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2'"]
                # Для субтитров используем более качественный пресет
                subs_args = ffmpeg_args.copy()
                subs_args['preset'] = 'medium'
                cmd.extend(self._get_encoder_params(subs_args))
                cmd.extend(["-c:a", "copy" if audio_duration > 0 else "-an"])
                cmd.extend(["-movflags", "+faststart", "-y", output_file])
                self.run_ffmpeg_command(cmd)
            else:
                # Добавляем faststart для YouTube совместимости
                self.run_ffmpeg_command([
                    FFMPEG_EXE, "-i", temp_output,
                    "-c", "copy", "-movflags", "+faststart",
                    "-y", output_file
                ])
            
            # Рандомизация метаданных для уникальности каждого видео
            if self.randomize_metadata and os.path.exists(output_file):
                self.log("🔀 Рандомизация метаданных...")
                output_file = self._apply_random_metadata(output_file, temp_dir, audio_duration > 0)
            
            _phase("ШАГ 4 Финальная сборка")
            print_user(f"⏱ ВСЕГО рендер: {time.time() - _t_render:.1f}с", "SUCCESS")
            self.log("\n" + "="*70)
            self.log(f"✅ 🎉 ГОТОВО!")
            self.log(f"📁 {output_file}")
            self.log("="*70)
            return output_file
            
        except Exception as e:
            self.log(f"\n❌ ОШИБКА: {e}")
            raise
        finally:
            self.cleanup_converted_images()
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                self.log(f"⚠️ временная папка не убрана: {e}")
            try:
                os.rmdir(temp_base)      # снимаем контейнер, если он опустел
            except OSError:
                pass                     # не пуст (параллельный рендер) или уже нет


def _run_self_test():
    """Внутренняя проверка логики пайплайна (без FFmpeg, без файлов)."""
    print("=" * 60)
    print(f"Render Worker v{__VERSION__} - Self-Test (Debug Mode)")
    print("=" * 60)
    
    # Мок-конфиг
    config = {
        "quality": "720p", "add_subtitles": False, "sync_audio": True,
        "min_image_duration": 3, "max_image_duration": 5,
        "enable_fade": True, "fade_duration": 0.5,
        "material_order": "alternate", "stock_video_limit": 2, "stock_video_cycle": True,
        "render_settings": {}, "target_fps": 30
    }
    
    # Создаём временную папку для теста
    import tempfile
    with tempfile.TemporaryDirectory(prefix="render_selftest_") as tmp:
        project_dir = tmp
        os.makedirs(project_dir, exist_ok=True)
        
        # Создаём минимальные тестовые файлы через FFmpeg (если доступен)
        try:
            r = subprocess.run([FFMPEG_EXE, "-version"], capture_output=True, timeout=5, **_SUBPROCESS_HIDE_WINDOW)
            ffmpeg_ok = r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            ffmpeg_ok = False
        
        if not ffmpeg_ok:
            print("\n[SKIP] FFmpeg not found - running logic-only tests")
            print("       Install FFmpeg for full integration test")
            # Логические проверки без FFmpeg (project_dir = tmp уже есть)
            r = VideoRenderer(project_dir, config)
            # 1. Пустой таймлайн
            tl_empty = r._build_timeline([], [], [], {}, [], 30.0, set(), set())
            assert tl_empty == [], f"Empty timeline expected, got {tl_empty}"
            print("  [OK] Empty timeline")
            # 2. Только видео (мок через кэш длительностей)
            v1 = os.path.join(project_dir, "v1.mp4")
            open(v1, "wb").write(b"x" * 100)  # непустой файл
            v1_key = os.path.normpath(os.path.abspath(v1))
            r._duration_cache[v1_key] = 5.0
            # Без cycle — ровно 1 сегмент
            r.stock_video_cycle = False
            tl_v = r._build_timeline([v1], [], [], {}, [v1], 30.0, {v1}, set())
            assert len(tl_v) >= 1 and tl_v[0]["type"] == "video" and tl_v[0]["duration"] == 5.0
            print("  [OK] Video-only timeline")
            # 3. Чередование (alternate): 2 видео, 2 картинки
            v2 = os.path.join(project_dir, "v2.mp4")
            open(v2, "wb").write(b"x" * 100)
            img1 = os.path.join(project_dir, "img1.png")
            img2 = os.path.join(project_dir, "img2.png")
            open(img1, "wb").write(b"x" * 100)
            open(img2, "wb").write(b"x" * 100)
            r._duration_cache[os.path.normpath(os.path.abspath(v2))] = 5.0
            img_to_safe = {img1: img1, img2: img2}
            all_m = [v1, img1, v2, img2]  # alternate order
            tl_alt = r._build_timeline([v1, v2], [img1, img2], [img1, img2], img_to_safe,
                                       all_m, 30.0, {v1, v2}, {img1, img2})
            assert len(tl_alt) >= 4, f"Expected >=4 segments, got {len(tl_alt)}"
            types = [s["type"] for s in tl_alt]
            assert "video" in types and "image" in types
            print("  [OK] Alternate timeline (video+image)")
            # 4. video_first: все видео и картинки, порядок v1,v2,img1,img2
            r.material_order = "video_first"
            all_m_vf = [v1, v2, img1, img2]
            tl_vf = r._build_timeline([v1, v2], [img1, img2], [img1, img2], img_to_safe,
                                      all_m_vf, 30.0, {v1, v2}, {img1, img2})
            assert len(tl_vf) == 4, f"video_first: expected 4 segments, got {len(tl_vf)}"
            order_vf = [s["type"] for s in tl_vf]
            assert order_vf == ["video", "video", "image", "image"], f"video_first order: {order_vf}"
            print("  [OK] video_first order (v,v,i,i)")
            # 5. images_first: порядок img1,img2,v1,v2
            r.material_order = "images_first"
            all_m_if = [img1, img2, v1, v2]
            tl_if = r._build_timeline([v1, v2], [img1, img2], [img1, img2], img_to_safe,
                                      all_m_if, 30.0, {v1, v2}, {img1, img2})
            assert len(tl_if) == 4, f"images_first: expected 4 segments, got {len(tl_if)}"
            order_if = [s["type"] for s in tl_if]
            assert order_if == ["image", "image", "video", "video"], f"images_first order: {order_if}"
            print("  [OK] images_first order (i,i,v,v)")
            # 6. Длительность: при has_both видео trim-аются под бюджет
            r.material_order = "video_first"
            tl_budget = r._build_timeline([v1, v2], [img1, img2], [img1, img2], img_to_safe,
                                         [v1, v2, img1, img2], 20.0, {v1, v2}, {img1, img2})
            total_dur = sum(s["duration"] for s in tl_budget)
            assert total_dur <= 21, f"Timeline total {total_dur}s should fit in ~20s"
            print(f"  [OK] Duration budget (total={total_dur:.1f}s <= 20s)")
            print("\n[OK] Logic-only tests passed")
            return 0
        # (далее — полный тест с FFmpeg)
        
        # Создаём 2 видео, 3 картинки, аудио 30с (синтаксис как в test_render_scenarios)
        v1 = os.path.join(project_dir, "v1.mp4")
        v2 = os.path.join(project_dir, "v2.mp4")
        def _run_ff(cmd, desc=""):
            r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, **_SUBPROCESS_HIDE_WINDOW)
            if r.returncode != 0 and desc:
                print(f"  [WARN] {desc}: {r.stderr[:200] if r.stderr else r.stdout[:200]}")
            return r.returncode == 0
        _run_ff([FFMPEG_EXE, "-y", "-f", "lavfi", "-i", "testsrc=duration=5:size=320x180:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                 "-c:v", "libx264", "-preset", "ultrafast", "-t", "5",
                 "-c:a", "aac", "-b:a", "128k", v1], "v1.mp4")
        _run_ff([FFMPEG_EXE, "-y", "-f", "lavfi", "-i", "testsrc=duration=5:size=320x180:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                 "-c:v", "libx264", "-preset", "ultrafast", "-t", "5",
                 "-c:a", "aac", "-b:a", "128k", v2], "v2.mp4")
        for i in range(1, 4):
            _run_ff([FFMPEG_EXE, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=0.1",
                    "-frames:v", "1", os.path.join(project_dir, f"img{i}.png")], f"img{i}.png")
        _run_ff([FFMPEG_EXE, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                 "-c:a", "libmp3lame", "-b:a", "128k",
                 os.path.join(project_dir, "audio.mp3")], "audio.mp3")
        
        if not all(os.path.exists(p) for p in [v1, v2, os.path.join(project_dir, "img1.png"), os.path.join(project_dir, "audio.mp3")]):
            print("\n[SKIP] Could not create test files (check FFmpeg stderr above)")
            return 0
        
        print("\n[RUN] Full render test (2 video, 3 images, 30s audio, alternate+cycle)...")
        try:
            renderer = VideoRenderer(project_dir, config)
            output = renderer.render_video()
            if output and os.path.exists(output) and os.path.getsize(output) > 1024:
                print(f"\n[OK] Render succeeded: {output}")
                print(f"     File size: {os.path.getsize(output) / 1024:.1f} KB")
                return 0
            else:
                print("\n[FAIL] Output file missing or empty")
                return 1
        except Exception as e:
            print(f"\n[FAIL] Exception: {e}")
            import traceback
            traceback.print_exc()
            return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project_dir", required=False)
    parser.add_argument("--config", required=False)
    parser.add_argument("--self-test", action="store_true", help="Run internal verification (debug)")
    args = parser.parse_args()
    
    if args.self_test:
        sys.exit(_run_self_test())
    
    if not args.project_dir or not args.config:
        parser.error("--project_dir and --config are required (or use --self-test)")
    
    try:
        config = json.loads(args.config)
        renderer = VideoRenderer(args.project_dir, config)
        output = renderer.render_video()
        print(f"\n__RENDER_SUCCESS__|{output}")
        sys.exit(0)
    except Exception as e:
        print(f"\n__RENDER_ERROR__|{str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
