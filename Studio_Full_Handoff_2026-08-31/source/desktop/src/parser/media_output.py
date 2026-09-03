"""Safe hand-off of parser-selected videos to the existing renderer contract."""
import glob
import json
import os
import shutil
import subprocess
import threading
import time


_META_LOCK = threading.RLock()

# Полнометражки archive.org: не больше 2 параллельных 300МБ-загрузок на процесс
# (archive.org режет бодрых ботов 503 «please slow down»).
FILM_DL_SEMAPHORE = threading.BoundedSemaphore(2)
_FILM_LOCKS = {}  # "<project>|<film_uid>" -> Lock: фильм качается один раз на проект


def _shared_film_source(conn, chosen: dict, project: str, max_bytes: int,
                        log=print) -> str:
    """Реюз movie-клипа из библиотеки без живого local_path: фильм качается один
    раз на проект в общий кэш кандидатов, а не 300МБ заново на каждую сцену."""
    film_uid = str(_candidate_extra(chosen).get("film_uid"))
    shared_dir = os.path.join(project, "parser", "candidates", "_movies", film_uid)
    os.makedirs(shared_dir, exist_ok=True)
    path = os.path.join(shared_dir, "source.mp4")
    with _META_LOCK:
        lock = _FILM_LOCKS.setdefault(f"{project}|{film_uid}", threading.Lock())
    with lock:
        if not os.path.exists(path):
            log(f"    реюз кинохроники: качаю фильм {film_uid} (один раз на проект)")
            with FILM_DL_SEMAPHORE:
                conn.download(chosen["full_url"], path,
                              media_type="video", max_bytes=max_bytes)
    return path


def _bin(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RuntimeError(f"{name} not found")
    return value


def probe_video(path: str) -> dict:
    """Return normalized ffprobe facts; raise for a missing/invalid video stream."""
    proc = subprocess.run([
        _bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,pix_fmt,width,height,r_frame_rate:format=duration,size",
        "-of", "json", path,
    ], capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        raise ValueError((proc.stderr or "ffprobe failed")[:300])
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("video stream missing")
    fmt, stream = data.get("format") or {}, streams[0]
    duration = float(fmt.get("duration") or 0)
    size = int(fmt.get("size") or os.path.getsize(path))
    if duration <= 0 or size < 1024:
        raise ValueError(f"invalid video duration/size: {duration}/{size}")
    return {"duration": duration, "size": size,
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "codec": stream.get("codec_name") or "",
            "pix_fmt": stream.get("pix_fmt") or "",
            "fps": stream.get("r_frame_rate") or ""}


def _candidate_extra(chosen: dict) -> dict:
    value = chosen.get("extra")
    return value if isinstance(value, dict) else {}


def _make_contact_sheet(video_path: str, out_path: str, duration: float):
    """Extract first/middle/last representative frames into one judgeable JPEG."""
    from PIL import Image, ImageOps
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    stamps = [max(0.05, duration * p) for p in (0.10, 0.50, 0.90)]
    frames = []
    for i, stamp in enumerate(stamps):
        frame = f"{out_path}.{i}.jpg"
        proc = subprocess.run([
            _bin("ffmpeg"), "-ss", f"{stamp:.3f}", "-i", video_path,
            "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", "-y", frame,
        ], capture_output=True, timeout=90)
        if proc.returncode != 0 or not os.path.exists(frame):
            raise ValueError(f"frame extraction failed at {stamp:.2f}s")
        frames.append(frame)
    opened = []
    try:
        for frame in frames:
            img = Image.open(frame).convert("RGB")
            opened.append(ImageOps.fit(img, (640, 360), method=Image.Resampling.LANCZOS))
        sheet = Image.new("RGB", (1920, 360), "black")
        for i, img in enumerate(opened):
            sheet.paste(img, (i * 640, 0))
        tmp = out_path + ".tmp"
        sheet.save(tmp, "JPEG", quality=88)
        os.replace(tmp, out_path)
    finally:
        for img in opened:
            img.close()
        for frame in frames:
            try:
                os.remove(frame)
            except OSError:
                pass


def stage_video(router, chosen: dict, project: str, scene_idx: int, log=print) -> dict:
    """Download, cap, normalize and inspect a clip, without activating it in renderer refs."""
    source_url = chosen.get("full_url")
    if not source_url:
        raise ValueError("video full_url missing")
    video_dir = os.path.join(project, "googler video")
    checks_dir = os.path.join(project, "parser", "video_checks")
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(checks_dir, exist_ok=True)
    stem = f"{scene_idx + 1:03d}_parser"
    token = f"{os.getpid()}-{threading.get_ident()}"
    raw_path = os.path.join(video_dir, f".{stem}.{token}.raw")
    pending = os.path.join(video_dir, f".{stem}.{token}.pending.mp4")
    check_path = os.path.join(checks_dir, f"{stem}.jpg")
    cfg = getattr(router, "niche", {}).get("video", {}) if hasattr(router, "niche") else {}
    max_bytes = int(float(cfg.get("max_download_mb", 120)) * 1024 * 1024)
    if str(chosen.get("source") or "") == "archive_movies":
        # полнометражная кинохроника режется на клипы — у неё свой лимит трафика
        max_bytes = int(float(cfg.get("movies_max_download_mb", 300)) * 1024 * 1024)
    max_seconds = float(cfg.get("max_clip_seconds", 18))
    extra = _candidate_extra(chosen)
    start = max(0.0, float(extra.get("start_time") or extra.get("start") or 0.0))
    end = max(0.0, float(extra.get("end_time") or extra.get("end") or 0.0))
    local_clip = str(extra.get("local_path") or "")
    conn = router._conns.get(chosen.get("source"))
    keep_raw = False
    try:
        if local_clip and os.path.exists(local_clip):
            # клип уже вырезан локально (parser.clips) — врезка по фильму не нужна;
            # C3b: коннектора может не быть (youtube_cc вне routes) — и не надо
            shutil.copyfile(local_clip, raw_path)
            start, end = 0.0, 0.0
        elif extra.get("movie_clip") and extra.get("film_uid"):
            if conn is None:
                raise ValueError(f"connector missing: {chosen.get('source')}")
            # клип из библиотеки, а mp4 остался в _movies/ другого проекта:
            # режем из общего кэша фильма, скачанного один раз на проект
            raw_path = _shared_film_source(conn, chosen, project, max_bytes, log=log)
            keep_raw = True
        else:
            if conn is None:
                raise ValueError(f"connector missing: {chosen.get('source')}")
            try:
                conn.download(source_url, raw_path, media_type="video", max_bytes=max_bytes)
            except TypeError:  # direct/legacy connector fallback
                conn.download(source_url, raw_path)
        source_probe = probe_video(raw_path)
        clip_seconds = min(max_seconds, max(0.5, source_probe["duration"] - start))
        if end > start:  # in/out тайм-коды клипа внутри длинного фильма
            clip_seconds = min(clip_seconds, max(0.5, end - start))
        if clip_seconds <= 0.5:
            raise ValueError("no usable video duration after start offset")
        cmd = [_bin("ffmpeg")]
        if start:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", raw_path, "-t", f"{clip_seconds:.3f}", "-an",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", pending]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0:
            raise ValueError((proc.stderr or b"ffmpeg normalize failed")[-500:].decode(
                "utf-8", errors="replace"))
        facts = probe_video(pending)
        _make_contact_sheet(pending, check_path, facts["duration"])
        return {"pending_path": pending, "check_path": check_path, "facts": facts,
                "final_path": os.path.join(video_dir, f"{stem}.mp4")}
    finally:
        if not keep_raw:  # общий кэш фильма живёт до конца проекта (реюз сцен)
            try:
                os.remove(raw_path)
            except OSError:
                pass


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _write_json_atomic(path: str, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _set_ref(project: str, scene_idx: int, enabled: bool):
    path = os.path.join(project, "googler_video_references.json")
    rel = f"googler/{scene_idx + 1:03d}.jpg"
    refs = _read_json(path, [])
    refs = [p for p in refs if isinstance(p, str) and p != rel]
    if enabled:
        refs.append(rel)
    _write_json_atomic(path, sorted(set(refs)))


def _update_manifest(project: str, scene_idx: int, chosen: dict = None,
                     final_path: str = "", facts: dict = None):
    path = os.path.join(project, "googler video", "stock_manifest.json")
    manifest = _read_json(path, {"version": 1, "scenes": {}})
    if not isinstance(manifest, dict):
        manifest = {"version": 1, "scenes": {}}
    scenes = manifest.setdefault("scenes", {})
    key = str(scene_idx + 1)
    if chosen is None:
        scenes.pop(key, None)
    else:
        scenes[key] = {
            "file": os.path.relpath(final_path, project).replace(os.sep, "/"),
            "source": chosen.get("source"), "page_url": chosen.get("page_url"),
            "full_url": chosen.get("full_url"), "title": chosen.get("title"),
            "license": chosen.get("license"), "license_raw": chosen.get("license_raw"),
            "author": chosen.get("author"), "media_type": "video",
            "duration": (facts or {}).get("duration"), "verified_at": time.time(),
        }
    _write_json_atomic(path, manifest)


def activate_video(project: str, scene_idx: int, chosen: dict, staged: dict):
    """Atomic final rename first; only then hide the scene image and publish provenance."""
    with _META_LOCK:
        if os.path.exists(staged["final_path"]):
            os.replace(staged["final_path"],
                       f'{staged["final_path"]}.bak.{int(time.time())}')
        os.replace(staged["pending_path"], staged["final_path"])
        probe_video(staged["final_path"])
        _set_ref(project, scene_idx, True)
        _update_manifest(project, scene_idx, chosen, staged["final_path"], staged["facts"])
    return staged["final_path"]


def discard_staged(staged: dict):
    for key in ("pending_path",):
        try:
            os.remove(staged.get(key, ""))
        except OSError:
            pass


def deactivate_parser_video(project: str, scene_idx: int):
    """Switch scene back to image/AI without destructively deleting the prior parser clip."""
    with _META_LOCK:
        prefix = os.path.join(project, "googler video", f"{scene_idx + 1:03d}_parser")
        for path in glob.glob(prefix + ".*"):
            if ".bak." in path:
                continue
            try:
                os.replace(path, f"{path}.bak.{int(time.time())}")
            except OSError:
                pass
        _set_ref(project, scene_idx, False)
        _update_manifest(project, scene_idx)


def scene_has_parser_video(project: str, scene_idx: int) -> bool:
    path = os.path.join(project, "googler video", f"{scene_idx + 1:03d}_parser.mp4")
    try:
        return os.path.getsize(path) > 1024
    except OSError:
        return False
