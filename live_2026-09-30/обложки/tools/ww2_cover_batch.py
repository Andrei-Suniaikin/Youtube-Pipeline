#!/usr/bin/env python3
"""Одноразовый resume-раннер для 22 WW2-сценариев волн 4–5.

Собирает текущий бренд-пресет + готовое обложечное семя из /Users/vova/ww2,
добавляет style-reference канала и создаёт по три варианта через Fast Gen Flow/NARWHAL.
Исходные сценарии и семена не изменяет.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cover_core import (  # noqa: E402
    Preset,
    _seed_folder_name,
    _seed_output_filename,
    compose_seed_prompt,
    process_batch,
)


DEFAULT_INPUT_ROOT = Path("/Users/vova/Downloads/WW2_тексты_без_роликов")
DEFAULT_WW2_ROOT = Path("/Users/vova/ww2")
DEFAULT_OUTPUT_ROOT = DEFAULT_INPUT_ROOT / "обложки_2026-07-19"

CHANNELS = {
    "01_Cień_PL": ("SHADOW", "Польский", "RU_don_01_gyV2coRH6pw.jpg", "RU_don_02_lKxp0REuEdM.jpg"),
    "02_Akta_PL": ("FILES", "Польский", "RU_don_28_XBOdP5WbJqQ.jpg", "RU_don_29_k3BucbqQPsI.jpg"),
    "03_Tajemnice_PL": ("SECRETS", "Польский", "RU_don_12_ayeU_BId9ZM.jpg", "RU_don_13_A-171vefbrM.jpg"),
    "04_FrontWschodni_PL": ("FRONT", "Польский", "RU_don_21_-NoQz9ts--U.jpg", "RU_don_22_oVCRXKpDL_0.jpg"),
    "05_Umbra_RO": ("SHADOW", "Румынский", "RU_don_05_nKiaJbpDB-A.jpg", "RU_don_06_RFnf_R06qEw.jpg"),
    "06_Dosarele_RO": ("FILES", "Румынский", "RU_don_32_aZZJm6c7Jp0.jpg", "RU_don_33_efPZQ2wp-yM.jpg"),
    "07_Secretele_RO": ("SECRETS", "Румынский", "RU_don_15_cU-PWDL25mM.jpg", "RU_don_16_ygglybxZ1q8.jpg"),
    "08_Frontul_RO": ("FRONT", "Румынский", "RU_don_25_X8xiNg8fvqM.jpg", "RU_don_26_ZXD6eLYGvXM.jpg"),
    "09_Stín_CS": ("SHADOW", "Чешский", "RU_don_09_EoTZBkIi3LU.jpg", "RU_don_10_izzpEMq45mw.jpg"),
    "11_Tajemství_CS": ("SECRETS", "Чешский", "RU_don_19_zv6lUqJl-JE.jpg", "RU_don_20_tTIlcbbRqGM.jpg"),
    "12_Vychodni_CS": ("FRONT", "Чешский", "RU_don_21_-NoQz9ts--U.jpg", "RU_don_22_oVCRXKpDL_0.jpg"),
}

PRESET_FILES = {
    "SHADOW": "01_ПРЕСЕТ_SHADOW.md",
    "FILES": "02_ПРЕСЕТ_FILES.md",
    "SECRETS": "03_ПРЕСЕТ_SECRETS.md",
    "FRONT": "04_ПРЕСЕТ_FRONT.md",
}

# wave, id из имени сценария, канал. Порядок = волна → канал.
ITEMS = [
    ("волна4_final", "23babb64", "01_Cień_PL"),
    ("волна4_final", "ebe693cb", "02_Akta_PL"),
    ("волна4_final", "7da382a9", "03_Tajemnice_PL"),
    ("волна4_final", "ac758d9e", "04_FrontWschodni_PL"),
    ("волна4_final", "6cc34077", "05_Umbra_RO"),
    ("волна4_final", "7b65fcb1", "06_Dosarele_RO"),
    ("волна4_final", "bf0d01fe", "07_Secretele_RO"),
    ("волна4_final", "3394c515", "08_Frontul_RO"),
    ("волна4_final", "53a59321", "09_Stín_CS"),
    ("волна4_final", "56fa4b1a", "11_Tajemství_CS"),
    ("волна4_final", "7dda9dae", "12_Vychodni_CS"),
    ("волна5", "6c166887", "01_Cień_PL"),
    ("волна5", "cea12a26", "02_Akta_PL"),
    ("волна5", "9517be81", "03_Tajemnice_PL"),
    ("волна5", "dc8b6f25", "04_FrontWschodni_PL"),
    ("волна5", "ef262d4b", "05_Umbra_RO"),
    ("волна5", "76a9f5c1", "06_Dosarele_RO"),
    ("волна5", "a9602f7a", "07_Secretele_RO"),
    ("волна5", "145c6c72", "08_Frontul_RO"),
    ("волна5", "f2e1d17c", "09_Stín_CS"),
    ("волна5", "47cc8c24", "11_Tajemství_CS"),
    ("волна5", "3a724a8e", "12_Vychodni_CS"),
]


@dataclass(frozen=True)
class BatchItem:
    wave: str
    script_id: str
    channel: str
    brand: str
    language: str
    script_path: Path
    seed_path: Path
    seed_section: int
    preset_path: Path
    reference_path: Path
    seed: str
    prompt_template: str
    final_prompt: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _first_fence(text: str, *, start: int = 0) -> str:
    fence_open = text.find("```", start)
    if fence_open < 0:
        raise ValueError("не найден открывающий ```")
    payload_start = text.find("\n", fence_open)
    if payload_start < 0:
        raise ValueError("повреждён открывающий fence")
    fence_close = text.find("```", payload_start + 1)
    if fence_close < 0:
        raise ValueError("не найден закрывающий ```")
    return text[payload_start + 1 : fence_close].strip()


def _seed_section(path: Path, number: int) -> str:
    text = _read(path)
    marker = re.compile(rf"^## {number}\.\s", re.MULTILINE)
    match = marker.search(text)
    if not match:
        raise ValueError(f"в {path} не найдена точная секция ^## {number}.")
    return _first_fence(text, start=match.start())


def _provider_safe_template(template: str) -> str:
    """Убирает конфликтующий бренд-блок и задаёт безопасную историческую рамку.

    Исходные SHADOW/FILES/SECRETS/FRONT-пресеты требуют крупной нацистской
    символики и портретов, даже когда конкретное семя прямо запрещает их. Flow
    отклоняет такие запросы safety-фильтром. Для batch-а сохраняем композицию и
    палитру бренда, но используем документальный, не-графический визуальный хук.
    """
    iconography_header = "━━ ИКОНОГРАФИЯ"
    cut_at = template.find(iconography_header)
    if cut_at >= 0:
        template = template[:cut_at].rstrip()
    safety_block = """

━━ ИСТОРИЧЕСКАЯ БЕЗОПАСНОСТЬ (ПРИОРИТЕТ) ━━
Это нейтральная документальная реконструкция истории Второй мировой войны, без
пропаганды и героизации. НЕ изображай свастики, нацистские эмблемы, экстремистские
знамёна или узнаваемые портреты политических лидеров. НЕ показывай графическое
насилие, казнь, пытки, кровь, оружие, направленное на человека, или ребёнка/подростка
в опасности. Если якорь семени конфликтует с этими ограничениями, выбери другой
безопасный хук из того же семени: исторический предмет, тайник, документ, техника,
архитектура или пейзаж; людей показывай только анонимными взрослыми и без насилия.
Сохрани факты, эпоху, язык надписи, брендовые свет, палитру и композицию.
"""
    return template + safety_block.rstrip()


def _resolve_script(input_root: Path, wave: str, script_id: str) -> Path:
    matches = [p for p in (input_root / wave).glob("*.txt") if f"[{script_id}]" in p.name]
    if len(matches) != 1:
        raise ValueError(f"ожидался 1 сценарий [{script_id}] в {wave}, найдено {len(matches)}")
    return matches[0]


def build_items(input_root: Path, ww2_root: Path) -> list[BatchItem]:
    result: list[BatchItem] = []
    for wave, script_id, channel in ITEMS:
        brand, language, wave4_ref, wave5_ref = CHANNELS[channel]
        is_wave4 = wave == "волна4_final"
        if is_wave4:
            seed_path = ww2_root / "обложки" / "_стиль" / "семена" / f"{channel}.md"
            seed_number = 19
            reference_name = wave4_ref
        else:
            seed_path = ww2_root / "пресеты_и_семена" / f"{channel}.md"
            seed_number = 20
            reference_name = wave5_ref
        preset_path = ww2_root / "обложки" / "_стиль" / "пресеты" / PRESET_FILES[brand]
        reference_path = ww2_root / "обложки" / reference_name
        script_path = _resolve_script(input_root, wave, script_id)
        for required in (seed_path, preset_path, reference_path, script_path):
            if not required.is_file():
                raise FileNotFoundError(required)
        seed = _seed_section(seed_path, seed_number)
        prompt_template = _provider_safe_template(_first_fence(_read(preset_path)))
        final_prompt = compose_seed_prompt(prompt_template, seed, language)
        result.append(BatchItem(
            wave=wave,
            script_id=script_id,
            channel=channel,
            brand=brand,
            language=language,
            script_path=script_path,
            seed_path=seed_path,
            seed_section=seed_number,
            preset_path=preset_path,
            reference_path=reference_path,
            seed=seed,
            prompt_template=prompt_template,
            final_prompt=final_prompt,
        ))
    return result


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _item_root(output_root: Path, item: BatchItem) -> Path:
    return output_root / item.wave / f"{item.channel}__{item.script_id}"


def _expected_images(output_root: Path, item: BatchItem, n_variants: int = 3) -> list[Path]:
    root = _item_root(output_root, item)
    folder = _seed_folder_name(item.seed)
    preset_name = f"WW2_{item.channel}_{item.brand}"
    lang_code = {"Польский": "pl", "Румынский": "ro", "Чешский": "cs"}[item.language]
    return [
        root / folder / _seed_output_filename(lang_code, preset_name, item.seed, variant)
        for variant in range(1, n_variants + 1)
    ]


def _materialize_inputs(output_root: Path, items: Iterable[BatchItem]) -> list[dict]:
    rows: list[dict] = []
    for item in items:
        item_root = _item_root(output_root, item)
        item_root.mkdir(parents=True, exist_ok=True)
        (item_root / "seed.txt").write_text(item.seed + "\n", encoding="utf-8")
        (item_root / "prompt.txt").write_text(item.final_prompt + "\n", encoding="utf-8")
        rows.append({
            "wave": item.wave,
            "script_id": item.script_id,
            "script": str(item.script_path),
            "channel": item.channel,
            "brand": item.brand,
            "language": item.language,
            "seed": f"{item.seed_path}#section-{item.seed_section}",
            "preset": str(item.preset_path),
            "reference_source": str(item.reference_path),
            "reference_used": False,
            "provider": "flow",
            "model": "NARWHAL",
            "n_variants": 3,
            "prompt_sha256": hashlib.sha256(item.final_prompt.encode("utf-8")).hexdigest(),
            "prompt_file": str(item_root / "prompt.txt"),
            "expected_images": [str(p) for p in _expected_images(output_root, item)],
        })
    _atomic_json(output_root / "manifest.json", rows)
    return rows


def _validate_images(paths: Iterable[Path]) -> tuple[list[dict], list[str]]:
    ok: list[dict] = []
    errors: list[str] = []
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"missing: {path}")
            continue
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                if height <= 0 or abs((width / height) - (16 / 9)) > 0.03:
                    errors.append(f"bad_aspect {width}x{height}: {path}")
                    continue
            ok.append({
                "path": str(path),
                "bytes": path.stat().st_size,
                "width": width,
                "height": height,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        except Exception as exc:
            errors.append(f"invalid {path}: {exc}")
    return ok, errors


def _select_items(items: list[BatchItem], only: list[str]) -> list[BatchItem]:
    if not only:
        return items
    needles = {value.strip() for value in only if value.strip()}
    selected = [
        item for item in items
        if item.script_id in needles or item.wave in needles or item.channel in needles
    ]
    if not selected:
        raise ValueError(f"--only не совпал ни с одним id/wave/channel: {sorted(needles)}")
    return selected


def _run_item(item: BatchItem, output_root: Path, api_key: str, n_variants: int, max_threads: int) -> bool:
    item_root = _item_root(output_root, item)
    preset = Preset(
        name=f"WW2_{item.channel}_{item.brand}",
        language=item.language,
        generation_provider="flow",
        generation_model="NARWHAL",
        aspect_ratio="16:9",
        max_generation_retries=3,
        prompt_template=item.prompt_template,
    )
    print(f"\n[{item.wave}/{item.channel}/{item.script_id}] {item.script_path.name}", flush=True)
    started = time.time()
    results = process_batch(
        [],
        preset,
        "",
        "",
        api_key,
        output_dir=str(item_root),
        # Конкурентные WW2-референсы содержат запрещённую символику; Flow блокирует
        # даже нейтральные сюжеты при таком image input. Стиль задан текстом пресета.
        refs_per_language={},
        max_threads=max_threads,
        skip_existing=True,
        seeds_per_lang={item.language: item.seed},
        n_variants=n_variants,
        log_fn=lambda message: print(message, flush=True),
    )
    expected = _expected_images(output_root, item, n_variants=n_variants)
    valid, errors = _validate_images(expected)
    state = {
        "item": {**asdict(item), "script_path": str(item.script_path), "seed_path": str(item.seed_path),
                 "preset_path": str(item.preset_path), "reference_path": str(item.reference_path)},
        "success_results": sum(1 for result in results if result.success),
        "expected": len(expected),
        "valid": valid,
        "errors": errors,
        "elapsed_seconds": round(time.time() - started, 1),
    }
    # Большие тексты уже сохранены отдельно; в state оставляем только их хэши.
    state["item"].pop("seed", None)
    state["item"].pop("prompt_template", None)
    state["item"].pop("final_prompt", None)
    _atomic_json(item_root / "state.json", state)
    if errors:
        for error in errors:
            print(f"  ERROR {error}", flush=True)
        return False
    print(f"  OK {len(valid)}/{len(expected)} · {state['elapsed_seconds']}s", flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--ww2-root", type=Path, default=DEFAULT_WW2_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="собрать manifest/prompts без API-вызовов")
    parser.add_argument("--pilot", action="store_true", help="один вариант первого выбранного сценария")
    parser.add_argument("--only", action="append", default=[], help="id, wave или channel; можно повторять")
    parser.add_argument(
        "--max-threads", type=int, default=25,
        help="общий желаемый пул; Flow использует вес 4, поэтому Fastgen сам ограничит активные запросы",
    )
    args = parser.parse_args()

    items = build_items(args.input_root, args.ww2_root)
    if len(items) != 22:
        raise RuntimeError(f"ожидалось 22 сценария, собрано {len(items)}")
    selected = _select_items(items, args.only)
    rows = _materialize_inputs(args.output_root, items)
    print(f"Dry manifest: {args.output_root / 'manifest.json'} ({len(rows)} сценария)", flush=True)
    if args.dry_run:
        return 0

    api_key = (os.getenv("COVER_FAST_GEN_KEY") or os.getenv("FAST_GEN_API_KEY") or "").strip()
    if not api_key:
        print("COVER_FAST_GEN_KEY/FAST_GEN_API_KEY не установлен", file=sys.stderr)
        return 2

    run_items = selected[:1] if args.pilot else selected
    n_variants = 1 if args.pilot else 3
    failures = 0
    total_threads = max(1, min(25, args.max_threads))
    per_item_threads = min(3, total_threads)
    item_workers = min(len(run_items), max(1, (total_threads + per_item_threads - 1) // per_item_threads))
    print(
        f"Параллельный прогон: item_workers={item_workers}, "
        f"per_item_threads={per_item_threads}, requested_pool={total_threads}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=item_workers) as executor:
        future_to_item = {
            executor.submit(
                _run_item, item, args.output_root, api_key, n_variants, per_item_threads,
            ): item
            for item in run_items
        }
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                if not future.result():
                    failures += 1
            except Exception as exc:
                failures += 1
                print(f"[{item.wave}/{item.channel}/{item.script_id}] FATAL: {exc}", flush=True)
    expected = [path for item in run_items for path in _expected_images(args.output_root, item, n_variants)]
    valid, errors = _validate_images(expected)
    hashes = [row["sha256"] for row in valid]
    duplicate_hashes = sorted({digest for digest in hashes if hashes.count(digest) > 1})
    summary = {
        "selected_items": len(run_items),
        "expected_images": len(expected),
        "valid_images": len(valid),
        "failures": failures,
        "errors": errors,
        "duplicate_sha256": duplicate_hashes,
    }
    _atomic_json(args.output_root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if failures or errors or duplicate_hashes else 0


if __name__ == "__main__":
    raise SystemExit(main())
