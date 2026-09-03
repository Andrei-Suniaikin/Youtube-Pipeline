"""Small shared helpers with no provider or model dependencies."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
from pathlib import Path
from typing import Any


def interval_frame_span(start: float, end: float, fps: float) -> tuple[int, int, int]:
    """Map an absolute timeline interval to a telescoping CFR frame span.

    Rounding every interval duration independently accumulates drift at
    fractional frame rates.  Rounding the two absolute boundaries guarantees
    that adjacent spans share one frame boundary and that their frame counts
    sum to ``round(final_end * fps)``.
    """

    try:
        start_value = float(start)
        end_value = float(end)
        fps_value = float(fps)
    except (TypeError, ValueError) as exc:
        raise ValueError("frame interval values must be numeric") from exc
    if (
        not math.isfinite(start_value)
        or not math.isfinite(end_value)
        or not math.isfinite(fps_value)
        or fps_value <= 0
        or end_value <= start_value
    ):
        raise ValueError("frame interval must be finite, ordered and use positive fps")
    start_frame = int(round(start_value * fps_value))
    end_frame = int(round(end_value * fps_value))
    if end_frame <= start_frame:
        raise ValueError("frame interval is shorter than one output frame")
    return start_frame, end_frame, end_frame - start_frame


def canonical_hash(value: Any) -> str:
    """Return a deterministic SHA-256 for a JSON-compatible value."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, target)


def probe_media(path: str | os.PathLike[str]) -> dict[str, Any]:
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or "ffprobe failed")[-1200:])
    return json.loads(process.stdout or "{}")


def media_duration(path: str | os.PathLike[str]) -> float:
    return float((probe_media(path).get("format") or {}).get("duration") or 0.0)
