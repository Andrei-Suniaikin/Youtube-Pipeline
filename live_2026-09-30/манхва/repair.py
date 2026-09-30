# -*- coding: utf-8 -*-
"""Автоповтор брака: то, чего у нас не было вовсе.

В первом прогоне семь кадров не сгенерировались, и это просто записали в отчёт —
дыра доехала до сборки видео. Дыра в видеоряде хуже любого качества картинки.

Что считаем браком (только надёжно измеримое — метрики «та же ли комната»
ненадёжны, и строить на них перегенерацию нельзя):
  1. файла нет вовсе;
  2. файл битый или подозрительно маленький;
  3. поля/рамки по краям больше порога;
  4. кадр выпал из стиля ролика сильнее всех (CLIP к среднему).
Пункт 4 — ранжирование, а не приговор: берём только явные выбросы и не больше
доли от всего ролика, иначе один сдвиг стиля утащит на перегенерацию половину.
"""
from __future__ import annotations

import os
import shutil
from typing import Callable, Dict, List, Optional

MAX_SHARE = 0.15         # больше 15% ролика на перегенерацию не отправляем
SEED_STEP = 7919         # простое число: новое зерно, а не соседнее
REPAIR_DIR = ".repair"   # подпапка в frames: кандидаты пересдачи и копии оригиналов
MIN_BYTES = 20000        # тот же порог, что в pick(): меньше — файл считаем битым


def _intact(path: Optional[str]) -> Optional[str]:
    """None, если кандидат целый; иначе текст причины.

    Проверяем ДО подмены оригинала: провайдер может вернуть «succeeded» с
    обрубком (edge-tts так отдал 2160 байт без ошибки, 09.09.2026), и кэш по
    размеру такое пропустит. Целым считаем файл > 20 КБ, который открывает PIL.
    """
    if not path or not os.path.exists(path):
        return "файл кандидата не появился"
    if os.path.getsize(path) < MIN_BYTES:
        return f"кандидат подозрительно маленький ({os.path.getsize(path)} байт)"
    try:
        from PIL import Image
    except ImportError:
        return None            # без PIL довольствуемся размером
    try:
        # load(), а не verify(): verify() пропустил PNG, обрубленный до половины
        # (317 КБ из 635, проверено 10.09.2026), load() на нём даёт OSError.
        with Image.open(path) as im:
            im.load()
    except Exception as e:
        return f"кандидат не открывается: {type(e).__name__}: {e}"
    return None


def pick(frames_meta: List[dict], frames_dir: str, qa_summary: Optional[dict] = None,
         style: Optional[dict] = None, log=print) -> List[dict]:
    """Возвращает список кадров на перегенерацию с причиной."""
    import cv2
    bad: Dict[int, str] = {}

    for fd in frames_meta:
        idx = int(fd.get("idx", 0))
        p = os.path.join(frames_dir, f"{idx:05d}.png")
        if not os.path.exists(p):
            bad[idx] = "не сгенерировался"
            continue
        if os.path.getsize(p) < 20000:
            bad[idx] = "файл подозрительно маленький"
            continue

    # Крупные планы, врезки и удары по замыслу стоят на абстрактном или
    # упрощённом фоне (блок PANEL), поэтому их яркость законно отличается от
    # медианы сцены — иначе каждый круг пересдачи гонял бы их по кругу.
    try:
        from types import SimpleNamespace
        from frameprompt import panel_class
        abstract = {int(fd.get("idx", 0)) for fd in frames_meta
                    if panel_class(SimpleNamespace(**fd)) in ("close", "eyes", "detail", "impact")}
    except Exception:
        abstract = set()

    # Кадры со словом-звуком: генератор рисует его сам, и OCR его найдёт —
    # это не брак. Чужой текст на этих ~3% кадров смотрим глазами.
    with_sfx = {int(fd.get("idx", 0)) for fd in frames_meta if (fd.get("sfx") or "").strip()}
    # Кадр, нарисованный с облаком своей реплики (_speech_bubble, MANGA_QUOTE_MODE=panel):
    # надпись из текста этой реплики — не брак; любая другая — брак, как везде.
    bubbles = {int(fd.get("idx", 0)): fd.get("_speech_bubble") for fd in frames_meta if fd.get("_speech_bubble")}

    for row in ((qa_summary or {}).get("frames") or []):
        if row.get("idx") is None:
            continue
        if row.get("bars_pct", 0) > 1.0:
            bad.setdefault(int(row["idx"]), f"поля по краям {row['bars_pct']}%")
        # Надписи: RapidOCR с уверенностью >0.6 на калибровке не дал ни одной
        # ложной тревоги, значит его находка — основание для пересдачи.
        if row.get("text") and int(row["idx"]) not in with_sfx \
                and not text_is_quote(row["text"], bubbles.get(int(row["idx"]))):
            bad.setdefault(int(row["idx"]), f"надписи: {row['text'][:3]}")
        if abs(row.get("bright_vs_scene", 0)) > 35 and int(row["idx"]) not in abstract:
            bad.setdefault(int(row["idx"]), f"яркость {row['bright_vs_scene']:+.0f} от медианы сцены")

    # Выброс по манере рисунка (>2.5 сигмы от остального ролика): так поймался
    # кадр, вышедший фотографией. Это ранжирование, поэтому порог строгий.
    for idx in ((qa_summary or {}).get("manner_outliers") or []):
        bad.setdefault(int(idx), "выпал по манере рисунка")

    if style and style.get("outliers"):
        for idx in style["outliers"]:
            bad.setdefault(int(idx), "выпал из стиля ролика")

    limit = max(1, int(len(frames_meta) * MAX_SHARE))
    picked = [{"idx": i, "why": w} for i, w in sorted(bad.items())]
    if len(picked) > limit:
        log(f"  на перегенерацию отобрано {len(picked)}, это больше {int(MAX_SHARE*100)}% "
            f"ролика — беру только первые {limit}, остальное смотрим глазами")
        picked = picked[:limit]
    return picked


def _letters(text) -> str:
    import re
    import unicodedata
    s = re.sub(r"[^\w]|_", "", str(text or "").upper().replace("Ё", "Е"))
    # Латинская диакритика (Ę Ł Ș Ř Ã Ñ…) снимается: RapidOCR на польском облаке читает
    # «MOGĘ» как «MOGE», и реплика засчитывалась бы чужой надписью. Кириллица не трогается
    # (Й остаётся Й) — русский ведёт себя как раньше.
    if s.isascii() or not re.search(r"[À-ɏḀ-ỿ]", s):
        return s
    out = []
    for ch in s:
        if "À" <= ch <= "ɏ" or "Ḁ" <= ch <= "ỿ":
            ch = {"Ł": "L", "Đ": "D", "Ø": "O", "Æ": "AE", "Œ": "OE", "ẞ": "SS"}.get(ch) or "".join(
                c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c))
        out.append(ch)
    return "".join(out)


def text_is_quote(words, quote) -> bool:
    """Все найденные OCR надписи — куски текста реплики из облака.

    OCR читает облако словами или строками и ошибается в отдельных буквах, поэтому
    слово засчитывается, если не меньше 80% его букв подряд совпало с текстом
    реплики (без пробелов и знаков). Хоть одно чужое слово — False."""
    target = _letters(quote)
    if not target or not words:
        return False
    from difflib import SequenceMatcher
    for word in words:
        w = _letters(word)
        if not w:
            continue
        if w in target:
            continue
        m = SequenceMatcher(None, w, target, autojunk=False)
        matched = sum(block.size for block in m.get_matching_blocks())
        match = m.find_longest_match(0, len(w), 0, len(target))
        if match.size < max(3, len(w) // 2) or matched < 0.8 * len(w):
            return False
    return True


def _sha(path):
    from pathlib import Path
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _copy_atomic(source, destination):
    """Retain immutable candidate/backup bytes; only replace the selected alias."""
    tmp = str(destination) + ".selected.tmp"
    shutil.copy2(source, tmp)
    os.replace(tmp, destination)


def save_candidate_job(job):
    """Persist exact source inputs; images.generate_many owns the accepted receipt."""
    from pathlib import Path
    from web_queue import read_json, write_json
    record = {k: job[k] for k in ("key", "prompt", "seed", "model", "aspect", "options", "kind", "tries", "quote_bubble") if k in job}
    record["refs"] = [{"name": r.name, "data_uri": r.data_uri} for r in (job.get("refs") or [])]
    record["soften"] = bool(job.get("soften"))
    path = Path(str(job["path"]) + ".job.json")
    if path.exists() and read_json(path) != record:
        raise RuntimeError("Сохранённое задание кандидата изменилось; продолжение остановлено")
    if not path.exists():
        write_json(path, record)
    return path


def backup_image(original, backup):
    """Back up both pixels and which generation was selected before this attempt."""
    from pathlib import Path
    from web_queue import read_json, write_json
    original, backup = Path(original), Path(backup)
    _copy_atomic(original, backup)
    selected = read_json(str(original) + ".selected.json")
    if selected is None:
        selected = {"version": 1, "baseline": True, "image_sha256": _sha(backup),
                    "candidate_path": str(backup.relative_to(original.parent.parent)), "job_path": None}
    write_json(str(backup) + ".selected.json", selected)
    receipt = Path(str(original) + ".generation.json")
    if receipt.exists():
        _copy_atomic(receipt, str(backup) + ".generation.json")


def select_image(original, candidate):
    """Journal selection before switching pixels; replay can finish either half."""
    from pathlib import Path
    from web_queue import read_json, write_json
    original, candidate = Path(original), Path(candidate)
    root = original.parent.parent
    job_path = Path(str(candidate) + ".job.json")
    if not job_path.is_file():
        raise RuntimeError("У кандидата нет сохранённого задания")
    selected = {"version": 1, "baseline": False, "image_sha256": _sha(candidate),
                "candidate_path": str(candidate.relative_to(root)),
                "job_path": str(job_path.relative_to(root)), "job_sha256": _sha(job_path)}
    receipt = read_json(str(candidate) + ".generation.json", {})
    selected["generation_id"] = ((receipt.get("attempts") or [{}])[-1]).get("id")
    write_json(str(original) + ".selected.json", selected)
    _copy_atomic(candidate, original)
    return selected


def restore_image(original, backup):
    from pathlib import Path
    from web_queue import read_json, write_json
    original, backup = Path(original), Path(backup)
    selected = read_json(str(backup) + ".selected.json")
    if selected is None:  # Old backups still preserve baseline A rather than candidate B.
        selected = {"version": 1, "baseline": True, "image_sha256": _sha(backup),
                    "candidate_path": str(backup.relative_to(original.parent.parent)), "job_path": None}
    write_json(str(original) + ".selected.json", selected)
    _copy_atomic(backup, original)


def ensure_selected(original, generate_many):
    """Return selected image metadata, restoring B locally or by B's paid receipt."""
    from pathlib import Path
    from web_queue import read_json
    import images as IM
    import frameprompt
    original = Path(original)
    sidecar = Path(str(original) + ".selected.json")
    if not sidecar.exists():
        return None
    selected = read_json(sidecar)
    if not isinstance(selected, dict) or selected.get("version") != 1:
        raise RuntimeError("Повреждена история выбранного кадра; новая генерация остановлена")
    expected = selected.get("image_sha256")
    if not _intact(str(original)) and _sha(original) == expected:
        return {**selected, "restored": False}
    root = original.parent.parent.resolve()
    def bound(relative):
        path = (root / relative).resolve()
        path.relative_to(root)
        return path
    candidate = bound(selected["candidate_path"])
    if _intact(str(candidate)) or _sha(candidate) != expected:
        if selected.get("baseline"):
            return None  # Original main-job receipt remains canonical for a legacy baseline.
        job_path = bound(selected["job_path"])
        if not job_path.is_file() or _sha(job_path) != selected["job_sha256"]:
            raise RuntimeError("Сохранённое задание выбранного кадра повреждено")
        receipt = read_json(str(candidate) + ".generation.json", {})
        paid = (receipt.get("attempts") or [{}])[-1]
        if not selected.get("generation_id") or paid.get("id") != selected["generation_id"]:
            raise RuntimeError("Нет квитанции выбранной генерации; повторная покупка остановлена")
        if paid.get("status") == "lost":
            # Прошлый прогон уже признал её утраченной у провайдера: новая покупка дала бы ДРУГУЮ
            # картинку, не выбранную (перепроверка 21.09) — стоп без покупки.
            raise RuntimeError("Выбранная генерация у провайдера утрачена; повторная покупка остановлена")
        job = read_json(job_path)
        job["path"] = str(candidate)
        job["refs"] = [IM.Ref(**r) for r in job.get("refs", [])]
        job["soften"] = frameprompt.soften if job.pop("soften", False) else None
        # Только догрузка выбранной генерации: новая покупка дала бы другую картинку, которая
        # никогда не совпадёт с выбранной (проверка 21.09) — утраченную генерацию не перекупаем.
        job["resume_only"] = True
        if candidate.exists() and not _intact(str(candidate)):
            # A valid but altered file must not be accepted as the immutable
            # candidate cache; retain it for diagnosis before re-downloading.
            saved = str(candidate) + ".changed.bak"
            if os.path.exists(saved):
                saved += "." + _sha(candidate)[:12]
            os.replace(candidate, saved)
        results = generate_many([job], threads=1)
        if not results or results[0].get("status") not in ("готово", "уже есть"):
            raise RuntimeError((results[0].get("error") if results else None) or "Выбранный кадр не восстановлен")
        if _intact(str(candidate)) or _sha(candidate) != expected:
            raise RuntimeError("Восстановленная генерация не совпала с выбранной картинкой")
    _copy_atomic(candidate, original)
    return {**selected, "restored": True}


def run(picked: List[dict], build_job: Callable[[int, int], Optional[dict]],
        generate_many: Callable, round_no: int = 1, threads: int = 12,
        log=print) -> dict:
    """build_job(idx, seed) -> job или None. Зерно новое, промпт тот же.

    Промпт намеренно НЕ меняем: сначала надо узнать, сколько брака чинится
    простой пересдачей. Если менять и промпт, и зерно сразу, будет непонятно,
    что помогло.

    Безопасная подмена: кандидат генерируется в <frames>/.repair/r<round>_<idx>.png,
    и только целый кандидат становится на место оригинала (копия оригинала
    остаётся в .repair/orig_<idx>.png). Раньше оригинал стирался ДО генерации —
    при отказе провайдера оставалась дыра, так пропали 12 кадров (09.09.2026).
    Кэш по пути при этом не вернёт старый брак: у кандидата свой путь.

    Возвращает {round, sent, ok, errors, frames:[{idx, status, why}]},
    status ∈ replaced | kept_original | no_original_and_failed.
    """
    jobs = []
    for p in picked:
        j = build_job(p["idx"], 100000 + p["idx"] + SEED_STEP * round_no)
        if not j:
            continue
        j["tries"] = 4
        j["_idx"] = int(p["idx"])
        j["_why"] = p.get("why", "")
        orig = j.get("path")
        j["_orig"] = orig
        if orig:
            rdir = os.path.join(os.path.dirname(orig), REPAIR_DIR)
            os.makedirs(rdir, exist_ok=True)
            import images as IM
            fingerprint = IM._source_fingerprint(j)[:16]
            cand = os.path.join(rdir, f"r{round_no}_{int(p['idx']):05d}_{fingerprint}.png")
            # Same exact job resumes its immutable candidate and paid receipt.
            j["path"] = cand
            # Кандидат лежит в .repair, а не в frames/ — по пути generate_many
            # его кадром не узнает и звёздочку не зальёт (38 кадров круга 2 —
            # см. images.clean_watermark). Помечаем явно: заливка идёт сразу
            # после записи кандидата, до проверки _intact и подмены оригинала.
            if os.path.basename(os.path.dirname(os.path.abspath(orig))) == "frames":
                j.setdefault("kind", "frame")
        if orig:
            save_candidate_job(j)
        jobs.append(j)
    if not jobs:
        return {"round": round_no, "sent": 0, "ok": 0, "errors": [], "frames": []}
    log(f"  повтор {round_no}: пересдаю {len(jobs)} кадров, потоков {threads}")
    res = generate_many(jobs, threads=threads,
                        on_done=lambda r: None)

    frames: List[dict] = []
    errors: List[dict] = []
    for j, r in zip(jobs, res):
        if r is None:
            # Поток generate_many упал без результата (раньше такое роняло весь
            # круг AttributeError-ом, и остальные кандидаты не подменялись).
            r = {**j, "status": "ошибка", "error": "поток генерации упал без результата"}
        idx = int(r.get("_idx", -1))
        orig = r.get("_orig")
        cand = r.get("path")
        has_orig = bool(orig) and os.path.exists(orig)
        why = None
        if r.get("status") in ("готово", "уже есть"):
            why = _intact(cand)
        else:
            why = r.get("error") or f"статус {r.get('status')}"
        if why is None and orig:
            from pathlib import Path
            from web_queue import read_json, write_json
            promotion_path = str(cand) + ".promotion.json"
            promotion = read_json(promotion_path)
            if promotion is None:
                keep = None
                if has_orig:
                    rdir = os.path.dirname(cand)
                    keep = os.path.join(rdir, f"orig_{idx:05d}.png")
                    n = 0
                    while os.path.exists(keep):
                        n += 1
                        keep = os.path.join(rdir, f"orig_{idx:05d}_r{round_no}_{n}.png")
                    backup_image(orig, keep)
                root = Path(orig).parent.parent
                verdict = read_json(root / "judge" / "verdicts.json", {}).get(str(idx), {})
                promotion = {"original_copy": str(Path(keep).relative_to(root)) if keep else None,
                             "original_verdict": verdict}
                write_json(promotion_path, promotion)
            keep_relative = promotion.get("original_copy")
            keep = str(Path(orig).parent.parent / keep_relative) if keep_relative else None
            select_image(orig, cand)
            frames.append({"idx": idx, "status": "replaced", "why": r.get("_why", ""),
                           "original_copy": keep, "original_verdict": promotion.get("original_verdict", {}),
                           "candidate": cand})
            continue
        why = why or "задание без пути к файлу"
        if has_orig:
            frames.append({"idx": idx, "status": "kept_original",
                           "why": f"оставлен оригинал: {why[:200]}"})
        else:
            frames.append({"idx": idx, "status": "no_original_and_failed",
                           "why": why[:200]})
        errors.append({"key": r.get("key"), "error": why[:300]})

    ok = sum(1 for f in frames if f["status"] == "replaced")
    kept = sum(1 for f in frames if f["status"] == "kept_original")
    log(f"  повтор {round_no}: заменено {ok} из {len(jobs)}, оставлен оригинал {kept}, "
        f"дыр {len(frames) - ok - kept}")
    return {"round": round_no, "sent": len(jobs), "ok": ok,
            "errors": errors[:10], "frames": frames}


def original_copy(frames_dir: str, idx: int) -> str:
    """Путь к сохранённой копии оригинала кадра (последняя по времени), если есть."""
    d = os.path.join(frames_dir, REPAIR_DIR) if "REPAIR_DIR" in globals() else os.path.join(frames_dir, ".repair")
    if not os.path.isdir(d):
        return ""
    cands = [os.path.join(d, f) for f in os.listdir(d) if f.startswith(f"orig_{idx:05d}") or f.startswith(f"orig_{idx}")]
    cands = [c for c in cands if c.endswith(".png") and os.path.getsize(c) > 4096]
    return max(cands, key=os.path.getmtime) if cands else ""
