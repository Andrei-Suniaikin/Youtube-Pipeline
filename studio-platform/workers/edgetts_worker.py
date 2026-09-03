import os
import argparse
import subprocess
import shutil
import tempfile
import json
import sys
import glob
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

# Импорт локализации
try:
    from i18n import _
except ImportError:
    # Если модуль не найден, используем функцию-заглушку
    def _(key, *args, **kwargs):
        return key

# Темп речи (atempo через ffmpeg); ставится из --speed в main()
try:
    from portable_paths import apply_audio_speed
except Exception:
    def apply_audio_speed(_p, _s, strict=False):
        if strict:
            raise RuntimeError("portable_paths.apply_audio_speed недоступен")
        return False
TTS_SPEED = 1.0

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
    def print_progress(current, total, item_name="элементов"):
        percentage = int((current / total) * 100) if total > 0 else 0
        print_user(f"Обработка {item_name}: {current} из {total} ({percentage}%)", "PROCESS")
    def print_error_user_friendly(error_msg, show_debug_hint=True):
        print_user(error_msg, "ERROR")

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

# Скрытие консольного окна при запуске дочерних процессов из exe (Windows)
_SUBPROCESS_HIDE_WINDOW = {}
if sys.platform == 'win32':
    _SUBPROCESS_HIDE_WINDOW = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}

# --- НАСТРОЙКИ ---
AUDIO_DIR_NAME = "_FINAL_AUDIO_EDGETTS"
# 10 параллельных потоков по вашему запросу
PARALLEL_REQUESTS = 10 
# Консервативный лимит символов одного запроса EdgeTTS.
MAX_CHUNK_SIZE = 4000
CACHE_MANIFEST_NAME = ".edgetts_manifest.json"
CACHE_MANIFEST_VERSION = 2  # v2: speed transform подтверждён до публикации чанка

_NUMERIC_MP3_RE = re.compile(r"^\d+\.mp3$", re.IGNORECASE)
_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?:[.!?…]+|[。！？]+)(?:[\"'»”’\)\]\}]+)?(?:\s+|$)"
)

# Флаг использования Python API вместо CLI
USE_PYTHON_API = False
try:
    import edge_tts
    USE_PYTHON_API = True
except ImportError:
    USE_PYTHON_API = False 


def _normalize_paragraphs_text(paragraphs):
    """Возвращает канонический текст, который целиком должен попасть в TTS.

    Вход кабинета и десктопа уже является списком абзацев. Для TTS переводы
    строк и повторные пробелы не несут смысла, поэтому нормализуем их в один
    пробел. Остальные символы (включая кавычки и пунктуацию) не меняем.
    """
    if not isinstance(paragraphs, list):
        raise ValueError("paragraphs_file должен содержать JSON-массив")

    normalized = []
    for index, paragraph in enumerate(paragraphs, 1):
        if not isinstance(paragraph, dict) or not isinstance(paragraph.get("text"), str):
            raise ValueError(f"Абзац {index}: ожидается объект со строковым полем text")
        text = " ".join(paragraph["text"].split())
        if text:
            normalized.append(text)
    return " ".join(normalized)


def _split_text_exact(text, max_chunk_size=MAX_CHUNK_SIZE):
    """Делит канонический текст на точные непрерывные срезы заданного размера.

    Приоритет границ: конец предложения, затем пробел, затем жёсткий срез для
    единственного сверхдлинного токена. Никакой символ не удаляется и не
    дублируется: ``"".join(result) == text``.
    """
    if not isinstance(text, str):
        raise ValueError("Текст TTS должен быть строкой")
    if not isinstance(max_chunk_size, int) or max_chunk_size <= 0:
        raise ValueError("max_chunk_size должен быть положительным целым числом")
    if not text:
        return []

    result = []
    start = 0
    text_length = len(text)
    while start < text_length:
        hard_end = min(start + max_chunk_size, text_length)
        if hard_end == text_length:
            end = hard_end
        else:
            end = None
            for match in _SENTENCE_BOUNDARY_RE.finditer(text, start, hard_end):
                if match.end() > start:
                    end = match.end()
            if end is None:
                whitespace = max(
                    text.rfind(" ", start + 1, hard_end),
                    text.rfind("\t", start + 1, hard_end),
                    text.rfind("\n", start + 1, hard_end),
                )
                end = whitespace + 1 if whitespace >= start + 1 else hard_end

        if end <= start or end - start > max_chunk_size:
            raise RuntimeError("Внутренняя ошибка разбиения EdgeTTS")
        result.append(text[start:end])
        start = end

    if "".join(result) != text:
        raise RuntimeError("Разбиение EdgeTTS потеряло или продублировало текст")
    if any(not chunk or len(chunk) > max_chunk_size for chunk in result):
        raise RuntimeError("Разбиение EdgeTTS создало недопустимый чанк")
    return result


def _chunk_text(paragraphs):
    """Собирает текст одного задания в точности как его отправит process_chunk."""
    if not isinstance(paragraphs, list):
        raise ValueError("Чанк EdgeTTS должен быть списком")
    parts = []
    for index, paragraph in enumerate(paragraphs, 1):
        if not isinstance(paragraph, dict) or not isinstance(paragraph.get("text"), str):
            raise ValueError(f"Элемент чанка {index}: отсутствует строковое поле text")
        parts.append(paragraph["text"])
    return " ".join(parts)


def _build_chunks(paragraphs, max_chunk_size=MAX_CHUNK_SIZE):
    """Строит задания EdgeTTS и до запуска доказывает полное покрытие текста."""
    canonical_text = _normalize_paragraphs_text(paragraphs)
    pieces = _split_text_exact(canonical_text, max_chunk_size=max_chunk_size)
    # Один точный срез на задание: process_chunk не добавит разделитель на границе.
    chunks = [[{"text": piece}] for piece in pieces]
    rendered_text = "".join(_chunk_text(chunk) for chunk in chunks)
    if rendered_text != canonical_text:
        raise RuntimeError("План EdgeTTS не покрывает канонический текст целиком")
    if any(len(_chunk_text(chunk)) > max_chunk_size for chunk in chunks):
        raise RuntimeError("План EdgeTTS содержит слишком длинный чанк")
    return canonical_text, chunks


def _cache_manifest(canonical_text, chunks, voice_name, speed):
    chunk_texts = [_chunk_text(chunk) for chunk in chunks]
    return {
        "version": CACHE_MANIFEST_VERSION,
        "voice": str(voice_name),
        "speed": format(float(speed), ".12g"),
        "max_chunk_size": MAX_CHUNK_SIZE,
        "text_sha256": hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
        "chunks": [
            {
                "file": f"{index}.mp3",
                "chars": len(chunk_text),
                "sha256": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
            }
            for index, chunk_text in enumerate(chunk_texts, 1)
        ],
    }


def _numeric_mp3_files(output_dir):
    try:
        names = os.listdir(output_dir)
    except FileNotFoundError:
        return {}
    return {
        name: os.path.join(output_dir, name)
        for name in names
        if _NUMERIC_MP3_RE.fullmatch(name)
    }


def _cache_is_valid(output_dir, expected_manifest):
    manifest_path = os.path.join(output_dir, CACHE_MANIFEST_NAME)
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            actual_manifest = json.load(stream)
    except (OSError, ValueError, TypeError):
        return False
    if actual_manifest != expected_manifest:
        return False

    return _audio_outputs_complete(output_dir, expected_manifest)


def _audio_outputs_complete(output_dir, expected_manifest):
    """Проверяет точный набор непустых numeric MP3 без опоры на их количество."""

    expected_names = {item["file"] for item in expected_manifest["chunks"]}
    actual_files = _numeric_mp3_files(output_dir)
    if set(actual_files) != expected_names:
        return False
    return all(os.path.isfile(path) and os.path.getsize(path) > 0 for path in actual_files.values())


def _clear_generated_audio(output_dir):
    """Удаляет только файлы, которыми владеет этот worker; чужие не трогает."""
    for path in _numeric_mp3_files(output_dir).values():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    for path in glob.glob(os.path.join(output_dir, ".edgetts-*.mp3")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    for path in glob.glob(os.path.join(output_dir, ".edgetts-manifest-*.json")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    try:
        os.remove(os.path.join(output_dir, CACHE_MANIFEST_NAME))
    except FileNotFoundError:
        pass


def _write_cache_manifest(output_dir, manifest):
    fd, temp_path = tempfile.mkstemp(
        prefix=".edgetts-manifest-", suffix=".json", dir=output_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, os.path.join(output_dir, CACHE_MANIFEST_NAME))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _apply_audio_speed_or_raise(audio_path, speed):
    """Применяет speed до публикации staging MP3 и требует явного успеха."""
    applied = apply_audio_speed(audio_path, speed, strict=True)
    if applied is not True:
        raise RuntimeError(
            f"не удалось подтвердить изменение скорости аудио до {speed!r}"
        )


def check_dependencies():
    """Проверяет наличие edge-tts через разные способы."""
    print_debug(_('edgetts_checking'))
    
    # Способ 1: Проверка через Python API (самый надежный)
    try:
        import edge_tts
        print_debug(_('edgetts_found_api'))
        return True
    except ImportError:
        print_debug(f"   {_('edgetts_not_found_api')}")
    except Exception as e:
        print(f"   ⚠️ Ошибка при импорте edge-tts: {e}", flush=True)
    
    # Способ 2: Проверка через shutil.which
    try:
        edge_tts_path = shutil.which("edge-tts")
        if edge_tts_path:
            print(f"{_('edgetts_found_path')}: {edge_tts_path}", flush=True)
            return True
        else:
            print(f"   {_('edgetts_not_found_path')}", flush=True)
    except (UnicodeDecodeError, UnicodeError, AttributeError) as e:
        print(f"   ⚠️ Ошибка кодировки при поиске в PATH: {e}", flush=True)
    except Exception as e:
        print(f"   ⚠️ Ошибка при поиске в PATH: {e}", flush=True)
    
    # Способ 3: Прямой запуск команды edge-tts
    try:
        result = subprocess.run(
            ["edge-tts", "--version"], 
            capture_output=True, 
            timeout=5,
            text=False,
            **_SUBPROCESS_HIDE_WINDOW
        )
        if result.returncode == 0:
            print(_('edgetts_found_cmd'), flush=True)
            return True
        else:
            print(f"   ⚠️ edge-tts вернул код {result.returncode}", flush=True)
    except FileNotFoundError:
        print(f"   {_('edgetts_not_found_cmd')}", flush=True)
    except subprocess.TimeoutExpired:
        print("   ⚠️ Таймаут при проверке edge-tts", flush=True)
    except Exception as e:
        print(f"   ⚠️ Ошибка при запуске edge-tts: {e}", flush=True)
    
    # Способ 4: Через python -m edge_tts
    try:
        result = subprocess.run(
            [sys.executable, "-m", "edge_tts", "--version"], 
            capture_output=True, 
            timeout=5,
            text=False,
            **_SUBPROCESS_HIDE_WINDOW
        )
        if result.returncode == 0:
            print(_('edgetts_found_module'), flush=True)
            return True
    except Exception as e:
        print(f"   ⚠️ Ошибка при проверке через python -m: {e}", flush=True)
    
    # Способ 5: Проверка через pip list
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list"], 
            capture_output=True, 
            timeout=10,
            text=True,
            encoding='utf-8',
            errors='replace',
            **_SUBPROCESS_HIDE_WINDOW
        )
        if result.returncode == 0 and 'edge-tts' in result.stdout.lower():
            print("✅ edge-tts найден в установленных пакетах, но не доступен как команда", flush=True)
            print("   💡 Попробуйте переустановить: pip install --upgrade edge-tts", flush=True)
            # Все равно возвращаем True, так как библиотека установлена
            return True
    except Exception as e:
        print(f"   ⚠️ Ошибка при проверке через pip list: {e}", flush=True)
    
    print_error_user_friendly(_('edgetts_error'))
    print_user(f"   {_('edgetts_install')}", "INFO")
    print_user("   🔄 Или переустановите: pip install --upgrade --force-reinstall edge-tts", "INFO")
    return False

def process_chunk(task_data):
    """
    Обрабатывает один большой чанк текста и сохраняет его как один MP3-файл.
    НЕ ИСПОЛЬЗУЕТ FFMPEG.
    """
    chunk_index, paragraphs, voice_name, output_dir = task_data
    
    # Имя файла будет 1.mp3, 2.mp3 и т.д. До полного успеха пишем в скрытый
    # staging-файл, чтобы рендер никогда не увидел частично записанный MP3.
    output_mp3_path = os.path.join(output_dir, f"{chunk_index}.mp3")
    staging_mp3_path = os.path.join(
        output_dir, f".edgetts-{chunk_index}-{os.getpid()}.mp3"
    )
    
    paragraph_count = len(paragraphs) if isinstance(paragraphs, list) else 0
    print_user(f"Обработка чанка {chunk_index} ({paragraph_count} абзацев)...", "PROCESS")
    
    # 1. Объединение текста. Инварианты проверяются повторно непосредственно
    # перед API: даже ошибочный внешний caller не сможет вызвать тихое обрезание.
    try:
        full_text = _chunk_text(paragraphs)
    except (TypeError, ValueError) as exc:
        print_error_user_friendly(f"Ошибка структуры чанка {chunk_index}")
        print_debug(f"Детали ошибки: {exc}")
        return False
    
    # Проверяем, что текст не пустой
    if not full_text or not full_text.strip():
        print_debug(f"[Чанк {chunk_index}] Текст пуст, пропускаем")
        return False
    
    if len(full_text) > MAX_CHUNK_SIZE:
        print_error_user_friendly(
            f"Чанк {chunk_index} превышает безопасный лимит EdgeTTS: "
            f"{len(full_text)} > {MAX_CHUNK_SIZE}"
        )
        print_debug(f"[Чанк {chunk_index}] Отказ без обрезания текста")
        return False

    try:
        os.remove(staging_mp3_path)
    except FileNotFoundError:
        pass
    
    temp_text_file = ""
    try:
        # 2. Используем временный файл для передачи текста
        # Важно: используем UTF-8 без BOM (edge-tts лучше работает с UTF-8 без BOM)
        # Создаем файл в системной временной директории для избежания проблем с путями
        temp_dir = tempfile.gettempdir()
        # Используем простые имена файлов без специальных символов
        temp_text_file = os.path.join(temp_dir, f"edgetts_{chunk_index}_{os.getpid()}.txt")
        
        # Убеждаемся, что путь не содержит проблемных символов
        temp_text_file = os.path.normpath(temp_text_file)
        
        with open(temp_text_file, 'w', encoding='utf-8', errors='replace') as tmp_file:
            tmp_file.write(full_text)
        
        # Проверяем, что файл создан
        if not os.path.exists(temp_text_file):
            raise Exception(f"Не удалось создать временный файл: {temp_text_file}")

        # 3. Формирование команды
        # Используем абсолютные пути для избежания проблем с кодировкой
        abs_temp_file = os.path.abspath(temp_text_file)
        abs_output_file = os.path.abspath(staging_mp3_path)
        
        # Нормализуем пути (убираем двойные слеши и т.д.)
        abs_temp_file = os.path.normpath(abs_temp_file)
        abs_output_file = os.path.normpath(abs_output_file)
        
        command = [
            "edge-tts", 
            "-v", voice_name, 
            "--file", abs_temp_file, 
            "--write-media", abs_output_file,
            "--rate=-20%" # Из вашего оригинального файла
        ]
        
        print_debug(f"   Текст: {len(full_text)} символов")
        print_debug(f"   Голос: {voice_name}")
        print_debug(f"   Временный файл: {temp_text_file}")
        print_debug(f"   Выходной файл: {output_mp3_path}")
        
        # 4. Выполнение - пробуем Python API, если CLI не работает
        use_python_api = USE_PYTHON_API
        if use_python_api:
            # Используем Python API edge-tts (более надежно)
            try:
                import edge_tts
                import asyncio
                
                print_debug(f"   Используем Python API edge-tts")
                
                async def generate_audio():
                    communicate = None
                    try:
                        communicate = edge_tts.Communicate(text=full_text, voice=voice_name, rate="-20%")
                        await communicate.save(abs_output_file)
                        _apply_audio_speed_or_raise(abs_output_file, TTS_SPEED)
                    finally:
                        # Явно закрываем communicate для освобождения ресурсов
                        if communicate is not None:
                            try:
                                # Пытаемся закрыть внутренние транспорты
                                if hasattr(communicate, 'close'):
                                    await communicate.close()
                            except Exception:
                                pass
                
                # Запускаем асинхронную функцию с правильной очисткой
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(generate_audio())
                        # Даем время на закрытие всех транспортов
                        loop.run_until_complete(asyncio.sleep(0.1))
                    finally:
                        # Правильно закрываем все pending tasks
                        try:
                            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                            for task in pending:
                                task.cancel()
                            if pending:
                                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                        except Exception:
                            pass  # Игнорируем ошибки при закрытии задач
                        
                        # Закрываем все транспорты перед закрытием loop
                        try:
                            # Даем время на закрытие транспортов
                            loop.run_until_complete(asyncio.sleep(0.05))
                        except Exception:
                            pass
                        
                        # Закрываем loop
                        try:
                            loop.close()
                        except Exception:
                            pass  # Игнорируем ошибки при закрытии loop
                except Exception as e:
                    # Если что-то пошло не так - пробуем простой вариант
                    print_debug(f"Ошибка при использовании нового loop: {e}, пробую asyncio.run()")
                    try:
                        asyncio.run(generate_audio())
                    except Exception as e2:
                        print_debug(f"Ошибка при использовании asyncio.run(): {e2}")
                        raise
                
                # Проверяем, что файл создан
                if os.path.exists(staging_mp3_path) and os.path.getsize(staging_mp3_path) > 0:
                    os.replace(staging_mp3_path, output_mp3_path)
                    print_debug(f"[Чанк {chunk_index}] Успешно сохранен через Python API")
                    print_user(f"Чанк {chunk_index} готов", "SUCCESS")
                else:
                    raise Exception(f"Файл {staging_mp3_path} не создан или пуст")
                    
            except Exception as e:
                print(f"   ⚠️ Ошибка при использовании Python API: {e}", flush=True)
                print(f"   🔄 Пробуем через CLI...", flush=True)
                # Пробуем CLI как запасной вариант
                try:
                    os.remove(staging_mp3_path)
                except FileNotFoundError:
                    pass
                use_python_api = False
        
        if not use_python_api:
            # Используем CLI edge-tts
            try:
                print_debug(f"   Запуск команды: {' '.join(command)}")
                
                result = subprocess.run(
                    command, 
                    check=True, 
                    stdout=subprocess.PIPE,  # Захватываем stdout для отладки
                    stderr=subprocess.PIPE, 
                    text=False,  # Используем bytes для stderr/stdout
                    timeout=300,  # Таймаут 5 минут на чанк
                    shell=False,  # НЕ используем shell для безопасности
                    **_SUBPROCESS_HIDE_WINDOW
                )

                # CLI и Python API должны соблюдать один и тот же --speed,
                # иначе manifest мог бы объявить кэш для скорости, которой нет.
                _apply_audio_speed_or_raise(abs_output_file, TTS_SPEED)
                
                # Выводим stdout для отладки (если есть)
                if result.stdout:
                    try:
                        stdout_text = result.stdout.decode('utf-8', errors='replace')
                        if stdout_text.strip():
                            print_debug(f"   Вывод edge-tts: {stdout_text[:200]}")
                    except Exception:
                        pass
                
                # Проверяем, что файл действительно создан и не пуст
                if os.path.exists(staging_mp3_path) and os.path.getsize(staging_mp3_path) > 0:
                    os.replace(staging_mp3_path, output_mp3_path)
                    print_debug(f"[Чанк {chunk_index}] Успешно сохранен")
                    print_user(f"Чанк {chunk_index} готов", "SUCCESS")
                else:
                    raise Exception(f"Файл {staging_mp3_path} не создан или пуст")
                    
            except subprocess.TimeoutExpired:
                raise Exception(f"Превышено время ожидания (5 минут) для чанка {chunk_index}")
            except subprocess.CalledProcessError as e:
                # Декодируем stderr с обработкой ошибок кодировки
                stderr_text = ""
                if e.stderr:
                    stderr_text = e.stderr.decode('utf-8', errors='replace')
                
                error_msg = f"edge-tts вернул код {e.returncode}"
                if stderr_text:
                    error_msg += f": {stderr_text[:500]}"  # Ограничиваем длину сообщения
                print_debug(f"Ошибка edge-tts: {error_msg}")
                raise Exception(error_msg)

        return True

    except Exception as e:
        print_error_user_friendly(f"Ошибка при обработке чанка {chunk_index}")
        print_debug(f"Детали ошибки: {e}")
        import traceback
        print_debug(traceback.format_exc())
        return False
    finally:
        # 5. Очистка
        if os.path.exists(temp_text_file):
            os.remove(temp_text_file)
        if os.path.exists(staging_mp3_path):
            os.remove(staging_mp3_path)


def main():
    parser = argparse.ArgumentParser(description="Worker для озвучки текста через EdgeTTS (режим больших чанков).")
    parser.add_argument("--project_dir", required=True) 
    parser.add_argument("--voice_name", required=True)
    parser.add_argument("--paragraphs_file", required=True)  # ИСПРАВЛЕНО: был paragraphs_json
    parser.add_argument("--use_cache", action="store_true", help="Использовать существующие файлы.")
    parser.add_argument("--speed", default=os.environ.get("TTS_SPEED", "1.0"), help="Темп речи (atempo): 1.0=норма, <1 медленнее, >1 быстрее")

    args = parser.parse_args()
    global TTS_SPEED
    try:
        TTS_SPEED = float(args.speed)
    except (TypeError, ValueError):
        TTS_SPEED = 1.0
    
    output_dir = os.path.join(args.project_dir, AUDIO_DIR_NAME)
    os.makedirs(output_dir, exist_ok=True)

    # Commit-marker всего запуска. Пока manifest не записан и повторно не
    # проверен, любой выход (False чанка, исключение потока, Ctrl+C, ошибка
    # completeness/ffmpeg) обязан убрать ВСЕ worker-owned результаты. Иначе
    # render_worker рекурсивно подхватит несколько уже готовых numeric MP3 как
    # якобы полную озвучку.
    committed = False
    try:
        # Читаем из файла вместо парсинга длинного JSON через аргументы.
        with open(args.paragraphs_file, 'r', encoding='utf-8', errors='replace') as f:
            all_paragraphs = json.load(f)

        # 1. Без потерь строим точные чанки. В частности, один абзац длиннее
        # MAX_CHUNK_SIZE тоже обязательно дробится, а не уезжает целиком в API.
        canonical_text, chunks = _build_chunks(all_paragraphs)

        if not chunks:
            # Пустой новый источник не должен оставлять numeric MP3 старого текста.
            _clear_generated_audio(output_dir)
            committed = True
            print(_('tts_no_new_paragraphs'), flush=True)
            return

        expected_manifest = _cache_manifest(
            canonical_text, chunks, args.voice_name, TTS_SPEED
        )

        # --- ЛОГИКА КЭША ---
        if args.use_cache and _cache_is_valid(output_dir, expected_manifest):
            committed = True
            print_user(
                f"Найден точный кэш EdgeTTS: {len(chunks)} mp3-файлов. Пропускаем.",
                "INFO",
            )
            return

        # Кэш отсутствует или не совпал по тексту/голосу/скорости/набору файлов.
        # Удаляем только наши numeric MP3, чтобы хвост старого запуска не попал в рендер.
        _clear_generated_audio(output_dir)
        if not check_dependencies():
            raise RuntimeError("EdgeTTS недоступен")

        print_user(f"Разбито на {len(chunks)} чанков для обработки", "INFO")
        print_debug(f"Абзацев: {len(all_paragraphs)} → Чанков: {len(chunks)}")

        # 2. Подготовка задач для ThreadPool
        tasks = [
            (i + 1, chunk, args.voice_name, output_dir)
            for i, chunk in enumerate(chunks)
        ]

        # 3. Параллельная обработка чанков
        print_user(f"Запуск обработки в {PARALLEL_REQUESTS} потоков...", "PROCESS")
        print_debug(f"Задач: {len(tasks)}")

        completed = 0
        failures = 0
        with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as executor:
            for result in executor.map(process_chunk, tasks):
                completed += 1
                if not result:
                    failures += 1
                if completed % 5 == 0:
                    print_progress(completed, len(tasks), "чанков")

        if failures:
            raise RuntimeError(
                f"{failures} из {len(tasks)} чанков не обработано"
            )
        if not _audio_outputs_complete(output_dir, expected_manifest):
            raise RuntimeError("получен неполный набор MP3")

        _write_cache_manifest(output_dir, expected_manifest)
        if not _cache_is_valid(output_dir, expected_manifest):
            raise RuntimeError("итоговый набор MP3 не прошёл проверку manifest")

        committed = True
        print_user("Озвучивание EdgeTTS завершено", "SUCCESS")
    except Exception as exc:
        print_error_user_friendly(f"Озвучивание EdgeTTS не завершено: {exc}")
        print_debug(f"Детали: {exc}")
        import traceback
        print_debug(traceback.format_exc())
        raise SystemExit(1)
    finally:
        if not committed:
            _clear_generated_audio(output_dir)

if __name__ == "__main__":
    main()
