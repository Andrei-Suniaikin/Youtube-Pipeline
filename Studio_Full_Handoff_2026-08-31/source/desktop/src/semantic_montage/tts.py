"""Edge TTS narration generation with durable word timings.

The module deliberately uses Edge ``WordBoundary`` events instead of running an
ASR pass over audio we just generated.  Long scripts are split at sentence/word
boundaries, rendered independently, and concatenated losslessly with FFmpeg.
Every timestamp is shifted onto the final continuous audio timeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_VOICE = "ru-RU-DmitryNeural"
DEFAULT_RATE = "+0%"
DEFAULT_MAX_CHUNK_CHARS = 4_000
EDGE_TICKS_PER_SECOND = 10_000_000
WORDS_SCHEMA_VERSION = 1


class TTSGenerationError(RuntimeError):
    """Raised when narration or its timing manifest cannot be produced safely."""


@dataclass(frozen=True)
class TextChunk:
    """A non-empty slice of the original script."""

    text: str
    start: int
    end: int


@dataclass(frozen=True)
class TTSResult:
    audio_path: Path
    words_path: Path
    request_hash: str
    audio_duration: float
    words: tuple[dict[str, Any], ...]
    cached: bool


def tts_request_hash(text: str, voice: str, rate: str) -> str:
    """Stable resume key.  Only the three user-visible synthesis inputs matter."""

    payload = json.dumps(
        {"rate": rate, "text": text, "voice": voice},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def split_text_for_edge(
    text: str, *, max_chars: int = DEFAULT_MAX_CHUNK_CHARS
) -> list[TextChunk]:
    """Split a long script without dropping or reordering any non-space text.

    Sentence punctuation is preferred, then a regular word boundary, and only a
    single overlong token is hard-cut.  The returned offsets always reference the
    exact original string, which lets us retain exact punctuation in ``words.json``.
    """

    if max_chars < 32:
        raise ValueError("max_chars must be at least 32")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")

    chunks: list[TextChunk] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break

        limit = min(cursor + max_chars, length)
        if limit < length:
            window = text[cursor:limit]
            minimum_preferred = max(1, int(max_chars * 0.45))
            sentence_cuts = [
                match.end()
                for match in re.finditer(r"[.!?…][\"'»)\]]*", window)
                if match.end() >= minimum_preferred
            ]
            if sentence_cuts:
                end = cursor + sentence_cuts[-1]
            else:
                whitespace = [
                    match.start()
                    for match in re.finditer(r"\s+", window)
                    if match.start() >= minimum_preferred
                ]
                end = cursor + (whitespace[-1] if whitespace else len(window))
        else:
            end = length

        while end > cursor and text[end - 1].isspace():
            end -= 1
        if end <= cursor:
            end = min(cursor + max_chars, length)
        chunks.append(TextChunk(text=text[cursor:end], start=cursor, end=end))
        cursor = end

    if not chunks:
        raise ValueError("text must not be empty")
    return chunks


def _default_words_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(".words.json")


def _normalise_token(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum())


def _script_tokens(text: str) -> list[tuple[str, int, int]]:
    # ``\w`` is Unicode-aware in Python and therefore covers Cyrillic too.
    pattern = re.compile(r"[^\W_]+(?:[\-'\u2019\u2011\u2013\u2014][^\W_]+)*", re.UNICODE)
    return [(match.group(0), match.start(), match.end()) for match in pattern.finditer(text)]


def _attach_script_positions(
    raw_words: Sequence[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    """Attach best-effort exact source spans to Edge's spoken word events."""

    tokens = _script_tokens(text)
    token_cursor = 0
    attached: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(raw_words):
        spoken = str(raw.get("spoken_text") or raw.get("text") or "")
        needle = _normalise_token(spoken)
        matched_index: int | None = None
        if needle:
            # A small look-ahead tolerates an occasional service tokenisation
            # difference without letting later words jump arbitrarily in the script.
            for index in range(token_cursor, min(len(tokens), token_cursor + 12)):
                if _normalise_token(tokens[index][0]) == needle:
                    matched_index = index
                    break

        word = dict(raw)
        word["index"] = ordinal
        if matched_index is not None:
            source_text, char_start, char_end = tokens[matched_index]
            word.update(
                {
                    "text": source_text,
                    "char_start": char_start,
                    "char_end": char_end,
                    "source_match": True,
                }
            )
            token_cursor = matched_index + 1
        else:
            word.update(
                {
                    "text": spoken,
                    "char_start": None,
                    "char_end": None,
                    "source_match": False,
                }
            )
        attached.append(word)

    for index, word in enumerate(attached):
        char_end = word.get("char_end")
        if not isinstance(char_end, int):
            word["punctuation_after"] = ""
            continue
        next_start = len(text)
        for following in attached[index + 1 :]:
            candidate = following.get("char_start")
            if isinstance(candidate, int):
                next_start = candidate
                break
        between = text[char_end:next_start]
        word["punctuation_after"] = "".join(
            char for char in between if not char.isspace()
        )
    return attached


def _parse_word_boundary(event: Mapping[str, Any], base_seconds: float) -> dict[str, Any]:
    try:
        offset_ticks = int(event["offset"])
        duration_ticks = int(event["duration"])
        spoken_text = str(event["text"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TTSGenerationError(f"invalid Edge WordBoundary event: {event!r}") from exc
    if offset_ticks < 0 or duration_ticks < 0 or not spoken_text:
        raise TTSGenerationError(f"invalid Edge WordBoundary values: {event!r}")

    base_ticks = round(base_seconds * EDGE_TICKS_PER_SECOND)
    global_offset = base_ticks + offset_ticks
    start = global_offset / EDGE_TICKS_PER_SECOND
    end = (global_offset + duration_ticks) / EDGE_TICKS_PER_SECOND
    return {
        "spoken_text": spoken_text,
        "offset_ticks": global_offset,
        "duration_ticks": duration_ticks,
        "start": round(start, 7),
        "end": round(end, 7),
        "confidence": 1.0,
    }


def probe_audio_duration(path: str | os.PathLike[str], *, ffprobe: str = "ffprobe") -> float:
    """Return container duration in seconds using the project's FFmpeg dependency."""

    process = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            os.fspath(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if process.returncode != 0:
        detail = process.stderr.strip()[-500:]
        raise TTSGenerationError(f"ffprobe could not read generated audio: {detail}")
    try:
        duration = float(process.stdout.strip())
    except ValueError as exc:
        raise TTSGenerationError("ffprobe returned an invalid audio duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise TTSGenerationError("generated audio duration must be positive")
    return duration


def _ffconcat_quote(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def concatenate_mp3_parts(
    parts: Sequence[Path], output_path: Path, *, ffmpeg: str = "ffmpeg"
) -> None:
    """Losslessly concatenate MP3 chunks into one atomic staging file."""

    if not parts:
        raise TTSGenerationError("no Edge audio chunks were generated")
    if len(parts) == 1:
        shutil.copyfile(parts[0], output_path)
        return

    list_path = output_path.with_suffix(output_path.suffix + ".ffconcat")
    list_path.write_text(
        "ffconcat version 1.0\n"
        + "".join(f"file {_ffconcat_quote(part)}\n" for part in parts),
        encoding="utf-8",
    )
    try:
        process = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                os.fspath(list_path),
                "-c",
                "copy",
                "-y",
                os.fspath(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if process.returncode != 0:
            raise TTSGenerationError(
                f"ffmpeg could not concatenate Edge chunks: {process.stderr.strip()[-800:]}"
            )
    finally:
        list_path.unlink(missing_ok=True)


def _new_staging_path(target: Path, suffix: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=suffix, dir=target.parent
    )
    os.close(descriptor)
    return Path(name)


def _write_json_staging(target: Path, payload: Mapping[str, Any]) -> Path:
    staging = _new_staging_path(target, ".tmp.json")
    try:
        with staging.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return staging
    except Exception:
        staging.unlink(missing_ok=True)
        raise


def _read_cached_result(
    audio_path: Path, words_path: Path, request_hash: str
) -> TTSResult | None:
    if not audio_path.is_file() or not words_path.is_file():
        return None
    try:
        payload = json.loads(words_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != WORDS_SCHEMA_VERSION:
            return None
        if payload.get("request_hash") != request_hash:
            return None
        if audio_path.stat().st_size != int(payload.get("audio_size", -1)):
            return None
        duration = float(payload["audio_duration"])
        words = payload["words"]
        if duration <= 0 or not isinstance(words, list) or not words:
            return None
        previous = -1.0
        for word in words:
            start = float(word["start"])
            end = float(word["end"])
            if start < previous - 1e-7 or end < start:
                return None
            previous = start
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return TTSResult(
        audio_path=audio_path,
        words_path=words_path,
        request_hash=request_hash,
        audio_duration=duration,
        words=tuple(words),
        cached=True,
    )


async def synthesize_edge_tts(
    text: str,
    audio_path: str | os.PathLike[str],
    words_path: str | os.PathLike[str] | None = None,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    force: bool = False,
    enforce_script_quality: bool = True,
    communicate_factory: Callable[..., Any] | None = None,
    duration_probe: Callable[[Path], float] | None = None,
    audio_concatenator: Callable[[Sequence[Path], Path], None] | None = None,
) -> TTSResult:
    """Generate narration and an exact Edge WordBoundary timing document.

    Dependency injection hooks are intentionally narrow and make the complete
    state/resume behaviour testable without network access.
    """

    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")
    if enforce_script_quality:
        # Fail before cache lookup or any provider call: an old audio file must
        # never make a newly detected raw ASR transcript look acceptable.
        from .script_qa import require_script_quality

        require_script_quality(text)
    if not isinstance(voice, str) or not voice.strip():
        raise ValueError("voice must not be empty")
    if not re.fullmatch(r"[+-]\d+%", rate):
        raise ValueError("rate must look like '+0%' or '-10%'")

    final_audio = Path(audio_path).expanduser().resolve()
    final_words = (
        Path(words_path).expanduser().resolve()
        if words_path is not None
        else _default_words_path(final_audio)
    )
    request_hash = tts_request_hash(text, voice, rate)
    if not force:
        cached = _read_cached_result(final_audio, final_words, request_hash)
        if cached is not None:
            return cached

    edge_version = "injected"
    if communicate_factory is None:
        try:
            import edge_tts
        except ImportError as exc:
            raise TTSGenerationError(
                "edge-tts 7.2.7 is required: python -m pip install edge-tts==7.2.7"
            ) from exc
        communicate_factory = edge_tts.Communicate
        edge_version = str(getattr(edge_tts, "__version__", "unknown"))

    duration_probe = duration_probe or probe_audio_duration
    audio_concatenator = audio_concatenator or concatenate_mp3_parts
    chunks = split_text_for_edge(text, max_chars=max_chunk_chars)
    final_audio.parent.mkdir(parents=True, exist_ok=True)
    final_words.parent.mkdir(parents=True, exist_ok=True)

    audio_staging = _new_staging_path(final_audio, ".tmp.mp3")
    words_staging: Path | None = None
    raw_words: list[dict[str, Any]] = []
    audio_hasher = hashlib.sha256()
    try:
        with tempfile.TemporaryDirectory(
            prefix=".edge-tts-parts-", dir=final_audio.parent
        ) as temporary_directory:
            temp_dir = Path(temporary_directory)
            parts: list[Path] = []
            cumulative_seconds = 0.0

            for chunk_index, chunk in enumerate(chunks):
                part_path = temp_dir / f"part-{chunk_index:04d}.mp3"
                communicator = communicate_factory(
                    text=chunk.text,
                    voice=voice,
                    rate=rate,
                    boundary="WordBoundary",
                )
                local_word_count = 0
                with part_path.open("wb") as part_handle:
                    async for event in communicator.stream():
                        event_type = str(event.get("type", ""))
                        if event_type == "audio":
                            data = event.get("data")
                            if not isinstance(data, (bytes, bytearray)) or not data:
                                raise TTSGenerationError("Edge returned an empty audio event")
                            part_handle.write(data)
                        elif event_type == "WordBoundary":
                            word = _parse_word_boundary(event, cumulative_seconds)
                            word["chunk_index"] = chunk_index
                            raw_words.append(word)
                            local_word_count += 1
                    part_handle.flush()
                    os.fsync(part_handle.fileno())

                if not part_path.is_file() or part_path.stat().st_size <= 0:
                    raise TTSGenerationError(f"Edge returned no audio for chunk {chunk_index}")
                if local_word_count == 0:
                    raise TTSGenerationError(
                        f"Edge returned no WordBoundary events for chunk {chunk_index}"
                    )
                part_duration = float(duration_probe(part_path))
                if not math.isfinite(part_duration) or part_duration <= 0:
                    raise TTSGenerationError(
                        f"invalid audio duration for Edge chunk {chunk_index}"
                    )
                parts.append(part_path)
                cumulative_seconds += part_duration

            audio_concatenator(parts, audio_staging)
            if not audio_staging.is_file() or audio_staging.stat().st_size <= 0:
                raise TTSGenerationError("final Edge narration is empty")
            audio_duration = float(duration_probe(audio_staging))

        words = _attach_script_positions(raw_words, text)
        previous_start = -1.0
        for word in words:
            start = float(word["start"])
            end = float(word["end"])
            if start < previous_start - 1e-7 or end < start:
                raise TTSGenerationError("Edge WordBoundary events are not monotonic")
            previous_start = start

        with audio_staging.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                audio_hasher.update(block)
        payload: dict[str, Any] = {
            "schema_version": WORDS_SCHEMA_VERSION,
            "provider": "edge-tts",
            "provider_version": edge_version,
            "request_hash": request_hash,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": text,
            "voice": voice,
            "rate": rate,
            "audio_duration": round(audio_duration, 7),
            "audio_size": audio_staging.stat().st_size,
            "audio_sha256": audio_hasher.hexdigest(),
            "chunk_count": len(chunks),
            "max_chunk_chars": max_chunk_chars,
            "matched_source_words": sum(bool(word["source_match"]) for word in words),
            "word_count": len(words),
            "words": words,
        }
        words_staging = _write_json_staging(final_words, payload)

        # No cross-file atomic primitive exists.  Replacing media first is safe:
        # an interrupted run leaves a hash-mismatched manifest and therefore resumes
        # by regenerating, never by accepting a half-committed pair.
        os.replace(audio_staging, final_audio)
        os.replace(words_staging, final_words)
        words_staging = None
        return TTSResult(
            audio_path=final_audio,
            words_path=final_words,
            request_hash=request_hash,
            audio_duration=audio_duration,
            words=tuple(words),
            cached=False,
        )
    except BaseException:
        # Cancellation/KeyboardInterrupt must leave the same clean state as a
        # provider failure; otherwise a stopped long narration leaks staging
        # files and makes resume diagnostics ambiguous.
        audio_staging.unlink(missing_ok=True)
        if words_staging is not None:
            words_staging.unlink(missing_ok=True)
        raise


def synthesize_edge_tts_sync(*args: Any, **kwargs: Any) -> TTSResult:
    """Synchronous CLI-friendly wrapper around :func:`synthesize_edge_tts`."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(synthesize_edge_tts(*args, **kwargs))
    raise RuntimeError(
        "synthesize_edge_tts_sync cannot run inside an active event loop; "
        "await synthesize_edge_tts(...) instead"
    )


def load_words_document(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and minimally validate a durable ``words.json`` document."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != WORDS_SCHEMA_VERSION:
        raise ValueError("unsupported words.json schema_version")
    if not isinstance(payload.get("words"), list) or not payload["words"]:
        raise ValueError("words.json contains no words")
    return payload


__all__ = [
    "DEFAULT_MAX_CHUNK_CHARS",
    "DEFAULT_RATE",
    "DEFAULT_VOICE",
    "EDGE_TICKS_PER_SECOND",
    "TTSGenerationError",
    "TTSResult",
    "TextChunk",
    "concatenate_mp3_parts",
    "load_words_document",
    "probe_audio_duration",
    "split_text_for_edge",
    "synthesize_edge_tts",
    "synthesize_edge_tts_sync",
    "tts_request_hash",
]
