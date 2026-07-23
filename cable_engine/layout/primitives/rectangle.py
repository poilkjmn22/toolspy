"""cable_engine.layout.primitives.rectangle — Closed-rectangle detection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ...ir import Document, LineGeometry
from ...ir.entities import BBox, Point


@dataclass
class DetectedRect:
    bbox: BBox
    source_handles: list[str] = field(default_factory=list)
    source_type: str = 'polyline'
    layer: str = ''


def detect_rectangles(doc: Document) -> list[DetectedRect]:
    out: list[DetectedRect] = []

    for e in doc.entities:
        if not isinstance(e, LineGeometry):
            continue
        pts = list(e.points or [])
        n = len(pts)
        if n not in (4, 5):
            continue
        if n == 5:
            if abs(pts[0].x - pts[-1].x) > 0.1 or abs(pts[0].y - pts[-1].y) > 0.1:
                continue
            pts = pts[:4]
        bbox = _rect_bbox(pts)
        if bbox is None:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        out.append(DetectedRect(
            bbox=bbox,
            source_handles=[e.handle or ''],
            source_type='polyline',
            layer=cf.get('layer', '') or '',
        ))

    segs = _collect_2pt_segments(doc)
    _find_rect_from_4seg(segs, out)

    seen_bbox: set[tuple] = set()
    deduped: list[DetectedRect] = []
    for r in out:
        key = (round(r.bbox.x, 1), round(r.bbox.y, 1),
               round(r.bbox.w, 1), round(r.bbox.h, 1))
        if key not in seen_bbox:
            seen_bbox.add(key)
            deduped.append(r)
    return deduped


def _collect_2pt_segments(doc: Document) -> list[dict]:
    segs: list[dict] = []
    for e in doc.entities:
        if not isinstance(e, LineGeometry):
            continue
        pts = list(e.points or [])
        if len(pts) != 2:
            continue
        p1, p2 = pts[0], pts[-1]
        segs.append({
            'p1': (p1.x, p1.y),
            'p2': (p2.x, p2.y),
            'handle': e.handle or '',
        })
    return segs


def _find_rect_from_4seg(
    segs: list[dict], out: list[DetectedRect],
) -> None:
    _ROUND = 2

    def _r(pt: tuple[float, float]) -> tuple[float, float]:
        return (round(pt[0], _ROUND), round(pt[1], _ROUND))

    ep_to_idxs: dict[tuple[float, float], list[int]] = defaultdict(list)
    for i, seg in enumerate(segs):
        ep_to_idxs[_r(seg['p1'])].append(i)
        ep_to_idxs[_r(seg['p2'])].append(i)

    def other_key(si: int, key: tuple[float, float]) -> tuple[float, float]:
        s = segs[si]
        k1, k2 = _r(s['p1']), _r(s['p2'])
        return k2 if k1 == key else k1

    used: set[int] = set()
    for start_i in range(len(segs)):
        if start_i in used:
            continue
        p1 = _r(segs[start_i]['p1'])
        p2 = _r(segs[start_i]['p2'])
        if len(ep_to_idxs.get(p1, [])) != 2 or len(ep_to_idxs.get(p2, [])) != 2:
            continue

        chain = [start_i]
        cur_key = p2
        ok = True
        for _ in range(3):
            cands = ep_to_idxs.get(cur_key, [])
            nxt = next((c for c in cands if c not in chain), None)
            if nxt is None:
                ok = False
                break
            chain.append(nxt)
            cur_key = other_key(nxt, cur_key)
        if not ok or cur_key != p1:
            continue

        corners = [_r(segs[chain[0]]['p1'])]
        for idx in chain:
            k = _r(segs[idx]['p2'])
            if k != corners[-1]:
                corners.append(k)
        if len(corners) == 5:
            corners.pop()
        if len(corners) != 4:
            continue

        corner_pts = [Point(x, y) for x, y in corners]
        bbox = _rect_bbox(corner_pts)
        if bbox is None:
            continue

        handles = [segs[i]['handle'] for i in chain if segs[i]['handle']]
        out.append(DetectedRect(
            bbox=bbox, source_handles=handles, source_type='4seg',
        ))

        for i in chain:
            used.add(i)


def _rect_bbox(pts: list[Point]) -> Optional[BBox]:
    if len(pts) < 4:
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    w = x_max - x_min
    h = y_max - y_min
    if w < 0.5 or h < 0.5:
        return None
    tol = min(3.0, max(w, h) * 0.06)
    corners = {(x_min, y_min), (x_min, y_max), (x_max, y_min), (x_max, y_max)}
    for p in pts:
        ok = False
        for cx, cy in corners:
            if abs(p.x - cx) <= tol and abs(p.y - cy) <= tol:
                ok = True
                break
        if not ok:
            return None
    return BBox(x=x_min, y=y_min, w=w, h=h)


__all__ = [
    'DetectedRect', 'detect_rectangles',
]
