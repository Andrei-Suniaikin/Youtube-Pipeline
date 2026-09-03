# -*- coding: utf-8 -*-
"""
Миграция presets v2 (Round 1 of Generation refactor, 2026-05-23).

Idempotent: при повторном запуске проверяет наличие старых полей в первом preset →
если их нет, миграция уже применена, выход без изменений.

Маппинг старой → новой schema:
  name                     → name (без изменений)
  total_words              → target_length_words
  prompt_template +
   custom_instructions     → склеить в `content` через "\n\n"
  content_type             → prepend в content: "Type: {value}\n\n..." (не теряем данные)
  num_chapters             → drop (dead, worker вычисляет сам)
  words_per_chapter        → drop (dead)
  created_at, updated_at   → drop (dead)
  (отсутствовал)           → id = slugify(name)
  (отсутствовал)           → created_at = float ts (новый формат — unix timestamp)
  (отсутствовал)           → last_used_at = None

Backup: presets/generation.json.bak.preMigrate.<ts> перед записью.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict


def _slugify(text: str, max_len: int = 40) -> str:
    """Простой slug: транслит через NFKD, lowercase, замена не-alnum на дефис."""
    text = unicodedata.normalize("NFKD", text)
    # Cyrillic transliteration (минимальный набор для имён пресетов)
    cyr_map = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
        "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    text = text.lower().translate(cyr_map)
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:max_len] or "preset"
    return slug


def migrate_presets_v2(presets_file: str) -> Dict[str, Any]:
    """
    Применяет миграцию presets v2.

    Returns: {migrated: int, skipped: int (idempotent no-op), message: str, backup: str|None}
    """
    p = Path(presets_file)
    if not p.is_file():
        return {"migrated": 0, "skipped": 0, "message": "[migrate presets v2] no file, skip", "backup": None}

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"migrated": 0, "skipped": 0, "message": f"[migrate presets v2] parse error: {e}", "backup": None}

    presets = data.get("presets", {}) if isinstance(data, dict) else {}
    if not isinstance(presets, dict) or not presets:
        return {"migrated": 0, "skipped": 0, "message": "[migrate presets v2] empty presets, skip", "backup": None}

    # Idempotency check: первый preset не имеет старого поля prompt_template
    # И имеет новое поле content/id → миграция уже применена.
    sample = next(iter(presets.values()), {})
    has_old_field = isinstance(sample, dict) and ("prompt_template" in sample or "custom_instructions" in sample)
    has_new_field = isinstance(sample, dict) and "content" in sample and "id" in sample
    if has_new_field and not has_old_field:
        return {"migrated": 0, "skipped": len(presets),
                "message": f"[migrate presets v2] already migrated ({len(presets)} presets, no-op)",
                "backup": None}

    # Backup
    bak = str(p) + f".bak.preMigrate.{int(time.time())}"
    p.replace(bak) if False else None  # don't move, just write atomic; backup via cp
    with open(bak, "w", encoding="utf-8") as bf:
        json.dump(data, bf, ensure_ascii=False, indent=2)

    # Migrate each preset
    now = time.time()
    migrated_presets: Dict[str, Dict[str, Any]] = {}
    used_ids: set = set()
    n = 0
    for old_name, old in presets.items():
        if not isinstance(old, dict):
            continue
        name = str(old.get("name", old_name) or old_name)
        # id: slugify(name) с защитой от коллизий
        base_id = _slugify(name)
        pid = base_id
        suffix = 1
        while pid in used_ids:
            suffix += 1
            pid = f"{base_id}-{suffix}"
        used_ids.add(pid)

        # content = склейка prompt_template + custom_instructions с content_type prefix
        parts = []
        ct = str(old.get("content_type") or "").strip()
        if ct:
            parts.append(f"Type: {ct}")
        pt = str(old.get("prompt_template") or "").strip()
        if pt:
            parts.append(pt)
        ci = str(old.get("custom_instructions") or "").strip()
        if ci:
            parts.append(ci)
        content = "\n\n".join(parts) if parts else "(empty preset)"

        target_words = old.get("total_words")
        try:
            target_words = int(target_words) if target_words is not None else None
        except Exception:
            target_words = None

        migrated_presets[name] = {
            "id": pid,
            "name": name,
            "target_length_words": target_words,
            "content": content,
            "created_at": now,
            "last_used_at": None,
        }
        n += 1

    # Atomic write
    new_data = {"presets": migrated_presets, "updated_at": now}
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(p))

    return {
        "migrated": n,
        "skipped": 0,
        "message": f"[migrate presets v2] migrated {n} presets (ids: {sorted(used_ids)})",
        "backup": bak,
    }


if __name__ == "__main__":
    presets_file = os.environ.get(
        "PRESETS_FILE",
        "/root/rewrite-server/presets/generation.json"
    )
    result = migrate_presets_v2(presets_file)
    print(result["message"])
    if result.get("backup"):
        print(f"  backup: {result['backup']}")
