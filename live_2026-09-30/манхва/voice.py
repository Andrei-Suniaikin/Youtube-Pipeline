# -*- coding: utf-8 -*-
"""Озвучка КРУПНЫМИ блоками, а не по кадрам.

Почему блоками: на 240 кадрах поштучная озвучка дала бы 240 склеек, на каждой
обрыв интонации и вдох. Плюс сама возможность рассинхрона исчезает — тайминги
кадров считаются ПОТОМ, по словам из распознавания, а не по длине кусочков.

Движок: edge-tts (бесплатный, ключей не требует, есть на сервере).
Промышленный вариант — платный движок; интерфейс тот же.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional
from generation_receipts import atomic_json

RU_VOICE = "ru-RU-DmitryNeural"      # мужской, повествовательный
BLOCK_SECONDS = 55                    # целевая длина блока озвучки
WPM = 135.0                           # замер русской начитки

# MANGA_TTS_ENGINE: edge (по умолчанию, бесплатно), lumean (платно, app/voice_lumean.py),
# voicegen (app/voice_voicegen.py) или fastgen (подписка FastGen, app/voice_fastgen.py).
TTS_ENGINES = ("edge", "lumean", "voicegen", "fastgen")


def tts_engine() -> str:
    engine = (os.environ.get("MANGA_TTS_ENGINE") or "edge").strip().lower()
    if engine not in TTS_ENGINES:
        raise ValueError("MANGA_TTS_ENGINE: допустимо " + " или ".join(TTS_ENGINES))
    return engine


def _float_env(name: str, default: float, minimum: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name}: нужно число, а не {raw!r}") from None
    if not value >= minimum:
        raise ValueError(f"{name}: не меньше {minimum:g}")
    return value


# Предел одной попытки синтеза блока edge-tts. 17.09.2026 comm.save() висел 27 минут:
# receive_timeout edge_tts (sock_read aiohttp) действует только до перехода на websocket —
# aiohttp 3.13 при ws_connect без ws_receive сбрасывает read_timeout в None, и чтение
# сообщений ждёт вечно. Поэтому предел ставится снаружи: базовые секунды + множитель на
# ожидаемую длину речи блока. Замер 17.09 с Мака: 84 с речи синтезируются за 33 с,
# то есть предел 60 + 2×84 = 228 с оставляет запас в семь раз.
EDGE_TIMEOUT_BASE = _float_env("MANGA_EDGE_TIMEOUT_BASE", 60.0, 1.0)
EDGE_TIMEOUT_FACTOR = _float_env("MANGA_EDGE_TIMEOUT_FACTOR", 2.0, 0.0)
RETRY_DELAYS = (5, 15, 45)
tts_engine()   # неверное значение останавливает запуск службы с понятной причиной


def expected_seconds(text: str) -> float:
    """Ожидаемая длина речи блока по темпу студии (для пределов, а не для тайминга)."""
    return len(text.split()) / (WPM / 60.0)


def edge_attempt_timeout(text: str) -> float:
    return EDGE_TIMEOUT_BASE + EDGE_TIMEOUT_FACTOR * expected_seconds(text)


@dataclass
class Block:
    idx: int
    start_char: int
    end_char: int
    text: str
    path: str = ""
    words_path: str = ""   # block_NNNN.words.jsonl — метки слов от синтезатора
    seconds: float = 0.0


def split_blocks(src: str, beats, block_seconds: int = BLOCK_SECONDS,
                 max_chars: Optional[int] = None, wpm: float = WPM) -> List[Block]:
    """Режем на блоки ПО ГРАНИЦАМ БИТОВ и по концу предложения.

    Границы битов уже совпадают с грамматическими швами, поэтому блок никогда
    не обрывается посреди фразы.

    max_chars — предел знаков блока у платного движка (заказ = блок). Если следующий
    бит не влезает, блок закрывается на границе бита, даже не в конце предложения.
    Бит длиннее предела — ошибка ДО любого заказа, а не обрезка текста.
    """
    wps = wpm / 60.0                      # темп языка проекта (languages.py); русский — 135
    budget = block_seconds * wps          # слов на блок
    blocks: List[Block] = []
    cur_from = 0
    cur_words = 0
    for i, bt in enumerate(beats):
        if max_chars is not None:
            if len(src[bt.start:bt.end].strip()) > max_chars:
                raise ValueError(f"бит {i + 1} длиннее предела озвучки: "
                                 f"{len(src[bt.start:bt.end].strip())} знаков при пределе {max_chars}")
            if cur_from < i and len(src[beats[cur_from].start:bt.end].strip()) > max_chars:
                a, b = beats[cur_from].start, beats[i - 1].end
                blocks.append(Block(idx=len(blocks) + 1, start_char=a, end_char=b,
                                    text=src[a:b].strip()))
                cur_from = i
                cur_words = 0
        cur_words += bt.words
        ends_sentence = bool(re.search(r"[.!?…]\s*$", src[bt.start:bt.end]))
        last = (i == len(beats) - 1)
        if (cur_words >= budget and ends_sentence) or last:
            a = beats[cur_from].start
            b = bt.end
            blocks.append(Block(idx=len(blocks) + 1, start_char=a, end_char=b,
                                text=src[a:b].strip()))
            cur_from = i + 1
            cur_words = 0
    return blocks


# Слово-токен: буквы И цифры (те же правила, что в align_native.TOKEN), регистр
# не важен. Имена героев и числа синтезатор отдаёт в метках как в тексте.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def words_path_for(mp3_path: str) -> str:
    """block_0001.mp3 -> block_0001.words.jsonl (лежит рядом с mp3)."""
    return re.sub(r"\.mp3$", "", mp3_path) + ".words.jsonl"


def words_missing(text: str, words_path: str) -> Optional[str]:
    """Проверка полноты озвучки по меткам синтезатора, а не по длительности.

    Почему: порог по длительности (0.4 от ожидаемой) пропускает ЧАСТИЧНЫЙ обрубок —
    блок, где синтез оборвался на второй половине текста. Метки WordBoundary
    перечисляют каждое произнесённое слово, поэтому множество токенов из jsonl
    обязано покрыть множество токенов текста. Возвращает описание проблемы или
    None, если всё на месте.
    """
    if not os.path.exists(words_path) or os.path.getsize(words_path) == 0:
        return "нет меток слов (пустой jsonl)"
    said = set()
    with open(words_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("type", "WordBoundary") != "WordBoundary":
                continue
            said.update(t.casefold() for t in _TOKEN.findall(ev.get("text") or ""))
    want = [t.casefold() for t in _TOKEN.findall(text)]
    lost = [t for t in dict.fromkeys(want) if t not in said]
    if lost:
        return (f"меток слов {len(said)}, в тексте {len(want)} токенов, "
                f"не озвучено {len(lost)}: {' '.join(lost[:6])}…")
    return None


def voice_fingerprint(text, voice, rate):
    return hashlib.sha256(json.dumps([text, voice, rate], ensure_ascii=False).encode()).hexdigest()


def cached_block_ok(block, voice=RU_VOICE, rate="+0%"):
    """Only reuse complete audio with matching text/voice; adopt old default-voice caches."""
    state_path = block.path + ".voice.json"
    expected = voice_fingerprint(block.text, voice, rate)
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if state.get("fingerprint") != expected or state.get("status") != "complete":
            return False
    elif voice != RU_VOICE or rate != "+0%":
        return False
    if not (os.path.exists(block.path) and os.path.getsize(block.path) > 2048):
        return False
    if duration(block.path) < 0.4 * (len(block.text.split()) / 2.5):
        return False
    try:
        return words_missing(block.text, block.words_path) is None
    except (OSError, ValueError, TypeError):
        return False


def recover_saved_block(block, voice, rate):
    """Finish the two-file commit if synthesis completed before the worker stopped."""
    state_path = block.path + ".voice.json"
    try:
        state = json.load(open(state_path, encoding="utf-8"))
    except (OSError, ValueError):
        return
    if state.get("fingerprint") != voice_fingerprint(block.text, voice, rate) or state.get("status") != "saved":
        return
    tmp_audio = block.path + ".tmp.mp3"
    tmp_words = tmp_audio + ".words.jsonl"
    candidate = tmp_audio if os.path.exists(tmp_audio) else block.path
    candidate_words = tmp_words if os.path.exists(tmp_words) else block.words_path
    probe = Block(block.idx, block.start_char, block.end_char, block.text,
                  candidate, candidate_words)
    if not os.path.exists(candidate) or os.path.getsize(candidate) <= 2048:
        return
    try:
        with open(candidate, "rb") as f:
            audio_sha = hashlib.sha256(f.read()).hexdigest()
        with open(candidate_words, "rb") as f:
            words_sha = hashlib.sha256(f.read()).hexdigest()
        complete = (audio_sha == state.get("audio_sha256") and words_sha == state.get("words_sha256")
                    and duration(candidate) >= 0.4 * (len(block.text.split()) / 2.5)
                    and words_missing(block.text, candidate_words) is None)
    except (OSError, ValueError, TypeError):
        return
    if complete:
        if candidate == tmp_audio:
            os.replace(tmp_audio, block.path)
        if candidate_words == tmp_words:
            os.replace(tmp_words, block.words_path)
        atomic_json(state_path, {"fingerprint": state["fingerprint"], "status": "complete"})


def _tmp_paths(out_path: str):
    tmp = out_path + ".tmp.mp3"
    return tmp, tmp + ".words.jsonl"


def discard_partial(out_path: str) -> None:
    """Убрать недописанные файлы прерванной попытки: они никогда не принимаются за блок."""
    for path in _tmp_paths(out_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


async def _tts_one(text: str, out_path: str, voice: str, rate: str = "+0%"):
    import edge_tts
    tmp, meta = _tmp_paths(out_path)
    # boundary="WordBoundary": синтезатор сам отдаёт время каждого слова (100-нс
    # тики) — по ним align_native ставит границы кадров без whisper. Whisper на
    # 493 кадрах опаздывал на 0.3–1.8 с в 225 кадрах (не узнаёт имена героев).
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    await comm.save(tmp, meta)
    if not os.path.exists(meta) or os.path.getsize(meta) == 0:
        # Пустые метки = блок не принят, как и обрубок по длительности.
        raise RuntimeError("синтезатор не вернул метки слов (пустой jsonl)")
    # Saved audio+metadata can be recovered even between the two renames or
    # before the caller's validation; an older voice cannot masquerade as new.
    with open(tmp, "rb") as f:
        audio_sha = hashlib.sha256(f.read()).hexdigest()
    with open(meta, "rb") as f:
        words_sha = hashlib.sha256(f.read()).hexdigest()
    atomic_json(out_path + ".voice.json", {"fingerprint": voice_fingerprint(text, voice, rate),
                "status": "saved", "audio_sha256": audio_sha, "words_sha256": words_sha})
    os.replace(tmp, out_path)
    os.replace(meta, words_path_for(out_path))


def synth(blocks: List[Block], out_dir: str, voice: str = RU_VOICE,
          rate: str = "+0%", concurrency: int = 4, log=print) -> List[Block]:
    os.makedirs(out_dir, exist_ok=True)

    async def runner():
        sem = asyncio.Semaphore(concurrency)

        async def one(b: Block):
            b.path = os.path.join(out_dir, f"block_{b.idx:04d}.mp3")
            b.words_path = words_path_for(b.path)
            recover_saved_block(b, voice, rate)
            state = {"fingerprint": voice_fingerprint(b.text, voice, rate), "status": "complete"}
            if cached_block_ok(b, voice, rate):
                atomic_json(b.path + ".voice.json", state)
                return
            async with sem:
                # Окно повторов шире (5/15/45 с): edge-tts отдаёт обрубки и пустые
                # метки сериями, короткие паузы 2/4 с попадали в ту же серию.
                delays = RETRY_DELAYS
                limit = edge_attempt_timeout(b.text)
                for attempt in range(1, len(delays) + 2):
                    try:
                        atomic_json(b.path + ".voice.json", {**state, "status": "synthesizing"})
                        try:
                            # После comm.save() в _tts_one нет await: отмена по пределу
                            # приходит только во время синтеза, до записи "saved".
                            await asyncio.wait_for(_tts_one(b.text, b.path, voice, rate), limit)
                        except asyncio.TimeoutError:
                            discard_partial(b.path)
                            if attempt > len(delays):
                                raise RuntimeError(
                                    f"синтез не уложился в {limit:.0f} с в {attempt} попытках подряд — "
                                    f"озвучка остановлена, повторите позже") from None
                            log(f"  блок {b.idx}: синтез не уложился в {limit:.0f} с — повтор через "
                                f"{delays[attempt - 1]} с (попытка {attempt + 1} из {len(delays) + 1})")
                            await asyncio.sleep(delays[attempt - 1])
                            continue
                        if duration(b.path) < 0.4 * (len(b.text.split()) / 2.5):
                            raise RuntimeError(f"обрубок: {duration(b.path):.1f}s на "
                                               f"{len(b.text.split())} слов")
                        lost = words_missing(b.text, b.words_path)
                        if lost:
                            raise RuntimeError(f"неполная озвучка: {lost}")
                        atomic_json(b.path + ".voice.json", state)
                        return
                    except Exception as e:
                        if attempt > len(delays):
                            raise RuntimeError(f"блок {b.idx}: {type(e).__name__}: {e}") from e
                        await asyncio.sleep(delays[attempt - 1])

        await asyncio.gather(*(one(b) for b in blocks))

    asyncio.run(runner())
    for b in blocks:
        b.seconds = duration(b.path)
        log(f"  блок {b.idx:3d}: {b.seconds:6.1f}s  {len(b.text.split()):4d} слов")
    return blocks


def duration(path: str) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    try:
        return float((p.stdout or "0").strip())
    except ValueError:
        return 0.0


def concat(blocks: List[Block], out_path: str) -> float:
    """Склейка блоков в одну дорожку БЕЗ пережатия."""
    lst = out_path + ".list"
    with open(lst, "w", encoding="utf-8") as f:
        for b in blocks:
            f.write(f"file '{os.path.abspath(b.path)}'\n")
    tmp = out_path + ".tmp.mp3"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", tmp],
                       capture_output=True, text=True)
    os.unlink(lst)
    if p.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError(f"склейка звука не удалась: {(p.stderr or '')[:300]}")
    os.replace(tmp, out_path)
    return duration(out_path)
