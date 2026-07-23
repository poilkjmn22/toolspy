"""cable_engine.layout.primitives.line — Long-line detection."""

from __future__ import annotations

from dataclasses import dataclass

from ...ir import Document, LineGeometry


@dataclass
class LongLine:
    x: float = 0.0
    y: float = 0.0
    start: float = 0.0
    end: float = 0.0
    length: float = 0.0
    is_vertical: bool = True
    handle: str = ''


def detect_long_lines(
    doc: Document, min_length: float = 50.0,
) -> tuple[list[LongLine], list[LongLine]]:
    verts: list[LongLine] = []
    hors: list[LongLine] = []

    for e in doc.entities:
        if not isinstance(e, LineGeometry):
            continue
        pts = list(e.points or [])
        if len(pts) != 2:
            continue
        x1, y1 = pts[0].x, pts[0].y
        x2, y2 = pts[1].x, pts[1].y

        if abs(x1 - x2) < 1:
            length = abs(y1 - y2)
            if length >= min_length:
                verts.append(LongLine(
                    x=x1, start=min(y1, y2), end=max(y1, y2),
                    length=length, is_vertical=True, handle=e.handle or '',
                ))
        elif abs(y1 - y2) < 1:
            length = abs(x1 - x2)
            if length >= min_length:
                hors.append(LongLine(
                    y=y1, start=min(x1, x2), end=max(x1, x2),
                    length=length, is_vertical=False, handle=e.handle or '',
                ))

    return verts, hors


__all__ = [
    'LongLine', 'detect_long_lines',
]
