"""Table region detection for PANEL_LAYOUT.

Detects rectangular areas that are likely equipment tables based on
text density and layout geometry.
"""

from __future__ import annotations

from ...ir import Document, AttributeEntity, TextEntity, BBox
from ..primitives.rectangle import detect_rectangles


def detect_table_regions(doc: Document, container: BBox,
                         ) -> list[BBox]:
    """Find candidate table regions inside *container*.

    Strategy:
      1. Look for large rectangles (the outer table border).
      2. Accept rects that are ≥ 60u wide and ≥ 80u tall
         (large enough for a multi-row table).
      3. Verify that the rect contains at least 4 text entities
         (enough for a minimal 2×2 grid).
    """
    candidates: list[BBox] = []
    for r in detect_rectangles(doc):
        bb = r.bbox
        if not (container.x <= bb.x <= container.x + container.w and
                container.y <= bb.y <= container.y + container.h):
            continue
        if bb.w < 60 or bb.h < 80:
            continue
        text_count = _count_texts_in(doc, bb)
        if text_count >= 4:
            candidates.append(bb)
    return candidates


def _count_texts_in(doc: Document, bbox: BBox) -> int:
    count = 0
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if bbox.x <= ex <= bbox.x + bbox.w and bbox.y <= ey <= bbox.y + bbox.h:
            count += 1
    return count


__all__ = ['detect_table_regions']
