# -*- coding: utf-8 -*-
"""Генерация картинок через FastGen (api/v6) на СВОЁМ ключе.

Ключ отдельный от кабинета vsgen — своя квота 6000/час и 25 потоков,
поэтому прогон не отбирает ёмкость у основного бизнеса.

Замеры латентности (медианы по 8 повторам, 07.09.2026):
  nano-banana-2       + 8 образцов  54 с    <- ВЫБРАНА 08.09.2026
  nano-banana-2-lite  + 8 образцов  57 с    та же скорость, качество ниже
  nano-banana-2-lite  без образцов  16,1 с
  nano-banana-pro    + 10 образцов  52,9 с
  openai-image       + 10 образцов  45,3 с   но seed НЕ поддерживает
Отсюда правило: число образцов — регулятор скорости, ставим по типу кадра.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import generation_receipts as GR
import fastgen_pool as FP_POOL
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Dict, List, Optional

BASE = "https://googler.fast-gen.ai/api/v6"
# Замер 08.09.2026 на 5 одинаковых кадрах (тот же промпт, эталоны и зерно):
# banana-2 медиана 54 с, lite 57 с — скорость та же, а качество выше:
# у lite вокруг фигур ореол-обводка, лица грубее, анатомия слабее.
IMAGE_MODELS = ("nano-banana-pro", "nano-banana-2", "nano-banana-2-lite", "openai-image")
# MANGA_IMAGE_MODEL, по умолчанию nano-banana-pro. openai-image = GPT Image 2.5 на FastGen: seed не принимает,
# режим MANGA_IMAGE_QUALITY fast (4 единицы квоты) или thinking (6); замер 10.09 — thinking не окупается.
MODEL = os.environ.get("MANGA_IMAGE_MODEL", "").strip() or "nano-banana-pro"
if MODEL not in IMAGE_MODELS:
    raise ValueError("MANGA_IMAGE_MODEL: допустимо " + ", ".join(IMAGE_MODELS))
IMAGE_QUALITY = os.environ.get("MANGA_IMAGE_QUALITY", "").strip() or "fast"
if IMAGE_QUALITY not in ("fast", "thinking"):
    raise ValueError("MANGA_IMAGE_QUALITY: fast или thinking")
# Замер 09.09.2026 на 70 одинаковых кадрах (тот же промпт, те же эталоны, то же зерно):
#   banana-2   единство стиля 0.885, смысловых разрывов 10, ~50 с на кадр
#   banana-pro единство стиля 0.901, смысловых разрывов  3,  73 с на кадр
# Pro точнее следует бланку (одежду и снег из паспорта сцены рисует, banana-2 путал),
# но рисует мягче — стилевую строку приходится держать жёстче.
# 3:4 — вертикальная панель, как у всех трёх топовых рекап-каналов (замер по 90
# кадрам 09.09.2026): панель по центру 16:9, поля — размытая копия. Проверено,
# что API принимает 3:4 и 9:16; 3:4 ближе к пропорции панелей у конкурентов.
ASPECT = "3:4"
POLL_EVERY = 3.0
POLL_LIMIT = 420
# Продолжение принятой раньше генерации (перезапуск службы, повтор задачи) ждёт её не дольше
# LOST_AFTER от принятия, а потом квитанция «lost» и кадр заказывается заново. Замер 21.09 по
# 17 967 скачанным кадрам: от принятия до картинки не больше 372 с (p99 110 с), и ни одна
# генерация, прерванная перезапуском службы, потом не догрузилась — даже через минуту. Без этого
# кадр вечно ждёт тот же номер: 20.09 так застряли 91 квитанция в 4 роликах, по 420 с на повтор.
LOST_AFTER = 600       # скептик 21.09: после ~6 мин провайдер номер уже не отдаёт; 15 мин держали поток впустую
LOST_POLL = 60          # давнюю генерацию всё же спрашиваем: готовую провайдер отдаёт с первого опроса
MAX_REFS = 10
MAX_THREADS = max(1, int(os.environ.get("MANGA_IMAGE_THREADS", "14")))
# 14 потоков подобраны под часовую квоту ключа: 14 × ~101 картинки/ч × 4 единицы ≈ 5700 из 6000.
# Без бюджета FastGen (по умолчанию) "квота часа исчерпана" не ждёт, а роняет кадр (break в generate_many),
# и 14 — общий предел процесса. С бюджетом (fastgen_pool, MANGA_PARALLEL_VIDEOS > 1 или MANGA_FASTGEN_KEYS)
# 14 — предел на ролик, общий предел — до 25 потоков на ключ, а исчерпанный час кадр пережидает.
_SLOTS = threading.BoundedSemaphore(MAX_THREADS)

_KEY: Optional[str] = None
_KEY_LOCK = threading.Lock()


class ImageError(RuntimeError):
    pass


class BusyError(ImageError):
    """Временная занятость слотов провайдера. Лечится ожиданием, не отказом."""
    pass


class PendingImageError(ImageError):
    """Accepted/uncertain request: resume it, never purchase another automatically."""
    pass


class LostImageError(ImageError):
    """Принятая давно генерация у провайдера не завершается: квитанция «lost», кадр заказывается заново."""
    pass


class QuotaWaitError(ImageError):
    """Час ключа исчерпан по /usage (бюджет FastGen включён): кадр ждёт следующего часа, попытка не тратится."""
    pass


# Общий бюджет единиц и потоков на ключ (fastgen_pool). Без новых настроек выключен: всё как раньше.
_POOL = None
_POOL_READY = False
_POOL_LOCK = threading.Lock()
FP_POOL.settings()   # неверные MANGA_FASTGEN_* останавливают запуск службы, а не кадр


def _pool():
    global _POOL, _POOL_READY
    with _POOL_LOCK:
        if not _POOL_READY:
            cfg = FP_POOL.settings()
            if cfg is not None:
                keys = cfg["keys"] or [{"key": key(), "name": "ключ 1", "limit": None, "threads": None}]
                _POOL = FP_POOL.Pool(keys, lambda api_key: _req("/usage", api_key=api_key, timeout=20),
                                     threads=cfg["threads"], reserve=cfg["reserve"], usage_every=cfg["usage_every"])
                FP_POOL.CURRENT = _POOL
            _POOL_READY = True
        return _POOL


# Звёздочка-водяной знак генератора в правом нижнем углу (центр ≈100 px от
# краёв, ≈40 px в поперечнике — замер 10.09 по 92 кадрам двух размеров).
# Раньше её заливали только в assemble.prepare_frame при сборке — и судья,
# пересдача и просмотр видели её на кадрах (38 кадров круга 2, review_synthesis
# §3). Поэтому теперь заливаем СРАЗУ после записи кадра, поверх файла.
# Параметры те же, что в assemble (там прежняя заливка остаётся: повторная
# заливка уже чистого места безвредна — проверено, diff = 0).
# Листы персонажей (1376×768 тоже в _GEN_SIZES!) не трогаем — только кадры.
_WM_OFFSET = 100
_WM_RADIUS = 26
_GEN_SIZES = {(896, 1200), (1376, 768), (768, 1376), (1200, 896)}

# Заливать ВСЛЕПУЮ, как в assemble, здесь нельзя: звезда есть лишь на 22–30% кадров
# (замер 10.09 по 1512 кадрам run_ceo, r2, run_v, run_isagi), а на остальных
# слепая заливка стирает круг в 52 px и там, где контур пересекает окружность,
# Telea тянет тёмные линии к центру — получается тёмный крест (run_v 00003,
# run_isagi 00010). В копии .prepared это было терпимо, в самом файле кадра —
# порча навсегда. Поэтому сначала ищем звезду: она — четырёхлучевая астроида
# |x|^0.75+|y|^0.75 ≤ 24^0.75 (IoU 0.91 с формой на r2/00014), полупрозрачно-белая,
# пляшет в пределах ±6 px. Признак — доля точек контура, где внутри светлее,
# чем снаружи (у звезды край резкий по ВСЕМУ контуру, у светлого пола с линиями —
# лишь на части): ≥0.80 — звезда (полоса 0.8–0.95 — 48 из 48 звёзд глазами,
# 27 кадров рецензента r2 — все ≥0.86); 0.65–0.80 берём только с корреляцией
# формы ≥0.30 (8 звёзд из 9). Ниже — не трогаем: пропуск оставит звезду судье,
# как было раньше, ложная заливка испортит кадр навсегда.
_WM_STAR_A = 24.0
_WM_STAR_PW = 0.75
_WM_JITTER = 6


def watermark_offset(gray):
    """(dx, dy) звезды относительно (w−100, h−100) или None, если звезды нет.
    gray — серый кадр (numpy, uint8/float)."""
    import numpy as np
    h, w = gray.shape[:2]
    cx, cy = w - _WM_OFFSET, h - _WM_OFFSET
    if cx - 40 < 0 or cy - 40 < 0 or cx + 40 >= w or cy + 40 >= h:
        return None
    g = gray.astype(float)
    ang = np.linspace(0, 2 * np.pi, 64, endpoint=False)
    ca, sa = np.cos(ang), np.sin(ang)
    rad = _WM_STAR_A / ((np.abs(ca) ** _WM_STAR_PW + np.abs(sa) ** _WM_STAR_PW) ** (1 / _WM_STAR_PW))
    best, best_off = 0.0, (0, 0)
    for dx in range(-_WM_JITTER, _WM_JITTER + 1, 2):
        for dy in range(-_WM_JITTER, _WM_JITTER + 1, 2):
            gi = g[np.round(cy + dy + rad * 0.8 * sa).astype(int), np.round(cx + dx + rad * 0.8 * ca).astype(int)]
            go = g[np.round(cy + dy + (rad + 5) * sa).astype(int), np.round(cx + dx + (rad + 5) * ca).astype(int)]
            f = float(((gi - go) > 2).mean())
            if f > best:
                best, best_off = f, (dx, dy)
    if best >= 0.80:
        return best_off
    if best < 0.65:
        return None
    # серая зона: подтверждаем корреляцией круга r<26 с формой звезды
    yy, xx = np.mgrid[-30:30, -30:30]
    disc = np.hypot(xx, yy) < _WM_RADIUS
    shape = (np.abs(xx) ** _WM_STAR_PW + np.abs(yy) ** _WM_STAR_PW) <= _WM_STAR_A ** _WM_STAR_PW
    t = shape[disc].astype(float)
    t = (t - t.mean()) / t.std()
    patch = g[cy - 30:cy + 30, cx - 30:cx + 30][disc]
    sd = patch.std()
    corr = 0.0 if sd < 1 else float(((patch - patch.mean()) / sd * t).mean())
    return best_off if corr >= 0.30 else None


def is_frame_job(job: dict) -> bool:
    """Кадр ролика — по флагу kind='frame' или по папке frames/."""
    if job.get("kind") == "frame":
        return True
    path = job.get("path") or ""
    return os.path.basename(os.path.dirname(os.path.abspath(path))) == "frames" if path else False


def clean_watermark(path: str) -> bool:
    """Заливает пятно звёздочки НА МЕСТЕ (inpaint Telea, круг r=26 в точке
    (w−100, h−100) плюс найденный сдвиг звезды); пишет поверх исходного файла
    атомарно через .tmp. Генератор отдаёт JPEG под именем .png — перезаписываем
    настоящим PNG (без потерь), чтобы повторный вызов давал ровно тот же файл.
    Заливаем только когда звезда найдена (watermark_offset) — см. замер выше.
    True — файл перезаписан; False — звезды нет, не наш размер, нечитаем или ошибка."""
    if not path or not os.path.exists(path):
        return False
    try:
        import cv2
        import numpy as np
        im = cv2.imread(path)
        if im is None:
            return False
        h, w = im.shape[:2]
        if (w, h) not in _GEN_SIZES:      # не наш размер — не трогаем
            return False
        off = watermark_offset(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY))
        if off is None:                   # звезды нет — кадр не трогаем
            return False
        mask = np.zeros((h, w), np.uint8)
        cv2.circle(mask, (w - _WM_OFFSET + off[0], h - _WM_OFFSET + off[1]), _WM_RADIUS, 255, -1)
        fixed = cv2.inpaint(im, mask, 3, cv2.INPAINT_TELEA)
        ok, buf = cv2.imencode(".png", fixed)
        if not ok:
            return False
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(buf.tobytes())
        os.replace(tmp, path)             # атомарно: либо чистый кадр, либо прежний
        return True
    except Exception as e:   # noqa: BLE001 — кадр важнее водяного знака
        print(f"  ! clean_watermark {os.path.basename(path)}: {e}")
        return False


def key() -> str:
    global _KEY
    with _KEY_LOCK:
        if _KEY:
            return _KEY
        for path in (os.environ.get("MANGA_KEYS"),
                     os.path.expanduser("~/manga_rewrite/keys.json"),
                     "/root/manga_rewrite/keys.json"):
            if path and os.path.exists(path):
                _KEY = json.load(open(path, encoding="utf-8"))["fastgen"]["key"]
                return _KEY
        env = os.environ.get("FASTGEN_KEY")
        if env:
            _KEY = env
            return _KEY
        raise ImageError("ключ FastGen не найден: положи keys.json или задай FASTGEN_KEY")


def _req(path: str, method: str = "GET", body: Optional[dict] = None, timeout: int = 90,
         api_key: Optional[str] = None):
    r = urllib.request.Request(
        BASE + path, method=method,
        headers={"X-API-Key": api_key or key(), "Content-Type": "application/json; charset=utf-8"},
        data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            return f.status, json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": "нечитаемый ответ"}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _failure_reason(p: object) -> str:
    """Почему генератор отказал — словами, а не первыми 200 знаками ответа.

    Причина лежит в конце ответа (error/message/results[].error), а начало занято
    служебными полями. 20.09 из-за этого лист героя трижды вставал с «failed: {"id":…»,
    и настоящая причина отказа в квитанцию не попадала вовсе.
    """
    if not isinstance(p, dict):
        return str(p)[:300]
    seen = []
    def dig(value):
        if isinstance(value, str) and value.strip():
            seen.append(value.strip())
        elif isinstance(value, dict):
            for field in ("message", "reason", "detail", "description", "code"):
                dig(value.get(field))
    for field in ("error", "message", "failure_reason", "reason", "moderation", "detail"):
        dig(p.get(field))
    for item in (p.get("results") or []) if isinstance(p.get("results"), list) else []:
        if isinstance(item, dict):
            dig(item.get("error"))
    if seen:
        # Порядок сохраняем, повторы убираем: «blocked; content_policy_violation».
        out, done = [], set()
        for s in seen:
            if s.lower() not in done:
                done.add(s.lower())
                out.append(s)
        return "; ".join(out)[:400]
    return json.dumps(p, ensure_ascii=False)[:300]


@dataclass
class Ref:
    """Образец внешности: имя файла попадает в промпт, поэтому модель знает,
    к кому относится картинка. Форму {name, input} провайдер принимает —
    проверено живым вызовом; форма без имени отвергается с 422."""
    name: str
    data_uri: str


def data_uri(path: str) -> str:
    """Картинка текстом для запроса. Один файл кодируется один раз.

    21.09.2026: шаг картинок собирает задания на все ~850 кадров заранее, и каждому кадру
    заново кодировал те же 4 образца героев. Копии жили до конца шага: процесс студии
    рос на ~700 МБ в минуту, дошёл до 10,5 ГБ, забил подкачку и душил кабинет VideoStudio.
    Теперь кадры делят одну строку на файл; правка файла меняет размер или время — и ключ.
    """
    st = os.stat(path)
    return _data_uri_of(path, st.st_mtime_ns, st.st_size)


@functools.lru_cache(maxsize=256)
def _data_uri_of(path: str, _mtime_ns: int, _size: int) -> str:
    # FastGen sometimes returns JPEG bytes in a .png file.
    from PIL import Image
    with Image.open(path) as im:
        mime = Image.MIME.get(im.format, "image/png")
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


SEED_MOD = 2**31 - 1   # FastGen отвергает seed больше 2^31-1 (HTTP 422, замер 09.09.2026)


def norm_seed(seed, log=print) -> int:
    """Зерно в диапазон [0, 2^31-2]. Сумма 100000 + idx + шаг*круг в диапазон
    укладывается, но чужой или случайный seed — нет; лучше урезать и сказать
    об этом, чем получить 422 на весь кадр."""
    raw = int(seed)
    s = raw % SEED_MOD
    if s != raw:
        log(f"  seed {raw} вне диапазона FastGen, урезан до {s}")
    return s


def _request_body(prompt, refs=None, seed=None, model=MODEL, aspect=None, options=None):
    """Build the exact wire body once, for sending and resume/cache validation."""
    body: Dict = {"model": model, "prompt": prompt, "aspect_ratio": aspect or ASPECT}
    if refs:
        if len(refs) > MAX_REFS:
            refs = refs[:MAX_REFS]
        body["inputs"] = [{"name": r.name, "input": r.data_uri} for r in refs]
    if seed is not None and model.startswith("nano-banana"):
        body["seed"] = norm_seed(seed)   # openai-image seed НЕ принимает (422)
    if options is None and model == "openai-image":
        options = {"quality": IMAGE_QUALITY}
    if options:
        body.update(options)

    return body


def _source_fingerprint(job):
    return GR.fingerprint({
        "prompt": job["prompt"], "seed": job.get("seed"),
        "model": job.get("model", MODEL), "aspect": job.get("aspect") or ASPECT,
        "refs": [{"name": r.name, "input": r.data_uri} for r in (job.get("refs") or [])],
        "options": job.get("options") or {},
    })


def generate(prompt: str, refs: Optional[List[Ref]] = None, seed: Optional[int] = None,
             model: str = MODEL, poll_limit: int = POLL_LIMIT,
             aspect: Optional[str] = None, options: Optional[Dict] = None,
             receipt_path: Optional[str] = None, source_fingerprint: Optional[str] = None,
             soften_level: int = 0, quote_bubble: Optional[str] = None,
             retry_failed: bool = False) -> bytes:
    """Один кадр. Возвращает байты картинки или бросает ImageError.
    options — параметры конкретной модели поверх тела запроса: у openai-image
    (GPT Image 2.5 на FastGen, 10.09.2026) это {"quality": "fast"|"thinking"}
    (4 и 6 единиц часовой квоты за картинку против 1 у nano-banana)."""
    body = _request_body(prompt, refs, seed, model, aspect, options)
    pool = _pool()
    lease = None

    def reserve():
        # Бюджет включён: место на ключе и единицы часа берутся, когда решено покупать, до записи «отправляю».
        nonlocal lease
        lease = pool.acquire(FP_POOL.units_for(body))
        return {"key_id": lease.key_id}

    try:
        try:
            receipt = GR.Receipt(receipt_path)
            gid = receipt.begin(body, source_fingerprint, soften_level, quote_bubble=quote_bubble,
                                before_post=reserve if pool is not None else None,
                                retry_failed=retry_failed)
        except GR.UncertainRequest as e:
            raise PendingImageError(str(e)) from e
        except GR.FailedRequest as e:
            raise ImageError(str(e)) from e
        if gid and pool is not None:
            lease = _resume_lease(pool, (receipt.current or {}).get("key_id"))
        elif gid:
            bought_by = (receipt.current or {}).get("key_id")
            if bought_by and bought_by != FP_POOL.key_id(key()):
                # Бюджет выключен, а кадр куплен другим ключом (из MANGA_FASTGEN_KEYS): ключ keys.json его не увидит.
                raise PendingImageError(f"кадр куплен ключом FastGen {bought_by}, а бюджет с этим ключом выключен "
                                        "(MANGA_FASTGEN_KEYS не задан); догрузка остановлена, новая покупка не делается")
        return _post_and_fetch(body, receipt, gid, lease, poll_limit)
    finally:
        if lease is not None:
            lease.close()


def _resume_lease(pool, bought_by):
    """Принятая раньше генерация догружается тем же ключом, которым куплена; единицы не резервируются."""
    if bought_by is None:
        # Купленные до бюджета кадры — ключом из keys.json; если его нет в пуле, догружаем им же без места в пуле.
        bought_by = FP_POOL.key_id(key())
        if not any(k.id == bought_by for k in pool.keys):
            return None
    try:
        return pool.acquire(0, key=bought_by)
    except KeyError:
        raise PendingImageError(f"кадр куплен ключом FastGen {bought_by}, которого больше нет в MANGA_FASTGEN_KEYS; "
                                "догрузка остановлена, новая покупка не делается") from None


def _post_and_fetch(body, receipt, gid, lease, poll_limit):
    api_key = lease.key if lease is not None else None
    accepted_at = (receipt.current or {}).get("accepted_at") if gid else None
    if accepted_at:
        poll_limit = max(float(accepted_at) + LOST_AFTER - time.time(), LOST_POLL)
    if not gid:
        sc, j = _req("/generations", "POST", body) if api_key is None else _req("/generations", "POST", body, api_key=api_key)
    else:
        sc, j = 200, {"id": gid}
    if sc == 429:
        receipt.update("rejected", http_status=sc, error=str(j)[:200])
        if lease is not None:
            lease.refund()   # отказ POST единиц не съел
        # 429 бывает ДВУХ разных видов, и путать их дорого:
        #   concurrency_exceeded — заняты все 25 одновременных слотов. Это на
        #     секунды, надо просто подождать и повторить.
        #   исчерпан часовой лимит — до конца часа делать нечего.
        # Раньше оба считались «квота исчерпана», и код бросал работу при
        # первой же занятости слотов: проверено 08.09.2026, три кадра из трёх
        # умерли за 3 секунды, хотя ошибка была временная.
        code = (j.get("code") or "") if isinstance(j, dict) else ""
        text = str(j)[:200]
        if "concurrency" in code or "concurrency" in text.lower():
            raise BusyError("все слоты заняты: " + text[:120])
        if lease is not None and _pool().confirm_exhausted(lease):
            # /usage подтвердил: час ключа израсходован (в том числе чужими тратами). Кадр ждёт следующего часа.
            raise QuotaWaitError("квота часа исчерпана, по /usage подтверждено; кадр ждёт следующего часа: " + text[:120])
        raise ImageError("квота часа исчерпана: " + text[:160])
    if sc not in (200, 201, 202) or not isinstance(j, dict) or not j.get("id"):
        uncertain = sc == 0 or sc >= 500 or sc in (200, 201, 202)
        receipt.update("uncertain" if uncertain else "rejected", http_status=sc,
                       error=f"POST {sc}: {str(j)[:200]}")
        if uncertain:
            raise PendingImageError("FastGen мог принять запрос, но номер не получен. Повторная покупка остановлена")
        if lease is not None:
            lease.refund()
        raise ImageError(f"POST {sc}: {str(j)[:200]}")

    gid = j["id"]
    if receipt.current.get("status") == "posting":
        receipt.accepted(gid, body)
        if lease is not None:
            lease.accepted()
    t0 = time.time()
    # Продолжение давней генерации: опрос короче LOST_POLL, чтобы один зависший ответ не съел окно.
    short = {"timeout": max(5, int(LOST_POLL // 2))} if accepted_at else {}
    gone = False     # сам провайдер ответил, что номера нет (404) или что генерация всё ещё в работе
    while time.time() - t0 < poll_limit:
        time.sleep(POLL_EVERY)
        sc, p = (_req(f"/generations/{gid}", **short) if api_key is None
                 else _req(f"/generations/{gid}", api_key=api_key, **short))
        status = p.get("status") if isinstance(p, dict) else None
        # Потерю решает только ответ провайдера: 404 — номера нет; 200 без итога — висит. Обрыв сети,
        # 5xx и 429 — не ответ про генерацию: такой кадр ждёт дальше, новую покупку не делаем.
        gone = sc == 404 or (sc == 200 and status not in ("succeeded", "failed", "cancelled"))
        if status == "succeeded":
            receipt.update("succeeded")
            res = (p.get("results") or [])
            if not res:
                raise ImageError("succeeded, но results пуст")
            r0 = res[0]
            if r0.get("data"):
                return base64.b64decode(r0["data"])
            url = r0.get("download_url")
            if url:
                if url.startswith("/"):
                    url = "https://googler.fast-gen.ai" + url
                rq = urllib.request.Request(url, headers={"X-API-Key": api_key or key()})
                with urllib.request.urlopen(rq, timeout=120) as f:
                    return f.read()
            raise ImageError("в результате нет ни data, ни download_url")
        if status in ("failed", "cancelled"):
            error = f"{status}: {_failure_reason(p)}"
            refunded = (p.get("usage") or {}).get("refunded") is True if isinstance(p.get("usage"), dict) else False
            if refunded:
                receipt.update("failed", error=error, refunded=True)
                if lease is not None:
                    lease.refund()   # провайдер вернул единицы за неудачную генерацию
            else:
                receipt.update("failed", error=error)
            raise ImageError(error)
    if accepted_at and gone:
        age = (time.time() - float(accepted_at)) / 60
        receipt.update("lost", error=f"принята {age:.0f} мин назад, у провайдера так и не завершилась")
        raise LostImageError(f"Генерация {gid} принята {age:.0f} мин назад и не нашлась у провайдера; "
                             "кадр будет заказан заново")
    raise PendingImageError(f"Генерация {gid} ещё не завершилась за {poll_limit}s; продолжение проверит тот же номер")


# Признаки отказа по содержанию в тексте ошибки провайдера (27.09.2026): по ним лестница мягкости
# поднимается сразу, всё остальное считается обрывом связи.
_СЛОВА_ЦЕНЗУРЫ = ("safety", "policy", "content", "moderat", "blocked",
                  "prohibit", "violat", "nsfw")


def _обрывы_подряд(журнал, level, source_fingerprint) -> int:
    """Сколько последних НАСТОЯЩИХ ответов провайдера на этой ступени подряд — сбои не по содержанию.

    27.09.2026 (I2-01): счётчик в памяти рос и когда попытка лишь перечитывала сохранённую ошибку —
    после одного сбоя второй же POST уходил смягчённым. Запись в квитанции появляется только при
    новой отправке, поэтому считаем по ней; и перезапуск службы счёт не обнуляет.
    429 «слоты заняты» — не ответ про картинку: не считаем и цепочку не рвём."""
    n = 0
    for a in reversed(журнал.data.get("attempts") or []):
        if a.get("status") == "rejected" and a.get("http_status") == 429:
            continue
        if (a.get("status") not in ("failed", "rejected") or (a.get("soften_level") or 0) != level
                or a.get("source_job_sha256") != source_fingerprint
                or any(w in str(a.get("error") or "").lower() for w in _СЛОВА_ЦЕНЗУРЫ)):
            break
        n += 1
    return n


def generate_many(jobs: List[dict], threads: int = MAX_THREADS, on_done=None) -> List[dict]:
    """jobs: [{'key':str,'prompt':str,'refs':[Ref],'seed':int,'path':str}]

    Пропуск НЕ сдвигает остальные: у каждой работы свой ключ, и упавшая
    возвращается со своей ошибкой, а не выпадает из списка.
    Кэш по содержимому: если файл уже есть — не перегенерируем.
    """
    out: List[dict] = [None] * len(jobs)   # type: ignore
    lock = threading.Lock()
    sem = threading.Semaphore(max(1, min(MAX_THREADS, int(threads))))
    # Бюджет FastGen включён: потоки считаются на ключ (до 25) между всеми роликами процесса, а не общие 14.
    slots = nullcontext() if _pool() is not None else _SLOTS
    owner = FP_POOL.context()   # ролик и признак отмены потока-вызывателя (исполнитель очереди) — в потоки кадров

    def one(i: int, job: dict):
        FP_POOL.bind(*owner)
        with sem, slots, GR.destination_lock(job.get("path")):
            path = job.get("path")
            t0 = time.time()
            err = None
            обрывов = 0          # подряд идущие сбои связи, не связанные с содержанием
            tries = job.get("tries", 6)
            soften = job.get("soften")
            receipt_path = path + ".generation.json" if path else None
            try:
                kept = (GR.drawn_after(receipt_path, job["drawn_after"])
                        if job.get("drawn_after") is not None and GR.valid_image(path) else None)
                if kept is not None:
                    # Картинка скачана ПОСЛЕ нынешней раскадровки — это картинка этого самого кадра.
                    # Промпт мог разойтись с квитанцией только правкой того, как студия собирает
                    # промпт (самопроверка, чистка паспорта), и перерисовывать ради этого нельзя:
                    # 21.09 испанский ролик встал на 262 таких кадрах (~1050 единиц впустую).
                    with lock:
                        out[i] = {**job, "status": "уже есть", "seconds": 0.0, "attempts": 0,
                                  "soften_level": kept.get("soften_level") or 0}
                    if on_done: on_done(out[i])
                    return
                source_fingerprint = _source_fingerprint(job)
                receipt = GR.Receipt(receipt_path)
                level = receipt.resume_level(source_fingerprint)
                prompt = job["prompt"] if not (soften and level) else soften(job["prompt"], level)
                actual_body = _request_body(prompt, job.get("refs"), job.get("seed"),
                                            job.get("model", MODEL), job.get("aspect"), job.get("options"))
                if GR.valid_image(path):
                    if receipt.current and not receipt.matches(actual_body, source_fingerprint):
                        raise GR.UncertainRequest("Готовый файл относится к другому запросу; кэш не использован")
                    if receipt.current and receipt.current.get("id") and receipt.current.get("status") != "downloaded":
                        if is_frame_job(job):
                            clean_watermark(path)
                        receipt.update("downloaded", output_bytes=os.path.getsize(path))
                    with lock:
                        out[i] = {**job, "status": "уже есть", "seconds": 0.0,
                                  "attempts": 0, "soften_level": level}
                    if on_done: on_done(out[i])
                    return
            except Exception as e:
                with lock:
                    out[i] = {**job, "status": "ошибка", "error": str(e), "seconds": 0.0,
                              "attempts": 0, "soften_level": 0}
                if on_done: on_done(out[i])
                return
            attempt = 0                         # сколько попыток реально сделано
            last_level = 0                      # уровень мягкости последней попытки
            повтор_сбоя = False                 # тот же текст ещё раз поверх «failed» (решает лестница)
            while attempt < tries:
                attempt += 1
                try:
                    last_level = level
                    prompt = job["prompt"] if not (soften and level) else soften(job["prompt"], level)
                    data = generate(prompt, job.get("refs"),
                                    job.get("seed"), job.get("model", MODEL),
                                    aspect=job.get("aspect"), options=job.get("options"),
                                    receipt_path=receipt_path, source_fingerprint=source_fingerprint,
                                    soften_level=level, quote_bubble=job.get("quote_bubble"),
                                    retry_failed=повтор_сбоя)
                    if path:
                        tmp = path + ".tmp"
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(tmp, "wb") as f:
                            f.write(data)
                            f.flush()
                            os.fsync(f.fileno())
                        if not GR.valid_image(tmp):
                            raise PendingImageError("FastGen вернул нечитаемую картинку; продолжение скачает ту же генерацию")
                        os.replace(tmp, path)     # атомарно: либо целый файл, либо старый
                        # Звёздочку убираем сразу, пока кадр не увидели судья,
                        # пересдача и просмотр (38 кадров круга 2). Только кадры:
                        # листы персонажей того же размера 1376×768 не трогаем.
                        if is_frame_job(job):
                            clean_watermark(path)
                        GR.Receipt(path + ".generation.json").update("downloaded", output_bytes=os.path.getsize(path))
                    with lock:
                        out[i] = {**job, "status": "готово", "seconds": round(time.time() - t0, 1),
                                  "attempts": attempt, "bytes": len(data),
                                  "soften_level": level}
                    if on_done: on_done(out[i])
                    return
                except BusyError as e:
                    # Слоты заняты: ждём дольше и НЕ тратим попытку зря.
                    err = str(e)
                    time.sleep(min(30.0, 5.0 * attempt) + random.uniform(0, 3.0))
                except PendingImageError as e:
                    err = str(e)
                    break
                except FP_POOL.Cancelled as e:
                    # Остановка службы или ролик забрали, пока кадр ждал места: покупки не было, повторять нельзя.
                    err = str(e)
                    break
                except LostImageError as e:
                    # Потерянная генерация: следующая попытка покупает тот же запрос на той же
                    # ступени мягкости (лестницу не поднимаем — отказа по содержанию не было).
                    err = str(e)
                    if job.get("resume_only"):
                        # Восстановление выбранной картинки: новая покупка дала бы другую картинку.
                        err = "выбранная генерация у провайдера утрачена — новую не покупаю: " + err
                        break
                except QuotaWaitError as e:
                    # Час ключа исчерпан по /usage: тот же заказ ждёт следующего часа в бюджете (fastgen_pool),
                    # попытка не тратится. Бесконечно не крутится: каждый такой отказ подтверждён /usage.
                    err = str(e)
                    attempt -= 1
                except ImageError as e:
                    err = str(e)
                    if "квота часа" in err:
                        break
                    журнал = GR.Receipt(receipt_path) if receipt_path else None
                    current = журнал.current if журнал else None
                    if current and current.get("status") in ("posting", "uncertain", "accepted", "succeeded", "downloaded"):
                        # A result/download problem never starts another prompt
                        # stage for an already accepted request.
                        break
                    # Отказ по содержанию повтором того же текста не лечится:
                    # шесть одинаковых попыток умрут одинаково и кадр приедет
                    # дырой в ролик. Поэтому поднимаемся по лестнице мягкости.
                    low = err.lower()
                    # обрыв связи — не цензура. Провайдер начинает словом «failed» ЛЮБУЮ
                    # ошибку, включая таймаут, и кадр после первого же сбоя рисовался по
                    # обеднённому заданию (1247 кадров, замер 27.09). По содержанию урезаем
                    # сразу; по обрывам — только с третьего подряд, на случай если цензура
                    # спряталась за общей формулировкой.
                    содержание = any(w in low for w in _СЛОВА_ЦЕНЗУРЫ)
                    if not содержание:
                        # 27.09.2026 (I2-01): считаем только новые ответы провайдера (записи квитанции),
                        # а не перечитывания одной ошибки: раньше 1 сбой давал смягчённый второй POST.
                        обрывов = (_обрывы_подряд(журнал, level, source_fingerprint) if журнал
                                   else обрывов + 1)
                    повтор_сбоя = False
                    if soften and (level or содержание or обрывов >= 3):
                        level = min(3, level + 1)
                    elif soften:
                        # Тот же текст ещё раз: квитанция «failed» пускает новую отправку только с этим флагом.
                        повтор_сбоя = True
                    time.sleep(2 * attempt + random.uniform(0, 1.5))
                except Exception as e:
                    # Не ImageError (таймаут скачивания по download_url, битый
                    # base64, ошибка записи) раньше убивал поток: в out[i]
                    # оставался None, и step_images/repair падали на r["status"].
                    err = f"{type(e).__name__}: {e}"
                    time.sleep(2 * attempt + random.uniform(0, 1.5))
            with lock:
                out[i] = {**job, "status": "ошибка", "error": err,
                          "seconds": round(time.time() - t0, 1),
                          "attempts": attempt, "soften_level": last_level}
            if on_done: on_done(out[i])

    ths = [threading.Thread(target=one, args=(i, j), daemon=True) for i, j in enumerate(jobs)]
    for t in ths: t.start()
    for t in ths: t.join()
    return out


def quota() -> dict:
    pool = _pool()
    if pool is not None:
        # Бюджет включён: остаток по всем ключам пула (MANGA_FASTGEN_KEYS), каждый — своим /usage.
        got = pool.summary()
        if not got["read"]:
            raise ImageError("usage: остаток ни одного ключа FastGen не прочитан")
        return {"limit": got["limit"], "threads": got["threads"], "used": got["used"]}
    sc, j = _req("/usage")
    if sc != 200:
        raise ImageError(f"usage {sc}: {str(j)[:150]}")
    lim = j.get("account_limits", {})
    used = ((j.get("current_usage") or {}).get("hourly_usage") or {}).get("image_generation") or {}
    return {
        "limit": lim.get("img_gen_per_hour_limit"),
        "threads": lim.get("img_generation_threads_allowed"),
        "used": used.get("current_usage", 0) if isinstance(used, dict) else 0,
    }
