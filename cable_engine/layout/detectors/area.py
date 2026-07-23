"""cable_engine.layout.detectors.area — Panel area detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...ir import AttributeEntity, Document, TextEntity
from ...ir.entities import BBox
from ..primitives.bbox import bbox_contains_center, bbox_same
from ..primitives.rectangle import DetectedRect
from ..primitives.line import LongLine
from ..model import LayoutNode, LayoutNodeType


@dataclass
class CabinetInterior:
    header_rects: list[DetectedRect] = field(default_factory=list)
    device_area: Optional[DetectedRect] = None
    other_rects: list[DetectedRect] = field(default_factory=list)


def detect_cabinet_interior(
    cab: LayoutNode, rects: list[DetectedRect],
) -> CabinetInterior:
    result = CabinetInterior()
    cab_bb = cab.bbox
    min_w = cab_bb.w * 0.8

    for r in rects:
        if not bbox_contains_center(cab_bb, r.bbox):
            continue
        if r.bbox.w >= min_w and r.bbox.h <= 15:
            result.header_rects.append(r)
        elif r.bbox.w >= cab_bb.w * 0.5 and r.bbox.h >= cab_bb.h * 0.4:
            if r.bbox.w > cab_bb.w * 1.1 or r.bbox.h > cab_bb.h * 1.1:
                continue
            if (result.device_area is None
                    or r.bbox.w * r.bbox.h > result.device_area.bbox.w * result.device_area.bbox.h):
                result.device_area = r
        else:
            result.other_rects.append(r)

    result.header_rects.sort(key=lambda r: r.bbox.y)
    return result


def detect_areas(
    doc: Document, cabinet: LayoutNode, hors: list[LongLine],
) -> list[LayoutNode]:
    return _detect_areas_from_dividers(doc, cabinet, hors)


def detect_areas_v2(
    doc: Document, cabinet: LayoutNode, hors: list[LongLine],
    interior: CabinetInterior,
) -> list[LayoutNode]:
    cab = cabinet.bbox

    if interior.device_area is not None:
        area_bbox = interior.device_area.bbox
        if bbox_same(area_bbox, cabinet.bbox):
            return []
        name = _find_area_name(doc, area_bbox, cab)
        area_node = LayoutNode(
            id=f'{cabinet.id}_area_0',
            node_type=LayoutNodeType.PANEL_AREA,
            bbox=area_bbox, name=name,
        )
        return [area_node]

    return _detect_areas_from_dividers(doc, cabinet, hors)


def _detect_areas_from_dividers(
    doc: Document, cabinet: LayoutNode, hors: list[LongLine],
) -> list[LayoutNode]:
    cab = cabinet.bbox
    cab_width = cab.w
    min_span = cab_width * 0.5

    dividers: list[float] = []
    for h in hors:
        if h.y <= cab.y or h.y >= cab.y + cab.h:
            continue
        span = min(h.end, cab.x + cab.w) - max(h.start, cab.x)
        if span >= min_span:
            dividers.append(h.y)
            continue
        near_left = abs(h.start - cab.x) < 5.0
        near_right = abs(h.end - (cab.x + cab.w)) < 5.0
        if (near_left or near_right) and span > cab_width * 0.3:
            dividers.append(h.y)

    if not dividers:
        return []

    all_y = sorted(set(dividers + [cab.y, cab.y + cab.h]))
    areas: list[LayoutNode] = []
    for i in range(len(all_y) - 1):
        y0 = all_y[i]
        y1 = all_y[i + 1]
        h = y1 - y0
        if h < 30:
            continue
        area_bbox = BBox(x=cab.x, y=y0, w=cab_width, h=h)
        name = _find_area_name(doc, area_bbox, cab)
        area_node = LayoutNode(
            id=f'{cabinet.id}_area_{i}',
            node_type=LayoutNodeType.PANEL_AREA,
            bbox=area_bbox, name=name,
        )
        areas.append(area_node)
    return areas


def _find_area_name(doc: Document, area_bbox: BBox, cab: BBox) -> str:
    top_band_y = area_bbox.y + area_bbox.h - (area_bbox.h * 0.2)
    candidates: list[tuple[float, str]] = []
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
        if not (cab.x <= ex <= cab.x + cab.w):
            continue
        if not (top_band_y <= ey <= area_bbox.y + area_bbox.h):
            continue
        dy = (area_bbox.y + area_bbox.h) - ey
        candidates.append((dy, t))
    candidates.sort(key=lambda x: x[0])
    if candidates:
        return candidates[0][1]
    return ''


__all__ = [
    'CabinetInterior', 'detect_cabinet_interior',
    'detect_areas', 'detect_areas_v2',
]
