"""Static local review page for selected clips."""
from __future__ import annotations

import html
import os
from pathlib import Path


def _relative(asset: str, page: Path) -> str:
    return Path(os.path.relpath(Path(asset).resolve(), page.parent.resolve())).as_posix()


def write_review_html(timeline: dict, path: str | os.PathLike[str]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    for index, slot in enumerate(timeline.get("slots") or []):
        clip = slot.get("selected_clip") or {}
        sheet = clip.get("contact_sheet") or clip.get("thumbnail")
        image = (f'<img loading="lazy" src="{html.escape(_relative(sheet, target))}">' if sheet else "")
        verdict = html.escape(str(slot.get("verdict") or slot.get("status") or "?"))
        reason = html.escape(str(slot.get("reason") or ""))
        text = html.escape(str(slot.get("text") or ""))
        source = html.escape(Path(str(clip.get("source_path") or "?")).name)
        source_range = f"{float(clip.get('source_in') or 0):.2f}–{float(clip.get('source_out') or 0):.2f}"
        cards.append(f"""
        <article class="card {verdict}">
          <div class="head"><b>{index + 1:03d}</b> · {float(slot.get('start') or 0):.2f}–{float(slot.get('end') or 0):.2f}s · <span>{verdict}</span></div>
          {image}
          <p>{text}</p>
          <small>{source} · {source_range}s · {reason}</small>
        </article>""")
    document = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Semantic montage review</title><style>
body{{font-family:system-ui;background:#111;color:#eee;margin:24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(390px,1fr));gap:16px}}
.card{{background:#1d1d1d;border:2px solid #444;border-radius:10px;padding:12px}}
.card.exact,.card.relevant,.card.resolved{{border-color:#27864b}} .card.generic{{border-color:#a87b17}}
.card.wrong,.card.contradiction,.card.needs_review,.card.unresolved{{border-color:#b53c3c}}
img{{width:100%;aspect-ratio:16/9;object-fit:cover;background:#000}} p{{min-height:3.2em}} small{{color:#bbb}}
</style></head><body><h1>Semantic montage review</h1><div class="grid">{''.join(cards)}</div></body></html>"""
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(document, encoding="utf-8")
    os.replace(temp, target)
    return str(target)
