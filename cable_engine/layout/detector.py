"""cable_engine.layout.detector — High-level orchestrator for LayoutTree.

Imports detection primitives and detectors, runs the pipeline, and
re-exports everything for backward compatibility.

Pipeline:
  entities  →  detect_rectangles  →  detect_cabinets  →  areas/detect_areas_v2
            →  detect_devices + _detect_open_rect_devices + _detect_text_devices
            →  _detect_device_sub_groups  →  LayoutTree
            →  _apply_grouping  →  GROUP nodes
            →  _annotate_groups  →  semantic types
"""

from __future__ import annotations

from typing import Optional

from .primitives.rectangle import DetectedRect, detect_rectangles
from .primitives.line import LongLine, detect_long_lines
from .primitives.bbox import (
    bbox_overlap_ratio, bbox_contains, bbox_contains_center,
    bbox_same, drawing_extents,
)
from .detectors.cabinet import (
    detect_cabinets, _merge_cabinet_candidates, _find_cabinet_name_at,
)
from .detectors.device import (
    detect_devices, _detect_open_rect_devices, _detect_text_devices,
    _merge_devices,
    _find_device_name_by_text, _find_blockref_bboxes,
    _detect_device_sub_groups,
)
from .detectors.area import (
    CabinetInterior, detect_areas, detect_areas_v2,
    detect_cabinet_interior,
)
from .model import LayoutNode, LayoutNodeType, LayoutTree
from .grouping import detect_layout_groups
from .semantics.group_type import annotate_groups as _annotate_groups
from ..ir import Document

# ---------------------------------------------------------------------------
# Build pipeline
# ---------------------------------------------------------------------------


def _collect_leaf_devices(node: LayoutNode) -> list[LayoutNode]:
    """Collect DEVICE nodes that are *direct* children (not in sub-groups/areas)."""
    return [c for c in (node.children or [])
            if c.node_type == LayoutNodeType.DEVICE]


def _collect_text_positions(doc: Document) -> list[tuple[float, float, str]]:
    """Collect (x, y, text) tuples for group label assignment."""
    out: list[tuple[float, float, str]] = []
    for e in doc.entities:
        from ..ir import TextEntity, AttributeEntity
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t or len(t) > 20:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is not None and ey is not None:
            out.append((ex, ey, t))
    return out


def _apply_grouping(
    parent: LayoutNode,
    cab_bbox: BBox,
    text_positions: Optional[list[tuple[float, float, str]]] = None,
) -> None:
    """Cluster direct DEVICE children into GROUP nodes, insert into *parent*."""
    all_devices = _collect_leaf_devices(parent)
    if len(all_devices) < 3:
        return

    groups = detect_layout_groups(all_devices, cab_bbox, text_positions)
    if not groups:
        return

    grouped_ids = set()
    for g in groups:
        for c in g.children:
            grouped_ids.add(c.id)

    # Remove grouped devices from parent; add GROUP node instead.
    remaining: list[LayoutNode] = []
    for c in parent.children:
        if c.node_type == LayoutNodeType.DEVICE and c.id in grouped_ids:
            continue
        remaining.append(c)
    for g in groups:
        g.parent = parent
        remaining.append(g)
    parent.children = remaining


def _identify_front_back(cabinets: list[LayoutNode]) -> None:
    if len(cabinets) < 2:
        return
    sorted_cabs = sorted(cabinets, key=lambda n: n.bbox.y)
    sorted_cabs[0].data['face'] = 'front'
    if not sorted_cabs[0].name:
        sorted_cabs[0].name = '正面'
    for c in sorted_cabs[1:]:
        c.data['face'] = 'back'
        if not c.name:
            c.name = '背面'


def build_layout_tree(doc: Document) -> LayoutTree:
    tree = LayoutTree()

    rects = detect_rectangles(doc)
    verts, hors = detect_long_lines(doc, min_length=50.0)
    all_verts, all_hors = detect_long_lines(doc, min_length=3.0)

    cabinets = detect_cabinets(doc, rects, verts, hors)
    if not cabinets:
        return tree

    text_positions = _collect_text_positions(doc)

    for cab in cabinets:
        interior = detect_cabinet_interior(cab, rects)
        area_nodes = detect_areas_v2(doc, cab, hors, interior)

        if area_nodes:
            for area in area_nodes:
                devices = detect_devices(doc, rects, area)
                open_devs = _detect_open_rect_devices(doc, area, all_verts, all_hors)
                devices = _merge_devices(devices, open_devs)
                text_devs = _detect_text_devices(doc, area)
                devices = _merge_devices(devices, text_devs)
                groups = _detect_device_sub_groups(doc, area, rects)
                if groups:
                    for d in devices:
                        assigned = False
                        for g in groups:
                            if bbox_contains(g.bbox, d.bbox):
                                g.add_child(d)
                                assigned = True
                                break
                        if not assigned:
                            area.add_child(d)
                    groups = [g for g in groups if g.children or (g.name and len(g.name) <= 15)]
                    for g in groups:
                        area.add_child(g)
                else:
                    for d in devices:
                        area.add_child(d)

                cab.add_child(area)

                # Apply spatial grouping within each area.
                _apply_grouping(area, cab.bbox, text_positions)

            # Detect orphan devices inside the cabinet but outside all areas.
            cab_rect_devs = detect_devices(doc, rects, cab)
            cab_open_devs = _detect_open_rect_devices(doc, cab, all_verts, all_hors)
            cab_all = _merge_devices(cab_rect_devs, cab_open_devs)
            area_bboxes = [a.bbox for a in area_nodes]
            orphans = []
            for d in cab_all:
                inside_area = any(
                    bbox_contains_center(ab, d.bbox, pad=2.0)
                    for ab in area_bboxes
                )
                if not inside_area:
                    orphans.append(d)
            # Text-device fallback for orphans too.
            cab_text_devs = _detect_text_devices(doc, cab)
            for d in cab_text_devs:
                inside_area = any(
                    bbox_contains_center(ab, d.bbox, pad=2.0)
                    for ab in area_bboxes
                )
                if not inside_area:
                    orphans.append(d)
            for d in orphans:
                cab.add_child(d)

            # Apply grouping on orphans too.
            _apply_grouping(cab, cab.bbox, text_positions)
        else:
            devices = detect_devices(doc, rects, cab)
            open_devs = _detect_open_rect_devices(doc, cab, all_verts, all_hors)
            devices = _merge_devices(devices, open_devs)
            text_devs = _detect_text_devices(doc, cab)
            devices = _merge_devices(devices, text_devs)
            for d in devices:
                if d.name:
                    cab.add_child(d)

            # Apply spatial grouping at cabinet level.
            _apply_grouping(cab, cab.bbox, text_positions)

        tree.add_root(cab)

    # Annotate GROUP nodes with semantic types.
    _annotate_groups(tree)

    # Identify front/back cabinets by relative x position.
    _identify_front_back(cabinets)

    return tree


# Re-export everything for backward compat
__all__ = [
    'build_layout_tree',
    'DetectedRect', 'detect_rectangles',
    'LongLine', 'detect_long_lines',
    'bbox_overlap_ratio', 'bbox_contains', 'bbox_contains_center',
    'bbox_same', 'drawing_extents',
    'detect_cabinets', '_merge_cabinet_candidates', '_find_cabinet_name_at',
    'detect_devices', '_detect_open_rect_devices', '_detect_text_devices',
    '_merge_devices',
    '_find_device_name_by_text', '_find_blockref_bboxes',
    '_detect_device_sub_groups',
    'CabinetInterior', 'detect_areas', 'detect_areas_v2',
    'detect_cabinet_interior',
    'LayoutNode', 'LayoutNodeType', 'LayoutTree',
]
