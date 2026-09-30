"""Compile verified TTS WordBoundary records into frame cuts, with no fallback.

Text/span correspondence can be proved here. Acoustic accuracy cannot: provider
offsets and the sum of real MP3 durations still require independent validation.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import unicodedata

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)  # Keep letters AND digits.


class NativeAlignmentError(ValueError):
    pass


def tokens(text):
    return [{"text": m.group(), "normal": unicodedata.normalize("NFC", m.group()).casefold(),
             "start": m.start(), "end": m.end()} for m in TOKEN.finditer(text)]


def normalized(text):
    return [token["normal"] for token in tokens(text)]


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise NativeAlignmentError(f"{label}: expected a finite number")
    return float(value)


def span(value, source, label):
    a,b=value.get("start_char"),value.get("end_char")
    if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(source):
        raise NativeAlignmentError(f"{label}: invalid source span")
    return a,b


def frame_spans(storyboard, source):
    beats = {b["idx"]:b for b in storyboard["beats"]}
    if len(beats)!=len(storyboard["beats"]):
        raise NativeAlignmentError("Duplicate beat IDs")
    result=[]
    for frame in storyboard["frames"]:
        try:
            start=beats[frame["beat_from"]]["start"]
            end=beats[frame["beat_to"]]["end"]
        except KeyError as exc:
            raise NativeAlignmentError("Frame references a missing source beat") from exc
        if normalized(frame.get("text", "")) != normalized(source[start:end]):
            raise NativeAlignmentError(f"Frame {frame['idx']}: text differs from source beat span")
        result.append({"idx":frame["idx"],"start_char":start,"end_char":end,"text":frame["text"]})
    return result


def compile_boundaries(source, frames, blocks, total_seconds=None, codec_tolerance=0.12):
    """Pure compiler. Each block requires measured seconds and metadata records.

    A metadata event may contain multiple source tokens. A cut at its first token
    is supported; a cut inside that event is rejected because no onset is given.
    """
    source_tokens=tokens(source)
    if not source_tokens or not frames or not blocks:
        raise NativeAlignmentError("Source, frames and blocks must all be nonempty")
    token_cursor=0
    mapped=[]
    block_rows=[]
    audio_offset=0.0
    last_char_end=0
    block_ids=set()
    for block in blocks:
        idx=block.get("idx")
        if idx in block_ids:
            raise NativeAlignmentError("Duplicate block ID")
        block_ids.add(idx)
        start,end=span(block,source,f"Block {idx}")
        if start<last_char_end:
            raise NativeAlignmentError("Blocks overlap or are out of source order")
        last_char_end=end
        if "text" in block and normalized(block["text"])!=normalized(source[start:end]):
            raise NativeAlignmentError(f"Block {idx}: supplied text differs from source span")
        seconds=number(block.get("seconds"),f"Block {idx} duration")
        if seconds<=0:
            raise NativeAlignmentError("Audio blocks must have positive measured duration")
        expected=[]
        first_token_index=token_cursor
        while token_cursor<len(source_tokens) and source_tokens[token_cursor]["start"]<end:
            token=source_tokens[token_cursor]
            if token["start"]<start or token["end"]>end:
                raise NativeAlignmentError("Block source spans skip or split a source token")
            expected.append(token)
            token_cursor+=1
        metadata=block.get("metadata")
        if not expected or not isinstance(metadata,list) or not metadata:
            raise NativeAlignmentError(f"Block {idx}: missing WordBoundary metadata or empty source")
        observed=[]
        previous_end=-1.0
        event_count=0
        for event_index,event in enumerate(metadata):
            if event.get("type","WordBoundary")!="WordBoundary":
                continue
            event_tokens=tokens(event.get("text", ""))
            if not event_tokens:
                # Произносимый символ без букв/цифр («№», «$», «%» отдельно): edge-tts
                # отдаёт на него своё событие, а в тексте ему не соответствует ни один
                # токен. Замер 10.09 на VPS: «Пункт №4.» -> события «№» и «4», и
                # компилятор ронял весь ролик в whisper. Пропускаем — порядок слов
                # ниже всё равно сверяется строго.
                continue
            onset=number(event.get("offset"),"WordBoundary offset")/1e7
            event_duration=number(event.get("duration"),"WordBoundary duration")/1e7
            event_end=onset+event_duration
            if onset<0 or event_duration<=0 or onset<previous_end-0.001 or event_end>seconds+codec_tolerance:
                raise NativeAlignmentError(f"Block {idx}: WordBoundary timings overlap, are nonpositive, or exceed audio")
            previous_end=event_end
            event_count+=1
            for inside,token in enumerate(event_tokens):
                observed.append(token["normal"])
                mapped.append({"block_idx":idx,"block_start_seconds":audio_offset,"event_index":event_index,
                    "event_token_index":inside,"event_text":event["text"],"provider_offset_seconds":onset,
                    "global_provider_onset_seconds":audio_offset+onset,"global_provider_end_seconds":audio_offset+event_end})
        if observed != [token["normal"] for token in expected]:
            raise NativeAlignmentError(f"Block {idx}: metadata tokens differ from source; no fuzzy fallback")
        block_rows.append({"idx":idx,"source_span":[start,end],"first_source_token":first_token_index,
            "source_tokens":len(expected),"word_events":event_count,"audio_offset_seconds":round(audio_offset,6),
            "audio_seconds":seconds})
        audio_offset+=seconds
    if token_cursor!=len(source_tokens):
        raise NativeAlignmentError("Blocks do not cover all source tokens")
    total=audio_offset if total_seconds is None else number(total_seconds,"Concatenated audio duration")
    if total<=0 or abs(total-audio_offset)>codec_tolerance:
        raise NativeAlignmentError("Concatenated duration disagrees with measured block sum")
    boundary_rows=[]
    frame_ids=set()
    source_cursor=0
    last_char_end=0
    for ordinal,frame in enumerate(frames):
        idx=frame.get("idx")
        if idx in frame_ids:
            raise NativeAlignmentError("Duplicate frame ID")
        frame_ids.add(idx)
        start,end=span(frame,source,f"Frame {idx}")
        if start<last_char_end:
            raise NativeAlignmentError("Frames overlap or are out of source order")
        last_char_end=end
        first=source_cursor
        while source_cursor<len(source_tokens) and source_tokens[source_cursor]["start"]<end:
            token=source_tokens[source_cursor]
            if token["start"]<start or token["end"]>end:
                raise NativeAlignmentError("Frame source spans skip or split a source token")
            source_cursor+=1
        if source_cursor==first:
            raise NativeAlignmentError("A frame has no source word to anchor")
        if "text" in frame and normalized(frame["text"]) != [t["normal"] for t in source_tokens[first:source_cursor]]:
            raise NativeAlignmentError("Frame text differs from its verified source tokens")
        anchor=mapped[first]
        if anchor["event_token_index"]!=0:
            raise NativeAlignmentError(f"Frame {idx}: first word is inside a multiword metadata event; onset unavailable")
        onset=anchor["global_provider_onset_seconds"]
        cut=0.0 if ordinal==0 else onset
        boundary_rows.append({"frame_id":idx,"source_span":[start,end],"first_source_token_index":first,
            "first_source_word":source_tokens[first]["text"],"first_source_word_span":[source_tokens[first]["start"],source_tokens[first]["end"]],
            "cut_seconds":round(cut,6),"first_cut_forced_zero":ordinal==0,**anchor})
    if source_cursor!=len(source_tokens):
        raise NativeAlignmentError("Frames do not cover all source tokens")
    cuts=[row["cut_seconds"] for row in boundary_rows]+[round(total,6)]
    if any(right<=left for left,right in zip(cuts,cuts[1:])):
        raise NativeAlignmentError("Cuts must be strictly monotonic and every frame nonzero")
    pairs=[[left,right] for left,right in zip(cuts,cuts[1:])]
    return {"source":"tts_word_boundary_verified_text","total":round(total,6),"pairs":pairs,
        "boundaries":boundary_rows,"blocks":block_rows,
        "coverage":{"frames":len(frames),"source_tokens":len(source_tokens),"verified_first_word_events":len(frames),
                    "forced_zero_first_cut":1,"interpolated":0,"inherited":0,"unknown_source_mapping":0},
        "check":{"ok":True,"scope":"text_span_and_timeline_only","monotonic":True,"nonzero":True,"end_matches_audio":True,
                 "min":min(b-a for a,b in pairs),"max":max(b-a for a,b in pairs)},
        "acoustic_alignment":{"status":"not_proven_by_text_or_provider_metadata",
            "statement":"Source correspondence is exact under the declared tokenization. Provider onsets, codec delay and block-offset accumulation require independent audio/ASR checks.",
            "total_duration_basis":"measured_concatenated_audio" if total_seconds is not None else "sum_of_measured_blocks",
            "concatenated_minus_block_sum_seconds":total-audio_offset}}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def probe_duration(path):
    result=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",str(path)],
                          check=True,text=True,capture_output=True)
    return number(float(result.stdout.strip()),"ffprobe duration")


def compile_files(source_path,storyboard_path,blocks_path,voice_path=None):
    source_path,storyboard_path,blocks_path=map(Path,(source_path,storyboard_path,blocks_path))
    source=source_path.read_text(encoding="utf-8")  # utf-8 явно: на VPS локаль может быть POSIX
    storyboard=json.loads(storyboard_path.read_text(encoding="utf-8"))
    records=json.loads(blocks_path.read_text(encoding="utf-8"))
    if isinstance(records,dict):
        records=records.get("blocks")
    if not isinstance(records,list):
        raise NativeAlignmentError("Block manifest must contain a block list")
    prepared=[]
    evidence=[]
    for block in records:
        audio_path=Path(block["path"])
        raw_metadata=block.get("word_metadata_path") or block.get("words_metadata_path")
        if not raw_metadata:
            raise NativeAlignmentError(f"Block {block.get('idx')}: no metadata path")
        metadata_path=Path(raw_metadata)
        if not audio_path.is_file() or not metadata_path.is_file():
            raise NativeAlignmentError("Block audio or metadata is missing")
        metadata=[json.loads(line) for line in metadata_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        measured=probe_duration(audio_path)
        prepared.append({**block,"seconds":measured,"metadata":metadata})
        evidence.append({"idx":block["idx"],"audio_path":str(audio_path.resolve()),"audio_sha256":sha256(audio_path),
            "audio_seconds_measured":measured,"metadata_path":str(metadata_path.resolve()),"metadata_sha256":sha256(metadata_path)})
    total=probe_duration(voice_path) if voice_path is not None else None
    result=compile_boundaries(source,frame_spans(storyboard,source),prepared,total)
    result["provenance"]={"compiler_sha256":sha256(Path(__file__)),"source_path":str(source_path.resolve()),
        "source_sha256":sha256(source_path),"storyboard_path":str(storyboard_path.resolve()),"storyboard_sha256":sha256(storyboard_path),
        "blocks_manifest_path":str(blocks_path.resolve()),"blocks_manifest_sha256":sha256(blocks_path),"blocks":evidence,
        "voice_path":str(Path(voice_path).resolve()) if voice_path else None,
        "voice_sha256":sha256(voice_path) if voice_path else None}
    return result
