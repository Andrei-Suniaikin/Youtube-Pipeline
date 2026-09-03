"""
Софт Trust The Process

🚀 НОВОЕ в v18.0:
- Автоопределение языка для EdgeTTS (langdetect) - 70+ языков
- Все мужские голоса EdgeTTS доступны в списке
- Кнопки массового запуска: "Озвучка для всех" и "Рендер для всех"
- Исправлено сохранение настроек вкладки "Вор" в пресеты

v17.0:
- Вкладка "Вор" - извлечение слайдов из YouTube видео
- Image-to-image трансформация через Googler AI
- Автоматический полный цикл: YouTube → Слайды → Трансформация → Проект

v16.2:
- Возможность закрывать проекты (кнопка + правый клик)
- Кнопка "Открыть папку проекта"
- Контекстное меню вкладок (ПКМ)
- Защита от закрытия последнего проекта
- Предупреждение при закрытии с активными процессами

v16.1:
- Интеграция Googler AI для генерации изображений
- Генерация промтов через GPT-4.1-nano
- Генерация видео из изображений через Googler AI
- Настройки Googler сохраняются в пресеты

v16.0:
- Интерфейс с вкладками вместо прокрутки
- 4 вкладки: Проект, Озвучка, Изображения, Рендер
"""

import sys
import os
import glob
import shutil
import subprocess
import tempfile
import threading
import queue
import json
import hashlib
import time
import re
import importlib
from dataclasses import dataclass
from datetime import datetime
from rotating_text_stream import RotatingTextStream
try:
    import server_client  # Фаза 2: серверный режим (если ~/video_studio_server.json enabled) — иначе None и всё локально
except Exception:
    server_client = None
try:
    import upload_manager  # автозалив YouTube (Channels, run_upload, редактор каналов)
except Exception:
    upload_manager = None
try:
    import chapters  # главы ролика по транскрипту рендера (таймкоды для описания)
except Exception:
    chapters = None

# ═══════════════ DEBUG LOG REDIRECT (опционально, не падает если не сработало) ═══════════════
try:
    _log_dir = os.path.join(
        os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
        'VideoStudio'
    )
    os.makedirs(_log_dir, exist_ok=True)
    _log_path = os.path.join(_log_dir, 'debug.log')
    _log_file = RotatingTextStream(
        _log_path,
        max_bytes=50 * 1024 * 1024,
        backup_count=2,
    )
    _log_file.write(f"\n\n========== START {datetime.now().isoformat()} ==========\n")
    sys.stdout = _log_file
    sys.stderr = _log_file
except Exception:
    pass  # fallback на default stdout если что-то пошло не так
# ═══════════════════════════════════════════════════════════════════════════════════════════

try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

# Импорт langdetect с обработкой всех возможных ошибок
# В onefile режиме Nuitka файлы langdetect могут не распаковаться корректно
# Используем максимально безопасный импорт с полной обработкой всех исключений
LANGDETECT_AVAILABLE = False
_detect_function = None

try:
    from langdetect import detect as _detect_function
    _detect_function("test")
    LANGDETECT_AVAILABLE = True
except Exception:
    LANGDETECT_AVAILABLE = False
    _detect_function = None

# Создаем безопасную функцию detect
def _warn_lang_fallback(reason):
    """Видимое предупреждение про откат языка на 'ru' (чтобы не озвучить чужой текст
    русским молча). Дублируем в logger_config-канал, если он доступен."""
    msg = f"⚠️ Язык не определён ({reason}) → озвучиваю как 'ru'. Проверь, что текст действительно русский."
    try:
        from logger_config import print_user as _pu
        _pu(msg, "WARNING")
    except Exception:
        print(msg, flush=True)


def detect(text):
    """Безопасная обертка для langdetect.detect - НИКОГДА не падает"""
    if LANGDETECT_AVAILABLE and _detect_function:
        try:
            return _detect_function(text)
        except Exception as e:
            # Если что-то пошло не так - возвращаем русский по умолчанию, но ВИДИМО
            _warn_lang_fallback(f"ошибка автодетекта: {str(e)[:60]}")
            return 'ru'
    # Если langdetect недоступен - возвращаем русский по умолчанию, но ВИДИМО
    _warn_lang_fallback("langdetect недоступен")
    return 'ru'

from tkinter import (
    Tk, Button, Label, END, filedialog, StringVar, Entry, messagebox,
    Checkbutton, BooleanVar, Frame, LabelFrame, Toplevel, Menu, Canvas, Text, Listbox
)
from tkinter.scrolledtext import ScrolledText
from tkinter import ttk
import tkinter as tk

from reference_contract import (
    HERO_ANGLES_MAX,
    HERO_GROUPS_MAX,
    MODEL_REFERENCE_CAPACITIES,
    PROJECT_REFERENCE_STATE_FILE,
    PROJECT_REFERENCE_STATE_POINTER_FILE,
    STYLE_SOURCE_MAX,
    ReferenceContractError,
    build_reference_bundle_manifest,
    import_reference_sources,
    load_project_reference_state,
    normalize_alias,
    replace_named_reference_group,
    save_project_reference_state,
)

# Revision history is additive to the reference contract.  Keep older desktop
# builds importable while reference_contract.py is upgraded in-place.
try:
    from reference_contract import (
        current_project_reference_revision,
        current_project_reference_state,
        list_project_reference_revisions,
        restore_project_reference_revision,
    )
except ImportError:
    current_project_reference_revision = None
    current_project_reference_state = None
    list_project_reference_revisions = None
    restore_project_reference_revision = None

try:
    import sv_ttk
    SV_TTK_AVAILABLE = True
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "sv-ttk", "-q"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              startupinfo=get_subprocess_flags())
        import sv_ttk
        SV_TTK_AVAILABLE = True
    except Exception:
        SV_TTK_AVAILABLE = False

# Импорт локализации
try:
    from i18n import _, get_language, set_language, get_available_languages
except ImportError:
    # Если модуль не найден, используем функцию-заглушку
    def _(key, *args, **kwargs):
        return key
    def get_language():
        return 'ru'
    def set_language(lang):
        pass
    def get_available_languages():
        return ['ru', 'en']

# --- НАСТРОЙКИ ---
# Импортируем модуль для определения путей portable Python
try:
    import portable_paths  # type: ignore[reportMissingImports]
    SCRIPTS_DIR = portable_paths.get_scripts_dir()
    PYTHON_EXE = portable_paths.get_python_executable()
    FFMPEG_EXE = portable_paths.get_ffmpeg_executable()
    YTDLP_EXE = portable_paths.get_ytdlp_executable()
    IS_PORTABLE = portable_paths.is_portable_mode()
except ImportError:
    # Если модуль не найден - используем стандартные пути
    SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
    PYTHON_EXE = sys.executable
    FFMPEG_EXE = "ffmpeg"
    YTDLP_EXE = "yt-dlp"
    IS_PORTABLE = False

def get_worker_command(worker_script, worker_args):
    """Собирает команду для запуска воркера. Работает с portable_paths (Windows) и без него (Mac/Linux)."""
    try:
        return portable_paths.get_worker_command(worker_script, worker_args)
    except (NameError, ImportError, AttributeError):
        script_path = os.path.join(SCRIPTS_DIR, worker_script)
        if not os.path.exists(script_path):
            return None
        return [PYTHON_EXE, script_path] + [str(a) for a in worker_args]

def get_subprocess_flags():
    """
    Возвращает флаги для subprocess.Popen для скрытия окон на Windows.
    Использует startupinfo для надежной работы с PyInstaller.
    
    Returns:
        subprocess.STARTUPINFO или None
    """
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return startupinfo
    return None


def get_subprocess_creationflags():
    """
    Возвращает creationflags для subprocess.Popen, чтобы дочерний процесс
    не создавал консольное окно (нет чёрного окна при запуске воркеров из exe).
    Только Windows.
    """
    if sys.platform == 'win32':
        return getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    return 0


def _make_mousewheel_scroll(scrollable, direction_mult=-1):
    """
    Возвращает обработчик прокрутки колёсиком мыши (Windows/Linux/Mac).
    scrollable — виджет с yview (Text, Canvas, Listbox).
    direction_mult: -1 для инвертированной прокрутки (как в десктопе).
    """
    def _scroll(event):
        if sys.platform == 'win32':
            # Windows: event.delta = 120 или -120
            delta = int(-1 * (event.delta / 120))
        elif sys.platform == 'darwin':
            # macOS: event.delta = 1 или -1
            delta = int(-1 * event.delta)
        else:
            # Linux: Button-4 (вверх), Button-5 (вниз)
            delta = -1 if event.num == 5 else 1
        scrollable.yview_scroll(direction_mult * delta, "units")
    return _scroll


def _bind_mousewheel_to_scrollable(widget, scrollable):
    """Привязывает прокрутку колёсиком к виджету (для Text/ScrolledText)."""
    handler = _make_mousewheel_scroll(scrollable)
    if sys.platform == 'win32':
        widget.bind("<MouseWheel>", handler)
    elif sys.platform == 'darwin':
        widget.bind("<MouseWheel>", handler)
    else:
        widget.bind("<Button-4>", handler)
        widget.bind("<Button-5>", handler)


def _bind_mousewheel_to_canvas_recursive(parent, canvas, exclude=None):
    """
    Рекурсивно привязывает прокрутку колёсиком к parent и всем дочерним виджетам.
    При наведении на любой из них будет прокручиваться canvas.
    exclude — множество виджетов, которые пропускаем (для них своя прокрутка, напр. лог).
    """
    exclude = exclude or set()
    if parent in exclude:
        return
    handler = _make_mousewheel_scroll(canvas)
    if sys.platform == 'win32':
        parent.bind("<MouseWheel>", handler)
    elif sys.platform == 'darwin':
        parent.bind("<MouseWheel>", handler)
    else:
        parent.bind("<Button-4>", handler)
        parent.bind("<Button-5>", handler)
    for child in parent.winfo_children():
        if child not in exclude:
            _bind_mousewheel_to_canvas_recursive(child, canvas, exclude)


def _worker_script_to_module(script_name):
    """Имя скрипта воркера -> имя модуля Python (без .py)."""
    return os.path.splitext(script_name)[0]


def _can_import_worker(script_name):
    """Проверяет, можно ли импортировать воркер (режим одного exe)."""
    mod_name = _worker_script_to_module(script_name)
    try:
        importlib.import_module(mod_name)
        return True
    except Exception:
        return False


def run_worker_inprocess(script_name, args_list, log_callback, kind="tts", stop_event=None):
    """
    Запускает воркер в текущем процессе (вывод в log_callback).
    Используется в режиме «один exe», когда воркеры встроены в основной exe.
    
    Args:
        stop_event: threading.Event — если установлен, воркер будет прерван при следующем выводе.
    
    Returns:
        (True, exit_code) если воркер запущен в процессе и завершился;
        (False, None) если воркер не найден как модуль — вызывающий код должен использовать subprocess.
    """
    # ВАЖНО: in-process держит ГЛОБАЛЬНЫЙ лок (общий sys.stdout/argv) → все воркеры сериализуются
    # по одному за раз. Локально (.py, НЕ собранный exe) запускаем воркеры СУБПРОЦЕССАМИ:
    # изоляция stdout/argv, без лока → картинки/видео/TTS разных проектов идут ПАРАЛЛЕЛЬНО.
    # В one-exe режиме (frozen) воркеры встроены модулями — оставляем in-process.
    if not getattr(sys, 'frozen', False):
        return (False, None)
    mod_name = _worker_script_to_module(script_name)
    try:
        mod = importlib.import_module(mod_name)
    except Exception:
        return (False, None)

    class LogRedirect:
        def __init__(self, callback, kind, stop_event=None):
            self._cb = callback
            self._kind = kind
            self._buf = ""
            self._stop_event = stop_event

        def write(self, s):
            if self._stop_event and self._stop_event.is_set():
                raise SystemExit("Остановлено пользователем")
            self._buf += s
            while "\n" in self._buf or "\r" in self._buf:
                line, rest = self._buf.split("\n", 1) if "\n" in self._buf else self._buf.split("\r", 1)
                self._buf = rest
                line = line.strip()
                if line:
                    self._cb(line, kind=self._kind)

        def flush(self):
            if self._stop_event and self._stop_event.is_set():
                raise SystemExit("Остановлено пользователем")
            if self._buf.strip():
                self._cb(self._buf.strip(), kind=self._kind)
                self._buf = ""

    _inprocess_lock = getattr(run_worker_inprocess, '_lock', None)
    if _inprocess_lock is None:
        run_worker_inprocess._lock = threading.Lock()
        _inprocess_lock = run_worker_inprocess._lock

    with _inprocess_lock:
        old_argv = sys.argv
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.argv = [script_name] + [str(a) for a in args_list]
            redirect = LogRedirect(log_callback, kind, stop_event=stop_event)
            sys.stdout = redirect
            sys.stderr = redirect
            mod.main()
            return (True, 0)
        except SystemExit as e:
            code = e.code if e.code is not None else 0
            if stop_event and stop_event.is_set():
                log_callback("⏹️ Воркер остановлен пользователем", kind=kind)
                return (True, -1)
            return (True, code)
        except Exception as e:
            if stop_event and stop_event.is_set():
                log_callback("⏹️ Воркер остановлен пользователем", kind=kind)
                return (True, -1)
            # Громко: тип ошибки + подсказка по pip для отсутствующего модуля,
            # а не голый {e} (по которому непонятно, что чинить).
            etype = type(e).__name__
            hint = ""
            if isinstance(e, ModuleNotFoundError) or isinstance(e, ImportError):
                missing_mod = getattr(e, 'name', None) or ""
                _PIP_HINT = {
                    'cv2': 'pip install opencv-python',
                    'PIL': 'pip install pillow',
                    'numpy': 'pip install numpy',
                    'requests': 'pip install requests',
                    'imagehash': 'pip install imagehash',
                    'langdetect': 'pip install langdetect',
                    'edge_tts': 'pip install edge-tts',
                    'whisper': 'pip install openai-whisper',
                    'icrawler': 'pip install icrawler',
                    'imageio_ffmpeg': 'pip install imageio-ffmpeg',
                }
                install_cmd = _PIP_HINT.get(missing_mod, f"pip install {missing_mod}" if missing_mod else "")
                if missing_mod:
                    hint = f" → {missing_mod}" + (f": {install_cmd}" if install_cmd else "")
            log_callback(f"❌ Ошибка воркера [{etype}]{hint}: {e}", kind=kind)
            import traceback
            log_callback(traceback.format_exc(), kind=kind)
            return (True, 1)
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout
            sys.stderr = old_stderr


def check_worker_exists(script_name):
    """
    Проверяет существование воркера (.py, .exe или встроенный модуль в одном exe).
    """
    # Режим одного exe: воркер может быть встроен как модуль
    if getattr(sys, "frozen", False) and _can_import_worker(script_name):
        return True
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    exe_path = script_path.replace(".py", ".exe")
    if os.path.exists(script_path):
        return True
    if os.path.exists(exe_path):
        return True
    base_name = os.path.splitext(script_name)[0]
    dist_exe = os.path.join(SCRIPTS_DIR, base_name + ".dist", base_name + ".exe")
    return os.path.exists(dist_exe)
# Определяем базовую директорию - пробуем диск D, если доступен, иначе домашняя папка
def get_base_dir():
    """Определяет базовую директорию - предпочтительно диск D"""
    if sys.platform == 'win32':
        # Пробуем диск D
        d_drive = "D:\\"
        if os.path.exists(d_drive) and os.access(d_drive, os.W_OK):
            return d_drive
    # Если диск D недоступен, используем домашнюю папку
    return os.path.expanduser("~")

BASE_DIR_ROOT = os.path.join(get_base_dir(), "video_projects")
BASE_SLIDES_DIR = os.path.join(get_base_dir(), "youtube_slides")  # НОВОЕ: отдельная папка для слайдов
TEMPLATE_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "tts_templates.json")
PRESETS_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "video_studio_presets.json")
API_KEYS_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "video_studio_api_keys.json")
RENDER_SETTINGS_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "video_studio_render_settings.json")
UI_SETTINGS_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "video_studio_ui_settings.json")

# Логируем, куда сохраняются файлы (без эмодзи для совместимости с кодировкой)
try:
    print(f"Базовая директория проектов: {BASE_DIR_ROOT}")
    print(f"Базовая директория слайдов: {BASE_SLIDES_DIR}")
except UnicodeEncodeError:
    # Если не удается вывести из-за кодировки - пропускаем
    pass

AUDIO_DIR_MAP = {
    'elevenlabs': "_FINAL_AUDIO_ELEVENLABS",
    'edge': "_FINAL_AUDIO_EDGETTS",
    'voicer': "_FINAL_AUDIO_VOICER",
    'voicegencore': "_FINAL_AUDIO_VGC"
}

# --- Гейт полноты озвучки (audio-completeness) ---
# Скорость речи ~12.5 симв/сек (тот же коэффициент, что и в googler_worker.SPEECH_CHARS_PER_SEC).
# Идея: суммарная длина озвученного аудио должна примерно соответствовать длине исходного текста.
# Если аудио сильно короче ожидаемого — озвучка обрезана/пропущены абзацы → НЕ отдаём в рендер.
AUDIO_GATE_CHARS_PER_SEC = 12.5
AUDIO_GATE_MIN_EXPECTED_SEC = 120.0   # не триггерим на реально коротких скриптах (<2 мин)
AUDIO_GATE_MIN_RATIO = 0.65           # аудио < 65% ожидаемого = неполное


def evaluate_audio_completeness(source_text, total_audio_sec,
                                chars_per_sec=AUDIO_GATE_CHARS_PER_SEC,
                                min_expected_sec=AUDIO_GATE_MIN_EXPECTED_SEC,
                                min_ratio=AUDIO_GATE_MIN_RATIO):
    """Чистая проверка: покрывает ли озвученное аудио исходный текст.

    Возвращает dict: {ok, expected_sec, actual_sec, ratio, pct, msg, checked}.
    ok=True — полно (или проверку невозможно/незачем делать: короткий текст, нет данных).
    checked=False — гейт не применялся (нет текста / короткий скрипт / нет длительности).
    """
    text = (source_text or "").strip()
    n_chars = len(text)
    expected_sec = n_chars / float(chars_per_sec) if chars_per_sec > 0 else 0.0
    try:
        actual = float(total_audio_sec) if total_audio_sec is not None else 0.0
    except (TypeError, ValueError):
        actual = 0.0
    ratio = (actual / expected_sec) if expected_sec > 0 else 1.0
    pct = int(round(ratio * 100))

    # Не применяем гейт: нет текста, короткий скрипт, или не смогли замерить аудио (actual<=0 при наличии текста
    # трактуем как «нет данных для проверки» — не блокируем на пустом замере).
    if n_chars == 0 or expected_sec < min_expected_sec or actual <= 0:
        return {"ok": True, "checked": False, "expected_sec": expected_sec,
                "actual_sec": actual, "ratio": ratio, "pct": pct, "msg": ""}

    if ratio < min_ratio:
        msg = (f"⚠️ Озвучка неполная: {int(round(actual))} сек аудио при ожидаемых "
               f"~{int(round(expected_sec))} сек по тексту (озвучено ~{pct}%). "
               f"Доозвучь проект, рендер остановлен.")
        return {"ok": False, "checked": True, "expected_sec": expected_sec,
                "actual_sec": actual, "ratio": ratio, "pct": pct, "msg": msg}

    return {"ok": True, "checked": True, "expected_sec": expected_sec,
            "actual_sec": actual, "ratio": ratio, "pct": pct, "msg": ""}

os.makedirs(BASE_DIR_ROOT, exist_ok=True)
os.makedirs(BASE_SLIDES_DIR, exist_ok=True)  # НОВОЕ: создаем папку для слайдов
IMAGE_DIR_NAME = "Images"
OUTPUT_FILENAME = "final_video.mp4"

# Максимум параллельных рендеров
MAX_PARALLEL_RENDERS = 3

# Максимум параллельных озвучек EdgeTTS
MAX_PARALLEL_EDGETTS = 3

# Максимум параллельных озвучек ElevenLabs (строгое ограничение API)
MAX_PARALLEL_ELEVENLABS = 1

# Максимум параллельных озвучек Voicer API (Mat3u)
MAX_PARALLEL_VOICER = 2
MAX_PARALLEL_VGC = 2

# Максимум параллельных процессов Thief (извлечение слайдов)
MAX_PARALLEL_THIEF = 2

# Максимум параллельных процессов Googler (генерация изображений/видео).
# ОДИН recrafter-ключ = один rate-limit. Картинки/видео обоих типов делят его, плюс воркер
# уже шлёт пачкой (image_threads) → один проект и так насыщает ключ. Параллель 2+ проектов
# даёт только 429-шторм без прироста скорости. Поэтому = 1 (проекты идут по очереди, на полной
# скорости ключа). Реальная параллель — между РАЗНЫМИ сервисами (recrafter ∥ TTS ∥ render).
# Для настоящей мульти-проектной параллели картинок нужен МУЛЬТИ-КЛЮЧ (round-robin).
MAX_PARALLEL_GOOGLER = 1
MAX_PARALLEL_GOOGLER_VIDEO = 1

# Список языков для субтитров (все из Edge TTS)
SUBTITLE_LANGUAGES = [
    'Afrikaans', 'Albanian', 'Amharic', 'Arabic', 'Armenian', 'Azerbaijani',
    'Bangla', 'Bosnian', 'Bulgarian', 'Burmese', 'Catalan', 'Chinese',
    'Croatian', 'Czech', 'Danish', 'Dutch', 'English', 'Estonian',
    'Filipino', 'Finnish', 'French', 'Galician', 'Georgian', 'German',
    'Greek', 'Gujarati', 'Hebrew', 'Hindi', 'Hungarian', 'Icelandic',
    'Indonesian', 'Irish', 'Italian', 'Japanese', 'Javanese', 'Kannada',
    'Kazakh', 'Khmer', 'Korean', 'Lao', 'Latvian', 'Lithuanian',
    'Macedonian', 'Malay', 'Malayalam', 'Maltese', 'Marathi', 'Mongolian',
    'Nepali', 'Norwegian', 'Pashto', 'Persian', 'Polish', 'Portuguese',
    'Romanian', 'Russian', 'Serbian', 'Sinhala', 'Slovak', 'Slovenian',
    'Somali', 'Spanish', 'Sundanese', 'Swahili', 'Swedish', 'Tamil',
    'Telugu', 'Thai', 'Turkish', 'Ukrainian', 'Urdu', 'Uzbek',
    'Vietnamese', 'Welsh', 'Zulu'
]


# Словарь соответствия кодов языков (langdetect) → названия языков для субтитров
LANG_CODE_TO_SUBTITLE = {
    'af': 'Afrikaans', 'sq': 'Albanian', 'am': 'Amharic', 'ar': 'Arabic',
    'hy': 'Armenian', 'az': 'Azerbaijani', 'bn': 'Bangla', 'bs': 'Bosnian',
    'bg': 'Bulgarian', 'my': 'Burmese', 'ca': 'Catalan', 'zh-cn': 'Chinese',
    'zh-tw': 'Chinese', 'zh-hk': 'Chinese', 'hr': 'Croatian', 'cs': 'Czech',
    'da': 'Danish', 'nl': 'Dutch', 'en': 'English', 'et': 'Estonian',
    'tl': 'Filipino', 'fi': 'Finnish', 'fr': 'French', 'gl': 'Galician',
    'ka': 'Georgian', 'de': 'German', 'el': 'Greek', 'gu': 'Gujarati',
    'he': 'Hebrew', 'hi': 'Hindi', 'hu': 'Hungarian', 'is': 'Icelandic',
    'id': 'Indonesian', 'ga': 'Irish', 'it': 'Italian', 'ja': 'Japanese',
    'jv': 'Javanese', 'kn': 'Kannada', 'kk': 'Kazakh', 'km': 'Khmer',
    'ko': 'Korean', 'lo': 'Lao', 'lv': 'Latvian', 'lt': 'Lithuanian',
    'mk': 'Macedonian', 'ms': 'Malay', 'ml': 'Malayalam', 'mt': 'Maltese',
    'mr': 'Marathi', 'mn': 'Mongolian', 'ne': 'Nepali', 'no': 'Norwegian',
    'ps': 'Pashto', 'fa': 'Persian', 'pl': 'Polish', 'pt': 'Portuguese',
    'ro': 'Romanian', 'ru': 'Russian', 'sr': 'Serbian', 'si': 'Sinhala',
    'sk': 'Slovak', 'sl': 'Slovenian', 'so': 'Somali', 'es': 'Spanish',
    'su': 'Sundanese', 'sw': 'Swahili', 'sv': 'Swedish', 'ta': 'Tamil',
    'te': 'Telugu', 'th': 'Thai', 'tr': 'Turkish', 'uk': 'Ukrainian',
    'ur': 'Urdu', 'uz': 'Uzbek', 'vi': 'Vietnamese', 'cy': 'Welsh',
    'zu': 'Zulu'
}


# Полный список ВСЕХ голосов Edge TTS (мужские и женские)
EDGE_ALL_VOICES = [
    # Африкаанс
    "af-ZA-AdriNeural", "af-ZA-WillemNeural",
    # Албанский
    "sq-AL-AnilaNeural", "sq-AL-IlirNeural",
    # Амхарский
    "am-ET-AmehaNeural", "am-ET-MekdesNeural",
    # Арабский
    "ar-AE-FatimaNeural", "ar-AE-HamdanNeural",
    "ar-BH-AliNeural", "ar-BH-LailaNeural",
    "ar-DZ-AminaNeural", "ar-DZ-IsmaelNeural",
    "ar-EG-SalmaNeural", "ar-EG-ShakirNeural",
    "ar-IQ-BasselNeural", "ar-IQ-RanaNeural",
    "ar-JO-SanaNeural", "ar-JO-TaimNeural",
    "ar-KW-FahedNeural", "ar-KW-NouraNeural",
    "ar-LB-LaylaNeural", "ar-LB-RamiNeural",
    "ar-LY-ImanNeural", "ar-LY-OmarNeural",
    "ar-MA-JamalNeural", "ar-MA-MounaNeural",
    "ar-OM-AbdullahNeural", "ar-OM-AyshaNeural",
    "ar-QA-AmalNeural", "ar-QA-MoazNeural",
    "ar-SA-HamedNeural", "ar-SA-ZariyahNeural",
    "ar-SY-AmanyNeural", "ar-SY-LaithNeural",
    "ar-TN-HediNeural", "ar-TN-ReemNeural",
    "ar-YE-MaryamNeural", "ar-YE-SalehNeural",
    # Армянский
    "hy-AM-AnahitNeural", "hy-AM-HaykNeural",
    # Азербайджанский
    "az-AZ-BabekNeural", "az-AZ-BanuNeural",
    # Бенгальский
    "bn-BD-NabanitaNeural", "bn-BD-PradeepNeural",
    "bn-IN-BashkarNeural", "bn-IN-TanishaaNeural",
    # Боснийский
    "bs-BA-GoranNeural", "bs-BA-VesnaNeural",
    # Болгарский
    "bg-BG-BorislavNeural", "bg-BG-KalinaNeural",
    # Бирманский
    "my-MM-NilarNeural", "my-MM-ThihaNeural",
    # Каталанский
    "ca-ES-AlbaNeural", "ca-ES-EnricNeural", "ca-ES-JoanaNeural",
    # Китайский
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-YunjianNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunyangNeural", "zh-CN-XiaochenNeural", "zh-CN-XiaohanNeural", "zh-CN-XiaomengNeural",
    "zh-CN-XiaomoNeural", "zh-CN-XiaoqiuNeural", "zh-CN-XiaoruiNeural", "zh-CN-XiaoshuangNeural",
    "zh-CN-XiaoxuanNeural", "zh-CN-XiaoyanNeural", "zh-CN-XiaoyouNeural", "zh-CN-XiaozhenNeural",
    "zh-CN-YunfengNeural", "zh-CN-YunhaoNeural", "zh-CN-YunxiaNeural", "zh-CN-YunyeNeural",
    "zh-CN-YunzeNeural",
    "zh-HK-HiuGaaiNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-HsiaoYuNeural", "zh-TW-YunJheNeural",
    # Хорватский
    "hr-HR-GabrijelaNeural", "hr-HR-SreckoNeural",
    # Чешский
    "cs-CZ-AntoninNeural", "cs-CZ-VlastaNeural",
    # Датский
    "da-DK-ChristelNeural", "da-DK-JeppeNeural",
    # Голландский
    "nl-BE-ArnaudNeural", "nl-BE-DenaNeural",
    "nl-NL-ColetteNeural", "nl-NL-FennaNeural", "nl-NL-MaartenNeural",
    # Английский
    "en-AU-NatashaNeural", "en-AU-WilliamNeural",
    "en-CA-ClaraNeural", "en-CA-LiamNeural",
    "en-GB-LibbyNeural", "en-GB-MiaNeural", "en-GB-RyanNeural", "en-GB-SoniaNeural", "en-GB-ThomasNeural",
    "en-HK-SamNeural", "en-HK-YanNeural",
    "en-IE-ConnorNeural", "en-IE-EmilyNeural",
    "en-IN-NeerjaNeural", "en-IN-PrabhatNeural",
    "en-KE-AsiliaNeural", "en-KE-ChilembaNeural",
    "en-NG-AbeoNeural", "en-NG-EzinneNeural",
    "en-NZ-MitchellNeural", "en-NZ-MollyNeural",
    "en-PH-JamesNeural", "en-PH-RosaNeural",
    "en-SG-LunaNeural", "en-SG-WayneNeural",
    "en-TZ-ElimuNeural", "en-TZ-ImaniNeural",
    "en-US-AriaNeural", "en-US-AnaNeural", "en-US-ChristopherNeural", "en-US-EricNeural",
    "en-US-GuyNeural", "en-US-JennyNeural", "en-US-MichelleNeural", "en-US-RogerNeural",
    "en-US-SteffanNeural",
    "en-ZA-LeahNeural", "en-ZA-LukeNeural",
    # Эстонский
    "et-EE-AnuNeural", "et-EE-KertNeural",
    # Филиппинский
    "fil-PH-AngeloNeural", "fil-PH-BlessicaNeural",
    # Финский
    "fi-FI-HarriNeural", "fi-FI-NooraNeural", "fi-FI-SelmaNeural",
    # Французский
    "fr-BE-CharlineNeural", "fr-BE-GerardNeural",
    "fr-CA-AntoineNeural", "fr-CA-JeanNeural", "fr-CA-SylvieNeural",
    "fr-CH-ArianeNeural", "fr-CH-FabriceNeural",
    "fr-FR-DeniseNeural", "fr-FR-EloiseNeural", "fr-FR-HenriNeural",
    # Галисийский
    "gl-ES-RoiNeural", "gl-ES-SabelaNeural",
    # Грузинский
    "ka-GE-EkaNeural", "ka-GE-GiorgiNeural",
    # Немецкий
    "de-AT-IngridNeural", "de-AT-JonasNeural",
    "de-CH-JanNeural", "de-CH-LeniNeural",
    "de-DE-AmalaNeural", "de-DE-ConradNeural", "de-DE-KatjaNeural", "de-DE-KillianNeural",
    # Греческий
    "el-GR-AthinaNeural", "el-GR-NestorasNeural",
    # Гуджарати
    "gu-IN-DhwaniNeural", "gu-IN-NiranjanNeural",
    # Иврит
    "he-IL-AvriNeural", "he-IL-HilaNeural",
    # Хинди
    "hi-IN-MadhurNeural", "hi-IN-SwaraNeural",
    # Венгерский
    "hu-HU-NoemiNeural", "hu-HU-TamasNeural",
    # Исландский
    "is-IS-GudrunNeural", "is-IS-GunnarNeural",
    # Индонезийский
    "id-ID-ArdiNeural", "id-ID-GadisNeural",
    # Ирландский
    "ga-IE-ColmNeural", "ga-IE-OrlaNeural",
    # Итальянский
    "it-IT-DiegoNeural", "it-IT-ElsaNeural", "it-IT-IsabellaNeural",
    # Японский
    "ja-JP-KeitaNeural", "ja-JP-NanamiNeural",
    # Яванский
    "jv-ID-DimasNeural", "jv-ID-SitiNeural",
    # Каннада
    "kn-IN-GaganNeural", "kn-IN-SapnaNeural",
    # Казахский
    "kk-KZ-AigulNeural", "kk-KZ-DauletNeural",
    # Кхмерский
    "km-KH-PisethNeural", "km-KH-SreymomNeural",
    # Корейский
    "ko-KR-InJoonNeural", "ko-KR-SunHiNeural",
    # Лаосский
    "lo-LA-ChanthavongNeural", "lo-LA-KeomanyNeural",
    # Латышский
    "lv-LV-EveritaNeural", "lv-LV-NilsNeural",
    # Литовский
    "lt-LT-LeonasNeural", "lt-LT-OnaNeural",
    # Македонский
    "mk-MK-AleksandarNeural", "mk-MK-MarijaNeural",
    # Малайский
    "ms-MY-OsmanNeural", "ms-MY-YasminNeural",
    # Малаялам
    "ml-IN-MidhunNeural", "ml-IN-SobhanaNeural",
    # Мальтийский
    "mt-MT-GraceNeural", "mt-MT-JosephNeural",
    # Маратхи
    "mr-IN-AarohiNeural", "mr-IN-ManoharNeural",
    # Монгольский
    "mn-MN-BataaNeural", "mn-MN-YesuiNeural",
    # Непальский
    "ne-NP-HemkalaNeural", "ne-NP-SagarNeural",
    # Норвежский
    "nb-NO-FinnNeural", "nb-NO-PernilleNeural",
    # Пушту
    "ps-AF-GulNawazNeural", "ps-AF-LatifaNeural",
    # Персидский
    "fa-IR-DilaraNeural", "fa-IR-FaridNeural",
    # Польский
    "pl-PL-MarekNeural", "pl-PL-ZofiaNeural",
    # Португальский
    "pt-BR-AntonioNeural", "pt-BR-FranciscaNeural",
    "pt-PT-DuarteNeural", "pt-PT-RaquelNeural",
    # Румынский
    "ro-RO-AlinaNeural", "ro-RO-EmilNeural",
    # Русский
    "ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural",
    # Сербский
    "sr-RS-NicholasNeural", "sr-RS-SophieNeural",
    # Сингальский
    "si-LK-SameeraNeural", "si-LK-ThiliniNeural",
    # Словацкий
    "sk-SK-LukasNeural", "sk-SK-ViktoriaNeural",
    # Словенский
    "sl-SI-PetraNeural", "sl-SI-RokNeural",
    # Сомалийский
    "so-SO-MuuseNeural", "so-SO-UbaxNeural",
    # Испанский
    "es-AR-ElenaNeural", "es-AR-TomasNeural",
    "es-BO-MarceloNeural", "es-BO-SofiaNeural",
    "es-CL-CatalinaNeural", "es-CL-LorenzoNeural",
    "es-CO-GonzaloNeural", "es-CO-SalomeNeural",
    "es-CR-JuanNeural", "es-CR-MariaNeural",
    "es-CU-BelkysNeural", "es-CU-ManuelNeural",
    "es-DO-EmilioNeural", "es-DO-RamonaNeural",
    "es-EC-AndreaNeural", "es-EC-LuisNeural",
    "es-ES-AlvaroNeural", "es-ES-ElviraNeural",
    "es-GQ-JavierNeural", "es-GQ-TeresaNeural",
    "es-GT-AndresNeural", "es-GT-MartaNeural",
    "es-HN-CarlosNeural", "es-HN-KarlaNeural",
    "es-MX-DaliaNeural", "es-MX-JorgeNeural",
    "es-NI-FedericoNeural", "es-NI-YolandaNeural",
    "es-PA-MargaritaNeural", "es-PA-RobertoNeural",
    "es-PE-AlexNeural", "es-PE-CamilaNeural",
    "es-PR-KarinaNeural", "es-PR-VictorNeural",
    "es-PY-MarioNeural", "es-PY-TaniaNeural",
    "es-SV-LorenaNeural", "es-SV-RodrigoNeural",
    "es-US-AlonsoNeural", "es-US-PalomaNeural",
    "es-UY-MateoNeural", "es-UY-ValentinaNeural",
    "es-VE-PaolaNeural", "es-VE-SebastianNeural",
    # Сунданский
    "su-ID-JajangNeural", "su-ID-TutiNeural",
    # Суахили
    "sw-KE-RafikiNeural", "sw-KE-ZuriNeural",
    "sw-TZ-DaudiNeural", "sw-TZ-RehemaNeural",
    # Шведский
    "sv-SE-MattiasNeural", "sv-SE-SofieNeural",
    # Тамильский
    "ta-IN-PallaviNeural", "ta-IN-ValluvarNeural",
    "ta-LK-KumarNeural", "ta-LK-SaranyaNeural",
    "ta-MY-KaniNeural", "ta-MY-SuryaNeural",
    "ta-SG-AnbuNeural", "ta-SG-VenbaNeural",
    # Телугу
    "te-IN-MohanNeural", "te-IN-ShrutiNeural",
    # Тайский
    "th-TH-NiwatNeural", "th-TH-PremwadeeNeural",
    # Турецкий
    "tr-TR-AhmetNeural", "tr-TR-EmelNeural",
    # Украинский
    "uk-UA-OstapNeural", "uk-UA-PolinaNeural",
    # Урду
    "ur-IN-GulNeural", "ur-IN-SalmanNeural",
    "ur-PK-AsadNeural", "ur-PK-UzmaNeural",
    # Узбекский
    "uz-UZ-MadinaNeural", "uz-UZ-SardorNeural",
    # Вьетнамский
    "vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural",
    # Уэльский
    "cy-GB-AledNeural", "cy-GB-NiaNeural",
    # Зулу
    "zu-ZA-ThandoNeural", "zu-ZA-ThembaNeural"
]


# ============= АВТООПРЕДЕЛЕНИЕ ЯЗЫКА ДЛЯ EDGE TTS =============
# Mapping: язык (код langdetect) -> мужской голос EdgeTTS
EDGE_MALE_VOICES_MAP = {
    'af': 'af-ZA-WillemNeural', 'sq': 'sq-AL-IlirNeural', 'am': 'am-ET-AmehaNeural',
    'ar': 'ar-SA-HamedNeural', 'hy': 'hy-AM-HaykNeural', 'az': 'az-AZ-BabekNeural',
    'bn': 'bn-IN-BashkarNeural', 'bs': 'bs-BA-GoranNeural', 'bg': 'bg-BG-BorislavNeural',
    'my': 'my-MM-ThihaNeural', 'ca': 'ca-ES-EnricNeural', 'zh-cn': 'zh-CN-YunxiNeural',
    'zh-tw': 'zh-TW-YunJheNeural', 'zh-hk': 'zh-HK-WanLungNeural', 'hr': 'hr-HR-SreckoNeural',
    'cs': 'cs-CZ-AntoninNeural', 'da': 'da-DK-JeppeNeural', 'nl': 'nl-NL-MaartenNeural',
    'en': 'en-US-GuyNeural', 'et': 'et-EE-KertNeural', 'tl': 'fil-PH-AngeloNeural',
    'fi': 'fi-FI-HarriNeural', 'fr': 'fr-FR-HenriNeural', 'gl': 'gl-ES-RoiNeural',
    'ka': 'ka-GE-GiorgiNeural', 'de': 'de-DE-ConradNeural', 'el': 'el-GR-NestorasNeural',
    'gu': 'gu-IN-NiranjanNeural', 'he': 'he-IL-AvriNeural', 'hi': 'hi-IN-MadhurNeural',
    'hu': 'hu-HU-TamasNeural', 'is': 'is-IS-GunnarNeural', 'id': 'id-ID-ArdiNeural',
    'ga': 'ga-IE-ColmNeural', 'it': 'it-IT-DiegoNeural', 'ja': 'ja-JP-KeitaNeural',
    'jv': 'jv-ID-DimasNeural', 'kn': 'kn-IN-GaganNeural', 'kk': 'kk-KZ-DauletNeural',
    'km': 'km-KH-PisethNeural', 'ko': 'ko-KR-InJoonNeural', 'lo': 'lo-LA-ChanthavongNeural',
    'lv': 'lv-LV-NilsNeural', 'lt': 'lt-LT-LeonasNeural', 'mk': 'mk-MK-AleksandarNeural',
    'ms': 'ms-MY-OsmanNeural', 'ml': 'ml-IN-MidhunNeural', 'mt': 'mt-MT-JosephNeural',
    'mr': 'mr-IN-ManoharNeural', 'mn': 'mn-MN-BataaNeural', 'ne': 'ne-NP-SagarNeural',
    'no': 'nb-NO-FinnNeural', 'ps': 'ps-AF-GulNawazNeural', 'fa': 'fa-IR-FaridNeural',
    'pl': 'pl-PL-MarekNeural', 'pt': 'pt-BR-AntonioNeural', 'ro': 'ro-RO-EmilNeural',
    'ru': 'ru-RU-DmitryNeural', 'sr': 'sr-RS-NicholasNeural', 'si': 'si-LK-SameeraNeural',
    'sk': 'sk-SK-LukasNeural', 'sl': 'sl-SI-RokNeural', 'so': 'so-SO-MuuseNeural',
    'es': 'es-ES-AlvaroNeural', 'su': 'su-ID-JajangNeural', 'sw': 'sw-KE-RafikiNeural',
    'sv': 'sv-SE-MattiasNeural', 'ta': 'ta-IN-ValluvarNeural', 'te': 'te-IN-MohanNeural',
    'th': 'th-TH-NiwatNeural', 'tr': 'tr-TR-AhmetNeural', 'uk': 'uk-UA-OstapNeural',
    'ur': 'ur-PK-AsadNeural', 'uz': 'uz-UZ-SardorNeural', 'vi': 'vi-VN-NamMinhNeural',
    'cy': 'cy-GB-AledNeural', 'zu': 'zu-ZA-ThembaNeural'
}

# Полный список всех мужских голосов для комбобокса
EDGE_ALL_MALE_VOICES = sorted(set(EDGE_MALE_VOICES_MAP.values()))


def detect_language_and_get_voice(text):
    """
    Определяет язык текста и возвращает соответствующий мужской голос EdgeTTS.
    Если langdetect не установлен или язык не определен - возвращает русский голос.
    """
    if not LANGDETECT_AVAILABLE:
        return 'ru-RU-DmitryNeural'
    
    try:
        # Берем первые 500 символов для определения языка
        sample = text[:500] if len(text) > 500 else text
        lang_code = detect(sample)
        
        # Ищем соответствующий голос
        voice = EDGE_MALE_VOICES_MAP.get(lang_code)
        if voice:
            return voice
        else:
            # Если точного совпадения нет, пробуем без региона
            if '-' in lang_code:
                base_lang = lang_code.split('-')[0]
                voice = EDGE_MALE_VOICES_MAP.get(base_lang)
                if voice:
                    return voice
            
            # Если ничего не найдено - русский по умолчанию
            return 'ru-RU-DmitryNeural'
    except Exception as e:
        print(f"Ошибка определения языка: {e}")
        return 'ru-RU-DmitryNeural'


def detect_language_from_file(filepath):
    """
    Определяет язык из текстового файла и возвращает:
    - код языка (для langdetect)
    - голос EdgeTTS
    - язык субтитров
    """
    if not LANGDETECT_AVAILABLE:
        return 'ru', 'ru-RU-DmitryNeural', 'Russian'
    
    try:
        # Читаем файл
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        
        # Определяем язык
        sample = text[:500] if len(text) > 500 else text
        lang_code = detect(sample)
        
        # Получаем голос
        voice = EDGE_MALE_VOICES_MAP.get(lang_code)
        if not voice:
            # Пробуем без региона
            if '-' in lang_code:
                base_lang = lang_code.split('-')[0]
                voice = EDGE_MALE_VOICES_MAP.get(base_lang)
            if not voice:
                voice = 'ru-RU-DmitryNeural'
                lang_code = 'ru'
        
        # Получаем язык субтитров
        subtitle_lang = LANG_CODE_TO_SUBTITLE.get(lang_code, 'Russian')
        
        return lang_code, voice, subtitle_lang
        
    except Exception as e:
        print(f"Ошибка определения языка из файла: {e}")
        return 'ru', 'ru-RU-DmitryNeural', 'Russian'


# --- МЕНЕДЖЕР ОЧЕРЕДИ РЕНДЕРИНГА ---
class RenderQueueManager:
    """Управление очередью рендеринга с ограничением параллельных процессов."""
    
    def __init__(self, max_parallel=MAX_PARALLEL_RENDERS, queue_name=""):
        self.max_parallel = max_parallel
        self.queue_name = queue_name
        self.active_renders = {}  # {project_name: process}
        self.queue = []  # [(project_name, callback), ...]
        self.lock = threading.Lock()
        self.on_complete_callback = None  # вызывается при завершении задачи: (project_name) -> None
    
    def get_status(self):
        """Получить статус очереди."""
        with self.lock:
            active_count = len(self.active_renders)
            queued_count = len(self.queue)
            return {
                'active': active_count,
                'queued': queued_count,
                'max': self.max_parallel,
                'can_start': active_count < self.max_parallel
            }
    
    def get_active_list(self):
        """Список проектов, которые сейчас рендерятся."""
        with self.lock:
            return list(self.active_renders.keys())
    
    def get_queue_list(self):
        """Список проектов в очереди (в порядке выполнения)."""
        with self.lock:
            return [name for name, _ in self.queue]
    
    def remove_from_queue(self, project_name):
        """Удалить проект из очереди (не влияет на уже запущенные)."""
        with self.lock:
            new_queue = [(n, f) for n, f in self.queue if n != project_name]
            if len(new_queue) == len(self.queue):
                return False
            self.queue = new_queue
            return True
    
    def move_up(self, project_name):
        """Поднять проект в очереди на одну позицию."""
        with self.lock:
            idx = next((i for i, (n, _) in enumerate(self.queue) if n == project_name), None)
            if idx is None or idx == 0:
                return False
            self.queue[idx], self.queue[idx - 1] = self.queue[idx - 1], self.queue[idx]
            return True
    
    def move_down(self, project_name):
        """Опустить проект в очереди на одну позицию."""
        with self.lock:
            idx = next((i for i, (n, _) in enumerate(self.queue) if n == project_name), None)
            if idx is None or idx >= len(self.queue) - 1:
                return False
            self.queue[idx], self.queue[idx + 1] = self.queue[idx + 1], self.queue[idx]
            return True
    
    def _cleanup_dead(self):
        """Удаляет завершённые/мёртвые потоки из active_renders (защита от зомби)."""
        dead = [name for name, thread in self.active_renders.items()
                if not thread.is_alive()]
        for name in dead:
            del self.active_renders[name]

    def add_to_queue(self, project_name, render_func):
        """Добавить рендер в очередь."""
        with self.lock:
            self._cleanup_dead()
            if project_name in self.active_renders:
                return False, f"[{self.queue_name}] Задача уже запущена: {project_name}"
            for queued_name, _ in self.queue:
                if queued_name == project_name:
                    return False, f"[{self.queue_name}] Уже в очереди: {project_name}"
            if len(self.active_renders) < self.max_parallel:
                self._start_render(project_name, render_func)
                return True, f"[{self.queue_name}] Запущено: {project_name}"
            else:
                self.queue.append((project_name, render_func))
                return True, f"[{self.queue_name}] В очереди (позиция {len(self.queue)})"
    
    def _start_render(self, project_name, render_func):
        """Запустить рендер в отдельном потоке."""
        def render_wrapper():
            try:
                render_func()
            finally:
                self._on_render_complete(project_name)
        
        thread = threading.Thread(target=render_wrapper, daemon=True)
        self.active_renders[project_name] = thread
        thread.start()
    
    def _on_render_complete(self, project_name):
        """Обработка завершения рендера."""
        cb = None
        with self.lock:
            if project_name in self.active_renders:
                del self.active_renders[project_name]
            cb = self.on_complete_callback
            if self.queue and len(self.active_renders) < self.max_parallel:
                next_project, next_func = self.queue.pop(0)
                self._start_render(next_project, next_func)
        if cb:
            try:
                cb(project_name)
            except Exception:
                pass

    def notify_stopped(self, project_name):
        """Public API (Fix 5 May 2026): после внешнего kill процесса — убрать из active
        и стартовать следующий из очереди. Раньше stop_render лез напрямую в lock+del,
        не запуская next из queue → очередь зависала после Cancel.
        """
        cb = None
        with self.lock:
            if project_name in self.active_renders:
                del self.active_renders[project_name]
            cb = self.on_complete_callback
            if self.queue and len(self.active_renders) < self.max_parallel:
                next_project, next_func = self.queue.pop(0)
                self._start_render(next_project, next_func)
        if cb:
            try:
                cb(project_name)
            except Exception:
                pass


class TTSQueueManager:
    """Управление очередью озвучки с ограничением параллельных процессов."""
    
    def __init__(self, max_parallel=MAX_PARALLEL_EDGETTS, queue_name=""):
        self.max_parallel = max_parallel
        self.queue_name = queue_name
        self.active_tts = {}  # {project_name: thread}
        self.queue = []  # [(project_name, callback), ...]
        self.lock = threading.Lock()
        self.on_complete_callback = None  # вызывается при завершении задачи: (project_name) -> None
    
    def get_status(self):
        """Получить статус очереди."""
        with self.lock:
            active_count = len(self.active_tts)
            queued_count = len(self.queue)
            return {
                'active': active_count,
                'queued': queued_count,
                'max': self.max_parallel,
                'can_start': active_count < self.max_parallel
            }
    
    def get_active_list(self):
        with self.lock:
            return list(self.active_tts.keys())
    
    def get_queue_list(self):
        with self.lock:
            return [name for name, _ in self.queue]
    
    def remove_from_queue(self, project_name):
        with self.lock:
            new_queue = [(n, f) for n, f in self.queue if n != project_name]
            if len(new_queue) == len(self.queue):
                return False
            self.queue = new_queue
            return True
    
    def move_up(self, project_name):
        with self.lock:
            idx = next((i for i, (n, _) in enumerate(self.queue) if n == project_name), None)
            if idx is None or idx == 0:
                return False
            self.queue[idx], self.queue[idx - 1] = self.queue[idx - 1], self.queue[idx]
            return True
    
    def move_down(self, project_name):
        with self.lock:
            idx = next((i for i, (n, _) in enumerate(self.queue) if n == project_name), None)
            if idx is None or idx >= len(self.queue) - 1:
                return False
            self.queue[idx], self.queue[idx + 1] = self.queue[idx + 1], self.queue[idx]
            return True
    
    def _cleanup_dead(self):
        """Удаляет завершённые/мёртвые потоки из active_tts (защита от зомби)."""
        dead = [name for name, thread in self.active_tts.items()
                if not thread.is_alive()]
        for name in dead:
            del self.active_tts[name]

    def add_to_queue(self, project_name, tts_func):
        """Добавить задачу в очередь."""
        with self.lock:
            self._cleanup_dead()
            if project_name in self.active_tts:
                return False, f"[{self.queue_name}] Задача уже запущена: {project_name}"
            for queued_name, _ in self.queue:
                if queued_name == project_name:
                    return False, f"[{self.queue_name}] Уже в очереди: {project_name}"
            if len(self.active_tts) < self.max_parallel:
                self._start_tts(project_name, tts_func)
                return True, f"[{self.queue_name}] Запущено: {project_name}"
            else:
                self.queue.append((project_name, tts_func))
                return True, f"[{self.queue_name}] В очереди (позиция {len(self.queue)})"
    
    def _start_tts(self, project_name, tts_func):
        """Запустить озвучку в отдельном потоке."""
        def tts_wrapper():
            try:
                tts_func()
            finally:
                self._on_tts_complete(project_name)
        
        thread = threading.Thread(target=tts_wrapper, daemon=True)
        self.active_tts[project_name] = thread
        thread.start()
    
    def _on_tts_complete(self, project_name):
        """Обработка завершения озвучки."""
        cb = None
        with self.lock:
            if project_name in self.active_tts:
                del self.active_tts[project_name]
            cb = self.on_complete_callback
            if self.queue and len(self.active_tts) < self.max_parallel:
                next_project, next_func = self.queue.pop(0)
                self._start_tts(next_project, next_func)
        if cb:
            try:
                cb(project_name)
            except Exception:
                pass


# --- СВОРАЧИВАЕМЫЕ БЛОКИ ---
class CollapsibleFrame(Frame):
    def __init__(self, parent, text=""):
        super().__init__(parent)
        self.columnconfigure(1, weight=1)
        self.text = text
        self.toggle_button = Button(self, text=f"[-] {self.text}", width=1, command=self.toggle, relief="flat", anchor="w")
        self.toggle_button.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.sub_frame = Frame(self, relief="sunken", borderwidth=1)
        self.sub_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.is_expanded = True

    def toggle(self):
        self.is_expanded = not self.is_expanded
        if self.is_expanded:
            self.sub_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
            self.toggle_button.configure(text=f"[-] {self.text}")
        else:
            self.sub_frame.grid_forget()
            self.toggle_button.configure(text=f"[+] {self.text}")


# --- МЕНЕДЖЕР API КЛЮЧЕЙ ---
class APIKeysManager:
    def __init__(self, config_path=API_KEYS_CONFIG_PATH):
        self.config_path = config_path
        self.keys = self.load_keys()
    
    def load_keys(self):
        """Загружает API ключи из файла"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8', errors='replace') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_keys(self):
        """Сохраняет API ключи в файл"""
        try:
            with open(self.config_path, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(self.keys, f, indent=2, ensure_ascii=False)
            # Обновляем переменные окружения для текущей сессии
            self.update_env_vars()
            return True
        except Exception as e:
            print(f"Ошибка сохранения ключей: {e}")
            return False
    
    def update_env_vars(self):
        """Обновляет переменные окружения из сохраненных ключей"""
        if 'googler' in self.keys:
            os.environ['GOOGLER_API_KEY'] = self.keys['googler']
        if 'openai' in self.keys:
            os.environ['OPENAI_API_KEY'] = self.keys['openai']
        if 'elevenlabs' in self.keys:
            os.environ['ELEVENLABS_API_KEY'] = self.keys['elevenlabs']
        if 'voicer' in self.keys:
            os.environ['VOICER_API_TOKEN'] = self.keys['voicer']
        if 'voicegencore' in self.keys:
            os.environ['VGC_API_TOKEN'] = self.keys['voicegencore']
        if 'veo' in self.keys:
            os.environ['VEO_API_KEY'] = self.keys['veo']

    def get_key(self, service):
        """Получить ключ для сервиса"""
        return self.keys.get(service, '')
    
    def set_key(self, service, key):
        """Установить ключ для сервиса"""
        self.keys[service] = key
        return self.save_keys()


# --- МЕНЕДЖЕР НАСТРОЕК РЕНДЕРА ---
class RenderSettingsManager:
    """Управление настройками рендера для оптимизации производительности"""
    def __init__(self, config_path=RENDER_SETTINGS_CONFIG_PATH):
        self.config_path = config_path
        self.settings = self.load_settings()
    
    def load_settings(self):
        """Загружает настройки рендера из файла"""
        default_settings = {
            # Принудительный выбор кодировщика (None = авто, 'libx264', 'h264_nvenc', 'h264_videotoolbox', 'h264_vaapi')
            'force_encoder': '',  # пустая строка = авто
            # Принудительно использовать только CPU (игнорировать GPU)
            'force_cpu': False,
            # CPU пресеты libx264: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
            'cpu_preset': 'fast',  # Для максимальной скорости используем 'fast' или 'veryfast'
            # GPU пресеты
            'nvenc_preset': 'p4',  # p1 (быстро) - p7 (качество)
            'videotoolbox_preset': 'fast',  # fast, medium, slow
            'vaapi_preset': 'fast',
            'vaapi_device': '',  # путь к VAAPI устройству, например /dev/dri/renderD128
            # Качество (CRF): 18 (лучшее) - 28 (худшее, но быстрее)
            'crf': 23,  # Баланс между качеством и скоростью
            'max_bitrate_mbps': 8,  # NVENC: макс. битрейт (5 мин ~300 MB), 0 = без лимита
            # Размер батча для больших проектов
            'batch_size_small': 20,  # <500 картинок
            'batch_size_medium': 18,  # 500-1000 картинок
            'batch_size_large': 15,  # >1000 картинок
            # Количество потоков (0 = авто)
            'threads': 0,
            # Таймаут в секундах
            'timeout_seconds': 14400,  # 4 часа
            # Приоритет процесса (Windows: ниже/нормальный/выше, Unix: nice value)
            'process_priority': 'normal',  # low, normal, high
            # Параллельных FFmpeg в одном рендере (0 = авто: GPU 6, CPU до 12)
            'max_pool_workers': 0,
            # Макс. параллельных рендеров до постановки в очередь (по умолчанию 3)
            'max_parallel_renders': 3
        }
        
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8', errors='replace') as f:
                    loaded = json.load(f)
                    # Объединяем с дефолтными (на случай если добавились новые параметры)
                    default_settings.update(loaded)
                    return default_settings
            except:
                return default_settings
        return default_settings
    
    def save_settings(self):
        """Сохраняет настройки рендера в файл"""
        try:
            with open(self.config_path, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"Ошибка сохранения настроек рендера: {e}")
            return False
    
    def get_setting(self, key, default=None):
        """Получить значение настройки"""
        return self.settings.get(key, default)
    
    def set_setting(self, key, value):
        """Установить значение настройки"""
        self.settings[key] = value
        return self.save_settings()


# --- МЕНЕДЖЕР ПРЕСЕТОВ ---
class PresetManager:
    def __init__(self, config_path=PRESETS_CONFIG_PATH):
        self.config_path = config_path
        self.presets = self.load_presets()
    
    def load_presets(self):
        """Загружает пресеты из файла. Если файла нет - создает с дефолтными пресетами."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8', errors='replace') as f:
                    loaded = json.load(f)
                    # Если файл пустой или поврежден - используем дефолты
                    if not isinstance(loaded, dict):
                        loaded = {}
                    # Если есть пресеты - возвращаем их
                    if loaded:
                        return loaded
            except:
                pass
        
        # Если файла нет или он пустой - создаем дефолтные пресеты
        default_presets = self.get_default_presets()
        if default_presets:
            try:
                # Создаем директорию если не существует
                os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
                # Сохраняем дефолтные пресеты
                with open(self.config_path, 'w', encoding='utf-8', errors='replace') as f:
                    json.dump(default_presets, f, indent=2, ensure_ascii=False)
            except:
                pass
        return default_presets
    
    def get_default_presets(self):
        """Возвращает дефолтные пресеты (можно настроить под ваши нужды)"""
        return {
            # Пример дефолтного пресета - можно добавить свои
            # "Default": {
            #     "tts_engine": "edge",
            #     "selected_edge_voice": "ru-RU-DmitryNeural",
            #     "video_quality": "720p",
            #     "max_image_duration": "5.0",
            #     "subtitle_language": "Russian",
            #     "auto_detect_language": True
            # }
        }
    
    def save_presets(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(self.presets, f, indent=2, ensure_ascii=False)
            return True
        except:
            return False
    
    def add_preset(self, name, settings):
        self.presets[name] = settings
        return self.save_presets()
    
    def get_preset(self, name):
        return self.presets.get(name, None)
    
    def delete_preset(self, name):
        if name in self.presets:
            del self.presets[name]
            return self.save_presets()
        return False
    
    def get_preset_names(self):
        return sorted(self.presets.keys())


# Модели генерации изображений: внутренний ID → понятное название
# Источник: https://googler.fast-gen.ai/openapi.json (FastGen v1.0.0). Полный набор провайдеров.
GOOGLER_IMAGE_MODELS = [
    # Проверено live 2026-06-05 (research/IMAGE_GEN_TEST_2026-06-05.md):
    #   GEMINI убран — провайдера нет, /v4/gemini/image/generate = 404.
    #   GEM_PIX_2/IMAGEN_3_5 возвращены — живы через /v4/flow/image/generate (generate-only).
    #   WHISK_IMAGEN4 не возвращён — whisk-провайдера нет.
    # Порядок = порядок в дропдауне; дефолт OPENAI (1 кредит) первым.
    ("OPENAI",        "ChatGPT Image"),                 # 1 кредит, дефолт. gen + transform
    ("FLOWER",        "Nano Banana 2 (Flower)"),        # 1 кредит. gen + transform
    ("GROK",          "Grok Quality"),                  # 3 кредита. generate-only (edit = анти-бот)
    ("GROK_SPEED",    "Grok Speed"),                    # 1 кредит. generate-only
    ("NARWHAL",       "Nano Banana 2 (Flow)"),          # 4 кредита. generate-only
    ("NARWHAL_LITE",  "Nano Banana 2 Lite (Flow)"),     # v6 nano-banana-2-lite. generate-only — добавлена 2026-07-04
    ("GEM_PIX_2",     "Nano Banana Pro (4cr)"),         # 4 кредита. generate-only
    ("IMAGEN_3_5",    "Imagen 4 (4cr)"),                # 4 кредита. generate-only
    ("VEO_BANANA",    "Nano Banana (Veo) ★"),           # VeoNonStop (ключ veo). generate-only, синхронно, до 5 char-refs
    ("VEO_BANANA_2",  "Nano Banana 2 (Veo) ★"),         # VeoNonStop, до 5 reference inputs
    ("VEO_BANANA_LITE", "Nano Banana 2 Lite (Veo) ★"),  # VeoNonStop, до 5 reference inputs
    ("GPT_IMAGE2",    "💲 GPT-Image-2 low — ПЛАТНО ~$0.006/шт"),  # OpenAI НАПРЯМУЮ (реальные деньги с openai-ключа!). generate-only
]
GOOGLER_IMAGE_MODEL_IDS = [m[0] for m in GOOGLER_IMAGE_MODELS]
GOOGLER_IMAGE_MODEL_LABELS = [m[1] for m in GOOGLER_IMAGE_MODELS]
GOOGLER_IMAGE_MODEL_LABEL_MAP = dict(GOOGLER_IMAGE_MODELS)

# Deliberate product invariant, not a user-configurable feature flag.  Image
# receipts and reference passports are project-owned; cross-project copying
# cannot preserve their identity atomically, so every project generates itself.
CROSS_PROJECT_IMAGE_REUSE_ENABLED = False
CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON = (
    "у проектов разные паспорта кадров и референсов"
)


@dataclass(frozen=True)
class ImageGenerationSnapshot:
    """Immutable identity of the settings and refs actually sent to one run."""

    prompt_mode: str
    style: str
    model: str
    aspect_ratio: str
    custom_prompts: tuple
    existing_prompts: tuple
    seed: str
    use_legacy: bool
    split_long: bool
    split_long_seconds: str
    sentences_per_prompt: str
    seconds_per_image: str
    auto_hero: bool
    analyze_story: bool
    copy_similar: bool
    reference_manifest: str
    reference_manifest_sha256: str
    reference_bundle_id: str
    reference_source_fingerprint: str
    reference_reuse_allowed: bool
    reference_reuse_reason: str
    source_preset_key: tuple


# --- КЛАСС ПРОЕКТА ---
class ProjectFrame(Frame):
    """Вкладка для одного видео-проекта."""
    
    def __init__(self, parent, project_name, app_reference):
        super().__init__(parent)
        self.project_name = sanitize_project_name(project_name)  # защитный санитайз имени
        self.app_reference = app_reference
        self.preset_manager = app_reference.preset_manager
        self.render_queue = app_reference.render_queue
        
        # ДВЕ ОТДЕЛЬНЫЕ ОЧЕРЕДИ TTS
        self.edgetts_queue = getattr(app_reference, 'edgetts_queue', None)
        self.elevenlabs_queue = getattr(app_reference, 'elevenlabs_queue', None)
        self.voicer_queue = getattr(app_reference, 'voicer_queue', None)
        self.voicegencore_queue = getattr(app_reference, 'voicegencore_queue', None)
        
        # Очереди для Thief и Googler
        self.thief_queue = getattr(app_reference, 'thief_queue', None)
        self.googler_queue = getattr(app_reference, 'googler_queue', None)
        self.googler_video_queue = getattr(app_reference, 'googler_video_queue', None)
        
        # Переменные настроек
        self.source_text_file = StringVar()
        self.image_search_query = StringVar()
        self.num_images_per_tag = StringVar(value="3")
        self.tts_engine = StringVar(value="edge")
        self.selected_voice_template = StringVar()
        self.selected_voice_set = StringVar(value="— нет —")  # Набор голосов (ниша→язык→voice_id) для VGC/Voicer
        self.selected_edge_voice = StringVar(value="ru-RU-DmitryNeural")
        self.auto_detect_language = BooleanVar(value=True)  # НОВОЕ: автоопределение языка
        self.video_quality = StringVar(value="720p")
        self.min_image_duration_var = StringVar(value="3.0")
        self.max_image_duration_var = StringVar(value="5.0")
        self.subtitle_language_var = StringVar(value="Russian")
        
        # НОВОЕ: Паузы - длительность и интервал
        self.pause_duration_var = StringVar(value="0.0")  # Длительность паузы в секундах
        self.pause_interval_var = StringVar(value="0")    # Каждые N сегментов
        
        # НОВОЕ: Фоновое аудио
        self.background_audio_file = StringVar()
        self.main_audio_volume_var = StringVar(value="100")  # 100% - громкость основного аудио
        self.background_audio_volume_var = StringVar(value="20")  # 20% - громкость фонового аудио
        
        # Интро-видео (список, всегда первыми в ролике)
        self.intro_video_files = []
        # НОВОЕ: Трек в конце ролика (на черном фоне)
        self.end_track_file = StringVar()
        self.end_track_volume_var = StringVar(value="100")  # 100% - громкость трека в конце
        
        # Чекбоксы
        self.use_cache = BooleanVar(value=True)
        self.trust_legacy_image_cache = BooleanVar(value=False)
        self._trust_legacy_image_cache_once = False
        self._trust_legacy_image_cache_snapshot = ()
        self.add_subtitles_var = BooleanVar(value=False)
        self.sync_audio_var = BooleanVar(value=True)
        self.auto_upload_var = BooleanVar(value=False)       # авто-залив на YouTube после рендера
        self.auto_chapters_var = BooleanVar(value=False)     # главы (таймкоды в описание) после рендера
        self.upload_channel_var = StringVar(value="")        # привязанный канал (имя из Channels)
        self.distribute_evenly_var = BooleanVar(value=True)
        self.vintage_effect_var = BooleanVar(value=False)
        self.randomize_metadata_var = BooleanVar(value=False)
        
        # GOOGLER: Настройки генерации изображений
        self.image_source = StringVar(value="bing")  # bing или googler
        self.googler_style = StringVar(value="cinematic, photorealistic")
        self.googler_aspect_ratio = StringVar(value="landscape")  # landscape/portrait/square
        self.googler_sentences_per_prompt = StringVar(value="3")  # X предложений на промт
        self.googler_seconds_per_image = StringVar(value="")  # N секунд на кадр (пусто = по тексту/sentences_per_prompt)
        self.googler_split_long_var = BooleanVar(value=True)  # Дробить длинные одиночные сцены
        self.googler_split_long_seconds = StringVar(value="6")  # Примерный лимит до Whisper
        self.googler_seed = StringVar(value="")  # Фиксированный seed для генерации (пусто = авто из стиля)
        # Единственный канонический режим промптов. Два независимых bool раньше
        # допускали одновременно GPT+custom, после чего custom молча игнорировался.
        self.image_prompt_mode = StringVar(value="gpt")  # gpt | custom | existing
        self.googler_use_legacy = BooleanVar(value=False)  # Использовать legacy API v2 для всех изображений
        self.googler_copy_similar = BooleanVar(value=False)
        self._cross_project_reuse_notice_logged = False
        self.googler_model = StringVar(value="OPENAI")  # Дефолт OPENAI (1 кредит, gen+transform). См. research/IMAGE_GEN_TEST_2026-06-05.md
        # НОВОЕ: Персонажи и референсы
        self.auto_hero_var = BooleanVar(value=False)  # Только явное opt-in: может тратить отдельный кредит
        self.analyze_story_var = BooleanVar(value=False)  # Анализ текста → персонажи+локации (ref-aware), по умолчанию OFF
        self.user_references = {}  # Словарь: {имя_референса: [список_путей_к_изображениям]}
        self.style_references = []  # Список путей к референсам стиля (стабильно в каждую генерацию)
        self._reference_state_issues = ()
        self._reference_state_revision = None
        self._reference_state_commit_id = None
        self._reference_state_token = None
        self._reference_copy_blocker = None
        
        # GOOGLER: Настройки генерации видео
        self.googler_video_prompt = StringVar()
        self.googler_video_loop = BooleanVar(value=True)
        self.googler_video_first_n = StringVar(value="0")  # Количество первых картинок для автоматической генерации видео (0 = отключено)
        self.googler_selected_images = []  # Список выбранных изображений для видео
        self.googler_video_method = StringVar(value="veo")  # Метод: veo (Veo NonStop, дефолт), flower, flow, grok
        # Автозапуск генерации видео после завершения генерации изображений
        self.googler_video_after_images = BooleanVar(value=False)
        self.googler_video_from_prompts = BooleanVar(value=False)  # t2v: первые N сцен видео ИЗ ПРОМПТОВ сцен (без картинок-исходников)
        # Флаг отложенной генерации видео (когда пользователь уже нажал «Видео», но ещё идут картинки)
        self._deferred_video_pending = False
        
        # THIEF: Настройки извлечения слайдов
        self.thief_youtube_url = StringVar()
        self.thief_transform_prompt = StringVar(value="Transform this slide into a professional illustration, maintain the same composition and information")
        self.thief_sensitivity = StringVar(value="0.70")  # Снижено для меньше слайдов
        self.thief_strict = BooleanVar(value=True)
        self.thief_min_duration = StringVar(value="2.0")
        self.thief_sample_interval = StringVar(value="1.0")  # Интервал выборки кадров
        self.thief_source_quality = StringVar(value="best")  # Качество исходного видео
        self.thief_proc_handle = None  # Для отслеживания процесса
        
        # Events для остановки inprocess воркеров (раздельные для разных задач)
        self._images_stop_event = threading.Event()
        self._thief_stop_event = threading.Event()
        self._tts_stop_event = threading.Event()
        
        # НОВОЕ: Настройки переходов и эффектов
        self.fade_duration_var = StringVar(value="0.5")  # Длительность fade перехода
        self.enable_fade_var = BooleanVar(value=False)   # Включить fade переходы
        self.enable_kenburns_var = BooleanVar(value=False)  # Ken Burns эффект
        self.use_video_audio_var = BooleanVar(value=False)  # 🔉 Подмешивать звук видео-клипов в озвучку
        self.kenburns_zoom_var = StringVar(value="1.1")  # Зум для Ken Burns
        # Порядок материалов: video_first, images_first, random, alternate, alphabetical
        self.material_order_var = StringVar(value="video_first")
        # Масштаб и смещение картинок/видео при рендере (глобально и по элементам)
        self.media_scale_pct_var = StringVar(value="100")
        self.media_offset_x_var = StringVar(value="0")
        self.media_offset_y_var = StringVar(value="0")
        self.media_per_item = {}  # normpath -> {scale_pct, offset_x, offset_y}
        
        # Папка дополнительных материалов (изображения/видео вне проекта)
        self.extra_materials_dir = StringVar()
        # Лимит стоковых видео: 0 = без лимита, N = взять N роликов, рандом + цикл
        self.stock_video_limit_var = StringVar(value="0")
        
        # Оверлеи (наложения поверх видео)
        # Каждый элемент: {'path': str, 'opacity': float 0.0-1.0, 'order': int}
        self.overlays = []
        
        # Нормализуем путь для корректной работы с русскими символами
        self.project_dir = os.path.normpath(os.path.join(BASE_DIR_ROOT, project_name))
        os.makedirs(self.project_dir, exist_ok=True)
        
        self.log_queue = queue.Queue()
        # Дебаунс для scrollregion-обновлений (коалесинг лавины <Configure>)
        self._scroll_pending = {}
        self.setup_ui()
        self._load_managed_reference_state()

    def _schedule_scrollregion(self, canvas, pad=30):
        """Дебаунсит обновление scrollregion канваса.
        КРИТИЧНО: НЕ зовём update_idletasks() внутри <Configure> — при 40 проектах
        (120 канвасов) это даёт каскад пересчётов геометрии и краш Tcl. Вместо этого
        коалесим все события в одно отложенное обновление через after_idle."""
        key = id(canvas)
        if self._scroll_pending.get(key):
            return  # обновление уже запланировано — глушим лавину
        self._scroll_pending[key] = True

        def _do_update():
            self._scroll_pending[key] = False
            try:
                if not canvas.winfo_exists():
                    return
                bbox = canvas.bbox("all")
                if bbox:
                    x1, y1, x2, y2 = bbox
                    canvas.configure(scrollregion=(0, 0, max(x2, 10), y2 + pad))
            except Exception:
                pass  # канвас уничтожен/в переходном состоянии — пропускаем

        try:
            canvas.after_idle(_do_update)
        except Exception:
            self._scroll_pending[key] = False

    def setup_ui(self):
        """UI с вкладками вместо прокрутки"""
        
        # Главный контейнер
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        
        # --- СТАТУС ОЧЕРЕДИ РЕНДЕРИНГА (вверху, всегда видно) ---
        queue_frame = Frame(self, bg="#e8e8e8", relief="raised", borderwidth=2)
        queue_frame.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        self.queue_status_label = Label(queue_frame, text=_('render_queue'), bg="#e8e8e8", fg="#000000", font=("Arial", 10, "bold"))
        self.queue_status_label.pack(pady=5)
        
        # --- ВНУТРЕННИЙ NOTEBOOK С ВКЛАДКАМИ ЭТАПОВ ---
        self.stages_notebook = ttk.Notebook(self)
        self.stages_notebook.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        
        # Сохраняем выбранную вкладку при смене
        self.stages_notebook.bind("<<NotebookTabChanged>>", self.on_stage_tab_changed)
        
        # Сохраняем ссылки на виджеты для обновления при смене языка
        self.ui_widgets = {}
        self.ui_widgets['queue_frame'] = queue_frame
        
        # ==================== ВКЛАДКА 1: ПРОЕКТ ====================
        tab_project = Frame(self.stages_notebook)
        self.stages_notebook.add(tab_project, text=f"📄 {_('tab_project')}")
        tab_project.columnconfigure(0, weight=1)
        
        # --- ПРЕСЕТЫ ---
        preset_frame = LabelFrame(tab_project, text=_('presets'), padx=10, pady=10)
        preset_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        preset_frame.columnconfigure(1, weight=1)
        self.ui_widgets['preset_frame'] = preset_frame
        
        self.ui_widgets['preset_label'] = Label(preset_frame, text=_('preset'))
        self.ui_widgets['preset_label'].grid(row=0, column=0, sticky="w")
        self.preset_combo = ttk.Combobox(preset_frame, state="readonly")
        self.preset_combo.grid(row=0, column=1, sticky="ew", padx=5)
        self.update_preset_list()
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        
        Button(preset_frame, text="💾", command=self.save_preset, width=3).grid(row=0, column=2, padx=2)
        Button(preset_frame, text="🗑️", command=self.delete_preset, width=3).grid(row=0, column=3, padx=2)
        
        # --- ТЕКСТ ---
        text_frame = LabelFrame(tab_project, text=f"📄 {_('source_text')}", padx=10, pady=10)
        text_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=10)
        text_frame.columnconfigure(1, weight=1)
        self.ui_widgets['text_frame'] = text_frame
        
        self.ui_widgets['file_label'] = Label(text_frame, text=_('file'))
        self.ui_widgets['file_label'].grid(row=0, column=0, sticky="w", padx=5, pady=5)
        Entry(text_frame, textvariable=self.source_text_file, state='readonly').grid(row=0, column=1, sticky="ew", padx=5)
        self.ui_widgets['select_file_btn'] = ttk.Button(text_frame, text=_('select_file_multiple'), command=self.select_source_file)
        self.ui_widgets['select_file_btn'].grid(row=0, column=2, padx=5)
        
        # Информационная панель
        info_frame = LabelFrame(tab_project, text=_('info'), padx=10, pady=10)
        info_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        self.ui_widgets['info_frame'] = info_frame
        self.ui_widgets['info_label'] = Label(info_frame, text=_('info_text'), 
              justify="left", fg="#666")
        self.ui_widgets['info_label'].pack(anchor="w")
        
        # --- УПРАВЛЕНИЕ ПРОЕКТОМ ---
        project_controls = LabelFrame(tab_project, text=_('project_controls'), padx=10, pady=10)
        project_controls.grid(row=3, column=0, sticky="ew", padx=10, pady=20)
        self.ui_widgets['project_controls'] = project_controls
        
        # Левая часть - управление вкладками
        left_controls = Frame(project_controls)
        left_controls.pack(side="left", padx=5)
        
        self.ui_widgets['tab_controls_label'] = Label(left_controls, text=_('tab_controls'), font=("Arial", 9, "bold"))
        self.ui_widgets['tab_controls_label'].pack(anchor="w", pady=(0, 5))
        controls_row = Frame(left_controls)
        controls_row.pack()
        
        self.ui_widgets['rename_btn'] = ttk.Button(controls_row, text=_('rename'), 
               command=self.app_reference.rename_selected_tab, width=15)
        self.ui_widgets['rename_btn'].pack(side="left", padx=2)
        self.ui_widgets['move_left_btn'] = ttk.Button(controls_row, text=_('move_left'), 
               command=self.app_reference.move_tab_left, width=10)
        self.ui_widgets['move_left_btn'].pack(side="left", padx=2)
        self.ui_widgets['move_right_btn'] = ttk.Button(controls_row, text=_('move_right'), 
               command=self.app_reference.move_tab_right, width=10)
        self.ui_widgets['move_right_btn'].pack(side="left", padx=2)
        
        # Правая часть - управление проектом
        right_controls = Frame(project_controls)
        right_controls.pack(side="right", padx=5)
        
        self.ui_widgets['open_folder_btn'] = ttk.Button(right_controls, text=_('open_project_folder'), command=self.open_project_folder)
        self.ui_widgets['open_folder_btn'].pack(side="right", padx=5)
        self.ui_widgets['close_project_btn'] = ttk.Button(right_controls, text=_('close_project'), command=self.close_project)
        self.ui_widgets['close_project_btn'].pack(side="right", padx=5)
        
        # ==================== ВКЛАДКА 2: ОЗВУЧКА ====================
        tab_tts = Frame(self.stages_notebook)
        self.stages_notebook.add(tab_tts, text=f"🎤 {_('tab_tts')}")
        tab_tts.columnconfigure(0, weight=1)
        tab_tts.rowconfigure(2, weight=1)
        
        # Настройки TTS
        tts_settings_frame = LabelFrame(tab_tts, text=_('tts_settings'), padx=10, pady=10)
        tts_settings_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        tts_settings_frame.columnconfigure(1, weight=1)
        self.ui_widgets['tts_settings_frame'] = tts_settings_frame
        
        self.ui_widgets['tts_engine_label'] = Label(tts_settings_frame, text=_('tts_engine'))
        self.ui_widgets['tts_engine_label'].grid(row=0, column=0, sticky="w", padx=5)
        engine_frame = Frame(tts_settings_frame)
        engine_frame.grid(row=0, column=1, sticky="w", padx=5)
        ttk.Radiobutton(engine_frame, text="Edge TTS", variable=self.tts_engine, value="edge", command=self.on_engine_change).pack(side="left")
        ttk.Radiobutton(engine_frame, text="ElevenLabs", variable=self.tts_engine, value="elevenlabs", command=self.on_engine_change).pack(side="left")
        ttk.Radiobutton(engine_frame, text="Voicer", variable=self.tts_engine, value="voicer", command=self.on_engine_change).pack(side="left")
        ttk.Radiobutton(engine_frame, text="VoiceGenCore", variable=self.tts_engine, value="voicegencore", command=self.on_engine_change).pack(side="left")
        
        # ElevenLabs
        self.elevenlabs_frame = Frame(tts_settings_frame)
        self.elevenlabs_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        self.ui_widgets['voice_template_label'] = Label(self.elevenlabs_frame, text=_('voice_template'))
        self.ui_widgets['voice_template_label'].grid(row=0, column=0, sticky="w", padx=5)
        self.voice_template_combo = ttk.Combobox(self.elevenlabs_frame, textvariable=self.selected_voice_template, state="readonly")
        self.voice_template_combo.grid(row=0, column=1, sticky="ew", padx=5)
        self.ui_widgets['edit_template_btn'] = Button(self.elevenlabs_frame, text=_('edit_template'), command=self.open_elevenlabs_template_editor, width=16)
        self.ui_widgets['edit_template_btn'].grid(row=0, column=2, padx=3)
        self.ui_widgets['delete_template_btn'] = Button(self.elevenlabs_frame, text=_('delete_template'), command=self.delete_elevenlabs_template, width=10)
        self.ui_widgets['delete_template_btn'].grid(row=0, column=3, padx=3)
        self.elevenlabs_frame.columnconfigure(1, weight=1)
        
        # Edge
        self.edge_frame = Frame(tts_settings_frame)
        self.edge_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        
        # НОВОЕ: Чекбокс автоопределения языка
        self.ui_widgets['auto_detect_cb'] = Checkbutton(self.edge_frame, text=_('auto_detect_language'), 
                   variable=self.auto_detect_language,
                   command=self.on_auto_detect_change)
        self.ui_widgets['auto_detect_cb'].grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=3)
        
        if not LANGDETECT_AVAILABLE:
            self.ui_widgets['auto_detect_install_label'] = Label(self.edge_frame, text=_('auto_detect_install'), 
                 fg="orange", font=("Arial", 8))
            self.ui_widgets['auto_detect_install_label'].grid(row=1, column=0, columnspan=2, sticky="w", padx=5)
        
        self.ui_widgets['edge_voice_label'] = Label(self.edge_frame, text=_('edge_voice'))
        self.ui_widgets['edge_voice_label'].grid(row=2, column=0, sticky="w", padx=5)
        self.edge_voice_combo = ttk.Combobox(self.edge_frame, textvariable=self.selected_edge_voice, values=EDGE_ALL_VOICES, state="readonly")
        self.edge_voice_combo.grid(row=2, column=1, sticky="ew", padx=5)
        self.edge_frame.columnconfigure(1, weight=1)
        
        # Voicer API (Mat3u)
        VOICER_MODELS = ("eleven_multilingual_v2", "eleven_turbo_v2", "eleven_turbo_v2_5", "eleven_flash_v2", "eleven_flash_v2_5", "eleven_v3")
        VOICER_SPLIT_TYPES = ("smart", "sentences", "paragraphs", "max_length")
        self.voicer_frame = Frame(tts_settings_frame)
        self.voicer_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        Label(self.voicer_frame, text="Voice ID:", width=12, anchor="w").grid(row=0, column=0, sticky="w", padx=5)
        self.voicer_voice_id = StringVar(value="AB9XsbSA4eLG12t2myjN")
        Entry(self.voicer_frame, textvariable=self.voicer_voice_id, width=28).grid(row=0, column=1, sticky="ew", padx=5)
        Label(self.voicer_frame, text="Model:", width=12, anchor="w").grid(row=1, column=0, sticky="w", padx=5)
        self.voicer_model_id = StringVar(value="eleven_multilingual_v2")
        ttk.Combobox(self.voicer_frame, textvariable=self.voicer_model_id, values=VOICER_MODELS, width=26, state="readonly").grid(row=1, column=1, sticky="ew", padx=5)
        Label(self.voicer_frame, text="Split:", width=12, anchor="w").grid(row=2, column=0, sticky="w", padx=5)
        self.voicer_split_type = StringVar(value="smart")
        ttk.Combobox(self.voicer_frame, textvariable=self.voicer_split_type, values=VOICER_SPLIT_TYPES, width=26, state="readonly").grid(row=2, column=1, sticky="ew", padx=5)
        self.voicer_auto_pause_enabled = BooleanVar(value=False)
        Checkbutton(self.voicer_frame, text="Автопаузы", variable=self.voicer_auto_pause_enabled).grid(row=3, column=0, columnspan=2, sticky="w", padx=5)
        Label(self.voicer_frame, text="Пауза (сек):", width=12, anchor="w").grid(row=4, column=0, sticky="w", padx=5)
        self.voicer_auto_pause_duration = StringVar(value="0.5")
        Entry(self.voicer_frame, textvariable=self.voicer_auto_pause_duration, width=10).grid(row=4, column=1, sticky="w", padx=5)
        Label(self.voicer_frame, text="Каждые N чанков:", width=14, anchor="w").grid(row=5, column=0, sticky="w", padx=5)
        self.voicer_auto_pause_frequency = StringVar(value="1")
        Entry(self.voicer_frame, textvariable=self.voicer_auto_pause_frequency, width=10).grid(row=5, column=1, sticky="w", padx=5)
        self.voicer_frame.columnconfigure(1, weight=1)

        # VoiceGenCore настройки
        self.voicegencore_frame = Frame(tts_settings_frame)
        self.voicegencore_frame.grid(row=4, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        Label(self.voicegencore_frame, text="Voice ID:", width=12, anchor="w").grid(row=0, column=0, sticky="w", padx=5)
        self.vgc_voice_id = StringVar(value="")
        Entry(self.voicegencore_frame, textvariable=self.vgc_voice_id, width=28).grid(row=0, column=1, sticky="ew", padx=5)
        Label(self.voicegencore_frame, text="Engine:", width=12, anchor="w").grid(row=1, column=0, sticky="w", padx=5)
        self.vgc_voice_engine = StringVar(value="elevenLabsV3")
        ttk.Combobox(self.voicegencore_frame, textvariable=self.vgc_voice_engine,
                     values=["elevenLabsV3", "elevenLabsV2"], width=26, state="readonly").grid(row=1, column=1, sticky="ew", padx=5)
        Label(self.voicegencore_frame, text="Stability:", width=12, anchor="w").grid(row=2, column=0, sticky="w", padx=5)
        self.vgc_stability = StringVar(value="0.5")
        Entry(self.voicegencore_frame, textvariable=self.vgc_stability, width=10).grid(row=2, column=1, sticky="w", padx=5)
        Label(self.voicegencore_frame, text="Threads:", width=12, anchor="w").grid(row=3, column=0, sticky="w", padx=5)
        self.vgc_thread_count = StringVar(value="10")
        Entry(self.voicegencore_frame, textvariable=self.vgc_thread_count, width=10).grid(row=3, column=1, sticky="w", padx=5)
        Label(self.voicegencore_frame, text="Скорость:", width=12, anchor="w").grid(row=4, column=0, sticky="w", padx=5)
        self.tts_speed = StringVar(value="1.0")
        Entry(self.voicegencore_frame, textvariable=self.tts_speed, width=10).grid(row=4, column=1, sticky="w", padx=5)
        Label(self.voicegencore_frame, text="(1.0=норма, 0.9 медленнее, 1.1 быстрее; работает для всех движков)", font=("Arial", 8), fg="gray").grid(row=5, column=0, columnspan=2, sticky="w", padx=5)
        self.voicegencore_frame.columnconfigure(1, weight=1)

        # 🎙 Набор голосов (ниша → язык → voice_id) — автоподстановка по языку скрипта для VGC/Voicer
        self.voice_set_frame = Frame(tts_settings_frame)
        self.voice_set_frame.grid(row=5, column=0, columnspan=3, sticky="ew", padx=5, pady=(8, 3))
        Label(self.voice_set_frame, text="🎙 Набор голосов:", width=16, anchor="w").grid(row=0, column=0, sticky="w", padx=5)
        self.voice_set_combo = ttk.Combobox(self.voice_set_frame, textvariable=self.selected_voice_set, state="readonly", width=24)
        self.voice_set_combo['postcommand'] = self._refresh_voice_set_combo  # обновлять список при каждом открытии
        self.voice_set_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_voice_set_to_ui())  # при выборе — заполнить поле Voice ID
        self.voice_set_combo.grid(row=0, column=1, sticky="w", padx=5)
        Button(self.voice_set_frame, text="✏️ Управление", command=self._open_voice_set_editor, width=14).grid(row=0, column=2, padx=3)
        Label(self.voice_set_frame, text="(для VoiceGenCore/Voicer — голос сам подставится по языку скрипта)",
              font=("Arial", 8), fg="gray").grid(row=1, column=0, columnspan=3, sticky="w", padx=5)
        self._refresh_voice_set_combo()

        # Кнопки TTS
        tts_buttons_frame = Frame(tab_tts)
        tts_buttons_frame.grid(row=1, column=0, sticky="w", padx=10, pady=5)
        self.ui_widgets['start_tts_btn'] = Button(tts_buttons_frame, text=_('start_tts'), command=self.run_tts, bg="#2196F3", fg="white", font=("Arial", 10, "bold"))
        self.ui_widgets['start_tts_btn'].pack(side="left", padx=(0,5))
        self.ui_widgets['start_tts_full_btn'] = Button(tts_buttons_frame, text=_('start_tts_full'), command=self.run_tts_full_text, bg="#FF9800", fg="white", font=("Arial", 10, "bold"))
        self.ui_widgets['start_tts_full_btn'].pack(side="left", padx=(0,5))
        self.ui_widgets['stop_tts_btn'] = Button(tts_buttons_frame, text=_('stop_tts'), command=self.stop_tts, 
               bg="#757575", fg="white", width=14)
        self.ui_widgets['stop_tts_btn'].pack(side="left")
        
        # Лог TTS
        tts_log_frame = LabelFrame(tab_tts, text=_('tts_log'), padx=5, pady=5)
        tts_log_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        tts_log_frame.columnconfigure(0, weight=1)
        tts_log_frame.rowconfigure(1, weight=1)
        self.ui_widgets['tts_log_frame'] = tts_log_frame
        
        # Кнопка копирования лога
        tts_log_header = Frame(tts_log_frame)
        tts_log_header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.ui_widgets['copy_tts_log_btn'] = Button(tts_log_header, text=_('copy_log'), command=lambda: self.copy_log_to_clipboard("tts"),
               bg="#e3f2fd", fg="#000000", font=("Arial", 9))
        self.ui_widgets['copy_tts_log_btn'].pack(side="right")
        
        self.tts_log_text = ScrolledText(tts_log_frame, height=15, wrap="word", state="disabled")
        self.tts_log_text.grid(row=1, column=0, sticky="nsew")
        _bind_mousewheel_to_scrollable(self.tts_log_text, self.tts_log_text)
        
        self.on_engine_change()
        
        # ==================== ВКЛАДКА 3: ИЗОБРАЖЕНИЯ ====================
        # Создаем контейнер с прокруткой для всей вкладки
        tab_images_container = Frame(self.stages_notebook)
        self.stages_notebook.add(tab_images_container, text=f"🖼️ {_('tab_images')}")
        tab_images_container.columnconfigure(0, weight=1)
        tab_images_container.rowconfigure(0, weight=1)
        
        # Canvas и Scrollbar для прокрутки
        self.canvas_images = Canvas(tab_images_container)
        scrollbar_images = ttk.Scrollbar(tab_images_container, orient="vertical", command=self.canvas_images.yview)
        tab_images = Frame(self.canvas_images)
        
        def update_scrollregion(event=None):
            self._schedule_scrollregion(self.canvas_images)
        
        def on_canvas_configure(event):
            # Обновляем ширину окна в Canvas при изменении размера Canvas
            canvas_width = event.width
            if self.canvas_images.find_all():
                self.canvas_images.itemconfig(self.canvas_images.find_all()[0], width=canvas_width)
        
        tab_images.bind("<Configure>", update_scrollregion)
        self.canvas_images.bind("<Configure>", on_canvas_configure)
        
        self.canvas_images.create_window((0, 0), window=tab_images, anchor="nw")
        self.canvas_images.configure(yscrollcommand=scrollbar_images.set)
        
        self.canvas_images.grid(row=0, column=0, sticky="nsew")
        scrollbar_images.grid(row=0, column=1, sticky="ns")
        
        # Настройки для tab_images
        tab_images.columnconfigure(0, weight=1)
        tab_images.rowconfigure(3, weight=1)  # Лог растягивается
        
        # --- ВЫБОР ИСТОЧНИКА ---
        source_frame = LabelFrame(tab_images, text=_('image_source'), padx=10, pady=5)
        source_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10,5))
        self.ui_widgets['source_frame'] = source_frame
        
        ttk.Radiobutton(source_frame, text=_('bing_search'), variable=self.image_source, value="bing", command=self.on_image_source_change).pack(side="left", padx=10)
        ttk.Radiobutton(source_frame, text=_('googler_ai'), variable=self.image_source, value="googler", command=self.on_image_source_change).pack(side="left", padx=10)
        # Галерея готовых картинок: просмотр + удаление поштучно/выбранных/всех
        Button(source_frame, text="🖼 Галерея / удаление", command=self.open_images_gallery,
               bg="#e8eaf6", fg="#000000", font=("Arial", 8)).pack(side="right", padx=10)
        
        # --- BING FRAME ---
        self.bing_frame = LabelFrame(tab_images, text=_('bing_search_title'), padx=10, pady=10)
        self.bing_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        self.bing_frame.columnconfigure(1, weight=1)
        self.ui_widgets['bing_frame'] = self.bing_frame
        
        self.ui_widgets['tags_label'] = Label(self.bing_frame, text=_('tags_comma'))
        self.ui_widgets['tags_label'].grid(row=0, column=0, sticky="w", padx=5, pady=5)
        Entry(self.bing_frame, textvariable=self.image_search_query).grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        
        self.ui_widgets['images_per_tag_label'] = Label(self.bing_frame, text=_('images_per_tag'))
        self.ui_widgets['images_per_tag_label'].grid(row=1, column=0, sticky="w", padx=5, pady=5)
        Entry(self.bing_frame, textvariable=self.num_images_per_tag, width=10).grid(row=1, column=1, sticky="w", padx=5, pady=5)
        
        bing_buttons = Frame(self.bing_frame)
        bing_buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=10)
        self.ui_widgets['download_bing_btn'] = Button(bing_buttons, text=_('download_bing'), command=self.run_image_download, bg="#4CAF50", fg="white", font=("Arial", 10, "bold"))
        self.ui_widgets['download_bing_btn'].pack(side="left", padx=(0,5))
        self.ui_widgets['stop_images_btn'] = Button(bing_buttons, text=_('stop_tts'), command=self.stop_images, 
               bg="#757575", fg="white", width=14)
        self.ui_widgets['stop_images_btn'].pack(side="left")
        
        # --- GOOGLER FRAME --- (максимально компактно)
        self.googler_frame = LabelFrame(tab_images, text=_('googler_generate_title'), padx=5, pady=3)
        self.googler_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=2)
        self.googler_frame.columnconfigure(1, weight=1)
        self.googler_frame.columnconfigure(3, weight=1)
        self.ui_widgets['googler_frame'] = self.googler_frame
        
        # Строка 0: Стиль (компактно)
        self.ui_widgets['style_label'] = Label(self.googler_frame, text=_('style'), font=("Arial", 8))
        self.ui_widgets['style_label'].grid(row=0, column=0, sticky="w", padx=3, pady=1)
        Entry(self.googler_frame, textvariable=self.googler_style, font=("Arial", 8)).grid(row=0, column=1, columnspan=3, sticky="ew", padx=3, pady=1)
        
        # Строка 1: Соотношение сторон + Предложений на промт (компактно)
        self.ui_widgets['aspect_ratio_label'] = Label(self.googler_frame, text=_('aspect_ratio'), font=("Arial", 8))
        self.ui_widgets['aspect_ratio_label'].grid(row=1, column=0, sticky="w", padx=3, pady=1)
        ttk.Combobox(self.googler_frame, textvariable=self.googler_aspect_ratio, values=['landscape', 'portrait', 'square'], state="readonly", width=10, font=("Arial", 8)).grid(row=1, column=1, sticky="w", padx=3, pady=1)
        
        self.ui_widgets['sentences_per_prompt_label'] = Label(self.googler_frame, text=_('sentences_per_prompt'), font=("Arial", 8))
        self.ui_widgets['sentences_per_prompt_label'].grid(row=1, column=2, sticky="w", padx=3, pady=1)
        spp_entry = Entry(self.googler_frame, textvariable=self.googler_sentences_per_prompt, width=12, font=("Arial", 8))
        spp_entry.grid(row=1, column=3, sticky="w", padx=3, pady=1)
        self.app_reference._bind_tooltip(spp_entry, lambda: "Сколько предложений на одну картинку.\n  3        = везде по 3\n  1x40,3 = первые 40 предложений по 1, дальше по 3\nРазделитель любой: 1x40,3 / 1х40,3 / 1-40-3 / 1 40 3")
        Label(self.googler_frame, text="сек/кадр:", font=("Arial", 8)).grid(row=1, column=4, sticky="w", padx=3, pady=1)
        Entry(self.googler_frame, textvariable=self.googler_seconds_per_image, width=6, font=("Arial", 8)).grid(row=1, column=5, sticky="w", padx=3, pady=1)
        self.ui_widgets['split_long_cb'] = Checkbutton(self.googler_frame, text="✂️ Дроблю длинные", variable=self.googler_split_long_var, font=("Arial", 8))
        self.ui_widgets['split_long_cb'].grid(row=1, column=6, sticky="w", padx=3, pady=1)
        split_limit_entry = Entry(self.googler_frame, textvariable=self.googler_split_long_seconds, width=4, font=("Arial", 8))
        split_limit_entry.grid(row=1, column=7, sticky="w", padx=(0, 1), pady=1)
        Label(self.googler_frame, text="≈с", font=("Arial", 8)).grid(row=1, column=8, sticky="w", padx=(0, 3), pady=1)
        _split_tip = lambda: ("Только там, где одна картинка = одно предложение (например, хук в 1x40,3).\n"
                              "Длинная фраза делится по смысловым паузам, затем по словам.\n"
                              "Секунды приблизительные: план пишется до Whisper; дефолт 6 с = 75 символов.\n"
                              "Тело «по 2/3/5» не меняется.")
        self.app_reference._bind_tooltip(self.ui_widgets['split_long_cb'], _split_tip)
        self.app_reference._bind_tooltip(split_limit_entry, _split_tip)

        # Строка 2: Модель генерации + Seed (в комбобоксе — понятные названия, в переменной — ID)
        Label(self.googler_frame, text="🤖 Модель:", font=("Arial", 8)).grid(row=2, column=0, sticky="w", padx=3, pady=1)
        model_combo = ttk.Combobox(self.googler_frame, values=GOOGLER_IMAGE_MODEL_LABELS,
            state="readonly", width=22, font=("Arial", 8))
        model_combo.grid(row=2, column=1, sticky="w", padx=3, pady=1)
        self.ui_widgets['googler_model_combo'] = model_combo
        try:
            idx = GOOGLER_IMAGE_MODEL_IDS.index(self.googler_model.get())
        except (ValueError, AttributeError):
            # fallback: GEM_PIX_2 → ищем по ID (после reorder список не индексирован константой)
            try:
                idx = GOOGLER_IMAGE_MODEL_IDS.index("OPENAI")
            except ValueError:
                idx = 0
        model_combo.current(idx)
        def _on_model_combo_select(event=None):
            i = model_combo.current()
            if 0 <= i < len(GOOGLER_IMAGE_MODEL_IDS):
                self.googler_model.set(GOOGLER_IMAGE_MODEL_IDS[i])
                if 'thief_model_combo' in self.ui_widgets:
                    self.ui_widgets['thief_model_combo'].current(i)
                self._update_style_refs_label()
        model_combo.bind("<<ComboboxSelected>>", _on_model_combo_select)
        self.ui_widgets['seed_label'] = Label(self.googler_frame, text="🎲 Seed:", font=("Arial", 8))
        self.ui_widgets['seed_label'].grid(row=2, column=2, sticky="w", padx=3, pady=1)
        seed_entry = Entry(self.googler_frame, textvariable=self.googler_seed, width=12, font=("Arial", 8))
        seed_entry.grid(row=2, column=3, sticky="w", padx=3, pady=1)

        # Стиль-референсы — в основных настройках (не в расширенных)
        style_refs_frame = Frame(self.googler_frame)
        style_refs_frame.grid(row=2, column=4, columnspan=2, sticky="w", padx=3, pady=1)
        Button(style_refs_frame, text="🎨 Стиль-рефы", command=self.edit_style_references,
               bg="#fff3e0", fg="#000000", font=("Arial", 8)).pack(side="left", padx=(0,2))
        self.style_refs_clear_btn = Button(style_refs_frame, text="🗑", command=self.clear_style_references,
               bg="#ffebee", fg="#000000", font=("Arial", 8))
        self.style_refs_clear_btn.pack(side="left", padx=(0,2))
        self.style_refs_label = Label(style_refs_frame, text="", font=("Arial", 7), fg="#666666")
        self.style_refs_label.pack(side="left")
        self._update_style_refs_label()

        # Расширенные настройки Googler (сворачиваемый блок)
        self.googler_advanced = CollapsibleFrame(self.googler_frame, text="Расширенные настройки")
        self.googler_advanced.grid(row=3, column=0, columnspan=4, sticky="ew", padx=3, pady=1)
        self.googler_advanced.toggle()  # Свернуть по умолчанию
        googler_adv = self.googler_advanced.sub_frame

        # Один взаимоисключающий режим промптов + независимые технические флаги.
        checkboxes_frame = Frame(googler_adv)
        checkboxes_frame.grid(row=0, column=0, columnspan=4, sticky="w", padx=3, pady=1)
        self.ui_widgets['gpt_prompts_cb'] = ttk.Radiobutton(
            checkboxes_frame,
            text=_('generate_gpt_prompts'),
            variable=self.image_prompt_mode,
            value="gpt",
        )
        self.ui_widgets['gpt_prompts_cb'].pack(side="left", padx=(0, 10))
        self.ui_widgets['custom_prompts_cb'] = ttk.Radiobutton(
            checkboxes_frame,
            text="📝 Кастомные промты (по абзацам)",
            variable=self.image_prompt_mode,
            value="custom",
        )
        self.ui_widgets['custom_prompts_cb'].pack(side="left", padx=(0, 10))
        self.ui_widgets['existing_prompts_rb'] = ttk.Radiobutton(
            checkboxes_frame,
            text="📄 Готовый prompts.json",
            variable=self.image_prompt_mode,
            value="existing",
        )
        self.ui_widgets['existing_prompts_rb'].pack(side="left", padx=(0, 10))
        self.ui_widgets['legacy_api_cb'] = Checkbutton(checkboxes_frame, text="🔧 Использовать Legacy API v2", variable=self.googler_use_legacy, font=("Arial", 8))
        self.ui_widgets['legacy_api_cb'].pack(side="left")
        self.ui_widgets['copy_similar_cb'] = Checkbutton(
            checkboxes_frame,
            text="♻️ Картинки между проектам (отключено: разные паспорта кадров)",
            variable=self.googler_copy_similar,
            font=("Arial", 8),
            state="disabled",
        )
        self.ui_widgets['copy_similar_cb'].pack(side="left", padx=(10, 0))
        self.app_reference._bind_tooltip(
            self.ui_widgets['copy_similar_cb'],
            lambda: (
                "Отключено fail-closed: receipt кадров и паспорт "
                "референсов принадлежат конкретному проекту.\n"
                "Каждый проект всегда генерирует свои кадры."
            ),
        )
        
        # Строка 4: Кастомные промты (текстовое поле: один абзац = один промт)
        custom_prompts_frame = LabelFrame(googler_adv, text="📝 Кастомные промты для изображений", padx=5, pady=2)
        custom_prompts_frame.grid(row=1, column=0, columnspan=4, sticky="ew", padx=3, pady=1)
        custom_prompts_frame.columnconfigure(0, weight=1)
        Label(custom_prompts_frame, text="Один абзац = один промт (между промтами — пустая строка)", font=("Arial", 7), fg="gray").grid(row=0, column=0, sticky="w", pady=(0, 2))
        custom_text_container = Frame(custom_prompts_frame)
        custom_text_container.grid(row=1, column=0, sticky="ew", pady=0)
        custom_text_container.columnconfigure(0, weight=1)
        scrollbar_custom = ttk.Scrollbar(custom_text_container)
        scrollbar_custom.grid(row=0, column=1, sticky="ns")
        self.googler_custom_prompts_text = Text(custom_text_container, height=5, wrap="word", font=("Consolas", 9), yscrollcommand=scrollbar_custom.set, state="normal")
        self.googler_custom_prompts_text.grid(row=0, column=0, sticky="nsew")
        scrollbar_custom.config(command=self.googler_custom_prompts_text.yview)
        self.ui_widgets['googler_custom_prompts_text'] = self.googler_custom_prompts_text
        
        # Строка 5: 🎭 Персонажи и Стиль (максимально компактно)
        characters_frame = LabelFrame(googler_adv, text="🎭 Персонажи и Стиль", padx=5, pady=2)
        characters_frame.grid(row=2, column=0, columnspan=4, sticky="ew", padx=3, pady=1)
        characters_frame.columnconfigure(0, weight=1)
        self.ui_widgets['characters_frame'] = characters_frame
        
        # Чекбокс авто-героя (компактно)
        self.ui_widgets['auto_hero_cb'] = Checkbutton(characters_frame, text="Авто-герой", variable=self.auto_hero_var, font=("Arial", 8))
        self.ui_widgets['auto_hero_cb'].grid(row=0, column=0, sticky="w", padx=3, pady=1)
        # Чекбокс анализа истории (персонажи+локации, ref-aware промпты)
        self.ui_widgets['analyze_story_cb'] = Checkbutton(characters_frame, text="🔍 Анализ персонажей/локаций", variable=self.analyze_story_var, font=("Arial", 8))
        self.ui_widgets['analyze_story_cb'].grid(row=0, column=1, sticky="w", padx=3, pady=1)
        self.ui_widgets['trust_legacy_image_cache_cb'] = Checkbutton(
            characters_frame,
            text="⚠ Доверять старым кадрам без паспорта",
            variable=self.trust_legacy_image_cache,
            font=("Arial", 8),
        )
        self.ui_widgets['trust_legacy_image_cache_cb'].grid(
            row=0, column=2, sticky="w", padx=3, pady=1
        )
        self.app_reference._bind_tooltip(
            self.ui_widgets['trust_legacy_image_cache_cb'],
            lambda: (
                "По умолчанию картинки без image receipt не считаются актуальным кэшем.\n"
                "Перед запуском будет показано точное число таких кадров и отдельное\n"
                "подтверждение. Согласие действует один запуск и не сохраняется."
            ),
        )
        
        # Список референсов (максимально компактно)
        ref_list_frame = Frame(characters_frame)
        ref_list_frame.grid(row=1, column=0, sticky="ew", padx=3, pady=1)
        ref_list_frame.columnconfigure(0, weight=1)
        
        Label(
            ref_list_frame,
            text=(f"Референсы (до {HERO_GROUPS_MAX} героев; "
                  f"до {HERO_ANGLES_MAX} фото героя → 1 вход):"),
            font=("Arial", 8),
        ).grid(row=0, column=0, sticky="w", pady=(0,1))
        
        # Treeview для отображения именованных референсов (меньше высота)
        ref_tree_container = Frame(ref_list_frame)
        ref_tree_container.grid(row=1, column=0, sticky="ew")
        ref_tree_container.columnconfigure(0, weight=1)
        
        scrollbar_ref = ttk.Scrollbar(ref_tree_container)
        scrollbar_ref.grid(row=0, column=1, sticky="ns")
        
        self.ref_treeview = ttk.Treeview(ref_tree_container, height=2, yscrollcommand=scrollbar_ref.set, columns=("count",), show="tree headings")
        self.ref_treeview.heading("#0", text="Имя")
        self.ref_treeview.heading("count", text="Исходники → вход")
        self.ref_treeview.column("#0", width=150)
        self.ref_treeview.column("count", width=145)
        self.ref_treeview.grid(row=0, column=0, sticky="ew")
        scrollbar_ref.config(command=self.ref_treeview.yview)
        self.ui_widgets['ref_treeview'] = self.ref_treeview
        
        # Кнопки управления референсами (компактнее, меньший шрифт)
        ref_buttons = Frame(ref_list_frame)
        ref_buttons.grid(row=2, column=0, sticky="w", pady=(1,0))
        
        Button(ref_buttons, text="[+]", command=self.edit_user_reference,
               bg="#e3f2fd", fg="#000000", width=4, font=("Arial", 8)).pack(side="left", padx=(0,2))
        Button(ref_buttons, text="[-]", command=self.remove_user_reference,
               bg="#ffebee", fg="#000000", width=4, font=("Arial", 8)).pack(side="left", padx=(0,4))
        Button(ref_buttons, text="↶", command=self.restore_previous_reference_revision,
               bg="#fff3e0", fg="#000000", width=4, font=("Arial", 8)).pack(side="left", padx=(0,4))
        Button(ref_buttons, text="⟳", command=self.reload_project_reference_state,
               bg="#e8f5e9", fg="#000000", width=4, font=("Arial", 8)).pack(side="left", padx=(0,4))
        # (стиль-рефы вынесены в основные настройки googler_frame)

        # Строка 6: Кнопки генерации (компактнее)
        googler_img_buttons = Frame(self.googler_frame)
        googler_img_buttons.grid(row=6, column=0, columnspan=4, sticky="w", pady=2)
        self.ui_widgets['generate_images_btn'] = Button(googler_img_buttons, text=_('generate_images'), command=self.run_googler_images, bg="#9C27B0", fg="white", font=("Arial", 10, "bold"))
        self.ui_widgets['generate_images_btn'].pack(side="left", padx=(0,5))
        self.ui_widgets['stop_googler_btn'] = Button(googler_img_buttons, text=_('stop_tts'), command=self.stop_images, 
               bg="#757575", fg="white", width=14)
        self.ui_widgets['stop_googler_btn'].pack(side="left")
        
        # --- GOOGLER VIDEO FRAME --- (возвращаем отображение)
        self.googler_video_frame = LabelFrame(tab_images, text=_('googler_video_title'), padx=5, pady=2)
        self.googler_video_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=1)
        self.googler_video_frame.columnconfigure(1, weight=1)
        self.ui_widgets['googler_video_frame'] = self.googler_video_frame
        
        # Строка 0: Выбор изображений (компактно)
        self.ui_widgets['images_label'] = Label(self.googler_video_frame, text=_('images_label'), font=("Arial", 8))
        self.ui_widgets['images_label'].grid(row=0, column=0, sticky="w", padx=3, pady=1)
        img_select_frame = Frame(self.googler_video_frame)
        img_select_frame.grid(row=0, column=1, sticky="ew", padx=3, pady=1)
        self.selected_images_label = Label(img_select_frame, text=_('not_selected'), fg="#666", font=("Arial", 8))
        self.selected_images_label.pack(side="left", fill="x", expand=True)
        self.ui_widgets['select_images_btn'] = Button(img_select_frame, text=_('select_images'), command=self.select_images_for_video,
               bg="#e3f2fd", fg="#000000", font=("Arial", 8))
        self.ui_widgets['select_images_btn'].pack(side="left", padx=1)
        Button(img_select_frame, text="❌", command=self.clear_selected_images, 
               bg="#ffebee", fg="#000000", width=2, font=("Arial", 8)).pack(side="left")
        
        # Строка 0.5: Автоматический выбор первых X картинок
        auto_select_frame = Frame(self.googler_video_frame)
        auto_select_frame.grid(row=0, column=2, sticky="w", padx=3, pady=1)
        Label(auto_select_frame, text="Первых:", font=("Arial", 8)).pack(side="left", padx=(0,2))
        first_n_entry = Entry(auto_select_frame, textvariable=self.googler_video_first_n, width=5, font=("Arial", 8))
        first_n_entry.pack(side="left", padx=(0,2))
        self.ui_widgets['auto_video_btn'] = Button(auto_select_frame, text="🎬 Авто", command=self.run_googler_video_auto_first_n,
               bg="#FF9800", fg="white", font=("Arial", 8))
        self.ui_widgets['auto_video_btn'].pack(side="left")
        self.ui_widgets['video_from_prompts_cb'] = Checkbutton(auto_select_frame, text="🎥 Из промптов (t2v)",
               variable=self.googler_video_from_prompts, font=("Arial", 8))
        self.ui_widgets['video_from_prompts_cb'].pack(side="left", padx=(6, 0))
        self.app_reference._bind_tooltip(self.ui_widgets['video_from_prompts_cb'],
               lambda: "Первые N сцен генерятся как text-to-video ПО ПРОМПТАМ сцен (scenes.json) —\n"
                       "картинки-исходники не нужны. N — поле «Первых:» слева; пусто/0 = ВСЕ сцены\n"
                       "(весь ролик из видео, стадию картинок можно пропустить).\n"
                       "Картинки покрытых сцен исключаются из рендера. Метод: Veo NonStop.")
        
        # Строка 1: Промт для видео (компактно)
        self.ui_widgets['video_prompt_label'] = Label(self.googler_video_frame, text=_('video_prompt'), font=("Arial", 8))
        self.ui_widgets['video_prompt_label'].grid(row=1, column=0, sticky="w", padx=3, pady=1)
        Entry(self.googler_video_frame, textvariable=self.googler_video_prompt, font=("Arial", 8)).grid(row=1, column=1, sticky="ew", padx=3, pady=1)
        
        # Строка 1.5: Метод генерации видео
        Label(self.googler_video_frame, text="🤖 Метод:", font=("Arial", 8)).grid(row=1, column=2, sticky="w", padx=3, pady=1)
        video_method_combo = ttk.Combobox(self.googler_video_frame, textvariable=self.googler_video_method,
            values=["veo", "flower", "flow", "grok"],
            state="readonly", width=14, font=("Arial", 8))
        video_method_combo.grid(row=1, column=3, sticky="w", padx=3, pady=1)
        
        # Строка 2: Чекбоксы + кнопки (компактно)
        video_controls = Frame(self.googler_video_frame)
        video_controls.grid(row=2, column=0, columnspan=4, sticky="w", pady=2)
        self.ui_widgets['loop_video_cb'] = Checkbutton(video_controls, text=_('loop_video'), variable=self.googler_video_loop, font=("Arial", 8))
        self.ui_widgets['loop_video_cb'].pack(side="left", padx=(0,5))
        # Новый чекбокс: автозапуск видео после генерации изображений
        self.ui_widgets['video_after_images_cb'] = Checkbutton(
            video_controls,
            text="🎬 После картинок",
            variable=self.googler_video_after_images,
            font=("Arial", 8)
        )
        self.ui_widgets['video_after_images_cb'].pack(side="left", padx=(0,5))
        self.ui_widgets['generate_video_btn'] = Button(video_controls, text=_('generate_video'), command=self.run_googler_video, bg="#FF5722", fg="white", font=("Arial", 9, "bold"))
        self.ui_widgets['generate_video_btn'].pack(side="left", padx=(0,2))
        self.ui_widgets['stop_video_btn'] = Button(video_controls, text=_('stop_tts'), command=self.stop_images, 
               bg="#757575", fg="white", width=10, font=("Arial", 8))
        self.ui_widgets['stop_video_btn'].pack(side="left")
        
        # --- ЛОГ --- (занимает все оставшееся пространство)
        img_log_frame = LabelFrame(tab_images, text=_('log'), padx=5, pady=3)
        img_log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=5)
        img_log_frame.columnconfigure(0, weight=1)
        img_log_frame.rowconfigure(1, weight=1)  # Лог растягивается
        self.ui_widgets['img_log_frame'] = img_log_frame
        
        # Кнопка копирования лога (компактно)
        img_log_header = Frame(img_log_frame)
        img_log_header.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        self.ui_widgets['copy_img_log_btn'] = Button(img_log_header, text=_('copy_log'), command=lambda: self.copy_log_to_clipboard("images"),
               bg="#e3f2fd", fg="#000000", font=("Arial", 8))
        self.ui_widgets['copy_img_log_btn'].pack(side="right")
        
        # ScrolledText автоматически имеет прокрутку справа, используем минимальную высоту (будет растягиваться)
        self.images_log_text = ScrolledText(img_log_frame, height=15, wrap="word", state="disabled", font=("Consolas", 9))
        self.images_log_text.grid(row=1, column=0, sticky="nsew")
        _bind_mousewheel_to_scrollable(self.images_log_text, self.images_log_text)
        
        # Начальное состояние - показываем Bing
        self.on_image_source_change()
        # Прокрутка колёсиком: canvas при наведении на контент, лог — при наведении на лог
        _bind_mousewheel_to_canvas_recursive(tab_images, self.canvas_images, exclude={self.images_log_text})
        _bind_mousewheel_to_canvas_recursive(self.canvas_images, self.canvas_images)
        
        # ==================== ВКЛАДКА 4: РЕНДЕР ====================
        tab_render = Frame(self.stages_notebook)
        self.stages_notebook.add(tab_render, text=f"🎬 {_('tab_render')}")
        tab_render.columnconfigure(0, weight=1)
        tab_render.rowconfigure(0, weight=1)
        
        # Контейнер с прокруткой: настройки сверху, лог снизу — ползунок справа (Canvas уже импортирован в начале файла)
        from tkinter import Scrollbar
        tab_render.columnconfigure(1, minsize=20)
        render_canvas = Canvas(tab_render, highlightthickness=0)
        render_scrollbar = Scrollbar(tab_render, orient="vertical", command=render_canvas.yview, width=20)
        render_inner = Frame(render_canvas)
        render_win_id = render_canvas.create_window((0, 0), window=render_inner, anchor="nw")
        def _on_inner_configure(event):
            self._schedule_scrollregion(render_canvas)
        render_inner.bind("<Configure>", _on_inner_configure)
        render_canvas.configure(yscrollcommand=render_scrollbar.set)
        def _on_render_resize(event):
            render_canvas.itemconfig(render_win_id, width=event.width)
        render_canvas.bind("<Configure>", _on_render_resize)
        render_scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 5), pady=10)
        render_canvas.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)
        self.ui_widgets['render_canvas'] = render_canvas
        self.ui_widgets['render_scrollbar'] = render_scrollbar
        
        # Настройки видео (внутри скролла)
        video_settings_frame = LabelFrame(render_inner, text=_('render_settings'), padx=10, pady=10)
        video_settings_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        render_inner.columnconfigure(0, weight=1)
        video_settings_frame.columnconfigure(1, weight=1)
        video_settings_frame.columnconfigure(3, weight=1)
        video_settings_frame.columnconfigure(4, weight=1)
        self.ui_widgets['video_settings_frame'] = video_settings_frame
        
        # Строка 0: Качество + Макс длительность
        self.ui_widgets['quality_label'] = Label(video_settings_frame, text=_('quality'))
        self.ui_widgets['quality_label'].grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Combobox(video_settings_frame, textvariable=self.video_quality, values=['144p', '240p', '360p', '480p', '720p', '1080p'], state="readonly", width=10).grid(row=0, column=1, sticky="w", padx=5, pady=3)
        
        self.ui_widgets['min_duration_label'] = Label(video_settings_frame, text=_('min_image_duration'))
        self.ui_widgets['min_duration_label'].grid(row=0, column=2, sticky="w", padx=5, pady=3)
        Entry(video_settings_frame, textvariable=self.min_image_duration_var, width=6).grid(row=0, column=3, sticky="w", padx=2, pady=3)
        self.ui_widgets['max_duration_label'] = Label(video_settings_frame, text=_('max_image_duration'))
        self.ui_widgets['max_duration_label'].grid(row=0, column=4, sticky="w", padx=5, pady=3)
        Entry(video_settings_frame, textvariable=self.max_image_duration_var, width=6).grid(row=0, column=5, sticky="w", padx=2, pady=3)
        
        # Строка 1: Язык субтитров
        self.ui_widgets['subtitle_lang_label'] = Label(video_settings_frame, text=_('subtitle_language_label'))
        self.ui_widgets['subtitle_lang_label'].grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Combobox(video_settings_frame, textvariable=self.subtitle_language_var, values=SUBTITLE_LANGUAGES, state="readonly", width=15).grid(row=1, column=1, sticky="w", padx=5, pady=3)
        
        # Строка 7: Чекбоксы (на виду — только субтитры)
        checkboxes_frame = Frame(video_settings_frame)
        checkboxes_frame.grid(row=7, column=0, columnspan=4, sticky="w", padx=5, pady=5)
        self.ui_widgets['subtitles_cb'] = Checkbutton(checkboxes_frame, text=_('subtitles_whisper'), variable=self.add_subtitles_var)
        self.ui_widgets['subtitles_cb'].pack(side="left", padx=5)
        Checkbutton(checkboxes_frame, text="🎯 Синхрон с озвучкой", variable=self.sync_audio_var).pack(side="left", padx=5)
        _uva_cb = Checkbutton(checkboxes_frame, text="🔉 Звук из видео-клипов", variable=self.use_video_audio_var)
        _uva_cb.pack(side="left", padx=5)
        self.app_reference._bind_tooltip(_uva_cb, lambda: "Аудио-дорожки видео-клипов (googler video/NNN_video.mp4)\nподмешиваются в озвучку ровно на их сценах.\nВыкл = в ролике только озвучка (как раньше).")
        if chapters is not None:
            _ch_cb = Checkbutton(checkboxes_frame, text="📑 Главы", variable=self.auto_chapters_var)
            _ch_cb.pack(side="left", padx=5)
            self.app_reference._bind_tooltip(_ch_cb, lambda: "После рендера разбить ролик на главы с шагом 2–4 минуты\nи написать им заголовки (тем же каналом, что пишет промпты).\nБлок таймкодов ляжет в chapters/description.txt и уйдёт\nв описание при заливке. Нужен «🎯 Синхрон с озвучкой».")
            Button(checkboxes_frame, text="📑 Сейчас", command=self._start_chapters).pack(side="left", padx=3)

        # 📤 Заливка на YouTube (AdsPower) — привязка канала к проекту + ручная/авто заливка
        if upload_manager is not None:
            upload_frame = Frame(video_settings_frame)
            upload_frame.grid(row=8, column=0, columnspan=6, sticky="w", padx=5, pady=4)
            Label(upload_frame, text="📤 Канал:").pack(side="left", padx=(0, 3))
            self._chan_combo = ttk.Combobox(upload_frame, textvariable=self.upload_channel_var, state="readonly", width=22)
            self._chan_combo.configure(postcommand=lambda: self._chan_combo.configure(values=upload_manager.Channels.list_names()))
            self._chan_combo.pack(side="left", padx=3)
            self._chan_combo.bind("<<ComboboxSelected>>", lambda e: upload_manager.save_project_target(self.project_dir, channel_name=self.upload_channel_var.get()))
            Button(upload_frame, text="Каналы…", command=lambda: upload_manager.build_channels_editor(upload_frame.winfo_toplevel())).pack(side="left", padx=3)
            Button(upload_frame, text="📤 Залить", command=self._start_upload).pack(side="left", padx=3)
            Checkbutton(upload_frame, text="авто после рендера", variable=self.auto_upload_var).pack(side="left", padx=8)
            try:
                _t = upload_manager.load_project_target(self.project_dir)
                if _t.get('channel_name'):
                    self.upload_channel_var.set(_t['channel_name'])
            except Exception:
                pass

        # Музыка, вставки и паузы (сворачиваемый блок) — редко-используемое
        self.media_extras = CollapsibleFrame(video_settings_frame, text="Музыка, вставки и паузы")
        self.media_extras.grid(row=9, column=0, columnspan=6, sticky="ew", padx=5, pady=5)
        self.media_extras.toggle()  # Свернуть по умолчанию
        media_extras_frame = self.media_extras.sub_frame
        media_extras_frame.columnconfigure(1, weight=1)
        media_extras_frame.columnconfigure(3, weight=1)
        media_extras_frame.columnconfigure(4, weight=1)

        # Строка 0: Фоновое аудио
        self.ui_widgets['bg_audio_label'] = Label(media_extras_frame, text=_('background_audio'))
        self.ui_widgets['bg_audio_label'].grid(row=0, column=0, sticky="w", padx=5, pady=3)
        bg_audio_frame = Frame(media_extras_frame)
        bg_audio_frame.grid(row=0, column=1, columnspan=3, sticky="ew", padx=5, pady=3)
        Entry(bg_audio_frame, textvariable=self.background_audio_file, state='readonly').pack(side="left", fill="x", expand=True)
        self.ui_widgets['select_bg_audio_btn'] = Button(bg_audio_frame, text=_('select_bg_audio'), command=self.select_background_audio,
               bg="#e3f2fd", fg="#000000")
        self.ui_widgets['select_bg_audio_btn'].pack(side="left", padx=2)
        Button(bg_audio_frame, text="❌", command=lambda: self.background_audio_file.set(""),
               bg="#ffebee", fg="#000000", width=2).pack(side="left")

        # Строка 1: Громкость
        self.ui_widgets['main_volume_label'] = Label(media_extras_frame, text=_('main_audio_volume'))
        self.ui_widgets['main_volume_label'].grid(row=1, column=0, sticky="w", padx=5, pady=3)
        Entry(media_extras_frame, textvariable=self.main_audio_volume_var, width=8).grid(row=1, column=1, sticky="w", padx=5, pady=3)
        self.ui_widgets['bg_volume_label'] = Label(media_extras_frame, text=_('background_audio_volume'))
        self.ui_widgets['bg_volume_label'].grid(row=1, column=2, sticky="w", padx=5, pady=3)
        Entry(media_extras_frame, textvariable=self.background_audio_volume_var, width=8).grid(row=1, column=3, sticky="w", padx=5, pady=3)

        # Строка 2: Интро-видео (список, всегда первыми)
        self.ui_widgets['intro_video_label'] = Label(media_extras_frame, text=_('intro_video'))
        self.ui_widgets['intro_video_label'].grid(row=2, column=0, sticky="nw", padx=5, pady=3)
        intro_video_frame = Frame(media_extras_frame)
        intro_video_frame.grid(row=2, column=1, columnspan=2, sticky="ew", padx=5, pady=3)
        intro_video_frame.columnconfigure(0, weight=1)
        self.intro_video_listbox = Listbox(intro_video_frame, height=3, selectmode="single")
        self.intro_video_listbox.grid(row=0, column=0, sticky="ew")
        intro_scroll = tk.Scrollbar(intro_video_frame, orient="vertical", command=self.intro_video_listbox.yview)
        intro_scroll.grid(row=0, column=1, sticky="ns")
        self.intro_video_listbox.configure(yscrollcommand=intro_scroll.set)
        intro_btns = Frame(intro_video_frame)
        intro_btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.ui_widgets['select_intro_video_btn'] = Button(intro_btns, text=_('select_intro_video'), command=self.select_intro_video,
               bg="#e3f2fd", fg="#000000")
        self.ui_widgets['select_intro_video_btn'].pack(side="left", padx=2)
        Button(intro_btns, text="❌ Удалить", command=self.remove_intro_video, bg="#ffebee", fg="#000000").pack(side="left", padx=2)
        Button(intro_btns, text="🗑 Очистить", command=self.clear_intro_videos, bg="#ffebee", fg="#000000").pack(side="left", padx=2)

        # Строка 3: Трек в конце ролика
        self.ui_widgets['end_track_label'] = Label(media_extras_frame, text=_('end_track'))
        self.ui_widgets['end_track_label'].grid(row=3, column=0, sticky="w", padx=5, pady=3)
        end_track_frame = Frame(media_extras_frame)
        end_track_frame.grid(row=3, column=1, columnspan=2, sticky="ew", padx=5, pady=3)
        Entry(end_track_frame, textvariable=self.end_track_file, state='readonly').pack(side="left", fill="x", expand=True)
        self.ui_widgets['select_end_track_btn'] = Button(end_track_frame, text=_('select_end_track'), command=self.select_end_track,
               bg="#e3f2fd", fg="#000000")
        self.ui_widgets['select_end_track_btn'].pack(side="left", padx=2)
        Button(end_track_frame, text="❌", command=lambda: self.end_track_file.set(""),
               bg="#ffebee", fg="#000000", width=2).pack(side="left")
        self.ui_widgets['end_track_volume_label'] = Label(media_extras_frame, text=_('end_track_volume'))
        self.ui_widgets['end_track_volume_label'].grid(row=3, column=3, sticky="w", padx=5, pady=3)
        Entry(media_extras_frame, textvariable=self.end_track_volume_var, width=8).grid(row=3, column=4, sticky="w", padx=5, pady=3)

        # Строка 4: Паузы
        self.ui_widgets['pause_label'] = Label(media_extras_frame, text=_('pause_settings'))
        self.ui_widgets['pause_label'].grid(row=4, column=0, sticky="w", padx=5, pady=3)
        pause_controls_frame = Frame(media_extras_frame)
        pause_controls_frame.grid(row=4, column=1, columnspan=3, sticky="w", padx=5, pady=3)
        Entry(pause_controls_frame, textvariable=self.pause_duration_var, width=5).pack(side="left", padx=2)
        self.ui_widgets['sec_every_label'] = Label(pause_controls_frame, text=_('sec_every'))
        self.ui_widgets['sec_every_label'].pack(side="left", padx=2)
        Entry(pause_controls_frame, textvariable=self.pause_interval_var, width=5).pack(side="left", padx=2)
        self.ui_widgets['audio_zero_label'] = Label(pause_controls_frame, text=_('audio_zero'))
        self.ui_widgets['audio_zero_label'].pack(side="left", padx=2)

        # Строка 5: Прочие галки (распределение, кэш, vintage, рандом-метаданные)
        extra_checkboxes_frame = Frame(media_extras_frame)
        extra_checkboxes_frame.grid(row=5, column=0, columnspan=4, sticky="w", padx=5, pady=5)
        self.ui_widgets['distribute_cb'] = Checkbutton(extra_checkboxes_frame, text=_('distribute_evenly'), variable=self.distribute_evenly_var)
        self.ui_widgets['distribute_cb'].pack(side="left", padx=5)
        self.ui_widgets['cache_cb'] = Checkbutton(extra_checkboxes_frame, text=_('cache'), variable=self.use_cache)
        self.ui_widgets['cache_cb'].pack(side="left", padx=5)
        self.ui_widgets['vintage_cb'] = Checkbutton(extra_checkboxes_frame, text=_('vintage_effect'), variable=self.vintage_effect_var)
        self.ui_widgets['vintage_cb'].pack(side="left", padx=5)
        self.ui_widgets['randomize_meta_cb'] = Checkbutton(extra_checkboxes_frame, text=_('randomize_metadata'), variable=self.randomize_metadata_var)
        self.ui_widgets['randomize_meta_cb'].pack(side="left", padx=5)

        # Расширенные настройки рендера (сворачиваемый блок)
        self.render_advanced = CollapsibleFrame(video_settings_frame, text="Дополнительные настройки рендера")
        self.render_advanced.grid(row=10, column=0, columnspan=6, sticky="ew", padx=5, pady=5)
        self.render_advanced.toggle()  # Свернуть по умолчанию
        adv_frame = self.render_advanced.sub_frame
        adv_frame.columnconfigure(1, weight=1)

        # Настройки переходов и эффектов
        transitions_frame = LabelFrame(adv_frame, text=_('transitions_effects'), padx=5, pady=5)
        transitions_frame.grid(row=0, column=0, columnspan=4, sticky="ew", padx=5, pady=5)
        self.ui_widgets['transitions_frame'] = transitions_frame
        self.ui_widgets['fade_cb'] = Checkbutton(transitions_frame, text=_('fade_transitions'), variable=self.enable_fade_var)
        self.ui_widgets['fade_cb'].pack(side="left", padx=5)
        self.ui_widgets['duration_short_label'] = Label(transitions_frame, text=_('duration_short'))
        self.ui_widgets['duration_short_label'].pack(side="left")
        Entry(transitions_frame, textvariable=self.fade_duration_var, width=5).pack(side="left", padx=2)
        self.ui_widgets['sec_label'] = Label(transitions_frame, text=_('sec'))
        self.ui_widgets['sec_label'].pack(side="left", padx=(0,15))
        self.ui_widgets['kenburns_cb'] = Checkbutton(transitions_frame, text=_('kenburns'), variable=self.enable_kenburns_var)
        self.ui_widgets['kenburns_cb'].pack(side="left", padx=5)
        self.ui_widgets['zoom_label'] = Label(transitions_frame, text=_('zoom'))
        self.ui_widgets['zoom_label'].pack(side="left")
        Entry(transitions_frame, textvariable=self.kenburns_zoom_var, width=5).pack(side="left", padx=2)
        
        # Порядок материалов (видео/картинки)
        MATERIAL_ORDER_KEYS = ('video_first', 'images_first', 'random', 'alternate', 'alphabetical', 'manual')
        MATERIAL_ORDER_DISPLAYS = (
            _('material_order_video_first'),
            _('material_order_images_first'),
            _('material_order_random'),
            _('material_order_alternate'),
            _('material_order_alphabetical'),
            _('material_order_manual')
        )
        material_order_frame = LabelFrame(adv_frame, text=_('material_order_title'), padx=5, pady=5)
        material_order_frame.grid(row=1, column=0, columnspan=4, sticky="ew", padx=5, pady=5)
        self.ui_widgets['material_order_frame'] = material_order_frame
        self.ui_widgets['material_order_label'] = Label(material_order_frame, text=_('material_order'))
        self.ui_widgets['material_order_label'].pack(side="left", padx=(0, 5))
        material_order_combo = ttk.Combobox(material_order_frame, textvariable=self.material_order_var, width=28, state="readonly")
        material_order_combo['values'] = MATERIAL_ORDER_DISPLAYS
        material_order_combo.pack(side="left", padx=2)
        self.ui_widgets['material_order_combo'] = material_order_combo
        self.ui_widgets['material_order_edit_btn'] = Button(material_order_frame, text=_('material_order_edit_btn'), command=self.open_manual_order_dialog, width=18)
        self.ui_widgets['material_order_edit_btn'].pack(side="left", padx=5)
        self._material_order_keys = MATERIAL_ORDER_KEYS
        self._material_order_displays = MATERIAL_ORDER_DISPLAYS
        if self.material_order_var.get() not in MATERIAL_ORDER_DISPLAYS:
            self.material_order_var.set(MATERIAL_ORDER_DISPLAYS[0])
        
        # Масштаб и смещение (картинки/видео)
        scale_offset_frame = LabelFrame(adv_frame, text=_('media_scale_offset_title'), padx=5, pady=5)
        scale_offset_frame.grid(row=2, column=0, columnspan=4, sticky="ew", padx=5, pady=5)
        scale_offset_frame.columnconfigure(2, weight=1)
        self.ui_widgets['scale_offset_frame'] = scale_offset_frame
        Label(scale_offset_frame, text=_('media_scale_pct')).grid(row=0, column=0, sticky="w", padx=(0, 5))
        Entry(scale_offset_frame, textvariable=self.media_scale_pct_var, width=6).grid(row=0, column=1, sticky="w", padx=2)
        Label(scale_offset_frame, text="%").grid(row=0, column=2, sticky="w")
        Label(scale_offset_frame, text=_('media_offset_x')).grid(row=0, column=3, sticky="w", padx=(10, 5))
        Entry(scale_offset_frame, textvariable=self.media_offset_x_var, width=6).grid(row=0, column=4, sticky="w", padx=2)
        Label(scale_offset_frame, text=_('media_offset_y')).grid(row=0, column=5, sticky="w", padx=(5, 5))
        Entry(scale_offset_frame, textvariable=self.media_offset_y_var, width=6).grid(row=0, column=6, sticky="w", padx=2)
        self.ui_widgets['media_scale_edit_btn'] = Button(scale_offset_frame, text=_('media_scale_edit_btn'), command=self.open_media_scale_offset_dialog, width=22)
        self.ui_widgets['media_scale_edit_btn'].grid(row=1, column=0, columnspan=4, sticky="w", pady=(5, 0))
        
        # Папка дополнительных материалов
        extra_mat_frame = LabelFrame(adv_frame, text="Дополнительные материалы", padx=5, pady=5)
        extra_mat_frame.grid(row=3, column=0, columnspan=5, sticky="ew", padx=5, pady=5)
        extra_mat_frame.columnconfigure(1, weight=1)
        self.ui_widgets['extra_mat_frame'] = extra_mat_frame
        Label(extra_mat_frame, text="Папка:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        Entry(extra_mat_frame, textvariable=self.extra_materials_dir, state='readonly').grid(row=0, column=1, sticky="ew", padx=2)
        self.ui_widgets['select_extra_mat_btn'] = Button(extra_mat_frame, text="Выбрать папку", command=self.select_extra_materials_dir, bg="#e3f2fd", fg="#000000")
        self.ui_widgets['select_extra_mat_btn'].grid(row=0, column=2, padx=2)
        Button(extra_mat_frame, text="❌", command=lambda: self.extra_materials_dir.set(""), bg="#ffebee", fg="#000000", width=2).grid(row=0, column=3, padx=2)
        Label(extra_mat_frame, text="Изображения и видео из этой папки будут добавлены в рендер", fg="#666", font=("Arial", 8)).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))
        stock_limit_frame = Frame(extra_mat_frame)
        stock_limit_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=(5, 0))
        Label(stock_limit_frame, text="Лимит стоковых видео (0=все):").pack(side="left", padx=(0, 5))
        stock_spin = ttk.Spinbox(stock_limit_frame, from_=0, to=999, width=6, textvariable=self.stock_video_limit_var)
        stock_spin.pack(side="left", padx=2)
        Label(stock_limit_frame, text="— берёт N роликов рандомно, цикл при исчерпании", fg="#666", font=("Arial", 8)).pack(side="left", padx=(5, 0))
        
        # Оверлеи (наложения поверх видео)
        overlay_frame = LabelFrame(adv_frame, text="Оверлеи (наложения)", padx=5, pady=5)
        overlay_frame.grid(row=4, column=0, columnspan=5, sticky="ew", padx=5, pady=5)
        overlay_frame.columnconfigure(0, weight=1)
        self.ui_widgets['overlay_frame'] = overlay_frame
        
        # Listbox для оверлеев
        overlay_list_frame = Frame(overlay_frame)
        overlay_list_frame.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        overlay_list_frame.columnconfigure(0, weight=1)
        self.overlay_listbox = Listbox(overlay_list_frame, height=4, selectmode="single")
        self.overlay_listbox.grid(row=0, column=0, sticky="ew")
        overlay_scrollbar_v = tk.Scrollbar(overlay_list_frame, orient="vertical", command=self.overlay_listbox.yview)
        overlay_scrollbar_v.grid(row=0, column=1, sticky="ns")
        self.overlay_listbox.configure(yscrollcommand=overlay_scrollbar_v.set)
        
        # Кнопки управления оверлеями
        overlay_btns = Frame(overlay_frame)
        overlay_btns.grid(row=1, column=0, sticky="w")
        Button(overlay_btns, text="➕ Добавить", command=self.add_overlay, bg="#4CAF50", fg="white").pack(side="left", padx=2)
        Button(overlay_btns, text="❌ Удалить", command=self.remove_overlay, bg="#f44336", fg="white").pack(side="left", padx=2)
        Button(overlay_btns, text="🔼 Выше", command=self.move_overlay_up).pack(side="left", padx=2)
        Button(overlay_btns, text="🔽 Ниже", command=self.move_overlay_down).pack(side="left", padx=2)
        
        # Прозрачность выбранного оверлея
        opacity_frame = Frame(overlay_frame)
        opacity_frame.grid(row=2, column=0, sticky="w", pady=(5, 0))
        Label(opacity_frame, text="Прозрачность:").pack(side="left", padx=(0, 5))
        self.overlay_opacity_var = StringVar(value="50")
        Entry(opacity_frame, textvariable=self.overlay_opacity_var, width=5).pack(side="left", padx=2)
        Label(opacity_frame, text="%").pack(side="left")
        Button(opacity_frame, text="Применить", command=self.apply_overlay_opacity, bg="#2196F3", fg="white").pack(side="left", padx=5)
        Label(overlay_frame, text="Порядок: верхние оверлеи в списке рисуются первыми (ниже), нижние — последними (сверху)", fg="#666", font=("Arial", 8)).grid(row=3, column=0, sticky="w", pady=(2, 0))
        
        # Кнопки рендера
        render_buttons_frame = Frame(video_settings_frame)
        render_buttons_frame.grid(row=13, column=0, columnspan=4, sticky="w", pady=10)
        self.deferred_render = BooleanVar(value=False)
        Checkbutton(render_buttons_frame, text="⏳ Авто-рендер этого проекта когда материалы готовы",
                    variable=self.deferred_render).pack(side="left", padx=10)
        self.ui_widgets['assemble_video_btn'] = Button(render_buttons_frame, text=_('assemble_video'), command=self.run_video_render, bg="#f44336", fg="white", font=("Arial", 12, "bold"))
        self.ui_widgets['assemble_video_btn'].pack(side="left", padx=(0,10))
        self.ui_widgets['stop_render_btn'] = Button(render_buttons_frame, text=_('stop_render'), command=self.stop_render, 
               bg="#757575", fg="white", width=14)
        self.ui_widgets['stop_render_btn'].pack(side="left")
        
        # Лог рендера (внутри скролла, под настройками)
        render_log_frame = LabelFrame(render_inner, text=_('render_log'), padx=5, pady=5)
        render_log_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        render_log_frame.columnconfigure(0, weight=1)
        render_log_frame.rowconfigure(1, weight=1)
        self.ui_widgets['render_log_frame'] = render_log_frame
        
        # Кнопка копирования лога
        render_log_header = Frame(render_log_frame)
        render_log_header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.ui_widgets['copy_render_log_btn'] = Button(render_log_header, text=_('copy_log'), command=lambda: self.copy_log_to_clipboard("render"),
               bg="#e3f2fd", fg="#000000", font=("Arial", 9))
        self.ui_widgets['copy_render_log_btn'].pack(side="right")
        self.ui_widgets['open_render_log_btn'] = Button(render_log_header, text="📂 Открыть лог", command=lambda: self.open_log_file("render"),
               bg="#e3f2fd", fg="#000000", font=("Arial", 9))
        self.ui_widgets['open_render_log_btn'].pack(side="right", padx=(0, 5))
        
        self.render_log_text = ScrolledText(render_log_frame, height=12, wrap="word", state="disabled")
        self.render_log_text.grid(row=1, column=0, sticky="nsew")
        _bind_mousewheel_to_scrollable(self.render_log_text, self.render_log_text)
        _bind_mousewheel_to_canvas_recursive(render_inner, render_canvas, exclude={self.render_log_text})
        _bind_mousewheel_to_canvas_recursive(render_canvas, render_canvas)
        
        # ==================== ВКЛАДКА 5: ВОР ====================
        tab_thief = Frame(self.stages_notebook)
        self.stages_notebook.add(tab_thief, text=f"📥 {_('tab_thief')}")
        tab_thief.columnconfigure(0, weight=1)
        tab_thief.rowconfigure(0, weight=1)
        # Прокручиваемый контейнер: на малой высоте экрана лог не обрезается
        thief_canvas = Canvas(tab_thief, highlightthickness=0)
        thief_scrollbar = Scrollbar(tab_thief, orient="vertical", command=thief_canvas.yview, width=20)
        thief_inner = Frame(thief_canvas)
        thief_win_id = thief_canvas.create_window((0, 0), window=thief_inner, anchor="nw")
        def _on_thief_inner_configure(event):
            self._schedule_scrollregion(thief_canvas)
        thief_inner.bind("<Configure>", _on_thief_inner_configure)
        thief_canvas.configure(yscrollcommand=thief_scrollbar.set)
        def _on_thief_canvas_resize(event):
            thief_canvas.itemconfig(thief_win_id, width=event.width)
        thief_canvas.bind("<Configure>", _on_thief_canvas_resize)
        thief_scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 5), pady=10)
        thief_canvas.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)
        self.ui_widgets['thief_canvas'] = thief_canvas
        thief_inner.columnconfigure(0, weight=1)
        thief_inner.rowconfigure(3, weight=1)  # Лог занимает оставшееся место
        
        # --- YouTube URL ---
        url_frame = LabelFrame(thief_inner, text=_('video_source'), padx=10, pady=10)
        url_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10,5))
        url_frame.columnconfigure(1, weight=1)
        self.ui_widgets['url_frame'] = url_frame
        
        self.ui_widgets['youtube_url_label'] = Label(url_frame, text=_('youtube_url'))
        self.ui_widgets['youtube_url_label'].grid(row=0, column=0, sticky="w", padx=5, pady=5)
        Entry(url_frame, textvariable=self.thief_youtube_url).grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        self.ui_widgets['paste_btn'] = Button(url_frame, text=_('paste'), command=self.paste_youtube_url,
               bg="#e3f2fd", fg="#000000")
        self.ui_widgets['paste_btn'].grid(row=0, column=2, padx=5)
        
        # --- Настройки извлечения ---
        extract_frame = LabelFrame(thief_inner, text=_('extract_settings'), padx=10, pady=10)
        extract_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        extract_frame.columnconfigure(1, weight=1)
        extract_frame.columnconfigure(3, weight=1)
        self.ui_widgets['extract_frame'] = extract_frame
        
        # Строка 0: Чувствительность + Мин. длительность
        self.ui_widgets['sensitivity_label'] = Label(extract_frame, text=_('sensitivity'))
        self.ui_widgets['sensitivity_label'].grid(row=0, column=0, sticky="w", padx=5, pady=3)
        sensitivity_frame = Frame(extract_frame)
        sensitivity_frame.grid(row=0, column=1, sticky="w", padx=5, pady=3)
        Entry(sensitivity_frame, textvariable=self.thief_sensitivity, width=6).pack(side="left")
        self.ui_widgets['sensitivity_hint'] = Label(sensitivity_frame, text=_('sensitivity_hint'), fg="#666")
        self.ui_widgets['sensitivity_hint'].pack(side="left", padx=5)
        
        self.ui_widgets['min_duration_label'] = Label(extract_frame, text=_('min_duration'))
        self.ui_widgets['min_duration_label'].grid(row=0, column=2, sticky="w", padx=5, pady=3)
        Entry(extract_frame, textvariable=self.thief_min_duration, width=6).grid(row=0, column=3, sticky="w", padx=5, pady=3)
        
        # Строка 1: Интервал выборки + Чекбокс strict
        self.ui_widgets['interval_label'] = Label(extract_frame, text=_('sample_interval'))
        self.ui_widgets['interval_label'].grid(row=1, column=0, sticky="w", padx=5, pady=3)
        interval_frame = Frame(extract_frame)
        interval_frame.grid(row=1, column=1, sticky="w", padx=5, pady=3)
        Entry(interval_frame, textvariable=self.thief_sample_interval, width=5).pack(side="left")
        self.ui_widgets['interval_hint'] = Label(interval_frame, text=_('interval_hint'), fg="#666")
        self.ui_widgets['interval_hint'].pack(side="left", padx=5)
        
        self.ui_widgets['strict_cb'] = Checkbutton(extract_frame, text=_('strict_dedup'), 
                   variable=self.thief_strict)
        self.ui_widgets['strict_cb'].grid(row=1, column=2, columnspan=2, sticky="w", padx=5, pady=3)
        
        # Строка 2: Качество исходника
        self.ui_widgets['source_quality_label'] = Label(extract_frame, text=_('source_quality'))
        self.ui_widgets['source_quality_label'].grid(row=2, column=0, sticky="w", padx=5, pady=3)
        thief_quality_combo = ttk.Combobox(extract_frame, textvariable=self.thief_source_quality,
            values=["best", "1080p", "720p", "480p", "360p", "fastest"],
            state="readonly", width=10, font=("Arial", 9))
        thief_quality_combo.grid(row=2, column=1, sticky="w", padx=5, pady=3)
        
        # Строка 3: Промт для трансформации
        self.ui_widgets['transform_prompt_label'] = Label(extract_frame, text=_('transform_prompt'))
        self.ui_widgets['transform_prompt_label'].grid(row=3, column=0, sticky="w", padx=5, pady=3)
        Entry(extract_frame, textvariable=self.thief_transform_prompt).grid(row=3, column=1, columnspan=3, sticky="ew", padx=5, pady=3)
        
        # Строка 4: Модель для трансформации (те же понятные названия, общая переменная с Изображениями)
        Label(extract_frame, text="🤖 Модель:", font=("Arial", 9)).grid(row=4, column=0, sticky="w", padx=5, pady=3)
        thief_model_frame = Frame(extract_frame)
        thief_model_frame.grid(row=4, column=1, columnspan=3, sticky="w", padx=5, pady=3)
        thief_model_combo = ttk.Combobox(thief_model_frame, values=GOOGLER_IMAGE_MODEL_LABELS,
            state="readonly", width=22, font=("Arial", 9))
        thief_model_combo.pack(side="left")
        self.ui_widgets['thief_model_combo'] = thief_model_combo
        try:
            idx = GOOGLER_IMAGE_MODEL_IDS.index(self.googler_model.get())
        except (ValueError, AttributeError):
            try:
                idx = GOOGLER_IMAGE_MODEL_IDS.index("OPENAI")
            except ValueError:
                idx = 0
        thief_model_combo.current(idx)
        def _on_thief_model_combo_select(event=None):
            i = thief_model_combo.current()
            if 0 <= i < len(GOOGLER_IMAGE_MODEL_IDS):
                self.googler_model.set(GOOGLER_IMAGE_MODEL_IDS[i])
                if 'googler_model_combo' in self.ui_widgets:
                    self.ui_widgets['googler_model_combo'].current(i)
        thief_model_combo.bind("<<ComboboxSelected>>", _on_thief_model_combo_select)
        Label(thief_model_frame, text="(общая с вкладкой Изображения)", font=("Arial", 7), fg="gray").pack(side="left", padx=5)
        
        # Строка 5: Кнопки
        thief_buttons = Frame(extract_frame)
        thief_buttons.grid(row=5, column=0, columnspan=4, sticky="w", pady=10)
        
        self.thief_copy_similar = BooleanVar(value=True)
        Checkbutton(thief_buttons, text="Копировать в похожие проекты (85%+)",
                    variable=self.thief_copy_similar).pack(anchor="w", pady=2)
        
        self.ui_widgets['extract_btn'] = Button(thief_buttons, text=_('extract_slides'), command=self.run_thief_extract, 
               bg="#2196F3", fg="white", font=("Arial", 10, "bold"))
        self.ui_widgets['extract_btn'].pack(side="left", padx=(0,5))
        self.ui_widgets['transform_btn'] = Button(thief_buttons, text=_('transform'), command=self.run_thief_transform,
               bg="#9C27B0", fg="white", font=("Arial", 10, "bold"))
        self.ui_widgets['transform_btn'].pack(side="left", padx=(0,5))
        self.ui_widgets['full_cycle_btn'] = Button(thief_buttons, text=_('full_cycle'), command=self.run_thief_full,
               bg="#f44336", fg="white", font=("Arial", 11, "bold"))
        self.ui_widgets['full_cycle_btn'].pack(side="left", padx=(0,10))
        self.ui_widgets['stop_thief_btn'] = Button(thief_buttons, text=_('stop_thief'), command=self.stop_thief, 
               bg="#757575", fg="white", width=14)
        self.ui_widgets['stop_thief_btn'].pack(side="left")
        
        # --- Информация ---
        info_frame = LabelFrame(thief_inner, text=_('info'), padx=10, pady=5)
        info_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        self.ui_widgets['thief_info_frame'] = info_frame
        self.ui_widgets['thief_info_label'] = Label(info_frame, text=_('thief_info'),
              justify="left", fg="#666")
        self.ui_widgets['thief_info_label'].pack(anchor="w")
        
        # --- Лог ---
        thief_log_frame = LabelFrame(thief_inner, text=_('log'), padx=5, pady=5)
        thief_log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=10)
        thief_log_frame.rowconfigure(1, weight=1, minsize=120)  # Минимум 120px высоты лога
        thief_log_frame.columnconfigure(0, weight=1)
        thief_log_frame.rowconfigure(1, weight=1)
        self.ui_widgets['thief_log_frame'] = thief_log_frame
        
        # Кнопка копирования лога
        thief_log_header = Frame(thief_log_frame)
        thief_log_header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.ui_widgets['copy_thief_log_btn'] = Button(thief_log_header, text=_('copy_log'), command=lambda: self.copy_log_to_clipboard("thief"),
               bg="#e3f2fd", fg="#000000", font=("Arial", 9))
        self.ui_widgets['copy_thief_log_btn'].pack(side="right")
        
        self.thief_log_text = ScrolledText(thief_log_frame, height=12, wrap="word", state="disabled")
        self.thief_log_text.grid(row=1, column=0, sticky="nsew")
        _bind_mousewheel_to_scrollable(self.thief_log_text, self.thief_log_text)
        _bind_mousewheel_to_canvas_recursive(thief_inner, thief_canvas, exclude={self.thief_log_text})
        _bind_mousewheel_to_canvas_recursive(thief_canvas, thief_canvas)
        
        # Кнопка копирования исходников в другие проекты
        Button(thief_log_header, text="📋 В другие проекты", command=self._copy_slides_to_projects,
               bg="#FF9800", fg="white", font=("Arial", 9)).pack(side="right", padx=5)
        
        # Хэндлы процессов
        self.tts_proc_handle = None
        self.bing_proc_handle = None
        self.googler_proc_handle = None
        self.googler_video_proc_handle = None  # отдельный handle для видео — не затирается картинками при параллели
        self.render_proc_handle = None
        self._deferred_render_pending = False
        # Флаг загрузки картинок Bing (при run_worker_inprocess subprocess не создаётся — проверяем флаг)
        self._images_download_busy = False
        self._googler_images_busy = False
    
    def select_background_audio(self):
        # Начальная директория - projects (где могут быть аудио файлы)
        initial_dir = BASE_DIR_ROOT if os.path.exists(BASE_DIR_ROOT) else os.path.expanduser("~")
        filename = filedialog.askopenfilename(
            title="Выбрать фоновое аудио",
            filetypes=[
                ("Audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"),
                ("All", "*.*")
            ],
            initialdir=initial_dir
        )
        if filename:
            self.background_audio_file.set(filename)
    
    def _refresh_intro_video_listbox(self):
        """Обновить список интро в UI"""
        if not hasattr(self, 'intro_video_listbox'):
            return
        self.intro_video_listbox.delete(0, END)
        for p in self.intro_video_files:
            self.intro_video_listbox.insert(END, os.path.basename(p))
    
    def select_intro_video(self):
        """Выбрать интро-видео (можно несколько файлов)"""
        initial_dir = os.path.expanduser("~")
        if self.intro_video_files and os.path.exists(self.intro_video_files[-1]):
            initial_dir = os.path.dirname(self.intro_video_files[-1])
        elif self.project_dir and os.path.isdir(self.project_dir):
            initial_dir = self.project_dir
        filenames = filedialog.askopenfilenames(
            title=_('select_intro_video'),
            filetypes=[
                ("Video", "*.mp4 *.mov *.avi *.mkv *.webm"),
                ("All", "*.*")
            ],
            initialdir=initial_dir
        )
        if filenames:
            for f in filenames:
                if f and f not in self.intro_video_files:
                    self.intro_video_files.append(f)
            self._refresh_intro_video_listbox()
    
    def remove_intro_video(self):
        """Удалить выбранный интро из списка"""
        sel = self.intro_video_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.intro_video_files.pop(idx)
        self._refresh_intro_video_listbox()
    
    def clear_intro_videos(self):
        """Очистить весь список интро-видео"""
        self.intro_video_files = []
        self._refresh_intro_video_listbox()
    
    def select_end_track(self):
        """Выбрать трек для конца ролика"""
        initial_dir = os.path.expanduser("~")
        if self.end_track_file.get() and os.path.exists(self.end_track_file.get()):
            initial_dir = os.path.dirname(self.end_track_file.get())
        
        filename = filedialog.askopenfilename(
            title=_('select_end_track'),
            filetypes=[
                ("Audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"),
                ("All", "*.*")
            ],
            initialdir=initial_dir
        )
        if filename:
            self.end_track_file.set(filename)
    
    def select_extra_materials_dir(self):
        """Выбрать папку с дополнительными материалами"""
        initial_dir = self.extra_materials_dir.get() if self.extra_materials_dir.get() and os.path.isdir(self.extra_materials_dir.get()) else os.path.expanduser("~")
        folder = filedialog.askdirectory(title="Выбрать папку с дополнительными материалами", initialdir=initial_dir)
        if folder:
            self.extra_materials_dir.set(folder)
            img_count = vid_count = 0
            ext_img = {'.jpg', '.jpeg', '.jfif', '.png', '.webp', '.bmp', '.tiff', '.tif', '.gif', '.ico'}
            ext_vid = {'.mp4', '.mov', '.avi', '.mkv', '.webm'}
            try:
                for root, dirs, files in os.walk(folder):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for f in files:
                        ext = os.path.splitext(f)[1].lower()
                        if ext in ext_img:
                            img_count += 1
                        elif ext in ext_vid:
                            vid_count += 1
            except Exception:
                pass
            parts = []
            if img_count:
                parts.append(f"{img_count} картинок")
            if vid_count:
                parts.append(f"{vid_count} видео")
            total = img_count + vid_count
            self.log(f"📂 Папка доп. материалов: {folder} ({total} медиафайлов: {', '.join(parts)})", kind="render")
    
    # --- ОВЕРЛЕИ ---
    def _refresh_overlay_listbox(self):
        """Обновить список оверлеев в UI"""
        self.overlay_listbox.delete(0, END)
        for i, ov in enumerate(self.overlays):
            name = os.path.basename(ov['path'])
            opacity_pct = int(ov.get('opacity', 0.5) * 100)
            self.overlay_listbox.insert(END, f"[{i+1}] {name} — {opacity_pct}%")
    
    def add_overlay(self):
        """Добавить оверлей (изображение или видео)"""
        filename = filedialog.askopenfilename(
            title="Выбрать файл оверлея",
            filetypes=[
                ("Images/Video", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.mp4 *.mov *.avi *.mkv *.webm"),
                ("All", "*.*")
            ],
            initialdir=os.path.expanduser("~")
        )
        if filename:
            opacity = float(self.overlay_opacity_var.get() or 50) / 100.0
            opacity = max(0.0, min(1.0, opacity))
            self.overlays.append({
                'path': filename,
                'opacity': opacity,
                'order': len(self.overlays)
            })
            self._refresh_overlay_listbox()
            self.log(f"➕ Оверлей добавлен: {os.path.basename(filename)} ({int(opacity*100)}%)", kind="render")
    
    def remove_overlay(self):
        """Удалить выбранный оверлей"""
        sel = self.overlay_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        removed = self.overlays.pop(idx)
        for i, ov in enumerate(self.overlays):
            ov['order'] = i
        self._refresh_overlay_listbox()
        self.log(f"❌ Оверлей удалён: {os.path.basename(removed['path'])}", kind="render")
    
    def move_overlay_up(self):
        """Поднять оверлей на позицию выше (рисуется раньше = ниже)"""
        sel = self.overlay_listbox.curselection()
        if not sel or sel[0] == 0:
            return
        idx = sel[0]
        self.overlays[idx], self.overlays[idx-1] = self.overlays[idx-1], self.overlays[idx]
        for i, ov in enumerate(self.overlays):
            ov['order'] = i
        self._refresh_overlay_listbox()
        self.overlay_listbox.selection_set(idx - 1)
    
    def move_overlay_down(self):
        """Опустить оверлей на позицию ниже (рисуется позже = выше)"""
        sel = self.overlay_listbox.curselection()
        if not sel or sel[0] >= len(self.overlays) - 1:
            return
        idx = sel[0]
        self.overlays[idx], self.overlays[idx+1] = self.overlays[idx+1], self.overlays[idx]
        for i, ov in enumerate(self.overlays):
            ov['order'] = i
        self._refresh_overlay_listbox()
        self.overlay_listbox.selection_set(idx + 1)
    
    def apply_overlay_opacity(self):
        """Применить значение прозрачности к выбранному оверлею"""
        sel = self.overlay_listbox.curselection()
        if not sel:
            self.log("⚠️ Выберите оверлей в списке", kind="render")
            return
        idx = sel[0]
        try:
            opacity = float(self.overlay_opacity_var.get() or 50) / 100.0
            opacity = max(0.0, min(1.0, opacity))
        except ValueError:
            self.log("❌ Введите число от 0 до 100", kind="render")
            return
        self.overlays[idx]['opacity'] = opacity
        self._refresh_overlay_listbox()
        self.overlay_listbox.selection_set(idx)
        self.log(f"🔧 Прозрачность оверлея [{idx+1}] установлена: {int(opacity*100)}%", kind="render")
    
    def update_queue_status(self):
        """Обновление статуса очереди (thread-safe). Fix 4 (May 2026):
        Tkinter не thread-safe, при вызове из worker-thread маршалим в main loop через .after(0,...).
        """
        try:
            self.after(0, self._do_update_queue_status)
        except Exception:
            # fallback на прямой вызов если widget ещё не готов
            try: self._do_update_queue_status()
            except Exception: pass

    def _do_update_queue_status(self):
        try:
            status = self.render_queue.get_status()
            text = _('render_queue_active').format(active=status['active'], max=status['max'], queued=status['queued'])
            self.queue_status_label.configure(text=text)
        except Exception:
            pass
    
    def update_preset_list(self):
        preset_names = self.preset_manager.get_preset_names()
        values = [_('preset_new')] + preset_names
        current = self.preset_combo.get()
        self.preset_combo['values'] = values
        if current and current in values:
            self.preset_combo.set(current)
            return
        # Всегда по умолчанию выбираем пункт создания нового, чтобы не было автоперезаписи
        self.preset_combo.current(0)
    
    def get_media_list_for_project(self):
        """Сканирует папку проекта + доп. материалов и возвращает список путей (как воркер рендера)."""
        SUPPORTED_IMAGE = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.ico', '.gif']
        SUPPORTED_VIDEO = ['.mp4', '.mov', '.avi', '.mkv', '.webm']
        project_dir_normalized = os.path.normpath(os.path.abspath(self.project_dir))
        assets_dir = os.path.normpath(os.path.join(self.project_dir, "_ASSETS"))
        proj_name = os.path.basename(self.project_dir)
        safe_fn = re.sub(r'[<>:"/\\|?*]', '_', proj_name).strip(' .')[:150] or 'video'
        output_file_norm = os.path.normpath(os.path.join(project_dir_normalized, f"{safe_fn}.mp4"))
        paths = []
        try:
            for root, dirs, files in os.walk(project_dir_normalized):
                rn = os.path.normpath(root)
                if rn == assets_dir:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if not d.startswith('.') and 'temp' not in d.lower() and d != '_ASSETS']
                for f in files:
                    if f.startswith('.') or 'temp' in f.lower() or 'final_video' in f.lower():
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext in SUPPORTED_IMAGE or ext in SUPPORTED_VIDEO:
                        full = os.path.normpath(os.path.join(root, f))
                        if full == output_file_norm:
                            continue
                        if full not in paths:
                            paths.append(full)
        except Exception:
            pass
        # Добавляем материалы из доп. папки
        extra_dir = (self.extra_materials_dir.get() or '').strip() if hasattr(self, 'extra_materials_dir') else ''
        if extra_dir and os.path.isdir(extra_dir):
            try:
                for root, dirs, files in os.walk(extra_dir):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for f in files:
                        if f.startswith('.'):
                            continue
                        ext = os.path.splitext(f)[1].lower()
                        if ext in SUPPORTED_IMAGE or ext in SUPPORTED_VIDEO:
                            full = os.path.normpath(os.path.join(root, f))
                            if full not in paths:
                                paths.append(full)
            except Exception:
                pass
        paths.sort()
        return paths
    
    def open_manual_order_dialog(self):
        """Диалог настройки ручного порядка материалов: список, вверх/вниз, сохранить в файл."""
        order_file = os.path.join(self.project_dir, "manual_media_order.json")
        saved_order = []
        if os.path.isfile(order_file):
            try:
                with open(order_file, 'r', encoding='utf-8', errors='replace') as f:
                    saved_order = json.load(f)
                if not isinstance(saved_order, list):
                    saved_order = []
            except Exception:
                saved_order = []
        
        dialog = Toplevel(self.winfo_toplevel())
        dialog.title(_('material_order_dialog_title'))
        dialog.geometry("520x420")
        dialog.transient(self.winfo_toplevel())
        
        main = Frame(dialog, padx=15, pady=15)
        main.pack(fill="both", expand=True)
        Label(main, text=_('material_order_dialog_title'), font=("Arial", 10, "bold")).pack(anchor="w")
        list_frame = Frame(main)
        list_frame.pack(fill="both", expand=True, pady=5)
        listbox = Listbox(list_frame, height=14, font=("Consolas", 9), selectmode="single")
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        
        paths_in_order = []  # текущий порядок путей (полные пути)
        
        def refresh_list():
            scanned = self.get_media_list_for_project()
            existing_saved = [p for p in saved_order if p in scanned]
            rest = [p for p in scanned if p not in existing_saved]
            paths_in_order[:] = existing_saved + rest
            listbox.delete(0, END)
            for p in paths_in_order:
                try:
                    listbox.insert(END, os.path.relpath(p, self.project_dir))
                except ValueError:
                    listbox.insert(END, os.path.basename(p))
        
        def _display_path(p):
            try:
                return os.path.relpath(p, self.project_dir)
            except ValueError:
                return os.path.basename(p)
        
        def on_up():
            sel = listbox.curselection()
            if not sel or sel[0] == 0:
                return
            i = sel[0]
            paths_in_order[i], paths_in_order[i - 1] = paths_in_order[i - 1], paths_in_order[i]
            listbox.delete(0, END)
            for p in paths_in_order:
                listbox.insert(END, _display_path(p))
            listbox.selection_set(i - 1)
        
        def on_down():
            sel = listbox.curselection()
            if not sel or sel[0] >= len(paths_in_order) - 1:
                return
            i = sel[0]
            paths_in_order[i], paths_in_order[i + 1] = paths_in_order[i + 1], paths_in_order[i]
            listbox.delete(0, END)
            for p in paths_in_order:
                listbox.insert(END, _display_path(p))
            listbox.selection_set(i + 1)
        
        def on_shuffle():
            import random
            random.shuffle(paths_in_order)
            listbox.delete(0, END)
            for p in paths_in_order:
                listbox.insert(END, _display_path(p))
        
        def on_shuffle_videos_first():
            """Видео вначало, затем картинки — оба блока перемешаны."""
            import random
            _vid = ['.mp4', '.mov', '.avi', '.mkv', '.webm']
            _img = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.ico', '.gif']
            videos = [p for p in paths_in_order if os.path.splitext(p)[1].lower() in _vid]
            images = [p for p in paths_in_order if os.path.splitext(p)[1].lower() in _img]
            random.shuffle(videos)
            random.shuffle(images)
            paths_in_order[:] = videos + images
            listbox.delete(0, END)
            for p in paths_in_order:
                listbox.insert(END, _display_path(p))
        
        def save_order():
            try:
                with open(order_file, 'w', encoding='utf-8', errors='replace') as f:
                    json.dump(paths_in_order, f, indent=2, ensure_ascii=False)
                if hasattr(self, '_material_order_displays') and 'manual' in self._material_order_keys:
                    self.material_order_var.set(self._material_order_displays[self._material_order_keys.index('manual')])
                self.log(_('material_order_saved'), kind="render")
            except Exception as e:
                self.log(str(e), kind="render")
        
        refresh_list()
        btn_row = Frame(main)
        btn_row.pack(fill="x", pady=5)
        Button(btn_row, text=_('material_order_refresh'), command=refresh_list, width=16).pack(side="left", padx=2)
        Button(btn_row, text="↑", command=on_up, width=3).pack(side="left", padx=2)
        Button(btn_row, text="↓", command=on_down, width=3).pack(side="left", padx=2)
        Button(btn_row, text="🎲 Перемешать", command=on_shuffle, width=14).pack(side="left", padx=2)
        Button(btn_row, text="🎬 Видео вначало", command=on_shuffle_videos_first, width=16).pack(side="left", padx=2)
        Button(btn_row, text=_('material_order_save'), command=save_order, width=18).pack(side="left", padx=2)
        Button(main, text=_('close'), command=dialog.destroy, width=12).pack(pady=8)
    
    def _load_media_scale_offset_file(self):
        """Загружает media_per_item из media_scale_offset.json в папке проекта."""
        path = os.path.join(self.project_dir, "media_scale_offset.json")
        if not os.path.isfile(path):
            self.media_per_item = {}
            return
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            self.media_per_item = data if isinstance(data, dict) else {}
        except Exception:
            self.media_per_item = {}
    
    def _save_media_scale_offset_file(self):
        """Сохраняет self.media_per_item в media_scale_offset.json."""
        path = os.path.join(self.project_dir, "media_scale_offset.json")
        try:
            with open(path, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(self.media_per_item, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    
    def _get_media_per_item_for_render(self):
        """Загружает и возвращает media_per_item из файла проекта (для передачи в воркер рендера)."""
        path = os.path.join(self.project_dir, "media_scale_offset.json")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _start_upload(self, video_path=None):
        """Заливка готового mp4 проекта на привязанный канал (ручная кнопка или авто после рендера)."""
        if upload_manager is None:
            self.log("❌ Модуль заливки недоступен (upload_manager не загрузился)", kind="render")
            return
        name = self.upload_channel_var.get().strip()
        if not name:
            self.log("⚠️ Заливка: не выбран канал (выпадашка «Канал»)", kind="render")
            return
        entry = upload_manager.Channels.get(name)
        if not entry:
            self.log(f"⚠️ Заливка: канал «{name}» не найден в списке каналов", kind="render")
            return
        vp = video_path
        if not vp:
            try:
                mp4s = [os.path.join(self.project_dir, f) for f in os.listdir(self.project_dir)
                        if f.lower().endswith('.mp4') and not f.startswith('.')]
            except Exception:
                mp4s = []
            if not mp4s:
                self.log("⚠️ Заливка: в проекте нет mp4 — сначала отрендерь", kind="render")
                return
            vp = max(mp4s, key=os.path.getmtime)
        title = self.project_name
        thumb = ""
        try:
            thumb = upload_manager.find_thumbnail(self.project_name)
        except Exception:
            thumb = ""

        # Главы в описание: только если они посчитаны по ЭТОМУ ролику (длительность сходится).
        desc = ""
        if chapters is not None:
            try:
                desc = chapters.load_description(self.project_dir,
                                                 video_duration=self._get_audio_duration_ffprobe(vp))
            except Exception as e:
                self.log(f"⚠️ Главы: описание не прочитано ({e}) — заливаю без таймкодов", kind="render")
                desc = ""

        def _run():
            _tinfo = f" + обложка «{os.path.basename(thumb)}»" if thumb else " (обложка не найдена)"
            self.log(f"📤 Заливка «{os.path.basename(vp)}» → канал «{name}» (профиль {entry.get('profile_id')}){_tinfo}…", kind="render")
            if desc:
                self.log(f"   📑 В описание уходят главы: {len(desc.splitlines())} шт", kind="render")
            try:
                res = upload_manager.run_upload(self.project_dir, vp, title, desc, entry,
                                                thumbnail=thumb,
                                                log_callback=lambda l: self.log(l, kind="render"))
            except Exception as e:
                self.log(f"❌ Заливка: исключение {e}", kind="render")
                return
            if res.get('success'):
                tag = " (уже было залито)" if res.get('skipped') else ""
                self.log(f"✅ Залито{tag}: {res.get('url')}", kind="render")
            else:
                self.log(f"❌ Заливка не удалась: {res.get('error')}", kind="render")
        threading.Thread(target=_run, daemon=True).start()

    def run_chapters(self, log_kind="render"):
        """Считает главы по chapters/transcript.json (его пишет рендер в момент whisper-прогона).

        БЛОКИРУЮЩАЯ: зовётся из рабочего потока рендера, чтобы описание было готово
        до авто-залива. True = главы записаны и годны к подстановке в описание.
        """
        if chapters is None:
            self.log("⚠️ Главы: модуль chapters недоступен в этой сборке", kind=log_kind)
            return False
        try:
            res = chapters.run_project(self.project_dir,
                                       log=lambda m: self.log(m, kind=log_kind))
        except chapters.ChaptersError as e:
            self.log(f"❌ Главы: {e}", kind=log_kind)
            return False
        except Exception as e:
            self.log(f"❌ Главы: неожиданная ошибка — {e}", kind=log_kind)
            return False
        if res.get("status") != "ok":
            self.log("❌ Главы: результат забракован, описание НЕ записано", kind=log_kind)
            return False
        self.log(f"✅ Главы: {len(res['chapters'])} шт → chapters/description.txt", kind=log_kind)
        return True

    def _start_chapters(self):
        """Кнопка «📑 Сейчас»: считает главы в фоне, UI не блокируется."""
        threading.Thread(target=self.run_chapters, daemon=True).start()

    def open_media_scale_offset_dialog(self):
        """Диалог: масштаб и смещение по элементам. Список медиа, «Применить ко всем» — взять текущие значения из формы."""
        self._load_media_scale_offset_file()
        scanned = self.get_media_list_for_project()
        if not scanned:
            self.log(_('media_scale_offset_dialog_title') + "\n\nНет изображений/видео в проекте.", kind="render")
            return
        dialog = Toplevel(self.winfo_toplevel())
        dialog.title(_('media_scale_offset_dialog_title'))
        dialog.geometry("560x380")
        dialog.transient(self.winfo_toplevel())
        main = Frame(dialog, padx=15, pady=15)
        main.pack(fill="both", expand=True)
        Label(main, text=_('media_scale_offset_dialog_title'), font=("Arial", 10, "bold")).pack(anchor="w")
        list_frame = Frame(main)
        list_frame.pack(fill="both", expand=True, pady=5)
        listbox = Listbox(list_frame, height=12, font=("Consolas", 9), selectmode="single")
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        for p in scanned:
            try:
                listbox.insert(END, os.path.relpath(p, self.project_dir))
            except ValueError:
                listbox.insert(END, os.path.basename(p))
        def apply_to_all():
            try:
                scale_val = self.media_scale_pct_var.get().strip() or "100"
                x_val = self.media_offset_x_var.get().strip() or "0"
                y_val = self.media_offset_y_var.get().strip() or "0"
                for p in scanned:
                    norm = os.path.normpath(os.path.abspath(p))
                    self.media_per_item[norm] = {
                        'scale_pct': float(scale_val),
                        'offset_x': int(x_val),
                        'offset_y': int(y_val)
                    }
                self._save_media_scale_offset_file()
                self.log(_('media_scale_apply_all') + " — сохранено.", kind="render")
            except (ValueError, TypeError) as e:
                self.log(f"Неверные числа: {e}", kind="render")
        def clear_per_item():
            self.media_per_item.clear()
            self._save_media_scale_offset_file()
            self.log("Индивидуальные настройки сброшены. Будет использован общий масштаб/смещение.", kind="render")
        btn_row = Frame(main)
        btn_row.pack(fill="x", pady=5)
        Button(btn_row, text=_('media_scale_apply_all'), command=apply_to_all, width=20).pack(side="left", padx=2)
        Button(btn_row, text="Сбросить по элементам", command=clear_per_item, width=22).pack(side="left", padx=2)
        Button(main, text=_('close'), command=dialog.destroy, width=12).pack(pady=8)
    
    def _auto_save_voice_mapping(self):
        """Автосохранение голоса для текущего проекта/языка."""
        try:
            engine = self.tts_engine.get()
            lang = self.selected_edge_voice.get().split('-')[0] if engine == 'edge' else 'all'
            voice_settings = {
                'engine': engine,
                'edge_voice': self.selected_edge_voice.get(),
                'voicer_voice_id': self.voicer_voice_id.get() if hasattr(self, 'voicer_voice_id') else '',
                'voicer_model_id': self.voicer_model_id.get() if hasattr(self, 'voicer_model_id') else '',
                'voice_template': self.selected_voice_template.get(),
            }
            VoiceMapping.save_voice(self.project_name, lang, voice_settings)
        except Exception:
            pass

    def _auto_load_voice_mapping(self):
        """Автозагрузка голоса для текущего проекта/языка."""
        try:
            engine = self.tts_engine.get()
            lang = self.selected_edge_voice.get().split('-')[0] if engine == 'edge' else 'all'
            settings = VoiceMapping.get_voice(self.project_name, lang)
            if settings:
                if 'engine' in settings:
                    self.tts_engine.set(settings['engine'])
                if 'edge_voice' in settings:
                    self.selected_edge_voice.set(settings['edge_voice'])
                if 'voicer_voice_id' in settings and hasattr(self, 'voicer_voice_id'):
                    self.voicer_voice_id.set(settings['voicer_voice_id'])
                if 'voicer_model_id' in settings and hasattr(self, 'voicer_model_id'):
                    self.voicer_model_id.set(settings['voicer_model_id'])
                if 'voice_template' in settings:
                    self.selected_voice_template.set(settings['voice_template'])
                self.on_engine_change()
                self.log(f"🔊 Голос загружен из маппинга для '{self.project_name}'", kind="tts")
        except Exception:
            pass

    def _get_material_order_key(self):
        """Возвращает ключ порядка материалов (video_first, images_first, ...) из текущего значения комбобокса."""
        if not hasattr(self, '_material_order_keys') or not hasattr(self, '_material_order_displays'):
            return 'video_first'
        cur = self.material_order_var.get()
        if cur in self._material_order_displays:
            return self._material_order_keys[self._material_order_displays.index(cur)]
        return 'video_first'
    
    def _set_action_buttons_state(self, group, state):
        """Блокирует/разблокирует кнопки действий (защита от двойного запуска).
        group: 'tts', 'render', 'images', 'thief'
        state: 'disabled' or 'normal'
        """
        btn_map = {
            'tts': ['start_tts_btn', 'start_tts_full_btn'],
            'render': ['assemble_video_btn'],
            'images': ['generate_images_btn', 'download_bing_btn'],
            'videos': ['generate_video_btn', 'auto_video_btn'],
            'thief': ['extract_btn', 'transform_btn', 'full_cycle_btn'],
        }
        for key in btn_map.get(group, []):
            try:
                if key in self.ui_widgets:
                    self.ui_widgets[key].config(state=state)
            except Exception:
                pass

    def update_theme_colors(self, theme):
        """Обновляет цвета tk.Button при смене темы. Сохраняет семантику кнопок."""
        if theme == "dark":
            action_colors = {
                'start_tts_btn': ("#1565C0", "white"),
                'start_tts_full_btn': ("#E65100", "white"),
                'stop_tts_btn': ("#616161", "white"),
                'download_bing_btn': ("#2E7D32", "white"),
                'stop_images_btn': ("#616161", "white"),
                'generate_images_btn': ("#6A1B9A", "white"),
                'stop_googler_btn': ("#616161", "white"),
                'generate_video_btn': ("#D84315", "white"),
                'stop_video_btn': ("#616161", "white"),
                'auto_video_btn': ("#E65100", "white"),
                'assemble_video_btn': ("#1565C0", "white"),
                'stop_render_btn': ("#616161", "white"),
                'extract_btn': ("#2E7D32", "white"),
                'transform_btn': ("#6A1B9A", "white"),
                'full_cycle_btn': ("#1565C0", "white"),
                'stop_thief_btn': ("#616161", "white"),
            }
            neutral = ("#424242", "#E0E0E0")
        else:
            action_colors = {
                'start_tts_btn': ("#2196F3", "white"),
                'start_tts_full_btn': ("#FF9800", "white"),
                'stop_tts_btn': ("#757575", "white"),
                'download_bing_btn': ("#4CAF50", "white"),
                'stop_images_btn': ("#757575", "white"),
                'generate_images_btn': ("#9C27B0", "white"),
                'stop_googler_btn': ("#757575", "white"),
                'generate_video_btn': ("#FF5722", "white"),
                'stop_video_btn': ("#757575", "white"),
                'auto_video_btn': ("#FF9800", "white"),
                'assemble_video_btn': ("#2196F3", "white"),
                'stop_render_btn': ("#757575", "white"),
                'extract_btn': ("#4CAF50", "white"),
                'transform_btn': ("#9C27B0", "white"),
                'full_cycle_btn': ("#2196F3", "white"),
                'stop_thief_btn': ("#757575", "white"),
            }
            neutral = ("#e0e0e0", "#000000")

        for key, (bg, fg) in action_colors.items():
            if key in self.ui_widgets:
                try:
                    w = self.ui_widgets[key]
                    if w.winfo_exists() and w.winfo_class() == 'Button':
                        w.config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
                except Exception:
                    pass

        for key, w in self.ui_widgets.items():
            if key in action_colors:
                continue
            try:
                if w.winfo_exists() and w.winfo_class() == 'Button':
                    w.config(bg=neutral[0], fg=neutral[1],
                             activebackground=neutral[0], activeforeground=neutral[1])
            except Exception:
                pass

        try:
            if hasattr(self, 'queue_status_label'):
                qbg = "#404040" if theme == "dark" else "#e8e8e8"
                qfg = "white" if theme == "dark" else "#000000"
                self.queue_status_label.config(bg=qbg, fg=qfg)
                if self.queue_status_label.master:
                    self.queue_status_label.master.config(bg=qbg)
        except Exception:
            pass

    def get_current_settings(self):
        """Получить текущие настройки (БЕЗ source_text_file для пресета)"""
        return {
            # НЕ сохраняем source_text_file в пресет
            'image_search_query': self.image_search_query.get(),
            'num_images_per_tag': self.num_images_per_tag.get(),
            'tts_engine': self.tts_engine.get(),
            'selected_voice_template': self.selected_voice_template.get(),
            'selected_edge_voice': self.selected_edge_voice.get(),
            'auto_detect_language': self.auto_detect_language.get(),
            'voicer_voice_id': self.voicer_voice_id.get() if hasattr(self, 'voicer_voice_id') else 'AB9XsbSA4eLG12t2myjN',
            'voicer_model_id': self.voicer_model_id.get() if hasattr(self, 'voicer_model_id') else 'eleven_multilingual_v2',
            'vgc_voice_id': self.vgc_voice_id.get() if hasattr(self, 'vgc_voice_id') else '',
            'vgc_voice_engine': self.vgc_voice_engine.get() if hasattr(self, 'vgc_voice_engine') else 'elevenLabsV3',
            'vgc_stability': self.vgc_stability.get() if hasattr(self, 'vgc_stability') else '0.5',
            'tts_speed': self.tts_speed.get() if hasattr(self, 'tts_speed') else '1.0',
            'vgc_thread_count': self.vgc_thread_count.get() if hasattr(self, 'vgc_thread_count') else '10',
            'selected_voice_set': self.selected_voice_set.get() if hasattr(self, 'selected_voice_set') else '— нет —',
            'voicer_split_type': self.voicer_split_type.get() if hasattr(self, 'voicer_split_type') else 'smart',
            'voicer_auto_pause_enabled': self.voicer_auto_pause_enabled.get() if hasattr(self, 'voicer_auto_pause_enabled') else False,
            'voicer_auto_pause_duration': self.voicer_auto_pause_duration.get() if hasattr(self, 'voicer_auto_pause_duration') else '0.5',
            'voicer_auto_pause_frequency': self.voicer_auto_pause_frequency.get() if hasattr(self, 'voicer_auto_pause_frequency') else '1',
            'video_quality': self.video_quality.get(),
            'min_image_duration': self.min_image_duration_var.get(),
            'max_image_duration': self.max_image_duration_var.get(),
            'subtitle_language': self.subtitle_language_var.get(),
            'pause_duration': self.pause_duration_var.get(),
            'pause_interval': self.pause_interval_var.get(),
            'background_audio_file': self.background_audio_file.get(),
            'main_audio_volume': self.main_audio_volume_var.get(),
            'background_audio_volume': self.background_audio_volume_var.get(),
            'intro_video_files': list(self.intro_video_files),
            'end_track_file': self.end_track_file.get(),
            'end_track_volume': self.end_track_volume_var.get(),
            'use_cache': self.use_cache.get(),
            'add_subtitles': self.add_subtitles_var.get(),
            'sync_audio': self.sync_audio_var.get(),
            'auto_upload': self.auto_upload_var.get(),
            'auto_chapters': self.auto_chapters_var.get(),
            'distribute_evenly': self.distribute_evenly_var.get(),
            'vintage_effect': self.vintage_effect_var.get(),
            'randomize_metadata': self.randomize_metadata_var.get(),
            # Переходы и эффекты
            'fade_duration': self.fade_duration_var.get(),
            'enable_fade': self.enable_fade_var.get(),
            'enable_kenburns': self.enable_kenburns_var.get(),
            'use_video_audio': self.use_video_audio_var.get(),
            'kenburns_zoom': self.kenburns_zoom_var.get(),
            'material_order': self._get_material_order_key(),
            'media_scale_pct': self.media_scale_pct_var.get(),
            'media_offset_x': self.media_offset_x_var.get(),
            'media_offset_y': self.media_offset_y_var.get(),
            # GOOGLER настройки (все параметры сохраняются в пресет)
            'image_source': self.image_source.get(),  # Вариант создания: "bing" или "googler"
            'googler_style': self.googler_style.get(),
            'googler_aspect_ratio': self.googler_aspect_ratio.get(),
            'googler_sentences_per_prompt': self.googler_sentences_per_prompt.get(),
            'googler_split_long': self.googler_split_long_var.get(),  # галка «дробить длинные предложения»
            'googler_split_long_seconds': self.googler_split_long_seconds.get(),
            'googler_seconds_per_image': self.googler_seconds_per_image.get(),
            'googler_seed': self.googler_seed.get(),  # Фиксированный seed (пусто = авто)
            'image_prompt_mode': self.image_prompt_mode.get(),
            'googler_custom_prompts': self.googler_custom_prompts_text.get("1.0", "end").strip() if hasattr(self, 'googler_custom_prompts_text') else '',
            'googler_use_legacy': self.googler_use_legacy.get(),  # Чекбокс "Использовать Legacy API v2"
            'googler_copy_similar': False,  # Отключено: receipt/референсы project-owned
            'googler_model': self.googler_model.get(),  # Модель генерации: GEM_PIX_2, IMAGEN_3_5, NARWHAL, …
            'googler_video_method': self.googler_video_method.get(),
            'googler_video_prompt': self.googler_video_prompt.get(),
            'googler_video_loop': self.googler_video_loop.get(),  # Чекбокс "Зацикливание видео"
            'googler_video_first_n': self.googler_video_first_n.get(),  # Количество первых картинок для авто-видео
            'googler_video_after_images': self.googler_video_after_images.get(),  # Авто-видео после генерации изображений
            'googler_video_from_prompts': self.googler_video_from_prompts.get(),  # t2v из промптов сцен (без картинок)
            'deferred_render': bool(getattr(self, 'deferred_render', None) and self.deferred_render.get()),  # Чекбокс "Авто-рендер после завершения"
            # Персонажи и референсы
            'auto_hero': self.auto_hero_var.get(),  # Чекбокс "Авто-герой"
            'analyze_story': self.analyze_story_var.get(),  # Чекбокс "Анализ персонажей/локаций"
            # Референсы намеренно НЕ являются полями пресета: их источник истины —
            # project-owned паспорт в _ASSETS. Копирование в новый проект выполняется
            # отдельной проверенной операцией и не затрагивает существующий паспорт.
            # THIEF настройки (ИСПРАВЛЕНО - теперь сохраняются)
            'thief_transform_prompt': self.thief_transform_prompt.get(),
            'thief_sensitivity': self.thief_sensitivity.get(),
            'thief_strict': self.thief_strict.get(),
            'thief_min_duration': self.thief_min_duration.get(),
            'thief_sample_interval': self.thief_sample_interval.get(),
            'thief_source_quality': self.thief_source_quality.get(),
            # Дополнительные материалы и оверлеи
            'extra_materials_dir': self.extra_materials_dir.get(),
            'stock_video_limit': int(self.stock_video_limit_var.get() or 0),
            'overlays': [ov.copy() for ov in self.overlays] if self.overlays else []
        }
    
    def apply_settings(self, settings):
        # НЕ применяем source_text_file из пресета
        self.image_search_query.set(settings.get('image_search_query', ''))
        self.num_images_per_tag.set(settings.get('num_images_per_tag', '3'))
        self.tts_engine.set(settings.get('tts_engine', 'edge'))
        self.selected_voice_template.set(settings.get('selected_voice_template', ''))
        self.selected_edge_voice.set(settings.get('selected_edge_voice', 'ru-RU-DmitryNeural'))
        self.auto_detect_language.set(settings.get('auto_detect_language', True))
        if hasattr(self, 'voicer_voice_id'):
            self.voicer_voice_id.set(settings.get('voicer_voice_id', 'AB9XsbSA4eLG12t2myjN'))
            self.voicer_model_id.set(settings.get('voicer_model_id', 'eleven_multilingual_v2'))
            self.voicer_split_type.set(settings.get('voicer_split_type', 'smart'))
            self.voicer_auto_pause_enabled.set(settings.get('voicer_auto_pause_enabled', False))
            # VoiceGenCore
            if hasattr(self, 'vgc_voice_id'):
                self.vgc_voice_id.set(settings.get('vgc_voice_id', ''))
            if hasattr(self, 'vgc_voice_engine'):
                self.vgc_voice_engine.set(settings.get('vgc_voice_engine', 'elevenLabsV3'))
            if hasattr(self, 'vgc_stability'):
                self.vgc_stability.set(settings.get('vgc_stability', '0.5'))
            if hasattr(self, 'tts_speed'):
                self.tts_speed.set(settings.get('tts_speed', '1.0'))
            if hasattr(self, 'vgc_thread_count'):
                self.vgc_thread_count.set(settings.get('vgc_thread_count', '10'))
            self.voicer_auto_pause_duration.set(settings.get('voicer_auto_pause_duration', '0.5'))
            self.voicer_auto_pause_frequency.set(settings.get('voicer_auto_pause_frequency', '1'))
        if hasattr(self, 'selected_voice_set'):
            self.selected_voice_set.set(settings.get('selected_voice_set', '— нет —'))
            self._refresh_voice_set_combo()
            self._apply_voice_set_to_ui(log_miss=False)
        self.video_quality.set(settings.get('video_quality', '720p'))
        self.min_image_duration_var.set(settings.get('min_image_duration', '3.0'))
        self.max_image_duration_var.set(settings.get('max_image_duration', '5.0'))
        self.subtitle_language_var.set(settings.get('subtitle_language', 'Russian'))
        self.pause_duration_var.set(settings.get('pause_duration', '0.0'))
        self.pause_interval_var.set(settings.get('pause_interval', '0'))
        self.background_audio_file.set(settings.get('background_audio_file', ''))
        self.main_audio_volume_var.set(settings.get('main_audio_volume', '100'))
        self.background_audio_volume_var.set(settings.get('background_audio_volume', '20'))
        intro_data = settings.get('intro_video_files', settings.get('intro_video_file'))
        if isinstance(intro_data, list):
            self.intro_video_files = [p for p in intro_data if p]
        elif intro_data and isinstance(intro_data, str):
            self.intro_video_files = [intro_data]
        else:
            self.intro_video_files = []
        if hasattr(self, 'intro_video_listbox'):
            self._refresh_intro_video_listbox()
        self.end_track_file.set(settings.get('end_track_file', ''))
        self.end_track_volume_var.set(settings.get('end_track_volume', '100'))
        self.use_cache.set(settings.get('use_cache', True))
        # Это согласие строго one-shot и не восстанавливается ни из пресета, ни
        # из настроек другого проекта.
        self.trust_legacy_image_cache.set(False)
        self._trust_legacy_image_cache_once = False
        self._trust_legacy_image_cache_snapshot = ()
        self.add_subtitles_var.set(settings.get('add_subtitles', False))
        self.sync_audio_var.set(settings.get('sync_audio', True))
        self.use_video_audio_var.set(settings.get('use_video_audio', False))
        self.auto_upload_var.set(settings.get('auto_upload', False))
        self.auto_chapters_var.set(settings.get('auto_chapters', False))
        self.distribute_evenly_var.set(settings.get('distribute_evenly', True))
        self.vintage_effect_var.set(settings.get('vintage_effect', False))
        self.randomize_metadata_var.set(settings.get('randomize_metadata', False))
        # Переходы и эффекты
        self.fade_duration_var.set(settings.get('fade_duration', '0.5'))
        self.enable_fade_var.set(settings.get('enable_fade', False))
        self.enable_kenburns_var.set(settings.get('enable_kenburns', False))
        self.kenburns_zoom_var.set(settings.get('kenburns_zoom', '1.1'))
        mo_key = settings.get('material_order', 'video_first')
        if hasattr(self, '_material_order_keys') and hasattr(self, '_material_order_displays') and mo_key in self._material_order_keys:
            self.material_order_var.set(self._material_order_displays[self._material_order_keys.index(mo_key)])
        elif hasattr(self, '_material_order_displays'):
            self.material_order_var.set(self._material_order_displays[0])
        self.media_scale_pct_var.set(str(settings.get('media_scale_pct', 100)))
        self.media_offset_x_var.set(str(settings.get('media_offset_x', 0)))
        self.media_offset_y_var.set(str(settings.get('media_offset_y', 0)))
        # GOOGLER настройки
        self.image_source.set(settings.get('image_source', 'bing'))
        self.googler_style.set(settings.get('googler_style', 'cinematic, photorealistic'))
        self.googler_aspect_ratio.set(settings.get('googler_aspect_ratio', 'landscape'))
        self.googler_sentences_per_prompt.set(settings.get('googler_sentences_per_prompt', '3'))
        if hasattr(self, 'googler_split_long_var'):
            self.googler_split_long_var.set(settings.get('googler_split_long', True))
        if hasattr(self, 'googler_split_long_seconds'):
            self.googler_split_long_seconds.set(str(settings.get('googler_split_long_seconds') or '6'))
        self.googler_seconds_per_image.set(settings.get('googler_seconds_per_image', ''))
        self.googler_seed.set(settings.get('googler_seed', ''))
        saved_prompt_mode = settings.get('image_prompt_mode')
        if saved_prompt_mode in ('gpt', 'custom', 'existing'):
            prompt_mode = saved_prompt_mode
        elif saved_prompt_mode is not None:
            prompt_mode = 'existing'
            self.log(
                f"❌ Неизвестный режим промптов в настройках: {saved_prompt_mode!r}; "
                "выбран безопасный режим готового prompts.json. Выберите режим явно.",
                kind="images",
            )
        elif 'use_gpt_prompts' in settings or 'use_custom_prompts' in settings:
            legacy_gpt = bool(settings.get('use_gpt_prompts', False))
            legacy_custom = bool(settings.get('use_custom_prompts', False))
            if legacy_gpt and legacy_custom:
                # Historically GPT silently won.  Preserve the user's explicit
                # custom opt-in instead and make the migration visible.
                prompt_mode = 'custom'
                self.log(
                    "⚠️ Старый пресет одновременно включал GPT и кастомные промты; "
                    "явно выбран режим «Кастомные промты». Проверьте текст перед запуском.",
                    kind="images",
                )
            elif legacy_custom:
                prompt_mode = 'custom'
            elif legacy_gpt:
                prompt_mode = 'gpt'
            else:
                prompt_mode = 'existing'
        else:
            prompt_mode = 'gpt'
        self.image_prompt_mode.set(prompt_mode)
        if hasattr(self, 'googler_custom_prompts_text'):
            self.googler_custom_prompts_text.delete("1.0", "end")
            self.googler_custom_prompts_text.insert("1.0", settings.get('googler_custom_prompts', ''))
        self.googler_use_legacy.set(settings.get('googler_use_legacy', False))
        if hasattr(self, 'googler_copy_similar'):
            self._apply_cross_project_image_reuse_setting(
                settings.get('googler_copy_similar', False)
            )
        saved_model = settings.get('googler_model', 'OPENAI')
        if saved_model in GOOGLER_IMAGE_MODEL_IDS:
            self.googler_model.set(saved_model)
        else:
            # Не заменяем провайдера молча: preflight остановит генерацию, пока
            # пользователь явно не выберет зарегистрированную модель.
            self.googler_model.set(saved_model)
            self.log(
                f"❌ Сохранённая модель картинок {saved_model!r} больше не поддерживается; выберите модель заново.",
                kind="images",
            )
        try:
            idx = GOOGLER_IMAGE_MODEL_IDS.index(self.googler_model.get())
            if 'googler_model_combo' in self.ui_widgets:
                self.ui_widgets['googler_model_combo'].current(idx)
            if 'thief_model_combo' in self.ui_widgets:
                self.ui_widgets['thief_model_combo'].current(idx)
        except (ValueError, AttributeError, KeyError):
            pass
        # НОВОЕ: Персонажи и референсы
        self.auto_hero_var.set(settings.get('auto_hero', False))
        if hasattr(self, 'analyze_story_var'):
            self.analyze_story_var.set(settings.get('analyze_story', False))
        refs_fields_present = (
            'user_references' in settings or 'style_references' in settings
        )
        if refs_fields_present:
            self.log(
                "ℹ️ Референсы из старого пресета пропущены: сохранён project-owned "
                "паспорт текущего проекта.",
                kind="images",
            )
        self.googler_video_prompt.set(settings.get('googler_video_prompt', ''))
        self.googler_video_loop.set(settings.get('googler_video_loop', True))
        self.googler_video_first_n.set(settings.get('googler_video_first_n', '0'))
        # Автозапуск видео после изображений может быть не в старых пресетах
        self.googler_video_after_images.set(settings.get('googler_video_after_images', False))
        if hasattr(self, 'googler_video_from_prompts'):
            self.googler_video_from_prompts.set(settings.get('googler_video_from_prompts', False))
        # Авто-рендер после завершения (может не быть в старых пресетах)
        if hasattr(self, 'deferred_render'):
            self.deferred_render.set(settings.get('deferred_render', False))
        saved_video_method = settings.get('googler_video_method', 'veo')
        # мёртвые методы (whisk_legacy/veo_flow/gemini_veo) авто-мигрируют на veo
        if saved_video_method in ["veo", "flower", "flow", "grok"]:
            self.googler_video_method.set(saved_video_method)
        else:
            self.googler_video_method.set("veo")
        # THIEF настройки (ИСПРАВЛЕНО - теперь восстанавливаются)
        self.thief_transform_prompt.set(settings.get('thief_transform_prompt', 'Transform this slide into a professional illustration, maintain the same composition and information'))
        self.thief_sensitivity.set(settings.get('thief_sensitivity', '0.70'))
        self.thief_strict.set(settings.get('thief_strict', True))
        self.thief_min_duration.set(settings.get('thief_min_duration', '2.0'))
        self.thief_sample_interval.set(settings.get('thief_sample_interval', '1.0'))
        self.thief_source_quality.set(settings.get('thief_source_quality', 'best'))
        # Дополнительные материалы и оверлеи
        self.extra_materials_dir.set(settings.get('extra_materials_dir', ''))
        self.stock_video_limit_var.set(str(settings.get('stock_video_limit', 0)))
        overlays_data = settings.get('overlays', [])
        self.overlays = []
        for ov in overlays_data:
            if isinstance(ov, dict) and 'path' in ov and os.path.exists(ov['path']):
                self.overlays.append({
                    'path': ov['path'],
                    'opacity': float(ov.get('opacity', 0.5)),
                    'order': int(ov.get('order', len(self.overlays)))
                })
        if hasattr(self, 'overlay_listbox'):
            self._refresh_overlay_listbox()
        # Обновляем UI
        self.on_engine_change()
        self.on_image_source_change()

    def save_preset(self):
        name = self.preset_combo.get()
        # Если выбран пункт "Новый" или ничего не выбрано — показываем диалог создания
        if not name or name == _('preset_new'):
            dialog = Toplevel(self)
            dialog.title(_('save_preset'))
            dialog.geometry("300x100")
            dialog.transient(self.winfo_toplevel())
            dialog.grab_set()
            Label(dialog, text=_('preset_name')).pack(pady=10)
            name_var = StringVar()
            Entry(dialog, textvariable=name_var, width=30).pack(pady=5)
            def save_action():
                preset_name = name_var.get().strip()
                if not preset_name:
                    self.log(_('enter_name'), kind="tts")
                    return
                if self.preset_manager.add_preset(preset_name, self.get_current_settings()):
                    # Обновляем список и выбираем только что созданный пресет
                    self.update_preset_list()
                    try:
                        values = list(self.preset_combo['values'])
                        idx = values.index(preset_name) if preset_name in values else 0
                        self.preset_combo.current(idx)
                    except:
                        pass
                    dialog.destroy()
            Button(dialog, text=_('save'), command=save_action).pack(pady=5)
        else:
            if messagebox.askyesno(_('confirm'), _('overwrite_preset').format(name=name)):
                self.preset_manager.add_preset(name, self.get_current_settings())
    
    def _on_preset_selected(self, event=None):
        """Автоматическое применение пресета при выборе в списке (без кнопки активации)."""
        name = self.preset_combo.get()
        if not name or name == _('preset_new'):
            return
        settings = self.preset_manager.get_preset(name)
        if settings:
            self.apply_settings(settings)
    
    def load_preset(self):
        """Применить выбранный пресет (вызывается при необходимости программно)."""
        name = self.preset_combo.get()
        if not name or name == _('preset_new'):
            self.log(_('select_preset'), kind="render")
            return
        settings = self.preset_manager.get_preset(name)
        if settings:
            self.apply_settings(settings)
    
    def delete_preset(self):
        name = self.preset_combo.get()
        if not name or name == _('preset_new'):
            self.log(_('select_preset'), kind="render")
            return
        if messagebox.askyesno(_('confirm'), _('delete_preset_confirm').format(name=name)):
            if self.preset_manager.delete_preset(name):
                self.log(_('preset_deleted'), kind="render")
                self.update_preset_list()
    
    def on_engine_change(self):
        engine = self.tts_engine.get()
        self.elevenlabs_frame.grid_remove()
        self.edge_frame.grid_remove()
        self.voicer_frame.grid_remove()
        self.voicegencore_frame.grid_remove()
        if engine == "elevenlabs":
            self.elevenlabs_frame.grid()
            self.load_elevenlabs_templates()
        elif engine == "voicer":
            self.voicer_frame.grid()
        elif engine == "voicegencore":
            self.voicegencore_frame.grid()
        else:
            self.edge_frame.grid()
            self.on_auto_detect_change()
    
    def on_auto_detect_change(self):
        """Обработчик изменения чекбокса автоопределения языка"""
        # Больше не отключаем комбобокс - пользователь должен видеть выбранный голос
        pass
    
    def load_elevenlabs_templates(self):
        try:
            if os.path.exists(TEMPLATE_CONFIG_PATH):
                with open(TEMPLATE_CONFIG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                    config = json.load(f)
                    templates = config.get('elevenlabs', {})
                    values = list(templates.keys())
                    self.voice_template_combo['values'] = values
                    # Если в пресете сохранено имя шаблона — выберем его
                    desired = self.selected_voice_template.get() or ""
                    if desired and desired in values:
                        try:
                            self.voice_template_combo.current(values.index(desired))
                        except:
                            pass
                    elif values:
                        # Иначе не навязываем выбор, но если пусто — выберем первый для удобства
                        if not self.selected_voice_template.get():
                            self.voice_template_combo.current(0)
        except:
            pass

    def ensure_templates_file(self):
        if not os.path.exists(TEMPLATE_CONFIG_PATH):
            try:
                with open(TEMPLATE_CONFIG_PATH, 'w', encoding='utf-8', errors='replace') as f:
                    json.dump({"elevenlabs": {}}, f, indent=2, ensure_ascii=False)
            except:
                pass

    def open_elevenlabs_template_editor(self):
        """Диалог создания/редактирования шаблона ElevenLabs (name, voice_uuid, model_uuid)."""
        self.ensure_templates_file()
        # Загружаем текущие шаблоны
        try:
            with open(TEMPLATE_CONFIG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                config = json.load(f)
        except:
            config = {"elevenlabs": {}}
        if "elevenlabs" not in config:
            config["elevenlabs"] = {}

        dialog = Toplevel(self)
        dialog.title("Шаблон ElevenLabs")
        dialog.geometry("420x220")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        Label(dialog, text="Название шаблона:").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        name_var = StringVar(value=self.selected_voice_template.get())
        Entry(dialog, textvariable=name_var, width=35).grid(row=0, column=1, sticky="w")

        Label(dialog, text="Voice UUID:").grid(row=1, column=0, sticky="w", padx=8, pady=8)
        voice_var = StringVar()
        Label(dialog, text="Model UUID (опц.):").grid(row=2, column=0, sticky="w", padx=8, pady=8)
        model_var = StringVar()

        # Если выбран существующий шаблон — подставим значения
        existing = config["elevenlabs"].get(name_var.get() or "", {})
        voice_var.set(existing.get("voice_uuid", ""))
        model_var.set(existing.get("model_uuid", ""))

        Entry(dialog, textvariable=voice_var, width=35).grid(row=1, column=1, sticky="w")
        Entry(dialog, textvariable=model_var, width=35).grid(row=2, column=1, sticky="w")

        def save_template():
            name = (name_var.get() or "").strip()
            voice_uuid = (voice_var.get() or "").strip()
            model_uuid = (model_var.get() or "").strip()
            if not name or not voice_uuid:
                self.log("⚠️ Укажите имя и Voice UUID", kind="tts")
                return
            config["elevenlabs"][name] = {"voice_uuid": voice_uuid, "model_uuid": model_uuid}
            try:
                with open(TEMPLATE_CONFIG_PATH, 'w', encoding='utf-8', errors='replace') as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                self.load_elevenlabs_templates()
                # Выбираем только что сохраненный шаблон
                try:
                    idx = list(self.voice_template_combo['values']).index(name)
                    self.voice_template_combo.current(idx)
                except:
                    pass
                dialog.destroy()
                self.log(f"Шаблон '{name}' сохранен", kind="tts")
            except Exception as e:
                self.log(f"Не удалось сохранить шаблон: {e}", kind="tts")

        Button(dialog, text="Сохранить", command=save_template, bg="#4CAF50", fg="white").grid(row=3, column=0, columnspan=2, pady=12)

    def delete_elevenlabs_template(self):
        """Удаление выбранного шаблона ElevenLabs."""
        selected = self.selected_voice_template.get()
        if not selected:
            self.log("⚠️ Сначала выберите шаблон", kind="tts")
            return
        if not os.path.exists(TEMPLATE_CONFIG_PATH):
            self.log("⚠️ Файл шаблонов не найден", kind="tts")
            return
        try:
            with open(TEMPLATE_CONFIG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                config = json.load(f)
            templates = config.get("elevenlabs", {})
            if selected not in templates:
                self.log("⚠️ Шаблон не найден в файле", kind="tts")
                return
            if not messagebox.askyesno("Подтверждение", f"Удалить шаблон '{selected}'?"):
                return
            del templates[selected]
            config["elevenlabs"] = templates
            with open(TEMPLATE_CONFIG_PATH, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            self.load_elevenlabs_templates()
            self.selected_voice_template.set("")
            self.log("Шаблон удален", kind="tts")
        except Exception as e:
            self.log(f"Не удалось удалить: {e}", kind="tts")
    
    def select_source_file(self):
        # Начальная директория - projects (где фрилансер загружает файлы через FileBrowser)
        initial_dir = BASE_DIR_ROOT if os.path.exists(BASE_DIR_ROOT) else os.path.expanduser("~")
        filenames = filedialog.askopenfilenames(
            title="Выберите текстовые файлы", 
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
            initialdir=initial_dir
        )
        if not filenames:
            return
        
        # Если выбран только один файл - обрабатываем как раньше
        if len(filenames) == 1:
            filename = filenames[0]
            self.source_text_file.set(filename)
            # Если выбран набор голосов — видимо заполнить поле Voice ID по языку этого текста
            self._apply_voice_set_to_ui(filename)

            # НОВОЕ: Автоопределение языка сразу при выборе файла
            if self.auto_detect_language.get() and LANGDETECT_AVAILABLE and self.tts_engine.get() == 'edge':
                try:
                    lang_code, voice, subtitle_lang = detect_language_from_file(filename)
                    self.selected_edge_voice.set(voice)
                    self.subtitle_language_var.set(subtitle_lang)
                    print(f"🔍 Автоопределен язык из файла: {lang_code} → голос: {voice}, субтитры: {subtitle_lang}")
                except Exception as e:
                    print(f"Ошибка автоопределения: {e}")
            
            # Сохраняем текущую reference mapping до переключения project_dir:
            # если у нового проекта ещё нет своего паспорта, bytes будут
            # переимпортированы внутрь него, а не останутся ссылками на старую папку.
            previous_manual_refs = {
                name: list(paths) for name, paths in self.user_references.items()
            }
            previous_style_refs = list(self.style_references)
            reference_copy_allowed = True
            reference_copy_error = ""
            try:
                self._build_current_reference_bundle()
            except ReferenceContractError as exc:
                reference_copy_allowed = False
                reference_copy_error = str(exc)
                self.log(
                    f"❌ Референсы нельзя перенести в новый проект: {exc}",
                    kind="images",
                )

            # Обновляем имя проекта на основе имени файла (санитайз: хвостовые пробелы/спецсимволы)
            new_name = sanitize_project_name(os.path.splitext(os.path.basename(filename))[0])
            # Убираем СЕБЯ из словаря ДО дедупа (чтобы не конфликтовать с собственным старым именем)
            try:
                for key, val in list(self.app_reference.projects.items()):
                    if val is self:
                        del self.app_reference.projects[key]
                        break
            except Exception:
                pass
            new_name = self.app_reference._unique_project_name(new_name)  # дедуп среди остальных
            # Переименовываем вкладку
            try:
                self.app_reference.notebook.tab(self, text=new_name)
            except:
                pass
            # Обновляем директорию проекта (нормализуем путь для кириллицы)
            self.project_name = new_name
            target_project_dir = os.path.normpath(
                os.path.join(BASE_DIR_ROOT, new_name)
            )
            target_dir_existed = os.path.isdir(target_project_dir)
            self.project_dir = target_project_dir
            os.makedirs(self.project_dir, exist_ok=True)
            self._reference_state_revision = None
            self._reference_state_commit_id = None
            self._reference_state_token = None
            self._reference_copy_blocker = None
            self._switch_reference_project(
                previous_manual_refs,
                previous_style_refs,
                copy_allowed=(reference_copy_allowed and not target_dir_existed),
                copy_error=(
                    reference_copy_error
                    if not reference_copy_allowed else
                    "целевая папка уже существовала без паспорта; референсы "
                    "не скопированы автоматически"
                ),
            )
            try:
                self.app_reference.projects[new_name] = self
            except Exception:
                pass
        else:
            # Если выбрано несколько файлов - создаем новые проекты
            for filename in filenames:
                # Санитайз (хвостовые пробелы/спецсимволы) + дедуп (не делить папку между текстами)
                new_name = self.app_reference._unique_project_name(
                    sanitize_project_name(os.path.splitext(os.path.basename(filename))[0]))
                target_project_dir = os.path.normpath(
                    os.path.join(BASE_DIR_ROOT, new_name)
                )
                target_dir_existed = os.path.isdir(target_project_dir)
                # Создаем новый проект
                new_project = ProjectFrame(self.app_reference.notebook, new_name, self.app_reference)
                self.app_reference.notebook.add(new_project, text=new_name)
                self.app_reference.projects[new_name] = new_project

                # Устанавливаем путь к txt файлу
                new_project.source_text_file.set(filename)
                new_project.project_name = new_name
                # Нормализуем путь для корректной работы с русскими символами
                new_project.project_dir = target_project_dir
                os.makedirs(new_project.project_dir, exist_ok=True)
                
                # Копируем текущие настройки в новый проект
                current_settings = self.get_current_settings()
                new_project.apply_settings(current_settings)
                new_project._initialize_references_from_project(
                    self, target_dir_existed=target_dir_existed
                )
                # Показываем в комбобоксе тот же пресет, что был выбран в исходном проекте
                try:
                    preset_name = self.preset_combo.get()
                    if preset_name and preset_name in new_project.preset_combo['values']:
                        new_project.preset_combo.set(preset_name)
                except Exception:
                    pass
                
                # НОВОЕ: Автоопределение языка для каждого нового проекта
                if new_project.auto_detect_language.get() and LANGDETECT_AVAILABLE and new_project.tts_engine.get() == 'edge':
                    try:
                        lang_code, voice, subtitle_lang = detect_language_from_file(filename)
                        new_project.selected_edge_voice.set(voice)
                        new_project.subtitle_language_var.set(subtitle_lang)
                        print(f"🔍 Проект '{new_name}': автоопределен язык {lang_code} → голос: {voice}, субтитры: {subtitle_lang}")
                    except Exception as e:
                        print(f"Ошибка автоопределения для '{new_name}': {e}")
    
    def log(self, message, kind="render"):
        # По умолчанию пишем в лог рендера, можно указать kind: "tts" | "images" | "render" | "thief"
        self.log_queue.put((kind, message))
    
    def on_stage_tab_changed(self, event=None):
        """Сохранить выбранную вкладку этапа в App"""
        try:
            tab_index = self.stages_notebook.index(self.stages_notebook.select())
            self.app_reference.save_current_stage_tab(tab_index)
            # Сбросить индикатор активности
            tab_id = self.stages_notebook.tabs()[tab_index]
            current_text = self.stages_notebook.tab(tab_id, "text")
            self.stages_notebook.tab(tab_id, text=current_text.replace(" 🔴", ""))
        except:
            pass
    
    def _drain_log_queue(self):
        """Вывести накопленные сообщения из log_queue в виджеты. Батчинг: одна перерисовка на виджет за цикл."""
        kind_to_widget = {
            "tts": getattr(self, "tts_log_text", None),
            "images": getattr(self, "images_log_text", None),
            "render": getattr(self, "render_log_text", None),
            "thief": getattr(self, "thief_log_text", None),
        }
        tab_map = {"tts": 1, "images": 2, "render": 3, "thief": 4}
        # Собираем все сообщения по видам лога
        batches = {}
        try:
            while True:
                kind, message = self.log_queue.get_nowait()
                if kind not in batches:
                    batches[kind] = []
                batches[kind].append(message + "\n")
                # Перехватываем строки прогресса (PROCESS-эмодзи ⏳) для статуса в заголовке окна
                if "⏳" in message:
                    app = getattr(self, "app_reference", None)
                    if app is not None:
                        app._last_progress_line = message
                        app._last_progress_ts = time.time()
        except queue.Empty:
            pass
        # Один раз вставляем и один раз обновляем экран на виджет — меньше перерисовок
        for kind, lines in batches.items():
            widget = kind_to_widget.get(kind)
            if not widget or not widget.winfo_exists() or not lines:
                continue
            try:
                widget.configure(state="normal")
                widget.insert(END, "".join(lines))
                try:
                    line_count = int(widget.index("end-1c").split(".")[0])
                    if line_count > 2000:
                        widget.delete("1.0", f"{line_count - 2000}.0")
                except Exception:
                    pass
                widget.configure(state="disabled")
                widget.see(END)
            except Exception:
                pass
            if kind in tab_map and hasattr(self, "stages_notebook"):
                try:
                    tab_idx = tab_map[kind]
                    current = self.stages_notebook.index(self.stages_notebook.select())
                    if current != tab_idx:
                        tab_id = self.stages_notebook.tabs()[tab_idx]
                        current_text = self.stages_notebook.tab(tab_id, "text")
                        if "🔴" not in current_text:
                            self.stages_notebook.tab(tab_id, text=current_text + " 🔴")
                except Exception:
                    pass

    def check_log_queue(self):
        """Оставлено для совместимости; опрос логов — в App._poll_all_log_queues."""
        self._drain_log_queue()

    def copy_log_to_clipboard(self, kind="render"):
        """Копирует содержимое лога в буфер обмена"""
        widget = {
            "tts": getattr(self, "tts_log_text", None),
            "images": getattr(self, "images_log_text", None),
            "render": getattr(self, "render_log_text", None),
            "thief": getattr(self, "thief_log_text", None)
        }.get(kind)
        
        if widget and widget.winfo_exists():
            try:
                # Включаем редактирование временно
                widget.configure(state="normal")
                # Получаем весь текст
                log_content = widget.get("1.0", "end-1c")
                # Возвращаем состояние disabled
                widget.configure(state="disabled")
                
                if log_content.strip():
                    # Копируем в буфер обмена
                    self.clipboard_clear()
                    self.clipboard_append(log_content)
                    self.log(f"✅ Лог скопирован в буфер обмена ({len(log_content)} символов)", kind=kind)
                else:
                    self.log("⚠️ Лог пуст", kind=kind)
            except Exception as e:
                self.log(f"Не удалось скопировать лог: {e}", kind=kind)
    
    def open_log_file(self, kind="render"):
        """Открывает полный debug.log системным приложением (Блокнот/TextEdit и т.п.)."""
        try:
            log_dir = os.path.join(
                os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                'VideoStudio'
            )
            log_path = os.path.join(log_dir, 'debug.log')
            if not os.path.exists(log_path):
                self.log("⚠️ Файл лога ещё не создан", kind=kind)
                return
            if sys.platform == 'win32':
                os.startfile(log_path)  # noqa: pylint
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', log_path])
            else:
                subprocess.Popen(['xdg-open', log_path])
            self.log(f"📂 Открыт лог: {log_path}", kind=kind)
        except Exception as e:
            self.log(f"Не удалось открыть лог: {e}", kind=kind)

    def _tts_worker_with_reenable(self, worker_func):
        """Обёртка для прямого запуска TTS — гарантирует re-enable кнопки."""
        try:
            worker_func()
        finally:
            try:
                self.root.after(0, lambda: self._set_action_buttons_state('tts', 'normal'))
            except Exception:
                pass

    def _refresh_voice_set_combo(self):
        """Обновляет значения выпадашки наборов голосов (+ сохраняет выбор)."""
        try:
            names = VoiceSets.get_set_names()
            values = ["— нет —"] + names
            self.voice_set_combo['values'] = values
            if self.selected_voice_set.get() not in values:
                self.selected_voice_set.set("— нет —")
        except Exception:
            pass

    def _resolve_voice_from_set(self, source_file):
        """Если выбран набор голосов — детектит язык скрипта (сырой langdetect, без edge-привязки)
        и возвращает voice_id из набора. None → набор не выбран / язык не найден (ручной voice_id)."""
        try:
            name = (self.selected_voice_set.get() or "").strip()
        except Exception:
            return None
        if not name or name == "— нет —":
            return None
        if not LANGDETECT_AVAILABLE:
            self.log("⚠️ Набор голосов выбран, но langdetect недоступен — использую ручной voice_id", kind="tts")
            return None
        try:
            text = ""
            try:
                with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except Exception:
                pass
            if not text.strip():
                self.log(f"⚠️ Набор «{name}»: пустой текст — ручной voice_id", kind="tts")
                return None
            sample = text[:500] if len(text) > 500 else text
            code = detect(sample)  # сырой код langdetect: 'ru','en','de','kk','zh-cn'...
            voice = VoiceSets.get_voice(name, code)
            if voice:
                self.log(f"🎙 Набор «{name}»: язык {code} → voice {voice[:10]}…", kind="tts")
                return voice
            self.log(f"⚠️ Набор «{name}»: для языка '{code}' нет голоса — использую ручной voice_id", kind="tts")
            return None
        except Exception as e:
            self.log(f"⚠️ Набор голосов: ошибка детекта языка ({str(e)[:80]}) — ручной voice_id", kind="tts")
            return None

    def _apply_voice_set_to_ui(self, source_file=None, log_miss=True):
        """ВИДИМО заполняет поля Voice ID (vgc + voicer — ID общие) голосом из набора
        по языку скрипта. Вызывается при выборе набора / выборе текста — обратная связь в UI."""
        try:
            name = (self.selected_voice_set.get() or "").strip()
        except Exception:
            return
        if not name or name == "— нет —":
            return
        if source_file is None:
            try:
                source_file = self.source_text_file.get()
            except Exception:
                source_file = ""
        if not source_file or not os.path.exists(source_file):
            if log_miss:
                self.log(f"🎙 Набор «{name}» выбран — голос подставится в поле по языку после выбора текста", kind="tts")
            return
        voice = self._resolve_voice_from_set(source_file)  # детект языка + лог
        if voice:
            if hasattr(self, 'vgc_voice_id'):
                self.vgc_voice_id.set(voice)
            if hasattr(self, 'voicer_voice_id'):
                self.voicer_voice_id.set(voice)

    def _open_voice_set_editor(self):
        """Редактор наборов голосов: имя набора + 36 строк (язык → voice_id) + сохр/удал/импорт/экспорт."""
        dialog = Toplevel(self.winfo_toplevel())
        dialog.title("Наборы голосов (ниша → язык → voice_id)")
        dialog.geometry("560x640")
        dialog.transient(self.winfo_toplevel())

        name_var = StringVar(value="")
        entry_vars = {}  # code -> StringVar

        # --- Верх: имя + загрузка существующего ---
        top = Frame(dialog)
        top.pack(fill="x", padx=12, pady=(12, 6))
        Label(top, text="Имя набора:", width=12, anchor="w").grid(row=0, column=0, sticky="w")
        Entry(top, textvariable=name_var, width=28).grid(row=0, column=1, sticky="w", padx=5)
        Label(top, text="Загрузить:", width=10, anchor="w").grid(row=1, column=0, sticky="w", pady=(5, 0))
        load_combo = ttk.Combobox(top, state="readonly", width=26)
        load_combo.grid(row=1, column=1, sticky="w", padx=5, pady=(5, 0))

        def _reload_names():
            load_combo['values'] = VoiceSets.get_set_names()

        def _load_into_editor(_evt=None):
            nm = load_combo.get().strip()
            if not nm:
                return
            name_var.set(nm)
            voices = VoiceSets.get_set(nm)
            for code, var in entry_vars.items():
                var.set(voices.get(code, ""))
        load_combo.bind("<<ComboboxSelected>>", _load_into_editor)
        _reload_names()

        # --- Прокручиваемый список 36 языков ---
        mid = Frame(dialog)
        mid.pack(fill="both", expand=True, padx=12, pady=6)
        canvas = Canvas(mid, highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
        inner = Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: self._schedule_scrollregion(canvas))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(inner_id, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def _on_wheel(e):
            try:
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        canvas.bind_all("<MouseWheel>", _on_wheel)

        for i, (code, label) in enumerate(VOICE_SET_LANGUAGES):
            var = StringVar(value="")
            entry_vars[code] = var
            Label(inner, text=f"{label} ({code})", width=24, anchor="w", font=("Arial", 9)).grid(row=i, column=0, sticky="w", padx=4, pady=1)
            Entry(inner, textvariable=var, width=40, font=("Arial", 9)).grid(row=i, column=1, sticky="ew", padx=4, pady=1)
        inner.columnconfigure(1, weight=1)

        # --- Низ: кнопки ---
        bottom = Frame(dialog)
        bottom.pack(fill="x", padx=12, pady=(6, 12))

        def _do_save():
            nm = name_var.get().strip()
            if not nm or nm == "— нет —":
                messagebox.showwarning("Наборы голосов", "Укажи имя набора.", parent=dialog)
                return
            voices = {code: var.get().strip() for code, var in entry_vars.items()}
            filled = sum(1 for v in voices.values() if v)
            if VoiceSets.save_set(nm, voices):
                self._refresh_voice_set_combo()
                self.selected_voice_set.set(nm)
                _reload_names()
                messagebox.showinfo("Наборы голосов", f"Набор «{nm}» сохранён ({filled} языков).", parent=dialog)
            else:
                messagebox.showerror("Наборы голосов", "Не удалось сохранить.", parent=dialog)

        def _do_delete():
            nm = name_var.get().strip()
            if not nm:
                return
            if messagebox.askyesno("Наборы голосов", f"Удалить набор «{nm}»?", parent=dialog):
                VoiceSets.delete_set(nm)
                self._refresh_voice_set_combo()
                _reload_names()
                for var in entry_vars.values():
                    var.set("")
                name_var.set("")
                messagebox.showinfo("Наборы голосов", "Удалён.", parent=dialog)

        def _do_export():
            path = filedialog.asksaveasfilename(parent=dialog, title="Экспорт наборов голосов",
                                                defaultextension=".json",
                                                initialfile="voice_sets.json",
                                                filetypes=[("JSON", "*.json")])
            if not path:
                return
            try:
                with open(path, 'w', encoding='utf-8', errors='replace') as f:
                    json.dump(VoiceSets.load(), f, ensure_ascii=False, indent=2)
                messagebox.showinfo("Наборы голосов", "Экспортировано (все наборы).", parent=dialog)
            except Exception as e:
                messagebox.showerror("Наборы голосов", f"Ошибка экспорта: {e}", parent=dialog)

        def _do_import():
            path = filedialog.askopenfilename(parent=dialog, title="Импорт наборов голосов",
                                              filetypes=[("JSON", "*.json")])
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    incoming = json.load(f)
                inc_sets = incoming.get("sets", {}) if isinstance(incoming, dict) else {}
                if not inc_sets:
                    messagebox.showwarning("Наборы голосов", "В файле нет наборов.", parent=dialog)
                    return
                data = VoiceSets.load()
                data.setdefault("sets", {}).update(inc_sets)  # merge (импорт перезаписывает одноимённые)
                VoiceSets.save(data)
                self._refresh_voice_set_combo()
                _reload_names()
                messagebox.showinfo("Наборы голосов", f"Импортировано наборов: {len(inc_sets)}.", parent=dialog)
            except Exception as e:
                messagebox.showerror("Наборы голосов", f"Ошибка импорта: {e}", parent=dialog)

        def _on_close():
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            dialog.destroy()

        Button(bottom, text="💾 Сохранить", command=_do_save, bg="#4CAF50", fg="white", width=13).pack(side="left", padx=3)
        Button(bottom, text="🗑 Удалить", command=_do_delete, width=11).pack(side="left", padx=3)
        Button(bottom, text="⬇ Импорт", command=_do_import, width=10).pack(side="left", padx=3)
        Button(bottom, text="⬆ Экспорт", command=_do_export, width=10).pack(side="left", padx=3)
        Button(bottom, text="Закрыть", command=_on_close, width=10).pack(side="right", padx=3)
        dialog.protocol("WM_DELETE_WINDOW", _on_close)

    def run_tts(self):
        self._set_action_buttons_state('tts', 'disabled')
        engine = self.tts_engine.get()
        
        if engine == "elevenlabs":
            if self.elevenlabs_queue:
                success, message = self.elevenlabs_queue.add_to_queue(self.project_name, self._run_tts_worker)
                self.log(f"\n[Очередь ElevenLabs] {message}", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        elif engine == "voicer":
            if self.voicer_queue:
                success, message = self.voicer_queue.add_to_queue(self.project_name, self._run_tts_worker)
                self.log(f"\n[Очередь Voicer] {message}", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        elif engine == "voicegencore":
            if self.voicegencore_queue:
                success, message = self.voicegencore_queue.add_to_queue(self.project_name, self._run_tts_worker)
                self.log(f"\n[Очередь VoiceGenCore] {message}", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        else:
            if self.edgetts_queue:
                success, message = self.edgetts_queue.add_to_queue(self.project_name, self._run_tts_worker)
                self.log(f"\n[Очередь EdgeTTS] {message}", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        threading.Thread(target=lambda: self._tts_worker_with_reenable(self._run_tts_worker), daemon=True).start()
    
    def run_tts_full_text(self):
        """Запуск озвучки всего текста целиком (без разбивки на абзацы)"""
        self._set_action_buttons_state('tts', 'disabled')
        engine = self.tts_engine.get()
        
        if engine == "elevenlabs":
            if self.elevenlabs_queue:
                success, message = self.elevenlabs_queue.add_to_queue(self.project_name, self._run_tts_worker_full_text)
                self.log(f"\n[Очередь ElevenLabs] {message} (весь текст)", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        elif engine == "voicer":
            if self.voicer_queue:
                success, message = self.voicer_queue.add_to_queue(self.project_name, self._run_tts_worker_full_text)
                self.log(f"\n[Очередь Voicer] {message} (весь текст)", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        elif engine == "voicegencore":
            if self.voicegencore_queue:
                success, message = self.voicegencore_queue.add_to_queue(self.project_name, self._run_tts_worker_full_text)
                self.log(f"\n[Очередь VoiceGenCore] {message} (весь текст)", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        else:
            if self.edgetts_queue:
                success, message = self.edgetts_queue.add_to_queue(self.project_name, self._run_tts_worker_full_text)
                self.log(f"\n[Очередь EdgeTTS] {message} (весь текст)", kind="tts")
                if not success:
                    self._set_action_buttons_state('tts', 'normal')
                    try:
                        self.log(message, kind="tts")
                    except:
                        pass
                return
        threading.Thread(target=lambda: self._tts_worker_with_reenable(self._run_tts_worker_full_text), daemon=True).start()
    
    def _run_tts_worker(self):
        try:
            source_file = self.source_text_file.get()
            if not source_file or not os.path.exists(source_file):
                self.log("❌ Выберите текстовый файл!")
                return
            
            self._auto_save_voice_mapping()
            
            engine = self.tts_engine.get()
            self.log(f"\n🚀 Озвучка ({engine})...")
            try:
                os.environ["TTS_SPEED"] = (self.tts_speed.get().strip() if hasattr(self, 'tts_speed') else "1.0") or "1.0"
            except Exception:
                os.environ["TTS_SPEED"] = "1.0"
            
            if engine == "elevenlabs":
                worker_script = os.path.join(SCRIPTS_DIR, "tts_worker.py")
                if not check_worker_exists("tts_worker.py"):
                    self.log(f"Не найден скрипт: tts_worker.py (или tts_worker.exe)", kind="tts")
                    return
                
                template_name = self.selected_voice_template.get()
                if not template_name:
                    self.log("❌ Выберите шаблон!")
                    return
                
                if not os.path.exists(TEMPLATE_CONFIG_PATH):
                    self.log(f"❌ Файл шаблонов не найден: {TEMPLATE_CONFIG_PATH}", kind="tts")
                    return
                
                try:
                    with open(TEMPLATE_CONFIG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                        config = json.load(f)
                        template_data = config.get('elevenlabs', {}).get(template_name, {})
                        voice_uuid = template_data.get('voice_uuid')
                        model_uuid = template_data.get('model_uuid')
                except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
                    self.log(f"❌ Ошибка чтения шаблонов: {e}", kind="tts")
                    return
                
                worker_args = ["--source_file", source_file, "--project_dir", self.project_dir, "--voice_uuid", voice_uuid if voice_uuid else "", "--model_uuid", model_uuid if model_uuid else ""]
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                self._tts_stop_event.clear()
                used, code = run_worker_inprocess("tts_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                if used:
                    self.log("✅ Озвучка завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="tts")
                    return
                command = [str(arg) for arg in command if arg is not None]
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                if process.stdout:
                    for line in process.stdout:
                        line = line.rstrip()
                        self.log(line, kind="tts")
                        if "422 Client Error" in line:
                            self.log("⚠️ Ошибка 422: проверьте Voice UUID/Model UUID и длину текста абзаца.", kind="tts")
                process.wait()
                self.tts_proc_handle = None
                self.log("✅ Озвучка завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
            elif engine == "voicer":
                worker_script = os.path.join(SCRIPTS_DIR, "voicer_worker.py")
                if not check_worker_exists("voicer_worker.py"):
                    self.log(f"Не найден скрипт: voicer_worker.py (или voicer_worker.exe)", kind="tts")
                    return
                voice_id = self.voicer_voice_id.get().strip() or "AB9XsbSA4eLG12t2myjN"
                _vs = self._resolve_voice_from_set(source_file)
                if _vs:
                    voice_id = _vs
                model_id = self.voicer_model_id.get().strip() or "eleven_multilingual_v2"
                split_type = self.voicer_split_type.get().strip() or "smart"
                worker_args = ["--source_file", source_file, "--project_dir", self.project_dir,
                              "--voice_id", voice_id, "--model_id", model_id, "--split_type", split_type]
                if self.voicer_auto_pause_enabled.get():
                    worker_args.append("--auto_pause_enabled")
                    worker_args.extend(["--auto_pause_duration", self.voicer_auto_pause_duration.get() or "0.5"])
                    worker_args.extend(["--auto_pause_frequency", self.voicer_auto_pause_frequency.get() or "1"])
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                self._tts_stop_event.clear()
                used, code = run_worker_inprocess("voicer_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                if used:
                    self.log("✅ Озвучка Voicer завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}!", kind="tts")
                    return
                command = [str(arg) for arg in command if arg is not None]
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                if process.stdout:
                    for line in process.stdout:
                        self.log(line.rstrip(), kind="tts")
                process.wait()
                self.tts_proc_handle = None
                self.log("✅ Озвучка Voicer завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
            elif engine == "voicegencore":
                worker_script = os.path.join(SCRIPTS_DIR, "voicegencore_worker.py")
                if not check_worker_exists("voicegencore_worker.py"):
                    self.log(f"Не найден скрипт: voicegencore_worker.py (или voicegencore_worker.exe)", kind="tts")
                    return
                voice_id = self.vgc_voice_id.get().strip()
                _vs = self._resolve_voice_from_set(source_file)
                if _vs:
                    voice_id = _vs
                if not voice_id:
                    self.log("❌ Укажите Voice ID для VoiceGenCore!", kind="tts")
                    return
                voice_engine = self.vgc_voice_engine.get().strip() or "elevenLabsV3"
                stability = self.vgc_stability.get().strip() or "0.5"
                thread_count = self.vgc_thread_count.get().strip() or "10"
                worker_args = ["--source_file", source_file, "--project_dir", self.project_dir,
                              "--voice_id", voice_id, "--voice_engine", voice_engine,
                              "--stability", stability, "--thread_count", thread_count]
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                self._tts_stop_event.clear()
                used, code = run_worker_inprocess("voicegencore_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                if used:
                    self.log("✅ Озвучка VoiceGenCore завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}!", kind="tts")
                    return
                command = [str(arg) for arg in command if arg is not None]
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                if process.stdout:
                    for line in process.stdout:
                        self.log(line.rstrip(), kind="tts")
                process.wait()
                self.tts_proc_handle = None
                self.log("✅ Озвучка VoiceGenCore завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
            else:
                worker_script = os.path.join(SCRIPTS_DIR, "edgetts_worker.py")
                if not check_worker_exists("edgetts_worker.py"):
                    self.log(f"Не найден скрипт: edgetts_worker.py (или edgetts_worker.exe)", kind="tts")
                    return
                
                # НОВОЕ: Автоопределение языка
                if self.auto_detect_language.get() and LANGDETECT_AVAILABLE:
                    # Читаем текст и определяем язык
                    with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
                        text_content = f.read()
                    
                    voice_name = detect_language_and_get_voice(text_content)
                    # Устанавливаем выбранный голос в комбобокс, чтобы пользователь видел
                    self.selected_edge_voice.set(voice_name)
                    self.log(f"🔍 Автоопределен язык, голос: {voice_name}", kind="tts")
                else:
                    voice_name = self.selected_edge_voice.get()
                
                with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
                    paragraphs = [{'text': p.strip()} for p in f.read().splitlines() if p.strip()]
                
                # ИСПРАВЛЕНИЕ: Сохраняем параграфы во временный файл вместо передачи через аргументы
                # (для больших текстов аргументы слишком длинные - ошибка "Argument list too long")
                temp_json_file = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', delete=False)
                temp_file_path = None
                try:
                    json.dump(paragraphs, temp_json_file, ensure_ascii=False, indent=2)
                    temp_json_file.close()
                    temp_file_path = temp_json_file.name
                    
                    worker_args = ["--project_dir", self.project_dir, "--voice_name", voice_name, "--paragraphs_file", temp_file_path]
                    if self.use_cache.get():
                        worker_args.append("--use_cache")
                    # Фаза 2: серверный режим — гоним воркер на сервере, файлы кладём в project_dir (1-в-1). Иначе локально.
                    if server_client is not None and server_client.is_enabled():
                        srv_args = ["--project_dir", "__OUT__", "--voice_name", voice_name, "--paragraphs_file", "paragraphs.json"]
                        if self.use_cache.get():
                            srv_args.append("--use_cache")
                        self.log("☁️ Озвучка на сервере (edge)...", kind="tts")
                        ok, info = server_client.run_worker(
                            "edgetts_worker.py", srv_args,
                            {"paragraphs.json": json.dumps(paragraphs, ensure_ascii=False)}, self.project_dir)
                        if ok:
                            if temp_file_path and os.path.exists(temp_file_path):
                                try:
                                    os.unlink(temp_file_path)
                                except Exception:
                                    pass
                            self.log(f"✅ Озвучка (сервер) завершена! [{info}]", kind="tts")
                            return
                        self.log(f"⚠️ Сервер не сработал ({info}) — локальная генерация...", kind="tts")
                    self._tts_stop_event.clear()
                    used, code = run_worker_inprocess("edgetts_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                    if used:
                        if temp_file_path and os.path.exists(temp_file_path):
                            try:
                                os.unlink(temp_file_path)
                            except Exception:
                                pass
                        self.log("✅ Озвучка завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                        return
                    command = get_worker_command(worker_script, worker_args)
                    if command is None or len(command) == 0:
                        self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_FINAL", kind="tts")
                        if temp_file_path and os.path.exists(temp_file_path):
                            os.unlink(temp_file_path)
                        return
                    command = [str(arg) for arg in command if arg is not None]
                except Exception as e:
                    temp_json_file.close()
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
                    raise e
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                if process.stdout:
                    for line in process.stdout:
                        line = line.rstrip()
                        self.log(line, kind="tts")
                        if "422 Client Error" in line:
                            self.log("⚠️ Ошибка 422: проверьте Voice UUID/Model UUID и длину текста абзаца.", kind="tts")
                process.wait()
                self.tts_proc_handle = None
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.unlink(temp_file_path)
                    except Exception:
                        pass
                self.log("✅ Озвучка завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
        except Exception as e:
            self.log(f"❌ Ошибка: {e}", kind="tts")
    
    def _run_tts_worker_full_text(self):
        """Озвучка всего текста целиком без разбивки на абзацы"""
        try:
            source_file = self.source_text_file.get()
            if not source_file or not os.path.exists(source_file):
                self.log("❌ Выберите текстовый файл!", kind="tts")
                return
            
            engine = self.tts_engine.get()
            self.log(f"\n🚀 Озвучка всего текста целиком ({engine})...", kind="tts")
            try:
                os.environ["TTS_SPEED"] = (self.tts_speed.get().strip() if hasattr(self, 'tts_speed') else "1.0") or "1.0"
            except Exception:
                os.environ["TTS_SPEED"] = "1.0"
            
            if engine == "elevenlabs":
                # Для ElevenLabs читаем весь текст и передаем как один абзац
                worker_script = os.path.join(SCRIPTS_DIR, "tts_worker.py")
                if not check_worker_exists("tts_worker.py"):
                    self.log(f"Не найден скрипт: tts_worker.py (или tts_worker.exe)", kind="tts")
                    return
                
                template_name = self.selected_voice_template.get()
                if not template_name:
                    self.log("❌ Выберите шаблон!", kind="tts")
                    return
                
                if not os.path.exists(TEMPLATE_CONFIG_PATH):
                    self.log(f"❌ Файл шаблонов не найден: {TEMPLATE_CONFIG_PATH}", kind="tts")
                    return
                
                try:
                    with open(TEMPLATE_CONFIG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                        config = json.load(f)
                        template_data = config.get('elevenlabs', {}).get(template_name, {})
                        voice_uuid = template_data.get('voice_uuid')
                        model_uuid = template_data.get('model_uuid')
                except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
                    self.log(f"❌ Ошибка чтения шаблонов: {e}", kind="tts")
                    return
                
                # Читаем весь текст из файла
                with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
                    full_text = f.read().strip()
                
                # Заменяем все переносы строк на пробелы, чтобы весь текст был в одной строке
                # Это гарантирует, что tts_worker.py обработает его как один абзац
                full_text_single_line = ' '.join(full_text.split())
                
                # Создаем временный файл с одним большим абзацем (одна строка)
                temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8', errors='replace')
                temp_file.write(full_text_single_line)
                temp_file.close()
                
                worker_args = ["--source_file", temp_file.name, "--project_dir", self.project_dir, "--voice_uuid", voice_uuid if voice_uuid else "", "--model_uuid", model_uuid if model_uuid else ""]
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                self._tts_stop_event.clear()
                used, code = run_worker_inprocess("tts_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                if used:
                    try:
                        if temp_file and os.path.exists(temp_file.name):
                            os.unlink(temp_file.name)
                    except Exception:
                        pass
                    self.log("✅ Озвучка завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="tts")
                    return
                command = [str(arg) for arg in command if arg is not None]
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                try:
                    if process.stdout:
                        for line in process.stdout:
                            line = line.rstrip()
                            self.log(line, kind="tts")
                            if "422 Client Error" in line:
                                self.log("⚠️ Ошибка 422: проверьте Voice UUID/Model UUID и длину текста.", kind="tts")
                    process.wait()
                finally:
                    self.tts_proc_handle = None
                    try:
                        if 'temp_file' in locals() and temp_file and os.path.exists(temp_file.name):
                            os.unlink(temp_file.name)
                    except Exception:
                        pass
                self.log("✅ Озвучка завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
            elif engine == "voicer":
                worker_script = os.path.join(SCRIPTS_DIR, "voicer_worker.py")
                if not check_worker_exists("voicer_worker.py"):
                    self.log(f"Не найден скрипт: voicer_worker.py (или voicer_worker.exe)", kind="tts")
                    return
                with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
                    full_text = ' '.join(f.read().strip().split())
                temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8', errors='replace')
                temp_file.write(full_text)
                temp_file.close()
                voice_id = self.voicer_voice_id.get().strip() or "AB9XsbSA4eLG12t2myjN"
                _vs = self._resolve_voice_from_set(source_file)
                if _vs:
                    voice_id = _vs
                model_id = self.voicer_model_id.get().strip() or "eleven_multilingual_v2"
                split_type = self.voicer_split_type.get().strip() or "smart"
                worker_args = ["--source_file", temp_file.name, "--project_dir", self.project_dir,
                              "--voice_id", voice_id, "--model_id", model_id, "--split_type", split_type]
                if self.voicer_auto_pause_enabled.get():
                    worker_args.append("--auto_pause_enabled")
                    worker_args.extend(["--auto_pause_duration", self.voicer_auto_pause_duration.get() or "0.5"])
                    worker_args.extend(["--auto_pause_frequency", self.voicer_auto_pause_frequency.get() or "1"])
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                self._tts_stop_event.clear()
                used, code = run_worker_inprocess("voicer_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                if used:
                    try:
                        if temp_file and os.path.exists(temp_file.name):
                            os.unlink(temp_file.name)
                    except Exception:
                        pass
                    self.log("✅ Озвучка Voicer завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    try:
                        os.unlink(temp_file.name)
                    except Exception:
                        pass
                    self.log(f"❌ Ошибка: не найден {worker_script}!", kind="tts")
                    return
                command = [str(arg) for arg in command if arg is not None]
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                try:
                    if process.stdout:
                        for line in process.stdout:
                            self.log(line.rstrip(), kind="tts")
                    process.wait()
                finally:
                    self.tts_proc_handle = None
                    try:
                        if temp_file and os.path.exists(temp_file.name):
                            os.unlink(temp_file.name)
                    except Exception:
                        pass
                self.log("✅ Озвучка Voicer завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
            elif engine == "voicegencore":
                worker_script = os.path.join(SCRIPTS_DIR, "voicegencore_worker.py")
                if not check_worker_exists("voicegencore_worker.py"):
                    self.log(f"Не найден скрипт: voicegencore_worker.py (или voicegencore_worker.exe)", kind="tts")
                    return
                voice_id = self.vgc_voice_id.get().strip()
                _vs = self._resolve_voice_from_set(source_file)
                if _vs:
                    voice_id = _vs
                if not voice_id:
                    self.log("❌ Укажите Voice ID для VoiceGenCore!", kind="tts")
                    return
                with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
                    full_text = ' '.join(f.read().strip().split())
                temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8', errors='replace')
                temp_file.write(full_text)
                temp_file.close()
                voice_engine = self.vgc_voice_engine.get().strip() or "elevenLabsV3"
                stability = self.vgc_stability.get().strip() or "0.5"
                thread_count = self.vgc_thread_count.get().strip() or "10"
                worker_args = ["--source_file", temp_file.name, "--project_dir", self.project_dir,
                              "--voice_id", voice_id, "--voice_engine", voice_engine,
                              "--stability", stability, "--thread_count", thread_count]
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                self._tts_stop_event.clear()
                used, code = run_worker_inprocess("voicegencore_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                if used:
                    try:
                        if temp_file and os.path.exists(temp_file.name):
                            os.unlink(temp_file.name)
                    except Exception:
                        pass
                    self.log("✅ Озвучка VoiceGenCore завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    try:
                        os.unlink(temp_file.name)
                    except Exception:
                        pass
                    self.log(f"❌ Ошибка: не найден {worker_script}!", kind="tts")
                    return
                command = [str(arg) for arg in command if arg is not None]
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                try:
                    if process.stdout:
                        for line in process.stdout:
                            self.log(line.rstrip(), kind="tts")
                    process.wait()
                finally:
                    self.tts_proc_handle = None
                    try:
                        if temp_file and os.path.exists(temp_file.name):
                            os.unlink(temp_file.name)
                    except Exception:
                        pass
                self.log("✅ Озвучка VoiceGenCore завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
            else:
                # Для Edge TTS передаем весь текст как один большой абзац
                worker_script = os.path.join(SCRIPTS_DIR, "edgetts_worker.py")
                if not check_worker_exists("edgetts_worker.py"):
                    self.log(f"Не найден скрипт: edgetts_worker.py (или edgetts_worker.exe)", kind="tts")
                    return

                # Читаем весь текст из файла
                with open(source_file, 'r', encoding='utf-8', errors='replace') as f:
                    full_text = f.read().strip()
                
                # НОВОЕ: Автоопределение языка
                if self.auto_detect_language.get() and LANGDETECT_AVAILABLE:
                    voice_name = detect_language_and_get_voice(full_text)
                    # Устанавливаем выбранный голос в комбобокс, чтобы пользователь видел
                    self.selected_edge_voice.set(voice_name)
                    self.log(f"🔍 Автоопределен язык, голос: {voice_name}", kind="tts")
                else:
                    voice_name = self.selected_edge_voice.get()
                
                # Передаем весь текст как один абзац
                paragraphs = [{'text': full_text}]
                
                # ИСПРАВЛЕНИЕ: Сохраняем во временный файл
                temp_json_file = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', delete=False)
                temp_file_path = None
                try:
                    json.dump(paragraphs, temp_json_file, ensure_ascii=False, indent=2)
                    temp_json_file.close()
                    temp_file_path = temp_json_file.name
                    
                    worker_args = ["--project_dir", self.project_dir, "--voice_name", voice_name, "--paragraphs_file", temp_file_path]
                    if self.use_cache.get():
                        worker_args.append("--use_cache")
                    # Фаза 2: серверный режим — гоним воркер на сервере, файлы кладём в project_dir (1-в-1). Иначе локально.
                    if server_client is not None and server_client.is_enabled():
                        srv_args = ["--project_dir", "__OUT__", "--voice_name", voice_name, "--paragraphs_file", "paragraphs.json"]
                        if self.use_cache.get():
                            srv_args.append("--use_cache")
                        self.log("☁️ Озвучка на сервере (edge)...", kind="tts")
                        ok, info = server_client.run_worker(
                            "edgetts_worker.py", srv_args,
                            {"paragraphs.json": json.dumps(paragraphs, ensure_ascii=False)}, self.project_dir)
                        if ok:
                            if temp_file_path and os.path.exists(temp_file_path):
                                try:
                                    os.unlink(temp_file_path)
                                except Exception:
                                    pass
                            self.log(f"✅ Озвучка (сервер) завершена! [{info}]", kind="tts")
                            return
                        self.log(f"⚠️ Сервер не сработал ({info}) — локальная генерация...", kind="tts")
                    self._tts_stop_event.clear()
                    used, code = run_worker_inprocess("edgetts_worker.py", worker_args, self.log, kind="tts", stop_event=self._tts_stop_event)
                    if used:
                        if temp_file_path and os.path.exists(temp_file_path):
                            try:
                                os.unlink(temp_file_path)
                            except Exception:
                                pass
                        self.log("✅ Озвучка завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="tts")
                        return
                    command = get_worker_command(worker_script, worker_args)
                    if command is None or len(command) == 0:
                        self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_FINAL", kind="tts")
                        if temp_file_path and os.path.exists(temp_file_path):
                            os.unlink(temp_file_path)
                        return
                    command = [str(arg) for arg in command if arg is not None]
                except Exception as e:
                    temp_json_file.close()
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
                    raise e
                env = os.environ.copy()
                try:
                    from logger_config import is_debug_mode
                    if is_debug_mode():
                        env['VIDEO_STUDIO_DEBUG'] = '1'
                except ImportError:
                    pass
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
                self.tts_proc_handle = process
                if process.stdout:
                    for line in process.stdout:
                        self.log(line.rstrip(), kind="tts")
                process.wait()
                self.tts_proc_handle = None
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.unlink(temp_file_path)
                    except Exception:
                        pass
                self.log("✅ Озвучка завершена!" if process.returncode == 0 else f"❌ Ошибка (код {process.returncode})", kind="tts")
        except Exception as e:
            self.log(f"❌ Ошибка: {e}", kind="tts")
    
    def run_image_download(self):
        threading.Thread(target=self._run_image_download_worker, daemon=True).start()
    
    def _run_image_download_worker(self):
        try:
            self._images_download_busy = True
            # The shared gate validates damaged state first, then rejects
            # self.user_references or self.style_references,
            # self.auto_hero_var.get(), and self.analyze_story_var.get().
            if self._reference_preflight("bing") is None:
                return
            tags = self.image_search_query.get()
            if not tags:
                self.log("❌ Введите теги!")
                return
            
            try:
                num_images = int(self.num_images_per_tag.get())
                if num_images <= 0:
                    raise ValueError
            except:
                self.log("❌ Неверное количество!")
                return
            
            self.log("\n🖼️ Скачивание изображений...", kind="images")
            worker_script = os.path.join(SCRIPTS_DIR, "image_worker.py")
            worker_args = ["--tags", tags, "--num_images", str(num_images), "--project_dir", self.project_dir]
            if self.use_cache.get():
                worker_args.append("--use_cache")
            used, code = run_worker_inprocess("image_worker.py", worker_args, self.log, kind="images")
            if used:
                self.log("✅ Загрузка изображений завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="images")
                return
            command = get_worker_command(worker_script, worker_args)
            
            # Проверяем, что команда валидна
            if command is None or len(command) == 0:
                self.log("❌ Ошибка: не найден image_worker.exe! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                return
            
            # Проверяем, что команда не запускает сам VideoStudio.exe
            if len(command) > 0:
                cmd_exe = os.path.basename(command[0]).lower()
                if cmd_exe == 'videostudio.exe':
                    self.log("❌ Ошибка: не найден image_worker.exe! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                    return
                # Проверяем, что файл существует
                if not os.path.exists(command[0]):
                    self.log(f"❌ Ошибка: файл {command[0]} не найден!", kind="images")
                    return
            
            # Фильтруем None и преобразуем все в строки
            command = [str(arg) for arg in command if arg is not None]
            
            # Передаем переменную окружения DEBUG если включен режим отладки
            env = os.environ.copy()
            try:
                from logger_config import is_debug_mode
                if is_debug_mode():
                    env['VIDEO_STUDIO_DEBUG'] = '1'
            except ImportError:
                pass
            
            startupinfo = get_subprocess_flags()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
            self.bing_proc_handle = process
            if process.stdout:
                for line in process.stdout:
                    self.log(line.rstrip(), kind="images")
            process.wait()
            self.bing_proc_handle = None
            
            if process.returncode == 0:
                self.log("✅ Изображения скачаны!", kind="images")
            else:
                self.log(f"❌ Ошибка (код {process.returncode})", kind="images")
        except Exception as e:
            self.log(f"❌ Ошибка: {e}", kind="images")
        finally:
            self._images_download_busy = False
    
    # --- GOOGLER МЕТОДЫ ---
    
    def on_image_source_change(self):
        """Переключение между Bing и Googler"""
        source = self.image_source.get()
        if source == "bing":
            self.bing_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
            self.googler_frame.grid_remove()
            self.googler_video_frame.grid_remove()
            if self.user_references or self.style_references or self.auto_hero_var.get():
                self.log(
                    "⚠️ Bing выбран, но визуальные референсы/Авто-герой активны: запуск будет заблокирован, сами файлы сохранены.",
                    kind="images",
                )
        else:
            self.bing_frame.grid_remove()
            self.googler_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=2)
            self.googler_video_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=1)
    
    def select_images_for_video(self):
        """Выбор изображений для генерации видео"""
        # Начальная директория - папка googler или Images проекта
        initial_dir = os.path.join(self.project_dir, "googler")
        if not os.path.exists(initial_dir):
            initial_dir = os.path.join(self.project_dir, "Images")
        if not os.path.exists(initial_dir):
            initial_dir = self.project_dir
        # Если ничего не найдено - используем projects (где фрилансер может загрузить файлы)
        if not os.path.exists(initial_dir):
            initial_dir = BASE_DIR_ROOT if os.path.exists(BASE_DIR_ROOT) else os.path.expanduser("~")
        
        filenames = filedialog.askopenfilenames(
            title="Выберите изображения для генерации видео",
            initialdir=initial_dir,
            filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.webp"),
                ("All", "*.*")
            ]
        )
        
        if filenames:
            self.googler_selected_images = list(filenames)
            count = len(self.googler_selected_images)
            self.selected_images_label.config(text=f"Выбрано: {count} изображений", fg="#333")
            self.log(f"📷 Выбрано {count} изображений для генерации видео", kind="images")
    
    def clear_selected_images(self):
        """Очистка списка выбранных изображений"""
        self.googler_selected_images = []
        self.selected_images_label.config(text="Не выбрано", fg="#666")

    def _list_generated_images(self):
        """Список сгенерированных/скачанных картинок проекта: googler/ + Images/ (отсортировано)."""
        result = []
        for sub in ("googler", IMAGE_DIR_NAME):
            d = os.path.join(self.project_dir, sub)
            if not os.path.isdir(d):
                continue
            try:
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        result.append(os.path.join(d, f))
            except Exception as e:
                self.log(f"⚠️ Ошибка чтения папки {sub}: {e}", kind="images")
        return result

    def _delete_image_files(self, paths):
        """Удаляет файлы картинок с диска, чистит их из выбора для видео. Возвращает список удалённых."""
        deleted = []
        for p in paths:
            try:
                os.remove(p)
                deleted.append(p)
            except Exception as e:
                self.log(f"⚠️ Не удалилось {os.path.basename(p)}: {e}", kind="images")
        if deleted:
            deleted_set = set(os.path.normpath(p) for p in deleted)
            if self.googler_selected_images:
                self.googler_selected_images = [
                    p for p in self.googler_selected_images
                    if os.path.normpath(p) not in deleted_set
                ]
                count = len(self.googler_selected_images)
                try:
                    self.selected_images_label.config(
                        text=(f"Выбрано: {count} изображений" if count else "Не выбрано"),
                        fg=("#333" if count else "#666"))
                except Exception:
                    pass
            self.log(f"🗑 Удалено картинок: {len(deleted)}", kind="images")
            if any(os.path.basename(os.path.dirname(p)) == "googler" for p in deleted):
                self.log("💡 Повторная «Генерация картинок» с кэшем перегенерирует только удалённые номера", kind="images")
        return deleted

    def _get_image_prompt(self, path):
        """Исходный промпт картинки NNN.jpg из googler/scenes.json или prompts.json (для префилла)."""
        try:
            stem = os.path.splitext(os.path.basename(path))[0]
            if not stem.isdigit():
                return ""
            idx = int(stem) - 1
            googler_dir = os.path.join(self.project_dir, "googler")
            scenes_file = os.path.join(googler_dir, "scenes.json")
            if os.path.exists(scenes_file):
                with open(scenes_file, 'r', encoding='utf-8', errors='replace') as f:
                    scenes = json.load(f)
                if 0 <= idx < len(scenes) and isinstance(scenes[idx], dict):
                    p = scenes[idx].get('visual_prompt') or scenes[idx].get('visual_description')
                    if p:
                        return p
            prompts_file = os.path.join(googler_dir, "prompts.json")
            if os.path.exists(prompts_file):
                with open(prompts_file, 'r', encoding='utf-8', errors='replace') as f:
                    pdata = json.load(f)
                plist = pdata.get("prompts", [])
                if 0 <= idx < len(plist):
                    return plist[idx].get("prompt") or ""
        except Exception:
            pass
        return ""

    def _run_regen_image_worker(self, image_path, custom_prompt, done_cb=None):
        """Перегенерация одной картинки (блокирующе, звать из потока). done_cb(success) — в конце."""
        success = False
        try:
            reference_preflight = self._reference_preflight("regen")
            if reference_preflight is None:
                return
            reference_manifest = reference_preflight["reference_manifest"]
            worker_args = ["regen_image",
                           "--project_dir", self.project_dir,
                           "--image_file", image_path,
                           "--reference_manifest", reference_manifest]
            if custom_prompt:
                worker_args.extend(["--prompt", custom_prompt])
            style = self.googler_style.get().strip()
            if style:
                worker_args.extend(["--style", style])
            worker_args.extend(["--aspect_ratio", self.googler_aspect_ratio.get()])
            model = self.googler_model.get()
            if model:
                worker_args.extend(["--model", model])
            seed_raw = (self.googler_seed.get() or "").strip() if hasattr(self, 'googler_seed') else ""
            if seed_raw:
                try:
                    worker_args.extend(["--seed", str(int(seed_raw))])
                except ValueError:
                    pass
            if self.auto_hero_var.get():
                worker_args.append("--auto_hero")
            if getattr(self, 'analyze_story_var', None) and self.analyze_story_var.get():
                worker_args.append("--analyze_story")
            else:
                worker_args.append("--no_analyze_story")

            used, code = run_worker_inprocess("googler_worker.py", worker_args, self.log, kind="images")
            if used:
                success = (code == 0)
            else:
                command = get_worker_command("googler_worker.py", worker_args)
                if not command:
                    self.log("❌ Ошибка: не найден googler_worker!", kind="images")
                else:
                    self.app_reference.api_keys_manager.update_env_vars()
                    env = os.environ.copy()
                    if 'googler' in self.app_reference.api_keys_manager.keys:
                        env['GOOGLER_API_KEY'] = self.app_reference.api_keys_manager.keys['googler']
                    if 'openai' in self.app_reference.api_keys_manager.keys:
                        env['OPENAI_API_KEY'] = self.app_reference.api_keys_manager.keys['openai']
                    startupinfo = get_subprocess_flags()
                    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, encoding='utf-8', errors='replace', bufsize=1,
                                               env=env, startupinfo=startupinfo,
                                               creationflags=get_subprocess_creationflags())
                    if process.stdout:
                        for line in process.stdout:
                            self.log(line.rstrip(), kind="images")
                    process.wait()
                    success = (process.returncode == 0)
        except Exception as e:
            self.log(f"❌ Перегенерация: {e}", kind="images")
        finally:
            if done_cb:
                try:
                    done_cb(success)
                except Exception:
                    pass

    def open_images_gallery(self):
        """Галерея картинок проекта: миниатюры + удаление поштучно, выбранных или всех сразу."""
        if not self._list_generated_images():
            self.log("🖼 Галерея: картинок в проекте нет (папки googler/Images пусты)", kind="images")
            return
        try:
            from PIL import Image, ImageTk
            pil_ok = True
        except ImportError:
            pil_ok = False

        dialog = Toplevel(self.winfo_toplevel())
        dialog.title(f"🖼 Картинки проекта — {self.project_name}")
        dialog.geometry("880x640")
        dialog.transient(self.winfo_toplevel())

        top = Frame(dialog, padx=10, pady=6)
        top.pack(fill="x")
        info_label = Label(top, text="", font=("Arial", 9))
        info_label.pack(side="left")

        grid_container = Frame(dialog)
        grid_container.pack(fill="both", expand=True, padx=10)
        gal_canvas = Canvas(grid_container, highlightthickness=0)
        gal_scroll = ttk.Scrollbar(grid_container, orient="vertical", command=gal_canvas.yview)
        grid_frame = Frame(gal_canvas)
        gal_canvas.create_window((0, 0), window=grid_frame, anchor="nw")
        gal_canvas.configure(yscrollcommand=gal_scroll.set)
        gal_canvas.pack(side="left", fill="both", expand=True)
        gal_scroll.pack(side="right", fill="y")
        # Дебаунс через _schedule_scrollregion — прямой configure в <Configure> даёт каскад/зависание Tcl
        grid_frame.bind("<Configure>", lambda e: self._schedule_scrollregion(gal_canvas))

        COLS = 5
        THUMB = 150
        state = {'selected': set(), 'tiles': {}, 'thumb_cache': {}, 'load_job': None}

        btn_row = Frame(dialog, padx=10, pady=6)
        btn_row.pack(fill="x")

        def refresh_counter():
            n, total = len(state['selected']), len(state['tiles'])
            info_label.config(text=f"Всего: {total} | Выбрано: {n} — клик = выбрать, 🔄 = перегенерировать, 🗑 = удалить")
            del_sel_btn.config(text=f"🗑 Удалить выбранные ({n})", state=("normal" if n else "disabled"))

        def tile_color(path):
            return ("#e53935", "#ffebee") if path in state['selected'] else ("#cccccc", "#f5f5f5")

        def paint_tile(path):
            tile = state['tiles'].get(path)
            if not tile:
                return
            border, bg = tile_color(path)
            tile.config(highlightbackground=border, highlightcolor=border, bg=bg)
            stack = list(tile.winfo_children())
            while stack:
                w = stack.pop()
                if isinstance(w, Button):
                    continue
                try:
                    w.config(bg=bg)
                except Exception:
                    pass
                stack.extend(w.winfo_children())

        def toggle(path):
            if path in state['selected']:
                state['selected'].discard(path)
            else:
                state['selected'].add(path)
            paint_tile(path)
            refresh_counter()

        def delete_and_rebuild(paths, confirm_text):
            if not paths:
                return
            if not messagebox.askyesno("Удаление картинок", confirm_text, parent=dialog):
                return
            deleted = self._delete_image_files(paths)
            for p in deleted:
                state['selected'].discard(p)
                state['thumb_cache'].pop(p, None)
            rebuild()

        def delete_one(path):
            delete_and_rebuild(
                [path], f"Удалить {os.path.basename(path)}?\nФайл будет удалён с диска.")

        def delete_selected():
            paths = [p for p in state['tiles'] if p in state['selected']]
            delete_and_rebuild(
                paths, f"Удалить выбранные картинки ({len(paths)} шт.)?\nФайлы будут удалены с диска.")

        def delete_all():
            paths = list(state['tiles'].keys())
            delete_and_rebuild(
                paths, f"Удалить ВСЕ картинки проекта ({len(paths)} шт.)?\nФайлы будут удалены с диска.")

        def regen_one(path):
            """Диалог перегенерации: промпт опционален (пусто = исходный из scenes.json)."""
            orig_prompt = self._get_image_prompt(path)
            rdlg = Toplevel(dialog)
            rdlg.title(f"🔄 Перегенерация {os.path.basename(path)}")
            rdlg.geometry("560x280")
            rdlg.transient(dialog)
            Label(rdlg, text="Промпт (можно править; пусто = исходный из scenes.json/prompts.json):",
                  font=("Arial", 9)).pack(anchor="w", padx=10, pady=(10, 2))
            ptext = Text(rdlg, height=8, wrap="word", font=("Consolas", 9))
            ptext.pack(fill="both", expand=True, padx=10)
            if orig_prompt:
                ptext.insert("1.0", orig_prompt)
            rbtns = Frame(rdlg)
            rbtns.pack(fill="x", padx=10, pady=8)

            def start_regen():
                prompt_val = ptext.get("1.0", "end").strip()
                if not prompt_val and not orig_prompt:
                    self.log("❌ Исходного промпта нет (не googler-картинка?) — введите свой промпт", kind="images")
                    return
                # не менял исходный → не передаём (воркер возьмёт сцену целиком из scenes.json)
                custom = "" if prompt_val == orig_prompt.strip() else prompt_val
                rdlg.destroy()
                tile = state['tiles'].get(path)
                if tile and tile.winfo_exists():
                    for w in tile.winfo_children():
                        if isinstance(w, Label) and getattr(w, '_is_thumb', False):
                            w.config(image="", text="⏳ генерация…", width=18, height=9)
                            w.image = None
                state['thumb_cache'].pop(path, None)
                self.log(f"🔄 Перегенерация {os.path.basename(path)}…", kind="images")

                def _done(success):
                    def _ui():
                        try:
                            if dialog.winfo_exists():
                                rebuild()
                        except Exception:
                            pass
                    try:
                        dialog.after(0, _ui)
                    except Exception:
                        pass
                threading.Thread(target=lambda: self._run_regen_image_worker(path, custom, _done),
                                 daemon=True).start()

            Button(rbtns, text="🔄 Перегенерировать", command=start_regen,
                   bg="#9C27B0", fg="white", font=("Arial", 9, "bold")).pack(side="left")
            Button(rbtns, text="Отмена", command=rdlg.destroy, font=("Arial", 9)).pack(side="right")
            ptext.focus_set()

        def select_all():
            state['selected'] = set(state['tiles'].keys())
            for p in state['tiles']:
                paint_tile(p)
            refresh_counter()

        def deselect_all():
            state['selected'].clear()
            for p in state['tiles']:
                paint_tile(p)
            refresh_counter()

        def load_thumbs(paths, idx=0):
            """Порционная загрузка миниатюр, чтобы не морозить UI."""
            if not dialog.winfo_exists():
                return
            chunk = paths[idx:idx + 8]
            for p in chunk:
                tile = state['tiles'].get(p)
                if not tile or p in state['thumb_cache']:
                    continue
                try:
                    # with — закрыть файл сразу (Windows держит хендл → удаление упало бы)
                    with Image.open(p) as img:
                        img.thumbnail((THUMB, THUMB))
                        photo = ImageTk.PhotoImage(img)
                    state['thumb_cache'][p] = photo
                    for w in tile.winfo_children():
                        if isinstance(w, Label) and getattr(w, '_is_thumb', False):
                            # width/height=0 — сброс текстовых единиц placeholder'а (с image они стали бы пикселями)
                            w.config(image=photo, text="", width=0, height=0)
                            w.image = photo
                except Exception:
                    pass
            if idx + 8 < len(paths):
                state['load_job'] = dialog.after(15, lambda: load_thumbs(paths, idx + 8))

        def rebuild():
            if state['load_job']:
                try:
                    dialog.after_cancel(state['load_job'])
                except Exception:
                    pass
                state['load_job'] = None
            for w in grid_frame.winfo_children():
                w.destroy()
            state['tiles'].clear()
            images = self._list_generated_images()
            for i, path in enumerate(images):
                border, bg = tile_color(path)
                tile = Frame(grid_frame, bd=0, highlightthickness=2,
                             highlightbackground=border, highlightcolor=border, bg=bg)
                tile.grid(row=i // COLS, column=i % COLS, padx=4, pady=4, sticky="n")
                state['tiles'][path] = tile

                cached = state['thumb_cache'].get(path)
                thumb_lbl = Label(tile, bg=bg, width=(0 if cached else 18), height=(0 if cached else 9),
                                  text=("" if cached else "…"), font=("Arial", 8))
                thumb_lbl._is_thumb = True
                if cached:
                    thumb_lbl.config(image=cached)
                    thumb_lbl.image = cached
                thumb_lbl.pack(padx=2, pady=(2, 0))
                thumb_lbl.bind("<Button-1>", lambda e, p=path: toggle(p))

                bottom = Frame(tile, bg=bg)
                bottom.pack(fill="x", padx=2, pady=(0, 2))
                try:
                    rel = os.path.relpath(path, self.project_dir)
                except ValueError:
                    rel = os.path.basename(path)
                name_lbl = Label(bottom, text=rel, font=("Arial", 7), bg=bg, anchor="w")
                name_lbl.pack(side="left", fill="x", expand=True)
                name_lbl.bind("<Button-1>", lambda e, p=path: toggle(p))
                Button(bottom, text="🗑", command=lambda p=path: delete_one(p),
                       bg="#ffebee", fg="#000000", font=("Arial", 7), padx=2, pady=0).pack(side="right")
                Button(bottom, text="🔄", command=lambda p=path: regen_one(p),
                       bg="#f3e5f5", fg="#000000", font=("Arial", 7), padx=2, pady=0).pack(side="right", padx=(0, 1))
            _bind_mousewheel_to_canvas_recursive(grid_frame, gal_canvas)
            gal_canvas.yview_moveto(0)
            refresh_counter()
            if not images:
                dialog.destroy()
                return
            if pil_ok:
                to_load = [p for p in images if p not in state['thumb_cache']]
                if to_load:
                    state['load_job'] = dialog.after(15, lambda: load_thumbs(to_load))

        Button(btn_row, text="☑ Выбрать все", command=select_all, font=("Arial", 9)).pack(side="left", padx=2)
        Button(btn_row, text="☐ Снять выбор", command=deselect_all, font=("Arial", 9)).pack(side="left", padx=2)
        del_sel_btn = Button(btn_row, text="🗑 Удалить выбранные (0)", command=delete_selected,
                             bg="#ffcdd2", fg="#000000", font=("Arial", 9, "bold"), state="disabled")
        del_sel_btn.pack(side="left", padx=8)
        Button(btn_row, text="🗑 Удалить ВСЕ", command=delete_all,
               bg="#e53935", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=2)
        Button(btn_row, text="🔄 Обновить", command=rebuild, font=("Arial", 9)).pack(side="left", padx=8)
        Button(btn_row, text="Закрыть", command=dialog.destroy, font=("Arial", 9)).pack(side="right", padx=2)

        _bind_mousewheel_to_scrollable(gal_canvas, gal_canvas)
        rebuild()

    def _managed_reference_manifest_exists(self):
        assets_dir = os.path.join(self.project_dir, "_ASSETS")
        # The immutable pointer is authoritative. lexists intentionally treats
        # a broken/symlinked state artifact as existing so it is loaded and
        # blocked, never mistaken for a brand-new project and overwritten.
        return any(os.path.lexists(os.path.join(assets_dir, name)) for name in (
            PROJECT_REFERENCE_STATE_POINTER_FILE,
            PROJECT_REFERENCE_STATE_FILE,
        ))

    def _read_reference_state_identity(self):
        if callable(current_project_reference_state):
            state = current_project_reference_state(self.project_dir)
            if not isinstance(state, dict):
                raise ReferenceContractError("reference state identity is invalid")
            return {
                "revision_id": state.get("revision_id"),
                "commit_id": state.get("commit_id"),
                "state_token": state.get("state_token"),
            }
        revision = (
            current_project_reference_revision(self.project_dir)
            if callable(current_project_reference_revision)
            else None
        )
        return {
            "revision_id": revision,
            "commit_id": None,
            "state_token": None,
        }

    def _cache_reference_state_identity(self, identity):
        self._reference_state_revision = identity.get("revision_id")
        self._reference_state_commit_id = identity.get("commit_id")
        self._reference_state_token = identity.get("state_token")

    def _load_managed_reference_state(self):
        """Restore the project-owned reference map without hiding damage."""
        try:
            manual, style, issues = load_project_reference_state(self.project_dir)
            identity = self._read_reference_state_identity()
        except (ReferenceContractError, OSError, ValueError) as exc:
            self._reference_state_issues = (str(exc),)
            try:
                self._cache_reference_state_identity(
                    self._read_reference_state_identity()
                )
            except (ReferenceContractError, OSError, ValueError):
                self._reference_state_revision = None
                self._reference_state_commit_id = None
                self._reference_state_token = None
            self.log(f"❌ Паспорт референсов повреждён: {exc}", kind="images")
            if hasattr(self, 'ref_treeview'):
                self.update_ref_treeview()
            if hasattr(self, 'style_refs_label'):
                self._update_style_refs_label()
            return False
        self.user_references = manual
        self.style_references = style
        self._cache_reference_state_identity(identity)
        if self._managed_reference_manifest_exists():
            self._reference_copy_blocker = None
        self._reference_state_issues = tuple(
            ([self._reference_copy_blocker] if self._reference_copy_blocker else [])
            + list(issues)
        )
        for issue in issues:
            self.log(f"❌ Референс повреждён: {issue}", kind="images")
        if hasattr(self, 'ref_treeview'):
            self.update_ref_treeview()
        if hasattr(self, 'style_refs_label'):
            self._update_style_refs_label()
        return not issues

    def reload_project_reference_state(self):
        """Reload current state/revision after a CAS conflict in another app."""
        if self._reference_copy_blocker and not self._managed_reference_manifest_exists():
            if not messagebox.askyesno(
                    "Референсы не скопированы",
                    (f"{self._reference_copy_blocker}.\n\n"
                     "Продолжить этот проект без референсов исходного проекта? "
                     "Их project-owned копии в исходном проекте останутся.")):
                return
            self._reference_copy_blocker = None
            self._reference_state_issues = ()
            self.user_references = {}
            self.style_references = []
            self.update_ref_treeview()
            self._update_style_refs_label()
            self.log(
                "ℹ️ Пользователь явно подтвердил продолжение проекта без копирования референсов",
                kind="images",
            )
            return
        if self._load_managed_reference_state():
            self.log(
                "⟳ Паспорт референсов и его актуальная ревизия перечитаны с диска",
                kind="images",
            )

    def _save_managed_reference_state(self, manual_groups, style_sources):
        """CAS commit: a stale app instance can never overwrite a newer map."""
        try:
            if callable(current_project_reference_state):
                save_project_reference_state(
                    self.project_dir,
                    manual_groups,
                    style_sources,
                    expected_revision=self._reference_state_revision or "",
                    expected_commit_id=self._reference_state_commit_id or "",
                )
                self._cache_reference_state_identity(
                    self._read_reference_state_identity()
                )
            else:
                save_project_reference_state(
                    self.project_dir, manual_groups, style_sources
                )
        except ReferenceContractError as exc:
            if "changed in another app instance" in str(exc):
                raise ReferenceContractError(
                    "референсы уже изменены в другом окне; нажмите ⟳, "
                    "проверьте актуальный набор и повторите действие"
                ) from exc
            raise

    def _import_reference_configuration(self, manual_groups, style_sources):
        """Import all external bytes, then atomically publish one mapping."""
        if not isinstance(manual_groups, dict):
            raise ReferenceContractError("Именованные референсы должны быть словарём")
        if not isinstance(style_sources, (list, tuple)):
            raise ReferenceContractError("Стиль-референсы должны быть списком")

        managed_manual = {}
        for name, sources in manual_groups.items():
            if not isinstance(name, str) or not name.strip():
                raise ReferenceContractError("У каждой группы референсов должно быть имя")
            if not isinstance(sources, (list, tuple)) or not sources:
                raise ReferenceContractError(
                    f"Группа {name!r} настроена, но не содержит ни одного фото"
                )
            artifacts = import_reference_sources(
                list(sources),
                self.project_dir,
                kind="hero_source",
                max_sources=HERO_ANGLES_MAX,
            )
            managed_manual[name.strip()] = [artifact.path for artifact in artifacts]

        managed_style = []
        if style_sources:
            artifacts = import_reference_sources(
                list(style_sources),
                self.project_dir,
                kind="style_source",
                max_sources=STYLE_SOURCE_MAX,
            )
            managed_style = [artifact.path for artifact in artifacts]

        # This is the sole visibility/commit point. All files and cross-group
        # duplicate constraints have already been validated.
        self._save_managed_reference_state(managed_manual, managed_style)
        self.user_references = managed_manual
        self.style_references = managed_style
        self._reference_copy_blocker = None
        self._reference_state_issues = ()
        if hasattr(self, 'ref_treeview'):
            self.update_ref_treeview()
        if hasattr(self, 'style_refs_label'):
            self._update_style_refs_label()

    def _commit_managed_reference_mapping(self, manual_groups, style_sources):
        """Commit already imported project-owned paths before mutating the UI."""
        self._save_managed_reference_state(manual_groups, style_sources)
        self.user_references = {
            name: list(paths) for name, paths in manual_groups.items()
        }
        self.style_references = list(style_sources)
        self._reference_copy_blocker = None
        self._reference_state_issues = ()
        if hasattr(self, 'ref_treeview'):
            self.update_ref_treeview()
        if hasattr(self, 'style_refs_label'):
            self._update_style_refs_label()

    def _copy_references_from_project(self, source_project):
        """Explicitly clone verified refs into a genuinely new target project."""
        if self._managed_reference_manifest_exists():
            # The constructor has already loaded this state.  Never republish a
            # source project's mapping over an existing (even damaged) target.
            self.log(
                "🛡️ Существующий паспорт референсов проекта сохранён без изменений",
                kind="images",
            )
            return False
        try:
            source_project._build_current_reference_bundle()
        except ReferenceContractError as exc:
            issue = f"исходный паспорт референсов повреждён: {exc}"
            self._reference_copy_blocker = issue
            self._reference_state_issues = (issue,)
            self._refresh_reference_status_ui()
            self.log(
                f"❌ BLOCKED: референсы не скопированы: {issue}",
                kind="images",
            )
            return False
        manual = {
            name: list(paths)
            for name, paths in source_project.user_references.items()
        }
        style = list(source_project.style_references)
        if not manual and not style:
            return True
        try:
            self._import_reference_configuration(manual, style)
        except ReferenceContractError as exc:
            issue = f"референсы не скопированы в новый проект: {exc}"
            self._reference_copy_blocker = issue
            self._reference_state_issues = (issue,)
            self._refresh_reference_status_ui()
            self.log(
                f"❌ BLOCKED: {issue}",
                kind="images",
            )
            return False
        self.log(
            "🛡️ Проверенные референсы явно скопированы в project-owned хранилище нового проекта",
            kind="images",
        )
        return True

    def _initialize_references_from_project(self, source_project, *,
                                            target_dir_existed):
        """Copy only to a directory that did not exist before project creation."""
        if self._managed_reference_manifest_exists():
            self.log(
                "🛡️ Существующий паспорт референсов проекта сохранён без изменений",
                kind="images",
            )
            return False
        if target_dir_existed:
            if source_project.user_references or source_project.style_references:
                issue = (
                    "целевая папка уже существовала без паспорта; референсы "
                    "исходного проекта не скопированы автоматически"
                )
                self._reference_copy_blocker = issue
                self._reference_state_issues = (issue,)
                self._refresh_reference_status_ui()
                self.log(f"❌ BLOCKED: {issue}", kind="images")
            return False
        return self._copy_references_from_project(source_project)

    def _switch_reference_project(self, previous_manual, previous_style, *,
                                  copy_allowed=True, copy_error=""):
        """Load an existing target manifest or copy current refs into the target."""
        if self._managed_reference_manifest_exists():
            self._load_managed_reference_state()
            return
        if not previous_manual and not previous_style:
            self.user_references = {}
            self.style_references = []
            self._reference_copy_blocker = None
            self._reference_state_issues = ()
            self.update_ref_treeview()
            self._update_style_refs_label()
            return
        if not copy_allowed:
            self.user_references = {}
            self.style_references = []
            self._reference_copy_blocker = (
                copy_error or "исходный паспорт референсов не прошёл проверку"
            )
            self._reference_state_issues = (self._reference_copy_blocker,)
            self.update_ref_treeview()
            self._update_style_refs_label()
            return
        try:
            self._import_reference_configuration(previous_manual, previous_style)
        except ReferenceContractError as exc:
            self._reference_state_issues = (str(exc),)
            self.log(
                f"❌ Не удалось перенести референсы в новый проект: {exc}",
                kind="images",
            )

    def _choose_reference_revision(self, candidates, current_state_issue=""):
        """Show every valid historical commit; return the selected row or None."""
        dialog = Toplevel(self.winfo_toplevel())
        dialog.title("История референсов")
        dialog.geometry("760x390")
        outer = Frame(dialog, padx=12, pady=10)
        outer.pack(fill="both", expand=True)
        if current_state_issue:
            Label(
                outer,
                text=("⛔ Текущий паспорт повреждён. Выберите проверенную ревизию "
                      "для атомарного восстановления."),
                fg="#b71c1c",
                font=("Arial", 9, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(0, 8))
        Label(
            outer,
            text="Все проверенные предыдущие коммиты (новые сверху):",
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        list_frame = Frame(outer)
        list_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        revision_list = Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            exportselection=False,
            font=("Consolas", 9),
        )
        revision_list.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=revision_list.yview)
        for row in candidates:
            sequence = int(row.get("sequence") or 0)
            created_at = row.get("created_at")
            try:
                timestamp = datetime.fromtimestamp(float(created_at)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (TypeError, ValueError, OSError, OverflowError):
                timestamp = "время неизвестно"
            revision_id = str(row.get("revision_id") or "")
            commit_label = f"#{sequence}" if sequence else "legacy"
            revision_list.insert(
                END,
                (f"{commit_label:>8}  {timestamp}  "
                 f"фото героев: {int(row.get('manual_count') or 0):>2}  "
                 f"стиль: {int(row.get('style_count') or 0):>2}  "
                 f"rev {revision_id[:12]}…"),
            )
        if candidates:
            revision_list.selection_set(0)
            revision_list.activate(0)

        selected = {"row": None}

        def choose(_event=None):
            selection = revision_list.curselection()
            if not selection:
                return
            selected["row"] = candidates[int(selection[0])]
            dialog.destroy()

        controls = Frame(outer)
        controls.pack(fill="x", pady=(8, 0))
        Button(
            controls,
            text="↶ Восстановить выбранную",
            command=choose,
            bg="#ef6c00",
            fg="white",
            font=("Arial", 9, "bold"),
        ).pack(side="left")
        Button(controls, text="Отмена", command=dialog.destroy).pack(side="right")
        revision_list.bind("<Double-Button-1>", choose)
        try:
            dialog.grab_set()
        except Exception:
            pass
        self.wait_window(dialog)
        return selected["row"]

    def restore_previous_reference_revision(self):
        """Select and CAS-restore any valid non-current reference commit."""
        if not callable(list_project_reference_revisions) or not callable(
                restore_project_reference_revision):
            messagebox.showwarning(
                "История референсов",
                "В этой сборке модуль истории референсов ещё недоступен.",
            )
            return
        try:
            result = list_project_reference_revisions(self.project_dir)
        except (ReferenceContractError, OSError, ValueError) as exc:
            messagebox.showerror("История референсов", str(exc))
            return
        revisions = result.get("revisions", []) if isinstance(result, dict) else result
        if not isinstance(revisions, (list, tuple)):
            revisions = []
        candidates = []
        current_state_issue = ""
        for revision in revisions:
            if not isinstance(revision, dict):
                continue
            current_state_issue = current_state_issue or str(
                revision.get("current_state_issue") or ""
            )
            revision_id = revision.get("revision_id") or revision.get("id")
            if not isinstance(revision_id, str) or not revision_id:
                continue
            if revision.get("is_current") is True or revision.get("current") is True:
                continue
            issues = revision.get("issues") or ()
            if issues:
                continue
            candidates.append(revision)
        if not candidates:
            messagebox.showinfo(
                "История референсов",
                "Предыдущих проверенных ревизий пока нет.",
            )
            return
        metadata = self._choose_reference_revision(
            candidates, current_state_issue=current_state_issue
        )
        if metadata is None:
            return
        revision_id = str(metadata.get("revision_id") or "")
        manual_count = int(metadata.get("manual_count") or 0)
        style_count = int(metadata.get("style_count") or 0)
        if not messagebox.askyesno(
                "Восстановить референсы",
                (f"Восстановить выбранную ревизию {revision_id[:12]}…?\n"
                 f"Фото героев: {manual_count}; стиль-референсов: {style_count}.\n\n"
                 "Текущее состояние останется в истории.")):
            return
        try:
            if callable(current_project_reference_state):
                state_token = metadata.get("current_state_token")
                if not isinstance(state_token, str) or not state_token:
                    raise ReferenceContractError(
                        "история не содержит CAS-токен; обновите список и повторите"
                    )
                current_commit_id = metadata.get("current_commit_id")
                if current_commit_id is not None and not isinstance(
                        current_commit_id, str):
                    raise ReferenceContractError(
                        "история содержит некорректный идентификатор коммита"
                    )
                restore_project_reference_revision(
                    self.project_dir,
                    revision_id,
                    expected_current_commit_id=current_commit_id or "",
                    expected_state_token=state_token,
                    repair_damaged_current=bool(current_state_issue),
                )
            else:
                restore_project_reference_revision(self.project_dir, revision_id)
            if not self._load_managed_reference_state():
                raise ReferenceContractError(
                    "восстановленная ревизия не прошла проверку целостности"
                )
        except (ReferenceContractError, OSError, ValueError) as exc:
            messagebox.showerror("Референсы не восстановлены", str(exc))
            return
        self.log(
            f"↶ Референсы восстановлены из ревизии {revision_id[:12]}…",
            kind="images",
        )

    def _build_current_reference_bundle(self):
        """Verify current managed state and freeze one immutable worker bundle."""
        empty_groups = [
            name for name, paths in self.user_references.items()
            if not isinstance(paths, (list, tuple)) or not paths
        ]
        if empty_groups:
            raise ReferenceContractError(
                f"Группа {empty_groups[0]!r} настроена без фото"
            )
        saved_manual, saved_style, issues = load_project_reference_state(
            self.project_dir
        )
        configured = bool(self.user_references or self.style_references)
        if configured and not self._managed_reference_manifest_exists():
            raise ReferenceContractError(
                "у настроенных референсов нет project-owned паспорта; выберите их заново"
            )
        current_manual = {
            name: [os.path.realpath(path) for path in paths]
            for name, paths in self.user_references.items()
        }
        current_style = [os.path.realpath(path) for path in self.style_references]
        saved_manual_real = {
            name: [os.path.realpath(path) for path in paths]
            for name, paths in saved_manual.items()
        }
        saved_style_real = [os.path.realpath(path) for path in saved_style]
        if self._managed_reference_manifest_exists() and (
            current_manual != saved_manual_real or current_style != saved_style_real
        ):
            raise ReferenceContractError(
                "текущие референсы не совпадают с атомарным паспортом проекта"
            )
        if issues:
            self._reference_state_issues = tuple(issues)
            raise ReferenceContractError(
                f"нарушена целостность project-owned референсов: {issues[0]}"
            )
        manifest = build_reference_bundle_manifest(
            self.project_dir, self.user_references, self.style_references
        )
        self._reference_state_issues = ()
        return str(manifest)

    def _reference_preflight(self, operation):
        """One fail-closed reference gate for Bing, all AI modes and regen."""
        operation = str(operation or "").strip().lower()
        allowed = {"bing", "gpt", "custom", "existing", "regen"}
        if operation not in allowed:
            self.log(
                f"❌ Неизвестный режим проверки референсов: {operation or '—'}",
                kind="images",
            )
            return None
        if self._reference_copy_blocker:
            self._reference_state_issues = (self._reference_copy_blocker,)
            self._refresh_reference_status_ui()
            self.log(
                (f"❌ BLOCKED: {self._reference_copy_blocker}. Нажмите ⟳ и "
                 "явно подтвердите продолжение без копии либо выберите референсы заново."),
                kind="images",
            )
            return None
        try:
            reference_manifest = self._build_current_reference_bundle()
        except (ReferenceContractError, OSError, ValueError) as exc:
            if not self._reference_state_issues:
                self._reference_state_issues = (str(exc),)
            self._refresh_reference_status_ui()
            self.log(
                f"❌ BLOCKED: паспорт референсов не прошёл проверку: {exc}. Ничего не отправлено.",
                kind="images",
            )
            return None

        hero_groups = len(self.user_references)
        style_sources = len(self.style_references)
        raw_sources = (
            sum(len(paths) for paths in self.user_references.values())
            + style_sources
        )
        auto_hero = bool(self.auto_hero_var.get())
        analyze_story = bool(
            getattr(self, 'analyze_story_var', None)
            and self.analyze_story_var.get()
        )
        if operation == "bing":
            if hero_groups or style_sources or auto_hero or analyze_story:
                self.log(
                    "❌ Bing не принимает visual refs/Авто-героя и не строит story-plan. "
                    "Project-owned файлы сохранены; выберите AI-генерацию или выключите функции.",
                    kind="images",
                )
                return None
            return {
                "reference_manifest": reference_manifest,
                "raw_sources": raw_sources,
                "required_min": 0,
                "capacity": 0,
                "auto_hero": auto_hero,
                "analyze_story": analyze_story,
            }

        model = str(self.googler_model.get() or "").strip().upper()
        capacity = MODEL_REFERENCE_CAPACITIES.get(model)
        if capacity is None:
            self.log(
                f"❌ Для модели {model or '—'} не зарегистрирован лимит референсов",
                kind="images",
            )
            return None
        if hero_groups > HERO_GROUPS_MAX or any(
                len(paths) > HERO_ANGLES_MAX
                for paths in self.user_references.values()):
            self.log(
                f"❌ Лимит: до {HERO_GROUPS_MAX} героев и до {HERO_ANGLES_MAX} фото каждого. Ничего не отправлено.",
                kind="images",
            )
            return None
        if style_sources > STYLE_SOURCE_MAX:
            self.log(
                f"❌ Лимит style-референсов: {STYLE_SOURCE_MAX}. Ничего не отправлено.",
                kind="images",
            )
            return None
        if operation in ("custom", "existing") and (auto_hero or analyze_story):
            mode_name = "кастомных промтов" if operation == "custom" else "готового prompts.json"
            self.log(
                (f"❌ Режим {mode_name} не строит новый story-plan: выключите "
                 "«Авто-герой» и «Анализ персонажей/локаций» либо выберите GPT. "
                 "Настройки не будут проигнорированы молча."),
                kind="images",
            )
            return None
        if operation != "regen" and not analyze_story and hero_groups > 1:
            self.log(
                "❌ Несколько именованных героев требуют анализа истории для точной привязки к сценам.",
                kind="images",
            )
            return None
        visual_identity = bool(hero_groups or auto_hero)
        visual_requested = bool(visual_identity or style_sources)
        required_min = int(visual_identity) + int(bool(style_sources))
        if visual_requested and capacity == 0:
            self.log(
                f"❌ Модель {model} не принимает визуальные референсы; файлы сохранены, генерация не запущена.",
                kind="images",
            )
            return None
        if required_min > capacity:
            self.log(
                (f"❌ Для кадра нужно минимум {required_min} входа(ов), модель {model} "
                 f"принимает {capacity}. Референсы не потеряны; выберите другую модель или набор."),
                kind="images",
            )
            return None
        if visual_requested:
            self.log(
                (f"🛡️ Референсы: {raw_sources} исходников → минимум "
                 f"{required_min}/{capacity} входов модели; потеряно 0 "
                 "(точное использование фиксирует worker)."),
                kind="images",
            )
        return {
            "reference_manifest": reference_manifest,
            "raw_sources": raw_sources,
            "required_min": required_min,
            "capacity": capacity,
            "auto_hero": auto_hero,
            "analyze_story": analyze_story,
            "model": model,
        }

    def _refresh_reference_status_ui(self):
        def _refresh():
            if hasattr(self, 'ref_treeview'):
                self.update_ref_treeview()
            if hasattr(self, 'style_refs_label'):
                self._update_style_refs_label()
        try:
            self.after(0, _refresh)
        except Exception:
            try:
                _refresh()
            except Exception:
                pass

    def edit_user_reference(self):
        """Создание или редактирование именованного референса"""
        # Проверяем, выбран ли существующий референс для редактирования
        selected = self.ref_treeview.selection()
        existing_name = None
        existing_paths = []
        
        if selected:
            item = self.ref_treeview.item(selected[0])
            existing_name = item['text']
            if existing_name in self.user_references:
                existing_paths = self.user_references[existing_name]
        
        # Диалог для ввода имени референса
        from tkinter import simpledialog
        ref_name = simpledialog.askstring(
            "Именованный референс",
            "Введите имя референса:",
            initialvalue=existing_name if existing_name else ""
        )
        
        if not ref_name or not ref_name.strip():
            return
        
        ref_name = ref_name.strip()
        collision = next((
            current_name for current_name in self.user_references
            if current_name != existing_name
            and normalize_alias(current_name) == normalize_alias(ref_name)
        ), None)
        if collision is not None:
            messagebox.showerror(
                "Имя уже используется",
                f"Группа {collision!r} уже существует. Ничего не изменено.",
            )
            return
        if existing_name is None and len(self.user_references) >= HERO_GROUPS_MAX:
            messagebox.showerror(
                "Лимит референсов",
                f"Можно настроить не более {HERO_GROUPS_MAX} героев. Ничего не добавлено.",
            )
            return
        
        # Выбор изображений
        filenames = filedialog.askopenfilenames(
            title=f"Выберите изображения для референса '{ref_name}'",
            filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.webp"),
                ("All", "*.*")
            ],
            initialdir=existing_paths[0] if existing_paths and os.path.exists(existing_paths[0]) else None
        )
        
        if filenames:
            if len(filenames) > HERO_ANGLES_MAX:
                messagebox.showerror(
                    "Лимит ракурсов",
                    (f"Для одного героя можно выбрать до {HERO_ANGLES_MAX} фото. "
                     f"Выбрано {len(filenames)}; набор не изменён."),
                )
                return
            try:
                imported = import_reference_sources(
                    list(filenames),
                    self.project_dir,
                    kind="hero_source",
                    max_sources=HERO_ANGLES_MAX,
                )
                updated_groups = replace_named_reference_group(
                    self.user_references,
                    selected_name=existing_name,
                    new_name=ref_name,
                    sources=[artifact.path for artifact in imported],
                )
                self._commit_managed_reference_mapping(
                    updated_groups, self.style_references
                )
            except ReferenceContractError as exc:
                messagebox.showerror("Референсы не изменены", str(exc))
                return
            count = len(filenames)
            self.log(
                f"📎 {'Обновлен' if existing_name else 'Создан'} референс "
                f"'{ref_name}': {count} фото импортированы внутрь проекта → 1 вход",
                kind="images",
            )
    
    def remove_user_reference(self):
        """Удаление выбранного референса"""
        selected = self.ref_treeview.selection()
        if selected:
            item = self.ref_treeview.item(selected[0])
            ref_name = item['text']
            if ref_name in self.user_references:
                updated = {
                    name: list(paths)
                    for name, paths in self.user_references.items()
                    if name != ref_name
                }
                try:
                    self._commit_managed_reference_mapping(
                        updated, self.style_references
                    )
                except ReferenceContractError as exc:
                    messagebox.showerror("Референс не удалён", str(exc))
                    return
                self.log(
                    f"🗑️ Убрана привязка референса {ref_name!r}; "
                    "project-owned копия оставлена для восстановления",
                    kind="images",
                )
    
    def update_ref_treeview(self):
        """Обновляет отображение референсов в Treeview"""
        # Очищаем текущие элементы
        for item in self.ref_treeview.get_children():
            self.ref_treeview.delete(item)
        
        # Добавляем все configured референсы. Пропавшие файлы не удаляются
        # молча: это блокирующая ошибка worker-а, а здесь она видна заранее.
        for ref_name, paths in self.user_references.items():
            valid_paths = [p for p in paths if os.path.exists(p)]
            missing = len(paths) - len(valid_paths)
            value = f"{len(paths)} фото → 1 вход"
            issue_count = len(self._reference_state_issues)
            if missing or issue_count:
                details = []
                if missing:
                    details.append(f"нет файлов: {missing}")
                if issue_count:
                    details.append(f"ошибок паспорта: {issue_count}")
                value += f" · ⛔ BLOCKED ({', '.join(details)})"
            self.ref_treeview.insert("", "end", text=ref_name, values=(value,))
    
    def edit_style_references(self):
        """Выбор референсов стиля (стабильно в каждую генерацию)."""
        filenames = filedialog.askopenfilenames(
            title=f"Выберите изображения-референсы стиля (до {STYLE_SOURCE_MAX} шт.)",
            filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.webp"),
                ("All", "*.*")
            ]
        )
        if filenames:
            if len(filenames) > STYLE_SOURCE_MAX:
                messagebox.showerror(
                    "Лимит style-референсов",
                    (f"Можно выбрать до {STYLE_SOURCE_MAX} фото стиля. "
                     f"Выбрано {len(filenames)}; набор не изменён."),
                )
                return
            try:
                imported = import_reference_sources(
                    list(filenames),
                    self.project_dir,
                    kind="style_source",
                    max_sources=STYLE_SOURCE_MAX,
                )
                self._commit_managed_reference_mapping(
                    self.user_references,
                    [artifact.path for artifact in imported],
                )
            except ReferenceContractError as exc:
                messagebox.showerror("Стиль-референсы не изменены", str(exc))
                return
            self.log(f"🎨 Установлено {len(self.style_references)} стиль-референсов (стабильно в каждую генерацию)", kind="images")

    def clear_style_references(self):
        """Очистить все референсы стиля (кнопка 🗑 рядом со «Стиль-рефы»)."""
        if not self.style_references:
            return
        count = len(self.style_references)
        try:
            self._commit_managed_reference_mapping(self.user_references, [])
        except ReferenceContractError as exc:
            messagebox.showerror("Стиль-референсы не очищены", str(exc))
            return
        self.log(
            f"🗑️ Убрана привязка {count} стиль-референсов; "
            "project-owned копии оставлены для восстановления",
            kind="images",
        )

    def _update_style_refs_label(self):
        """Обновляет текст лейбла стиль-рефов + активность кнопки очистки"""
        if hasattr(self, 'style_refs_label'):
            all_paths = [
                path
                for paths in self.user_references.values()
                for path in paths
            ] + list(self.style_references)
            missing_total = sum(1 for path in all_paths if not os.path.isfile(path))
            issue_count = len(self._reference_state_issues)
            blocked = bool(missing_total or issue_count)
            blocked_text = ""
            if blocked:
                details = []
                if missing_total:
                    details.append(f"недоступно файлов: {missing_total}")
                if issue_count:
                    details.append(f"ошибок паспорта: {issue_count}")
                blocked_text = f"⛔ BLOCKED — {', '.join(details)}"
            if self.style_references:
                count = len(self.style_references)
                model = self.googler_model.get() if hasattr(self, 'googler_model') else ""
                capacity = MODEL_REFERENCE_CAPACITIES.get(model)
                suffix = f" · модель до {capacity}" if capacity is not None else ""
                self.style_refs_label.config(
                    text=(
                        f"({count} фото → 1 вход{suffix}; {blocked_text})"
                        if blocked else
                        f"({count} фото → 1 вход{suffix}; потеряно 0)"
                    )
                )
            else:
                model = self.googler_model.get() if hasattr(self, 'googler_model') else ""
                capacity = MODEL_REFERENCE_CAPACITIES.get(model)
                self.style_refs_label.config(
                    text=(
                        f"(0 фото; модель до {capacity} входов; {blocked_text})"
                        if capacity is not None and blocked else
                        f"(0 фото; модель до {capacity} входов)"
                        if capacity is not None else
                        f"({blocked_text})" if blocked else ""
                    )
                )
        if hasattr(self, 'style_refs_clear_btn'):
            self.style_refs_clear_btn.config(state=("normal" if self.style_references else "disabled"))

    def _get_audio_duration_ffprobe(self, audio_path):
        """Замер длины аудиофайла через ffprobe (секунды, float). None при ошибке."""
        try:
            import subprocess
            ffprobe_exe = "ffprobe"
            try:
                import portable_paths
                ffprobe_exe = portable_paths.get_ffprobe_executable()
            except ImportError:
                pass
            cmd = [ffprobe_exe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception:
            pass
        return None

    def _read_source_text(self):
        """Исходный текст проекта (тот же файл, что уходит в TTS). '' если недоступен."""
        try:
            src = self.source_text_file.get()
        except Exception:
            src = ""
        if not src or not os.path.exists(src):
            return ""
        try:
            with open(src, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except Exception:
            return ""

    def check_audio_completeness(self):
        """Кросс-проверка: покрывает ли озвученное аудио исходный текст проекта.

        Возвращает dict evaluate_audio_completeness (+ ok=True/checked=False если ffprobe нет).
        Считает суммарную длительность аудио из TTS-папок проекта (те же файлы, что идут в рендер,
        без фоновой музыки/конечного трека). Не удаляет и не меняет аудио.
        """
        source_text = self._read_source_text()
        if not source_text.strip():
            return {"ok": True, "checked": False, "msg": ""}

        project_dir_normalized = os.path.normpath(os.path.abspath(self.project_dir))
        assets_dir = os.path.normpath(os.path.join(self.project_dir, "_ASSETS"))
        SUPPORTED_AUDIO = ['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac']
        try:
            bg = self.background_audio_file.get()
        except Exception:
            bg = ""
        try:
            et = self.end_track_file.get()
        except Exception:
            et = ""
        bg_n = os.path.normpath(bg) if bg else ""
        et_n = os.path.normpath(et) if et else ""

        total_sec = 0.0
        measured = 0
        try:
            for root, dirs, files in os.walk(project_dir_normalized):
                rn = os.path.normpath(root)
                if rn == assets_dir:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if not d.startswith('.') and 'temp' not in d.lower() and d != '_ASSETS']
                for f in files:
                    if f.startswith('.') or 'temp' in f.lower() or 'final_video' in f.lower():
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in SUPPORTED_AUDIO:
                        continue
                    full = os.path.normpath(os.path.join(root, f))
                    if not (os.path.isfile(full) and os.path.getsize(full) > 0):
                        continue
                    if bg_n and full == bg_n:
                        continue
                    if et_n and full == et_n:
                        continue
                    dur = self._get_audio_duration_ffprobe(full)
                    if dur is not None and dur > 0:
                        total_sec += dur
                        measured += 1
        except Exception:
            # Не смогли обойти/замерить — не блокируем (гейт не применяется)
            return {"ok": True, "checked": False, "msg": ""}

        if measured == 0:
            # ffprobe не сработал ни на одном файле — нет данных для проверки, не блокируем
            return {"ok": True, "checked": False, "msg": ""}

        return evaluate_audio_completeness(source_text, total_sec)

    def _images_worker_with_reenable(self, worker_func):
        """Обёртка для прямого запуска Images — гарантирует re-enable кнопки."""
        try:
            worker_func()
        finally:
            try:
                self.root.after(0, lambda: self._set_action_buttons_state('images', 'normal'))
            except Exception:
                pass

    def _videos_worker_with_reenable(self, worker_func):
        """Обёртка для прямого запуска Videos — гарантирует re-enable кнопки."""
        try:
            worker_func()
        finally:
            try:
                self.root.after(0, lambda: self._set_action_buttons_state('videos', 'normal'))
            except Exception:
                pass

    @staticmethod
    def _sha256_file_for_cache(path):
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _unverified_legacy_cache_inventory(self):
        """Return exact (filename, sha) inventory lacking a fresh/trusted receipt."""
        googler_dir = os.path.join(self.project_dir, "googler")
        if not os.path.isdir(googler_dir):
            return ()
        frames = []
        for name in os.listdir(googler_dir):
            stem, extension = os.path.splitext(name)
            if not stem.isdigit() or extension.lower() != ".jpg":
                continue
            path = os.path.join(googler_dir, name)
            if os.path.isfile(path):
                frames.append((stem, path))
        if not frames:
            return ()

        receipt_frames = {}
        receipt_path = os.path.join(googler_dir, "image_receipts.v1.json")
        try:
            if os.path.getsize(receipt_path) > 16 * 1024 * 1024:
                raise ValueError("receipt file is too large")
            with open(receipt_path, "r", encoding="utf-8", errors="strict") as fh:
                receipt = json.load(fh)
            if (
                not isinstance(receipt, dict)
                or receipt.get("schema_version") != 2
                or not isinstance(receipt.get("frames"), dict)
            ):
                raise ValueError("unsupported receipt schema")
            receipt_frames = receipt["frames"]
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            receipt_frames = None

        unverified = []
        for frame_key, image_path in sorted(frames, key=lambda item: item[1]):
            row = receipt_frames.get(frame_key) if receipt_frames is not None else None
            try:
                actual_sha = self._sha256_file_for_cache(image_path)
            except OSError:
                actual_sha = "unreadable"
            if not isinstance(row, dict):
                unverified.append((os.path.basename(image_path), actual_sha))
                continue
            exact_output = row.get("output_sha256") == actual_sha
            exact_generation = bool(
                isinstance(row.get("generation_fingerprint"), str)
                and row.get("generation_fingerprint")
                and row.get("completed_fingerprint")
                == row.get("generation_fingerprint")
            )
            fresh = row.get("status") == "fresh"
            trusted_legacy = (
                row.get("status") == "legacy"
                and row.get("trusted_by_user") is True
                and isinstance(row.get("legacy_trusted_at"), int)
                and row.get("legacy_trusted_at") > 0
            )
            if not (exact_output and exact_generation and (fresh or trusted_legacy)):
                unverified.append((os.path.basename(image_path), actual_sha))
        return tuple(unverified)

    def _count_unverified_legacy_cache_frames(self):
        return len(self._unverified_legacy_cache_inventory())

    def _prepare_legacy_cache_consent(self):
        """Turn the checkbox into explicit consent for exactly one queued run."""
        requested = bool(self.trust_legacy_image_cache.get())
        self.trust_legacy_image_cache.set(False)
        self._trust_legacy_image_cache_once = False
        self._trust_legacy_image_cache_snapshot = ()
        if not requested:
            return True
        if not self.use_cache.get():
            self.log(
                "ℹ️ Доверие legacy-кэшу не требуется: кэш выключен. Галка сброшена.",
                kind="images",
            )
            return True
        try:
            inventory = self._unverified_legacy_cache_inventory()
            count = len(inventory)
        except (OSError, ValueError) as exc:
            self.log(
                f"❌ Не удалось проверить legacy-кэш: {exc}. Запуск отменён.",
                kind="images",
            )
            return False
        if count == 0:
            self.log(
                "🛡️ Непроверенных legacy-кадров: 0; дополнительное доверие не передаётся.",
                kind="images",
            )
            return True
        confirmed = messagebox.askyesno(
            "Одноразовое доверие legacy-кэшу",
            (f"Найдено непроверенных существующих кадров: {count}.\n\n"
             "Разрешить worker принять подходящие legacy-кадры без старого "
             "паспорта? Изменённые/несовместимые кадры всё равно будут "
             "пересозданы.\n\nСогласие действует только на этот запуск."),
        )
        if not confirmed:
            self.log(
                f"ℹ️ Доверие {count} непроверенным кадрам не подтверждено; запуск отменён.",
                kind="images",
            )
            return False
        self._trust_legacy_image_cache_once = True
        self._trust_legacy_image_cache_snapshot = inventory
        self.log(
            f"⚠️ Одноразово подтверждено до {count} непроверенных legacy-кадров; галка сброшена.",
            kind="images",
        )
        return True

    def run_googler_images(self):
        """Запуск генерации изображений через Googler (через очередь)"""
        if not self._prepare_legacy_cache_consent():
            return
        self._set_action_buttons_state('images', 'disabled')
        if self.googler_queue:
            success, message = self.googler_queue.add_to_queue(self.project_name + "_images", self._run_googler_images_worker)
            self.log(f"📊 {message}", kind="images")
            if not success:
                self._trust_legacy_image_cache_once = False
                self._trust_legacy_image_cache_snapshot = ()
                self._set_action_buttons_state('images', 'normal')
                self.log(message, kind="images")
                return
        else:
            threading.Thread(target=lambda: self._images_worker_with_reenable(self._run_googler_images_worker), daemon=True).start()

    def _finalize_googler_images_success(self, run_snapshot):
        """Run every post-success image action exactly once per worker path."""
        # Cross-project reuse is intentionally absent from the success path.
        # Coerce any stale in-memory value as a final defense; never call the
        # copier from a completed generation.
        copy_variable = getattr(self, 'googler_copy_similar', None)
        if copy_variable is not None:
            try:
                stale_request = bool(copy_variable.get())
                copy_variable.set(False)
            except Exception:
                stale_request = False
            if stale_request:
                self.log(
                    "🛡️ Копирование картинок между проектами BLOCKED: "
                    f"{CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON}.",
                    kind="images",
                )
        if (
            getattr(self, '_deferred_video_pending', False)
            and self.googler_video_after_images.get()
        ):
            self._deferred_video_pending = False
            self.log(
                "🎬 Изображения готовы — запускаю отложенную генерацию видео",
                kind="images",
            )
            try:
                if not self.googler_selected_images:
                    try:
                        first_n = int(self.googler_video_first_n.get())
                    except (ValueError, TypeError):
                        first_n = 0
                    if first_n > 0:
                        self._select_first_n_images(first_n)
                self.run_googler_video(_skip_defer=True)
            except Exception as exc:
                self.log(
                    f"❌ Не удалось запустить отложенное видео: {exc}",
                    kind="images",
                )
    
    def _run_googler_images_worker(self):
        """Воркер генерации изображений Googler"""
        self._googler_images_busy = True
        try:
            trust_legacy_cache = bool(self._trust_legacy_image_cache_once)
            legacy_cache_snapshot = tuple(self._trust_legacy_image_cache_snapshot)
            self._trust_legacy_image_cache_once = False
            self._trust_legacy_image_cache_snapshot = ()
            if trust_legacy_cache:
                current_inventory = self._unverified_legacy_cache_inventory()
                if current_inventory != legacy_cache_snapshot:
                    self.log(
                        "❌ Legacy-кэш изменился после подтверждения; одноразовое "
                        "согласие отменено. Проверьте число кадров и запустите снова.",
                        kind="images",
                    )
                    return
            prompt_mode = str(self.image_prompt_mode.get() or "").strip().lower()
            if prompt_mode not in ("gpt", "custom", "existing"):
                self.log(
                    f"❌ Неизвестный режим промптов: {prompt_mode or '—'}",
                    kind="images",
                )
                return
            use_gpt = prompt_mode == "gpt"
            use_custom = prompt_mode == "custom"
            source_file = self.source_text_file.get()
            # Текстовый файл нужен только для режима GPT-промптов
            if use_gpt and (not source_file or not os.path.exists(source_file)):
                self.log("❌ Сначала выберите текстовый файл! (или включите «Кастомные промты»)", kind="images")
                return
            
            style = self.googler_style.get().strip()
            if not style:
                self.log("❌ Укажите стиль для генерации!", kind="images")
                return

            aspect_ratio = str(self.googler_aspect_ratio.get() or "").strip()
            custom_prompts_snapshot = (
                self._current_custom_prompt_snapshot() if use_custom else ()
            )
            seed_raw = str(self.googler_seed.get() or "").strip()
            try:
                seed_value = str(int(seed_raw)) if seed_raw else ""
            except ValueError:
                self.log(
                    f"⚠️ Неверный формат seed: {seed_raw}, будет использован авто seed",
                    kind="images",
                )
                seed_value = ""
            use_legacy_snapshot = bool(self.googler_use_legacy.get())
            split_long_snapshot = bool(
                getattr(self, 'googler_split_long_var', None)
                and self.googler_split_long_var.get()
            )
            split_long_seconds_snapshot = str(
                self.googler_split_long_seconds.get() or "6"
            ).strip()
            spp_raw = (
                str(self.googler_sentences_per_prompt.get() or "").strip()
                if use_gpt else ""
            )
            seconds_per_image_str = (
                str(self.googler_seconds_per_image.get() or "").strip()
                if use_gpt else ""
            )
            copy_similar_snapshot = CROSS_PROJECT_IMAGE_REUSE_ENABLED

            reference_preflight = self._reference_preflight(prompt_mode)
            if reference_preflight is None:
                return
            reference_manifest = reference_preflight["reference_manifest"]
            model_val = str(reference_preflight.get("model") or "").strip().upper()
            auto_hero_snapshot = bool(reference_preflight.get("auto_hero"))
            analyze_story_snapshot = bool(reference_preflight.get("analyze_story"))
            
            spp_extended = False  # расширенный синтаксис "1x40,3" (переменный шаг)
            if use_gpt:
                # Разделитель любой (x / х / × / * / - / пробел) — берём числа из строки
                _nums = re.findall(r'\d+', spp_raw)
                if len(_nums) >= 3:
                    # переменный шаг: первые N предложений по A на картинку, дальше по B
                    _a, _b, _t = int(_nums[0]), int(_nums[1]), int(_nums[2])
                    if _a <= 0 or _b <= 0 or _t <= 0:
                        self.log("❌ Неверный формат «предложений на промт». Примеры:  3  или  1x40,3", kind="images")
                        return
                    sentences_per_prompt = f"{_a}x{_b},{_t}"  # нормализуем к каноническому виду
                    spp_extended = True
                elif len(_nums) == 1:
                    sentences_per_prompt = int(_nums[0])
                    if sentences_per_prompt <= 0:
                        self.log("❌ Неверное количество предложений!", kind="images")
                        return
                else:
                    self.log("❌ Неверный формат «предложений на промт». Примеры:  3  или  1x40,3", kind="images")
                    return

            # N-секунд на кадр: если задано, пересчитываем sentences_per_prompt из длины аудио
            seconds_per_image = None
            if seconds_per_image_str:
                try:
                    seconds_per_image = float(seconds_per_image_str)
                    if seconds_per_image <= 0:
                        raise ValueError
                except ValueError:
                    self.log("⚠️ Неверный формат «сек/кадр», используем предложений на промт", kind="images")
                    seconds_per_image = None

            if use_gpt and seconds_per_image and spp_extended:
                self.log("ℹ️ Режим «сек/кадр» игнорируется — задан переменный шаг предложений (1xN,M)", kind="images")
            if use_gpt and seconds_per_image and not spp_extended:
                # Ищем аудиофайлы в проекте для замера длительности
                audio_duration = None
                SUPPORTED_AUDIO = ['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac']
                for root_d, dirs, files in os.walk(self.project_dir):
                    dirs[:] = [d for d in dirs if not d.startswith('.') and d != '_ASSETS' and 'temp' not in d.lower()]
                    for f in sorted(files):
                        ext = os.path.splitext(f)[1].lower()
                        if ext in SUPPORTED_AUDIO:
                            dur = self._get_audio_duration_ffprobe(os.path.join(root_d, f))
                            if dur and dur > 0:
                                audio_duration = (audio_duration or 0) + dur
                    break  # только верхний уровень (не рекурсивно — аудио обычно сверху)
                if audio_duration and audio_duration > 0:
                    target_images = max(1, int(audio_duration / seconds_per_image))
                    # Считаем предложения из текста
                    try:
                        with open(source_file, 'r', encoding='utf-8', errors='replace') as _f:
                            text_content = _f.read().strip()
                        import re as _re
                        num_sentences = len([s for s in _re.split(r'(?<=[.!?。！？])\s+', text_content) if s.strip()])
                        if num_sentences > 0 and target_images > 0:
                            sentences_per_prompt = max(1, num_sentences // target_images)
                            self.log(f"⏱️ N-секунд режим: аудио {audio_duration:.0f}с / {seconds_per_image}с = {target_images} кадров → {sentences_per_prompt} предл./промт", kind="images")
                    except Exception as e:
                        self.log(f"⚠️ Ошибка подсчёта предложений: {e}", kind="images")
                else:
                    self.log(f"⚠️ Аудио не найдено в проекте, используем предложений на промт ({sentences_per_prompt})", kind="images")

            worker_script = os.path.join(SCRIPTS_DIR, "googler_worker.py")
            if not check_worker_exists("googler_worker.py"):
                self.log(f"Не найден скрипт: googler_worker.py (или googler_worker.exe)", kind="images")
                return

            googler_dir = os.path.join(self.project_dir, "googler")
            prompts_file = os.path.join(googler_dir, "prompts.json")
            existing_prompts_snapshot = ()
            if prompt_mode == "existing":
                try:
                    existing_prompts_snapshot = self._read_existing_prompt_snapshot()
                except ReferenceContractError as exc:
                    self.log(f"❌ {exc}", kind="images")
                    return
            if use_custom and not custom_prompts_snapshot:
                self.log("❌ Введите кастомные промты! (один промт на абзац, между абзацами — пустая строка)", kind="images")
                return
            try:
                run_snapshot = self._capture_image_generation_snapshot(
                    reference_preflight,
                    prompt_mode=prompt_mode,
                    style=style,
                    aspect_ratio=aspect_ratio,
                    custom_prompts=custom_prompts_snapshot,
                    existing_prompts=existing_prompts_snapshot,
                    seed=seed_value,
                    use_legacy=use_legacy_snapshot,
                    split_long=split_long_snapshot,
                    split_long_seconds=split_long_seconds_snapshot,
                    sentences_per_prompt=spp_raw,
                    seconds_per_image=seconds_per_image_str,
                    copy_similar=copy_similar_snapshot,
                )
            except (ReferenceContractError, OSError, ValueError) as exc:
                self.log(
                    f"❌ BLOCKED: не удалось зафиксировать immutable snapshot запуска: {exc}",
                    kind="images",
                )
                return
            # From here on argv and post-success actions use only this run's
            # frozen identity, never live widgets that may change mid-run.
            reference_manifest = run_snapshot.reference_manifest
            model_val = run_snapshot.model
            style = run_snapshot.style
            aspect_ratio = run_snapshot.aspect_ratio
            custom_prompts_snapshot = run_snapshot.custom_prompts
            existing_prompts_snapshot = run_snapshot.existing_prompts
            seed_value = run_snapshot.seed
            use_legacy_snapshot = run_snapshot.use_legacy
            split_long_snapshot = run_snapshot.split_long
            split_long_seconds_snapshot = run_snapshot.split_long_seconds
            auto_hero_snapshot = run_snapshot.auto_hero
            analyze_story_snapshot = run_snapshot.analyze_story

            if use_gpt:
                # Полный пайплайн: GPT промты + генерация изображений
                self.log("\n🤖 Запуск генерации изображений через Googler AI...", kind="images")
                self.log(f"📝 Стиль: {style}", kind="images")
                self.log(f"📐 Соотношение сторон: {aspect_ratio}", kind="images")
                self.log(f"📊 Предложений на промт: {sentences_per_prompt}", kind="images")
                self.log(f"🤖 Модель: {GOOGLER_IMAGE_MODEL_LABEL_MAP.get(model_val, model_val)}", kind="images")
                self.log(f"🤖 GPT промты: Да", kind="images")
                
                worker_args = [
                    "pipeline",
                    "--source_file", source_file,
                    "--project_dir", self.project_dir,
                    "--style", style,
                    "--sentences_per_prompt", str(sentences_per_prompt),
                    "--aspect_ratio", aspect_ratio,
                    "--reference_manifest", reference_manifest,
                ]
                
                # Добавляем модель из immutable snapshot запуска
                if model_val:
                    worker_args.extend(["--model", model_val])
                
                # Добавляем флаг legacy API если включен
                if use_legacy_snapshot:
                    worker_args.append("--use_legacy")
                
                # Добавляем seed если указан
                if seed_value:
                    worker_args.extend(["--seed", seed_value])
                
                # Добавляем настройки параллельности из настроек приложения
                if hasattr(self.app_reference, 'googler_settings'):
                    image_threads = self.app_reference.googler_settings.get('max_parallel_images')
                    if image_threads:
                        worker_args.extend(["--image_threads", str(image_threads)])
                
                # НОВОЕ: Добавляем параметры авто-героя и референсов
                if auto_hero_snapshot:
                    worker_args.append("--auto_hero")
                # Анализ персонажей/локаций (story_bible + ref-aware промпты)
                if analyze_story_snapshot:
                    worker_args.append("--analyze_story")
                # Галка «дробить длинные предложения» + примерный лимит до Whisper
                if split_long_snapshot:
                    worker_args.append("--split_long_sentences")
                    worker_args.extend(["--max_solo_scene_seconds",
                                        split_long_seconds_snapshot or "6"])
                
                # Кэш картинок: пропускать уже сгенерированные NNN.jpg (раньше флаг тут НЕ
                # передавался → основной режим перегенерировал всё заново и жёг кредиты).
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                    if trust_legacy_cache:
                        worker_args.append("--trust_legacy_cache")
                self._images_stop_event.clear()
                used, code = run_worker_inprocess("googler_worker.py", worker_args, self.log, kind="images", stop_event=self._images_stop_event)
                if used:
                    if code == -1:
                        return
                    self.log("✅ Googler завершён!" if code == 0 else f"❌ Ошибка (код {code})", kind="images")
                    if code == 0:
                        self._finalize_googler_images_success(run_snapshot)
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                    return
            elif use_custom:
                custom_prompts_list = list(custom_prompts_snapshot)
                self.log("\n🤖 Генерация изображений по кастомным промтам...", kind="images")
                self.log(f"📝 Промтов: {len(custom_prompts_list)}", kind="images")
                self.log(f"📐 Соотношение сторон: {aspect_ratio}", kind="images")
                self.log(f"🤖 Модель: {GOOGLER_IMAGE_MODEL_LABEL_MAP.get(model_val, model_val)}", kind="images")
                worker_args = [
                    "images",
                    "--project_dir", self.project_dir,
                    "--aspect_ratio", aspect_ratio,
                    "--prompts_json", json.dumps(custom_prompts_list, ensure_ascii=False),
                    "--reference_manifest", reference_manifest,
                ]
                if model_val:
                    worker_args.extend(["--model", model_val])
                if style:
                    worker_args.extend(["--style", style])
                if seed_value:
                    worker_args.extend(["--seed", seed_value])
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                    if trust_legacy_cache:
                        worker_args.append("--trust_legacy_cache")
                self._images_stop_event.clear()
                used, code = run_worker_inprocess("googler_worker.py", worker_args, self.log, kind="images", stop_event=self._images_stop_event)
                if used:
                    if code == -1:
                        return
                    self.log("✅ Googler завершён!" if code == 0 else f"❌ Ошибка (код {code})", kind="images")
                    if code == 0:
                        self._finalize_googler_images_success(run_snapshot)
                    return
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                    return
            else:
                # Только генерация изображений из существующего prompts.json
                if not os.path.exists(prompts_file):
                    self.log("❌ Файл prompts.json не найден!", kind="images")
                    self.log("💡 Включите «Генерировать промты через GPT» или «Кастомные промты», либо создайте промты.", kind="images")
                    return
                
                self.log("\n🤖 Генерация изображений из существующих промтов...", kind="images")
                self.log(f"📂 Файл промтов: {prompts_file}", kind="images")
                self.log(f"📐 Соотношение сторон: {aspect_ratio}", kind="images")
                self.log(f"🤖 Модель: {GOOGLER_IMAGE_MODEL_LABEL_MAP.get(model_val, model_val)}", kind="images")
                
                worker_args = [
                    "images",
                    "--project_dir", self.project_dir,
                    "--aspect_ratio", aspect_ratio,
                    "--prompts_json", json.dumps(
                        list(existing_prompts_snapshot), ensure_ascii=False
                    ),
                    "--reference_manifest", reference_manifest,
                ]
                if model_val:
                    worker_args.extend(["--model", model_val])
                if style:
                    worker_args.extend(["--style", style])
                if seed_value:
                    worker_args.extend(["--seed", seed_value])
                # Добавляем --use_cache в worker_args (до формирования команды)
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                    if trust_legacy_cache:
                        worker_args.append("--trust_legacy_cache")
                
                # В режиме одного exe воркер встроен — запускаем в процессе
                self._images_stop_event.clear()
                used, code = run_worker_inprocess("googler_worker.py", worker_args, self.log, kind="images", stop_event=self._images_stop_event)
                if used:
                    if code == -1:
                        return
                    self.log("✅ Googler завершён!" if code == 0 else f"❌ Ошибка (код {code})", kind="images")
                    if code == 0:
                        self._finalize_googler_images_success(run_snapshot)
                    return
                
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                    return
            
            # Обновляем переменные окружения из API ключей перед запуском
            self.app_reference.api_keys_manager.update_env_vars()
            
            # Передаем переменную окружения DEBUG если включен режим отладки
            env = os.environ.copy()
            # Явно добавляем API ключи в переменные окружения для subprocess
            if 'googler' in self.app_reference.api_keys_manager.keys:
                env['GOOGLER_API_KEY'] = self.app_reference.api_keys_manager.keys['googler']
            if 'openai' in self.app_reference.api_keys_manager.keys:
                env['OPENAI_API_KEY'] = self.app_reference.api_keys_manager.keys['openai']
            try:
                from logger_config import is_debug_mode
                if is_debug_mode():
                    env['VIDEO_STUDIO_DEBUG'] = '1'
            except ImportError:
                pass
            startupinfo = get_subprocess_flags()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
            self.googler_proc_handle = process
            
            if process.stdout:
                for line in process.stdout:
                    self.log(line.rstrip(), kind="images")
            
            process.wait()
            self.googler_proc_handle = None
            
            if process.returncode == 0:
                self.log("✅ Генерация изображений завершена!", kind="images")
                self._finalize_googler_images_success(run_snapshot)
            else:
                self.log(f"❌ Ошибка (код {process.returncode})", kind="images")
                # Картинки упали — снимаем отложенные видео и рендер:
                # (1) иначе рендер ждёт _deferred_video_pending вечно (дедлок),
                # (2) не рендерим неполный/битый материал.
                if getattr(self, '_deferred_video_pending', False):
                    self._deferred_video_pending = False
                    self.log("⛔ Картинки упали — авто-видео отменено", kind="images")
                if getattr(self, '_deferred_render_pending', False):
                    self._deferred_render_pending = False
                    self.log("⛔ Картинки упали — авто-рендер отменён (материал неполный)", kind="render")

        except Exception as e:
            self.log(f"❌ Ошибка: {e}", kind="images")
        finally:
            self._googler_images_busy = False
    
    def _select_first_n_images(self, first_n):
        """Выбирает первые N картинок (по имени) из папок googler/Images в self.googler_selected_images.
        Возвращает количество реально выбранных. Используется авто-N и отложенным видео."""
        googler_dir = os.path.join(self.project_dir, "googler")
        images_dir = os.path.join(self.project_dir, IMAGE_DIR_NAME)
        all_images = []
        if os.path.isdir(googler_dir):
            try:
                for f in os.listdir(googler_dir):
                    if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        all_images.append(os.path.join(googler_dir, f))
            except Exception as e:
                self.log(f"⚠️ Ошибка при чтении папки googler: {e}", kind="images")
        if len(all_images) < first_n and os.path.isdir(images_dir):
            try:
                for f in os.listdir(images_dir):
                    if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        img_path = os.path.join(images_dir, f)
                        if img_path not in all_images:
                            all_images.append(img_path)
            except Exception as e:
                self.log(f"⚠️ Ошибка при чтении папки Images: {e}", kind="images")
        all_images.sort()  # первые по алфавиту/номеру
        if not all_images:
            return 0
        selected = all_images[:first_n]
        self.googler_selected_images = selected
        try:
            self.selected_images_label.config(text=f"Выбрано: {len(selected)} изображений (авто)", fg="#333")
        except Exception:
            pass
        return len(selected)

    def run_googler_video_auto_first_n(self):
        """Автоматически выбирает первые X картинок и запускает генерацию видео"""
        try:
            first_n = int(self.googler_video_first_n.get())
            if first_n <= 0:
                self.log("❌ Укажите количество картинок (больше 0)", kind="images")
                return
        except ValueError:
            self.log("❌ Неверное количество картинок", kind="images")
            return
        count = self._select_first_n_images(first_n)
        if count == 0:
            self.log("❌ Не найдено картинок для генерации видео", kind="images")
            return
        if count < first_n:
            self.log(f"⚠️ Найдено только {count} картинок, запрошено {first_n}", kind="images")
        self.log(f"📷 Автоматически выбрано {count} первых картинок для генерации видео", kind="images")
        self.run_googler_video()
    
    def run_googler_video(self, _skip_defer=False):
        """Запуск генерации видео через Googler (через отдельную очередь для видео).
        _skip_defer=True — пропустить проверку «после картинок». Нужно при вызове из хука
        завершения картинок: там image-job ещё числится в active_tts (мы внутри его callback'а),
        иначе видео откладывается ПОВТОРНО и зависает навсегда (у видео нет 5-сек поллинга)."""
        self._set_action_buttons_state('videos', 'disabled')
        # Если включён режим «После картинок» и ещё есть активные задачи Googler-изображений — откладываем запуск
        if not _skip_defer and self.googler_video_after_images.get() and getattr(self, 'googler_queue', None):
            try:
                prefix = self.project_name + "_images"
                has_active = False
                with self.googler_queue.lock:
                    if any(name == prefix for name in self.googler_queue.active_tts.keys()):
                        has_active = True
                    elif any(name == prefix for name, _ in self.googler_queue.queue):
                        has_active = True
                if has_active:
                    self._deferred_video_pending = True
                    self.log("⏳ Видео отложено — жду завершения генерации изображений для этого проекта", kind="images")
                    # Возвращаем кнопки в нормальное состояние, запуск произойдёт позже
                    self._set_action_buttons_state('videos', 'normal')
                    return
            except Exception:
                pass
        if self.googler_video_queue:
            success, message = self.googler_video_queue.add_to_queue(self.project_name + "_video", self._run_googler_video_worker)
            self.log(f"📊 {message}", kind="images")
            if not success:
                self._set_action_buttons_state('videos', 'normal')
                self.log(message, kind="images")
                return
        else:
            threading.Thread(target=lambda: self._videos_worker_with_reenable(self._run_googler_video_worker), daemon=True).start()
    
    def _run_googler_video_worker(self):
        """Воркер генерации видео Googler"""
        try:
            from_prompts = bool(getattr(self, 'googler_video_from_prompts', None) and self.googler_video_from_prompts.get())
            # По-прежнему требуем явный список изображений для видео (кроме режима t2v из промптов)
            if not self.googler_selected_images and not from_prompts:
                self.log("❌ Сначала выберите изображения!", kind="images")
                return

            prompt = self.googler_video_prompt.get().strip()
            if not prompt and not from_prompts:
                self.log("❌ Введите промт для видео!", kind="images")
                return

            loop = self.googler_video_loop.get()

            video_method = self.googler_video_method.get()
            method_labels = {"veo": "Veo NonStop (3.1)", "flower": "Flower (Veo 3.1)", "flow": "Flow (VEO)", "grok": "Grok"}
            self.log("\n🎬 Запуск генерации видео через Googler AI...", kind="images")
            if from_prompts:
                try:
                    _fn_log = int(self.googler_video_first_n.get().strip() or "0")
                except (ValueError, TypeError):
                    _fn_log = 0
                self.log(f"🎥 Режим: t2v ИЗ ПРОМПТОВ сцен ({'первые ' + str(_fn_log) if _fn_log > 0 else 'ВСЕ'} сцен)", kind="images")
            else:
                self.log(f"📷 Изображений: {len(self.googler_selected_images)}", kind="images")
                self.log(f"📝 Промт: {prompt[:100]}...", kind="images")
            self.log(f"🎬 Метод: {method_labels.get(video_method, video_method)}", kind="images")
            self.log(f"🔄 Зацикливание: {'Да' if loop else 'Нет'}", kind="images")
            
            worker_script = os.path.join(SCRIPTS_DIR, "googler_worker.py")
            
            # Если выбраны конкретные изображения — сохраняем их список во временный файл
            temp_file = None
            temp_file_path = None
            worker_args: List[str] = [
                "videos",
                "--project_dir", self.project_dir,
                "--prompt", prompt or "t2v",
            ]
            try:
                if from_prompts:
                    # t2v из промптов сцен: картинки не передаём, воркер читает scenes.json
                    worker_args.append("--from_prompts")
                    try:
                        _fn = int(self.googler_video_first_n.get().strip() or "0")
                    except (ValueError, TypeError):
                        _fn = 0
                    worker_args.extend(["--first_n_scenes", str(max(0, _fn))])
                    worker_args.extend(["--aspect_ratio", self.googler_aspect_ratio.get() or "landscape"])
                    _st = self.googler_style.get().strip()
                    if _st:
                        worker_args.extend(["--style", _st])
                    # для фолбэка «scenes.json ещё нет» — воркер сам сгенерит план из текста
                    _sf = self.source_text_file.get()
                    if _sf and os.path.isfile(_sf):
                        worker_args.extend(["--source_file", _sf])
                        worker_args.extend(["--sentences_per_prompt", str(self.googler_sentences_per_prompt.get().strip() or "3")])
                        if getattr(self, 'googler_split_long_var', None) and self.googler_split_long_var.get():
                            worker_args.append("--split_long_sentences")
                            worker_args.extend(["--max_solo_scene_seconds",
                                                self.googler_split_long_seconds.get().strip() or "6"])
                elif self.googler_selected_images:
                    temp_file = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', delete=False)
                    json.dump(self.googler_selected_images, temp_file, ensure_ascii=False)
                    temp_file.close()
                    temp_file_path = temp_file.name
                    worker_args.extend(["--image_paths_file", temp_file_path])
                # Если список не выбран, но чекбокс включён — не передаём image_paths_file,
                # googler_worker сам возьмёт изображения из папки googler/
                
                # Добавляем метод генерации видео
                if video_method:
                    worker_args.extend(["--video_method", video_method])
                
                # Добавляем флаг use_legacy если включен
                if self.googler_use_legacy.get():
                    worker_args.append("--use_legacy")
                
                # Добавляем настройки параллельности из настроек приложения
                if hasattr(self.app_reference, 'googler_settings'):
                    video_threads = self.app_reference.googler_settings.get('max_parallel_videos')
                    if video_threads:
                        worker_args.extend(["--video_threads", str(video_threads)])
                
                if loop:
                    worker_args.append("--loop")
                if self.use_cache.get():
                    worker_args.append("--use_cache")
                
                # В режиме одного exe воркер встроен — запускаем в процессе
                self._images_stop_event.clear()
                used, code = run_worker_inprocess("googler_worker.py", worker_args, self.log, kind="images", stop_event=self._images_stop_event)
                if used:
                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.unlink(temp_file_path)
                        except Exception:
                            pass
                    self.log("✅ Генерация видео завершена!" if code == 0 else f"❌ Ошибка (код {code})", kind="images")
                    return
                
                command = get_worker_command(worker_script, worker_args)
                if command is None or len(command) == 0:
                    self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
                    return
            except Exception as e:
                if temp_file is not None:
                    try:
                        temp_file.close()
                    except Exception:
                        pass
                if temp_file_path and os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                raise e
            
            # Обновляем переменные окружения из API ключей перед запуском
            self.app_reference.api_keys_manager.update_env_vars()
            
            # Передаем переменную окружения DEBUG если включен режим отладки
            env = os.environ.copy()
            # Явно добавляем API ключи в переменные окружения для subprocess
            if 'googler' in self.app_reference.api_keys_manager.keys:
                env['GOOGLER_API_KEY'] = self.app_reference.api_keys_manager.keys['googler']
            if 'openai' in self.app_reference.api_keys_manager.keys:
                env['OPENAI_API_KEY'] = self.app_reference.api_keys_manager.keys['openai']
            try:
                from logger_config import is_debug_mode
                if is_debug_mode():
                    env['VIDEO_STUDIO_DEBUG'] = '1'
            except ImportError:
                pass
            startupinfo = get_subprocess_flags()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
            self.googler_video_proc_handle = process

            if process.stdout:
                for line in process.stdout:
                    self.log(line.rstrip(), kind="images")

            process.wait()
            self.googler_video_proc_handle = None

            # Удаляем временный файл
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
            
            if process.returncode == 0:
                self.log("✅ Генерация видео завершена!", kind="images")
            else:
                self.log(f"❌ Ошибка (код {process.returncode})", kind="images")
                
        except Exception as e:
            # Удаляем временный файл при ошибке
            if 'temp_file_path' in locals() and temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
            self.log(f"❌ Ошибка: {e}", kind="images")
    
    def check_render_readiness(self):
        """Проверка готовности к рендеру. Возвращает (errors, warnings) — списки строк."""
        errors = []
        warnings = []
        project_dir_normalized = os.path.normpath(os.path.abspath(self.project_dir))
        assets_dir = os.path.normpath(os.path.join(self.project_dir, "_ASSETS"))
        SUPPORTED_AUDIO = ['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac']
        SUPPORTED_IMAGE = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.ico', '.gif']
        SUPPORTED_VIDEO = ['.mp4', '.mov', '.avi', '.mkv', '.webm']
        
        if not os.path.isdir(self.project_dir):
            errors.append(_('render_check_no_project_dir'))
            return errors, warnings
        
        audio_count = 0
        media_count = 0
        try:
            for root, dirs, files in os.walk(project_dir_normalized):
                rn = os.path.normpath(root)
                if rn == assets_dir:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if not d.startswith('.') and 'temp' not in d.lower() and d != '_ASSETS']
                for f in files:
                    if f.startswith('.') or 'temp' in f.lower() or 'final_video' in f.lower():
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    full = os.path.normpath(os.path.join(root, f))
                    if ext in SUPPORTED_AUDIO and os.path.isfile(full) and os.path.getsize(full) > 0:
                        skip = False
                        if self.background_audio_file.get() and os.path.exists(self.background_audio_file.get()):
                            if os.path.normpath(full) == os.path.normpath(self.background_audio_file.get()):
                                skip = True
                        if not skip and self.end_track_file.get() and os.path.exists(self.end_track_file.get()):
                            if os.path.normpath(full) == os.path.normpath(self.end_track_file.get()):
                                skip = True
                        if not skip:
                            audio_count += 1
                    if ext in SUPPORTED_IMAGE or ext in SUPPORTED_VIDEO:
                        if not full.startswith(assets_dir + os.sep) and rn != assets_dir:
                            media_count += 1
        except Exception as e:
            errors.append(_('render_check_scan_error').format(err=str(e)))
            return errors, warnings
        
        # Учитываем доп. папку материалов
        extra_dir = self.extra_materials_dir.get()
        if extra_dir and os.path.isdir(extra_dir):
            try:
                for root, dirs, files in os.walk(extra_dir):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for f in files:
                        if f.startswith('.'):
                            continue
                        ext = os.path.splitext(f)[1].lower()
                        if ext in SUPPORTED_IMAGE or ext in SUPPORTED_VIDEO:
                            media_count += 1
            except Exception:
                pass
        
        if audio_count == 0:
            errors.append(_('render_check_no_audio'))
        # Интро-видео считается медиа (можно рендерить только интро + аудио)
        valid_intro = [p for p in self.intro_video_files if p and os.path.isfile(p)]
        if valid_intro:
            media_count = max(media_count, 1)
        if media_count == 0:
            errors.append(_('render_check_no_media'))
        
        bg = self.background_audio_file.get()
        if bg and not os.path.isfile(bg):
            warnings.append(_('render_check_bg_missing'))
        for iv in self.intro_video_files:
            if iv and not os.path.isfile(iv):
                warnings.append(_('render_check_intro_missing'))
                break
        et = self.end_track_file.get()
        if et and not os.path.isfile(et):
            warnings.append(_('render_check_end_track_missing'))

        # Гейт полноты озвучки: если аудио сильно короче ожидаемого по тексту — БЛОКИРУЕМ рендер
        # (частичная/обрезанная озвучка → усечённое видео молча уходило в "готово").
        try:
            if audio_count > 0:
                comp = self.check_audio_completeness()
                if comp and comp.get("checked") and not comp.get("ok"):
                    errors.append(comp.get("msg") or "⚠️ Озвучка неполная — рендер остановлен.")
        except Exception as _e:
            print(f"[audio-gate] check skipped: {_e}")

        return errors, warnings
    
    def _has_active_processes(self):
        """Проверяет есть ли активные процессы (кроме рендера) для этого проекта.
        Использует точное совпадение имён с учётом суффиксов (_images, _video и т.д.)."""
        pn = self.project_name
        suffixes = ('', '_images', '_video', '_extract', '_transform', '_full')
        expected_names = {pn + s for s in suffixes}
        print(f"[DEBUG-RENDER] _has_active_processes called for project='{pn}' expected={sorted(expected_names)}")

        # Видео взведено («после картинок»), но ещё НЕ встало в очередь — считаем проект активным,
        # чтобы отложенный рендер не выстрелил в окно между «картинки готовы» и «видео в очереди».
        # Без этого рендер стартует сразу после картинок, не дождавшись генерации видео.
        if getattr(self, '_deferred_video_pending', False):
            print(f"[DEBUG-RENDER]   _deferred_video_pending=True for '{pn}' → returns True (ждём видео)")
            return True

        for qname, q in (('edgetts', self.edgetts_queue), ('elevenlabs', self.elevenlabs_queue), ('voicer', self.voicer_queue), ('vgc', self.voicegencore_queue)):
            if q:
                with q.lock:
                    active_list = list(q.active_tts)
                    queue_names = [n for n, _ in q.queue]
                    if pn in q.active_tts:
                        print(f"[DEBUG-RENDER]   tts[{qname}] active_tts={active_list} queue={queue_names} → MATCH active")
                        print(f"[DEBUG-RENDER]   returns: True (tts {qname} active)")
                        return True
                    if any(n == pn for n, _ in q.queue):
                        print(f"[DEBUG-RENDER]   tts[{qname}] active_tts={active_list} queue={queue_names} → MATCH queue")
                        print(f"[DEBUG-RENDER]   returns: True (tts {qname} queued)")
                        return True
                    if active_list or queue_names:
                        print(f"[DEBUG-RENDER]   tts[{qname}] active_tts={active_list} queue={queue_names} (no match for '{pn}')")
        if self.googler_queue:
            with self.googler_queue.lock:
                active_list = list(self.googler_queue.active_tts)
                queue_names = [n for n, _ in self.googler_queue.queue]
                if any(n in expected_names for n in self.googler_queue.active_tts):
                    print(f"[DEBUG-RENDER]   googler active={active_list} queue={queue_names} → MATCH active")
                    print(f"[DEBUG-RENDER]   returns: True (googler images active)")
                    return True
                if any(n in expected_names for n, _ in self.googler_queue.queue):
                    print(f"[DEBUG-RENDER]   googler active={active_list} queue={queue_names} → MATCH queue")
                    print(f"[DEBUG-RENDER]   returns: True (googler images queued)")
                    return True
                if active_list or queue_names:
                    print(f"[DEBUG-RENDER]   googler active={active_list} queue={queue_names} (no match)")
        if getattr(self, 'googler_video_queue', None):
            with self.googler_video_queue.lock:
                active_list = list(self.googler_video_queue.active_tts)
                queue_names = [n for n, _ in self.googler_video_queue.queue]
                if any(n in expected_names for n in self.googler_video_queue.active_tts):
                    print(f"[DEBUG-RENDER]   googler_video active={active_list} queue={queue_names} → MATCH active")
                    print(f"[DEBUG-RENDER]   returns: True (googler_video active)")
                    return True
                if any(n in expected_names for n, _ in self.googler_video_queue.queue):
                    print(f"[DEBUG-RENDER]   googler_video active={active_list} queue={queue_names} → MATCH queue")
                    print(f"[DEBUG-RENDER]   returns: True (googler_video queued)")
                    return True
                if active_list or queue_names:
                    print(f"[DEBUG-RENDER]   googler_video active={active_list} queue={queue_names} (no match)")
        if self.thief_queue:
            with self.thief_queue.lock:
                active_list = list(self.thief_queue.active_tts)
                queue_names = [n for n, _ in self.thief_queue.queue]
                if any(n in expected_names for n in self.thief_queue.active_tts):
                    print(f"[DEBUG-RENDER]   thief active={active_list} queue={queue_names} → MATCH active")
                    print(f"[DEBUG-RENDER]   returns: True (thief active)")
                    return True
                if any(n in expected_names for n, _ in self.thief_queue.queue):
                    print(f"[DEBUG-RENDER]   thief active={active_list} queue={queue_names} → MATCH queue")
                    print(f"[DEBUG-RENDER]   returns: True (thief queued)")
                    return True
                if active_list or queue_names:
                    print(f"[DEBUG-RENDER]   thief active={active_list} queue={queue_names} (no match)")
        if self.tts_proc_handle and self.tts_proc_handle.poll() is None:
            print(f"[DEBUG-RENDER]   tts_proc_handle alive (PID={self.tts_proc_handle.pid})")
            print(f"[DEBUG-RENDER]   returns: True (tts_proc_handle running)")
            return True
        if self.bing_proc_handle and self.bing_proc_handle.poll() is None:
            print(f"[DEBUG-RENDER]   bing_proc_handle alive (PID={self.bing_proc_handle.pid})")
            print(f"[DEBUG-RENDER]   returns: True (bing_proc_handle running)")
            return True
        if self.googler_proc_handle and self.googler_proc_handle.poll() is None:
            print(f"[DEBUG-RENDER]   googler_proc_handle alive (PID={self.googler_proc_handle.pid})")
            print(f"[DEBUG-RENDER]   returns: True (googler_proc_handle running)")
            return True
        if getattr(self, '_images_download_busy', False):
            print(f"[DEBUG-RENDER]   _images_download_busy=True")
            print(f"[DEBUG-RENDER]   returns: True (images_download busy)")
            return True
        print(f"[DEBUG-RENDER]   returns: False (all queues empty for '{pn}')")
        return False

    def run_video_render(self):
        """Добавление в очередь рендеринга."""
        _deferred_flag = bool(getattr(self, 'deferred_render', None) and self.deferred_render.get())
        print(f"[DEBUG-RENDER] run_video_render START project='{self.project_name}' deferred={_deferred_flag}")
        self._set_action_buttons_state('render', 'disabled')
        # Если включён отложенный рендер — сначала проверяем очереди/процессы; только потом ошибки и предупреждения
        if getattr(self, 'deferred_render', None) and self.deferred_render.get():
            active = self._has_active_processes()
            print(f"[DEBUG-RENDER]   deferred_render ON for '{self.project_name}', _has_active_processes={active}")
            if active:
                self._deferred_render_pending = True
                print(f"[DEBUG-RENDER]   → SET _deferred_render_pending=True for '{self.project_name}'")
                self.log("⏳ Рендер отложен — жду завершения озвучки/картинок/нарезки...", kind="render")
                self._set_action_buttons_state('render', 'normal')
                return
        print(f"[DEBUG-RENDER]   → starting render for '{self.project_name}'")
        errors, warnings = self.check_render_readiness()
        if errors:
            self._set_action_buttons_state('render', 'normal')
            self.log(_('render_check_errors') + "\n\n" + "\n".join(errors), kind="render")
            return
        if warnings:
            if not messagebox.askyesno(_('render_check_title'), _('render_check_warnings') + "\n\n" + "\n".join(warnings) + "\n\n" + _('render_check_run_anyway'), default="no"):
                self._set_action_buttons_state('render', 'normal')
                return
        success, message = self.render_queue.add_to_queue(self.project_name, self._run_video_render_worker)
        self.log(f"\n📊 {message}", kind="render")
        self.update_queue_status()
        
        if not success:
            self._set_action_buttons_state('render', 'normal')
            self.log(message, kind="render")
    
    def _run_video_render_worker(self):
        try:
            self.log("\n🎬 ЗАПУСК РЕНДЕРИНГА...", kind="render")
            self.log("="*80)
            self.update_queue_status()
            
            # Получаем настройки рендера из менеджера
            render_settings = self.app_reference.render_settings_manager.settings
            
            config = {
                'quality': self.video_quality.get(),
                'tts_engine': self.tts_engine.get(),
                'add_subtitles': self.add_subtitles_var.get(),
                'sync_audio': self.sync_audio_var.get(),
                'pause_duration': float(self.pause_duration_var.get() or 0.0),
                'pause_interval': int(self.pause_interval_var.get() or 0),
                'min_image_duration': float(self.googler_seconds_per_image.get() or self.min_image_duration_var.get() or 3.0),
                'max_image_duration': float(self.googler_seconds_per_image.get() or self.max_image_duration_var.get() or 5.0),
                'distribute_evenly': self.distribute_evenly_var.get(),
                'source_text_path': self.source_text_file.get(),
                'subtitle_language': self.subtitle_language_var.get(),
                'vintage_effect': self.vintage_effect_var.get(),
                'randomize_metadata': self.randomize_metadata_var.get(),
                'background_audio_file': self.background_audio_file.get(),
                'main_audio_volume': float(self.main_audio_volume_var.get() or 100.0) / 100.0,  # Конвертируем в 0.0-2.0
                'background_audio_volume': float(self.background_audio_volume_var.get() or 20.0) / 100.0,  # Конвертируем в 0.0-1.0
                # Интро-видео (список, всегда первыми)
                'intro_video_files': [p for p in self.intro_video_files if p and os.path.isfile(p)],
                # Трек в конце ролика
                'end_track_file': self.end_track_file.get(),
                'end_track_volume': float(self.end_track_volume_var.get() or 100.0) / 100.0,  # Конвертируем в 0.0-1.0
                # Переходы и эффекты
                'enable_fade': self.enable_fade_var.get(),
                'fade_duration': float(self.fade_duration_var.get() or 0.5),
                'enable_kenburns': self.enable_kenburns_var.get(),
                'use_video_audio': self.use_video_audio_var.get(),
                'kenburns_zoom': float(self.kenburns_zoom_var.get() or 1.1),
                'material_order': self._get_material_order_key(),
                'media_scale_pct': float(self.media_scale_pct_var.get() or 100),
                'media_offset_x': int(self.media_offset_x_var.get() or 0),
                'media_offset_y': int(self.media_offset_y_var.get() or 0),
                'media_per_item': self._get_media_per_item_for_render(),
                # Папка дополнительных материалов
                'extra_materials_dir': self.extra_materials_dir.get() if self.extra_materials_dir.get() else None,
                # Лимит стоковых видео (0=все, N=взять N, рандом+цикл)
                'stock_video_limit': int(self.stock_video_limit_var.get() or 0),
                'stock_video_cycle': True,
                # Оверлеи
                'overlays': [ov.copy() for ov in self.overlays] if self.overlays else [],
                # Настройки рендера для оптимизации
                'render_settings': render_settings
            }
            
            config_json = json.dumps(config, ensure_ascii=False)
            
            # Используем единый воркер
            worker_script = os.path.join(SCRIPTS_DIR, "render_worker.py")
            if not check_worker_exists("render_worker.py"):
                self.log(f"Не найден скрипт: render_worker.py (или render_worker.exe)", kind="render")
                return
            
            if self.vintage_effect_var.get():
                self.log("🎞️ Режим: VINTAGE (старая кинопленка с царапинами)", kind="render")
            
            if self.background_audio_file.get():
                self.log(f"🎵 Фоновое аудио: {os.path.basename(self.background_audio_file.get())}", kind="render")
            
            if self.intro_video_files:
                self.log(f"🎬 Интро: {len(self.intro_video_files)} файлов", kind="render")
            if self.end_track_file.get():
                self.log(f"🎵 Трек в конце: {os.path.basename(self.end_track_file.get())}", kind="render")
            
            worker_args = ["--project_dir", self.project_dir, "--config", config_json]
            # Рендер через subprocess — иначе при 2+ проектах они делят GIL и один зависает
            command = get_worker_command(worker_script, worker_args)
            if command is None or len(command) == 0:
                # Fallback: встроенный модуль (один exe) — параллельный рендер не будет работать
                used, code = run_worker_inprocess("render_worker.py", worker_args, self.log, kind="render")
                if used:
                    self.log("✅ Рендер завершён!" if code == 0 else f"❌ Ошибка (код {code})", kind="render")
                    self.update_queue_status()
                    return
                self.log(f"❌ Ошибка: не найден {worker_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="render")
                self.update_queue_status()
                return
            
            # Фильтруем None и преобразуем все в строки
            command = [str(arg) for arg in command if arg is not None]
            
            # Передаем переменную окружения DEBUG если включен режим отладки
            env = os.environ.copy()
            try:
                from logger_config import is_debug_mode
                if is_debug_mode():
                    env['VIDEO_STUDIO_DEBUG'] = '1'
            except ImportError:
                pass
            
            startupinfo = get_subprocess_flags()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, startupinfo=startupinfo, creationflags=get_subprocess_creationflags())
            self.render_proc_handle = process
            
            if process.stdout:
                for line in process.stdout:
                    line = line.rstrip()
                    if line.startswith("__RENDER_SUCCESS__|"):
                        output_file = line.split("|")[1]
                        self.log(f"\n✅ ГОТОВО: {output_file}", kind="render")
                        # Главы считаем ДО заливки: описание должно быть готово к моменту,
                        # когда воркер заливки заполняет поля.
                        if chapters is not None and self.auto_chapters_var.get():
                            self.log("📑 Главы включены → считаю таймкоды…", kind="render")
                            self.run_chapters()
                        if upload_manager is not None and self.auto_upload_var.get() and self.upload_channel_var.get().strip():
                            self.log("📤 Авто-залив включён → ставлю заливку…", kind="render")
                            self._start_upload(output_file)
                        break
                    elif line.startswith("__RENDER_ERROR__|"):
                        error_msg = line.split("|")[1]
                        self.log(f"\n❌ ОШИБКА: {error_msg}", kind="render")
                        break
                    else:
                        self.log(line, kind="render")

            process.wait()
            self.render_proc_handle = None
            if process.returncode != 0:
                self.log(f"\n❌ Ошибка (код {process.returncode})", kind="render")
            self.update_queue_status()
        except Exception as e:
            self.log(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}", kind="render")
            self.update_queue_status()

    # --- ОСТАНОВКА ПРОЦЕССОВ ---
    def _force_kill_process(self, process, process_name="процесс"):
        """Принудительно останавливает процесс с таймаутом"""
        if not process:
            return False
        
        try:
            if process.poll() is None:  # Процесс еще работает
                # Сначала пробуем мягко остановить
                process.terminate()
                
                # Ждем до 2 секунд
                try:
                    process.wait(timeout=2)
                    return True
                except subprocess.TimeoutExpired:
                    # Если не остановился - убиваем принудительно
                    if sys.platform == 'win32':
                        process.kill()
                    else:
                        process.kill()
                    
                    # Ждем еще немного
                    try:
                        process.wait(timeout=1)
                    except:
                        pass
                    return True
            return False
        except Exception as e:
            # Если процесс уже завершился или ошибка - это нормально
            return False
    
    def stop_tts(self):
        """Остановить TTS процесс и очистить очередь"""
        try:
            if hasattr(self, '_tts_stop_event'):
                self._tts_stop_event.set()
            # Останавливаем процесс
            if self._force_kill_process(self.tts_proc_handle, "TTS"):
                self.log("⏹️ TTS остановлен пользователем", kind="tts")
            self.tts_proc_handle = None
            
            # Очищаем очереди TTS
            if hasattr(self, 'elevenlabs_queue') and self.elevenlabs_queue:
                with self.elevenlabs_queue.lock:
                    # Удаляем из очереди
                    self.elevenlabs_queue.queue = [
                        (name, func) for name, func in self.elevenlabs_queue.queue 
                        if name != self.project_name
                    ]
                    # Останавливаем активные процессы
                    if self.project_name in self.elevenlabs_queue.active_tts:
                        thread = self.elevenlabs_queue.active_tts[self.project_name]
                        # Поток остановится сам, когда процесс завершится
                        del self.elevenlabs_queue.active_tts[self.project_name]
            
            if hasattr(self, 'edgetts_queue') and self.edgetts_queue:
                with self.edgetts_queue.lock:
                    self.edgetts_queue.queue = [
                        (name, func) for name, func in self.edgetts_queue.queue 
                        if name != self.project_name
                    ]
                    if self.project_name in self.edgetts_queue.active_tts:
                        del self.edgetts_queue.active_tts[self.project_name]
            if hasattr(self, 'voicer_queue') and self.voicer_queue:
                with self.voicer_queue.lock:
                    self.voicer_queue.queue = [
                        (name, func) for name, func in self.voicer_queue.queue 
                        if name != self.project_name
                    ]
                    if self.project_name in self.voicer_queue.active_tts:
                        del self.voicer_queue.active_tts[self.project_name]
            self._set_action_buttons_state('tts', 'normal')
        except Exception as e:
            self.log(f"⚠️ Не удалось остановить TTS: {e}", kind="tts")

    def stop_images(self):
        """Остановить процесс загрузки изображений и очистить очередь"""
        try:
            self._trust_legacy_image_cache_once = False
            self._trust_legacy_image_cache_snapshot = ()
            self.trust_legacy_image_cache.set(False)
            # Сигнализируем inprocess воркеру об остановке
            self._images_stop_event.set()
            
            # Останавливаем процесс
            if self._force_kill_process(self.bing_proc_handle, "загрузка изображений (Bing)"):
                self.log("⏹️ Загрузка изображений (Bing) остановлена", kind="images")
            self.bing_proc_handle = None
            if self._force_kill_process(self.googler_proc_handle, "загрузка изображений (Googler)"):
                self.log("⏹️ Загрузка изображений (Googler) остановлена", kind="images")
            self.googler_proc_handle = None
            if self._force_kill_process(self.googler_video_proc_handle, "генерация видео (Googler)"):
                self.log("⏹️ Генерация видео (Googler) остановлена", kind="images")
            self.googler_video_proc_handle = None

            # Очищаем очередь Googler (изображения)
            if hasattr(self, 'googler_queue') and self.googler_queue:
                with self.googler_queue.lock:
                    # Удаляем из очереди
                    self.googler_queue.queue = [
                        (name, func) for name, func in self.googler_queue.queue 
                        if not name.startswith(self.project_name)
                    ]
                    # Останавливаем активные процессы (используем active_tts, так как это TTSQueueManager)
                    for name in list(self.googler_queue.active_tts.keys()):
                        if name.startswith(self.project_name):
                            del self.googler_queue.active_tts[name]
            
            # Очищаем очередь Googler (видео)
            if hasattr(self, 'googler_video_queue') and self.googler_video_queue:
                with self.googler_video_queue.lock:
                    # Удаляем из очереди
                    self.googler_video_queue.queue = [
                        (name, func) for name, func in self.googler_video_queue.queue 
                        if not name.startswith(self.project_name)
                    ]
                    # Останавливаем активные процессы
                    for name in list(self.googler_video_queue.active_tts.keys()):
                        if name.startswith(self.project_name):
                            del self.googler_video_queue.active_tts[name]
            self._set_action_buttons_state('images', 'normal')
            self._set_action_buttons_state('videos', 'normal')
        except Exception as e:
            self.log(f"⚠️ Не удалось остановить загрузку: {e}", kind="images")

    def stop_render(self):
        """Остановить рендер процесс и очистить очередь"""
        try:
            # Удаляем из очереди (если ещё не стартовал)
            if hasattr(self, 'render_queue') and self.render_queue:
                self.render_queue.remove_from_queue(self.project_name)
            # Убиваем процесс
            if self._force_kill_process(self.render_proc_handle, "рендер"):
                self.log("⏹️ Рендер остановлен пользователем", kind="render")
            self.render_proc_handle = None
            # Удаляем из active + стартуем следующий из очереди (Fix 5 May 2026)
            if hasattr(self, 'render_queue') and self.render_queue:
                self.render_queue.notify_stopped(self.project_name)
            self._set_action_buttons_state('render', 'normal')
        except Exception as e:
            self.log(f"⚠️ Не удалось остановить рендер: {e}", kind="render")
    
    # --- ВОР: МЕТОДЫ ---
    def paste_youtube_url(self):
        """Вставить URL из буфера обмена"""
        try:
            clipboard = self.clipboard_get()
            if clipboard and ('youtube.com' in clipboard or 'youtu.be' in clipboard):
                self.thief_youtube_url.set(clipboard)
                self.log("📋 URL вставлен из буфера обмена", kind="thief")
            else:
                self.log("⚠️ В буфере обмена нет YouTube URL", kind="thief")
        except Exception as e:
            self.log(f"⚠️ Ошибка вставки: {e}", kind="thief")
    
    def stop_thief(self):
        """Остановить процесс извлечения/трансформации и очистить очередь"""
        try:
            # Сигнализируем inprocess воркеру об остановке
            self._thief_stop_event.set()
            
            # Останавливаем процесс
            if self._force_kill_process(self.thief_proc_handle, "извлечение слайдов"):
                self.log("⏹️ Процесс остановлен пользователем", kind="thief")
            self.thief_proc_handle = None
            
            # Очищаем очередь Thief
            if hasattr(self, 'thief_queue') and self.thief_queue:
                with self.thief_queue.lock:
                    # Удаляем из очереди
                    self.thief_queue.queue = [
                        (name, func) for name, func in self.thief_queue.queue 
                        if not name.startswith(self.project_name)
                    ]
                    # Останавливаем активные процессы (используем active_tts, так как это TTSQueueManager)
                    for name in list(self.thief_queue.active_tts.keys()):
                        if name.startswith(self.project_name):
                            del self.thief_queue.active_tts[name]
            self._set_action_buttons_state('thief', 'normal')
        except Exception as e:
            self.log(f"⚠️ Не удалось остановить процесс: {e}", kind="thief")
    
    def _copy_slides_to_projects(self):
        """Копирование извлечённых слайдов в другие проекты."""
        slides_dir = os.path.normpath(os.path.join(BASE_SLIDES_DIR, self.project_name))
        if not os.path.exists(slides_dir) or not os.listdir(slides_dir):
            self.log("Нет извлечённых слайдов для копирования", kind="thief")
            return
        
        other_projects = [name for name in self.app_reference.projects.keys()
                         if name != self.project_name] if hasattr(self, 'app_reference') else []
        if not other_projects:
            self.log("Нет других открытых проектов", kind="thief")
            return
        
        dialog = Toplevel(self.root)
        dialog.title("Копировать исходники в проекты")
        dialog.geometry("400x350")
        dialog.resizable(False, True)
        dialog.transient(self.root)
        dialog.grab_set()
        
        Label(dialog, text=f"Исходники из: {self.project_name}", font=("Arial", 10, "bold")).pack(padx=10, pady=(10, 5))
        Label(dialog, text="Выберите проекты для копирования:", font=("Arial", 9)).pack(padx=10, pady=(0, 5))
        
        checks_frame = Frame(dialog)
        checks_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        check_vars = {}
        for proj_name in other_projects:
            var = BooleanVar(value=False)
            check_vars[proj_name] = var
            Checkbutton(checks_frame, text=proj_name, variable=var, font=("Arial", 10)).pack(anchor="w", pady=2)
        
        def do_copy():
            selected = [name for name, var in check_vars.items() if var.get()]
            if not selected:
                self.log("Выберите хотя бы один проект", kind="thief")
                return
            copied = 0
            for target_name in selected:
                try:
                    target_slides_dir = os.path.normpath(os.path.join(BASE_SLIDES_DIR, target_name))
                    os.makedirs(target_slides_dir, exist_ok=True)
                    for f in os.listdir(slides_dir):
                        src = os.path.join(slides_dir, f)
                        dst = os.path.join(target_slides_dir, f)
                        if os.path.isfile(src):
                            shutil.copy2(src, dst)
                    target_images_dir = os.path.normpath(os.path.join(BASE_DIR_ROOT, target_name, "Images"))
                    os.makedirs(target_images_dir, exist_ok=True)
                    for f in os.listdir(slides_dir):
                        src = os.path.join(slides_dir, f)
                        dst = os.path.join(target_images_dir, f)
                        if os.path.isfile(src) and f.lower().endswith(('.png', '.jpg', '.jpeg')):
                            shutil.copy2(src, dst)
                    copied += 1
                    self.log(f"✅ Скопировано в {target_name}", kind="thief")
                except Exception as e:
                    self.log(f"❌ Ошибка копирования в {target_name}: {e}", kind="thief")
            self.log(f"Скопировано в {copied} проект(ов)", kind="thief")
            dialog.destroy()
        
        btn_frame = Frame(dialog)
        btn_frame.pack(fill="x", padx=10, pady=10)
        Button(btn_frame, text="📋 Копировать", command=do_copy, bg="#4CAF50", fg="white",
               font=("Arial", 10, "bold")).pack(side="left", padx=5)
        Button(btn_frame, text="Отмена", command=dialog.destroy, bg="#757575", fg="white").pack(side="left", padx=5)
        
        dialog.wait_window()

    def run_thief_extract(self):
        """Извлечь слайды из YouTube видео (через очередь)"""
        url = self.thief_youtube_url.get().strip()
        if not url:
            self.log("⚠️ Введите YouTube URL!", kind="thief")
            return

        # Дедупликация: если похожий проект уже извлекает с тем же URL — не запускать повторно
        if getattr(self, 'thief_copy_similar', None) and self.thief_copy_similar.get():
            for other_name, other_frame in self.app_reference.projects.items():
                if other_name == self.project_name:
                    continue
                sim = _project_name_similarity(self.project_name, other_name)
                if sim >= 0.85:
                    check_name = other_name + "_extract"
                    if self.thief_queue and check_name in self.thief_queue.active_tts:
                        self.log(f"⏩ Нарезка уже идёт в '{other_name}', жду результат...", kind="thief")
                        return
        
        if self.thief_queue:
            success, message = self.thief_queue.add_to_queue(self.project_name + "_extract", self._run_thief_extract_worker)
            self.log(f"📊 {message}", kind="thief")
        else:
            threading.Thread(target=self._run_thief_extract_worker, daemon=True).start()
    
    def _run_thief_extract_worker(self):
        """Воркер извлечения слайдов"""
        url = self.thief_youtube_url.get().strip()
        
        # НОВОЕ: Папка для слайдов в отдельной директории
        # Нормализуем путь для корректной работы с русскими символами
        slides_dir = os.path.normpath(os.path.join(BASE_SLIDES_DIR, self.project_name))
        os.makedirs(slides_dir, exist_ok=True)
        
        # Параметры
        sensitivity = self.thief_sensitivity.get()
        min_duration = self.thief_min_duration.get()
        sample_interval = self.thief_sample_interval.get()
        strict = self.thief_strict.get()
        valid_qualities = ("best", "1080p", "720p", "480p", "360p", "fastest")
        source_quality = (self.thief_source_quality.get() or "best").strip().lower()
        if source_quality not in valid_qualities:
            source_quality = "best"
        
        # Проверяем наличие скрипта
        extractor_script = os.path.join(SCRIPTS_DIR, "youtube_slide_extractor.py")
        if not check_worker_exists("youtube_slide_extractor.py"):
            self.log(f"Не найден скрипт: youtube_slide_extractor.py (или youtube_slide_extractor.exe)", kind="thief")
            return
        
        self.log(f"🎬 Извлечение слайдов из: {url}", kind="thief")
        self.log(f"📁 Папка: {slides_dir}", kind="thief")
        self.log(f"⚙️ Sensitivity: {sensitivity}, Min duration: {min_duration}, Interval: {sample_interval}, Quality: {source_quality}, Strict: {strict}", kind="thief")
        
        try:
            abs_slides_dir = os.path.abspath(os.path.normpath(slides_dir))
            worker_args = [
                url,
                "--output", abs_slides_dir,
                "--sensitivity", str(sensitivity),
                "--min-duration", str(min_duration),
                "--sample-interval", str(sample_interval),
                "--source-quality", source_quality
            ]
            if strict:
                worker_args.append("--strict")
            used, code = run_worker_inprocess("youtube_slide_extractor.py", worker_args, self.log, kind="thief")
            if used:
                self.log("✅ Извлечение слайдов завершено!" if code == 0 else f"❌ Ошибка (код {code})", kind="thief")
                if code == 0 and getattr(self, 'thief_copy_similar', None) and self.thief_copy_similar.get():
                    self._copy_slides_to_similar_projects(slides_dir)
                return
            cmd = get_worker_command(extractor_script, worker_args)
            if cmd is None or len(cmd) == 0:
                self.log(f"❌ Ошибка: не найден {extractor_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="thief")
                return
            startupinfo = get_subprocess_flags()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                startupinfo=startupinfo,
                creationflags=get_subprocess_creationflags()
            )
            self.thief_proc_handle = process
            
            if process.stdout:
                # Читаем вывод в реальном времени с немедленным выводом
                while True:
                    line = process.stdout.readline()
                    if not line:
                        if process.poll() is not None:
                            break
                        continue
                    line = line.rstrip()
                    if line:
                        self.log(line, kind="thief")
            
            process.wait()
            self.thief_proc_handle = None
            
            if process.returncode == 0:
                # Подсчитываем слайды
                if os.path.exists(slides_dir):
                    slides = [f for f in os.listdir(slides_dir) if f.endswith('.png')]
                    self.log(f"\n✅ Извлечено {len(slides)} слайдов", kind="thief")
                    if getattr(self, 'thief_copy_similar', None) and self.thief_copy_similar.get():
                        self._copy_slides_to_similar_projects(slides_dir)
                else:
                    self.log("\n⚠️ Папка со слайдами не создана", kind="thief")
            else:
                self.log(f"\n❌ Ошибка извлечения (код {process.returncode})", kind="thief")
                
        except Exception as e:
            self.log(f"\n❌ ОШИБКА: {e}", kind="thief")
    
    def _copy_slides_to_similar_projects(self, slides_dir):
        """Копирует слайды в проекты с похожим названием (85%+)."""
        if not os.path.exists(slides_dir) or not os.listdir(slides_dir):
            return
        my_name = self.project_name
        for other_name, other_frame in self.app_reference.projects.items():
            if other_name == my_name:
                continue
            sim = _project_name_similarity(my_name, other_name)
            if sim >= 0.85:
                self.log(f"📋 Копирую слайды в '{other_name}' (совпадение {sim:.0%})", kind="thief")
                try:
                    target_slides = os.path.normpath(os.path.join(BASE_SLIDES_DIR, other_name))
                    os.makedirs(target_slides, exist_ok=True)
                    for f in os.listdir(slides_dir):
                        src = os.path.join(slides_dir, f)
                        if os.path.isfile(src):
                            shutil.copy2(src, os.path.join(target_slides, f))
                    target_images = os.path.normpath(os.path.join(other_frame.project_dir, "Images"))
                    os.makedirs(target_images, exist_ok=True)
                    for f in os.listdir(slides_dir):
                        src = os.path.join(slides_dir, f)
                        if os.path.isfile(src) and f.lower().endswith(('.png', '.jpg', '.jpeg')):
                            shutil.copy2(src, os.path.join(target_images, f))
                except Exception as e:
                    self.log(f"⚠️ Ошибка копирования в '{other_name}': {e}", kind="thief")

    def _content_key(self):
        """Ключ контента для группировки — имя ТЕКСТОВОГО файла проекта (без расширения).
        Языковой префикс снимает _project_name_similarity. Фолбэк — имя проекта (если текст не выбран)."""
        try:
            p = (self.source_text_file.get() or "").strip()
        except Exception:
            p = ""
        if p:
            return os.path.splitext(os.path.basename(p))[0]
        return self.project_name

    def _image_reference_reuse_status(self):
        """Return whether images can cross project boundaries, with a UI reason.

        Reuse is deliberately limited to the no-reference continuity contract.
        Both the live UI state and the project-owned passport are checked so a
        stale frame cannot silently copy images generated for another identity.
        """
        blocker = getattr(self, '_reference_copy_blocker', None)
        if blocker:
            return False, f"не завершено безопасное копирование референсов: {blocker}"

        state_issues = getattr(self, '_reference_state_issues', ()) or ()
        if isinstance(state_issues, str):
            state_issues = (state_issues,)
        if state_issues:
            return False, f"паспорт референсов помечен как повреждённый: {state_issues[0]}"

        if getattr(self, 'user_references', None):
            return False, "настроены фото персонажей"
        if getattr(self, 'style_references', None):
            return False, "настроены стиль-референсы"

        for attr_name, label in (
            ('auto_hero_var', 'авто-герой'),
            ('analyze_story_var', 'разбор персонажей и мест'),
        ):
            variable = getattr(self, attr_name, None)
            if variable is None:
                continue
            try:
                enabled = bool(variable.get())
            except Exception as exc:
                return False, f"не удалось проверить режим «{label}»: {exc}"
            if enabled:
                return False, f"включён режим «{label}»"

        try:
            saved_manual, saved_style, disk_issues = load_project_reference_state(
                self.project_dir
            )
        except (ReferenceContractError, OSError, ValueError) as exc:
            return False, f"паспорт референсов повреждён: {exc}"
        if disk_issues:
            return False, f"паспорт референсов не прошёл проверку: {disk_issues[0]}"
        if saved_manual:
            return False, "в project-owned паспорте есть фото персонажей"
        if saved_style:
            return False, "в project-owned паспорте есть стиль-референсы"
        return True, "референсы и continuity-режимы не используются"

    def _image_reference_reuse_safe(self):
        """Fail-closed boolean used by grouping and preset identity."""
        safe, _reason = self._image_reference_reuse_status()
        return safe

    def _apply_cross_project_image_reuse_setting(self, saved_value):
        """Migrate the removed preset option visibly and keep it disabled."""
        requested = bool(saved_value)
        self.googler_copy_similar.set(False)
        if requested and not getattr(
            self, '_cross_project_reuse_notice_logged', False
        ):
            self._cross_project_reuse_notice_logged = True
            self.log(
                "🛡️ Старый пресет запрашивал копирование картинок "
                "между проектами. BLOCKED: "
                f"{CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON}. "
                "Каждый проект будет генерировать свои кадры.",
                kind="images",
            )
        return False

    def _image_reuse_target_status(self):
        """Return whether this project is an empty, idle reuse destination."""
        continuity_safe, continuity_reason = self._image_reference_reuse_status()
        if not continuity_safe:
            return False, continuity_reason

        if getattr(self, '_googler_images_busy', False):
            return False, "в проекте идёт мутация Googler-кадров"
        if getattr(self, '_images_download_busy', False):
            return False, "в проекте идёт мутация изображений"
        for handle_name in ('googler_proc_handle', 'bing_proc_handle', 'thief_proc_handle'):
            handle = getattr(self, handle_name, None)
            if handle is None:
                continue
            try:
                if handle.poll() is None:
                    return False, f"активен image-процесс {handle_name}"
            except Exception as exc:
                return False, f"не удалось проверить {handle_name}: {exc}"

        project_name = str(getattr(self, 'project_name', '') or '')
        queue_checks = (
            (getattr(self, 'googler_queue', None), {project_name + "_images"}),
            (getattr(self, 'thief_queue', None), {
                project_name + "_transform", project_name + "_full",
            }),
        )
        for queue_manager, task_names in queue_checks:
            if queue_manager is None:
                continue
            try:
                with queue_manager.lock:
                    active_names = set(queue_manager.active_tts)
                    queued_names = {name for name, _callback in queue_manager.queue}
            except Exception as exc:
                return False, f"не удалось проверить image-очередь: {exc}"
            if task_names & (active_names | queued_names):
                return False, "для проекта уже активна image-мутация"

        googler_dir = os.path.join(self.project_dir, "googler")
        if os.path.lexists(googler_dir):
            if os.path.islink(googler_dir) or not os.path.isdir(googler_dir):
                return False, "папка googler повреждена или небезопасна"
            image_extensions = {
                '.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp',
                '.tif', '.tiff', '.avif', '.heic', '.heif',
            }
            try:
                with os.scandir(googler_dir) as entries:
                    for entry in entries:
                        stem, extension = os.path.splitext(entry.name)
                        output_name = stem.isdigit() or extension.lower() in image_extensions
                        active_stage = entry.name.startswith(".reuse-stage-")
                        if not output_name and not active_stage:
                            continue
                        if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                            return False, f"целевая папка уже содержит {entry.name}"
                        if active_stage:
                            return False, "в целевой папке остался незавершённый reuse-stage"
            except OSError as exc:
                return False, f"целевая папка googler не прочитана: {exc}"
        return True, "целевой проект пуст и не изменяется"

    def _current_custom_prompt_snapshot(self):
        try:
            raw = self.googler_custom_prompts_text.get("1.0", "end").strip()
        except Exception:
            raw = ""
        return tuple(
            block.strip()
            for block in re.split(r'\n\s*\n', raw)
            if block.strip()
        )

    def _read_existing_prompt_snapshot(self):
        """Read the exact prompt list that the images worker would consume."""
        prompts_path = os.path.join(self.project_dir, "googler", "prompts.json")
        try:
            if os.path.getsize(prompts_path) > 16 * 1024 * 1024:
                raise ReferenceContractError("prompts.json слишком большой")
            with open(prompts_path, "r", encoding="utf-8", errors="strict") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReferenceContractError(f"prompts.json не прочитан: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("prompts"), list):
            raise ReferenceContractError("prompts.json имеет неверную структуру")
        prompts = tuple(
            str(row.get("prompt") or "").strip()
            for row in payload["prompts"]
            if isinstance(row, dict) and str(row.get("prompt") or "").strip()
        )
        if not prompts:
            raise ReferenceContractError("prompts.json не содержит промтов")
        return prompts

    def _image_preset_key_from_values(
            self, *, style, model, aspect_ratio, prompt_mode,
            custom_prompts=(), existing_prompts=(), seed="", use_legacy=False,
            split_long=False, split_long_seconds="", sentences_per_prompt="",
            seconds_per_image="", reference_reuse_allowed=False):
        """Canonical identity for every setting that can change generated frames."""
        mode = str(prompt_mode or "").strip().lower()
        reference_identity = (
            ("reference-reuse-safe",)
            if reference_reuse_allowed
            else ("reference-reuse-blocked", os.path.realpath(self.project_dir))
        )
        return (
            "image-preset-v2",
            str(style or "").strip(),
            str(model or "").strip().upper(),
            str(aspect_ratio or "").strip(),
            mode,
            tuple(custom_prompts) if mode == "custom" else (),
            tuple(existing_prompts) if mode == "existing" else (),
            str(seed or "").strip(),
            bool(use_legacy) if mode == "gpt" else False,
            bool(split_long) if mode == "gpt" else False,
            str(split_long_seconds or "").strip() if mode == "gpt" and split_long else "",
            str(sentences_per_prompt or "").strip() if mode == "gpt" else "",
            str(seconds_per_image or "").strip() if mode == "gpt" else "",
            reference_identity,
        )

    def _capture_image_generation_snapshot(
            self, reference_preflight, *, prompt_mode, style, aspect_ratio,
            custom_prompts=(), existing_prompts=(), seed="", use_legacy=False,
            split_long=False, split_long_seconds="", sentences_per_prompt="",
            seconds_per_image="", copy_similar=False):
        """Freeze refs, continuity flags and preset identity used by one run."""
        manifest_path = os.path.realpath(
            str(reference_preflight.get("reference_manifest") or "")
        )
        try:
            if not manifest_path or os.path.islink(manifest_path):
                raise ReferenceContractError("путь immutable manifest неверен")
            if os.path.getsize(manifest_path) > 16 * 1024 * 1024:
                raise ReferenceContractError("immutable manifest слишком большой")
            with open(manifest_path, "rb") as handle:
                manifest_bytes = handle.read(16 * 1024 * 1024 + 1)
            manifest_payload = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReferenceContractError(f"immutable manifest не прочитан: {exc}") from exc
        expected_keys = {
            "schema_version", "bundle_id", "characters", "style",
            "source_fingerprint",
        }
        if (
            not isinstance(manifest_payload, dict)
            or set(manifest_payload) != expected_keys
            or manifest_payload.get("schema_version") != 1
            or not isinstance(manifest_payload.get("characters"), list)
            or not isinstance(manifest_payload.get("style"), list)
        ):
            raise ReferenceContractError("immutable manifest имеет неверную структуру")
        bundle_id = manifest_payload.get("bundle_id")
        source_fingerprint = manifest_payload.get("source_fingerprint")
        if (
            not isinstance(bundle_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", bundle_id) is None
            or os.path.basename(os.path.dirname(manifest_path)) != bundle_id
            or not isinstance(source_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_fingerprint) is None
        ):
            raise ReferenceContractError("immutable manifest не связан с bundle identity")

        auto_hero = bool(reference_preflight.get("auto_hero"))
        analyze_story = bool(reference_preflight.get("analyze_story"))
        state_issues = getattr(self, '_reference_state_issues', ()) or ()
        if isinstance(state_issues, str):
            state_issues = (state_issues,)
        blocker = getattr(self, '_reference_copy_blocker', None)
        reasons = []
        if manifest_payload["characters"]:
            reasons.append("в manifest есть фото персонажей")
        if manifest_payload["style"]:
            reasons.append("в manifest есть стиль-референсы")
        if auto_hero:
            reasons.append("на старте был включён авто-герой")
        if analyze_story:
            reasons.append("на старте был включён разбор истории")
        if blocker:
            reasons.append(str(blocker))
        reasons.extend(str(issue) for issue in state_issues if issue)
        reuse_allowed = not reasons
        model = str(reference_preflight.get("model") or "").strip().upper()
        source_preset_key = self._image_preset_key_from_values(
            style=style,
            model=model,
            aspect_ratio=aspect_ratio,
            prompt_mode=prompt_mode,
            custom_prompts=custom_prompts,
            existing_prompts=existing_prompts,
            seed=seed,
            use_legacy=use_legacy,
            split_long=split_long,
            split_long_seconds=split_long_seconds,
            sentences_per_prompt=sentences_per_prompt,
            seconds_per_image=seconds_per_image,
            reference_reuse_allowed=reuse_allowed,
        )
        return ImageGenerationSnapshot(
            prompt_mode=str(prompt_mode or "").strip().lower(),
            style=str(style or "").strip(),
            model=model,
            aspect_ratio=str(aspect_ratio or "").strip(),
            custom_prompts=tuple(custom_prompts),
            existing_prompts=tuple(existing_prompts),
            seed=str(seed or "").strip(),
            use_legacy=bool(use_legacy),
            split_long=bool(split_long),
            split_long_seconds=str(split_long_seconds or "").strip(),
            sentences_per_prompt=str(sentences_per_prompt or "").strip(),
            seconds_per_image=str(seconds_per_image or "").strip(),
            auto_hero=auto_hero,
            analyze_story=analyze_story,
            copy_similar=bool(copy_similar),
            reference_manifest=manifest_path,
            reference_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            reference_bundle_id=bundle_id,
            reference_source_fingerprint=source_fingerprint,
            reference_reuse_allowed=reuse_allowed,
            reference_reuse_reason=("; ".join(reasons) if reasons else "clean no-reference run"),
            source_preset_key=source_preset_key,
        )

    def _image_generation_snapshot_intact(self, snapshot):
        if not isinstance(snapshot, ImageGenerationSnapshot):
            return False, "нет проверенного snapshot запуска"
        try:
            if os.path.islink(snapshot.reference_manifest):
                raise OSError("manifest стал symlink")
            if os.path.getsize(snapshot.reference_manifest) > 16 * 1024 * 1024:
                raise OSError("manifest слишком большой")
            with open(snapshot.reference_manifest, "rb") as handle:
                current_bytes = handle.read(16 * 1024 * 1024 + 1)
        except OSError as exc:
            return False, f"immutable manifest после старта недоступен: {exc}"
        if hashlib.sha256(current_bytes).hexdigest() != snapshot.reference_manifest_sha256:
            return False, "immutable manifest изменился после старта"
        return True, "snapshot manifest не изменился"

    def _image_preset_key(self):
        """Ключ ПРЕСЕТА изображений: картинки переиспользуемы между проектами только при
        совпадении пресета (стиль/модель/ratio/режим промтов/кастом-промты). Иначе клон с
        ДРУГИМ пресетом получил бы картинки чужого стиля. Используется в группировке и копировании."""
        def value(name, default=""):
            try:
                return getattr(self, name).get()
            except Exception:
                return default

        prompt_mode = str(value('image_prompt_mode', 'gpt') or '').strip().lower()
        custom_prompts = self._current_custom_prompt_snapshot() if prompt_mode == "custom" else ()
        existing_prompts = ()
        if prompt_mode == "existing":
            try:
                existing_prompts = self._read_existing_prompt_snapshot()
            except ReferenceContractError as exc:
                existing_prompts = (("blocked", os.path.realpath(self.project_dir), str(exc)),)
        seed_raw = str(value('googler_seed') or '').strip()
        try:
            seed = str(int(seed_raw)) if seed_raw else ""
        except ValueError:
            seed = ""
        return self._image_preset_key_from_values(
            style=value('googler_style'),
            model=value('googler_model'),
            aspect_ratio=value('googler_aspect_ratio'),
            prompt_mode=prompt_mode,
            custom_prompts=custom_prompts,
            existing_prompts=existing_prompts,
            seed=seed,
            use_legacy=bool(value('googler_use_legacy', False)),
            split_long=bool(value('googler_split_long_var', False)),
            split_long_seconds=value('googler_split_long_seconds'),
            sentences_per_prompt=value('googler_sentences_per_prompt'),
            seconds_per_image=value('googler_seconds_per_image'),
            reference_reuse_allowed=self._image_reference_reuse_safe(),
        )

    def _validated_reusable_image_outputs(self, googler_dir):
        """Return the exact receipt-authorized ``(filename, sha256)`` outputs.

        The directory is intentionally never scanned for image extensions:
        files outside the contiguous schema-v2 receipt set belong to another
        run and are not eligible for cross-project reuse.
        """
        if not os.path.isdir(googler_dir) or os.path.islink(googler_dir):
            raise ReferenceContractError("googler output directory is missing or unsafe")
        receipt_path = os.path.join(googler_dir, "image_receipts.v1.json")
        if os.path.islink(receipt_path) or not os.path.isfile(receipt_path):
            raise ReferenceContractError("image receipt is missing or not a regular file")
        try:
            if os.path.getsize(receipt_path) > 16 * 1024 * 1024:
                raise ReferenceContractError("image receipt exceeds the size limit")

            def reject_duplicate_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate receipt key: {key}")
                    result[key] = value
                return result

            with open(receipt_path, "r", encoding="utf-8", errors="strict") as handle:
                receipt = json.load(handle, object_pairs_hook=reject_duplicate_keys)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ReferenceContractError(f"image receipt is unreadable: {exc}") from exc
        frames = receipt.get("frames") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 2
            or not isinstance(frames, dict)
        ):
            raise ReferenceContractError("image receipt schema is not version 2")
        if not frames:
            raise ReferenceContractError("image receipt contains no completed frames")

        frame_indexes = []
        for key in frames:
            if not isinstance(key, str) or re.fullmatch(r"[0-9]{3,}", key) is None:
                raise ReferenceContractError(f"image receipt frame key is not canonical: {key!r}")
            index = int(key)
            if index < 1 or key != f"{index:03d}":
                raise ReferenceContractError(f"image receipt frame key is not canonical: {key!r}")
            frame_indexes.append(index)
        expected_indexes = list(range(1, len(frames) + 1))
        if sorted(frame_indexes) != expected_indexes:
            raise ReferenceContractError(
                "image receipt frames are not the contiguous set 001..N"
            )

        outputs = []
        sha_pattern = re.compile(r"[0-9a-f]{64}")
        for index in expected_indexes:
            key = f"{index:03d}"
            row = frames.get(key)
            if not isinstance(row, dict):
                raise ReferenceContractError(f"image receipt frame {key} is invalid")
            generation_fingerprint = row.get("generation_fingerprint")
            completed_fingerprint = row.get("completed_fingerprint")
            if (
                not isinstance(generation_fingerprint, str)
                or sha_pattern.fullmatch(generation_fingerprint) is None
                or not isinstance(completed_fingerprint, str)
                or completed_fingerprint != generation_fingerprint
            ):
                raise ReferenceContractError(
                    f"image receipt frame {key} is incomplete or belongs to another generation"
                )
            status = row.get("status")
            trusted_legacy = (
                status == "legacy"
                and row.get("trusted_by_user") is True
                and isinstance(row.get("legacy_trusted_at"), int)
                and not isinstance(row.get("legacy_trusted_at"), bool)
                and row["legacy_trusted_at"] > 0
            )
            if status != "fresh" and not trusted_legacy:
                raise ReferenceContractError(
                    f"image receipt frame {key} is neither fresh nor explicitly trusted legacy"
                )
            output_sha = row.get("output_sha256")
            if not isinstance(output_sha, str) or sha_pattern.fullmatch(output_sha) is None:
                raise ReferenceContractError(f"image receipt frame {key} has no valid output hash")
            filename = f"{key}.jpg"
            output_path = os.path.join(googler_dir, filename)
            if os.path.islink(output_path) or not os.path.isfile(output_path):
                raise ReferenceContractError(
                    f"receipt-authorized output {filename} is missing or not a regular file"
                )
            try:
                actual_sha = self._sha256_file_for_cache(output_path)
            except OSError as exc:
                raise ReferenceContractError(
                    f"receipt-authorized output {filename} is unreadable"
                ) from exc
            if actual_sha != output_sha:
                raise ReferenceContractError(
                    f"receipt-authorized output {filename} does not match its hash"
                )
            outputs.append((filename, output_sha))
        return tuple(outputs)

    def _copy_googler_images_to_similar_projects(self, googler_dir, run_snapshot):
        """Fail closed: project-owned image identity cannot be copied safely."""
        self.log(
            "🛡️ Копирование картинок между проектами BLOCKED: "
            f"{CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON}. "
            "Каждый проект генерирует свои кадры.",
            kind="images",
        )
        return 0

        # Retained only as non-executable migration context for old builds.
        # The unconditional return above is the product contract.
        if not isinstance(run_snapshot, ImageGenerationSnapshot):
            self.log(
                "🛡️ Копирование между проектами BLOCKED: "
                "нет immutable snapshot запуска.",
                kind="images",
            )
            return 0
        if not run_snapshot.reference_reuse_allowed:
            self.log(
                "🛡️ Копирование между проектами BLOCKED по snapshot запуска: "
                f"{run_snapshot.reference_reuse_reason}.",
                kind="images",
            )
            return 0
        snapshot_intact, snapshot_reason = self._image_generation_snapshot_intact(
            run_snapshot
        )
        if not snapshot_intact:
            self.log(
                f"🛡️ Копирование между проектами BLOCKED: {snapshot_reason}.",
                kind="images",
            )
            return 0
        source_safe, source_reason = self._image_reference_reuse_status()
        if not source_safe:
            self.log(
                f"🛡️ Копирование между проектами BLOCKED: {source_reason}. "
                "Каждый проект должен сгенерировать свои кадры.",
                kind="images",
            )
            return 0
        try:
            reusable_outputs = self._validated_reusable_image_outputs(googler_dir)
        except (ReferenceContractError, OSError, ValueError) as exc:
            self.log(
                f"🛡️ Копирование между проектами BLOCKED: "
                f"receipt не прошёл проверку: {exc}.",
                kind="images",
            )
            return 0
        my_key = self._content_key()
        # Receipt continuity is the authoritative scene count for reuse.
        master_n = len(reusable_outputs)
        copied_to = 0
        for other_name, other_frame in self.app_reference.projects.items():
            if other_frame is self:
                continue
            target_opted_in = (
                getattr(other_frame, 'googler_copy_similar', None)
                and other_frame.googler_copy_similar.get()
            )
            if not target_opted_in:
                continue
            sim = _project_name_similarity(my_key, other_frame._content_key())
            if sim < 0.85:
                continue
            target_safe, target_reason = other_frame._image_reuse_target_status()
            if not target_safe:
                self.log(
                    f"🛡️ '{other_name}' тот же текст ({sim:.0%}), но не может "
                    f"быть целью reuse: {target_reason}. "
                    "Проект должен сгенерировать свои кадры.",
                    kind="images",
                )
                continue
            if other_frame._image_preset_key() != run_snapshot.source_preset_key:
                # тот же текст, но ДРУГОЙ пресет (стиль/модель/ratio/промты) → НЕ копируем чужой стиль
                self.log(f"⏭ '{other_name}' тот же текст ({sim:.0%}), но другой пресет изображений — НЕ копирую (сгенерит своё)", kind="images")
                continue
            try:
                target = os.path.normpath(os.path.join(other_frame.project_dir, "googler"))
                os.makedirs(target, exist_ok=True)
                # Recheck immediately before staging.  Grouping happened before
                # the source run, so the target may have gained outputs or an
                # active image mutation while the source was generating.
                target_safe, target_reason = other_frame._image_reuse_target_status()
                if not target_safe:
                    self.log(
                        f"🛡️ '{other_name}' изменился до копирования: "
                        f"{target_reason}. Reuse BLOCKED; проект должен "
                        "сгенерировать свои кадры.",
                        kind="images",
                    )
                    continue
                stage_dir = tempfile.mkdtemp(prefix=".reuse-stage-", dir=target)
                try:
                    for filename, expected_sha in reusable_outputs:
                        src = os.path.join(googler_dir, filename)
                        staged = os.path.join(stage_dir, filename)
                        if os.path.islink(src) or not os.path.isfile(src):
                            raise ReferenceContractError(
                                f"receipt-authorized output {filename} changed before copy"
                            )
                        shutil.copy2(src, staged)
                        if self._sha256_file_for_cache(staged) != expected_sha:
                            raise ReferenceContractError(
                                f"receipt-authorized output {filename} changed during copy"
                            )
                    for filename, _expected_sha in reusable_outputs:
                        os.replace(
                            os.path.join(stage_dir, filename),
                            os.path.join(target, filename),
                        )
                finally:
                    shutil.rmtree(stage_dir, ignore_errors=True)
                n = len(reusable_outputs)
                self.log(f"📋 Картинки ({n}) → '{other_name}' (тот же текст, {sim:.0%})", kind="images")
                if master_n > 0 and self._write_reuse_scenes(other_frame, master_n):
                    self.log(f"   🎯 scenes.json '{other_name}' пересобран на {master_n} сцен → синхрон совпадёт", kind="images")
                copied_to += 1
            except Exception as e:
                self.log(f"⚠️ Ошибка копирования картинок в '{other_name}': {e}", kind="images")
        if copied_to:
            self.log(f"♻️ Картинки переиспользованы в {copied_to} проект(ах) с тем же текстом — генерация для них не нужна", kind="images")
        return copied_to

    def _write_reuse_scenes(self, target_frame, n_scenes):
        """Idea-1: даёт целевому проекту (другой язык) scenes.json с РОВНО n_scenes кусками ЕГО текста,
        чтобы переиспользованные картинки совпали с предложениями при синхроне (image i = scene i).
        Нарезка — равномерная группировка предложений целевого текста на n_scenes групп. Без GPT."""
        try:
            try:
                p = target_frame.source_text_file.get()
            except Exception:
                p = ""
            if not p or not os.path.isfile(p):
                return False
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                src_txt = f.read()
            if not src_txt.strip():
                return False
            try:
                from googler_worker import split_text_to_sentences
                sents = split_text_to_sentences(src_txt)
            except Exception:
                sents = [s for s in re.split(r'(?<=[.!?…])\s+', src_txt) if s.strip()]
            chunks = _split_into_n_chunks(sents, n_scenes)
            scenes = [{"text_segment": c, "visual_prompt": "", "camera_angle": "", "subject_type": ""} for c in chunks]
            target_googler = os.path.normpath(os.path.join(target_frame.project_dir, "googler"))
            os.makedirs(target_googler, exist_ok=True)
            path = os.path.join(target_googler, "scenes.json")
            tmp = path + ".tmp"
            with open(tmp, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(scenes, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception as e:
            try:
                self.log(f"⚠️ scenes.json для reuse не пересобран ('{getattr(target_frame, 'project_name', '?')}'): {e}", kind="images")
            except Exception:
                pass
            return False

    def run_thief_transform(self):
        """Трансформировать слайды через Googler API (через очередь)"""
        # НОВОЕ: Слайды в отдельной директории
        # Нормализуем путь для корректной работы с русскими символами
        slides_dir = os.path.normpath(os.path.join(BASE_SLIDES_DIR, self.project_name))
        
        if not os.path.exists(slides_dir):
            self.log("⚠️ Сначала извлеките слайды!", kind="thief")
            return
        
        slides = sorted([
            os.path.join(slides_dir, f) 
            for f in os.listdir(slides_dir) 
            if f.lower().endswith(('.png', '.jpg', '.jpeg'))
        ])
        
        if not slides:
            self.log("⚠️ Нет слайдов для трансформации!", kind="thief")
            return
        
        prompt = self.thief_transform_prompt.get().strip()
        if not prompt:
            self.log("⚠️ Введите промт для трансформации!", kind="thief")
            return
        
        if self.thief_queue:
            success, message = self.thief_queue.add_to_queue(self.project_name + "_transform", self._run_thief_transform_worker)
            self.log(f"📊 {message}", kind="thief")
        else:
            threading.Thread(target=self._run_thief_transform_worker, daemon=True).start()
    
    def _run_thief_transform_worker(self):
        """Воркер трансформации слайдов"""
        # Нормализуем путь для корректной работы с русскими символами
        slides_dir = os.path.normpath(os.path.join(BASE_SLIDES_DIR, self.project_name))
        slides = sorted([
            os.path.normpath(os.path.join(slides_dir, f)) 
            for f in os.listdir(slides_dir) 
            if f.lower().endswith(('.png', '.jpg', '.jpeg'))
        ])
        
        prompt = self.thief_transform_prompt.get().strip()
        
        # Папка для результатов - Images проекта
        # Нормализуем путь для корректной работы с русскими символами
        output_dir = os.path.normpath(os.path.join(self.project_dir, IMAGE_DIR_NAME))
        os.makedirs(output_dir, exist_ok=True)
        
        # Проверяем наличие скрипта
        googler_script = os.path.join(SCRIPTS_DIR, "googler_worker.py")
        if not check_worker_exists("googler_worker.py"):
            self.log(f"❌ Не найден скрипт: googler_worker.py (или googler_worker.exe)", kind="thief")
            return
        
        self.log(f"🔄 Трансформация {len(slides)} слайдов...", kind="thief")
        self.log(f"📝 Промт: {prompt[:80]}...", kind="thief")
        self.log(f"📁 Результат: {output_dir}", kind="thief")
        
        try:
            import json as json_module
            
            # ИСПРАВЛЕНИЕ: Сохраняем список путей во временный файл
            temp_file = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', delete=False)
            temp_file_path = None
            try:
                json_module.dump(slides, temp_file, ensure_ascii=False)
                temp_file.close()
                temp_file_path = temp_file.name
                
                # Убеждаемся, что путь нормализован и абсолютный для корректной передачи в subprocess
                abs_output_dir = os.path.abspath(os.path.normpath(output_dir))
                worker_args = [
                    "transform",
                    "--prompt", prompt,
                    "--image_paths_file", temp_file_path,
                    "--output_dir", abs_output_dir
                ]
                model_val = self.googler_model.get()
                if model_val:
                    worker_args.extend(["--model", model_val])
                # В режиме одного exe воркер встроен — запускаем в процессе
                self._thief_stop_event.clear()
                used, code = run_worker_inprocess("googler_worker.py", worker_args, self.log, kind="thief", stop_event=self._thief_stop_event)
                if used:
                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.unlink(temp_file_path)
                        except Exception:
                            pass
                    if code == -1:
                        self.log("⏹️ Трансформация остановлена", kind="thief")
                        return
                    if code == 0:
                        results = [f for f in os.listdir(output_dir) if '_transformed' in f]
                        self.log(f"\n✅ Трансформировано {len(results)} изображений", kind="thief")
                        self.log(f"📁 Сохранено в: {output_dir}", kind="thief")
                    else:
                        self.log(f"\n❌ Ошибка трансформации (код {code})", kind="thief")
                    return
                cmd = get_worker_command(googler_script, worker_args)
                if cmd is None or len(cmd) == 0:
                    self.log(f"❌ Ошибка: не найден {googler_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                    temp_file.close()
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
                    return
            except Exception as e:
                temp_file.close()
                if temp_file_path and os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                raise e
            
            startupinfo = get_subprocess_flags()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                startupinfo=startupinfo,
                creationflags=get_subprocess_creationflags()
            )
            self.thief_proc_handle = process
            
            if process.stdout:
                # Читаем вывод в реальном времени с немедленным выводом
                while True:
                    line = process.stdout.readline()
                    if not line:
                        if process.poll() is not None:
                            break
                        continue
                    line = line.rstrip()
                    if line:
                        self.log(line, kind="thief")
            
            process.wait()
            self.thief_proc_handle = None
            
            # Удаляем временный файл
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
            
            if process.returncode == 0:
                # Подсчитываем результаты
                results = [f for f in os.listdir(output_dir) if '_transformed' in f]
                self.log(f"\n✅ Трансформировано {len(results)} изображений", kind="thief")
                self.log(f"📁 Сохранено в: {output_dir}", kind="thief")
            else:
                self.log(f"\n❌ Ошибка трансформации (код {process.returncode})", kind="thief")
                
        except Exception as e:
            # Удаляем временный файл при ошибке
            if 'temp_file_path' in locals() and temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
            self.log(f"\n❌ ОШИБКА: {e}", kind="thief")
    
    def run_thief_full(self):
        """Полный цикл: извлечение + трансформация (через очередь)"""
        url = self.thief_youtube_url.get().strip()
        if not url:
            self.log("⚠️ Введите YouTube URL!", kind="thief")
            return

        prompt = self.thief_transform_prompt.get().strip()
        if not prompt:
            self.log("⚠️ Введите промт для трансформации!", kind="thief")
            return
        
        # Проверяем наличие скриптов
        extractor_script = os.path.join(SCRIPTS_DIR, "youtube_slide_extractor.py")
        googler_script = os.path.join(SCRIPTS_DIR, "googler_worker.py")
        
        if not check_worker_exists("youtube_slide_extractor.py"):
            self.log(f"Не найден скрипт: youtube_slide_extractor.py (или youtube_slide_extractor.exe)", kind="thief")
            self.log("Не найден скрипт: youtube_slide_extractor.py", kind="thief")
            return
        if not check_worker_exists("googler_worker.py"):
            self.log(f"Не найден скрипт: googler_worker.py (или googler_worker.exe)", kind="thief")
            self.log("Не найден скрипт: googler_worker.py", kind="thief")
            return
        
        if self.thief_queue:
            success, message = self.thief_queue.add_to_queue(self.project_name + "_full", self._run_thief_full_worker)
            self.log(f"📊 {message}", kind="thief")
        else:
            threading.Thread(target=self._run_thief_full_worker, daemon=True).start()
    
    def _run_thief_full_worker(self):
        """Воркер полного цикла: извлечение + трансформация"""
        url = self.thief_youtube_url.get().strip()
        prompt = self.thief_transform_prompt.get().strip()
        
        # НОВОЕ: Слайды в отдельной директории, трансформированные - в Images проекта
        # Нормализуем пути для корректной работы с русскими символами
        slides_dir = os.path.normpath(os.path.join(BASE_SLIDES_DIR, self.project_name))
        output_dir = os.path.normpath(os.path.join(self.project_dir, IMAGE_DIR_NAME))
        os.makedirs(slides_dir, exist_ok=True)
        
        # Параметры
        sensitivity = self.thief_sensitivity.get()
        min_duration = self.thief_min_duration.get()
        sample_interval = self.thief_sample_interval.get()
        strict = self.thief_strict.get()
        valid_qualities = ("best", "1080p", "720p", "480p", "360p", "fastest")
        source_quality = (self.thief_source_quality.get() or "best").strip().lower()
        if source_quality not in valid_qualities:
            source_quality = "best"
        
        extractor_script = os.path.join(SCRIPTS_DIR, "youtube_slide_extractor.py")
        googler_script = os.path.join(SCRIPTS_DIR, "googler_worker.py")
        
        self.log("🚀 ПОЛНЫЙ ЦИКЛ: YouTube → Слайды → Трансформация", kind="thief")
        self.log("="*50, kind="thief")
        
        try:
            # ШАГ 1: Извлечение слайдов
            self.log("\n--- ШАГ 1: Извлечение слайдов ---", kind="thief")
            self.log(f"🎬 URL: {url}", kind="thief")
            
            abs_slides_dir = os.path.abspath(os.path.normpath(slides_dir))
            worker_args = [
                url,
                "--output", abs_slides_dir,
                "--sensitivity", str(sensitivity),
                "--min-duration", str(min_duration),
                "--sample-interval", str(sample_interval),
                "--source-quality", source_quality
            ]
            if strict:
                worker_args.append("--strict")
            used, code = run_worker_inprocess("youtube_slide_extractor.py", worker_args, self.log, kind="thief")
            if used:
                if code != 0:
                    self.log("\n❌ Ошибка извлечения слайдов", kind="thief")
                    return
            else:
                cmd = get_worker_command(extractor_script, worker_args)
                if cmd is None or len(cmd) == 0:
                    self.log(f"❌ Ошибка: не найден {extractor_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="thief")
                    return
                startupinfo = get_subprocess_flags()
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    startupinfo=startupinfo,
                    creationflags=get_subprocess_creationflags()
                )
                self.thief_proc_handle = process
                if process.stdout:
                    while True:
                        line = process.stdout.readline()
                        if not line:
                            if process.poll() is not None:
                                break
                            continue
                        line = line.rstrip()
                        if line:
                            self.log(line, kind="thief")
                process.wait()
                if process.returncode != 0:
                    self.log(f"\n❌ Ошибка извлечения слайдов", kind="thief")
                    return
            
            # Проверяем слайды
            if not os.path.exists(slides_dir):
                self.log("\n❌ Папка со слайдами не создана", kind="thief")
                return
            
            slides = sorted([
                os.path.join(slides_dir, f) 
                for f in os.listdir(slides_dir) 
                if f.lower().endswith(('.png', '.jpg', '.jpeg'))
            ])
            
            if not slides:
                self.log("\n❌ Слайды не найдены", kind="thief")
                return
            
            self.log(f"\n✅ Извлечено {len(slides)} слайдов", kind="thief")
            
            # ШАГ 2: Трансформация
            self.log("\n--- ШАГ 2: Трансформация через Googler ---", kind="thief")
            self.log(f"📝 Промт: {prompt[:80]}...", kind="thief")
            
            os.makedirs(output_dir, exist_ok=True)
            
            # ИСПРАВЛЕНИЕ: Сохраняем список путей во временный файл
            import json as json_module
            temp_file = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', delete=False)
            temp_file_path = None
            try:
                json_module.dump(slides, temp_file, ensure_ascii=False)
                temp_file.close()
                temp_file_path = temp_file.name
                
                abs_output_dir = os.path.abspath(os.path.normpath(output_dir))
                worker_args = [
                    "transform",
                    "--prompt", prompt,
                    "--image_paths_file", temp_file_path,
                    "--output_dir", abs_output_dir
                ]
                model_val = self.googler_model.get()
                if model_val:
                    worker_args.extend(["--model", model_val])
                self._thief_stop_event.clear()
                used, code = run_worker_inprocess("googler_worker.py", worker_args, self.log, kind="thief", stop_event=self._thief_stop_event)
                if used:
                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.unlink(temp_file_path)
                        except Exception:
                            pass
                    if code == -1:
                        self.log("⏹️ Трансформация остановлена", kind="thief")
                        return
                    if code == 0:
                        results = [f for f in os.listdir(output_dir) if '_transformed' in f]
                        self.log(f"\n" + "="*50, kind="thief")
                        self.log(f"🎉 ПОЛНЫЙ ЦИКЛ ЗАВЕРШЕН!", kind="thief")
                        self.log(f"✅ Трансформировано: {len(results)} изображений", kind="thief")
                        self.log(f"📁 Папка: {output_dir}", kind="thief")
                    else:
                        self.log(f"\n❌ Ошибка трансформации", kind="thief")
                    return
                cmd = get_worker_command(googler_script, worker_args)
                if cmd is None or len(cmd) == 0:
                    self.log(f"❌ Ошибка: не найден {googler_script}! Проверьте, что файл существует в RELEASE_SECURE", kind="images")
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
                    return
            except Exception as e:
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.unlink(temp_file_path)
                    except Exception:
                        pass
                raise e
            
            startupinfo = get_subprocess_flags()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                startupinfo=startupinfo,
                creationflags=get_subprocess_creationflags()
            )
            self.thief_proc_handle = process
            if process.stdout:
                while True:
                    line = process.stdout.readline()
                    if not line:
                        if process.poll() is not None:
                            break
                        continue
                    line = line.rstrip()
                    if line:
                        self.log(line, kind="thief")
            process.wait()
            self.thief_proc_handle = None
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    pass
            if process.returncode == 0:
                results = [f for f in os.listdir(output_dir) if '_transformed' in f]
                self.log(f"\n" + "="*50, kind="thief")
                self.log(f"🎉 ПОЛНЫЙ ЦИКЛ ЗАВЕРШЕН!", kind="thief")
                self.log(f"✅ Трансформировано: {len(results)} изображений", kind="thief")
                self.log(f"📁 Папка: {output_dir}", kind="thief")
            else:
                self.log(f"\n❌ Ошибка трансформации", kind="thief")
                
        except Exception as e:
            # Удаляем временный файл при ошибке
            if 'temp_file_path' in locals() and temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except:
                    pass
            self.log(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}", kind="thief")
            self.thief_proc_handle = None
    
    def open_project_folder(self):
        """Открыть папку проекта в проводнике"""
        try:
            if os.path.exists(self.project_dir):
                if sys.platform == 'win32':
                    try:
                        os.startfile(self.project_dir)
                    except Exception:
                        # Fallback для Windows
                        subprocess.run(['explorer', self.project_dir], check=False)
                elif sys.platform == 'darwin':
                    subprocess.run(['open', self.project_dir], check=False)
                else:
                    subprocess.run(['xdg-open', self.project_dir], check=False)
            else:
                self.log(f"⚠️ Папка проекта не существует: {self.project_dir}", kind="render")
        except Exception as e:
            self.log(f"Не удалось открыть папку: {e}", kind="render")
    
    def _confirm_close_dialog(self, title, message, on_confirm):
        """Диалог с кнопками «Закрыть» и «Оставить» — «Оставить» явно оставляет проект открытым."""
        d = Toplevel(self.winfo_toplevel())
        d.title(title)
        d.transient(self.winfo_toplevel())
        d.grab_set()
        main_f = Frame(d, padx=20, pady=15)
        main_f.pack(fill="both", expand=True)
        Label(main_f, text=message, justify="left", font=("Arial", 10)).pack(anchor="w", pady=(0, 10))
        btn_frame = Frame(main_f)
        btn_frame.pack(pady=(5, 0))
        def do_close():
            d.grab_release()
            d.destroy()
            on_confirm()
        def do_leave():
            d.grab_release()
            d.destroy()
        Button(btn_frame, text="Закрыть проект", command=do_close, bg="#f44336", fg="white", width=16).pack(side="left", padx=5)
        Button(btn_frame, text="Оставить", command=do_leave, bg="#757575", fg="white", width=14).pack(side="left", padx=5)
        d.wait_window()
    
    def close_project(self):
        """Закрыть проект (вкладку)"""
        active_processes = []
        if self.tts_proc_handle and self.tts_proc_handle.poll() is None:
            active_processes.append("озвучка")
        if (self.bing_proc_handle and self.bing_proc_handle.poll() is None) or \
           (self.googler_proc_handle and self.googler_proc_handle.poll() is None) or \
           (self.googler_video_proc_handle and self.googler_video_proc_handle.poll() is None):
            active_processes.append("изображения/видео")
        if self.render_proc_handle and self.render_proc_handle.poll() is None:
            active_processes.append("рендер")
        if self.thief_proc_handle and self.thief_proc_handle.poll() is None:
            active_processes.append("вор")
        
        if active_processes:
            def on_confirm():
                self.stop_tts()
                self.stop_images()
                self.stop_render()
                self.stop_thief()
                self.app_reference.remove_project(self.project_name)
            self._confirm_close_dialog(
                "Активные процессы",
                f"В проекте запущены процессы: {', '.join(active_processes)}.\n\n"
                "Закрыть проект и остановить все процессы?",
                on_confirm
            )
            return
        
        def on_confirm():
            self.app_reference.remove_project(self.project_name)
        self._confirm_close_dialog(
            "Закрыть проект",
            f"Закрыть проект '{self.project_name}'?\n\nПапка проекта и файлы НЕ будут удалены.",
            on_confirm
        )
    
    def update_ui_language(self):
        """Обновляет все тексты интерфейса при смене языка"""
        if not hasattr(self, 'ui_widgets'):
            return
        
        # Обновляем статус очереди
        if hasattr(self, 'queue_status_label'):
            status = self.render_queue.get_status()
            self.queue_status_label.config(text=_('render_queue_active').format(active=status['active'], max=status['max'], queued=status['queued']))
        
        # Обновляем вкладки
        try:
            tabs = self.stages_notebook.tabs()
            if len(tabs) >= 5:
                self.stages_notebook.tab(tabs[0], text=f"📄 {_('tab_project')}")
                self.stages_notebook.tab(tabs[1], text=f"🎤 {_('tab_tts')}")
                self.stages_notebook.tab(tabs[2], text=f"🖼️ {_('tab_images')}")
                self.stages_notebook.tab(tabs[3], text=f"🎬 {_('tab_render')}")
                self.stages_notebook.tab(tabs[4], text=f"📥 {_('tab_thief')}")
        except:
            pass
        
        # Обновляем виджеты проекта
        if 'preset_frame' in self.ui_widgets:
            self.ui_widgets['preset_frame'].config(text=_('presets'))
        if 'preset_label' in self.ui_widgets:
            self.ui_widgets['preset_label'].config(text=_('preset'))
        if 'text_frame' in self.ui_widgets:
            self.ui_widgets['text_frame'].config(text=f"📄 {_('source_text')}")
        if 'file_label' in self.ui_widgets:
            self.ui_widgets['file_label'].config(text=_('file'))
        if 'select_file_btn' in self.ui_widgets:
            self.ui_widgets['select_file_btn'].config(text=_('select_file_multiple'))
        if 'info_frame' in self.ui_widgets:
            self.ui_widgets['info_frame'].config(text=_('info'))
        if 'info_label' in self.ui_widgets:
            self.ui_widgets['info_label'].config(text=_('info_text'))
        if 'project_controls' in self.ui_widgets:
            self.ui_widgets['project_controls'].config(text=_('project_controls'))
        if 'tab_controls_label' in self.ui_widgets:
            self.ui_widgets['tab_controls_label'].config(text=_('tab_controls'))
        if 'rename_btn' in self.ui_widgets:
            self.ui_widgets['rename_btn'].config(text=_('rename'))
        if 'move_left_btn' in self.ui_widgets:
            self.ui_widgets['move_left_btn'].config(text=_('move_left'))
        if 'move_right_btn' in self.ui_widgets:
            self.ui_widgets['move_right_btn'].config(text=_('move_right'))
        if 'open_folder_btn' in self.ui_widgets:
            self.ui_widgets['open_folder_btn'].config(text=_('open_project_folder'))
        if 'close_project_btn' in self.ui_widgets:
            self.ui_widgets['close_project_btn'].config(text=_('close_project'))
        
        # Обновляем виджеты TTS
        if 'tts_settings_frame' in self.ui_widgets:
            self.ui_widgets['tts_settings_frame'].config(text=_('tts_settings'))
        if 'tts_engine_label' in self.ui_widgets:
            self.ui_widgets['tts_engine_label'].config(text=_('tts_engine'))
        if 'voice_template_label' in self.ui_widgets:
            self.ui_widgets['voice_template_label'].config(text=_('voice_template'))
        if 'edit_template_btn' in self.ui_widgets:
            self.ui_widgets['edit_template_btn'].config(text=_('edit_template'))
        if 'delete_template_btn' in self.ui_widgets:
            self.ui_widgets['delete_template_btn'].config(text=_('delete_template'))
        if 'auto_detect_cb' in self.ui_widgets:
            self.ui_widgets['auto_detect_cb'].config(text=_('auto_detect_language'))
        if 'auto_detect_install_label' in self.ui_widgets:
            self.ui_widgets['auto_detect_install_label'].config(text=_('auto_detect_install'))
        if 'edge_voice_label' in self.ui_widgets:
            self.ui_widgets['edge_voice_label'].config(text=_('edge_voice'))
        if 'start_tts_btn' in self.ui_widgets:
            self.ui_widgets['start_tts_btn'].config(text=_('start_tts'))
        if 'start_tts_full_btn' in self.ui_widgets:
            self.ui_widgets['start_tts_full_btn'].config(text=_('start_tts_full'))
        if 'stop_tts_btn' in self.ui_widgets:
            self.ui_widgets['stop_tts_btn'].config(text=_('stop_tts'))
        if 'tts_log_frame' in self.ui_widgets:
            self.ui_widgets['tts_log_frame'].config(text=_('tts_log'))
        if 'copy_tts_log_btn' in self.ui_widgets:
            self.ui_widgets['copy_tts_log_btn'].config(text=_('copy_log'))
        
        # Обновляем виджеты Изображений
        if 'source_frame' in self.ui_widgets:
            self.ui_widgets['source_frame'].config(text=_('image_source'))
        if 'bing_frame' in self.ui_widgets:
            self.ui_widgets['bing_frame'].config(text=_('bing_search_title'))
        if 'tags_label' in self.ui_widgets:
            self.ui_widgets['tags_label'].config(text=_('tags_comma'))
        if 'images_per_tag_label' in self.ui_widgets:
            self.ui_widgets['images_per_tag_label'].config(text=_('images_per_tag'))
        if 'download_bing_btn' in self.ui_widgets:
            self.ui_widgets['download_bing_btn'].config(text=_('download_bing'))
        if 'stop_images_btn' in self.ui_widgets:
            self.ui_widgets['stop_images_btn'].config(text=_('stop_tts'))
        if 'googler_frame' in self.ui_widgets:
            self.ui_widgets['googler_frame'].config(text=_('googler_generate_title'))
        if 'style_label' in self.ui_widgets:
            self.ui_widgets['style_label'].config(text=_('style'))
        if 'aspect_ratio_label' in self.ui_widgets:
            self.ui_widgets['aspect_ratio_label'].config(text=_('aspect_ratio'))
        if 'sentences_per_prompt_label' in self.ui_widgets:
            self.ui_widgets['sentences_per_prompt_label'].config(text=_('sentences_per_prompt'))
        if 'gpt_prompts_cb' in self.ui_widgets:
            self.ui_widgets['gpt_prompts_cb'].config(text=_('generate_gpt_prompts'))
        if 'generate_images_btn' in self.ui_widgets:
            self.ui_widgets['generate_images_btn'].config(text=_('generate_images'))
        if 'stop_googler_btn' in self.ui_widgets:
            self.ui_widgets['stop_googler_btn'].config(text=_('stop_tts'))
        if 'googler_video_frame' in self.ui_widgets:
            self.ui_widgets['googler_video_frame'].config(text=_('googler_video_title'))
        if 'images_label' in self.ui_widgets:
            self.ui_widgets['images_label'].config(text=_('images_label'))
        if hasattr(self, 'selected_images_label'):
            if self.selected_images_label.cget('text') == 'Не выбрано' or self.selected_images_label.cget('text') == 'Not selected':
                self.selected_images_label.config(text=_('not_selected'))
        if 'select_images_btn' in self.ui_widgets:
            self.ui_widgets['select_images_btn'].config(text=_('select_images'))
        if 'video_prompt_label' in self.ui_widgets:
            self.ui_widgets['video_prompt_label'].config(text=_('video_prompt'))
        if 'loop_video_cb' in self.ui_widgets:
            self.ui_widgets['loop_video_cb'].config(text=_('loop_video'))
        if 'generate_video_btn' in self.ui_widgets:
            self.ui_widgets['generate_video_btn'].config(text=_('generate_video'))
        if 'stop_video_btn' in self.ui_widgets:
            self.ui_widgets['stop_video_btn'].config(text=_('stop_tts'))
        if 'img_log_frame' in self.ui_widgets:
            self.ui_widgets['img_log_frame'].config(text=_('log'))
        if 'copy_img_log_btn' in self.ui_widgets:
            self.ui_widgets['copy_img_log_btn'].config(text=_('copy_log'))
        
        # Обновляем виджеты Рендера
        if 'video_settings_frame' in self.ui_widgets:
            self.ui_widgets['video_settings_frame'].config(text=_('render_settings'))
        if 'quality_label' in self.ui_widgets:
            self.ui_widgets['quality_label'].config(text=_('quality'))
        if 'min_duration_label' in self.ui_widgets:
            self.ui_widgets['min_duration_label'].config(text=_('min_image_duration'))
        if 'max_duration_label' in self.ui_widgets:
            self.ui_widgets['max_duration_label'].config(text=_('max_image_duration'))
        if 'subtitle_lang_label' in self.ui_widgets:
            self.ui_widgets['subtitle_lang_label'].config(text=_('subtitle_language_label'))
        if 'bg_audio_label' in self.ui_widgets:
            self.ui_widgets['bg_audio_label'].config(text=_('background_audio'))
        if 'select_bg_audio_btn' in self.ui_widgets:
            self.ui_widgets['select_bg_audio_btn'].config(text=_('select_bg_audio'))
        if 'main_volume_label' in self.ui_widgets:
            self.ui_widgets['main_volume_label'].config(text=_('main_audio_volume'))
        if 'bg_volume_label' in self.ui_widgets:
            self.ui_widgets['bg_volume_label'].config(text=_('background_audio_volume'))
        if 'intro_video_label' in self.ui_widgets:
            self.ui_widgets['intro_video_label'].config(text=_('intro_video'))
        if 'select_intro_video_btn' in self.ui_widgets:
            self.ui_widgets['select_intro_video_btn'].config(text=_('select_intro_video'))
        if 'end_track_label' in self.ui_widgets:
            self.ui_widgets['end_track_label'].config(text=_('end_track'))
        if 'select_end_track_btn' in self.ui_widgets:
            self.ui_widgets['select_end_track_btn'].config(text=_('select_end_track'))
        if 'end_track_volume_label' in self.ui_widgets:
            self.ui_widgets['end_track_volume_label'].config(text=_('end_track_volume'))
        if 'pause_label' in self.ui_widgets:
            self.ui_widgets['pause_label'].config(text=_('pause_settings'))
        if 'sec_every_label' in self.ui_widgets:
            self.ui_widgets['sec_every_label'].config(text=_('sec_every'))
        if 'audio_zero_label' in self.ui_widgets:
            self.ui_widgets['audio_zero_label'].config(text=_('audio_zero'))
        if 'distribute_cb' in self.ui_widgets:
            self.ui_widgets['distribute_cb'].config(text=_('distribute_evenly'))
        if 'subtitles_cb' in self.ui_widgets:
            self.ui_widgets['subtitles_cb'].config(text=_('subtitles_whisper'))
        if 'cache_cb' in self.ui_widgets:
            self.ui_widgets['cache_cb'].config(text=_('cache'))
        if 'vintage_cb' in self.ui_widgets:
            self.ui_widgets['vintage_cb'].config(text=_('vintage_effect'))
        if 'transitions_frame' in self.ui_widgets:
            self.ui_widgets['transitions_frame'].config(text=_('transitions_effects'))
        if 'fade_cb' in self.ui_widgets:
            self.ui_widgets['fade_cb'].config(text=_('fade_transitions'))
        if 'duration_short_label' in self.ui_widgets:
            self.ui_widgets['duration_short_label'].config(text=_('duration_short'))
        if 'sec_label' in self.ui_widgets:
            self.ui_widgets['sec_label'].config(text=_('sec'))
        if 'kenburns_cb' in self.ui_widgets:
            self.ui_widgets['kenburns_cb'].config(text=_('kenburns'))
        if 'zoom_label' in self.ui_widgets:
            self.ui_widgets['zoom_label'].config(text=_('zoom'))
        if 'material_order_frame' in self.ui_widgets:
            self.ui_widgets['material_order_frame'].config(text=_('material_order_title'))
        if 'material_order_label' in self.ui_widgets:
            self.ui_widgets['material_order_label'].config(text=_('material_order'))
        if 'material_order_edit_btn' in self.ui_widgets:
            self.ui_widgets['material_order_edit_btn'].config(text=_('material_order_edit_btn'))
        if 'scale_offset_frame' in self.ui_widgets:
            self.ui_widgets['scale_offset_frame'].config(text=_('media_scale_offset_title'))
        if 'media_scale_edit_btn' in self.ui_widgets:
            self.ui_widgets['media_scale_edit_btn'].config(text=_('media_scale_edit_btn'))
        if 'material_order_combo' in self.ui_widgets and hasattr(self, '_material_order_keys'):
            cur_key = self._get_material_order_key()
            self._material_order_displays = (
                _('material_order_video_first'),
                _('material_order_images_first'),
                _('material_order_random'),
                _('material_order_alternate'),
                _('material_order_alphabetical'),
                _('material_order_manual')
            )
            self.ui_widgets['material_order_combo']['values'] = self._material_order_displays
            if cur_key in self._material_order_keys:
                self.material_order_var.set(self._material_order_displays[self._material_order_keys.index(cur_key)])
        if 'assemble_video_btn' in self.ui_widgets:
            self.ui_widgets['assemble_video_btn'].config(text=_('assemble_video'))
        if 'stop_render_btn' in self.ui_widgets:
            self.ui_widgets['stop_render_btn'].config(text=_('stop_render'))
        if 'render_log_frame' in self.ui_widgets:
            self.ui_widgets['render_log_frame'].config(text=_('render_log'))
        if 'copy_render_log_btn' in self.ui_widgets:
            self.ui_widgets['copy_render_log_btn'].config(text=_('copy_log'))
        
        # Обновляем виджеты Вора
        if 'url_frame' in self.ui_widgets:
            self.ui_widgets['url_frame'].config(text=_('video_source'))
        if 'youtube_url_label' in self.ui_widgets:
            self.ui_widgets['youtube_url_label'].config(text=_('youtube_url'))
        if 'paste_btn' in self.ui_widgets:
            self.ui_widgets['paste_btn'].config(text=_('paste'))
        if 'extract_frame' in self.ui_widgets:
            self.ui_widgets['extract_frame'].config(text=_('extract_settings'))
        if 'sensitivity_label' in self.ui_widgets:
            self.ui_widgets['sensitivity_label'].config(text=_('sensitivity'))
        if 'sensitivity_hint' in self.ui_widgets:
            self.ui_widgets['sensitivity_hint'].config(text=_('sensitivity_hint'))
        if 'min_duration_label' in self.ui_widgets:
            self.ui_widgets['min_duration_label'].config(text=_('min_duration'))
        if 'interval_label' in self.ui_widgets:
            self.ui_widgets['interval_label'].config(text=_('sample_interval'))
        if 'interval_hint' in self.ui_widgets:
            self.ui_widgets['interval_hint'].config(text=_('interval_hint'))
        if 'strict_cb' in self.ui_widgets:
            self.ui_widgets['strict_cb'].config(text=_('strict_dedup'))
        if 'source_quality_label' in self.ui_widgets:
            self.ui_widgets['source_quality_label'].config(text=_('source_quality'))
        if 'transform_prompt_label' in self.ui_widgets:
            self.ui_widgets['transform_prompt_label'].config(text=_('transform_prompt'))
        if 'extract_btn' in self.ui_widgets:
            self.ui_widgets['extract_btn'].config(text=_('extract_slides'))
        if 'transform_btn' in self.ui_widgets:
            self.ui_widgets['transform_btn'].config(text=_('transform'))
        if 'full_cycle_btn' in self.ui_widgets:
            self.ui_widgets['full_cycle_btn'].config(text=_('full_cycle'))
        if 'stop_thief_btn' in self.ui_widgets:
            self.ui_widgets['stop_thief_btn'].config(text=_('stop_thief'))
        if 'thief_info_frame' in self.ui_widgets:
            self.ui_widgets['thief_info_frame'].config(text=_('info'))
        if 'thief_info_label' in self.ui_widgets:
            self.ui_widgets['thief_info_label'].config(text=_('thief_info'))
        if 'thief_log_frame' in self.ui_widgets:
            self.ui_widgets['thief_log_frame'].config(text=_('log'))
        if 'copy_thief_log_btn' in self.ui_widgets:
            self.ui_widgets['copy_thief_log_btn'].config(text=_('copy_log'))


# --- ГЛАВНОЕ ПРИЛОЖЕНИЕ ---
class VoiceMapping:
    """Сохраняет маппинг {project_prefix: {language: voice_settings}} для автоподстановки голосов."""
    _PATH = os.path.join(os.path.expanduser("~"), "video_studio_voice_mapping.json")

    @classmethod
    def load(cls):
        try:
            if os.path.exists(cls._PATH):
                with open(cls._PATH, 'r', encoding='utf-8', errors='replace') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    @classmethod
    def save(cls, data):
        try:
            with open(cls._PATH, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @classmethod
    def get_voice(cls, project_name, language):
        """Получить сохранённые настройки голоса для проекта и языка."""
        data = cls.load()
        prefix = project_name.rstrip('0123456789').strip('_ ')
        candidates = [project_name]
        if prefix and prefix != project_name:
            candidates.append(prefix)
        for key in candidates:
            if key in data and language in data[key]:
                return data[key][language]
        return None

    @classmethod
    def save_voice(cls, project_name, language, voice_settings):
        """Сохранить настройки голоса для проекта и языка."""
        data = cls.load()
        prefix = project_name.rstrip('0123456789').strip('_ ')
        key = prefix if prefix else project_name
        if key not in data:
            data[key] = {}
        data[key][language] = voice_settings
        cls.save(data)


# 36 языков для наборов голосов: (langdetect_code, русское_название). Порядок = порядок в редакторе.
VOICE_SET_LANGUAGES = [
    ("ru", "Русский"), ("pl", "Польский"), ("cs", "Чешский"), ("hr", "Хорватский"), ("en", "Английский"),
    ("de", "Немецкий"), ("fr", "Французский"), ("es", "Испанский"), ("it", "Итальянский"),
    ("pt", "Португальский"), ("nl", "Нидерландский"), ("sv", "Шведский"), ("no", "Норвежский"),
    ("da", "Датский"), ("fi", "Финский"), ("lt", "Литовский"), ("lv", "Латышский"),
    ("et", "Эстонский"), ("el", "Греческий"), ("ro", "Румынский"), ("hu", "Венгерский"),
    ("tr", "Турецкий"), ("kk", "Казахский"), ("uz", "Узбекский"), ("az", "Азербайджанский"),
    ("hy", "Армянский"), ("ka", "Грузинский"), ("ar", "Арабский"), ("he", "Иврит"),
    ("fa", "Персидский (фарси)"), ("hi", "Хинди"), ("zh-cn", "Китайский (упрощённый)"),
    ("ja", "Японский"), ("ko", "Корейский"), ("vi", "Вьетнамский"), ("th", "Тайский"),
    ("id", "Индонезийский"),
]


class VoiceSets:
    """Глобальные наборы голосов: {набор: {lang_code: voice_id}}.
    Один voice_id на язык — общий для VoiceGenCore и Voicer (у них одинаковые ID).
    Файл ~/video_studio_voice_sets.json. Автоподстановка по детекту языка скрипта."""
    _PATH = os.path.join(os.path.expanduser("~"), "video_studio_voice_sets.json")

    @classmethod
    def load(cls):
        try:
            if os.path.exists(cls._PATH):
                with open(cls._PATH, 'r', encoding='utf-8', errors='replace') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get("sets"), dict):
                        return data
        except Exception:
            pass
        return {"sets": {}}

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
    def get_set_names(cls):
        return sorted(cls.load().get("sets", {}).keys())

    @classmethod
    def get_set(cls, name):
        """Возвращает {lang_code: voice_id} для набора (или {})."""
        return cls.load().get("sets", {}).get(name, {}).get("voices", {})

    @classmethod
    def save_set(cls, name, voices):
        """Сохраняет набор. Пустые voice_id отбрасываются."""
        data = cls.load()
        clean = {k: str(v).strip() for k, v in voices.items() if v and str(v).strip()}
        data.setdefault("sets", {})[name] = {"voices": clean}
        return cls.save(data)

    @classmethod
    def delete_set(cls, name):
        data = cls.load()
        if name in data.get("sets", {}):
            del data["sets"][name]
            return cls.save(data)
        return False

    # Известные путаницы langdetect среди близких языков (детект → реальный целевой код).
    # Применяется ТОЛЬКО если детект-код сам не является целевым языком набора.
    _CONFUSION_ALIASES = {
        "sk": "cs",   # словацкий часто детектится для чешского (langdetect их путает)
    }

    @classmethod
    def get_voice(cls, name, lang_code):
        """voice_id для (набор, язык-код langdetect). С фолбэком по базовому коду и алиасам близких языков."""
        voices = cls.get_set(name)
        if not voices:
            return None
        code = (lang_code or "").lower().strip()
        if code in voices:
            return voices[code]
        base = code.split('-')[0]
        if base in voices:
            return voices[base]
        if base == "zh" and "zh-cn" in voices:
            return voices["zh-cn"]
        alias = cls._CONFUSION_ALIASES.get(code) or cls._CONFUSION_ALIASES.get(base)
        if alias and alias in voices:
            return voices[alias]
        return None


def _project_name_similarity(name1, name2):
    """Сравнивает два имени проекта. Возвращает 0.0-1.0.
    Удаляет языковые префиксы (en_, ru_, de_ и т.д.) перед сравнением.
    """
    import difflib
    def strip_lang(n):
        return re.sub(r'^[a-zA-Z]{2,3}[_\-\s]', '', n).strip()
    s1, s2 = strip_lang(name1), strip_lang(name2)
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    return difflib.SequenceMatcher(None, s1.lower(), s2.lower()).ratio()


def _split_into_n_chunks(sentences, n):
    """Распределяет предложения на РОВНО n групп максимально равномерно (для Idea-1 reuse-синхрона).
    Возвращает список из n строк (text_segment). Если предложений меньше n — хвостовые группы пустые."""
    n = max(1, int(n))
    sents = [s.strip() for s in (sentences or []) if s and str(s).strip()]
    if not sents:
        return [""] * n
    if len(sents) <= n:
        out = list(sents) + [""] * (n - len(sents))
        return out[:n]
    k, m = divmod(len(sents), n)   # m групп получат по k+1 предложению, остальные по k
    out, idx = [], 0
    for i in range(n):
        size = k + (1 if i < m else 0)
        out.append(' '.join(sents[idx:idx + size]))
        idx += size
    return out


def sanitize_project_name(name):
    """Приводит имя проекта (из имени текст-файла) к безопасному для пути и устойчивому:
    — убирает хвостовые/ведущие пробелы и хвостовые точки (источник рассинхрона папок);
    — заменяет символы, ломающие пути/дубли (/ \\ : ： * ? " < > | переводы строк);
    — схлопывает повторяющиеся пробелы; ограничивает длину (имена иногда обрезаны кривo).
    Без этого обрезанные/грязные имена дают разные папки на одно видео и [Errno 2] при озвучке."""
    name = (name or "").strip()
    for ch in ('/', '\\', ':', '：', '\n', '\t', '\r', '*', '?', '"', '<', '>', '|'):
        name = name.replace(ch, '_')
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.rstrip(' .')          # хвостовые пробелы и точки — главный виновник
    if len(name) > 80:
        name = name[:80].rstrip(' .')
    return name or "project"


class App:
    def __init__(self, root):
        self.root = root
        # Загружаем язык перед установкой заголовка
        current_lang = get_language()
        self.root.title(_('app_title'))
        self.root.geometry("1400x900")

        # Глобальный статус прогресса в заголовке окна (видимость долгих фаз).
        # Проекты пишут сюда последнюю строку прогресса; _poll_all_log_queues отражает её в title.
        self._last_progress_line = None
        self._last_progress_ts = 0.0
        self._title_progress_active = False   # сейчас в заголовке статус, а не дефолт
        self._title_stale_seconds = 30        # нет прогресса дольше → вернуть дефолтный заголовок

        # Fix 6 (May 2026) — handler закрытия окна: убить subprocess'ы перед exit,
        # иначе ffmpeg/yt-dlp остаются зомби, файл рендера может остаться corrupt.
        try:
            self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        except Exception:
            pass

        def _app_log(msg, kind="render"):
            # Fix 3 (May 2026) — пишем во ВСЕ проекты, не только в первый.
            # Раньше break после первого → 4 проекта из 5 не видели summary типа "Добавлено: 5".
            for _f in (getattr(self, "projects", None) or {}).values():
                try:
                    _f.log(msg, kind=kind)
                except Exception:
                    pass
        self._app_log = _app_log

        self.preset_manager = PresetManager()
        
        # Периодическая проверка лицензии (каждые 12 часов)
        self.last_license_check = time.time()
        self.license_check_interval = 12 * 60 * 60  # 12 часов в секундах
        self.schedule_license_check()

    def _on_closing(self):
        """Fix 6 (May 2026) — graceful shutdown: убить все subprocess'ы перед destroy().
        Иначе ffmpeg/yt-dlp остаются в фоне после закрытия exe, файл render может остаться corrupt."""
        try:
            projects = getattr(self, 'projects', None) or {}
            for proj in projects.values():
                for h_attr in ('tts_proc_handle', 'bing_proc_handle', 'googler_proc_handle',
                               'googler_video_proc_handle', 'render_proc_handle', 'thief_proc_handle'):
                    h = getattr(proj, h_attr, None)
                    if h and hasattr(h, 'poll') and h.poll() is None:
                        try:
                            h.terminate()
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def schedule_license_check(self):
        """Планирует следующую проверку лицензии через 12 часов."""
        def check_license_periodic():
            try:
                import license_guard
                # Проверяем лицензию (если не валидна - завершит программу)
                license_guard.check_license()
                # Обновляем время последней проверки
                self.last_license_check = time.time()
            except ImportError:
                # Если модуль не найден - пропускаем
                pass
            except Exception:
                # Если ошибка - пропускаем, но планируем следующую проверку
                pass
            finally:
                # Планируем следующую проверку через 12 часов
                self.root.after(int(self.license_check_interval * 1000), check_license_periodic)
        
        # Запускаем первую проверку через 12 часов
        self.root.after(int(self.license_check_interval * 1000), check_license_periodic)
        self.api_keys_manager = APIKeysManager()
        self.render_settings_manager = RenderSettingsManager()
        # Обновляем переменные окружения при запуске
        self.api_keys_manager.update_env_vars()
        try:
            max_parallel = self.render_settings_manager.get_setting('max_parallel_renders', MAX_PARALLEL_RENDERS)
            max_parallel = max(1, min(16, int(max_parallel) if max_parallel is not None else MAX_PARALLEL_RENDERS))
        except (TypeError, ValueError):
            max_parallel = MAX_PARALLEL_RENDERS
        self.render_queue = RenderQueueManager(max_parallel=max_parallel, queue_name="Render")
        self._load_ui_settings()
        
        # ДВЕ ОТДЕЛЬНЫЕ ОЧЕРЕДИ для разных движков TTS
        self.edgetts_queue = TTSQueueManager(max_parallel=MAX_PARALLEL_EDGETTS, queue_name="EdgeTTS")
        self.elevenlabs_queue = TTSQueueManager(max_parallel=MAX_PARALLEL_ELEVENLABS, queue_name="ElevenLabs")
        self.voicer_queue = TTSQueueManager(max_parallel=MAX_PARALLEL_VOICER, queue_name="Voicer")
        self.voicegencore_queue = TTSQueueManager(max_parallel=MAX_PARALLEL_VGC, queue_name="VoiceGenCore")

        # Загружаем настройки параллельности Googler
        googler_settings_path = os.path.join(os.path.expanduser("~"), "video_studio_googler_settings.json")
        googler_settings = {}
        if os.path.exists(googler_settings_path):
            try:
                with open(googler_settings_path, 'r', encoding='utf-8', errors='replace') as f:
                    googler_settings = json.load(f)
            except:
                pass
        
        # Очереди для Thief и Googler
        self.thief_queue = TTSQueueManager(max_parallel=MAX_PARALLEL_THIEF, queue_name="Thief")
        self.googler_queue = TTSQueueManager(max_parallel=MAX_PARALLEL_GOOGLER, queue_name="Googler")  # Для изображений
        video_projects_parallel = googler_settings.get('max_parallel_video_projects', MAX_PARALLEL_GOOGLER_VIDEO)
        self.googler_video_queue = TTSQueueManager(max_parallel=video_projects_parallel, queue_name="GooglerVideo")
        self._setup_queue_complete_callbacks()
        
        # Сохраняем настройки для передачи в worker
        self.googler_settings = googler_settings
        
        # Запоминаем текущую вкладку этапа (0=Проект, 1=Озвучка, 2=Изображения, 3=Рендер, 4=Вор)
        self.current_stage_tab = 0
        
        self.check_dependencies()
        
        # Тёмная тема
        self._apply_saved_theme()

        # Верхняя панель — только иконки, подсказка при наведении (вмещается на любом экране)
        top_bar = Frame(root)
        top_bar.pack(fill="x", padx=5, pady=5)
        
        self.top_bar_widgets = {}
        BTN_WIDTH = 3
        _theme_icon = "☀️" if getattr(self, '_current_theme', 'light') == "dark" else "🌙"
        
        self.top_bar_widgets['theme_btn'] = ttk.Button(top_bar, text=_theme_icon,
               command=self.toggle_theme, width=BTN_WIDTH)
        self.top_bar_widgets['theme_btn'].pack(side="left", padx=(0, 5))
        self._bind_tooltip(self.top_bar_widgets['theme_btn'], lambda: "Тема (светлая/тёмная)")
        Label(top_bar, text="│", font=("Arial", 12)).pack(side="left", padx=5)
        
        self.top_bar_widgets['new_project'] = ttk.Button(top_bar, text="➕",
               command=lambda: self.add_project(_('project_name_template').format(num=len(self.projects)+1)),
               width=BTN_WIDTH)
        self.top_bar_widgets['new_project'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['new_project'], lambda: _('new_project'))
        
        Label(top_bar, text="│", font=("Arial", 12)).pack(side="left", padx=5)
        
        self.top_bar_widgets['api_settings'] = ttk.Button(top_bar, text="🔑",
               command=self.open_api_settings, width=BTN_WIDTH)
        self.top_bar_widgets['api_settings'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['api_settings'], lambda: _('api_settings'))
        
        self.top_bar_widgets['render_settings'] = ttk.Button(top_bar, text="🎬",
               command=self.open_render_settings, width=BTN_WIDTH)
        self.top_bar_widgets['render_settings'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['render_settings'], lambda: _('render_settings'))
        
        self.top_bar_widgets['render_queue_btn'] = ttk.Button(top_bar, text="📋",
               command=self.open_all_queues_dialog, width=BTN_WIDTH)
        self.top_bar_widgets['render_queue_btn'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['render_queue_btn'], lambda: _('queues_all_btn'))
        
        Label(top_bar, text="│", font=("Arial", 12)).pack(side="left", padx=5)
        
        self.top_bar_widgets['tts_for_all'] = ttk.Button(top_bar, text="🎙️",
               command=self.run_tts_for_all, width=BTN_WIDTH)
        self.top_bar_widgets['tts_for_all'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['tts_for_all'], lambda: _('tts_for_all'))
        
        self.top_bar_widgets['render_for_all'] = ttk.Button(top_bar, text="▶️",
               command=self.run_render_for_all, width=BTN_WIDTH)
        self.top_bar_widgets['render_for_all'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['render_for_all'], lambda: _('render_for_all'))
        
        self.top_bar_widgets['images_for_all'] = ttk.Button(top_bar, text="🖼️",
               command=self.run_googler_images_for_all, width=BTN_WIDTH)
        self.top_bar_widgets['images_for_all'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['images_for_all'], lambda: _('images_for_all'))
        
        self.top_bar_widgets['videos_for_all'] = ttk.Button(top_bar, text="🎥",
               command=self.run_googler_video_for_all, width=BTN_WIDTH)
        self.top_bar_widgets['videos_for_all'].pack(side="left", padx=5)
        self._bind_tooltip(self.top_bar_widgets['videos_for_all'], lambda: "Видео для всех")
        
        Label(top_bar, text="│", font=("Arial", 12)).pack(side="left", padx=5)
        current_lang = get_language()
        self.lang_var = StringVar(value=current_lang)
        lang_frame = Frame(top_bar)
        lang_frame.pack(side="left", padx=5)
        Label(lang_frame, text="🌐", font=("Arial", 12)).pack(side="left", padx=(0, 3))
        lang_menu = ttk.Combobox(lang_frame, textvariable=self.lang_var,
                                 values=get_available_languages(),
                                 state="readonly", width=4)
        lang_menu.pack(side="left")
        lang_menu.bind("<<ComboboxSelected>>", self.on_language_changed)
        
        self._lang_menu = lang_menu
        
        self._update_button_colors(getattr(self, '_current_theme', 'light'))
        
        self.update_window_title()
        self.root.after(200, self._apply_theme_to_projects)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=(0,5))
        
        # Обработчик смены проекта - восстанавливаем вкладку этапа
        self.notebook.bind("<<NotebookTabChanged>>", self.on_project_changed)
        
        # Контекстное меню для вкладок
        self.tab_menu = Menu(root, tearoff=0)
        self.tab_menu.add_command(label=_('rename'), command=self.rename_selected_tab)
        self.tab_menu.add_command(label=_('open_folder'), command=self.open_selected_tab_folder)
        self.tab_menu.add_separator()
        self.tab_menu.add_command(label=_('move_left'), command=self.move_tab_left)
        self.tab_menu.add_command(label=_('move_right'), command=self.move_tab_right)
        self.tab_menu.add_separator()
        self.tab_menu.add_command(label=_('close_tab'), command=self.close_selected_tab)
        
        # Привязываем контекстное меню к вкладкам
        self.notebook.bind("<Button-3>", self.show_tab_menu)
        # Также привязываем к Control+Click (для Mac) и правый клик мыши
        self.notebook.bind("<Control-Button-1>", self.show_tab_menu)
        
        self.projects = {}
        self.add_project(_('project_name_template').format(num=1))
        
        # Периодическое обновление статусов очереди (5с — меньше нагрузки при многих проектах)
        self.update_all_queue_statuses()
        # Опрос логов раз в 500мс — реже перерисовка, меньше подлагиваний
        self.root.after(500, self._poll_all_log_queues)
    
    def _show_tooltip(self, text, widget):
        """Показать подсказку рядом с виджетом."""
        self._hide_tooltip()
        if not text:
            return
        try:
            if not widget.winfo_exists():
                return
            tw = Toplevel(self.root)
            tw.wm_overrideredirect(True)
            tw.wm_geometry("+0+0")
            lbl = Label(tw, text=text, font=("Arial", 9), bg="#ffffcc", fg="#000",
                        padx=6, pady=4, relief="solid", borderwidth=1)
            lbl.pack()
            tw.update_idletasks()
            x = widget.winfo_rootx() + widget.winfo_width() // 2 - lbl.winfo_reqwidth() // 2
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tw.wm_geometry(f"+{x}+{y}")
            self._tooltip_window = tw
        except Exception:
            pass

    def _hide_tooltip(self):
        """Скрыть подсказку."""
        if getattr(self, '_tooltip_job', None):
            try:
                self.root.after_cancel(self._tooltip_job)
            except Exception:
                pass
            self._tooltip_job = None
        if getattr(self, '_tooltip_window', None):
            try:
                self._tooltip_window.destroy()
            except Exception:
                pass
            self._tooltip_window = None

    def _bind_tooltip(self, widget, text_getter):
        """Привязать подсказку к виджету: текст при наведении через text_getter()."""
        def on_enter(e):
            self._tooltip_job = self.root.after(400, lambda: self._show_tooltip(text_getter(), widget))
        def on_leave(e):
            self._hide_tooltip()
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)

    def _apply_theme_to_projects(self):
        """Применить текущую тему к кнопкам во всех открытых проектах."""
        theme = getattr(self, '_current_theme', 'light')
        for proj in self.projects.values():
            try:
                if hasattr(proj, 'update_theme_colors'):
                    proj.update_theme_colors(theme)
            except Exception:
                pass

    def _get_theme_settings_path(self):
        return os.path.join(os.path.expanduser("~"), "video_studio_theme.json")

    def _update_button_colors(self, theme):
        """Обновить цвета кнопок верхней панели (если это tk.Button)."""
        if theme == "dark":
            colors = {
                'new_project': ("#3d5afe", "white"),
                'api_settings': ("#ef6c00", "white"),
                'render_settings': ("#7b1fa2", "white"),
                'render_queue_btn': ("#3949ab", "white"),
                'tts_for_all': ("#2e7d32", "white"),
                'render_for_all': ("#c62828", "white"),
                'images_for_all': ("#6a1b9a", "white"),
                'videos_for_all': ("#d84315", "white"),
                'theme_btn': ("#555555", "white"),
            }
        else:
            colors = {
                'new_project': ("#e8f0fe", "#000000"),
                'api_settings': ("#ff9800", "white"),
                'render_settings': ("#9c27b0", "white"),
                'render_queue_btn': ("#5c6bc0", "white"),
                'tts_for_all': ("#4CAF50", "white"),
                'render_for_all': ("#f44336", "white"),
                'images_for_all': ("#9C27B0", "white"),
                'videos_for_all': ("#FF5722", "white"),
                'theme_btn': ("#333333", "white"),
            }
        for key, (bg, fg) in colors.items():
            if key in self.top_bar_widgets:
                try:
                    w = self.top_bar_widgets[key]
                    if w.winfo_class() == 'Button':
                        w.config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
                except Exception:
                    pass

    def _apply_saved_theme(self):
        """Применить сохранённую тему при запуске."""
        theme = "light"
        try:
            path = self._get_theme_settings_path()
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    theme = json.load(f).get('theme', 'light')
        except Exception:
            pass
        self._current_theme = theme
        if SV_TTK_AVAILABLE:
            try:
                sv_ttk.set_theme(theme)
            except Exception:
                pass

    def toggle_theme(self):
        """Переключить светлую/тёмную тему."""
        global SV_TTK_AVAILABLE, sv_ttk
        if not SV_TTK_AVAILABLE:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "sv-ttk", "-q"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      startupinfo=get_subprocess_flags())
                import sv_ttk as _sv
                sv_ttk = _sv
                SV_TTK_AVAILABLE = True
            except Exception:
                return
        new_theme = "dark" if self._current_theme == "light" else "light"
        self._current_theme = new_theme
        sv_ttk.set_theme(new_theme)
        self._update_button_colors(new_theme)
        btn_text = "☀️" if new_theme == "dark" else "🌙"
        if 'theme_btn' in self.top_bar_widgets:
            self.top_bar_widgets['theme_btn'].config(text=btn_text)
        self._apply_theme_to_projects()
        try:
            with open(self._get_theme_settings_path(), 'w', encoding='utf-8', errors='replace') as f:
                json.dump({'theme': new_theme}, f)
        except Exception:
            pass

    def update_window_title(self):
        """Обновить заголовок окна"""
        self.root.title(_('app_title'))
    
    def on_language_changed(self, event=None):
        """Обработчик смены языка"""
        new_lang = self.lang_var.get()
        set_language(new_lang)
        self.update_window_title()
        self.update_ui_language()
        self._app_log(_('language_changed').format(lang=new_lang.upper()), kind="render")
    
    def update_ui_language(self):
        """Обновляет все тексты интерфейса при смене языка"""
        # Верхняя панель — только иконки, подсказки при наведении подхватят новый язык сами
        
        # Обновляем контекстное меню вкладок
        if hasattr(self, 'tab_menu'):
            self.tab_menu.delete(0, 'end')
            self.tab_menu.add_command(label=_('rename'), command=self.rename_selected_tab)
            self.tab_menu.add_command(label=_('open_folder'), command=self.open_selected_tab_folder)
            self.tab_menu.add_separator()
            self.tab_menu.add_command(label=_('move_left'), command=self.move_tab_left)
            self.tab_menu.add_command(label=_('move_right'), command=self.move_tab_right)
            self.tab_menu.add_separator()
            self.tab_menu.add_command(label=_('close_tab'), command=self.close_selected_tab)
        
        # Обновляем все проекты
        for project_frame in self.projects.values():
            if hasattr(project_frame, 'update_ui_language'):
                project_frame.update_ui_language()
    
    def on_project_changed(self, event=None):
        """При смене проекта - восстанавливаем запомненную вкладку этапа"""
        try:
            tab_id = self.notebook.select()
            if tab_id:
                project_frame = self.notebook.nametowidget(tab_id)
                if hasattr(project_frame, 'stages_notebook'):
                    # Восстанавливаем вкладку
                    project_frame.stages_notebook.select(self.current_stage_tab)
        except:
            pass
    
    def save_current_stage_tab(self, tab_index):
        """Сохранить текущую вкладку этапа"""
        self.current_stage_tab = tab_index
    
    def update_all_queue_statuses(self):
        """Периодически обновляем статусы очереди во всех проектах. Интервал 5с — снижает нагрузку на UI при многих проектах."""
        for project in self.projects.values():
            try:
                project.update_queue_status()
            except:
                pass
        # Отложенный рендер: если проект ждёт завершения всех процессов, проверяем каждые 5с
        # (колбэки очередей вызывают _notify_task_complete; прямые процессы tts/googler/bing — нет, поэтому дублируем проверку здесь)
        for proj in self.projects.values():
            try:
                if getattr(proj, '_deferred_render_pending', False):
                    active = proj._has_active_processes()
                    print(f"[DEBUG-RENDER] TRIGGER (poll 5s) project='{proj.project_name}' pending=True active={active}")
                    if not active:
                        print(f"[DEBUG-RENDER]   → FIRING render for '{proj.project_name}' (poll)")
                        proj._deferred_render_pending = False
                        proj.log("✅ Все процессы завершены — запускаю рендер!", kind="render")
                        proj.run_video_render()
            except Exception as _e:
                print(f"[DEBUG-RENDER] TRIGGER (poll 5s) exception for '{getattr(proj, 'project_name', '?')}': {_e}")
        self.root.after(5000, self.update_all_queue_statuses)
    
    def _poll_all_log_queues(self):
        """Единый опрос логов всех проектов каждые 500мс — меньше перерисовок интерфейса."""
        for project in self.projects.values():
            try:
                project._drain_log_queue()
            except Exception:
                pass
        self._update_title_status()
        self.root.after(500, self._poll_all_log_queues)

    def _update_title_status(self):
        """Отражает последнюю строку прогресса в заголовке окна; по простою — дефолтный заголовок.
        Только дополнительная видимость, на логику/процессы не влияет."""
        try:
            now = time.time()
            line = self._last_progress_line
            fresh = bool(line) and (now - self._last_progress_ts) < self._title_stale_seconds
            if fresh:
                # Чистим: убираем таймкод [HH:MM:SS.mmm] и ведущие эмодзи
                label = re.sub(r'^\[\d\d:\d\d:\d\d\.\d+\]\s*', '', line)
                label = label.lstrip('⏳ℹ️✅⚠️❌🔧🎬🎨🔄🎲 ️').strip()
                if len(label) > 80:
                    label = label[:80] + '…'
                title = f"{label} — {_('app_title')}"
                if title != getattr(self, '_current_title_text', None):
                    self.root.title(title)
                    self._current_title_text = title
                self._title_progress_active = True
            elif self._title_progress_active:
                # Прогресс затих → вернуть дефолтный заголовок
                self.root.title(_('app_title'))
                self._current_title_text = _('app_title')
                self._title_progress_active = False
        except Exception:
            pass

    def run_tts_for_all(self):
        """Запустить озвучку для всех открытых проектов через правильные очереди"""
        if not self.projects:
            self._app_log("ℹ️ Нет открытых проектов", kind="render")
            return
        
        edgetts_count = 0
        elevenlabs_count = 0
        voicer_count = 0
        voicegencore_count = 0

        for project_name, project_frame in self.projects.items():
            source_file = project_frame.source_text_file.get()
            if source_file and os.path.exists(source_file):
                engine = project_frame.tts_engine.get()
                if engine == "elevenlabs":
                    success, message = self.elevenlabs_queue.add_to_queue(project_name, project_frame._run_tts_worker_full_text)
                    project_frame.log(f"\n[Очередь ElevenLabs] {message}", kind="tts")
                    if success:
                        elevenlabs_count += 1
                elif engine == "voicer":
                    success, message = self.voicer_queue.add_to_queue(project_name, project_frame._run_tts_worker_full_text)
                    project_frame.log(f"\n[Очередь Voicer] {message}", kind="tts")
                    if success:
                        voicer_count += 1
                elif engine == "voicegencore":
                    success, message = self.voicegencore_queue.add_to_queue(project_name, project_frame._run_tts_worker_full_text)
                    project_frame.log(f"\n[Очередь VoiceGenCore] {message}", kind="tts")
                    if success:
                        voicegencore_count += 1
                else:
                    success, message = self.edgetts_queue.add_to_queue(project_name, project_frame._run_tts_worker_full_text)
                    project_frame.log(f"\n[Очередь EdgeTTS] {message}", kind="tts")
                    if success:
                        edgetts_count += 1
        
        total = edgetts_count + elevenlabs_count + voicer_count + voicegencore_count

        if total > 0:
            edgetts_status = self.edgetts_queue.get_status()
            elevenlabs_status = self.elevenlabs_queue.get_status()
            voicer_status = self.voicer_queue.get_status()
            msg = f"Добавлено проектов: {total}\n\n"
            if edgetts_count > 0:
                msg += f"EdgeTTS:\n  Активных: {edgetts_status['active']}\n  В очереди: {edgetts_status['queued']}\n  Макс: {edgetts_status['max']}\n\n"
            if elevenlabs_count > 0:
                msg += f"ElevenLabs:\n  Активных: {elevenlabs_status['active']}\n  В очереди: {elevenlabs_status['queued']}\n  Макс: {elevenlabs_status['max']}\n\n"
            if voicer_count > 0:
                msg += f"Voicer:\n  Активных: {voicer_status['active']}\n  В очереди: {voicer_status['queued']}\n  Макс: {voicer_status['max']}\n\n"
            if voicegencore_count > 0 and getattr(self, 'voicegencore_queue', None):
                vgc_status = self.voicegencore_queue.get_status()
                msg += f"VoiceGenCore:\n  Активных: {vgc_status['active']}\n  В очереди: {vgc_status['queued']}\n  Макс: {vgc_status['max']}"
            self._app_log(msg, kind="tts")
        else:
            self._app_log("⚠️ Ни в одном проекте не выбран текстовый файл", kind="tts")

    
    def run_googler_images_for_all(self):
        """Queue an independent image generation for every runnable project."""
        if not self.projects:
            self._app_log("ℹ️ Нет открытых проектов", kind="render")
            return

        def _can_run(frame):
            source_file = frame.source_text_file.get()
            prompt_mode = str(frame.image_prompt_mode.get() or "").strip().lower()
            if prompt_mode == "gpt" and source_file and os.path.exists(source_file):
                return True
            if prompt_mode == "custom" and hasattr(frame, 'googler_custom_prompts_text'):
                custom_text = frame.googler_custom_prompts_text.get("1.0", "end").strip()
                if [b for b in re.split(r'\n\s*\n', custom_text) if b.strip()]:
                    return True
            if prompt_mode == "existing":
                if os.path.exists(os.path.join(frame.project_dir, "googler", "prompts.json")):
                    return True
            return False

        def _warn_cant_run(frame):
            prompt_mode = str(frame.image_prompt_mode.get() or "").strip().lower()
            if prompt_mode == "gpt":
                frame.log("⚠️ Выберите текстовый файл для генерации по GPT промтам", kind="images")
            elif prompt_mode == "custom":
                frame.log("⚠️ Введите кастомные промты (один абзац = один промт)", kind="images")
            elif prompt_mode == "existing":
                frame.log("⚠️ Создайте googler/prompts.json или выберите другой режим промптов", kind="images")
            else:
                frame.log(f"❌ Неизвестный режим промптов: {prompt_mode or '—'}", kind="images")

        def _queue(name, frame):
            if not self.googler_queue:
                return False
            if not frame._prepare_legacy_cache_consent():
                return False
            success, message = self.googler_queue.add_to_queue(name + "_images", frame._run_googler_images_worker)
            frame.log(f"\n[Очередь изображений] {message}", kind="images")
            if not success:
                frame._trust_legacy_image_cache_once = False
                frame._trust_legacy_image_cache_snapshot = ()
            return bool(success)

        items = list(self.projects.items())
        count = 0
        stale_requests = []
        for name, frame in items:
            variable = getattr(frame, 'googler_copy_similar', None)
            try:
                if variable is not None and variable.get():
                    stale_requests.append(name)
                    variable.set(False)
            except Exception:
                pass
            if _can_run(frame):
                if _queue(name, frame):
                    count += 1
            else:
                _warn_cant_run(frame)

        if stale_requests:
            self._app_log(
                "🛡️ Копирование картинок между проектами BLOCKED: "
                f"{CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON}. "
                "Все проекты поставлены в собственную генерацию.",
                kind="images",
            )

        if count > 0:
            msg = f"Запущена генерация изображений: {count} проект(ов)"
            if self.googler_queue:
                st = self.googler_queue.get_status()
                msg += f"\n\nОчередь:\n  Активных: {st['active']}\n  В очереди: {st['queued']}\n  Макс: {st['max']}"
            self._app_log(msg, kind="images")
        else:
            self._app_log("⚠️ Ни в одном проекте не выполнены условия: выберите файл (GPT) или кастомные промты, либо prompts.json", kind="images")
    
    def run_googler_video_for_all(self):
        """Запустить генерацию видео для всех открытых проектов с текущими настройками"""
        if not self.projects:
            self._app_log("ℹ️ Нет открытых проектов", kind="render")
            return
        
        count = 0
        skipped_no_images = 0
        skipped_no_prompt = 0
        deferred = 0

        for project_name, project_frame in self.projects.items():
            # Проверяем, есть ли промт для видео
            prompt = project_frame.googler_video_prompt.get().strip()
            if not prompt:
                skipped_no_prompt += 1
                project_frame.log("⚠️ Не указан промт для видео", kind="images")
                continue

            # «После картинок»: если у проекта галка ON и его картинки ещё генерятся —
            # откладываем видео (стартанёт само после завершения картинок), а не ставим в очередь сейчас.
            if project_frame.googler_video_after_images.get() and getattr(self, 'googler_queue', None):
                try:
                    img_prefix = project_name + "_images"
                    active = False
                    with self.googler_queue.lock:
                        if any(n == img_prefix for n in self.googler_queue.active_tts.keys()):
                            active = True
                        elif any(n == img_prefix for n, _ in self.googler_queue.queue):
                            active = True
                    if active:
                        project_frame._deferred_video_pending = True
                        project_frame.log("⏳ Видео отложено — жду завершения картинок этого проекта", kind="images")
                        deferred += 1
                        continue
                except Exception:
                    pass

            # Режим t2v «из промптов» — картинки не нужны, гейт пропускаем
            _from_prompts = bool(getattr(project_frame, 'googler_video_from_prompts', None)
                                 and project_frame.googler_video_from_prompts.get())

            # Проверяем, есть ли выбранные изображения или настройка автоматического выбора
            first_n_str = project_frame.googler_video_first_n.get().strip()
            has_auto_setting = first_n_str and first_n_str != "0"
            has_selected_images = project_frame.googler_selected_images and len(project_frame.googler_selected_images) > 0

            if not _from_prompts and not has_auto_setting and not has_selected_images:
                skipped_no_images += 1
                project_frame.log("⚠️ Не выбраны изображения для генерации видео. Выберите вручную или используйте автоматический выбор первых X", kind="images")
                continue

            # Если есть настройка автоматического выбора, но нет выбранных изображений - используем авто-выбор
            if not _from_prompts and has_auto_setting and not has_selected_images:
                try:
                    first_n = int(first_n_str)
                    if first_n > 0 and project_frame._select_first_n_images(first_n) > 0:
                        has_selected_images = True
                except ValueError:
                    pass

            if not _from_prompts and not has_selected_images:
                skipped_no_images += 1
                project_frame.log("⚠️ Не найдено картинок для генерации видео", kind="images")
                continue
            
            # Добавляем в очередь генерации видео
            if self.googler_video_queue:
                success, message = self.googler_video_queue.add_to_queue(project_name + "_video", project_frame._run_googler_video_worker)
                project_frame.log(f"\n[Очередь видео] {message}", kind="images")
                if success:
                    count += 1
        
        if count > 0 or deferred > 0:
            video_status = self.googler_video_queue.get_status() if self.googler_video_queue else {}
            msg = f"Добавлено в очередь видео: {count}\n"
            if deferred > 0:
                msg += f"⏳ Отложено (после картинок): {deferred} — стартуют сами по завершении картинок\n"
            msg += "\n"
            msg += f"Генерация видео:\n"
            msg += f"  В очереди: {video_status.get('queued', 0)}\n"
            msg += f"  Активных: {video_status.get('active', 0)}\n"
            msg += f"  Макс одновременно: {video_status.get('max', 0)}"
            if skipped_no_prompt > 0 or skipped_no_images > 0:
                msg += f"\n\nПропущено:\n"
                if skipped_no_prompt > 0:
                    msg += f"  Без промта: {skipped_no_prompt}\n"
                if skipped_no_images > 0:
                    msg += f"  Без изображений: {skipped_no_images}"
            self._app_log(msg, kind="images")
        else:
            msg = "Ни в одном проекте не найдено условий для генерации видео.\n\n"
            if skipped_no_prompt > 0:
                msg += f"Без промта: {skipped_no_prompt}\n"
            if skipped_no_images > 0:
                msg += f"Без изображений: {skipped_no_images}"
            self._app_log(msg, kind="images")
    
    
    def run_render_for_all(self):
        """Запустить рендер для всех открытых проектов с проверкой файлов"""
        print(f"[DEBUG-RENDER] run_render_for_all START, total projects: {len(self.projects)}")
        if not self.projects:
            self._app_log("ℹ️ Нет открытых проектов", kind="render")
            return

        count = 0
        skipped = []

        for project_name, project_frame in self.projects.items():
            # Проверяем, есть ли аудио и изображения (рекурсивно по всей папке проекта)
            project_dir = project_frame.project_dir
            project_dir_normalized = os.path.normpath(os.path.abspath(project_dir))
            
            # Ищем аудио и изображения рекурсивно
            audio_files = []
            image_files = []
            
            # Служебные папки для исключения
            assets_dir = os.path.join(project_dir_normalized, "_ASSETS")
            assets_dir_normalized = os.path.normpath(assets_dir)
            
            # Поддерживаемые форматы
            SUPPORTED_AUDIO_FORMATS = ['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac']
            SUPPORTED_IMAGE_FORMATS = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.ico', '.gif']
            
            try:
                for root, dirs, files in os.walk(project_dir_normalized):
                    # Пропускаем служебные папки
                    root_normalized = os.path.normpath(root)
                    if root_normalized == assets_dir_normalized:
                        dirs[:] = []
                        continue
                    
                    # Исключаем служебные папки из дальнейшего обхода
                    dirs[:] = [d for d in dirs if not d.startswith('.') and 'temp' not in d.lower() and d != '_ASSETS']
                    
                    for f in files:
                        if f.startswith('.') or 'temp' in f.lower() or 'final_video' in f.lower():
                            continue
                        ext = os.path.splitext(f)[1].lower()
                        full_path = os.path.join(root, f)
                        if ext in SUPPORTED_AUDIO_FORMATS:
                            audio_files.append(full_path)
                        elif ext in SUPPORTED_IMAGE_FORMATS:
                            image_files.append(full_path)
            except Exception as e:
                print(f"Ошибка при поиске файлов в проекте {project_name}: {e}")
            
            # Учитываем доп. папку материалов (os.walk для подпапок и видео)
            extra_dir = project_frame.extra_materials_dir.get()
            if extra_dir and os.path.isdir(extra_dir):
                try:
                    for root, dirs, files in os.walk(extra_dir):
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                        for f in files:
                            if f.startswith('.'):
                                continue
                            ext = os.path.splitext(f)[1].lower()
                            if ext in SUPPORTED_IMAGE_FORMATS or ext in ['.mp4', '.mov', '.avi', '.mkv', '.webm']:
                                full = os.path.normpath(os.path.join(root, f))
                                if full not in image_files:
                                    image_files.append(full)
                except Exception:
                    pass
            
            print(f"[DEBUG-RENDER]   checking '{project_name}': audio={len(audio_files)}, image={len(image_files)}")
            # Гейт полноты озвучки в отложенном/пакетном пути (он НЕ вызывает check_render_readiness):
            # частичная озвучка не должна молча уходить в рендер и помечаться "готово".
            if len(audio_files) > 0 and len(image_files) > 0:
                try:
                    comp = project_frame.check_audio_completeness()
                except Exception as _e:
                    print(f"[audio-gate] batch check skipped for '{project_name}': {_e}")
                    comp = None
                if comp and comp.get("checked") and not comp.get("ok"):
                    print(f"[DEBUG-RENDER]   → BLOCKED '{project_name}': audio incomplete ({comp.get('pct')}%)")
                    project_frame.log(comp.get("msg") or "⚠️ Озвучка неполная — рендер остановлен.", kind="render")
                    skipped.append(f"{project_name}: озвучка неполная (~{comp.get('pct')}%)")
                    continue

            # Проверяем что есть файлы — добавляем напрямую в очередь (без messagebox)
            if len(audio_files) > 0 and len(image_files) > 0:
                print(f"[DEBUG-RENDER]   → ADDING '{project_name}' to render_queue")
                success, message = self.render_queue.add_to_queue(project_name, project_frame._run_video_render_worker)
                project_frame.log(f"\n📊 [Рендер для всех] {message}", kind="render")
                project_frame.update_queue_status()
                if success:
                    count += 1
                else:
                    print(f"[DEBUG-RENDER]   → add_to_queue FAILED for '{project_name}': {message}")
                    skipped.append(f"{project_name}: {message}")
            else:
                reasons = []
                if len(audio_files) == 0:
                    reasons.append("нет аудио файлов")
                if len(image_files) == 0:
                    reasons.append("нет изображений")
                # Fix 1 (May 2026) — batch deferred render: если чекбокс ON, ВСЕГДА pending
                # (раньше skip когда нет файлов И нет активности — юзер думал "не работает").
                # Триггер каждые 5с (update_all_queue_statuses) запустит рендер когда материалы
                # появятся независимо от того, идёт ли генерация прямо сейчас.
                _def_flag = bool(getattr(project_frame, 'deferred_render', None) and project_frame.deferred_render.get())
                print(f"[DEBUG-RENDER]   '{project_name}' files missing ({reasons}); deferred={_def_flag}")
                if _def_flag:
                    project_frame._deferred_render_pending = True
                    print(f"[DEBUG-RENDER]   → DEFERRED PENDING '{project_name}' (ждём генерации)")
                    project_frame.log("⏳ Рендер отложен — запустим автоматически, как только материалы будут готовы", kind="render")
                    count += 1  # учитываем как принятого
                else:
                    print(f"[DEBUG-RENDER]   → SKIPPED '{project_name}': {reasons}")
                    skipped.append(f"{project_name}: {', '.join(reasons)}")

        print(f"[DEBUG-RENDER] run_render_for_all END: added={count}, skipped={len(skipped)}")
        if count > 0:
            render_status = self.render_queue.get_status()
            msg = f"Добавлено проектов: {count}\n\n"
            msg += f"Рендер:\n  Активных: {render_status['active']}\n  В очереди: {render_status['queued']}\n  Макс: {render_status['max']}"
            if skipped:
                msg += f"\n\nПропущено {len(skipped)} проектов:\n" + "\n".join(skipped[:5])
                if len(skipped) > 5:
                    msg += f"\n... и еще {len(skipped) - 5}"
            self._app_log(msg, kind="render")
        else:
            msg = "Ни в одном проекте нет аудио и изображений"
            if skipped:
                msg += "\n\nПричины:\n" + "\n".join(skipped[:5])
            self._app_log(msg, kind="render")
    
    def check_dependencies(self):
        missing = []
        
        # Проверка ffmpeg - через imageio-ffmpeg или системный
        ffmpeg_found = False
        if shutil.which("ffmpeg"):
            ffmpeg_found = True
        else:
            # Проверяем через imageio-ffmpeg
            try:
                import imageio_ffmpeg
                ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                if ffmpeg_path and os.path.exists(ffmpeg_path):
                    ffmpeg_found = True
            except ImportError:
                pass
            except Exception:
                pass
        
        if not ffmpeg_found:
            missing.append("FFmpeg (установите через imageio-ffmpeg или системный)")

        # Проверка ffprobe (нужен для измерения длительности аудио/видео — иначе тайминги ломаются)
        ffprobe_found = bool(shutil.which("ffprobe"))
        if not ffprobe_found:
            try:
                import portable_paths as _pp
                _fp = _pp.get_ffprobe_executable()
                if _fp and (os.path.exists(_fp) or shutil.which(_fp)):
                    ffprobe_found = True
            except Exception:
                pass
        if not ffprobe_found:
            try:
                import imageio_ffmpeg as _iio
                _ff = _iio.get_ffmpeg_exe()
                if _ff and os.path.exists(_ff):
                    _cand = os.path.join(os.path.dirname(_ff),
                                         "ffprobe.exe" if sys.platform == 'win32' else "ffprobe")
                    if os.path.exists(_cand):
                        ffprobe_found = True
            except Exception:
                pass
        if not ffprobe_found:
            missing.append("ffprobe (положите в src/bin/ или установите ffmpeg в PATH)")

        # Ключевые pip-пакеты: без них воркеры падают с ModuleNotFoundError
        _REQUIRED_PYMODULES = [
            ("cv2", "pip install opencv-python"),
            ("PIL", "pip install pillow"),
            ("numpy", "pip install numpy"),
            ("requests", "pip install requests"),
            ("imagehash", "pip install imagehash"),
        ]
        for _mod, _cmd in _REQUIRED_PYMODULES:
            try:
                importlib.import_module(_mod)
            except Exception:
                missing.append(f"{_mod} ({_cmd})")

        # Проверка edge-tts - через Python API или CLI
        edgetts_found = False
        try:
            import edge_tts
            edgetts_found = True
        except ImportError:
            pass
        
        # Также проверяем CLI команду
        if not edgetts_found:
            # Проверяем в стандартных местах установки Python скриптов
            import site
            user_site = site.getusersitepackages()
            scripts_dir = os.path.join(os.path.dirname(user_site), "Scripts")
            edge_tts_exe = os.path.join(scripts_dir, "edge-tts.exe")
            if os.path.exists(edge_tts_exe):
                edgetts_found = True
        
        if not edgetts_found and not shutil.which("edge-tts"):
            missing.append("edge-tts (pip install edge-tts)")
        
        if not WHISPER_AVAILABLE:
            self.root.after(1000, lambda: self._app_log("⚠️ Whisper не установлен - субтитры недоступны. Установите: pip install openai-whisper", kind="render"))
        
        if not LANGDETECT_AVAILABLE:
            self.root.after(2000, lambda: self._app_log("ℹ️ Для автоопределения языка установите: pip install langdetect", kind="render"))
        
        if missing:
            self._app_log("❌ Отсутствуют зависимости:\n\n" + "\n".join(missing), kind="render")
    
    def _unique_project_name(self, name):
        """Гарантирует уникальность имени среди открытых проектов (добавляет _2/_3 при коллизии).
        Без этого два текста с одинаковым (обрезанным) именем делят одну папку и затирают друг друга."""
        existing = set(self.projects.keys()) if hasattr(self, 'projects') else set()
        if name not in existing:
            return name
        i = 2
        while f"{name}_{i}" in existing:
            i += 1
        return f"{name}_{i}"

    def add_project(self, project_name):
        current_project = None
        try:
            current_tab = self.notebook.index(self.notebook.select())
            current_project_name = self.notebook.tab(current_tab, "text")
            current_project = self.projects.get(current_project_name)
        except Exception:
            pass
        project_name = self._unique_project_name(sanitize_project_name(project_name))
        target_project_dir = os.path.normpath(
            os.path.join(BASE_DIR_ROOT, project_name)
        )
        target_dir_existed = os.path.isdir(target_project_dir)
        project_frame = ProjectFrame(self.notebook, project_name, self)
        self.notebook.add(project_frame, text=project_name)
        self.projects[project_name] = project_frame
        
        # Сразу применить текущую тему к кнопкам нового проекта
        try:
            theme = getattr(self, '_current_theme', 'light')
            if hasattr(project_frame, 'update_theme_colors'):
                project_frame.update_theme_colors(theme)
        except Exception:
            pass
        
        # НОВОЕ: Копируем настройки из текущего активного проекта
        try:
            if current_project is not None:
                settings = current_project.get_current_settings()
                project_frame.apply_settings(settings)
                project_frame._initialize_references_from_project(
                    current_project, target_dir_existed=target_dir_existed
                )
                # Показываем в комбобоксе тот же пресет, что выбран в активном проекте
                preset_name = current_project.preset_combo.get()
                if preset_name and preset_name in project_frame.preset_combo['values']:
                    project_frame.preset_combo.set(preset_name)
        except:
            pass
        
        # Автозагрузка голосового маппинга для проекта
        try:
            project_frame._auto_load_voice_mapping()
        except Exception:
            pass
        
        # Устанавливаем запомненную вкладку этапа
        try:
            project_frame.stages_notebook.select(self.current_stage_tab)
        except:
            pass
    
    def remove_project(self, project_name):
        """Удалить проект (закрыть вкладку)"""
        if project_name not in self.projects:
            return
        
        # Не даём закрыть последний проект
        if len(self.projects) <= 1:
            self._app_log("⚠️ Нельзя закрыть последний проект. Создайте новый проект перед закрытием этого.", kind="render")
            return
        
        project_frame = self.projects[project_name]
        
        # Удаляем вкладку из notebook
        try:
            self.notebook.forget(project_frame)
        except:
            pass
        
        # Удаляем из словаря проектов
        del self.projects[project_name]
        
        # Уничтожаем фрейм
        try:
            project_frame.destroy()
        except:
            pass
    
    def show_tab_menu(self, event):
        """Показать контекстное меню при правом клике на вкладке"""
        # Определяем, на какой вкладке был клик
        try:
            # Проверяем, что клик был именно на вкладке, а не на содержимом
            clicked_tab = self.notebook.index(f"@{event.x},{event.y}")
            if clicked_tab >= 0:
                self.notebook.select(clicked_tab)  # Выбираем эту вкладку
                self._right_clicked_tab = clicked_tab
                # Показываем меню
                try:
                    self.tab_menu.tk_popup(event.x_root, event.y_root)
                except Exception as e:
                    # Если tk_popup не работает, используем post
                    try:
                        self.tab_menu.post(event.x_root, event.y_root)
                    except:
                        # Последняя попытка - просто показать меню
                        print(f"Не удалось показать меню: {e}")
        except Exception as e:
            # Отладочная информация (можно убрать после проверки)
            print(f"Ошибка при показе меню: {e}")
            import traceback
            traceback.print_exc()
    
    def close_selected_tab(self):
        """Закрыть вкладку из контекстного меню"""
        try:
            tab_id = self.notebook.select()
            project_frame = self.notebook.nametowidget(tab_id)
            project_frame.close_project()
        except:
            pass
    
    def open_selected_tab_folder(self):
        """Открыть папку проекта из контекстного меню"""
        try:
            tab_id = self.notebook.select()
            project_frame = self.notebook.nametowidget(tab_id)
            project_frame.open_project_folder()
        except:
            pass
    
    def rename_selected_tab(self):
        """Переименовать проект из контекстного меню"""
        try:
            tab_id = self.notebook.select()
            project_frame = self.notebook.nametowidget(tab_id)
            old_name = project_frame.project_name
            
            # Запрашиваем новое имя
            from tkinter import simpledialog
            new_name = simpledialog.askstring(
                "Переименовать проект",
                f"Введите новое имя для проекта '{old_name}':",
                initialvalue=old_name
            )
            
            if new_name and new_name.strip() and new_name != old_name:
                new_name = new_name.strip()
                # Проверяем что имя не занято
                if new_name in self.projects:
                    self.log(f"Проект с именем '{new_name}' уже существует!", kind="render")
                    return
                
                # Обновляем словарь проектов
                self.projects[new_name] = self.projects.pop(old_name)
                
                # Обновляем имя в project_frame
                project_frame.project_name = new_name
                
                # Обновляем текст вкладки
                self.notebook.tab(tab_id, text=new_name)
                
                self.log(f"Проект переименован в '{new_name}'", kind="render")
        except Exception as e:
            self.log(f"Не удалось переименовать проект: {e}", kind="render")
    
    def move_tab_left(self):
        """Переместить текущую вкладку влево"""
        try:
            tab_id = self.notebook.select()
            if not tab_id:
                return
            
            current_index = self.notebook.index(tab_id)
            if current_index <= 0:
                return  # Уже слева
            
            # Получаем widget и текст
            widget = self.notebook.nametowidget(tab_id)
            text = self.notebook.tab(tab_id, "text")
            
            # Удаляем и вставляем на позицию влево
            self.notebook.forget(tab_id)
            self.notebook.insert(current_index - 1, widget, text=text)
            self.notebook.select(widget)
        except Exception as e:
            self.log(f"Не удалось переместить вкладку: {e}", kind="render")
    
    def move_tab_right(self):
        """Переместить текущую вкладку вправо"""
        try:
            tab_id = self.notebook.select()
            if not tab_id:
                return
            
            current_index = self.notebook.index(tab_id)
            total_tabs = self.notebook.index("end")
            
            if current_index >= total_tabs - 1:
                return  # Уже справа
            
            # Получаем widget и текст
            widget = self.notebook.nametowidget(tab_id)
            text = self.notebook.tab(tab_id, "text")
            
            # Удаляем и вставляем на позицию вправо
            self.notebook.forget(tab_id)
            self.notebook.insert(current_index + 1, widget, text=text)
            self.notebook.select(widget)
        except Exception as e:
            self.log(f"Не удалось переместить вкладку: {e}", kind="render")
    
    def open_api_settings(self):
        """Открыть окно настроек API ключей"""
        dialog = Toplevel(self.root)
        dialog.title(_('api_settings_title'))
        dialog.geometry("600x420")
        dialog.transient(self.root)
        dialog.grab_set()
        
        # Заголовок
        Label(dialog, text=_('api_settings_header'), font=("Arial", 14, "bold")).pack(pady=10)
        Label(dialog, text=_('api_keys_info'), 
              font=("Arial", 9), fg="gray").pack(pady=(0, 10))
        
        # Контейнер со скроллом
        container = Frame(dialog)
        container.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        canvas = Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = Frame(canvas)
        canvas_window_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_window_id, width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)
        
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _on_dialog_destroy(event):
            if event.widget == dialog:
                try:
                    canvas.unbind_all("<MouseWheel>")
                except Exception:
                    pass
        dialog.bind("<Destroy>", _on_dialog_destroy)
        
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        
        fields_frame = scrollable_frame
        
        # Googler API Key
        googler_frame = Frame(fields_frame)
        googler_frame.pack(fill="x", pady=10)
        Label(googler_frame, text=_('googler_api_key'), width=20, anchor="w").pack(side="left")
        googler_var = StringVar(value=self.api_keys_manager.get_key('googler'))
        googler_entry = Entry(googler_frame, textvariable=googler_var, show="*", width=40)
        googler_entry.pack(side="left", padx=5)
        googler_show_var = BooleanVar()
        googler_show_btn = Button(googler_frame, text="👁️", width=3, 
                                  command=lambda: googler_entry.config(show="" if googler_show_var.get() else "*"))
        googler_show_btn.pack(side="left")
        Checkbutton(googler_frame, text=_('show'), variable=googler_show_var,
                   command=lambda: googler_entry.config(show="" if googler_show_var.get() else "*")).pack(side="left", padx=5)
        
        # OpenAI API Key
        openai_frame = Frame(fields_frame)
        openai_frame.pack(fill="x", pady=10)
        Label(openai_frame, text=_('openai_api_key'), width=20, anchor="w").pack(side="left")
        openai_var = StringVar(value=self.api_keys_manager.get_key('openai'))
        openai_entry = Entry(openai_frame, textvariable=openai_var, show="*", width=40)
        openai_entry.pack(side="left", padx=5)
        openai_show_var = BooleanVar()
        openai_show_btn = Button(openai_frame, text="👁️", width=3,
                                command=lambda: openai_entry.config(show="" if openai_show_var.get() else "*"))
        openai_show_btn.pack(side="left")
        Checkbutton(openai_frame, text=_('show'), variable=openai_show_var,
                   command=lambda: openai_entry.config(show="" if openai_show_var.get() else "*")).pack(side="left", padx=5)
        
        # ElevenLabs API Key
        elevenlabs_frame = Frame(fields_frame)
        elevenlabs_frame.pack(fill="x", pady=10)
        Label(elevenlabs_frame, text=_('elevenlabs_api_key'), width=20, anchor="w").pack(side="left")
        elevenlabs_var = StringVar(value=self.api_keys_manager.get_key('elevenlabs'))
        elevenlabs_entry = Entry(elevenlabs_frame, textvariable=elevenlabs_var, show="*", width=40)
        elevenlabs_entry.pack(side="left", padx=5)
        elevenlabs_show_var = BooleanVar()
        elevenlabs_show_btn = Button(elevenlabs_frame, text="👁️", width=3,
                                    command=lambda: elevenlabs_entry.config(show="" if elevenlabs_show_var.get() else "*"))
        elevenlabs_show_btn.pack(side="left")
        Checkbutton(elevenlabs_frame, text=_('show'), variable=elevenlabs_show_var,
                   command=lambda: elevenlabs_entry.config(show="" if elevenlabs_show_var.get() else "*")).pack(side="left", padx=5)
        
        # Voicer API Key (Mat3u)
        voicer_frame = Frame(fields_frame)
        voicer_frame.pack(fill="x", pady=10)
        Label(voicer_frame, text=_('voicer_api_key'), width=20, anchor="w").pack(side="left")
        voicer_var = StringVar(value=self.api_keys_manager.get_key('voicer'))
        voicer_entry = Entry(voicer_frame, textvariable=voicer_var, show="*", width=40)
        voicer_entry.pack(side="left", padx=5)
        voicer_show_var = BooleanVar()
        Button(voicer_frame, text="👁️", width=3,
               command=lambda: voicer_entry.config(show="" if voicer_show_var.get() else "*")).pack(side="left")
        Checkbutton(voicer_frame, text=_('show'), variable=voicer_show_var,
                    command=lambda: voicer_entry.config(show="" if voicer_show_var.get() else "*")).pack(side="left", padx=5)

        # VoiceGenCore API Key
        vgc_frame = Frame(fields_frame)
        vgc_frame.pack(fill="x", pady=10)
        Label(vgc_frame, text="VoiceGenCore API Key:", width=20, anchor="w").pack(side="left")
        vgc_var = StringVar(value=self.api_keys_manager.get_key('voicegencore'))
        vgc_entry = Entry(vgc_frame, textvariable=vgc_var, show="*", width=40)
        vgc_entry.pack(side="left", padx=5)
        vgc_show_var = BooleanVar()
        Button(vgc_frame, text="👁️", width=3,
               command=lambda: vgc_entry.config(show="" if vgc_show_var.get() else "*")).pack(side="left")
        Checkbutton(vgc_frame, text=_('show'), variable=vgc_show_var,
                    command=lambda: vgc_entry.config(show="" if vgc_show_var.get() else "*")).pack(side="left", padx=5)

        # VeoNonStop API Key (veo_… — картинки VEO_BANANA + видео-метод «veo»; отдельный провайдер, НЕ googler)
        veo_frame = Frame(fields_frame)
        veo_frame.pack(fill="x", pady=10)
        Label(veo_frame, text="VeoNonStop API Key:", width=20, anchor="w").pack(side="left")
        veo_var = StringVar(value=self.api_keys_manager.get_key('veo'))
        veo_entry = Entry(veo_frame, textvariable=veo_var, show="*", width=40)
        veo_entry.pack(side="left", padx=5)
        veo_show_var = BooleanVar()
        Button(veo_frame, text="👁️", width=3,
               command=lambda: veo_entry.config(show="" if veo_show_var.get() else "*")).pack(side="left")
        Checkbutton(veo_frame, text=_('show'), variable=veo_show_var,
                    command=lambda: veo_entry.config(show="" if veo_show_var.get() else "*")).pack(side="left", padx=5)

        # Режим отладки — сразу под ключами, чтобы было видно на Mac без прокрутки
        try:
            from logger_config import is_debug_mode, set_debug_mode
            debug_label_frame = LabelFrame(fields_frame, text="🐛 " + _('debug_mode'), font=("Arial", 10, "bold"), fg="#555")
            debug_label_frame.pack(fill="x", pady=10)
            debug_frame = Frame(debug_label_frame)
            debug_frame.pack(fill="x", padx=10, pady=8)
            debug_var = BooleanVar(value=is_debug_mode())
            debug_checkbox = Checkbutton(debug_frame, text=_('debug_mode_desc'), variable=debug_var, font=("Arial", 10))
            debug_checkbox.pack(side="left", padx=(0, 10))
            Label(debug_frame, text=_('debug_mode_hint'), font=("Arial", 8), fg="gray").pack(side="left")
        except ImportError:
            debug_var = None
        
        # Разделитель
        Label(fields_frame, text="─" * 60, fg="gray").pack(fill="x", pady=15)
        
        # Настройки параллельности Googler
        googler_parallel_frame = LabelFrame(fields_frame, text="⚙️ Параллельность Googler", font=("Arial", 10, "bold"))
        googler_parallel_frame.pack(fill="x", pady=10)
        
        # Загружаем текущие настройки
        googler_settings_path = os.path.join(os.path.expanduser("~"), "video_studio_googler_settings.json")
        googler_settings = {}
        if os.path.exists(googler_settings_path):
            try:
                with open(googler_settings_path, 'r', encoding='utf-8', errors='replace') as f:
                    googler_settings = json.load(f)
            except:
                pass
        
        # Параллельность изображений
        images_parallel_frame = Frame(googler_parallel_frame)
        images_parallel_frame.pack(fill="x", padx=10, pady=5)
        Label(images_parallel_frame, text="🖼️ Макс параллельных изображений:", width=30, anchor="w").pack(side="left")
        images_parallel_var = StringVar(value=str(googler_settings.get('max_parallel_images', 20)))
        Entry(images_parallel_frame, textvariable=images_parallel_var, width=10).pack(side="left", padx=5)
        Label(images_parallel_frame, text="(лимит API: 25)", font=("Arial", 8), fg="gray").pack(side="left", padx=5)
        
        # Параллельность видео
        video_parallel_frame = Frame(googler_parallel_frame)
        video_parallel_frame.pack(fill="x", padx=10, pady=5)
        Label(video_parallel_frame, text="🎬 Макс параллельных видео:", width=30, anchor="w").pack(side="left")
        video_parallel_var = StringVar(value=str(googler_settings.get('max_parallel_videos', 10)))
        Entry(video_parallel_frame, textvariable=video_parallel_var, width=10).pack(side="left", padx=5)
        Label(video_parallel_frame, text="(рекомендуется: 10)", font=("Arial", 8), fg="gray").pack(side="left", padx=5)
        
        # Максимум параллельных проектов для видео
        video_projects_frame = Frame(googler_parallel_frame)
        video_projects_frame.pack(fill="x", padx=10, pady=5)
        Label(video_projects_frame, text="📁 Макс параллельных проектов (видео):", width=30, anchor="w").pack(side="left")
        video_projects_var = StringVar(value=str(googler_settings.get('max_parallel_video_projects', 10)))
        Entry(video_projects_frame, textvariable=video_projects_var, width=10).pack(side="left", padx=5)
        Label(video_projects_frame, text="(можно делать видео для разных проектов одновременно)", font=("Arial", 8), fg="gray").pack(side="left", padx=5)
        
        # Кнопки
        buttons_frame = Frame(dialog)
        buttons_frame.pack(pady=20)
        
        def save_and_close():
            self.api_keys_manager.set_key('googler', googler_var.get().strip())
            self.api_keys_manager.set_key('openai', openai_var.get().strip())
            self.api_keys_manager.set_key('elevenlabs', elevenlabs_var.get().strip())
            self.api_keys_manager.set_key('voicer', voicer_var.get().strip())
            self.api_keys_manager.set_key('voicegencore', vgc_var.get().strip())
            self.api_keys_manager.set_key('veo', veo_var.get().strip())

            # Сохраняем режим отладки
            if debug_var is not None:
                try:
                    from logger_config import set_debug_mode
                    set_debug_mode(debug_var.get())
                except ImportError:
                    pass
            
            # Сохраняем настройки параллельности Googler
            try:
                googler_settings = {
                    'max_parallel_images': int(images_parallel_var.get()),
                    'max_parallel_videos': int(video_parallel_var.get()),
                    'max_parallel_video_projects': int(video_projects_var.get())
                }
                with open(googler_settings_path, 'w', encoding='utf-8', errors='replace') as f:
                    json.dump(googler_settings, f, indent=2, ensure_ascii=False)
                
                # Обновляем очереди с новыми значениями (без перезапуска)
                global MAX_PARALLEL_GOOGLER_VIDEO
                MAX_PARALLEL_GOOGLER_VIDEO = googler_settings['max_parallel_video_projects']
                self.googler_video_queue.max_parallel = MAX_PARALLEL_GOOGLER_VIDEO
                
                # Обновляем настройки в app_reference для передачи в worker
                self.googler_settings = googler_settings
                
                self._app_log("Настройки сохранены! Изменения применятся к новым задачам.", kind="render")
            except ValueError:
                self._app_log("Неверные значения для параллельности. Используйте целые числа.", kind="render")
                return
            
            self._app_log(_('api_keys_saved'), kind="render")
            dialog.destroy()
        
        Button(buttons_frame, text=f"💾 {_('save')}", command=save_and_close, 
              bg="#4CAF50", fg="white", width=15).pack(side="left", padx=5)
        Button(buttons_frame, text=f"❌ {_('cancel')}", command=dialog.destroy, 
              bg="#f44336", fg="white", width=15).pack(side="left", padx=5)
    
    def _load_ui_settings(self):
        self.notify_on_complete = True
        if os.path.exists(UI_SETTINGS_CONFIG_PATH):
            try:
                with open(UI_SETTINGS_CONFIG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                    data = json.load(f)
                self.notify_on_complete = data.get('notify_on_complete', True)
            except Exception:
                pass
    
    def _save_ui_settings(self):
        try:
            data = {'notify_on_complete': getattr(self, 'notify_on_complete', True)}
            with open(UI_SETTINGS_CONFIG_PATH, 'w', encoding='utf-8', errors='replace') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    
    def _setup_queue_complete_callbacks(self):
        def make_cb(task_type):
            return lambda name: self.root.after(0, lambda: self._notify_task_complete(task_type, name))
        self.render_queue.on_complete_callback = make_cb(_('queues_type_render'))
        self.edgetts_queue.on_complete_callback = make_cb(_('queues_type_tts_edge'))
        self.elevenlabs_queue.on_complete_callback = make_cb(_('queues_type_tts_eleven'))
        self.voicer_queue.on_complete_callback = make_cb(_('queues_type_tts_voicer'))
        self.thief_queue.on_complete_callback = make_cb(_('queues_type_thief'))
        self.googler_queue.on_complete_callback = make_cb(_('queues_type_googler_images'))
        self.googler_video_queue.on_complete_callback = make_cb(_('queues_type_googler_video'))
    
    def _notify_task_complete(self, task_type, project_name):
        base_name = project_name
        for suffix in ('_images', '_video', '_extract', '_transform', '_full'):
            if base_name.endswith(suffix):
                base_name = base_name[:-len(suffix)]
                break

        for proj_frame in self.projects.values():
            if proj_frame.project_name in (project_name, base_name):
                try:
                    if any(s in task_type.lower() for s in ['tts', 'озвучк', 'edge', 'eleven', 'voicer', 'voicegencore']):
                        proj_frame._set_action_buttons_state('tts', 'normal')
                    elif any(s in task_type.lower() for s in ['рендер', 'render']):
                        proj_frame._set_action_buttons_state('render', 'normal')
                    elif any(s in task_type.lower() for s in ['видео', 'video']):
                        proj_frame._set_action_buttons_state('videos', 'normal')
                    elif any(s in task_type.lower() for s in ['изображ', 'image', 'googler']):
                        proj_frame._set_action_buttons_state('images', 'normal')
                    elif any(s in task_type.lower() for s in ['thief', 'извлеч', 'нарезк']):
                        pass
                    else:
                        proj_frame._set_action_buttons_state('tts', 'normal')
                        proj_frame._set_action_buttons_state('render', 'normal')
                        proj_frame._set_action_buttons_state('images', 'normal')
                        proj_frame._set_action_buttons_state('videos', 'normal')
                except Exception:
                    pass
                break

        for proj_name, proj_frame in self.projects.items():
            if getattr(proj_frame, '_deferred_render_pending', False):
                active = proj_frame._has_active_processes()
                print(f"[DEBUG-RENDER] TRIGGER (notify_complete) project='{proj_frame.project_name}' pending=True active={active}")
                if not active:
                    print(f"[DEBUG-RENDER]   → FIRING render for '{proj_frame.project_name}' (notify_complete)")
                    proj_frame._deferred_render_pending = False
                    proj_frame.log("✅ Все процессы завершены — запускаю рендер!", kind="render")
                    proj_frame.run_video_render()

        if not getattr(self, 'notify_on_complete', True):
            return
        try:
            if sys.platform == 'win32':
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONINFORMATION)
                except Exception:
                    pass
            else:
                try:
                    self.root.bell()
                except Exception:
                    pass
        except Exception:
            pass

    def open_all_queues_dialog(self):
        """Единое окно «Все очереди»: Рендер, Озвучка (Edge/ElevenLabs), Изображения, Видео, Thief + уведомления."""
        dialog = Toplevel(self.root)
        dialog.title(_('queues_all_title'))
        dialog.geometry("580x620")
        dialog.transient(self.root)
        
        main = Frame(dialog, padx=12, pady=12)
        main.pack(fill="both", expand=True)
        
        notify_var = BooleanVar(value=getattr(self, 'notify_on_complete', True))
        def on_notify_change():
            self.notify_on_complete = notify_var.get()
            self._save_ui_settings()
        Checkbutton(main, text=_('notify_on_complete'), variable=notify_var, command=on_notify_change).pack(anchor="w", pady=(0, 10))
        
        canvas = Canvas(main)
        scroll = ttk.Scrollbar(main, orient="vertical", command=canvas.yview)
        inner = Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        
        queues_config = [
            (_('queues_type_render'), self.render_queue),
            (_('queues_type_tts_edge'), self.edgetts_queue),
            (_('queues_type_tts_eleven'), self.elevenlabs_queue),
            (_('queues_type_tts_voicer'), self.voicer_queue),
            (_('queues_type_googler_images'), self.googler_queue),
            (_('queues_type_googler_video'), self.googler_video_queue),
            (_('queues_type_thief'), self.thief_queue),
        ]
        
        def make_section(title, queue_mgr):
            lf = LabelFrame(inner, text=title, font=("Arial", 9, "bold"))
            lf.pack(fill="x", pady=4)
            row1 = Frame(lf)
            row1.pack(fill="x", padx=5, pady=2)
            Label(row1, text=_('render_queue_active_list'), font=("Arial", 8)).pack(side="left")
            active_lb = Listbox(row1, height=2, font=("Consolas", 8))
            active_lb.pack(side="left", fill="x", expand=True, padx=4)
            row2 = Frame(lf)
            row2.pack(fill="x", padx=5, pady=2)
            Label(row2, text=_('render_queue_queued_list'), font=("Arial", 8)).pack(side="left")
            queued_lb = Listbox(row2, height=3, font=("Consolas", 8), selectmode="single")
            queued_lb.pack(side="left", fill="x", expand=True, padx=4)
            btn_row = Frame(lf)
            btn_row.pack(fill="x", padx=5, pady=4)
            def refresh():
                active_lb.delete(0, END)
                queued_lb.delete(0, END)
                for p in queue_mgr.get_active_list():
                    active_lb.insert(END, f"  ▶ {p}")
                for p in queue_mgr.get_queue_list():
                    queued_lb.insert(END, p)
                for proj in self.projects.values():
                    try:
                        proj.update_queue_status()
                    except Exception:
                        pass
            def on_up():
                sel = queued_lb.curselection()
                if not sel:
                    return
                lst = queue_mgr.get_queue_list()
                if sel[0] >= len(lst):
                    return
                name = lst[sel[0]]
                if queue_mgr.move_up(name):
                    refresh()
                    queued_lb.selection_set(max(0, sel[0] - 1))
            def on_down():
                sel = queued_lb.curselection()
                if not sel:
                    return
                lst = queue_mgr.get_queue_list()
                if sel[0] >= len(lst):
                    return
                name = lst[sel[0]]
                if queue_mgr.move_down(name):
                    refresh()
                    queued_lb.selection_set(min(len(lst) - 1, sel[0] + 1))
            def on_remove():
                sel = queued_lb.curselection()
                if not sel:
                    return
                lst = queue_mgr.get_queue_list()
                if sel[0] >= len(lst):
                    return
                if queue_mgr.remove_from_queue(lst[sel[0]]):
                    refresh()
            Button(btn_row, text=_('render_queue_up'), command=on_up, width=8, font=("Arial", 8)).pack(side="left", padx=2)
            Button(btn_row, text=_('render_queue_down'), command=on_down, width=8, font=("Arial", 8)).pack(side="left", padx=2)
            Button(btn_row, text=_('render_queue_remove'), command=on_remove, width=12, font=("Arial", 8)).pack(side="left", padx=2)
            refresh()
            return refresh
        
        refresh_all = []
        for title, qm in queues_config:
            ref = make_section(title, qm)
            refresh_all.append(ref)
        
        # Автообновление каждые 2 секунды
        def _auto_refresh():
            if dialog.winfo_exists():
                for ref_fn in refresh_all:
                    try:
                        ref_fn()
                    except Exception:
                        pass
                dialog.after(2000, _auto_refresh)
        _auto_refresh()
        
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        dialog.bind("<MouseWheel>", _on_mousewheel)
        Button(main, text=_('close'), command=dialog.destroy, width=12).pack(pady=10)
        
        # Лог секция
        log_frame = LabelFrame(main, text="📋 Лог текущей задачи", font=("Arial", 9, "bold"))
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        queue_log_text = ScrolledText(log_frame, height=8, state="disabled", font=("Consolas", 8))
        queue_log_text.pack(fill="both", expand=True, padx=5, pady=5)
    
    def _detect_gpu_encoders(self):
        """Определяет доступные GPU кодировщики через FFmpeg"""
        available = []
        try:
            import shutil
            ffmpeg_path = shutil.which("ffmpeg")
            if not ffmpeg_path:
                # Пробуем найти в bin рядом с exe
                import sys
                if hasattr(sys, '_MEIPASS'):
                    test = os.path.join(sys._MEIPASS, 'bin', 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
                    if os.path.exists(test):
                        ffmpeg_path = test
                if not ffmpeg_path:
                    exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
                    test = os.path.join(exe_dir, 'bin', 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
                    if os.path.exists(test):
                        ffmpeg_path = test
            if not ffmpeg_path:
                ffmpeg_path = "ffmpeg"
            result = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
                                    startupinfo=get_subprocess_flags())
            enc = result.stdout
            if 'h264_nvenc' in enc:
                available.append(('h264_nvenc', 'NVIDIA NVENC (GPU)'))
            if 'h264_videotoolbox' in enc:
                available.append(('h264_videotoolbox', 'Apple VideoToolbox (GPU)'))
            if 'h264_vaapi' in enc:
                available.append(('h264_vaapi', 'VAAPI Intel/AMD (GPU)'))
            if 'h264_qsv' in enc:
                available.append(('h264_qsv', 'Intel Quick Sync (GPU)'))
            if 'h264_amf' in enc:
                available.append(('h264_amf', 'AMD AMF (GPU)'))
        except Exception:
            pass
        return available

    def open_render_settings(self):
        """Открыть окно настроек рендера"""
        dialog = Toplevel(self.root)
        dialog.title(_('render_settings_title'))
        dialog.geometry("750x750")
        dialog.transient(self.root)
        dialog.grab_set()
        
        # Заголовок
        Label(dialog, text=_('render_settings_header'), font=("Arial", 14, "bold")).pack(pady=10)
        Label(dialog, text=_('render_settings_info'), 
              font=("Arial", 9), fg="gray").pack(pady=(0, 10))
        
        # Создаем скроллируемый фрейм
        canvas = Canvas(dialog)
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=canvas.yview)
        scrollable_frame = Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Прокрутка колёсиком мыши
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _on_dialog_close():
            canvas.unbind_all("<MouseWheel>")
            dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", _on_dialog_close)
        
        canvas.pack(side="left", fill="both", expand=True, padx=10, pady=5)
        scrollbar.pack(side="right", fill="y")
        
        settings = self.render_settings_manager.settings
        
        # === КОДИРОВЩИК (ENCODER) — САМОЕ ВАЖНОЕ ===
        enc_frame = LabelFrame(scrollable_frame, text="Encoder / Codec", font=("Arial", 10, "bold"), fg="#1565c0")
        enc_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        # Автодетект GPU
        gpu_encoders = self._detect_gpu_encoders()
        gpu_names = [name for _, name in gpu_encoders]
        gpu_ids = [eid for eid, _ in gpu_encoders]
        
        # Информация о доступных кодировщиках
        if gpu_encoders:
            gpu_info_text = "GPU: " + ", ".join(gpu_names)
            gpu_info_color = "#2e7d32"
        else:
            gpu_info_text = "GPU encoders not found. Only CPU (libx264) available."
            gpu_info_color = "#c62828"
        Label(enc_frame, text=gpu_info_text, font=("Arial", 9, "bold"), fg=gpu_info_color, wraplength=680, justify="left").grid(row=0, column=0, columnspan=3, padx=5, pady=(5, 2), sticky="w")
        
        # Выбор кодировщика
        Label(enc_frame, text="Encoder:", width=20, anchor="w").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        encoder_choices = [('', 'Auto (recommended)'), ('libx264', 'libx264 (CPU)')]
        for eid, ename in gpu_encoders:
            encoder_choices.append((eid, ename))
        
        current_encoder = settings.get('force_encoder', '')
        encoder_display_values = [name for _, name in encoder_choices]
        encoder_id_values = [eid for eid, _ in encoder_choices]
        
        # Находим текущий индекс
        current_display = 'Auto (recommended)'
        for eid, ename in encoder_choices:
            if eid == current_encoder:
                current_display = ename
                break
        
        force_encoder_var = StringVar(value=current_display)
        encoder_combo = ttk.Combobox(enc_frame, textvariable=force_encoder_var, width=35, state="readonly")
        encoder_combo['values'] = encoder_display_values
        encoder_combo.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        Label(enc_frame, text="Auto = detect GPU, fallback to CPU. Choose specific encoder to force it.", 
              font=("Arial", 8), fg="gray").grid(row=2, column=0, columnspan=3, padx=5, sticky="w")
        
        # Force CPU чекбокс
        force_cpu_var = BooleanVar(value=settings.get('force_cpu', False))
        force_cpu_cb = Checkbutton(enc_frame, text="Force CPU only (disable GPU)", variable=force_cpu_var)
        force_cpu_cb.grid(row=3, column=0, columnspan=2, padx=5, pady=(2, 5), sticky="w")
        
        # === CPU ПРЕСЕТЫ ===
        cpu_frame = LabelFrame(scrollable_frame, text=_('cpu_presets'), font=("Arial", 10, "bold"))
        cpu_frame.pack(fill="x", padx=10, pady=5)
        
        Label(cpu_frame, text=_('cpu_preset_label'), width=20, anchor="w").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        cpu_preset_var = StringVar(value=settings.get('cpu_preset', 'fast'))
        cpu_preset_combo = ttk.Combobox(cpu_frame, textvariable=cpu_preset_var, width=35, state="readonly")
        cpu_preset_combo['values'] = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')
        cpu_preset_combo.grid(row=0, column=1, padx=5, pady=5)
        Label(cpu_frame, text=_('cpu_preset_hint'), 
              font=("Arial", 8), fg="gray").grid(row=1, column=0, columnspan=2, padx=5, sticky="w")
        
        # === GPU ПРЕСЕТЫ ===
        gpu_frame = LabelFrame(scrollable_frame, text=_('gpu_presets'), font=("Arial", 10, "bold"))
        gpu_frame.pack(fill="x", padx=10, pady=5)
        
        Label(gpu_frame, text=_('nvenc_label'), width=20, anchor="w").grid(row=0, column=0, padx=5, pady=4, sticky="w")
        nvenc_preset_var = StringVar(value=settings.get('nvenc_preset', 'p4'))
        nvenc_preset_combo = ttk.Combobox(gpu_frame, textvariable=nvenc_preset_var, width=35, state="readonly")
        nvenc_preset_combo['values'] = ('p1 (fastest)', 'p2', 'p3', 'p4 (balanced)', 'p5', 'p6', 'p7 (best quality)')
        # Устанавливаем текущее значение
        nvenc_val = settings.get('nvenc_preset', 'p4')
        nvenc_display_map = {'p1': 'p1 (fastest)', 'p2': 'p2', 'p3': 'p3', 'p4': 'p4 (balanced)', 'p5': 'p5', 'p6': 'p6', 'p7': 'p7 (best quality)'}
        nvenc_preset_var.set(nvenc_display_map.get(nvenc_val, 'p4 (balanced)'))
        nvenc_preset_combo.grid(row=0, column=1, padx=5, pady=4)
        Label(gpu_frame, text=_('nvenc_hint'), 
              font=("Arial", 8), fg="gray").grid(row=1, column=0, columnspan=2, padx=5, sticky="w")
        
        Label(gpu_frame, text=_('videotoolbox_label'), width=20, anchor="w").grid(row=2, column=0, padx=5, pady=4, sticky="w")
        videotoolbox_preset_var = StringVar(value=settings.get('videotoolbox_preset', 'fast'))
        videotoolbox_preset_combo = ttk.Combobox(gpu_frame, textvariable=videotoolbox_preset_var, width=35, state="readonly")
        videotoolbox_preset_combo['values'] = ('fast', 'medium', 'slow')
        videotoolbox_preset_combo.grid(row=2, column=1, padx=5, pady=4)
        
        Label(gpu_frame, text=_('vaapi_label'), width=20, anchor="w").grid(row=3, column=0, padx=5, pady=4, sticky="w")
        vaapi_preset_var = StringVar(value=settings.get('vaapi_preset', 'fast'))
        vaapi_preset_combo = ttk.Combobox(gpu_frame, textvariable=vaapi_preset_var, width=35, state="readonly")
        vaapi_preset_combo['values'] = ('fast', 'medium', 'slow')
        vaapi_preset_combo.grid(row=3, column=1, padx=5, pady=4)
        
        Label(gpu_frame, text="VAAPI device:", width=20, anchor="w").grid(row=4, column=0, padx=5, pady=4, sticky="w")
        vaapi_device_var = StringVar(value=settings.get('vaapi_device', ''))
        Entry(gpu_frame, textvariable=vaapi_device_var, width=38).grid(row=4, column=1, padx=5, pady=4, sticky="w")
        Label(gpu_frame, text="Linux only: /dev/dri/renderD128 (leave empty for auto)", 
              font=("Arial", 8), fg="gray").grid(row=5, column=0, columnspan=2, padx=5, sticky="w")
        
        # === КАЧЕСТВО (CRF) ===
        quality_frame = LabelFrame(scrollable_frame, text=_('quality_video'), font=("Arial", 10, "bold"))
        quality_frame.pack(fill="x", padx=10, pady=5)
        
        Label(quality_frame, text=_('crf_label'), width=20, anchor="w").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        crf_var = StringVar(value=str(settings.get('crf', 23)))
        crf_scale = ttk.Scale(quality_frame, from_=0, to=51, orient="horizontal", length=250,
                              command=lambda v: crf_var.set(str(int(float(v)))))
        crf_scale.set(int(settings.get('crf', 23)))
        crf_scale.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        crf_value_label = Label(quality_frame, textvariable=crf_var, width=4, font=("Arial", 10, "bold"))
        crf_value_label.grid(row=0, column=2, padx=5, pady=5)
        Label(quality_frame, text=_('crf_hint'), 
              font=("Arial", 8), fg="gray").grid(row=1, column=0, columnspan=3, padx=5, sticky="w")
        
        Label(quality_frame, text="Max bitrate (NVENC):", width=20, anchor="w").grid(row=2, column=0, padx=5, pady=5, sticky="w")
        maxrate_var = StringVar(value=str(settings.get('max_bitrate_mbps', 8)))
        Entry(quality_frame, textvariable=maxrate_var, width=10).grid(row=2, column=1, padx=5, pady=5, sticky="w")
        Label(quality_frame, text="Mbps (6 = ~225MB/5min, 0 = no limit)", 
              font=("Arial", 8), fg="gray").grid(row=3, column=0, columnspan=3, padx=5, sticky="w")
        
        # === БАТЧИНГ ===
        batch_frame = LabelFrame(scrollable_frame, text=_('batch_settings'), font=("Arial", 10, "bold"))
        batch_frame.pack(fill="x", padx=10, pady=5)
        
        Label(batch_frame, text=_('batch_small'), width=30, anchor="w").grid(row=0, column=0, padx=5, pady=4, sticky="w")
        batch_small_var = StringVar(value=str(settings.get('batch_size_small', 20)))
        Entry(batch_frame, textvariable=batch_small_var, width=10).grid(row=0, column=1, padx=5, pady=4, sticky="w")
        
        Label(batch_frame, text=_('batch_medium'), width=30, anchor="w").grid(row=1, column=0, padx=5, pady=4, sticky="w")
        batch_medium_var = StringVar(value=str(settings.get('batch_size_medium', 18)))
        Entry(batch_frame, textvariable=batch_medium_var, width=10).grid(row=1, column=1, padx=5, pady=4, sticky="w")
        
        Label(batch_frame, text=_('batch_large'), width=30, anchor="w").grid(row=2, column=0, padx=5, pady=4, sticky="w")
        batch_large_var = StringVar(value=str(settings.get('batch_size_large', 15)))
        Entry(batch_frame, textvariable=batch_large_var, width=10).grid(row=2, column=1, padx=5, pady=4, sticky="w")
        Label(batch_frame, text=_('batch_hint'), 
              font=("Arial", 8), fg="gray").grid(row=3, column=0, columnspan=2, padx=5, sticky="w")
        
        # === ПРОЧИЕ НАСТРОЙКИ ===
        other_frame = LabelFrame(scrollable_frame, text=_('other_settings'), font=("Arial", 10, "bold"))
        other_frame.pack(fill="x", padx=10, pady=5)
        
        Label(other_frame, text=_('threads_label'), width=20, anchor="w").grid(row=0, column=0, padx=5, pady=4, sticky="w")
        threads_var = StringVar(value=str(settings.get('threads', 0)))
        Entry(other_frame, textvariable=threads_var, width=10).grid(row=0, column=1, padx=5, pady=4, sticky="w")
        Label(other_frame, text="0 = auto (recommended)", font=("Arial", 8), fg="gray").grid(row=0, column=2, padx=5, pady=4, sticky="w")
        
        Label(other_frame, text=_('timeout_label'), width=20, anchor="w").grid(row=1, column=0, padx=5, pady=4, sticky="w")
        timeout_var = StringVar(value=str(settings.get('timeout_seconds', 14400)))
        Entry(other_frame, textvariable=timeout_var, width=10).grid(row=1, column=1, padx=5, pady=4, sticky="w")
        Label(other_frame, text="seconds (14400 = 4h)", font=("Arial", 8), fg="gray").grid(row=1, column=2, padx=5, pady=4, sticky="w")
        
        Label(other_frame, text=_('priority_label'), width=20, anchor="w").grid(row=2, column=0, padx=5, pady=4, sticky="w")
        priority_var = StringVar(value=settings.get('process_priority', 'normal'))
        priority_combo = ttk.Combobox(other_frame, textvariable=priority_var, width=15, state="readonly")
        priority_combo['values'] = ('low', 'normal', 'high')
        priority_combo.grid(row=2, column=1, padx=5, pady=4)
        
        Label(other_frame, text=_('max_pool_workers_label'), width=20, anchor="w").grid(row=3, column=0, padx=5, pady=4, sticky="w")
        max_pool_var = StringVar(value=str(settings.get('max_pool_workers', 0)))
        Entry(other_frame, textvariable=max_pool_var, width=10).grid(row=3, column=1, padx=5, pady=4, sticky="w")
        Label(other_frame, text="0 = auto (GPU: 6, CPU: до 12)", font=("Arial", 8), fg="gray").grid(row=3, column=2, padx=5, pady=4, sticky="w")
        
        Label(other_frame, text=_('max_parallel_renders_label'), width=20, anchor="w").grid(row=4, column=0, padx=5, pady=4, sticky="w")
        max_parallel_var = StringVar(value=str(settings.get('max_parallel_renders', MAX_PARALLEL_RENDERS)))
        Entry(other_frame, textvariable=max_parallel_var, width=10).grid(row=4, column=1, padx=5, pady=4, sticky="w")
        Label(other_frame, text=_('max_parallel_renders_hint'), 
              font=("Arial", 8), fg="gray").grid(row=4, column=2, padx=5, pady=4, sticky="w")
        
        Label(other_frame, text="Параллельных нарезок:", width=20, anchor="w").grid(row=5, column=0, padx=5, pady=4, sticky="w")
        max_thief_var = StringVar(value=str(getattr(self, 'thief_queue', None) and self.thief_queue.max_parallel or MAX_PARALLEL_THIEF))
        Entry(other_frame, textvariable=max_thief_var, width=10).grid(row=5, column=1, padx=5, pady=4, sticky="w")
        Label(other_frame, text="Сколько видео нарезать одновременно", font=("Arial", 8), fg="gray").grid(row=5, column=2, padx=5, pady=4, sticky="w")
        
        # Кнопки
        buttons_frame = Frame(dialog)
        buttons_frame.pack(pady=15)
        
        def save_and_close():
            try:
                # Encoder
                selected_display = force_encoder_var.get()
                selected_encoder = ''
                for eid, ename in encoder_choices:
                    if ename == selected_display:
                        selected_encoder = eid
                        break
                self.render_settings_manager.set_setting('force_encoder', selected_encoder)
                self.render_settings_manager.set_setting('force_cpu', force_cpu_var.get())
                
                # CPU
                self.render_settings_manager.set_setting('cpu_preset', cpu_preset_var.get())
                
                # GPU
                nvenc_display = nvenc_preset_var.get()
                nvenc_id = nvenc_display.split(' ')[0] if nvenc_display else 'p4'
                self.render_settings_manager.set_setting('nvenc_preset', nvenc_id)
                self.render_settings_manager.set_setting('videotoolbox_preset', videotoolbox_preset_var.get())
                self.render_settings_manager.set_setting('vaapi_preset', vaapi_preset_var.get())
                self.render_settings_manager.set_setting('vaapi_device', vaapi_device_var.get().strip())
                
                # Quality
                self.render_settings_manager.set_setting('crf', int(float(crf_var.get())))
                maxrate_val = max(0, int(maxrate_var.get()))
                self.render_settings_manager.set_setting('max_bitrate_mbps', maxrate_val)
                
                # Batch
                self.render_settings_manager.set_setting('batch_size_small', int(batch_small_var.get()))
                self.render_settings_manager.set_setting('batch_size_medium', int(batch_medium_var.get()))
                self.render_settings_manager.set_setting('batch_size_large', int(batch_large_var.get()))
                
                # Other
                self.render_settings_manager.set_setting('threads', int(threads_var.get()))
                self.render_settings_manager.set_setting('timeout_seconds', int(timeout_var.get()))
                self.render_settings_manager.set_setting('process_priority', priority_var.get())
                try:
                    max_pool_val = int(max_pool_var.get())
                    self.render_settings_manager.set_setting('max_pool_workers', max_pool_val if max_pool_val > 0 else 0)
                except (ValueError, TypeError):
                    self.render_settings_manager.set_setting('max_pool_workers', 0)
                max_par_val = max(1, min(16, int(max_parallel_var.get())))
                self.render_settings_manager.set_setting('max_parallel_renders', max_par_val)
                self.render_queue.max_parallel = max_par_val
                max_thief_val = max(1, min(5, int(max_thief_var.get())))
                if self.thief_queue:
                    self.thief_queue.max_parallel = max_thief_val
                
                self._app_log(_('render_settings_saved'), kind="render")
                canvas.unbind_all("<MouseWheel>")
                dialog.destroy()
            except ValueError as e:
                self._app_log(_('invalid_value').format(error=str(e)), kind="render")
        
        def reset_defaults():
            """Сброс настроек к значениям по умолчанию"""
            force_encoder_var.set('Auto (recommended)')
            force_cpu_var.set(False)
            cpu_preset_var.set('fast')
            nvenc_preset_var.set('p4 (balanced)')
            videotoolbox_preset_var.set('fast')
            vaapi_preset_var.set('fast')
            vaapi_device_var.set('')
            crf_var.set('23')
            crf_scale.set(23)
            maxrate_var.set('6')
            batch_small_var.set('20')
            batch_medium_var.set('18')
            batch_large_var.set('15')
            threads_var.set('0')
            timeout_var.set('14400')
            priority_var.set('normal')
            max_pool_var.set('0')
            max_parallel_var.set(str(MAX_PARALLEL_RENDERS))
        
        Button(buttons_frame, text=f"💾 {_('save')}", command=save_and_close, 
              bg="#4CAF50", fg="white", width=15, font=("Arial", 10)).pack(side="left", padx=5)
        Button(buttons_frame, text="Reset defaults", command=reset_defaults, 
              bg="#FF9800", fg="white", width=15, font=("Arial", 10)).pack(side="left", padx=5)
        Button(buttons_frame, text=f"❌ {_('cancel')}", command=_on_dialog_close, 
              bg="#f44336", fg="white", width=15, font=("Arial", 10)).pack(side="left", padx=5)
        
        # Обновляем размер canvas при изменении содержимого
        def update_scroll_region(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        scrollable_frame.bind('<Configure>', update_scroll_region)
    


if __name__ == "__main__":
    # Проверка версии Python и совместимости
    python_version = sys.version_info
    if python_version.major < 3 or (python_version.major == 3 and python_version.minor < 8):
        print("❌ ОШИБКА: Требуется Python 3.8 или выше", file=sys.stderr)
        print(f"   Текущая версия: {python_version.major}.{python_version.minor}.{python_version.micro}", file=sys.stderr)
        sys.exit(1)
    
    # Проверка на macOS - предупреждение о совместимости
    if sys.platform == 'darwin':
        try:
            import platform
            mac_version = platform.mac_ver()[0]
            if mac_version:
                mac_major = int(mac_version.split('.')[0])
                if mac_major < 10:
                    print(f"⚠️ Предупреждение: Обнаружена старая версия macOS: {mac_version}", file=sys.stderr)
                    print("   Рекомендуется использовать Python 3.10-3.12 для лучшей совместимости", file=sys.stderr)
        except:
            pass
    
    # Проверка лицензии перед запуском приложения
    try:
        import license_guard
        license_guard.check_license()
    except ImportError:
        # Если файл license_guard.py отсутствует, пропускаем проверку
        # (для разработки или если защита не нужна)
        pass
    except Exception as e:
        # На случай, если файл удалят или он сломан
        import tkinter.messagebox as mb
        mb.showerror("Security Error", f"License check failed: {e}")
        sys.exit(1)
    
    try:
        root = Tk()

        def _bind_clipboard_shortcuts(root):
            """Ctrl+C/V/X/A для любой раскладки (по keycode физической клавиши)."""
            import tkinter as tk

            def _on_key(event):
                if not (event.state & 0x4):
                    return
                # На английской раскладке Tkinter уже обрабатывает Ctrl+C/V/X/A —
                # повторное вмешательство вызовет двойную вставку / двойной cut
                if getattr(event, 'keysym', '').lower() in ('c', 'v', 'x', 'a'):
                    return
                kc = event.keycode
                w = event.widget
                # C=67, V=86, X=88, A=65 — одинаковые keycode при любой раскладке
                if kc == 67:
                    try:
                        root.clipboard_clear()
                        root.clipboard_append(w.selection_get())
                    except Exception:
                        pass
                    return "break"
                elif kc == 86:
                    if hasattr(w, 'insert'):
                        try:
                            text = root.clipboard_get()
                            try:
                                w.delete("sel.first", "sel.last")
                            except Exception:
                                pass
                            w.insert("insert", text)
                        except Exception:
                            pass
                    return "break"
                elif kc == 88:
                    try:
                        root.clipboard_clear()
                        root.clipboard_append(w.selection_get())
                        try:
                            w.delete("sel.first", "sel.last")
                        except Exception:
                            pass
                    except Exception:
                        pass
                    return "break"
                elif kc == 65:
                    if isinstance(w, tk.Text):
                        w.tag_add("sel", "1.0", "end")
                    elif isinstance(w, tk.Entry):
                        w.select_range(0, "end")
                    return "break"

            root.bind_all('<Key>', _on_key)

        _bind_clipboard_shortcuts(root)

        app = App(root)
        root.mainloop()
    except Exception as e:
        print(f"КРИТИЧЕСКАЯ ОШИБКА: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
