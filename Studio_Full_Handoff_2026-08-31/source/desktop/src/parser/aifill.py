"""Доливка AI-картинок для сцен, оставленных парсером под фолбэк (status=="ai").

Почему НЕ googler_worker `pipeline` и НЕ `images` (факты разведки 07.2026):
- `pipeline` перезаписывает <project>/googler/scenes.json новым планом и требует
  байт-в-байт исходные --source_file/--style/--sentences_per_prompt — их в проекте
  нет (report.json/scenes.json их не хранят); плюс count-check считает ВСЕ *.jpg
  без сверки имён и может целиком пропустить генерацию.
- `images` читает prompts.json (не scenes.json), не имеет пофайлового скипа и
  сохраняет файлы как {i+1}.jpg БЕЗ zero-padding — создаст параллельный набор имён.
- `regen_image` безопасен абсолютно: промпт берёт из СУЩЕСТВУЮЩЕГО scenes.json по
  номеру NNN.jpg, scenes.json не трогает, при ошибке старый файл не удаляется.

Поэтому доливка = по одному subprocess-вызову `regen_image` на недостающую сцену.
Медленнее батча, зато ноль риска для 40+ реальных фото парсера и плана сцен.
"""
import json
import os
import subprocess
import sys
import time

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(SRC_DIR, "googler_worker.py")
MAX_CONSEC_FAILS = 3  # подряд — стоп (системная проблема: ключ, сеть, диск)


def _detect_style(googler_dir: str) -> str:
    """Стиль исходного прогона, если сохранён (prompts.json пишет только legacy-путь)."""
    p = os.path.join(googler_dir, "prompts.json")
    try:
        return (json.load(open(p)).get("style") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def list_missing(project_dir: str) -> list:
    """Сцены под доливку: в report.json status=="ai" И файла googler/NNN.jpg нет.

    Возврат: список имён файлов ('003.jpg', ...) в порядке сцен.
    """
    rep_path = os.path.join(project_dir, "parser", "report.json")
    if not os.path.exists(rep_path):
        raise FileNotFoundError(f"Нет {rep_path} — сначала прогон parser_worker")
    report = json.load(open(rep_path))
    googler_dir = os.path.join(project_dir, "googler")
    missing = []
    for r in report.get("scenes", []):
        if r.get("status") != "ai":
            continue
        fname = f"{int(r['scene']) + 1:03d}.jpg"
        if not os.path.exists(os.path.join(googler_dir, fname)):
            missing.append(fname)
    return missing


def fill_missing(project_dir: str, log=print, dry: bool = False,
                 style: str = None, model: str = None,
                 aspect_ratio: str = "landscape",
                 throttle_sec: float = 1.0, timeout: int = 600) -> dict:
    """Догенерировать AI-картинки для недостающих сцен через regen_image.

    style: строка стиля исходного прогона (важно: от неё считается seed);
           None -> автопоиск в googler/prompts.json, иначе пустой стиль.
    dry:   только составить список, генерацию не запускать.
    Возврат: {"missing": [...], "generated": [...], "errors": [{"file","error"}]}.
    """
    project_dir = os.path.abspath(project_dir)
    googler_dir = os.path.join(project_dir, "googler")
    missing = list_missing(project_dir)
    result = {"missing": missing, "generated": [], "errors": []}
    log(f"[aifill] недостающих сцен (status=ai, файла нет): {len(missing)}"
        f"{': ' + ','.join(missing) if missing else ''}")
    if dry or not missing:
        return result

    # Сцены обязаны быть в scenes.json — regen_image берёт промпт оттуда по номеру
    scenes_path = os.path.join(googler_dir, "scenes.json")
    try:
        n_scenes = len(json.load(open(scenes_path)))
    except Exception as e:  # noqa: BLE001
        raise FileNotFoundError(f"Не читается {scenes_path}: {e}")

    if style is None:
        style = _detect_style(googler_dir)
        if style:
            log(f"[aifill] стиль из prompts.json: {style[:80]!r}")

    consec_fails = 0
    for k, fname in enumerate(missing):
        idx = int(os.path.splitext(fname)[0]) - 1
        if idx >= n_scenes:
            result["errors"].append({"file": fname,
                                     "error": f"сцены {idx} нет в scenes.json ({n_scenes})"})
            continue
        cmd = [sys.executable, WORKER, "regen_image",
               "--project_dir", project_dir, "--image_file", fname,
               "--aspect_ratio", aspect_ratio]
        if style:
            cmd += ["--style", style]
        if model:
            cmd += ["--model", model]
        log(f"[aifill] {k + 1}/{len(missing)} генерирую {fname} ...")
        try:
            proc = subprocess.run(cmd, cwd=SRC_DIR, capture_output=True,
                                  text=True, timeout=timeout)
            err_tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        except subprocess.TimeoutExpired:
            proc, err_tail = None, f"timeout {timeout}s"
        out_path = os.path.join(googler_dir, fname)
        if proc is not None and proc.returncode == 0 and os.path.exists(out_path):
            result["generated"].append(fname)
            consec_fails = 0
            log(f"[aifill] ✓ {fname}")
        else:
            result["errors"].append({"file": fname, "error": err_tail})
            consec_fails += 1
            log(f"[aifill] ✗ {fname}: {err_tail[:160]}")
            if consec_fails >= MAX_CONSEC_FAILS:
                result["errors"].append(
                    {"file": "", "error": f"стоп: {consec_fails} ошибки подряд — "
                                          "похоже на системную проблему (ключ/сеть)"})
                log("[aifill] СТОП: подряд ошибки, дальше не жгу вызовы")
                break
        if k + 1 < len(missing):
            time.sleep(throttle_sec)  # троттлинг внешнего API генерации

    log(f"[aifill] итог: сгенерировано {len(result['generated'])}/{len(missing)}, "
        f"ошибок {len(result['errors'])}")
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Доливка AI-картинок по report.json")
    ap.add_argument("--project", required=True)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--style", default=None)
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    out = fill_missing(a.project, dry=a.dry, style=a.style, model=a.model)
    print(json.dumps(out, ensure_ascii=False, indent=1))
