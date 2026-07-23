"""cable_engine.layout.primitives.bbox — Bounding-box arithmetic."""

from __future__ import annotations

from ...ir import (
    ArcGeometry, CircleGeometry, Document, LineGeometry,
)
from ...ir.entities import BBox


def bbox_overlap_ratio(a: BBox, b: BBox) -> float:
    ix = max(a.x, b.x)
    iy = max(a.y, b.y)
    iw = min(a.x + a.w, b.x + b.w) - ix
    ih = min(a.y + a.h, b.y + b.h) - iy
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    min_area = min(a.w * a.h, b.w * b.h)
    return inter / min_area if min_area > 0 else 0.0


def bbox_contains(outer: BBox, inner: BBox) -> bool:
    return (outer.x <= inner.x
            and outer.y <= inner.y
            and outer.x + outer.w >= inner.x + inner.w
            and outer.y + outer.h >= inner.y + inner.h)


def bbox_contains_center(outer: BBox, inner: BBox, pad: float = 5.0) -> bool:
    cx = inner.x + inner.w / 2
    cy = inner.y + inner.h / 2
    return (outer.x - pad <= cx <= outer.x + outer.w + pad and
            outer.y - pad <= cy <= outer.y + outer.h + pad)


def bbox_same(a: BBox, b: BBox, tol: float = 1.0) -> bool:
    return (abs(a.x - b.x) < tol and abs(a.y - b.y) < tol
            and abs(a.w - b.w) < tol and abs(a.h - b.h) < tol)


def drawing_extents(doc: Document) -> tuple[float, float, float, float]:
    x_min = y_min = float('inf')
    x_max = y_max = float('-inf')
    for e in doc.entities:
        if isinstance(e, LineGeometry):
            for p in (e.points or []):
                x_min = min(x_min, p.x)
                y_min = min(y_min, p.y)
                x_max = max(x_max, p.x)
                y_max = max(y_max, p.y)
        elif isinstance(e, CircleGeometry):
            c = e.center
            if c:
                r = e.radius or 0
                x_min = min(x_min, c.x - r)
                y_min = min(y_min, c.y - r)
                x_max = max(x_max, c.x + r)
                y_max = max(y_max, c.y + r)
        elif isinstance(e, ArcGeometry):
            c = e.center
            if c:
                r = e.radius or 0
                x_min = min(x_min, c.x - r)
                y_min = min(y_min, c.y - r)
                x_max = max(x_max, c.x + r)
                y_max = max(y_max, c.y + r)
    if x_min == float('inf'):
        return (0, 0, 1, 1)
    return (x_min, y_min, x_max, y_max)


__all__ = [
    'bbox_overlap_ratio', 'bbox_contains', 'bbox_contains_center',
    'bbox_same', 'drawing_extents',
]
