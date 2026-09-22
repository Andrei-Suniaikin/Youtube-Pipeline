"""
Модуль для определения путей в portable/EXE версии приложения.
Критически важен для работы скомпилированного EXE.
"""
import sys
import os


def is_frozen():
    """
    Проверяет, запущено ли приложение из скомпилированного EXE.
    
    Returns:
        bool: True если запущено из EXE (frozen), False если из скрипта
    """
    return getattr(sys, 'frozen', False)


def get_base_dir():
    """
    Получает базовую директорию приложения.
    
    Returns:
        str: Путь к директории с EXE (если frozen) или к директории скрипта
    """
    if is_frozen():
        # Если запущено из EXE — всегда абсолютный путь к папке с exe
        exe_dir = os.path.dirname(sys.executable)
        return os.path.abspath(exe_dir) if exe_dir else os.getcwd()
    # Если запущено из скрипта - возвращаем директорию со скриптом
    return os.path.dirname(os.path.abspath(__file__))


def get_scripts_dir():
    """
    Получает директорию со скриптами воркеров.
    В EXE версии это та же директория, что и get_base_dir().
    
    Returns:
        str: Путь к директории со скриптами
    """
    return get_base_dir()


def get_python_executable():
    """
    Получает путь к исполняемому файлу Python.
    В EXE версии возвращает sys.executable (сам EXE).
    В скрипт-версии возвращает sys.executable (интерпретатор Python).
    
    Returns:
        str: Путь к Python/EXE
    """
    return sys.executable


def is_portable_mode():
    """
    Проверяет, работает ли приложение в portable режиме.
    Сейчас это синоним is_frozen().
    
    Returns:
        bool: True если portable/EXE режим
    """
    return is_frozen()


def _get_bundled_bin_dir():
    """
    Путь к папке bin внутри распакованного бандла (PyInstaller _MEIPASS).
    Возвращает None, если не frozen или папки нет.
    """
    if not is_frozen():
        return None
    meipass = getattr(sys, '_MEIPASS', None)
    if not meipass or not os.path.isdir(meipass):
        return None
    bin_dir = os.path.join(meipass, "bin")
    return bin_dir if os.path.isdir(bin_dir) else None


def get_ffmpeg_executable():
    """
    Получает путь к исполняемому файлу FFmpeg.
    В EXE: сначала папка bin внутри бандла (_MEIPASS), затем bin рядом с exe.
    
    Returns:
        str: Путь к ffmpeg.exe в bin/ или системный "ffmpeg"
    """
    # Встроенная в exe папка bin (PyInstaller --add-data)
    bundled = _get_bundled_bin_dir()
    if bundled:
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = os.path.join(bundled, name)
            if os.path.exists(p):
                return os.path.abspath(p)
    base = get_base_dir()
    candidates = [
        os.path.join(base, "bin", "ffmpeg.exe"),
        os.path.join(base, "bin", "ffmpeg"),
        "ffmpeg.exe",
        "ffmpeg"
    ]
    for candidate in candidates:
        abs_candidate = os.path.abspath(candidate)
        if os.path.exists(abs_candidate):
            return abs_candidate
    return "ffmpeg"


def get_ffprobe_executable():
    """
    Получает путь к ffprobe (для EXE — сначала bin в бандле, затем рядом с exe).
    """
    bundled = _get_bundled_bin_dir()
    if bundled:
        for name in ("ffprobe.exe", "ffprobe"):
            p = os.path.join(bundled, name)
            if os.path.exists(p):
                return os.path.abspath(p)
    base = get_base_dir()
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = os.path.join(base, "bin", name)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return "ffprobe"


def get_ytdlp_executable():
    """
    Получает путь к исполняемому файлу yt-dlp.
    В EXE: сначала папка bin внутри бандла (_MEIPASS), затем bin рядом с exe.
    
    Returns:
        str: Путь к yt-dlp.exe в bin/ или системный "yt-dlp"
    """
    # Встроенная в exe папка bin (PyInstaller --add-data)
    bundled = _get_bundled_bin_dir()
    if bundled:
        for name in ("yt-dlp.exe", "yt-dlp"):
            p = os.path.join(bundled, name)
            if os.path.exists(p):
                return os.path.abspath(p)
    base = get_base_dir()
    candidates = [
        os.path.join(base, "bin", "yt-dlp.exe"),
        os.path.join(base, "bin", "yt-dlp"),
        os.path.join(base, "yt-dlp.exe"),
        os.path.join(base, "yt-dlp"),
    ]
    for candidate in candidates:
        abs_candidate = os.path.abspath(candidate)
        if os.path.exists(abs_candidate):
            return abs_candidate
    if not is_frozen():
        for c in ("yt-dlp.exe", "yt-dlp"):
            if os.path.exists(c):
                return c
    # Fallback: имя для поиска в PATH (frozen и не-frozen)
    return "yt-dlp"


def _probe_audio_bitrate(audio_path):
    """Битрейт исходника строкой для ``-b:a`` («128k»); "" — не удалось узнать.

    Нужен, чтобы смена темпа не роняла качество: без явного ``-b:a`` libmp3lame
    берёт свой дефолт, и 128 kbps источник выходил 64 kbps.
    """
    import os, subprocess
    try:
        probe = subprocess.run(
            [get_ffprobe_executable(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=bit_rate", "-of", "default=nw=1:nk=1", audio_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        bits = int((probe.stdout or "").strip() or 0)
    except Exception:
        return ""
    if not (8000 <= bits <= 512000):
        return ""
    return f"{bits // 1000}k"


def apply_audio_speed(audio_path, speed, strict=False):
    """Меняет ТЕМП аудио через ffmpeg atempo (питч сохраняется). speed≈1.0 → no-op.
    atempo держит 0.5–2.0 за один фильтр; вне диапазона — цепочкой.

    По умолчанию сохраняет прежний best-effort контракт для остальных worker-ов:
    ошибка возвращает False и не поднимается наружу. ``strict=True`` нужен там,
    где публикация исходного (неускоренного) файла была бы silent corruption.
    """
    import math, os, subprocess

    def fail(message):
        if strict:
            raise RuntimeError(message)
        return False

    try:
        s = float(speed)
    except (TypeError, ValueError):
        return fail(f"некорректная скорость аудио: {speed!r}")
    if not audio_path or not os.path.isfile(audio_path):
        return fail(f"нет аудиофайла для изменения скорости: {audio_path!r}")
    if not math.isfinite(s) or s <= 0:
        return fail(f"скорость аудио должна быть положительной: {s!r}")
    if abs(s - 1.0) < 0.01:
        return True
    factors = []
    rem = s
    while rem > 2.0:
        factors.append(2.0); rem /= 2.0
    while rem < 0.5:
        factors.append(0.5); rem /= 0.5
    factors.append(rem)
    chain = ",".join("atempo=%.4f" % f for f in factors)
    ff = get_ffmpeg_executable()
    tmp = audio_path + ".spd.tmp.mp3"
    # 03.09.2026. Два дефекта фиксированных 300 с и дефолтного битрейта:
    #  1) на длинной озвучке (весь скрипт одним mp3 — у партнёров это 2+ часа)
    #     ffmpeg не укладывался в 5 минут. Замер на VPS: atempo=0.9 на 2 ч 15 мин
    #     (130 МБ) — 3 мин 36 с БЕЗ соседней нагрузки, под картинками/рендером
    #     заведомо больше. Таймаут срабатывал три попытки подряд, и уже ОПЛАЧЕННАЯ
    #     озвучка терялась (85 задач у одного тенанта за две недели).
    #  2) libmp3lame без -b:a давал 64 kbps из 128 kbps источника — тихая потеря
    #     качества на каждом проекте со скоростью ≠ 1.0.
    size_mb = 0.0
    try:
        size_mb = os.path.getsize(audio_path) / (1024.0 * 1024.0)
    except OSError:
        pass
    try:
        env_timeout = int(os.environ.get("VS_AUDIO_SPEED_TIMEOUT", "0") or 0)
    except (TypeError, ValueError):
        env_timeout = 0
    speed_timeout = env_timeout or int(min(7200.0, max(900.0, size_mb * 12.0)))
    src_bitrate = _probe_audio_bitrate(audio_path)
    rate_args = ["-b:a", src_bitrate] if src_bitrate else []
    kw = {}
    try:
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
    except Exception:
        pass
    try:
        r = subprocess.run([ff, "-y", "-v", "error", "-i", audio_path, "-filter:a", chain]
                           + rate_args + [tmp],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=speed_timeout, **kw)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, audio_path)
            return True
        if os.path.exists(tmp):
            os.remove(tmp)
        details = str(getattr(r, "stderr", "") or "").strip()[:300]
        return fail(
            f"ffmpeg не применил скорость {s:g} (rc={r.returncode})"
            + (f": {details}" if details else "")
        )
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return fail(f"ffmpeg не применил скорость {s:g}: {exc}")


def get_worker_command(script_name, args=None):
    """
    Получает команду для запуска воркера.
    
    Если приложение скомпилировано (frozen):
    - Ищет соседний .exe файл (например, googler_worker.exe)
    - Если .exe не найден, ищет в .dist папке (standalone режим)
    - Если и там не найден, использует .py скрипт
    
    Если запущено из скрипта:
    - Возвращает [python, путь_к_скрипту.py] + args
    
    Args:
        script_name (str): Имя скрипта воркера (например, "googler_worker.py") 
                           или полный путь к скрипту
        args (list): Дополнительные аргументы для воркера
    
    Returns:
        list: Список аргументов для subprocess.Popen/subprocess.run
    """
    if args is None:
        args = []
    
    if is_frozen():
        # Если EXE - ищем соседний .exe файл
        # script_name может быть полным путем или просто именем
        if os.path.isabs(script_name):
            # Если полный путь - заменяем .py на .exe
            worker_exe = script_name.replace(".py", ".exe")
            base_name = os.path.splitext(os.path.basename(script_name))[0]
        else:
            # Если просто имя - ищем в базовой директории
            base_name = os.path.splitext(script_name)[0]
            worker_exe = os.path.join(get_base_dir(), base_name + ".exe")
        
        # Сначала пробуем найти .exe в корне
        if os.path.exists(worker_exe):
            return [worker_exe] + args
        
        # Если не найден, пробуем найти в .dist папке (standalone режим)
        dist_exe = os.path.join(get_base_dir(), base_name + ".dist", base_name + ".exe")
        if os.path.exists(dist_exe):
            return [dist_exe] + args
        
        # Если и там не найден, пробуем .py скрипт (fallback)
        if os.path.isabs(script_name):
            script_path = script_name
        else:
            script_path = os.path.join(get_base_dir(), script_name)
        if os.path.exists(script_path):
            # В EXE режиме НЕ запускаем .py через sys.executable (это будет VideoStudio.exe!)
            # Пытаемся найти Python в системе
            import shutil
            python_exe = shutil.which('python') or shutil.which('python3')
            if python_exe and os.path.exists(python_exe):
                return [python_exe, script_path] + args
            # Если Python не найден - возвращаем None для обработки ошибки
            return None
        
        # Если ничего не найдено - возвращаем None (приложение покажет ошибку)
        return None
    else:
        # Если скрипт - запускаем через Python
        # script_name может быть полным путем или просто именем
        if os.path.isabs(script_name):
            script_path = script_name
        else:
            script_path = os.path.join(get_base_dir(), script_name)
        return [sys.executable, script_path] + args
