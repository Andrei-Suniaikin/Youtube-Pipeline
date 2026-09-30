#!/usr/bin/env python3
"""Дозаполняет safety-пропуски WW2 batch прямыми нейтральными сценами."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cover_core import _last_fastgen_error, generate_image_v6  # noqa: E402
from tools.ww2_cover_batch import (  # noqa: E402
    DEFAULT_INPUT_ROOT,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_WW2_ROOT,
    _atomic_json,
    _expected_images,
    _validate_images,
    build_items,
)


# Позитивные предметные сцены: без ссылок на чувствительных персон/эпизоды.
# Текст остаётся конкретным хуком исходного сценария и на языке канала.
SAFE_SCENES = {
    "23babb64": (
        "3 LEŚNE SKRYTKI",
        "a snow-covered forest excavation, a rusted sealed canister beside an old field radio and folded map, an adult historian examining the find",
    ),
    "ebe693cb": (
        "2000 BOCHENKÓW",
        "a 1940s bakery storeroom with long rows of bread loaves, a sealed archival folder and an adult investigator under a single lamp",
    ),
    "7da382a9": (
        "DZIEŃ PO DNIU",
        "a worn handwritten diary open on a wooden medical desk, an adult doctor writing by an oil lamp in a quiet period clinic",
    ),
    "ac758d9e": (
        "60 LUDZI BEZ ŚLADU",
        "a vintage submarine cutting through a dark Baltic sea, an unfolded navigation chart and compass dominating the foreground",
    ),
    "6cc34077": (
        "DOSARELE CARE AU SCHIMBAT TOT",
        "steel archive cases and coded folders in a late-1940s intelligence office, an adult analyst studying a large wall map",
    ),
    "7b65fcb1": (
        "70 DE ZILE ÎN RADĂ",
        "an old passenger ship waiting offshore at dusk, viewed from a quiet harbor desk with a calendar and route chart",
    ),
    "bf0d01fe": (
        "MICROFOANE ÎN PEREȚI",
        "an elegant English manor room where a vintage hidden microphone is revealed inside the wall, an adult technician checking cables",
    ),
    "3394c515": (
        "DOUĂ SEMNĂTURI",
        "two signed archival orders under a desk lamp beside a detailed map of two Romanian villages, adult historian hands in frame",
    ),
    "53a59321": (
        "250 000 DOLARŮ",
        "a newly opened castle vault containing a medieval reliquary, an adult curator illuminating it with a handheld lamp",
    ),
    "56fa4b1a": (
        "95 JMEN V ARCHIVU",
        "a long archive table holding ninety-five carefully labeled record cards beside a Czech town map, an adult historian reviewing them",
    ),
    "7dda9dae": (
        "23 000 LETŮ",
        "a plywood biplane on a moonlit grass airfield, an adult woman pilot standing calmly beside the wing with a flight log",
    ),
    "6c166887": (
        "15 BRAKUJĄCYCH SKRZYŃ",
        "a vast monastery storage hall filled with numbered wooden crates, an adult scholar holding a rediscovered handwritten music score",
    ),
    "cea12a26": (
        "70 LAT PÓŹNIEJ",
        "a modern courtroom archive table with old accounting ledgers, a sealed evidence box and an adult clerk arranging documents",
    ),
    "9517be81": (
        "50 KOBIET I BATUTA",
        "an empty concert stage with a conductor baton, violin and handwritten sheet music in a dramatic warm spotlight",
    ),
    "dc8b6f25": (
        "OSTATNI SAMOLOT",
        "a small vintage liaison airplane lifting above a snow-covered European city at dawn, rooftops and a folded city map below",
    ),
    "ef262d4b": (
        "425 DE PAGINI",
        "a missing leather journal rediscovered on an archive desk, hundreds of pages fanned beside numbered storage drawers and an adult researcher",
    ),
    "76a9f5c1": (
        "1 000 000 ÎN REGISTRE",
        "a huge archival map of the Rhine covered with nineteen precise location marks, an open population ledger and an adult cartographer",
    ),
    "a9602f7a": (
        "10 000 S-AU ÎNTORS",
        "a 1950s railway platform filled with stacked suitcases and a long arrival register, adult travelers seen as distant silhouettes",
    ),
    "145c6c72": (
        "66 000 ÎN REGISTRE",
        "a snowbound Tatra mountain pass with a column of abandoned vintage trucks, a field map and large numbered ledger in the foreground",
    ),
    "f2e1d17c": (
        "TŘI RODNÉ LISTY",
        "three archival birth certificates beside three tiny knitted caps on a wooden table, warm window light and a railway timetable",
    ),
    "47cc8c24": (
        "3 792 POHLEDNIC",
        "thousands of dated postcards arranged across an archive table beside a canvas mailbag, clock and adult archivist hands",
    ),
    "3a724a8e": (
        "1,4 MILIONU V MAPĚ",
        "a large historical map of a Danish coastline covered with precise red location markers, measuring tools and an adult surveyor",
    ),
}

LAYOUTS = {
    1: "close cinematic composition, main artifact large on the left, headline on the right",
    2: "wide environmental composition, strong depth and leading lines, headline on the left",
    3: "dramatic slightly elevated composition, central artifact and headline across the lower third",
}


def _prompt(item, variant: int) -> str:
    headline, scene = SAFE_SCENES[item.script_id]
    language = {"Польский": "Polish", "Румынский": "Romanian", "Чешский": "Czech"}[item.language]
    return f"""Create one premium cinematic historical-documentary YouTube thumbnail in 16:9.
Show exactly one calm, artifact-centered scene: {scene}.
Use {LAYOUTS[variant]}. Cold steel-blue palette, one warm practical light, subtle red accent,
realistic period materials, deep contrast, fine film grain, expensive documentary look, clean hierarchy.
Add exactly this large bold uppercase {language} headline: \"{headline}\".
The headline must be perfectly readable at small size, with thick high-contrast lettering.
Do not add any other words, labels, logos or watermarks."""


def _generate_one(item, path: Path, api_key: str, variant: int) -> tuple[Path, bool, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size:
        return path, True, "exists"
    base_prompt = _prompt(item, variant)
    seed_base = int(hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8], 16)
    last_error = "unknown"
    for attempt in range(1, 4):
        prompt = base_prompt + f"\nVisual variation {attempt}: keep the same factual hook and change only camera angle and lighting."
        image = generate_image_v6(
            prompt,
            api_key,
            operation="nano_banana_2_image_generate",
            aspect_ratio="16:9",
            # Fastgen принимает seed только в signed 32-bit диапазоне.
            seed=(seed_base + attempt) % (2**31 - 1),
            max_retries=1,
        )
        if image is not None:
            tmp = path.with_name(path.name + ".tmp.jpg")
            image.convert("RGB").save(tmp, format="JPEG", quality=95, subsampling=0)
            os.replace(tmp, path)
            return path, True, f"generated attempt={attempt}"
        last_error = str(getattr(_last_fastgen_error, "value", None) or "generation failed")
    return path, False, last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--ww2-root", type=Path, default=DEFAULT_WW2_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--threads", type=int, default=25)
    args = parser.parse_args()

    api_key = (os.getenv("COVER_FAST_GEN_KEY") or os.getenv("FAST_GEN_API_KEY") or "").strip()
    if not api_key:
        print("COVER_FAST_GEN_KEY/FAST_GEN_API_KEY не установлен", file=sys.stderr)
        return 2

    items = build_items(args.input_root, args.ww2_root)
    tasks = []
    all_expected = []
    for item in items:
        expected = _expected_images(args.output_root, item)
        all_expected.extend(expected)
        for variant, path in enumerate(expected, start=1):
            if not path.is_file() or path.stat().st_size == 0:
                tasks.append((item, path, variant))
    print(f"Safety retry: missing={len(tasks)}, threads={max(1, min(25, args.threads))}", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(25, args.threads))) as executor:
        futures = {
            executor.submit(_generate_one, item, path, api_key, variant): (item, path, variant)
            for item, path, variant in tasks
        }
        for future in as_completed(futures):
            item, path, variant = futures[future]
            try:
                result_path, ok, detail = future.result()
            except Exception as exc:
                result_path, ok, detail = path, False, str(exc)
            results.append({
                "script_id": item.script_id,
                "variant": variant,
                "path": str(result_path),
                "ok": ok,
                "detail": detail,
            })
            print(f"[{item.script_id} v{variant}] {'OK' if ok else 'FAIL'} {detail}", flush=True)

    valid, errors = _validate_images(all_expected)
    hashes = [row["sha256"] for row in valid]
    duplicates = sorted({digest for digest in hashes if hashes.count(digest) > 1})
    summary = {
        "requested_retries": len(tasks),
        "expected_images": len(all_expected),
        "valid_images": len(valid),
        "errors": errors,
        "duplicate_sha256": duplicates,
        "retry_results": sorted(results, key=lambda row: (row["script_id"], row["variant"])),
    }
    _atomic_json(args.output_root / "retry_summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "retry_results"}, ensure_ascii=False), flush=True)
    return 1 if errors or duplicates else 0


if __name__ == "__main__":
    raise SystemExit(main())
