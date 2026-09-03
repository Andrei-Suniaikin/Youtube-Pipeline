"""Нарезка длинной кинохроники (archive.org movies) на короткие клипы.

split_video(): границы сцен через PySceneDetect (ContentDetector); если пакет
не установлен/упал — равномерная нарезка по UNIFORM_LEN секунд. Каждый выбранный
сегмент нормализуется ffmpeg тем же профилем, что stage_video (h264/yuv420p/
cfr30, faststart, без звука) — рендер получает одинаковые клипы из обоих путей.

contact_sheet()/mid_frame(): кадры для видео-суда и CLIP-ранжирования.
"""
import os
import subprocess

from .media_output import _bin, _make_contact_sheet, probe_video

UNIFORM_LEN = 8.0             # шаг равномерного fallback без scenedetect
EDGE_SKIP_FRAC = 0.04         # у длинных фильмов не берём края: титры/лидер
MIN_FILM_FOR_EDGE_SKIP = 120  # секунды; короткие ролики не режем по краям


def _detect_scenes(path: str, log=print):
    """-> [(start_sec, end_sec)] по ContentDetector | None = детектор недоступен."""
    try:
        from scenedetect import ContentDetector, detect
    except Exception as e:  # noqa: BLE001 — нет пакета/битая установка
        log(f"[clips] scenedetect недоступен ({e}) — равномерная нарезка")
        return None
    try:
        scenes = detect(path, ContentDetector())
    except Exception as e:  # noqa: BLE001 — кривой файл не роняет прогон
        log(f"[clips] scenedetect упал ({e}) — равномерная нарезка")
        return None
    out = [(float(s.get_seconds()), float(e.get_seconds())) for s, e in scenes]
    return out or None


def _plan_segments(duration: float, scenes, min_len: float = 3.0,
                   max_len: float = 15.0, max_clips: int = None) -> list:
    """Чистая логика отбора сегментов: fallback-сетка, клампы длин, срез титров,
    равномерная выборка до max_clips. -> [(start, end)]."""
    if scenes is None:
        scenes, t = [], 0.0
        while t < duration:
            scenes.append((t, min(t + UNIFORM_LEN, duration)))
            t += UNIFORM_LEN
    lo = duration * EDGE_SKIP_FRAC if duration >= MIN_FILM_FOR_EDGE_SKIP else 0.0
    hi = duration - lo
    segs = []
    for start, end in scenes:
        start, end = max(0.0, float(start)), min(float(end), duration)
        if start < lo or start > hi - min_len:
            continue  # титры в начале / хвост фильма
        end = min(end, hi)
        if end - start < min_len:
            continue
        if end - start > max_len:
            end = start + max_len
        segs.append((round(start, 3), round(end, 3)))
    if max_clips and max_clips > 0 and len(segs) > max_clips:
        # равномерно по таймлайну, а не первые N — хроника меняет сюжет по ходу
        if max_clips == 1:
            segs = [segs[len(segs) // 2]]
        else:
            step = (len(segs) - 1) / (max_clips - 1)
            idxs = sorted({round(i * step) for i in range(max_clips)})
            segs = [segs[i] for i in idxs]
    return segs


def _normalize(src: str, dst: str, start: float, seconds: float, log=print) -> bool:
    """Вырезка+нормализация одного клипа: тот же профиль, что stage_video."""
    cmd = [_bin("ffmpeg"), "-ss", f"{start:.3f}", "-i", src,
           "-t", f"{seconds:.3f}", "-an",
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", dst]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        log(f"[clips] ffmpeg не вырезал {os.path.basename(dst)}: "
            f"{(proc.stderr or b'')[-200:].decode('utf-8', errors='replace')}")
        return False
    return True


def split_video(path: str, out_dir: str, min_len: float = 3.0, max_len: float = 15.0,
                max_clips: int = None, log=print) -> list:
    """Режет видео на нормализованные клипы. -> [{path, start, end, duration}].

    Пустой список = из файла ничего не вырезалось (битый/слишком короткий)."""
    facts = probe_video(path)
    duration = facts["duration"]
    segs = _plan_segments(duration, _detect_scenes(path, log=log),
                          min_len=min_len, max_len=max_len, max_clips=max_clips)
    os.makedirs(out_dir, exist_ok=True)
    out = []
    for n, (start, end) in enumerate(segs, 1):
        clip_path = os.path.join(out_dir, f"clip_{n:03d}.mp4")
        if not _normalize(path, clip_path, start, end - start, log=log):
            continue
        try:
            clip_facts = probe_video(clip_path)
        except (ValueError, OSError, RuntimeError):
            try:
                os.remove(clip_path)
            except OSError:
                pass
            continue
        out.append({"path": clip_path, "start": start, "end": end,
                    "duration": clip_facts["duration"]})
    log(f"[clips] {os.path.basename(path)}: {len(out)} клипов "
        f"из {len(segs)} сегментов ({duration:.0f}s исходника)")
    return out


def contact_sheet(clip_path: str, out_path: str = None) -> str:
    """First/middle/last кадры клипа в один JPEG (реюз функции видео-пути)."""
    out_path = out_path or f"{clip_path}.sheet.jpg"
    _make_contact_sheet(clip_path, out_path, probe_video(clip_path)["duration"])
    return out_path


def mid_frame(clip_path: str, out_path: str = None):
    """Средний кадр клипа как JPEG (для CLIP-ранжирования). None = не извлёкся."""
    out_path = out_path or f"{clip_path}.mid.jpg"
    if os.path.exists(out_path):
        return out_path
    try:
        stamp = max(0.05, probe_video(clip_path)["duration"] * 0.5)
    except (ValueError, OSError, RuntimeError):
        return None
    proc = subprocess.run([
        _bin("ffmpeg"), "-ss", f"{stamp:.3f}", "-i", clip_path,
        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", "-y", out_path,
    ], capture_output=True, timeout=90)
    if proc.returncode != 0 or not os.path.exists(out_path):
        return None
    return out_path
