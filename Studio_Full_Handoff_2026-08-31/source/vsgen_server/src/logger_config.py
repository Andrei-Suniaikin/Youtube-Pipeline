"""
Модуль конфигурации логирования для чистого UI/UX
Фильтрует технические сообщения и показывает пользователю только понятные сообщения
"""
import os
import sys
import time


def _ts():
    """Таймкод HH:MM:SS.mmm для логов (диагностика длительности фаз)."""
    t = time.time()
    lt = time.localtime(t)
    ms = int((t - int(t)) * 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms:03d}"


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

# Глобальная переменная режима отладки
DEBUG_MODE = False

def set_debug_mode(enabled: bool):
    """Устанавливает режим отладки"""
    global DEBUG_MODE
    DEBUG_MODE = enabled
    if enabled:
        os.environ['VIDEO_STUDIO_DEBUG'] = '1'
    else:
        os.environ.pop('VIDEO_STUDIO_DEBUG', None)

def is_debug_mode() -> bool:
    """Проверяет, включен ли режим отладки"""
    return os.environ.get('VIDEO_STUDIO_DEBUG') == '1' or DEBUG_MODE

def print_user(msg: str, msg_type: str = "INFO"):
    """
    Печатает красивые сообщения для пользователя с эмодзи
    
    Args:
        msg: Текст сообщения
        msg_type: Тип сообщения (INFO, SUCCESS, WARNING, ERROR, PROCESS)
    """
    # Определяем эмодзи и префикс в зависимости от типа
    emoji_map = {
        "INFO": "ℹ️",
        "SUCCESS": "✅",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "PROCESS": "⏳",
    }
    
    emoji = emoji_map.get(msg_type.upper(), "ℹ️")

    # Форматируем сообщение (с таймкодом)
    formatted_msg = f"[{_ts()}] {emoji} {msg}"

    # Выводим с flush для real-time отображения
    try:
        print(formatted_msg, flush=True)
    except UnicodeEncodeError:
        # Если не удается вывести emoji - выводим без него
        print(f"[{_ts()}] {msg}", flush=True)

def print_debug(msg: str):
    """
    Печатает техническое сообщение ТОЛЬКО если включен режим отладки
    
    Args:
        msg: Техническое сообщение для отладки
    """
    if is_debug_mode():
        # В режиме отладки показываем технические сообщения с префиксом
        try:
            print(f"[{_ts()}] 🔧 [DEBUG] {msg}", flush=True)
        except UnicodeEncodeError:
            # Если не удается вывести emoji - выводим без него
            print(f"[{_ts()}] [DEBUG] {msg}", flush=True)

def print_progress(current: int, total: int, item_name: str = "элементов", show_bar: bool = True):
    """
    Печатает прогресс обработки в понятном формате с прогресс-баром
    
    Args:
        current: Текущий номер
        total: Всего элементов
        item_name: Название элементов (например, "изображений", "видео")
        show_bar: Показывать ли визуальный прогресс-бар
    """
    percentage = int((current / total) * 100) if total > 0 else 0
    
    if show_bar and total > 0:
        bar_length = 20
        filled = int(bar_length * current / total)
        bar = "█" * filled + "░" * (bar_length - filled)
        print_user(f"[{bar}] {percentage}% — {item_name}: {current}/{total}", "PROCESS")
    else:
        print_user(f"{item_name}: {current}/{total} ({percentage}%)", "PROCESS")

def print_error_user_friendly(error_msg: str, show_debug_hint: bool = True):
    """
    Печатает понятное сообщение об ошибке для пользователя
    
    Args:
        error_msg: Понятное сообщение об ошибке
        show_debug_hint: Показывать ли подсказку о режиме отладки
    """
    print_user(error_msg, "ERROR")
    if show_debug_hint and not is_debug_mode():
        print_user("Включите режим отладки для получения детальной информации", "INFO")

