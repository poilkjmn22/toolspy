"""cable_engine.layout.detector — V9 LayoutTree orchestrator.

Pipeline:
  entities  →  detect_rectangles / detect_long_lines
            →  detect_cabinets  →  detect_areas_v2
            →  candidate.build_device_candidates  →  DBSCANClusterer
            →  structure.* analyzers  →  GROUP + DEVICE nodes
            →  annotate_groups  →  semantic types
            →  _identify_front_back  →  face labels

DBSCAN answers "which devices are near each other".
Structure analyzers answer "what spatial pattern are they in".
"""

from __future__ import annotations

from typing import Optional

from .primitives.rectangle import DetectedRect, detect_rectangles
from .primitives.line import LongLine, detect_long_lines
from .primitives.bbox import (
    bbox_overlap_ratio, bbox_contains_center, drawing_extents,
)
from .detectors.cabinet import (
    detect_cabinets, _merge_cabinet_candidates, _find_cabinet_name_at,
)
from .detectors.area import (
    CabinetInterior, detect_areas, detect_areas_v2,
    detect_cabinet_interior,
)
from .model import LayoutNode, LayoutNodeType, LayoutTree
from .semantics.group_type import annotate_groups as _annotate_groups
from .candidate import DeviceCandidate, build_device_candidates
from .clustering import DeviceGroup, DBSCANClusterer
from .associator import TextAssociator
from .table import TableArea, detect_table_regions, parse_table_at, match_to_devices
from ..ir import Document, TextEntity, AttributeEntity
from ..ir.entities import BBox

# ---------------------------------------------------------------------------
# Build pipeline
# ---------------------------------------------------------------------------


def _collect_text_positions(doc: Document) -> list[tuple[float, float, str]]:
    """Collect (x, y, text) tuples for text association."""
    out: list[tuple[float, float, str]] = []
    for e in doc.entities:
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


def _build_device_node(cand: DeviceCandidate) -> Optional[LayoutNode]:
    """Build a DEVICE LayoutNode from a DeviceCandidate."""
    name = cand.name or (cand.texts[0][0] if cand.texts else '')
    if not name:
        return None
    desc_text = ' / '.join(cand.description) if cand.description else ''
    full_name = f'{name} / {desc_text}' if desc_text else name
    return LayoutNode(
        id=cand.id,
        node_type=LayoutNodeType.DEVICE,
        bbox=cand.bbox,
        name=full_name,
        data={'source': cand.source},
    )


def _build_group_node(group: DeviceGroup, label: int) -> LayoutNode:
    """Build a GROUP LayoutNode from a DeviceGroup."""
    g_node = LayoutNode(
        id=f'dbscan_{label}',
        node_type=LayoutNodeType.GROUP,
        group_type=group.group_type,
        bbox=group.bbox,
        name=group.name,
        data={
            'score': round(group.score, 2),
            'evidence': group.features.get('evidence', []),
            **group.features,
        },
    )
    for d in group.devices:
        dev_node = _build_device_node(d)
        if dev_node:
            g_node.add_child(dev_node)
    return g_node


def _apply_grouping_v2(
    parent: LayoutNode,
    cab_bbox: BBox,
    doc: Document,
    table: Optional[TableArea] = None,
) -> None:
    """V8.1 pipeline: CandidatePool → TextAssociator → DBSCAN → LayoutNodes."""
    text_positions = _collect_text_positions(doc)
    container = parent.bbox

    candidates = build_device_candidates(doc, container)
    if len(candidates) < 2:
        return

    # If parent has sub-containers (PANEL_AREA), exclude candidates inside them.
    exclude_bboxes = [
        c.bbox for c in parent.children
        if c.node_type == LayoutNodeType.PANEL_AREA
    ]
    if exclude_bboxes:
        candidates = [
            c for c in candidates
            if not any(bbox_contains_center(eb, c.bbox) for eb in exclude_bboxes)
        ]
        if len(candidates) < 2:
            return

    TextAssociator().associate_devices(candidates, text_positions)

    # Inject equipment table metadata into matching candidates.
    if table is not None:
        match_to_devices(table, candidates)

    clusterer = DBSCANClusterer(eps=30, min_samples=2)
    groups = clusterer.cluster(candidates, cab_bbox)

    TextAssociator().associate_groups(groups, text_positions, cab_bbox)

    grouped_ids = {d.id for g in groups for d in g.devices}
    device_children: list[LayoutNode] = []

    for label, g in enumerate(groups):
        group_node = _build_group_node(g, label)
        device_children.append(group_node)

    for c in candidates:
        if c.id not in grouped_ids:
            dev_node = _build_device_node(c)
            if dev_node:
                device_children.append(dev_node)

    # Preserve non-DEVICE children (areas, sub-groups, etc.)
    non_device = [c for c in parent.children
                  if c.node_type != LayoutNodeType.DEVICE]
    for c in device_children:
        c.parent = parent
    parent.children = non_device + device_children


def _detect_equipment_table(
    doc: Document, cab: LayoutNode,
) -> Optional[TableArea]:
    """Detect and parse an equipment table inside or near *cab*.

    Returns the first valid table found; ``None`` if none detected.
    """
    search_bbox = BBox(
        cab.bbox.x, cab.bbox.y,
        cab.bbox.w + 200.0, cab.bbox.h,
    )
    for tbbox in detect_table_regions(doc, search_bbox):
        table = parse_table_at(doc, tbbox)
        if table is not None and table.name_column_index >= 0:
            return table
    return None


def _identify_front_back(cabinets: list[LayoutNode],
                          doc: Optional[Document] = None) -> None:
    if len(cabinets) < 2:
        return

    face_texts: list[tuple[float, float, str]] = []
    if doc is not None:
        for e in doc.entities:
            if not isinstance(e, (TextEntity, AttributeEntity)):
                continue
            t = (e.text or '').strip().replace(' ', '')
            if t not in ('正面', '背面'):
                continue
            cf = getattr(e, 'custom_fields', None) or {}
            ex, ey = cf.get('x'), cf.get('y')
            if ex is not None and ey is not None:
                face_texts.append((ex, ey, t))

    if not face_texts:
        _identify_front_back_fallback(cabinets)
        return

    matched = set()
    for cab in cabinets:
        cx = cab.bbox.x + cab.bbox.w / 2
        bottom = cab.bbox.y
        best: Optional[tuple[str, str, float]] = None
        for tx, ty, tt in face_texts:
            if ty >= bottom:
                continue
            dx = abs(tx - cx)
            if dx > cab.bbox.w * 0.6:
                continue
            dy = bottom - ty
            if dy > 200:
                continue
            d = dx + dy * 0.5
            if best is None or d < best[2]:
                best = (tt, 'front' if '正' in tt else 'back', d)
        if best is not None:
            if not cab.name:
                cab.name = best[0]
            cab.data['face'] = best[1]
            matched.add(cab.id)

    # Fallback for cabinets without matching text.
    unmatched = [c for c in cabinets if c.id not in matched]
    if unmatched:
        sorted_u = sorted(unmatched, key=lambda c: c.bbox.y)
        for i, cab in enumerate(sorted_u):
            is_front = (i == 0)
            cab.data['face'] = 'front' if is_front else 'back'
            if not cab.name:
                cab.name = '正面' if is_front else '背面'


def _identify_front_back_fallback(cabinets: list[LayoutNode]) -> None:
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

    cabinets = detect_cabinets(doc, rects, verts, hors)
    if not cabinets:
        return tree

    for cab in cabinets:
        interior = detect_cabinet_interior(cab, rects)
        area_nodes = detect_areas_v2(doc, cab, hors, interior)

        # Attempt to detect equipment table for this cabinet.
        table = _detect_equipment_table(doc, cab)

        if area_nodes:
            for area in area_nodes:
                cab.add_child(area)
                _apply_grouping_v2(area, cab.bbox, doc, table)
            # Orphans: run V8.1 at cabinet level (excludes area children).
            _apply_grouping_v2(cab, cab.bbox, doc, table)
        else:
            _apply_grouping_v2(cab, cab.bbox, doc, table)

        tree.add_root(cab)

    _annotate_groups(tree)
    _identify_front_back(cabinets, doc)

    return tree


__all__ = [
    'build_layout_tree',
    'DetectedRect', 'detect_rectangles',
    'LongLine', 'detect_long_lines',
    'bbox_overlap_ratio', 'bbox_contains_center', 'drawing_extents',
    'detect_cabinets', '_merge_cabinet_candidates', '_find_cabinet_name_at',
    'CabinetInterior', 'detect_areas', 'detect_areas_v2',
    'detect_cabinet_interior',
    'LayoutNode', 'LayoutNodeType', 'LayoutTree',
]
