"""Table region detection — unified strategies for all document types.

Strategies (tried in order of confidence):
  A — DBSCAN grid clustering on rectangle centroids (high confidence)
  B — Single large rectangle + text count (medium confidence)
  C — Title text ending with "表" + offset bbox (low confidence)

For callers that already have a container bbox (e.g. PANEL_LAYOUT cabinet),
``detect_table_regions(doc, container)`` remains as a convenience wrapper.
"""

from __future__ import annotations

from typing import Optional

from ...ir import Document, BBox
from ..primitives.rectangle import detect_rectangles
from .text_utils import count_texts_in


_MIN_TABLE_W = 60.0
_MIN_TABLE_H = 50.0
_MIN_TEXTS = 4


def detect_table_regions(doc: Document, container: BBox,
                         ) -> list[BBox]:
    """Find candidate table regions inside *container* (convenience wrapper).

    Uses Strategy B (large rectangle + text count) scoped to *container*.
    This is the legacy API for PANEL_LAYOUT equipment table detection.
    """
    candidates: list[BBox] = []
    for r in detect_rectangles(doc):
        bb = r.bbox
        if not (container.x <= bb.x <= container.x + container.w and
                container.y <= bb.y <= container.y + container.h):
            continue
        if bb.w < _MIN_TABLE_W or bb.h < _MIN_TABLE_H:
            continue
        text_count = count_texts_in(doc, bb)
        if text_count >= _MIN_TEXTS:
            candidates.append(bb)
    return candidates


__all__ = ['detect_table_regions']
