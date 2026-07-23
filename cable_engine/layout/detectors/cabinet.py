"""cable_engine.layout.detectors.cabinet — Cabinet detection."""

from __future__ import annotations

from typing import Optional

from ...ir import AttributeEntity, Document, TextEntity
from ...ir.entities import BBox
from ..primitives.bbox import bbox_overlap_ratio, drawing_extents
from ..primitives.rectangle import DetectedRect
from ..primitives.line import LongLine
from ..model import LayoutNode, LayoutNodeType


_CABINET_KW = ('柜', '屏', '箱', '继电器', '控制', '保护')


def detect_cabinets(
    doc: Document, rects: list[DetectedRect],
    verts: list[LongLine], hors: list[LongLine],
) -> list[LayoutNode]:
    dx_min, dy_min, dx_max, dy_max = drawing_extents(doc)
    dw = dx_max - dx_min
    dh = dy_max - dy_min
    total_area = dw * dh

    candidates: list[LayoutNode] = []

    for r in rects:
        bb = r.bbox
        if bb.w < 30 or bb.h < 100:
            continue
        ratio = bb.h / bb.w
        if ratio < 1.5 or ratio > 5.0:
            continue
        if bb.w * bb.h < 5000:
            continue
        if total_area > 0 and (bb.w * bb.h) >= total_area * 0.9:
            continue
        name = _find_cabinet_name_at(doc, bb)
        candidates.append(LayoutNode(
            id='', node_type=LayoutNodeType.CABINET,
            bbox=bb, name=name,
            data={'source': 'rectangle', 'layer': r.layer},
        ))

    verts_sorted = sorted(verts, key=lambda v: v.x)
    for i, vl in enumerate(verts_sorted):
        for vr in verts_sorted[i + 1:]:
            width = vr.x - vl.x
            if width < 100:
                continue
            y_overlap = min(vl.end, vr.end) - max(vl.start, vr.start)
            if y_overlap < 100:
                continue
            bbox = BBox(x=vl.x, y=max(vl.start, vr.start),
                        w=width, h=y_overlap)
            if (y_overlap / width) < 1.5:
                continue
            name = _find_cabinet_name_at(doc, bbox)
            candidates.append(LayoutNode(
                id='', node_type=LayoutNodeType.CABINET,
                bbox=bbox, name=name,
                data={'source': 'paired_verticals'},
            ))

    cabinets = _merge_cabinet_candidates(candidates)

    rect_cabs = [c for c in cabinets if c.data.get('source') == 'rectangle']
    if rect_cabs:
        filtered: list[LayoutNode] = list(rect_cabs)
        for c in cabinets:
            if c.data.get('source') != 'rectangle':
                covered = False
                for rc in rect_cabs:
                    if bbox_overlap_ratio(c.bbox, rc.bbox) > 0.5:
                        covered = True
                        break
                if not covered:
                    filtered.append(c)
        cabinets = filtered

    for i, cab in enumerate(cabinets):
        cab.id = f'cab_{i}'
    return cabinets


def _merge_cabinet_candidates(candidates: list[LayoutNode]) -> list[LayoutNode]:
    if not candidates:
        return []

    sorted_c = sorted(candidates, key=lambda c: c.bbox.w * c.bbox.h, reverse=True)
    groups: list[list[LayoutNode]] = []
    for c in sorted_c:
        placed = False
        for g in groups:
            anchor = g[0]
            if bbox_overlap_ratio(c.bbox, anchor.bbox) > 0.3:
                g.append(c)
                placed = True
                break
        if not placed:
            groups.append([c])

    out: list[LayoutNode] = []
    for g in groups:
        rect_based = [c for c in g if c.data.get('source') == 'rectangle']
        if rect_based:
            best = max(rect_based, key=lambda c: c.bbox.w)
        else:
            named = [c for c in g if c.name]
            if named:
                best = max(named, key=lambda c: c.bbox.w)
            else:
                best = max(g, key=lambda c: c.bbox.w)
        out.append(best)
    return out


def _find_cabinet_name_at(doc: Document, bbox: BBox) -> str:
    best: Optional[tuple[float, str]] = None
    cx = bbox.x + bbox.w / 2
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
        if not (bbox.x - 20 <= ex <= bbox.x + bbox.w + 20 and
                bbox.y - 50 <= ey <= bbox.y + bbox.h + 10):
            continue
        if not any(kw in t for kw in _CABINET_KW):
            continue
        dy = abs(ey - (bbox.y + bbox.h))
        if best is None or dy < best[0]:
            best = (dy, t)
    return best[1] if best else ''


__all__ = [
    'detect_cabinets', '_merge_cabinet_candidates', '_find_cabinet_name_at',
]
