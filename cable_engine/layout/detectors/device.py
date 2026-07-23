"""cable_engine.layout.detectors.device — Device detection."""

from __future__ import annotations

import math
from typing import Optional

from ...ir import (
    AttributeEntity, BlockRef, Document, LineGeometry, TextEntity,
)
from ...ir.entities import BBox
from ..primitives.bbox import bbox_contains, bbox_contains_center, bbox_overlap_ratio
from ..primitives.rectangle import DetectedRect, _rect_bbox
from ..primitives.line import LongLine
from ..model import LayoutNode, LayoutNodeType


def detect_devices(
    doc: Document, rects: list[DetectedRect],
    container: LayoutNode,
) -> list[LayoutNode]:
    devices: list[LayoutNode] = []
    cb = container.bbox

    for r in rects:
        if r.bbox.w < 3.0 or r.bbox.h < 3.0:
            continue
        if r.bbox.w > 150 or r.bbox.h > 150:
            continue
        if not bbox_contains_center(cb, r.bbox):
            continue
        name = _find_device_name_by_text(doc, r.bbox)
        node = LayoutNode(
            id=f'{container.id}_dev_{len(devices)}',
            node_type=LayoutNodeType.DEVICE,
            bbox=r.bbox, name=name,
            data={'source': 'rectangle'},
        )
        devices.append(node)

    block_bboxes = _find_blockref_bboxes(doc, container)
    for bbox, bname in block_bboxes:
        dup = False
        for d in devices:
            if bbox_overlap_ratio(bbox, d.bbox) > 0.5:
                if not d.name and bname:
                    d.name = bname
                dup = True
                break
        if not dup:
            devices.append(LayoutNode(
                id=f'{container.id}_dev_{len(devices)}',
                node_type=LayoutNodeType.DEVICE,
                bbox=bbox, name=bname or '',
                data={'source': 'blockref'},
            ))

    return devices


def _find_device_name_by_text(doc: Document, bbox: BBox) -> str:
    texts: list[tuple[float, str]] = []
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
        if not (bbox.x <= ex <= bbox.x + bbox.w and
                bbox.y <= ey <= bbox.y + bbox.h):
            continue
        texts.append((ey, t))
    if not texts:
        return ''
    texts.sort(key=lambda x: -x[0])
    return ' / '.join(t for _, t in texts)


def _find_blockref_bboxes(
    doc: Document, container: LayoutNode,
) -> list[tuple[BBox, str]]:
    cb = container.bbox
    results: list[tuple[BBox, str]] = []

    for e in doc.entities:
        if not isinstance(e, BlockRef):
            continue
        if e.insert_point is None:
            continue
        ix = e.insert_point.x
        iy = e.insert_point.y
        if not (cb.x - 10 <= ix <= cb.x + cb.w + 10 and
                cb.y - 10 <= iy <= cb.y + cb.h + 10):
            continue

        best_bbox: Optional[BBox] = None
        best_d = 20.0
        for ent in doc.entities:
            if not isinstance(ent, LineGeometry):
                continue
            pts = list(ent.points or [])
            if len(pts) not in (4, 5):
                continue
            if len(pts) == 5:
                if abs(pts[0].x - pts[-1].x) > 0.1 or abs(pts[0].y - pts[-1].y) > 0.1:
                    continue
            rect = _rect_bbox(pts[:4])
            if rect is None:
                continue
            if rect.w < 3.0 or rect.h < 3.0:
                continue
            if rect.w > 150 or rect.h > 150:
                continue
            cx = rect.x + rect.w / 2
            cy = rect.y + rect.h / 2
            d = math.hypot(ix - cx, iy - cy)
            if d < best_d:
                best_d = d
                best_bbox = rect

        if best_bbox is not None:
            results.append((best_bbox, e.name or ''))

    return results


def _detect_open_rect_devices(
    doc: Document, container: LayoutNode, verts: list[LongLine],
    hors: list[LongLine],
) -> list[LayoutNode]:
    cb = container.bbox
    devices: list[LayoutNode] = []

    hors_short = [
        h for h in hors
        if cb.y - 3 <= h.y <= cb.y + cb.h + 3
        and h.start >= cb.x - 5 and h.end <= cb.x + cb.w + 5
        and (
            h.length < 50
            or abs(h.end - (cb.x + cb.w)) <= 3.0
            or abs(h.start - cb.x) <= 3.0
        )
    ]

    verts_long = [v for v in verts if v.length >= 100
                  and v.x >= cb.x - 2 and v.x <= cb.x + cb.w + 2]

    segs_by_span: dict[tuple[float, float], list[float]] = {}
    for h in hors_short:
        a, b = round(h.start, 0), round(h.end, 0)
        if a > b:
            a, b = b, a
        key = (a, b)
        segs_by_span.setdefault(key, []).append(h.y)

    for spine in verts_long:
        sx = spine.x
        for (x1, x2), ys in segs_by_span.items():
            if abs(x1 - sx) > 2.0 and abs(x2 - sx) > 2.0:
                continue
            far_x = x2 if abs(x1 - sx) <= 2.0 else x1
            width = abs(x2 - x1)
            if width < 2:
                continue
            ys_sorted = sorted(set(ys), reverse=True)
            for i in range(len(ys_sorted) - 1):
                y_top = ys_sorted[i]
                y_bot = ys_sorted[i + 1]
                h = y_top - y_bot
                if h < 5 or h > 100:
                    continue
                has_v = any(
                    abs(v.x - far_x) < 1.0
                    and v.start <= y_bot + 2 and v.end >= y_top - 2
                    for v in verts
                )
                if not has_v:
                    # No far-side vertical: accept if span reaches the
                    # container edge (spine-to-edge open rect, common
                    # for back-face terminal strips like 光纤终端盒).
                    at_edge = abs(far_x - (cb.x + cb.w)) <= 3.0 or abs(far_x - cb.x) <= 3.0
                    if not at_edge:
                        continue
                bbox = BBox(x=min(x1, far_x), y=y_bot, w=width, h=h)
                if not bbox_contains_center(cb, bbox):
                    continue
                name = _find_device_name_by_text(doc, bbox)
                if not name:
                    continue
                devices.append(LayoutNode(
                    id=f'{container.id}_o_{len(devices)}',
                    node_type=LayoutNodeType.DEVICE,
                    bbox=bbox, name=name,
                    data={'source': 'open_rect'},
                ))

    return devices


def _detect_text_devices(
    doc: Document, container: LayoutNode, min_char_width: float = 10.0,
    char_height: float = 15.0,
) -> list[LayoutNode]:
    """Create DEVICE nodes from text entities w/o surrounding rectangles.

    Each text entity inside *container* whose insertion point is NOT
    already covered by an existing DEVICE bbox creates a new DEVICE node.
    Estimated bbox: width = len(text) × min_char_width, height = char_height.

    This is the fallback that catches DH/DL/fiber-box labels the
    rectangle-based detectors miss.
    """
    cb = container.bbox
    devices: list[LayoutNode] = []

    # Collect bboxes of existing devices to avoid duplicates.
    existing_bboxes: list[BBox] = []
    for c in container.children or []:
        if c.node_type == LayoutNodeType.DEVICE:
            existing_bboxes.append(c.bbox)

    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t:
            continue
        # Skip long text blocks (cabinet names / labels, not devices).
        if len(t) > 20:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        # Must be inside the container.
        if not (cb.x <= ex <= cb.x + cb.w and cb.y <= ey <= cb.y + cb.h):
            continue

        tw = max(len(t) * min_char_width, 20.0)
        bbox = BBox(ex - tw / 2, ey - char_height / 2, tw, char_height)

        # Skip if this text is inside an existing device.
        dup = False
        for eb in existing_bboxes:
            if bbox_overlap_ratio(bbox, eb) > 0.2:
                dup = True
                break
        if dup:
            continue

        devices.append(LayoutNode(
            id=f'{container.id}_txt_{len(devices)}',
            node_type=LayoutNodeType.DEVICE,
            bbox=bbox, name=t,
            data={'source': 'text'},
        ))

    return devices


def _merge_devices(a: list[LayoutNode], b: list[LayoutNode]) -> list[LayoutNode]:
    merged = list(a)
    for d in b:
        dup = False
        for m in merged:
            if bbox_overlap_ratio(d.bbox, m.bbox) > 0.4:
                dup = True
                break
        if not dup:
            merged.append(d)
    return merged


def _detect_device_sub_groups(
    doc: Document, area_node: LayoutNode, rects: list[DetectedRect],
) -> list[LayoutNode]:
    area_bb = area_node.bbox
    min_size = 30.0
    max_area_ratio = 0.7

    candidates: list[DetectedRect] = []
    for r in rects:
        bb = r.bbox
        if not bbox_contains(area_bb, bb):
            continue
        if bb.w < min_size or bb.h < min_size:
            continue
        if (bb.w * bb.h) / (area_bb.w * area_bb.h) > max_area_ratio:
            continue
        candidates.append(r)

    def _find_group_label(grp_bbox: BBox) -> str:
        top_band_lo = grp_bbox.y + grp_bbox.h - 20
        top_band_hi = grp_bbox.y + grp_bbox.h + 5
        labels: list[tuple[float, str]] = []
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
            if not (top_band_lo <= ey <= top_band_hi):
                continue
            if not (grp_bbox.x - 5 <= ex <= grp_bbox.x + grp_bbox.w + 5):
                continue
            labels.append((ex, t))
        labels.sort(key=lambda x: x[0])
        return labels[0][1] if labels else ''

    groups: list[LayoutNode] = []
    for i, cand in enumerate(candidates):
        name = _find_group_label(cand.bbox)
        if len(name) > 15:
            continue
        group = LayoutNode(
            id=f'{area_node.id}_group_{i}',
            node_type=LayoutNodeType.PANEL_AREA,
            bbox=cand.bbox,
            name=name,
        )
        groups.append(group)

    groups.sort(key=lambda g: -g.bbox.y)
    return groups


__all__ = [
    'detect_devices', '_detect_open_rect_devices', '_merge_devices',
    '_find_device_name_by_text', '_find_blockref_bboxes',
    '_detect_device_sub_groups',
]
