import os
import argparse
import sys
import shutil
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from icrawler.builtin import BingImageCrawler
from icrawler import ImageDownloader
from urllib.parse import urlparse
from PIL import Image, UnidentifiedImageError
import random
import string
import threading

# Импорт локализации
try:
    from i18n import _
except ImportError:
    # Если модуль не найден, используем функцию-заглушку
    def _(key, *args, **kwargs):
        return key

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

# Имя главной папки для изображений (СТАРЫЙ режим «теги» → Images/{i}/*.jpg)
IMAGE_DIR_NAME = "Images"

# --- НОВЫЙ режим «по плану сцен» (2026-07-30) --------------------------------
# Папка и имена файлов ТЕ ЖЕ, что у генерации (googler_worker: googler/NNN.jpg,
# нумерация с 1). Благодаря этому галерея, удаление, перегенерация, видео из
# картинки сцены, синхрон длительностей и отложенные цепочки работают без правок.
SCENE_DIR_NAME = "googler"
SCENES_FILE_NAME = "scenes.json"
SCENE_QUERY_MAX_WORDS = 8
SCENE_QUERY_MAX_CHARS = 120
SCENE_CANDIDATES_PER_IMAGE = 8      # запас на фильтрацию стоков/битых файлов
SCENE_MIN_FILE_BYTES = 12000
SCENE_MIN_SIDE_PX = 320
_SCENE_TMP_DIR = ".bing_tmp"        # с точки: не попадает в googler/*.jpg-подсчёты

ASPECT_RATIO_CHOICES = ("landscape", "portrait", "square")
BING_LAYOUT_BY_ASPECT_RATIO = {
    "landscape": "wide",
    "portrait": "tall",
    "square": "square",
}
# Bing layout-фильтр — только предварительный отбор. После скачивания квадрат
# проверяем сами: разница сторон не больше 10% от большей стороны.
SQUARE_ASPECT_TOLERANCE = 0.10

# Стоп-слова стиля/камеры: в поиск Bing они тянут рендеры и артстейшн вместо
# самого предмета сцены. Список фиксированный — никаких LLM в воркере.
_STYLE_STOPWORDS = {
    "cinematic", "cinematically", "photorealistic", "photoreal", "realistic", "hyperrealistic",
    "hyper", "ultra", "ultrarealistic", "detailed", "highly", "intricate", "masterpiece",
    "award", "winning", "trending", "artstation", "octane", "unreal", "engine", "render",
    "rendering", "render3d", "3d", "2d", "cgi", "illustration", "illustrated", "artwork",
    "art", "digital", "painting", "drawing", "sketch", "stickman", "minimalistic", "minimal",
    "style", "styled", "aesthetic", "aesthetics", "mood", "moody", "atmosphere", "atmospheric",
    "dramatic", "epic", "beautiful", "stunning", "gorgeous", "vibrant", "muted", "saturated",
    "lighting", "light", "lights", "lit", "backlit", "rim", "volumetric", "godrays", "glow",
    "shadow", "shadows", "contrast", "colour", "color", "colors", "colours", "palette",
    "shot", "shots", "closeup", "close", "wide", "medium", "extreme", "angle", "angles",
    "camera", "lens", "lenses", "mm", "bokeh", "depth", "field", "focus", "sharp", "blur",
    "blurred", "composition", "framing", "frame", "foreground", "background", "backdrop",
    "aspect", "ratio", "landscape", "portrait", "square", "resolution", "4k", "8k", "hd",
    "uhd", "quality", "professional", "photography", "photograph", "photo", "image",
    "picture", "scene", "view", "viewpoint", "perspective", "texture", "grain", "film",
    "filmic", "still", "frames", "seed", "prompt", "negative", "no", "text", "watermark",
    "logo", "signature",
}
# Служебные слова языка: сами по себе не сужают поиск.
_GENERIC_STOPWORDS = {
    "the", "and", "with", "without", "from", "into", "onto", "over", "under", "near",
    "for", "his", "her", "hers", "its", "their", "theirs", "our", "your", "that", "this",
    "these", "those", "there", "here", "while", "during", "after", "before", "very",
    "some", "any", "all", "both", "each", "other", "another", "such", "than", "then",
    "when", "where", "which", "who", "whom", "whose", "what", "how", "are", "was", "were",
    "been", "being", "has", "have", "had", "can", "could", "would", "should", "will",
    "shall", "may", "might", "must", "does", "did", "not", "but", "out", "off", "about",
    "around", "across", "through", "between", "against", "toward", "towards", "upon",
}

# Список стоковых сайтов для блокировки
STOCK_SITES = [
    'alamy.com', 'shutterstock.com', 'gettyimages.com', 'gettyimages.in',
    'dreamstime.com', 'istockphoto.com', 'depositphotos.com', 'adobe.com',
    '123rf.com', 'stock.adobe.com', 'pngtree.com', 'vecteezy.com',
    'freepik.com', 'lovepik.com', 'dreamypixel.com', 'canstockphoto.com'
]

# Счетчики
blocked_stock_count = 0


def _normalize_aspect_ratio(aspect_ratio=""):
    """Каноническое значение aspect ratio; пусто сохраняет старое поведение."""
    value = str(aspect_ratio or "").strip().lower()
    if value and value not in ASPECT_RATIO_CHOICES:
        raise ValueError(
            f"aspect_ratio должен быть одним из: {', '.join(ASPECT_RATIO_CHOICES)}"
        )
    return value


def _bing_filters(aspect_ratio=""):
    """Фильтры Bing: layout добавляется только при явном aspect_ratio."""
    value = _normalize_aspect_ratio(aspect_ratio)
    filters = {"size": "large"}
    if value:
        filters["layout"] = BING_LAYOUT_BY_ASPECT_RATIO[value]
    return filters


def _aspect_ratio_matches(width, height, aspect_ratio=""):
    """Проверка геометрии кандидата после скачивания.

    landscape: width > height; portrait: height > width; square: стороны
    отличаются не больше чем на SQUARE_ASPECT_TOLERANCE от большей стороны.
    Пустое значение означает legacy-режим без проверки ориентации.
    """
    value = _normalize_aspect_ratio(aspect_ratio)
    if not value:
        return True
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        return False
    if value == "landscape":
        return width > height
    if value == "portrait":
        return height > width
    return abs(width - height) / max(width, height) <= SQUARE_ASPECT_TOLERANCE


def generate_random_name(length):
    """Генерирует случайное имя файла"""
    letters_and_digits = string.ascii_letters + string.digits
    return ''.join(random.choice(letters_and_digits) for _ in range(length))


class JpgDownloaderBing(ImageDownloader):
    """
    Кастомный загрузчик который:
    1. Блокирует стоки
    2. Сохраняет как JPG
    3. Использует случайные имена файлов (из Parser.py)
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocked_count = 0
        self.downloaded_count = 0
        self._convert_lock = threading.Lock()
    
    def get_filename(self, task, default_ext):
        """Генерирует случайное имя файла (из Parser.py)"""
        filename = super().get_filename(task, default_ext)
        base, extension = os.path.splitext(filename)
        random_name = generate_random_name(8)
        return random_name + extension
    
    def download(self, task, default_ext, *args, **kwargs):
        """Скачиваем с проверкой на стоки"""
        global blocked_stock_count
        
        # Получаем URL
        file_url = task.get('file_url', '')
        
        # Проверяем на сток
        if file_url:
            try:
                domain = urlparse(file_url).netloc.lower()
                domain = domain.replace('www.', '')
                
                # Если это сток - НЕ качаем
                if any(stock in domain for stock in STOCK_SITES):
                    print_debug(f"Сток заблокирован: {domain}")
                    self.blocked_count += 1
                    blocked_stock_count += 1
                    return  # ВЫХОДИМ, НЕ КАЧАЕМ
            except Exception:
                pass
        
        try:
            existing_before = set(os.listdir(self.storage.root_dir))
            super().download(task, default_ext, *args, **kwargs)
            existing_after = set(os.listdir(self.storage.root_dir))
            new_files = existing_after - existing_before
            target_file = new_files.pop() if len(new_files) == 1 else None
            self._convert_downloaded_file(task, target_file=target_file)
        except Exception as e:
            pass
    
    def _convert_downloaded_file(self, task, target_file=None):
        try:
            if target_file:
                latest_file = target_file
            else:
                with self._convert_lock:
                    all_files = os.listdir(self.storage.root_dir)
                    image_files = [f for f in all_files if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'))]
                    if not image_files:
                        return
                    image_files.sort()
                    latest_file = image_files[-1]
            filepath = os.path.join(self.storage.root_dir, latest_file)
            
            # Если это уже JPG - всё ОК
            if latest_file.lower().endswith('.jpg'):
                self.downloaded_count += 1
                print_debug(f"Скачано: {latest_file}")
                return
            
            # Конвертируем в JPG
            try:
                with Image.open(filepath) as img:
                    # Конвертируем в RGB
                    if img.mode in ('RGBA', 'P', 'LA'):
                        bg = Image.new('RGB', img.size, (255, 255, 255))
                        if img.mode == 'P':
                            img = img.convert('RGBA')
                        if img.mode in ('RGBA', 'LA'):
                            bg.paste(img, mask=img.split()[-1])
                        img = bg
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')
                    
                    # Новое имя с .jpg
                    base_name = os.path.splitext(latest_file)[0]
                    jpg_path = os.path.join(self.storage.root_dir, f"{base_name}.jpg")
                    
                    # Сохраняем
                    img.save(jpg_path, 'JPEG', quality=92, optimize=True)
                    
                    # Удаляем оригинал
                    if filepath != jpg_path:
                        os.remove(filepath)
                    
                    self.downloaded_count += 1
                    print_debug(f"Конвертировано: {base_name}.jpg")
            
            except Exception as e:
                print_debug(f"Ошибка конвертации: {e}")
                import traceback
                print_debug(traceback.format_exc())
                # Удаляем битый файл
                try:
                    os.remove(filepath)
                except Exception:
                    pass
        
        except Exception as e:
            pass


def download_images_for_tag(tag, index, num_images_to_get, base_img_dir, use_cache,
                            aspect_ratio=""):
    """
    Скачивает изображения для одного тега в отдельную папку.
    Использует Bing Image Crawler (из Parser.py)
    """
    
    aspect_ratio = _normalize_aspect_ratio(aspect_ratio)

    # Создаем папку для этого тега
    img_folder = os.path.join(base_img_dir, str(index))
    os.makedirs(img_folder, exist_ok=True)

    print_user(f"Скачивание изображений для тега '{tag}'...", "PROCESS")
    print_debug(f"[{index}] Тег: '{tag}' → папка '{img_folder}'")

    # Проверка кэша
    if use_cache:
        try:
            existing_files = [f for f in os.listdir(img_folder) if f.endswith('.jpg')]
            if aspect_ratio:
                existing_files = [
                    filename for filename in existing_files
                    if _image_candidate_ok(
                        os.path.join(img_folder, filename),
                        aspect_ratio=aspect_ratio,
                    )
                ]
            if len(existing_files) >= num_images_to_get:
                print_user(f"Найдено {len(existing_files)} изображений в кэше. Пропускаем.", "INFO")
                return
            else:
                print_debug(f"[Кэш] Найдено {len(existing_files)}/{num_images_to_get} изображений. Докачиваем...")
        except FileNotFoundError:
            pass

    # Ищем в 3 раза больше изображений, чтобы был запас на фильтрацию
    num_to_search = num_images_to_get * 3
    print_debug(f"Поиск {num_to_search} изображений (цель: {num_images_to_get} после фильтрации)")
    print_debug("АКТИВНА БЛОКИРОВКА СТОКОВЫХ САЙТОВ!")

    try:
        # Используем Bing вместо Google (из Parser.py)
        # Без --aspect_ratio фильтры остаются ровно прежними. Явный параметр
        # добавляет нативный Bing layout и включает локальную проверку геометрии.
        filters = _bing_filters(aspect_ratio)
        
        crawler = BingImageCrawler(
            downloader_cls=JpgDownloaderBing,
            downloader_threads=4,  # Из Parser.py
            storage={'root_dir': img_folder}
        )

        crawler.crawl(
            keyword=tag.strip(),
            filters=filters,
            offset=0,
            max_num=num_to_search
        )
        
        # Проверяем что скачалось
        downloaded_files = sorted([f for f in os.listdir(img_folder) if f.endswith('.jpg')])

        if aspect_ratio:
            matching_files = []
            for filename in downloaded_files:
                candidate = os.path.join(img_folder, filename)
                if _image_candidate_ok(candidate, aspect_ratio=aspect_ratio):
                    matching_files.append(filename)
                    continue
                try:
                    os.remove(candidate)
                except OSError:
                    pass
            downloaded_files = matching_files
        
        blocked_count = getattr(crawler.downloader, 'blocked_count', 0)
        print_debug(f"Заблокировано стоков: {blocked_count}")
        print_debug(f"Скачано изображений: {len(downloaded_files)}")
        
        # Удаляем лишние
        if len(downloaded_files) > num_images_to_get:
            print_debug(f"Очистка: скачано {len(downloaded_files)}, оставляем {num_images_to_get}")
            for f_to_del in downloaded_files[num_images_to_get:]:
                try:
                    os.remove(os.path.join(img_folder, f_to_del))
                except OSError:
                    pass
            downloaded_files = downloaded_files[:num_images_to_get]
        
        print_user(f"Завершено скачивание для тега '{tag}': {len(downloaded_files)} изображений", "SUCCESS")
        print_debug(f"   Итого файлов в папке: {len(downloaded_files)}")
        
        if len(downloaded_files) == 0:
            print_error_user_friendly(f"Не удалось скачать ни одного изображения для тега '{tag}'")

    except Exception as e:
        print_error_user_friendly(f"Ошибка при скачивании для тега '{tag}'")
        print_debug(f"Детали: {e}")
        import traceback
        print_debug(traceback.format_exc())


# ─────────────────────────────────────────────────────────────────────────────
# НОВЫЙ РЕЖИМ: одна найденная картинка на КАЖДУЮ сцену плана (googler/NNN.jpg).
# Запрос строится из scenes.json ДЕТЕРМИНИРОВАННО — ни одного сетевого LLM-вызова
# (задача генерации идёт с VS_DISABLE_PROMPT_LLM=1 и без LLM-ключа).
# ─────────────────────────────────────────────────────────────────────────────

def scene_output_path(project_dir, scene_number):
    """Путь картинки сцены — ровно как у генерации: <project>/googler/NNN.jpg."""
    return os.path.join(project_dir, SCENE_DIR_NAME, f"{int(scene_number):03d}.jpg")


def load_scene_plan(project_dir, scenes_file=""):
    """План сцен от задачи «промпты» → список сцен. Нет/битый → []."""
    path = scenes_file or os.path.join(project_dir, SCENE_DIR_NAME, SCENES_FILE_NAME)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            scenes = json.load(f)
    except Exception as e:
        print_debug(f"{SCENES_FILE_NAME} не прочитан ({path}): {e}")
        return []
    if not isinstance(scenes, list):
        print_debug(f"{SCENES_FILE_NAME} не список — план не годится")
        return []
    return scenes


def build_scene_query(scene, fallback_index=None):
    """Сцена плана → поисковый запрос Bing. Детерминированно, без LLM.

    Берём visual_prompt (иначе visual_description/text_segment), выкидываем
    стилевые и служебные слова, оставляем первые значимые слова и режем длину.
    """
    if isinstance(scene, str):
        raw = scene
    else:
        s = scene if isinstance(scene, dict) else {}
        raw = (s.get("visual_prompt") or s.get("visual_description")
               or s.get("text_segment") or "")
    words = re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", str(raw), flags=re.UNICODE)
    picked, seen = [], set()
    for w in words:
        low = w.lower()
        if low in _STYLE_STOPWORDS or low in _GENERIC_STOPWORDS:
            continue
        if len(low) < 3 and not any(ch.isdigit() for ch in low):
            continue
        if low in seen:
            continue
        seen.add(low)
        picked.append(w)
        if len(picked) >= SCENE_QUERY_MAX_WORDS:
            break
    if not picked:
        picked = words[:SCENE_QUERY_MAX_WORDS]   # промпт целиком из стоп-слов
    query = " ".join(picked)[:SCENE_QUERY_MAX_CHARS].strip()
    if not query:
        print_debug(f"[{fallback_index}] пустой промпт сцены — запрос не построен")
    return query


def _file_digest(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


def _image_candidate_ok(path, aspect_ratio=""):
    """Файл годен как картинка сцены и соответствует заданной ориентации."""
    aspect_ratio = _normalize_aspect_ratio(aspect_ratio)
    try:
        if os.path.getsize(path) < SCENE_MIN_FILE_BYTES:
            return False
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            w, h = img.size
        return (min(w, h) >= SCENE_MIN_SIDE_PX
                and _aspect_ratio_matches(w, h, aspect_ratio))
    except (UnidentifiedImageError, OSError, ValueError):
        return False
    except Exception:
        return False


def _pick_downloaded_image(folder, skip_digest="", aspect_ratio=""):
    """Первый годный скачанный файл (детерминированно по имени). None — нечего брать."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in names:
        if not name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')):
            continue
        path = os.path.join(folder, name)
        if (not os.path.isfile(path)
                or not _image_candidate_ok(path, aspect_ratio=aspect_ratio)):
            continue
        if skip_digest and _file_digest(path) == skip_digest:
            continue        # ровно та же картинка, что уже лежит (реген = НОВАЯ)
        return path
    return None


def _crawl_bing(keyword, dest_dir, max_num, offset=0, downloader_threads=2,
                aspect_ratio=""):
    """Один поиск Bing в свою папку (блокировка стоков — в JpgDownloaderBing)."""
    crawler = BingImageCrawler(
        downloader_cls=JpgDownloaderBing,
        downloader_threads=max(1, int(downloader_threads)),
        storage={'root_dir': dest_dir},
    )
    crawler.crawl(keyword=str(keyword).strip(), filters=_bing_filters(aspect_ratio),
                  offset=int(offset), max_num=max(1, int(max_num)))
    return crawler


def search_image_for_scene(query, output_path, tmp_dir,
                           max_num=SCENE_CANDIDATES_PER_IMAGE, offset=0,
                           skip_digest="", aspect_ratio=""):
    """Найти ОДНУ картинку по запросу и положить её в output_path. → bool.

    Пишем через .part + os.replace: читатель (галерея/рендер) никогда не видит
    недокачанный файл, а при неудаче прежний файл остаётся нетронутым.
    """
    if not str(query or "").strip():
        return False
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        crawl_kwargs = {"offset": offset}
        if aspect_ratio:
            crawl_kwargs["aspect_ratio"] = aspect_ratio
        _crawl_bing(query, tmp_dir, max_num, **crawl_kwargs)
        pick = _pick_downloaded_image(
            tmp_dir, skip_digest=skip_digest, aspect_ratio=aspect_ratio)
        if not pick:
            return False
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        part = output_path + ".part"
        os.replace(pick, part)
        os.replace(part, output_path)
        return True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def download_images_for_scenes(project_dir, scenes, threads=1, use_cache=True,
                               aspect_ratio=""):
    """Одна картинка на каждую сцену плана → googler/NNN.jpg. → (найдено, всего).

    Одна сцена без результата НЕ валит прогон (continue-on-error): пропуски
    добираются повторным запуском с кэшем, как у генерации.
    """
    aspect_ratio = _normalize_aspect_ratio(aspect_ratio)
    total = len(scenes)
    out_dir = os.path.join(project_dir, SCENE_DIR_NAME)
    os.makedirs(out_dir, exist_ok=True)
    tmp_root = os.path.join(out_dir, _SCENE_TMP_DIR)
    print_user(f"🔍 ШАГ 1: Поиск картинок по плану сцен ({total} сцен)...", "INFO")
    print_progress(0, total, "изображений", show_bar=True)

    jobs = []
    completed = 0
    for i, scene in enumerate(scenes, 1):
        out = scene_output_path(project_dir, i)
        cached_ok = os.path.exists(out)
        if cached_ok and aspect_ratio:
            cached_ok = _image_candidate_ok(out, aspect_ratio=aspect_ratio)
        if use_cache and cached_ok:
            print_debug(f"[{i}] Пропущено (уже существует): {os.path.basename(out)}")
            completed += 1
            continue
        jobs.append((i, scene, out))

    lock = threading.Lock()

    def _work(item):
        idx, scene, out = item
        query = build_scene_query(scene, idx)
        if not query:
            print_user(f"⚠️ [{idx}] Промпт сцены пуст — картинку не ищу", "WARNING")
            return False
        try:
            search_kwargs = {}
            if aspect_ratio:
                search_kwargs["aspect_ratio"] = aspect_ratio
            ok = search_image_for_scene(
                query, out, os.path.join(tmp_root, f"{idx:03d}"), **search_kwargs)
        except Exception as e:
            print_debug(f"[{idx}] поиск упал: {e}")
            ok = False
        if ok:
            print_debug(f"[{idx}] «{query}» → {os.path.basename(out)}")
        else:
            print_user(f"⚠️ [{idx}] Ничего подходящего по запросу «{query}» "
                       f"— сцена осталась без картинки", "WARNING")
        return ok

    if jobs:
        print_debug(f"Параллельных потоков поиска: {max(1, int(threads))}")
        with ThreadPoolExecutor(max_workers=max(1, int(threads))) as ex:
            futures = [ex.submit(_work, item) for item in jobs]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    print_debug(f"поток поиска упал: {e}")
                with lock:
                    completed += 1
                    print_progress(completed, total, "изображений", show_bar=True)

    shutil.rmtree(tmp_root, ignore_errors=True)
    def _scene_ready(scene_number):
        path = scene_output_path(project_dir, scene_number)
        if not os.path.exists(path):
            return False
        return (not aspect_ratio
                or _image_candidate_ok(path, aspect_ratio=aspect_ratio))

    have = len([i for i in range(1, total + 1) if _scene_ready(i)])
    print_progress(have, total, "изображений", show_bar=True)
    if have < total:
        gaps = [i for i in range(1, total + 1) if not _scene_ready(i)]
        prev = ", ".join(str(g) for g in gaps[:20]) + ("…" if len(gaps) > 20 else "")
        print_user(f"⚠️ Не хватает {total - have} кадров из {total}: сцены {prev}", "WARNING")
    return have, total


def run_scene_mode(args):
    """Bing по плану сцен: googler/scenes.json → googler/NNN.jpg. → bool (успех)."""
    scenes = load_scene_plan(args.project_dir, getattr(args, 'scenes_file', '') or "")
    if not scenes:
        print_error_user_friendly(
            "Нет плана сцен (googler/scenes.json) — сначала должна отработать задача «промпты»")
        return False
    have, total = download_images_for_scenes(
        args.project_dir, scenes,
        threads=getattr(args, 'image_threads', 1) or 1,
        use_cache=bool(getattr(args, 'use_cache', False)),
        aspect_ratio=getattr(args, 'aspect_ratio', '') or "",
    )
    print_debug(f"📊 Всего заблокировано стоковых сайтов: {blocked_stock_count}")
    if have == 0:
        print_error_user_friendly("Не удалось найти ни одной картинки по плану сцен")
        return False
    print_user(f"✅ Поиск картинок завершён: {have} из {total}", "SUCCESS")
    return True


def regen_scene_image(args):
    """Перегенерация ОДНОЙ картинки Bing-проекта = НОВЫЙ поиск по той же сцене.

    AI-генератор не зовём вообще. Старый файл заменяется только при успехе.
    """
    image_file = args.regen_image
    filename = os.path.basename(image_file)
    output_path = (image_file if os.path.isabs(image_file)
                   else os.path.join(args.project_dir, SCENE_DIR_NAME, filename))
    stem = os.path.splitext(filename)[0]
    # свой текст из модалки регена — приоритетнее промпта сцены и тегов
    query = str(getattr(args, 'query', '') or "").strip()
    if not query and stem.isdigit():
        scenes = load_scene_plan(args.project_dir, getattr(args, 'scenes_file', '') or "")
        idx = int(stem)
        if 1 <= idx <= len(scenes):
            query = build_scene_query(scenes[idx - 1], idx)
    if not query:
        # старый режим «теги» (Images/{i}/*.jpg): номера сцены нет — ищем по тегу
        tags = [t.strip() for t in str(getattr(args, 'tags', '') or "").split(',') if t.strip()]
        query = tags[0] if tags else ""
    if not query:
        print_error_user_friendly(
            f"Не из чего собрать поисковый запрос для {filename} "
            f"(нет плана сцен и не заданы теги)")
        return False
    print_user(f"🔄 Новый поиск картинки для {filename}: «{query}»", "PROCESS")
    skip = _file_digest(output_path) if os.path.exists(output_path) else ""
    tmp_base = os.path.join(os.path.dirname(os.path.abspath(output_path)), _SCENE_TMP_DIR)
    ok = False
    # Сначала смотрим ДАЛЬШЕ первой страницы выдачи: реген должен дать другую
    # картинку, а не ту же самую. Пусто → обычная первая страница.
    for offset in (SCENE_CANDIDATES_PER_IMAGE, 0):
        try:
            search_kwargs = {}
            aspect_ratio = getattr(args, 'aspect_ratio', '') or ""
            if aspect_ratio:
                search_kwargs["aspect_ratio"] = aspect_ratio
            ok = search_image_for_scene(
                query, output_path, os.path.join(tmp_base, f"regen_{stem}_{offset}"),
                max_num=SCENE_CANDIDATES_PER_IMAGE, offset=offset,
                skip_digest=skip, **search_kwargs)
        except Exception as e:
            print_debug(f"реген-поиск (offset={offset}) упал: {e}")
            ok = False
        if ok:
            break
    shutil.rmtree(tmp_base, ignore_errors=True)
    if ok:
        print_user(f"✅ Перегенерировано (новый поиск): {filename}", "SUCCESS")
        return True
    print_error_user_friendly(
        f"Не удалось найти новую картинку для {filename} — старый файл не тронут")
    return False


def build_arg_parser():
    """CLI-контракт воркера (вынесен отдельно для безопасной проверки)."""
    parser = argparse.ArgumentParser(description="Image Downloader Worker (Bing)")
    parser.add_argument("--tags", default="", help="Теги для поиска, разделенные ЗАПЯТОЙ (пусто = режим плана сцен)")
    parser.add_argument("--num_images", type=int, default=5, help="Количество изображений для скачивания на каждый тег")
    parser.add_argument("--project_dir", required=True, help="Базовая директория проекта")
    parser.add_argument("--use_cache", action="store_true", help="Использовать кэш (не скачивать заново)")
    parser.add_argument("--from_scenes", action="store_true",
                        help="Искать по плану сцен googler/scenes.json — одна картинка на сцену (googler/NNN.jpg)")
    parser.add_argument("--scenes_file", default="", help="Путь к scenes.json (по умолчанию <project_dir>/googler/scenes.json)")
    parser.add_argument("--image_threads", type=int, default=1, help="Потоков поиска в режиме плана сцен")
    parser.add_argument("--regen_image", default="",
                        help="Перегенерация ОДНОЙ картинки: новый поиск по её сцене (путь NNN.jpg)")
    parser.add_argument("--query", default="", help="Готовый поисковый запрос (приоритетнее сцены/тегов)")
    parser.add_argument(
        "--aspect_ratio", choices=ASPECT_RATIO_CHOICES, default="",
        help="Ориентация результата: landscape, portrait или square",
    )
    return parser


def main():
    print_user("Начинаю загрузку изображений...", "PROCESS")
    print_debug("="*80)
    print_debug(_('image_using_bing'))
    print_debug(_('image_stock_protection'))
    print_debug(_('image_blocking'))
    print_debug(_('image_auto_convert'))
    print_debug(_('image_random_names'))
    print_debug("="*80)

    parser = build_arg_parser()

    try:
        args = parser.parse_args()
    except Exception as e:
        print(f"❌ Ошибка парсинга аргументов: {e}")
        sys.exit(1)

    tags_now = [t.strip() for t in str(args.tags or "").split(',') if t.strip()]
    # Реген одной картинки и режим плана сцен — новые пути. Старый режим («теги»)
    # остаётся ровно тем же, что был: пачки картинок по тегу в Images/{i}/.
    if args.regen_image:
        sys.exit(0 if regen_scene_image(args) else 1)
    if args.from_scenes or not tags_now:
        sys.exit(0 if run_scene_mode(args) else 1)

    # Создаем главную папку 'Images'
    base_img_dir = os.path.join(args.project_dir, IMAGE_DIR_NAME)
    os.makedirs(base_img_dir, exist_ok=True)
    print_debug(f"Изображения будут сохранены в: {base_img_dir}\n")

    # Теги разделяются по запятой
    tags = [tag.strip() for tag in args.tags.split(',') if tag.strip()]
    
    num_images_per_tag = args.num_images
    
    if not tags:
        print_error_user_friendly(_('image_tags_required'))
        sys.exit(1)
        
    if num_images_per_tag <= 0:
        print_error_user_friendly(_('image_count_error'))
        sys.exit(1)

    # Запускаем скачивание для каждого тега
    for i, tag in enumerate(tags, 1):
        download_images_for_tag(
            tag=tag,
            index=i,
            num_images_to_get=num_images_per_tag,
            base_img_dir=base_img_dir,
            use_cache=args.use_cache,
            aspect_ratio=args.aspect_ratio,
        )
    
    print_user("Загрузка изображений завершена", "SUCCESS")
    print_debug(f"📊 Всего заблокировано стоковых сайтов: {blocked_stock_count}")
    print_debug("="*80)

if __name__ == "__main__":
    main()
