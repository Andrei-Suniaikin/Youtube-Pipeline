#!/usr/bin/env python3
"""
YouTube Slide Extractor v1.0

Извлекает уникальные слайды из YouTube видео.
Идеально для презентаций, обучающих роликов, лекций.

ИСПОЛЬЗОВАНИЕ:
    python youtube_slide_extractor.py "https://youtube.com/watch?v=..."
    python youtube_slide_extractor.py "https://youtube.com/watch?v=..." --output ./slides
    python youtube_slide_extractor.py "https://youtube.com/watch?v=..." --sensitivity 0.9

ТРЕБОВАНИЯ:
    pip install yt-dlp opencv-python numpy pillow imagehash

АЛГОРИТМ:
1. Скачивает видео в лучшем качестве
2. Извлекает кадры каждые 0.5 секунды
3. Сравнивает кадры через perceptual hash + structural comparison
4. Определяет смену слайда когда разница > порога
5. Фильтрует переходные кадры (fade, slide transitions)
6. Сохраняет уникальные слайды
"""

import os
import sys
import argparse
import tempfile
import shutil
import hashlib
import time
import subprocess

# Скрытие консольного окна при запуске дочерних процессов из exe (Windows)
_SUBPROCESS_HIDE_WINDOW = {}
if sys.platform == 'win32':
    _SUBPROCESS_HIDE_WINDOW = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
    _startupinfo = subprocess.STARTUPINFO()
    _startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _startupinfo.wShowWindow = subprocess.SW_HIDE
else:
    _startupinfo = None

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

# Импорт локализации (ДОЛЖЕН БЫТЬ ПЕРЕД check_dependencies!)
try:
    from i18n import _
except ImportError:
    # Если модуль не найден, используем функцию-заглушку
    def _(key, *args, **kwargs):
        return key

# Импортируем модуль для определения путей portable Python
try:
    import portable_paths  # type: ignore[reportMissingImports]
    YTDLP_EXE = portable_paths.get_ytdlp_executable()
    FFMPEG_EXE = portable_paths.get_ffmpeg_executable()
except ImportError:
    # Если модуль не найден - используем системные утилиты
    YTDLP_EXE = "yt-dlp"
    FFMPEG_EXE = "ffmpeg"
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional
from dataclasses import dataclass
import re

# Проверяем зависимости
def check_dependencies():
    """Проверяет и устанавливает зависимости"""
    global YTDLP_EXE
    missing = []
    
    try:
        import cv2
    except ImportError:
        missing.append("opencv-python")
    
    try:
        import numpy
    except ImportError:
        missing.append("numpy")
    
    try:
        from PIL import Image
    except ImportError:
        missing.append("pillow")
    
    try:
        import imagehash
    except ImportError:
        missing.append("imagehash")
    
    # Проверяем yt-dlp - сначала как команду, потом как Python модуль
    ytdlp_found = False
    ytdlp_cmd = None
    
    # Пробуем как команду (строка)
    if isinstance(YTDLP_EXE, str):
        # Проверяем, существует ли файл (если это путь)
        if os.path.exists(YTDLP_EXE):
            try:
                result = subprocess.run(
                    [YTDLP_EXE, "--version"], 
                    capture_output=True, 
                    text=True, 
                    encoding='utf-8',
                    errors='replace',
                    timeout=5
                )
                if result.returncode == 0:
                    ytdlp_found = True
                    ytdlp_cmd = [YTDLP_EXE]
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            except Exception:
                pass
        else:
            # Если путь не существует, пробуем найти в PATH или как команду
            try:
                result = subprocess.run(
                    [YTDLP_EXE, "--version"], 
                    capture_output=True, 
                    text=True, 
                    encoding='utf-8',
                    errors='replace',
                    timeout=5
                )
                if result.returncode == 0:
                    ytdlp_found = True
                    ytdlp_cmd = [YTDLP_EXE]
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            except Exception:
                pass
    
    # Если не найден как команда - проверяем как Python модуль
    if not ytdlp_found:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--version"], 
                capture_output=True, 
                text=True, 
                encoding='utf-8',
                errors='replace',
                timeout=5
            )
            if result.returncode == 0:
                ytdlp_found = True
                ytdlp_cmd = [sys.executable, "-m", "yt_dlp"]
        except Exception:
            pass
    
    # Обновляем глобальную переменную для использования в коде
    if ytdlp_found and ytdlp_cmd:
        YTDLP_EXE = ytdlp_cmd
    elif not ytdlp_found:
        # Дополнительная диагностика - проверяем bin/ напрямую
        base_dir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else os.path.dirname(sys.executable)
        bin_path = os.path.join(base_dir, "bin", "yt-dlp.exe")
        if os.path.exists(bin_path):
            print(f"ℹ️ Найден yt-dlp.exe в {bin_path}, пробую использовать...")
            try:
                result = subprocess.run(
                    [bin_path, "--version"], 
                    capture_output=True, 
                    text=True, 
                    encoding='utf-8',
                    errors='replace',
                    timeout=5
                )
                if result.returncode == 0:
                    ytdlp_found = True
                    ytdlp_cmd = [bin_path]
                    YTDLP_EXE = ytdlp_cmd
                    print(f"✅ yt-dlp найден и работает: {bin_path}")
                else:
                    print(f"⚠️ yt-dlp.exe найден, но не работает (код: {result.returncode})")
            except Exception as e:
                print(f"⚠️ Ошибка при проверке yt-dlp.exe: {e}")
        
        if not ytdlp_found:
            missing.append("yt-dlp")
    
    if missing:
        print(f"❌ ❌ Отсутствуют зависимости: {', '.join(missing)}")
        print()
        print("Установите их командой:")
        for dep in missing:
            if dep == "yt-dlp":
                print(f"  pip install {dep}")
                base_dir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else os.path.dirname(sys.executable)
                bin_path = os.path.join(base_dir, "bin", "yt-dlp.exe")
                print(f"  # или скачайте yt-dlp.exe и поместите в: {bin_path}")
            else:
                print(f"  pip install {dep}")
        return False
    
    return True

check_dependencies()

def get_latest_ytdlp_version():
    """Получает последнюю версию yt-dlp с GitHub"""
    try:
        import urllib.request
        import json
        
        # Получаем информацию о последнем релизе через GitHub API
        api_url = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        
        req = urllib.request.Request(api_url)
        req.add_header('User-Agent', 'yt-dlp-updater')
        
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8', errors='replace'))
            return data.get('tag_name', '').lstrip('v')  # Убираем 'v' из версии
        
    except Exception:
        return None

def download_ytdlp_exe(download_url, save_path):
    """Скачивает yt-dlp.exe с GitHub"""
    try:
        import urllib.request
        import tempfile
        import shutil
        
        print(f"📥 Скачиваю yt-dlp.exe...")
        
        # Скачиваем во временный файл
        temp_file = save_path + ".tmp"
        
        req = urllib.request.Request(download_url)
        req.add_header('User-Agent', 'yt-dlp-updater')
        
        with urllib.request.urlopen(req, timeout=60) as response:
            total_size = int(response.headers.get('Content-Length', 0))
            downloaded = 0
            
            with open(temp_file, 'wb') as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"\r   Прогресс: {percent:.1f}%", end='', flush=True)
        
        print()  # Новая строка после прогресса
        
        # Заменяем старый файл новым
        if os.path.exists(save_path):
            # Создаем резервную копию
            backup_path = save_path + ".bak"
            try:
                shutil.copy2(save_path, backup_path)
            except Exception:
                pass
        
        # Перемещаем временный файл на место старого
        shutil.move(temp_file, save_path)
        
        # Делаем файл исполняемым (на Windows это не нужно, но для совместимости)
        try:
            os.chmod(save_path, 0o755)
        except Exception:
            pass
        
        return True
    except Exception as e:
        print(f"❌ Ошибка при скачивании: {e}")
        # Удаляем временный файл при ошибке
        try:
            if os.path.exists(save_path + ".tmp"):
                os.remove(save_path + ".tmp")
        except Exception:
            pass
        return False

def check_and_update_ytdlp():
    """Проверяет версию yt-dlp и обновляет при необходимости"""
    global YTDLP_EXE
    
    try:
        # Получаем текущую версию
        if isinstance(YTDLP_EXE, list):
            version_cmd = YTDLP_EXE + ["--version"]
            ytdlp_path = YTDLP_EXE[0] if len(YTDLP_EXE) > 0 else None
        else:
            version_cmd = [YTDLP_EXE, "--version"]
            ytdlp_path = YTDLP_EXE
        
        result = subprocess.run(
            version_cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=10
        )
        
        if result.returncode == 0:
            current_version = result.stdout.strip()
            print(f"ℹ️ Текущая версия yt-dlp: {current_version}")
            
            # Проверяем, есть ли обновления
            is_exe = ytdlp_path and os.path.exists(ytdlp_path) and ytdlp_path.endswith('.exe')
            is_python_module = isinstance(YTDLP_EXE, list) and len(YTDLP_EXE) > 1 and YTDLP_EXE[0] == sys.executable
            
            try:
                if is_python_module:
                    # Для Python модуля обновляем через pip
                    print("🔄 Проверяю обновления yt-dlp (через pip)...")
                    pip_result = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=60
                    )
                    if pip_result.returncode == 0:
                        if "already satisfied" in pip_result.stdout.lower() or "up-to-date" in pip_result.stdout.lower():
                            print("ℹ️ yt-dlp уже обновлен до последней версии")
                        else:
                            print("✅ yt-dlp обновлен до последней версии")
                elif is_exe:
                    # Для .exe файла проверяем версию на GitHub и скачиваем если нужно
                    print("🔄 Проверяю обновления yt-dlp на GitHub...")
                    
                    # Сначала пробуем встроенное обновление
                    if isinstance(YTDLP_EXE, list):
                        update_cmd = YTDLP_EXE + ["--update"]
                    else:
                        update_cmd = [YTDLP_EXE, "--update"]
                    
                    try:
                        update_result = subprocess.run(
                            update_cmd,
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            timeout=30
                        )
                        
                        if update_result.returncode == 0:
                            output = update_result.stdout + update_result.stderr
                            if "updated" in output.lower() and "already" not in output.lower():
                                print("✅ yt-dlp обновлен до последней версии (встроенное обновление)")
                                return
                            elif "already" in output.lower() or "up to date" in output.lower():
                                print("ℹ️ yt-dlp уже обновлен до последней версии")
                                return
                    except Exception:
                        pass  # Если встроенное обновление не работает, пробуем скачать вручную
                    
                    # Если встроенное обновление не сработало, скачиваем с GitHub
                    latest_version = get_latest_ytdlp_version()
                    if latest_version:
                        # Сравниваем версии (простое сравнение строк)
                        if latest_version != current_version and latest_version > current_version:
                            print(f"🆕 Найдена новая версия: {latest_version} (текущая: {current_version})")
                            
                            # URL для скачивания yt-dlp.exe для Windows
                            download_url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{latest_version}/yt-dlp.exe"
                            
                            if download_ytdlp_exe(download_url, ytdlp_path):
                                print(f"✅ yt-dlp.exe обновлен до версии {latest_version}")
                                
                                # Проверяем новую версию
                                version_result = subprocess.run(
                                    version_cmd,
                                    capture_output=True,
                                    text=True,
                                    encoding='utf-8',
                                    errors='replace',
                                    timeout=10
                                )
                                if version_result.returncode == 0:
                                    new_version = version_result.stdout.strip()
                                    print(f"ℹ️ Подтверждена версия: {new_version}")
                            else:
                                print("⚠️ Не удалось скачать обновление, используем текущую версию")
                        else:
                            print("ℹ️ yt-dlp уже обновлен до последней версии")
                    else:
                        print("⚠️ Не удалось проверить версию на GitHub")
            except subprocess.TimeoutExpired:
                print("⚠️ Таймаут при проверке обновлений yt-dlp")
            except Exception as e:
                # Игнорируем ошибки обновления
                pass
        else:
            print(f"⚠️ Не удалось проверить версию yt-dlp: {result.stderr}")
    except Exception as e:
        # Игнорируем ошибки проверки версии
        pass

import cv2
import numpy as np
from PIL import Image
import imagehash


@dataclass
class SlideCandidate:
    """Кандидат на слайд"""
    frame_number: int
    timestamp: float
    image: np.ndarray
    phash: str
    dhash: str = ""
    whash: str = ""
    avg_color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    edge_density: float = 0.0


class SlideExtractor:
    """Извлекает слайды из видео"""
    
    SOURCE_QUALITY_FORMATS = {
        "best": "best[ext=mp4]/best[height<=1080][ext=mp4]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
        "1080p": "best[height<=1080][ext=mp4]/best[height<=1080]/best[height<=720]/best",
        "720p": "best[height<=720][ext=mp4]/best[height<=720]/best[height<=480]/best",
        "480p": "best[height<=480][ext=mp4]/best[height<=480]/best[height<=360]/best",
        "360p": "best[height<=360][ext=mp4]/best[height<=360]/best",
        "fastest": "worst[ext=mp4]/worst",
    }
    
    def __init__(
        self,
        sensitivity: float = 0.85,
        min_slide_duration: float = 1.0,
        sample_interval: float = 1.0,
        transition_filter: bool = True,
        strict_dedup: bool = False,
        source_quality: str = "best",
        verbose: bool = True
    ):
        """
        Args:
            sensitivity: Чувствительность к изменениям (0.0-1.0, выше = больше слайдов)
            min_slide_duration: Минимальная длительность слайда в секундах
            sample_interval: Интервал выборки кадров в секундах
            transition_filter: Фильтровать переходные кадры
            strict_dedup: Строгая дедупликация (удаляет похожие слайды агрессивнее)
            source_quality: Качество исходника (best, 1080p, 720p, 480p, 360p, fastest)
            verbose: Выводить прогресс
        """
        self.sensitivity = sensitivity
        self.min_slide_duration = min_slide_duration
        self.sample_interval = sample_interval
        self.transition_filter = transition_filter
        self.strict_dedup = strict_dedup
        self.source_quality = source_quality if source_quality in self.SOURCE_QUALITY_FORMATS else "best"
        self.verbose = verbose
        
        # Пороги для определения изменений
        # Чем выше sensitivity, тем ниже порог (больше слайдов детектится)
        # При sensitivity=0.85: hash_threshold=8, structural_threshold=0.92
        self.hash_threshold = max(3, int(25 * (1 - sensitivity) + 3))  # 3-28
        self.structural_threshold = 0.85 + 0.10 * (1 - sensitivity)  # 0.85-0.95
    
    def log(self, msg: str):
        """Выводит сообщение если verbose=True"""
        if self.verbose:
            print(msg, flush=True)  # flush=True для немедленного вывода
    
    def download_video(self, url: str, output_dir: str) -> str:
        """Скачивает видео с YouTube в низком качестве (быстрее) с автоматическим переключением клиентов"""
        self.log(_('youtube_downloading'))
        
        # Убеждаемся что директория создана
        os.makedirs(output_dir, exist_ok=True)
        
        output_template = os.path.join(output_dir, "video.%(ext)s")
        
        # В режиме EXE: путь к yt-dlp переразрешаем при каждом вызове (актуальная папка с exe)
        ytdlp_cmd = YTDLP_EXE
        try:
            import portable_paths as _pp
            if getattr(sys, 'frozen', False):
                ytdlp_cmd = _pp.get_ytdlp_executable()
                if isinstance(ytdlp_cmd, str) and not os.path.exists(ytdlp_cmd):
                    expected_dir = os.path.join(_pp.get_base_dir(), "bin")
                    raise FileNotFoundError(
                        f"yt-dlp не найден. Поместите yt-dlp.exe в папку рядом с программой:\n"
                        f"  {expected_dir}\n"
                        f"Скачать: https://github.com/yt-dlp/yt-dlp/releases"
                    )
        except ImportError:
            pass
        
        # Список клиентов для попыток (в порядке приоритета)
        # android и ios обычно работают лучше при проблемах с SABR streaming
        clients = ["android", "ios", "web", "mweb", "tv_embedded", "default"]
        
        # Форматы по выбранному качеству исходника (основной + fallback)
        primary = self.SOURCE_QUALITY_FORMATS.get(self.source_quality, self.SOURCE_QUALITY_FORMATS["best"])
        formats = [primary]
        if self.source_quality != "best" and self.source_quality != "fastest":
            formats.extend(["best[ext=mp4]/best", "best"])
        elif self.source_quality == "best":
            formats.extend([
                "best[height<=1080]/best[height<=720]/best",
                "best[ext=mp4]/best",
                "best",
            ])
        elif self.source_quality == "fastest":
            formats.append("worst")
        
        last_error = None
        start_time = time.time()
        
        for client_idx, client in enumerate(clients, 1):
            for fmt_idx, fmt in enumerate(formats, 1):
                try:
                    # Формируем команду БЕЗ --quiet для видимости прогресса
                    if isinstance(ytdlp_cmd, list):
                        cmd = ytdlp_cmd + [
                            "-f", fmt,
                            "-o", output_template,
                            "--no-playlist",
                            "--no-warnings",
                            "--extractor-args", f"youtube:player_client={client}",
                            "--socket-timeout", "60",  # Таймаут соединения (60 сек)
                            "--retries", "3",  # Количество повторов
                            url
                        ]
                    else:
                        cmd = [
                            ytdlp_cmd,
                            "-f", fmt,
                            "-o", output_template,
                            "--no-playlist",
                            "--no-warnings",
                            "--extractor-args", f"youtube:player_client={client}",
                            "--socket-timeout", "60",
                            "--retries", "3",
                            url
                        ]
                    
                    attempt_num = (client_idx - 1) * len(formats) + fmt_idx
                    total_attempts = len(clients) * len(formats)
                    elapsed = int(time.time() - start_time)
                    self.log(f"   🔄 Попытка {attempt_num}/{total_attempts}: клиент={client}, формат={fmt}")
                    self.log(f"   ⏱️ Время с начала: {elapsed} сек")
                    
                    # Запускаем процесс с выводом в реальном времени (без чёрного окна на Windows)
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        bufsize=1,
                        startupinfo=_startupinfo,
                        **_SUBPROCESS_HIDE_WINDOW
                    )
                    
                    # Читаем вывод в реальном времени и показываем прогресс
                    last_progress_time = time.time()
                    output_lines = []
                    last_progress_line = ""
                    process_start_time = time.time()
                    max_download_time = 1800  # Максимум 30 минут на скачивание одного видео
                    
                    while True:
                        # Проверяем таймаут
                        if time.time() - process_start_time > max_download_time:
                            process.kill()
                            raise subprocess.TimeoutExpired(cmd, max_download_time)
                        
                        line = process.stdout.readline()
                        if not line:
                            if process.poll() is not None:
                                break
                            continue
                        
                        line = line.strip()
                        if line:
                            output_lines.append(line)
                            
                            # Показываем прогресс сразу (без задержки)
                            # Ищем информацию о прогрессе
                            if any(keyword in line.lower() for keyword in ["%", "eta", "downloading", "download", "of", "at", "speed", "extracting"]):
                                if line != last_progress_line:  # Не дублируем одинаковые строки
                                    self.log(f"   📥 {line[:120]}")
                                    last_progress_line = line
                                    last_progress_time = time.time()
                            elif any(keyword in line.lower() for keyword in ["error", "warning", "failed"]):
                                self.log(f"   ⚠️ {line[:120]}")
                                last_progress_time = time.time()
                            # Также показываем важные сообщения сразу
                            elif any(keyword in line.lower() for keyword in ["youtube", "downloading", "player", "api"]):
                                if "extracting" in line.lower() or "downloading" in line.lower():
                                    self.log(f"   ℹ️ {line[:120]}")
                                    last_progress_time = time.time()
                    
                    process.wait()
                    
                    # Создаем объект результата
                    result = type('obj', (object,), {
                        'returncode': process.returncode,
                        'stdout': '\n'.join(output_lines),
                        'stderr': ''
                    })()
                    
                    if result.returncode == 0:
                        # Проверяем, что файл действительно скачался и не пустой
                        video_files = [f for f in os.listdir(output_dir) if f.startswith("video.")]
                        if video_files:
                            video_path = os.path.join(output_dir, video_files[0])
                            if os.path.exists(video_path):
                                file_size = os.path.getsize(video_path)
                                if file_size > 1024:  # Минимум 1KB (пустые файлы обычно < 1KB)
                                    elapsed_total = int(time.time() - start_time)
                                    download_speed = file_size / (1024 * 1024) / max(elapsed_total, 1)  # MB/s
                                    self.log(f"   ✅ {_('youtube_downloaded')}: {os.path.basename(video_path)}")
                                    self.log(f"   📊 Размер: {file_size / (1024*1024):.1f} MB, время: {elapsed_total} сек, скорость: {download_speed:.2f} MB/s")
                                    return video_path
                                else:
                                    # Файл пустой, удаляем и пробуем дальше
                                    try:
                                        os.remove(video_path)
                                    except Exception:
                                        pass
                                    self.log(f"   ⚠️ Скачанный файл пустой ({file_size} байт), пробую другой клиент...")
                                    last_error = "Downloaded file is empty"
                                    continue
                    
                    # Если не удалось, проверяем ошибку
                    error_msg = result.stderr if result.stderr else result.stdout
                    if "empty" in error_msg.lower() or "missing a url" in error_msg.lower() or "sabr" in error_msg.lower():
                        last_error = error_msg
                        continue
                    else:
                        last_error = error_msg
                        
                except subprocess.TimeoutExpired:
                    elapsed = int(time.time() - start_time)
                    self.log(f"   ⚠️ Таймаут при скачивании ({elapsed} сек), пробую другой клиент...")
                    last_error = "Timeout"
                    continue
                except KeyboardInterrupt:
                    self.log(f"   ⏹️ Скачивание прервано пользователем")
                    raise
                except Exception as e:
                    last_error = str(e)
                    continue
        
        # Если все попытки не удались
        raise Exception(f"Не удалось скачать видео после всех попыток ({len(clients)} клиентов x {len(formats)} форматов). Последняя ошибка: {last_error}")
    
    def compute_phash(self, image: np.ndarray) -> str:
        """Вычисляет perceptual hash изображения"""
        pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))  # type: ignore[arg-type]
        return str(imagehash.phash(pil_image, hash_size=16))  # type: ignore[arg-type]
    
    def compute_dhash(self, image: np.ndarray) -> str:
        """Вычисляет difference hash изображения"""
        pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))  # type: ignore[arg-type]
        return str(imagehash.dhash(pil_image, hash_size=16))  # type: ignore[arg-type]
    
    def compute_whash(self, image: np.ndarray) -> str:
        """Вычисляет wavelet hash изображения (дополнительный метод)"""
        pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))  # type: ignore[arg-type]
        return str(imagehash.whash(pil_image, hash_size=16))  # type: ignore[arg-type]
    
    def hash_distance(self, hash1: str, hash2: str) -> int:
        """Вычисляет расстояние Хэмминга между хэшами"""
        h1 = imagehash.hex_to_hash(hash1)
        h2 = imagehash.hex_to_hash(hash2)
        return h1 - h2
    
    def compute_edge_density(self, image: np.ndarray) -> float:
        """Вычисляет плотность границ (для детекции переходов)"""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)  # type: ignore[arg-type]
        edges = cv2.Canny(gray, 50, 150)
        return np.sum(edges > 0) / edges.size
    
    def compute_avg_color(self, image: np.ndarray) -> Tuple[float, float, float]:
        """Вычисляет средний цвет изображения"""
        return tuple(np.mean(image, axis=(0, 1)))
    
    def structural_similarity(self, img1: np.ndarray, img2: np.ndarray) -> float:
        """Вычисляет структурное сходство между изображениями (учитывает цвет)"""
        # Приводим к одному размеру
        h, w = min(img1.shape[0], img2.shape[0]), min(img1.shape[1], img2.shape[1])
        img1_resized = cv2.resize(img1, (w, h))  # type: ignore[arg-type]
        img2_resized = cv2.resize(img2, (w, h))  # type: ignore[arg-type]
        
        # 1. Сравнение по яркости (grayscale)
        gray1 = cv2.cvtColor(img1_resized, cv2.COLOR_BGR2GRAY)  # type: ignore[arg-type]
        gray2 = cv2.cvtColor(img2_resized, cv2.COLOR_BGR2GRAY)  # type: ignore[arg-type]
        
        gray1_norm = gray1.astype(np.float32) / 255.0
        gray2_norm = gray2.astype(np.float32) / 255.0
        
        mean1, mean2 = np.mean(gray1_norm), np.mean(gray2_norm)
        std1, std2 = np.std(gray1_norm), np.std(gray2_norm)
        
        if std1 < 0.01 or std2 < 0.01:
            gray_sim = 1.0 if abs(mean1 - mean2) < 0.1 else 0.0
        else:
            covariance = np.mean((gray1_norm - mean1) * (gray2_norm - mean2))
            gray_sim = max(0, covariance / (std1 * std2))
        
        # 2. Сравнение по цвету (histogram)
        color_sim = 0.0
        for i in range(3):  # BGR channels
            hist1 = cv2.calcHist([img1_resized], [i], None, [64], [0, 256])
            hist2 = cv2.calcHist([img2_resized], [i], None, [64], [0, 256])
            hist1 = hist1 / hist1.sum()
            hist2 = hist2 / hist2.sum()
            color_sim += cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
        color_sim /= 3.0
        color_sim = max(0, color_sim)
        
        # Комбинируем: 50% структура + 50% цвет
        return 0.5 * gray_sim + 0.5 * color_sim
    
    def is_transition_frame(self, candidate: SlideCandidate, 
                           prev_candidate: Optional[SlideCandidate],
                           next_candidate: Optional[SlideCandidate]) -> bool:
        """Проверяет, является ли кадр переходным (fade, dissolve)"""
        if not self.transition_filter:
            return False
        
        # Переходные кадры обычно имеют:
        # 1. ЗНАЧИТЕЛЬНО более низкую плотность границ чем соседи
        # 2. Средний цвет между двумя слайдами
        
        # Проверяем только если есть соседи для сравнения
        if not prev_candidate or not next_candidate:
            return False
        
        # Переход: edge_density значительно ниже чем у соседей
        neighbor_avg_edge = (prev_candidate.edge_density + next_candidate.edge_density) / 2
        if neighbor_avg_edge > 0.01 and candidate.edge_density < neighbor_avg_edge * 0.3:
            return True
        
        return False
    
    def extract_frames(self, video_path: str) -> List[SlideCandidate]:
        """Извлекает кадры из видео"""
        self.log(_('youtube_analyzing'))
        
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise Exception(f"Не удалось открыть видео: {video_path}")
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0
        
        self.log(f"   📊 {duration:.1f} {_('youtube_stats')}: {fps:.1f}, {total_frames}")
        
        # Интервал выборки в кадрах
        sample_interval_frames = max(1, int(fps * self.sample_interval))
        
        candidates = []
        frame_number = 0
        processed = 0
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            if frame_number % sample_interval_frames == 0:
                timestamp = frame_number / fps
                
                candidate = SlideCandidate(
                    frame_number=frame_number,
                    timestamp=timestamp,
                    image=frame.copy(),
                    phash=self.compute_phash(frame),
                    dhash=self.compute_dhash(frame),
                    whash=self.compute_whash(frame),
                    avg_color=self.compute_avg_color(frame),
                    edge_density=self.compute_edge_density(frame)
                )
                candidates.append(candidate)
                processed += 1
                
                if processed % 50 == 0:
                    self.log(f"   ⏳ {_('youtube_processed')}: {processed} ({timestamp:.1f}s / {duration:.1f}s)")
            
            frame_number += 1
        
        cap.release()
        self.log(f"   ✅ {_('youtube_extracted')}: {len(candidates)}")
        
        return candidates
    
    def detect_slide_changes(self, candidates: List[SlideCandidate]) -> List[int]:
        """Определяет индексы кадров, где происходит смена слайда"""
        if len(candidates) < 2:
            return [0] if candidates else []
        
        self.log(_('youtube_searching'))
        
        change_indices = [0]  # Первый кадр всегда слайд
        
        # ИСПРАВЛЕНИЕ: Увеличиваем минимальный интервал между слайдами
        # Это поможет избежать слишком частого детектирования похожих слайдов
        min_frames_between = max(2, int(self.min_slide_duration / self.sample_interval) + 1)  # Добавили +1 для более консервативного подхода
        
        for i in range(1, len(candidates)):
            prev = candidates[i - 1]
            curr = candidates[i]
            
            # Вычисляем различия
            hash_dist = self.hash_distance(prev.phash, curr.phash)
            structural_sim = self.structural_similarity(prev.image, curr.image)
            
            # Дополнительная проверка: вычисляем разницу в цвете
            color_diff = self.avg_color_distance(prev.avg_color, curr.avg_color)
            
            # ИСПРАВЛЕНИЕ: Более консервативные пороги для определения изменений
            # Нужно находить меньше потенциальных слайдов, чтобы не было 1500+
            # Требуем БОЛЬШИЕ различия, чтобы считать это новым слайдом
            strict_hash_threshold = self.hash_threshold * 1.3  # Увеличиваем на 30% (было 20%)
            strict_struct_threshold = self.structural_threshold * 0.88  # Еще более строгий порог (было 0.91)
            
            # Требуем ОДНОВРЕМЕННО несколько условий для более консервативного подхода
            is_different = (
                # Вариант 1: Очень большая разница в хэше
                hash_dist >= strict_hash_threshold or 
                # Вариант 2: Низкое структурное сходство И большая разница в цвете
                (structural_sim < strict_struct_threshold and color_diff > 50) or
                # Вариант 3: Средняя разница в хэше И очень большая разница в цвете
                (hash_dist >= self.hash_threshold * 1.1 and color_diff > 60)
            )
            
            # Проверяем минимальный интервал от последнего слайда
            frames_since_last = i - change_indices[-1]
            
            if is_different and frames_since_last >= min_frames_between:
                # Проверяем, не является ли это переходным кадром
                prev_cand = candidates[i - 1] if i > 0 else None
                next_cand = candidates[i + 1] if i < len(candidates) - 1 else None
                
                if not self.is_transition_frame(curr, prev_cand, next_cand):
                    # ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: Сравниваем с последним найденным слайдом
                    # Если новый слайд очень похож на последний найденный - пропускаем его
                    if len(change_indices) > 0:
                        last_slide_idx = change_indices[-1]
                        last_slide = candidates[last_slide_idx]
                        
                        # Используем строгую проверку на дубликат
                        if self.are_slides_duplicate(last_slide, curr, strict_mode=True):
                            # Пропускаем этот кадр - он слишком похож на последний слайд
                            continue
                    
                    change_indices.append(i)
        
        self.log(f"   ✅ {_('youtube_found')}: {len(change_indices)}")
        
        return change_indices
    
    def are_slides_duplicate(self, candidate1: SlideCandidate, candidate2: SlideCandidate, 
                            strict_mode: bool = False) -> bool:
        """
        Комплексная проверка на дубликаты с использованием нескольких методов.
        Возвращает True если слайды считаются дубликатами.
        """
        # Пороги для строгого режима (более агрессивная дедупликация)
        if strict_mode:
            phash_threshold = 2      # МАКСИМАЛЬНО строгий порог для phash (было 3)
            dhash_threshold = 2      # МАКСИМАЛЬНО строгий порог для dhash (было 3)
            whash_threshold = 2      # МАКСИМАЛЬНО строгий порог для whash (было 3)
            struct_threshold = 0.93  # Очень высокое структурное сходство (было 0.90)
            color_diff_threshold = 15  # Еще меньшая разница в цвете (было 20)
            hash_any_threshold = 8   # Если хотя бы один хэш близок (было 10)
        else:
            phash_threshold = 4      # Еще более строгий (было 6)
            dhash_threshold = 4      # Еще более строгий (было 6)
            whash_threshold = 4      # Еще более строгий (было 6)
            struct_threshold = 0.85  # Повышено с 0.80
            color_diff_threshold = 30  # Снижено с 40
            hash_any_threshold = 12   # Снижено с 15
        
        # 1. Проверка по perceptual hash (основной метод)
        phash_dist = self.hash_distance(candidate1.phash, candidate2.phash)
        if phash_dist <= phash_threshold:
            # Если phash очень близок, проверяем цвет для подтверждения
            color_diff = self.avg_color_distance(candidate1.avg_color, candidate2.avg_color)
            if color_diff <= color_diff_threshold:
                return True  # Определенно дубликат
        
        # 2. Проверка по difference hash (дополнительный метод)
        if candidate1.dhash and candidate2.dhash:
            dhash_dist = self.hash_distance(candidate1.dhash, candidate2.dhash)
            if dhash_dist <= dhash_threshold:
                color_diff = self.avg_color_distance(candidate1.avg_color, candidate2.avg_color)
                if color_diff <= color_diff_threshold:
                    return True
        
        # 3. Проверка по wavelet hash (еще один метод)
        if candidate1.whash and candidate2.whash:
            whash_dist = self.hash_distance(candidate1.whash, candidate2.whash)
            if whash_dist <= whash_threshold:
                color_diff = self.avg_color_distance(candidate1.avg_color, candidate2.avg_color)
                if color_diff <= color_diff_threshold:
                    return True
        
        # 4. Комбинированная проверка: если хотя бы один хэш близок + структурное сходство
        min_hash_dist = min(
            phash_dist,
            self.hash_distance(candidate1.dhash, candidate2.dhash) if candidate1.dhash and candidate2.dhash else 999,
            self.hash_distance(candidate1.whash, candidate2.whash) if candidate1.whash and candidate2.whash else 999
        )
        
        if min_hash_dist <= hash_any_threshold:
            # Проверяем структурное сходство
            struct_sim = self.structural_similarity(candidate1.image, candidate2.image)
            color_diff = self.avg_color_distance(candidate1.avg_color, candidate2.avg_color)
            
            # Если структурное сходство высокое И цвет похож = дубликат
            if struct_sim >= struct_threshold and color_diff <= color_diff_threshold:
                return True
        
        # 5. Проверка на очень высокое структурное сходство (даже если хэши отличаются)
        # Это помогает найти слайды с небольшими изменениями (например, анимация)
        struct_sim = self.structural_similarity(candidate1.image, candidate2.image)
        # Еще более агрессивный порог для структурного сходства
        struct_sim_threshold = 0.90 if strict_mode else 0.88  # Снижено с 0.92
        if struct_sim >= struct_sim_threshold:
            color_diff = self.avg_color_distance(candidate1.avg_color, candidate2.avg_color)
            # В strict mode еще более строгая проверка
            color_multiplier = 0.4 if strict_mode else 0.5  # Еще более строго
            if color_diff <= color_diff_threshold * color_multiplier:
                return True
        
        # 6. НОВОЕ: Комбинированная проверка - если несколько методов показывают сходство
        # Считаем "голоса" за дубликат
        votes_for_duplicate = 0
        
        # Голос 1: phash близок (более строгий порог)
        if phash_dist <= phash_threshold * 1.3:  # Снижено с 1.5
            votes_for_duplicate += 1
        
        # Голос 2: dhash близок
        if candidate1.dhash and candidate2.dhash:
            dhash_dist = self.hash_distance(candidate1.dhash, candidate2.dhash)
            if dhash_dist <= dhash_threshold * 1.3:  # Снижено с 1.5
                votes_for_duplicate += 1
        
        # Голос 3: whash близок
        if candidate1.whash and candidate2.whash:
            whash_dist = self.hash_distance(candidate1.whash, candidate2.whash)
            if whash_dist <= whash_threshold * 1.3:  # Снижено с 1.5
                votes_for_duplicate += 1
        
        # Голос 4: структурное сходство высокое (более строгий порог)
        if struct_sim >= struct_threshold * 0.85:  # Снижено с 0.9
            votes_for_duplicate += 1
        
        # Голос 5: цвет похож (более строгий порог)
        color_diff = self.avg_color_distance(candidate1.avg_color, candidate2.avg_color)
        if color_diff <= color_diff_threshold * 1.0:  # Снижено с 1.2
            votes_for_duplicate += 1
        
        # Если несколько методов показывают сходство - считаем дубликатом
        # В strict mode достаточно 2 голосов, в обычном - 3
        # НОВОЕ: Если 4+ голосов - точно дубликат даже в обычном режиме
        if votes_for_duplicate >= 4:
            return True
        
        required_votes = 2 if strict_mode else 3
        if votes_for_duplicate >= required_votes:
            return True
        
        return False
    
    def filter_duplicates(self, candidates: List[SlideCandidate], 
                         indices: List[int]) -> List[int]:
        """Фильтрует дубликаты слайдов - улучшенная версия с множественными проверками"""
        if len(indices) < 2:
            return indices
        
        self.log(f"🧹 {_('youtube_removing_duplicates')}{'(strict mode)' if self.strict_dedup else ''}...")
        
        filtered = [indices[0]]
        
        # Дополнительная проверка: удаляем последовательные дубликаты
        # (слайды, которые идут друг за другом и очень похожи)
        sequential_removed = 0
        if len(indices) > 1:
            # Сначала проходим по последовательным парам
            new_indices = [indices[0]]
            for i in range(1, len(indices)):
                prev_idx = indices[i - 1]
                curr_idx = indices[i]
                prev_cand = candidates[prev_idx]
                curr_cand = candidates[curr_idx]
                
                # ОПТИМИЗАЦИЯ: Более агрессивная проверка последовательных слайдов
                # Используем всегда strict_mode для последовательных слайдов
                # Также проверяем временную близость - если слайды близко по времени, они скорее всего дубликаты
                time_diff = abs(curr_cand.timestamp - prev_cand.timestamp)
                is_time_close = time_diff < 10.0  # Если слайды идут в пределах 10 секунд
                
                # Если слайды близки по времени И похожи - точно дубликат
                if is_time_close and self.are_slides_duplicate(prev_cand, curr_cand, strict_mode=True):
                    sequential_removed += 1
                    continue
                
                # Даже если не близки по времени, но очень похожи - тоже дубликат
                if self.are_slides_duplicate(prev_cand, curr_cand, strict_mode=True):
                    sequential_removed += 1
                    continue
                    
                new_indices.append(curr_idx)
            
            if sequential_removed > 0:
                self.log(f"   ✅ {_('youtube_sequential_removed')}: {sequential_removed}")
            indices = new_indices
        
        # Основная проверка: сравниваем каждый слайд со всеми уже отобранными слайдами
        # ОПТИМИЗАЦИЯ: В strict mode сравниваем только с последними N слайдами для ускорения
        # (похожие слайды обычно идут близко друг к другу)
        # НОВОЕ: Увеличиваем lookback для более тщательной проверки
        lookback_limit = 50 if self.strict_dedup else len(filtered)  # Увеличено с 20 до 50
        
        for idx in indices[1:]:
            is_duplicate = False
            curr = candidates[idx]
            
            # Определяем диапазон для сравнения
            start_idx = max(0, len(filtered) - lookback_limit)
            comparison_range = filtered[start_idx:]
            
            # НОВОЕ: Дополнительная проверка временной близости
            # Если слайд близок по времени к уже отобранному и похож - это дубликат
            curr_time = candidates[idx].timestamp
            
            # Сравниваем с отобранными слайдами
            for prev_idx in comparison_range:
                prev = candidates[prev_idx]
                prev_time = prev.timestamp
                
                # Вычисляем временную разницу
                time_diff = abs(curr_time - prev_time)
                
                # НОВОЕ: Если слайды близки по времени (в пределах 30 секунд) - более строгая проверка
                if time_diff < 30.0:
                    # Используем strict_mode для временно близких слайдов
                    if self.are_slides_duplicate(prev, curr, strict_mode=True):
                        is_duplicate = True
                        break
                else:
                    # Для далеких по времени слайдов используем обычную проверку
                    if self.are_slides_duplicate(prev, curr, strict_mode=self.strict_dedup):
                        is_duplicate = True
                        break
            
            if not is_duplicate:
                filtered.append(idx)
        
        removed = len(indices) - len(filtered) + sequential_removed
        if removed > 0:
            self.log(f"   ✅ {_('youtube_total_removed')}: {removed} (из {len(indices) + sequential_removed})")
        
        return filtered
    
    def avg_color_distance(self, color1: tuple, color2: tuple) -> float:
        """Евклидово расстояние между средними цветами"""
        return np.sqrt(sum((c1 - c2) ** 2 for c1, c2 in zip(color1, color2)))
    
    def color_similarity(self, img1: np.ndarray, img2: np.ndarray) -> float:
        """Сравнение по цветовым гистограммам"""
        try:
            h, w = min(img1.shape[0], img2.shape[0]), min(img1.shape[1], img2.shape[1])
            img1_resized = cv2.resize(img1, (w, h))  # type: ignore[arg-type]
            img2_resized = cv2.resize(img2, (w, h))  # type: ignore[arg-type]
            
            similarity = 0.0
            for i in range(3):
                hist1 = cv2.calcHist([img1_resized], [i], None, [64], [0, 256])
                hist2 = cv2.calcHist([img2_resized], [i], None, [64], [0, 256])
                hist1 = hist1 / hist1.sum()
                hist2 = hist2 / hist2.sum()
                similarity += cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
            
            return max(0, similarity / 3.0)
        except Exception:
            return 0.0
    
    def try_fix_directory_permissions(self, directory: str) -> bool:
        """Пытается автоматически исправить права доступа к директории. Возвращает True если успешно."""
        try:
            directory = os.path.normpath(directory)
            
            if sys.platform == 'win32':
                # Windows: используем icacls для выдачи прав
                try:
                    # Получаем имя текущего пользователя
                    import getpass
                    username = getpass.getuser()
                    
                    # Выдаем полные права текущему пользователю
                    cmd = ['icacls', directory, '/grant', f'{username}:(OI)(CI)F', '/T']
                    result = subprocess.run(
                        cmd, 
                        capture_output=True, 
                        text=True, 
                        encoding='utf-8',
                        errors='replace',
                        timeout=10,
                        **_SUBPROCESS_HIDE_WINDOW
                    )
                    
                    if result.returncode == 0:
                        self.log(f"   ✅ Права доступа успешно выданы через icacls")
                        return True
                    else:
                        self.log(f"   ⚠️ Не удалось выдать права через icacls: {result.stderr}")
                except Exception as e:
                    self.log(f"   ⚠️ Ошибка при попытке выдать права: {e}")
            else:
                # Linux/Mac: используем chmod
                try:
                    # Выдаем права на чтение, запись и выполнение владельцу
                    os.chmod(directory, 0o755)
                    self.log(f"   ✅ Права доступа успешно выданы через chmod")
                    return True
                except Exception as e:
                    self.log(f"   ⚠️ Ошибка при попытке выдать права: {e}")
            
            return False
        except Exception as e:
            self.log(f"   ⚠️ Ошибка при попытке исправить права: {e}")
            return False
    
    def get_permission_instructions(self, directory: str) -> str:
        """Возвращает инструкции по выдаче прав доступа для пользователя"""
        directory = os.path.normpath(directory)
        
        if sys.platform == 'win32':
            import getpass
            username = getpass.getuser()
            
            instructions = f"""
📋 ИНСТРУКЦИЯ ПО ВЫДАЧЕ ПРАВ ДОСТУПА (Windows):

1. ЧЕРЕЗ ПРОВОДНИК (самый простой способ):
   - Откройте проводник Windows
   - Найдите папку: {directory}
   - Правой кнопкой мыши → Свойства
   - Вкладка "Безопасность" → кнопка "Изменить"
   - Выберите "{username}" → установите "Полный доступ"
   - Нажмите "Применить" и "OK"

2. ЧЕРЕЗ КОМАНДНУЮ СТРОКУ (от имени администратора):
   - Откройте PowerShell или CMD от имени администратора
   - Выполните команду:
     icacls "{directory}" /grant {username}:(OI)(CI)F /T
   
3. ПЕРЕМЕСТИТЬ ПАПКУ:
   - Если папка в системной директории, переместите её в:
     C:\\Users\\{username}\\youtube_slides

4. ЗАПУСТИТЬ ОТ ИМЕНИ АДМИНИСТРАТОРА:
   - Правой кнопкой на программе → "Запуск от имени администратора"
"""
        else:
            instructions = f"""
📋 ИНСТРУКЦИЯ ПО ВЫДАЧЕ ПРАВ ДОСТУПА (Linux/Mac):

1. ЧЕРЕЗ ТЕРМИНАЛ:
   - Откройте терминал
   - Выполните команду:
     sudo chmod -R 755 "{directory}"
   
2. ИЗМЕНИТЬ ВЛАДЕЛЬЦА:
   - Выполните команду:
     sudo chown -R $USER:$USER "{directory}"

3. ПЕРЕМЕСТИТЬ ПАПКУ:
   - Если папка в системной директории, переместите её в домашнюю:
     mv "{directory}" ~/youtube_slides
"""
        
        return instructions
    
    def check_directory_access(self, directory: str):
        """Проверяет права доступа к директории. Возвращает (успех, сообщение)"""
        try:
            # Нормализуем путь
            directory = os.path.normpath(directory)
            
            # Проверяем существование
            if not os.path.exists(directory):
                return False, f"Директория не существует: {directory}"
            
            # Проверяем, что это действительно директория
            if not os.path.isdir(directory):
                return False, f"Путь существует, но это не директория: {directory}"
            
            # Проверяем права на чтение
            if not os.access(directory, os.R_OK):
                # Пробуем автоматически исправить
                if self.try_fix_directory_permissions(directory):
                    # Проверяем снова
                    if os.access(directory, os.R_OK):
                        pass  # Успешно исправлено
                    else:
                        return False, f"Нет прав на чтение директории: {directory}"
                else:
                    return False, f"Нет прав на чтение директории: {directory}"
            
            # Проверяем права на запись
            if not os.access(directory, os.W_OK):
                # Пробуем автоматически исправить
                if self.try_fix_directory_permissions(directory):
                    # Проверяем снова
                    if os.access(directory, os.W_OK):
                        pass  # Успешно исправлено
                    else:
                        return False, f"Нет прав на запись в директорию: {directory}"
                else:
                    return False, f"Нет прав на запись в директорию: {directory}"
            
            # Проверяем права на выполнение (для доступа к содержимому)
            if not os.access(directory, os.X_OK):
                # Пробуем автоматически исправить
                if self.try_fix_directory_permissions(directory):
                    # Проверяем снова
                    if os.access(directory, os.X_OK):
                        pass  # Успешно исправлено
                    else:
                        return False, f"Нет прав на выполнение (доступ к содержимому) директории: {directory}"
                else:
                    return False, f"Нет прав на выполнение (доступ к содержимому) директории: {directory}"
            
            # Пробуем создать тестовый файл
            test_file = os.path.join(directory, f".test_write_{os.getpid()}.tmp")
            try:
                with open(test_file, 'w') as f:
                    f.write("test")
                os.remove(test_file)
            except Exception as e:
                return False, f"Не удалось создать тестовый файл в директории: {e}"
            
            # Проверяем свободное место на диске
            try:
                stat = os.statvfs(directory) if hasattr(os, 'statvfs') else None
                if stat:
                    free_space_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
                    if free_space_mb < 10:
                        return False, f"Мало свободного места на диске: {free_space_mb:.1f} MB"
            except Exception:
                pass  # На Windows statvfs может быть недоступен
            
            # На Windows проверяем через shutil.disk_usage
            try:
                if sys.platform == 'win32':
                    total, used, free = shutil.disk_usage(directory)
                    free_mb = free / (1024 * 1024)
                    if free_mb < 10:
                        return False, f"Мало свободного места на диске: {free_mb:.1f} MB (требуется минимум 10 MB)"
            except Exception as e:
                self.log(f"   ⚠️ Не удалось проверить свободное место: {e}")
            
            return True, "OK"
            
        except Exception as e:
            return False, f"Ошибка при проверке директории: {e}"
    
    def save_slides(self, candidates: List[SlideCandidate], 
                   indices: List[int], output_dir: str) -> List[str]:
        """Сохраняет слайды в файлы"""
        self.log(_('youtube_saving'))
        self.log(f"   📁 {_('youtube_directory')}: {output_dir}")
        self.log(f"   📊 {_('youtube_total_slides')}: {len(indices)}")
        
        # Нормализуем путь
        output_dir = os.path.normpath(output_dir)
        
        # На Windows проверяем длину пути
        if sys.platform == 'win32' and len(output_dir) > 200:
            self.log(f"   ⚠️ Длинный путь ({len(output_dir)} символов), может быть проблема с MAX_PATH")
        
        # Создаем директорию с обработкой ошибок
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            self.log(f"   ❌ Ошибка при создании директории: {e}")
            return []
        
        # Детальная проверка прав доступа
        access_ok, access_msg = self.check_directory_access(output_dir)
        if not access_ok:
            self.log(f"   ❌ Проблема с доступом к директории: {access_msg}")
            self.log(f"   📁 Путь: {output_dir}")
            self.log(f"   💡 Попытка автоматического исправления...")
            
            # Пробуем автоматически исправить права
            if not self.try_fix_directory_permissions(output_dir):
                # Если автоматическое исправление не помогло, показываем инструкции
                self.log(f"   ❌ Автоматическое исправление не удалось")
                self.log(self.get_permission_instructions(output_dir))
                return []
            else:
                # Проверяем снова после исправления
                access_ok, access_msg = self.check_directory_access(output_dir)
                if not access_ok:
                    self.log(f"   ❌ После исправления проблема осталась: {access_msg}")
                    self.log(self.get_permission_instructions(output_dir))
                    return []
        
        self.log(f"   ✅ Директория доступна для записи: {output_dir}")
        
        saved_files = []
        failed_count = 0
        
        for i, idx in enumerate(indices):
            candidate = candidates[idx]
            
            try:
                # Форматируем имя файла с номером и временем
                minutes = int(candidate.timestamp // 60)
                seconds = int(candidate.timestamp % 60)
                
                filename = f"slide_{i+1:03d}_at_{minutes:02d}m{seconds:02d}s.png"
                
                # Нормализуем путь для избежания проблем с кодировкой
                filepath = os.path.normpath(os.path.join(output_dir, filename))
                
                # На Windows проверяем длину пути (MAX_PATH = 260 символов)
                # cv2.imwrite не всегда работает с \\?\ префиксом, поэтому используем оригинальный путь
                # но проверяем длину и предупреждаем
                if sys.platform == 'win32' and len(filepath) > 260:
                    self.log(f"   ⚠️ Слайд {i+1}: путь длиннее 260 символов ({len(filepath)}): {filepath[:80]}...")
                    # Пробуем использовать короткое имя файла
                    try:
                        # Создаем короткое имя через 8.3 формат (опционально, если pywin32 установлен)
                        try:
                            import win32api
                            short_path = win32api.GetShortPathName(output_dir)
                            filepath = os.path.normpath(os.path.join(short_path, filename))
                            self.log(f"   ℹ️ Используем короткий путь: {filepath[:80]}...")
                        except ImportError:
                            # Если win32api недоступен, пробуем как есть
                            self.log(f"   ⚠️ pywin32 не установлен, используем длинный путь (может не работать)")
                    except Exception as e:
                        # Если GetShortPathName не работает, пробуем как есть
                        self.log(f"   ⚠️ Не удалось получить короткий путь: {e}")
                
                # Проверяем, что изображение не пустое
                if candidate.image is None or candidate.image.size == 0:
                    self.log(f"   ⚠️ Слайд {i+1}: изображение пустое, пропускаем")
                    failed_count += 1
                    continue
                
                # Сохраняем изображение с обработкой ошибок
                try:
                    # Проверяем размер изображения перед сохранением
                    if candidate.image.shape[0] == 0 or candidate.image.shape[1] == 0:
                        self.log(f"   ⚠️ Слайд {i+1}: неверный размер изображения {candidate.image.shape}")
                        failed_count += 1
                        continue
                    
                    # Пробуем сохранить с разными параметрами сжатия
                    success = False
                    last_error = None
                    
                    # Попытка 1: с параметрами сжатия
                    try:
                        # Проверяем, что директория существует перед сохранением
                        dir_exists = os.path.exists(os.path.dirname(filepath))
                        if not dir_exists:
                            last_error = f"Директория не существует: {os.path.dirname(filepath)}"
                        else:
                            success = cv2.imwrite(filepath, candidate.image, [cv2.IMWRITE_PNG_COMPRESSION, 5])  # type: ignore[arg-type]
                            if not success:
                                # Проверяем, может быть файл все-таки создан
                                if os.path.exists(filepath):
                                    success = True  # Файл создан, считаем успешным
                                else:
                                    last_error = f"cv2.imwrite вернул False, файл не создан. Путь: {filepath[:100]}"
                    except Exception as e:
                        last_error = f"Ошибка при сохранении с параметрами: {e}"
                        import traceback
                        self.log(f"      Детали ошибки: {traceback.format_exc()[:200]}")
                    
                    # Попытка 2: без параметров сжатия
                    if not success:
                        try:
                            success = cv2.imwrite(filepath, candidate.image)  # type: ignore[arg-type]
                            if not success:
                                last_error = "cv2.imwrite вернул False без параметров"
                        except Exception as e:
                            last_error = f"Ошибка при сохранении без параметров: {e}"
                    
                    # Попытка 3: через PIL как запасной вариант
                    if not success:
                        try:
                            from PIL import Image
                            import numpy as np
                            # Конвертируем BGR в RGB для PIL
                            if len(candidate.image.shape) == 3:
                                rgb_image = cv2.cvtColor(candidate.image, cv2.COLOR_BGR2RGB)  # type: ignore[arg-type, call-overload]
                            else:
                                rgb_image = candidate.image
                            pil_image = Image.fromarray(rgb_image)
                            pil_image.save(filepath, 'PNG', compress_level=5)
                            success = True
                            self.log(f"   ℹ️ Слайд {i+1}: сохранен через PIL (запасной метод)")
                        except Exception as e:
                            last_error = f"Ошибка при сохранении через PIL: {e}"
                    
                    if success:
                        # Проверяем, что файл действительно создан и не пуст
                        # Используем нормализованный путь для проверки
                        check_path = os.path.normpath(filepath)
                        
                        # Небольшая задержка для Windows, чтобы файл успел записаться
                        import time
                        time.sleep(0.01)
                        
                        if os.path.exists(check_path):
                            file_size = os.path.getsize(check_path)
                            if file_size > 0:
                                saved_files.append(check_path)
                                if (i + 1) % 50 == 0:  # Логируем каждые 50 слайдов
                                    self.log(f"   ✅ Сохранено {i + 1}/{len(indices)} слайдов...")
                            else:
                                self.log(f"   ⚠️ Слайд {i+1}: файл создан, но пуст ({filename}, размер: {file_size} байт)")
                                failed_count += 1
                                # Удаляем пустой файл
                                try:
                                    os.remove(check_path)
                                except Exception:
                                    pass
                        else:
                            # Пробуем найти файл с разными вариантами пути
                            found = False
                            for alt_path in [filepath, os.path.abspath(filepath), os.path.realpath(filepath)]:
                                if os.path.exists(alt_path):
                                    file_size = os.path.getsize(alt_path)
                                    if file_size > 0:
                                        saved_files.append(alt_path)
                                        found = True
                                        break
                            
                            if not found:
                                self.log(f"   ❌ Слайд {i+1}: файл не создан ({filename})")
                                self.log(f"      Путь: {filepath[:150]}")
                                self.log(f"      Длина пути: {len(filepath)} символов")
                                failed_count += 1
                    else:
                        self.log(f"   ❌ Слайд {i+1}: не удалось сохранить ({filename}): {last_error}")
                        failed_count += 1
                        
                except cv2.error as e:
                    self.log(f"   ❌ Слайд {i+1}: ошибка OpenCV при сохранении ({filename}): {e}")
                    failed_count += 1
                except OSError as e:
                    self.log(f"   ❌ Слайд {i+1}: ошибка ОС/ввода-вывода при сохранении ({filename}): {e}")
                    failed_count += 1
                except Exception as e:
                    self.log(f"   ❌ Слайд {i+1}: неожиданная ошибка при сохранении ({filename}): {e}")
                    failed_count += 1
                    
            except Exception as e:
                self.log(f"   ❌ Слайд {i+1}: ошибка обработки: {e}")
                failed_count += 1
        
        if failed_count > 0:
            self.log(f"   ⚠️ Не удалось сохранить {failed_count} слайдов из {len(indices)}")
        
        self.log(f"   ✅ Сохранено {len(saved_files)} слайдов в {output_dir}")
        
        if len(saved_files) == 0 and len(indices) > 0:
            self.log(f"   ❌ КРИТИЧЕСКАЯ ОШИБКА: не удалось сохранить ни одного слайда!")
            self.log(f"   💡 Проверьте права доступа к директории: {output_dir}")
            self.log(f"   💡 Проверьте свободное место на диске")
        
        return saved_files
    
    def extract_from_url(self, url: str, output_dir: str) -> List[str]:
        """Основной метод: извлекает слайды из YouTube URL"""
        
        # Нормализуем путь выходной директории
        output_dir = os.path.normpath(output_dir)
        
        # Убеждаемся что выходная директория создана
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            self.log(f"❌ Ошибка при создании выходной директории: {e}")
            return []
        
        # Детальная проверка прав доступа
        access_ok, access_msg = self.check_directory_access(output_dir)
        if not access_ok:
            self.log(f"❌ Проблема с доступом к выходной директории: {access_msg}")
            self.log(f"📁 Путь: {output_dir}")
            self.log(f"💡 Попытка автоматического исправления...")
            
            # Пробуем автоматически исправить права
            if not self.try_fix_directory_permissions(output_dir):
                # Если автоматическое исправление не помогло, показываем инструкции
                self.log(f"❌ Автоматическое исправление не удалось")
                self.log(self.get_permission_instructions(output_dir))
                return []
            else:
                # Проверяем снова после исправления
                access_ok, access_msg = self.check_directory_access(output_dir)
                if not access_ok:
                    self.log(f"❌ После исправления проблема осталась: {access_msg}")
                    self.log(self.get_permission_instructions(output_dir))
                    return []
        
        self.log(f"✅ Выходная директория доступна для записи: {output_dir}")
        
        # Создаём временную директорию для видео
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix="slide_extractor_")
            
            # 1. Скачиваем видео
            video_path = self.download_video(url, temp_dir)
            
            # 2. Извлекаем кадры
            candidates = self.extract_frames(video_path)
            
            if not candidates:
                self.log("❌ Не удалось извлечь кадры из видео")
                return []
            
            # 3. Определяем смены слайдов
            change_indices = self.detect_slide_changes(candidates)
            
            if not change_indices:
                self.log("⚠️ Не найдено смен слайдов в видео")
                return []
            
            # 4. Фильтруем дубликаты
            unique_indices = self.filter_duplicates(candidates, change_indices)
            
            if not unique_indices:
                self.log("⚠️ После фильтрации дубликатов не осталось слайдов")
                return []
            
            # 5. Сохраняем слайды
            saved_files = self.save_slides(candidates, unique_indices, output_dir)
            
            return saved_files
            
        except Exception as e:
            self.log(f"❌ КРИТИЧЕСКАЯ ОШИБКА при извлечении слайдов: {e}")
            import traceback
            self.log(f"   Детали: {traceback.format_exc()}")
            return []
        finally:
            # Очищаем временные файлы
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    self.log(f"⚠️ Не удалось удалить временную директорию: {e}")
    
    def extract_from_file(self, video_path: str, output_dir: str) -> List[str]:
        """Извлекает слайды из локального видеофайла"""
        
        if not os.path.exists(video_path):
            raise Exception(f"Файл не найден: {video_path}")
        
        # Убеждаемся что выходная директория создана
        os.makedirs(output_dir, exist_ok=True)
        
        # 1. Извлекаем кадры
        candidates = self.extract_frames(video_path)
        
        # 2. Определяем смены слайдов
        change_indices = self.detect_slide_changes(candidates)
        
        # 3. Фильтруем дубликаты
        unique_indices = self.filter_duplicates(candidates, change_indices)
        
        # 4. Сохраняем слайды
        saved_files = self.save_slides(candidates, unique_indices, output_dir)
        
        return saved_files


def get_video_title(url: str) -> str:
    """Получает название видео для имени папки"""
    try:
        # YTDLP_EXE может быть строкой или списком
        if isinstance(YTDLP_EXE, list):
            cmd = YTDLP_EXE + ["--get-title", "--no-playlist", url]
        else:
            cmd = [YTDLP_EXE, "--get-title", "--no-playlist", url]
        result = subprocess.run(
            cmd,
            capture_output=True, 
            text=True, 
            encoding='utf-8',
            errors='replace',
            timeout=30,
            **_SUBPROCESS_HIDE_WINDOW
        )
        if result.returncode == 0:
            title = result.stdout.strip()
            # Очищаем от недопустимых символов
            title = re.sub(r'[\\/*?:"<>|]', '_', title)
            return title[:50]  # Ограничиваем длину
    except Exception:
        pass
    return ""  # Возвращаем пустую строку вместо None


def main():
    try:
        check_and_update_ytdlp()
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Извлекает слайды из YouTube видео",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  %(prog)s "https://youtube.com/watch?v=xxx"
  %(prog)s "https://youtube.com/watch?v=xxx" --output ./my_slides
  %(prog)s "https://youtube.com/watch?v=xxx" --sensitivity 0.9
  %(prog)s video.mp4 --local
        """
    )
    
    parser.add_argument("source", help="YouTube URL или путь к локальному видео")
    parser.add_argument("-o", "--output", help="Папка для сохранения слайдов")
    parser.add_argument("-s", "--sensitivity", type=float, default=0.80,
                       help="Чувствительность 0.0-1.0 (выше = больше слайдов, default: 0.80)")
    parser.add_argument("--min-duration", type=float, default=2.0,
                       help="Минимальная длительность слайда в секундах (default: 2.0)")
    parser.add_argument("--sample-interval", type=float, default=1.0,
                       help="Интервал выборки кадров в секундах (default: 1.0, больше = быстрее)")
    parser.add_argument("--no-transition-filter", action="store_true",
                       help="Не фильтровать переходные кадры")
    parser.add_argument("--strict", action="store_true",
                       help="Строгая дедупликация (меньше похожих слайдов)")
    parser.add_argument("--local", action="store_true",
                       help="Источник - локальный файл, а не YouTube URL")
    parser.add_argument("--source-quality", choices=["best", "1080p", "720p", "480p", "360p", "fastest"],
                       default="best",
                       help="Качество исходного видео при скачивании (default: best)")
    parser.add_argument("-q", "--quiet", action="store_true",
                       help="Минимальный вывод")
    
    args = parser.parse_args()
    
    # Определяем папку вывода
    if args.output:
        output_dir = args.output
    else:
        if args.local:
            base_name = os.path.splitext(os.path.basename(args.source))[0]
            output_dir = f"{base_name}_slides"
        else:
            title = get_video_title(args.source)
            if title:
                output_dir = f"{title}_slides"
            else:
                output_dir = f"slides_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Создаём экстрактор
    extractor = SlideExtractor(
        sensitivity=args.sensitivity,
        min_slide_duration=args.min_duration,
        sample_interval=args.sample_interval,
        transition_filter=not args.no_transition_filter,
        strict_dedup=args.strict,
        source_quality=args.source_quality,
        verbose=not args.quiet
    )
    
    print(_('youtube_title'))
    print(f"=" * 50)
    
    try:
        if args.local:
            slides = extractor.extract_from_file(args.source, output_dir)
        else:
            slides = extractor.extract_from_url(args.source, output_dir)
        
        print(f"\n{'=' * 50}")
        print(f"✅ {_('youtube_done')}: {len(slides)}")
        print(f"📁 {_('youtube_folder')}: {os.path.abspath(output_dir)}")
        
        if slides:
            print(f"\n{_('youtube_first_slides')}:")
            for slide in slides[:5]:
                print(f"  • {os.path.basename(slide)}")
            if len(slides) > 5:
                print(f"  ... {_('youtube_and_more')} {len(slides) - 5}")
    
    except KeyboardInterrupt:
        print(f"\n{_('youtube_interrupted')}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ {_('youtube_error')}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
