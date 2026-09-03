"""Deterministic, explicitly-designed fallbacks for exact scenes with no safe archive hit."""
import hashlib
import os
import random
import textwrap


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _clean(value) -> str:
    return " ".join(str(value or "").split()).strip()


def placeholder_contract(cls: dict):
    """Return truthful card fields or None; never invent generic wartime evidence."""
    person, place = _clean(cls.get("person")), _clean(cls.get("place"))
    country, event = _clean(cls.get("country")), _clean(cls.get("event"))
    year = _clean(cls.get("year"))
    if cls.get("type") == "abstract" and cls.get("claim_level") == "metaphor":
        return {"kind": "time_transition", "title": year or "TIME SHIFT",
                "subtitle": "", "detail": event}
    if cls.get("exact_object") == "document":
        return {"kind": "document_context",
                "title": event or _clean(cls.get("show")) or "HISTORICAL DOCUMENT",
                "subtitle": " · ".join(x for x in (place or country, year) if x),
                "detail": ""}
    if not any((person, place, country, event, year)):
        return None
    show = _clean(cls.get("show")).casefold()
    identity_only = (not event or event.casefold() in {
        "nazi official", "historical figure", "identity introduction", "person"}
        or show in {person.casefold(), f"{person.casefold()} portrait"})
    if person and not identity_only:
        return {"kind": "event_context", "title": event,
                "subtitle": " · ".join(x for x in (place or country, year) if x),
                "detail": person}
    if person:
        return {"kind": "person_dossier", "title": person,
                "subtitle": " · ".join(x for x in (place or country, year) if x),
                "detail": event}
    if event:
        return {"kind": "event_context", "title": event,
                "subtitle": " · ".join(x for x in (place or country, year) if x),
                "detail": ""}
    title = place or country
    if title or year:
        kind = "place_timeline" if (place or country) else (
            "event_context" if event else "date_context")
        return {"kind": kind, "title": title or year,
                "subtitle": " · ".join(x for x in (country if place else "", year) if x),
                "detail": event if event != title else ""}
    return None


def placeholder_allowed(cls: dict, cfg: dict) -> bool:
    """Configurable last-resort gate for truthful cards."""
    if not cfg.get("enabled", True) or not placeholder_contract(cls):
        return False
    if cls.get("type") in set(cfg.get("types") or ("named", "stats")):
        return True
    if cls.get("claim_level") in set(cfg.get("claim_levels") or ("exact",)):
        return True
    return bool(cfg.get("context_with_fields", False))


def build_placeholder(cls: dict, out_path: str) -> dict:
    """Create a restrained dossier/timeline JPG and return chosen-record fields."""
    contract = placeholder_contract(cls)
    if not contract:
        return None
    from PIL import Image, ImageDraw
    width, height = 1920, 1080
    seed = int(hashlib.sha256(repr(sorted(contract.items())).encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    img = Image.new("RGB", (width, height), (34, 31, 27))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        shade = int(42 + 14 * (y / height))
        draw.line((0, y, width, y), fill=(shade, shade - 4, shade - 9))
    for _ in range(180):
        x, y = rng.randrange(width), rng.randrange(height)
        r = rng.choice((1, 1, 2, 3))
        c = rng.randrange(48, 76)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(c, c - 6, c - 13))
    accent = (178, 143, 92)
    draw.rectangle((115, 105, width - 115, height - 105), outline=(112, 94, 70), width=3)
    draw.line((175, 260, 575, 260), fill=accent, width=8)
    draw.line((175, 790, width - 175, 790), fill=(99, 82, 61), width=2)
    kind_labels = {"person_dossier": "DOSSIER", "place_timeline": "PLACE · DATE",
                   "event_context": "EVENT · CONTEXT", "date_context": "DATE · CONTEXT"}
    kind_labels["document_context"] = "DOCUMENT · CONTEXT"
    kind_labels["time_transition"] = "TIME SHIFT"
    kind = kind_labels.get(contract["kind"], "CONTEXT")
    draw.text((175, 195), kind, font=_font(32, bold=True), fill=accent)
    title = contract["title"].upper()
    title_lines = textwrap.wrap(title, width=28)[:3] or ["ARCHIVE CONTEXT"]
    y = 330
    title_font = _font(86 if len(title) < 34 else 68, bold=True)
    for line in title_lines:
        draw.text((175, y), line, font=title_font, fill=(232, 222, 204))
        y += 105
    if contract["subtitle"]:
        draw.text((180, y + 25), contract["subtitle"], font=_font(42), fill=(193, 176, 148))
        y += 82
    if contract["detail"]:
        detail = textwrap.shorten(contract["detail"], width=72, placeholder="…")
        draw.text((180, min(y + 35, 845)), detail, font=_font(34), fill=(167, 153, 132))
    if contract["kind"] == "time_transition":
        # Language-neutral clock motif: clearly a transition card, never fake archive.
        cx, cy, radius = 1480, 535, 205
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline=accent, width=9)
        for n in range(12):
            import math
            angle = math.radians(n * 30 - 90)
            x1 = cx + int((radius - 28) * math.cos(angle))
            y1 = cy + int((radius - 28) * math.sin(angle))
            x2 = cx + int((radius - 8) * math.cos(angle))
            y2 = cy + int((radius - 8) * math.sin(angle))
            draw.line((x1, y1, x2, y2), fill=(193, 176, 148), width=5)
        draw.line((cx, cy, cx - 85, cy - 105), fill=(232, 222, 204), width=12)
        draw.line((cx, cy, cx + 115, cy - 35), fill=(232, 222, 204), width=9)
        draw.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=accent)
    draw.text((175, 925), "DESIGNED CONTEXT · NOT ARCHIVAL FOOTAGE",
              font=_font(24, bold=True), fill=(126, 109, 83))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    img.save(tmp, "JPEG", quality=90)
    os.replace(tmp, out_path)
    title_attr = contract["title"] or contract["subtitle"]
    return {"uid": "placeholder-" + hashlib.sha1(title_attr.encode()).hexdigest()[:12],
            "source": "designed_placeholder", "title": title_attr,
            "page_url": "", "full_url": "", "thumb_path": out_path,
            "license": "internal", "license_raw": "Designed in VideoStudio",
            "author": "VideoStudio", "clip": 0.0, "media_type": "image",
            "placeholder_type": contract["kind"], "extra": {"synthetic": True},
            "attribution": ""}
