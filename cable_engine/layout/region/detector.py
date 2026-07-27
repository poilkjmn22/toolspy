"""Region detection strategies for panel layout cabinets.

Two strategies:
  1. **Text-based**: Find region keyword texts (仪表区, 端子排区, etc.)
     near the cabinet, define region extent, reparent children.
  2. **Group aggregation**: When 3+ groups exist but no text labels,
     split by large gaps between groups.
"""

from __future__ import annotations

from typing import Optional

from ...ir import Document, TextEntity, AttributeEntity
from ...ir.entities import BBox
from ..model import LayoutNode, LayoutNodeType


# Chinese region keywords in priority order
_REGION_KEYWORDS = [
    '仪表区', '仪表',
    '端子排区', '端子排',
    '继电器区', '继电器',
    '控制区', '控制',
    '电源区', '电源',
    '通讯区', '通讯',
    '设备区', '设备',
    '预留区', '预留',
    '操作区', '操作',
    '区域',
]

# Gap threshold for group-based region splitting
_GAP_THRESHOLD = 40.0

# How far a region keyword text can be from the cabinet center
_MAX_REGION_TEXT_OFFSET = 30.0


def detect_regions(cab: LayoutNode, doc: Document) -> list[LayoutNode]:
    """Detect functional regions inside a cabinet.

    Returns a list of REGION LayoutNodes. Children of *cab* that fall
    within a region are re-parented under the corresponding REGION node.
    Children not belonging to any region remain directly under *cab*.
    """
    regions: list[LayoutNode] = []

    # Strategy 1: Text-based region detection
    text_regions = _detect_by_text(cab, doc)
    regions.extend(text_regions)

    # Strategy 2: Group aggregation fallback
    # Only run if text found ≤1 region (most cabinets have 1-3 regions)
    if len(text_regions) <= 1:
        agg_regions = _detect_by_aggregation(cab)
        # Avoid duplicating what text already captured
        for ar in agg_regions:
            if not _overlaps_any(ar, text_regions):
                regions.append(ar)

    # Reparent children
    _assign_children_to_regions(cab, regions)

    # Name regions from child group semantics when no explicit name
    for r in regions:
        if not r.name:
            _name_region_by_children(r)

    return regions


# ---------------------------------------------------------------------------
# Strategy 1: Text-based
# ---------------------------------------------------------------------------

def _detect_by_text(cab: LayoutNode, doc: Document) -> list[LayoutNode]:
    """Find region keyword texts and create REGION nodes."""
    positions = _collect_texts(doc)
    cab_cx = cab.bbox.x + cab.bbox.w / 2
    cab_cy = cab.bbox.y + cab.bbox.h / 2

    matches: list[tuple[float, float, str, str]] = []
    for x, y, text in positions:
        if not (cab.bbox.x <= x <= cab.bbox.x + cab.bbox.w and
                cab.bbox.y <= y <= cab.bbox.y + cab.bbox.h):
            continue
        for kw in _REGION_KEYWORDS:
            if kw in text:
                matches.append((x, y, text, kw))
                break

    if not matches:
        return []

    # Dedup: keep only the first keyword match per area
    kept: list[tuple[float, float, str, str]] = []
    seen_areas: list[BBox] = []
    for x, y, text, kw in sorted(matches, key=lambda m: m[2]):
        if _covered_by_any(BBox(x - 40, y - 20, 80, 40), seen_areas):
            continue
        seen_areas.append(BBox(x - 40, y - 20, 80, 40))
        kept.append((x, y, text, kw))

    regions: list[LayoutNode] = []
    for i, (x, y, text, kw) in enumerate(kept):
        region_bbox = _estimate_region_bbox(x, y, cab)
        region = LayoutNode(
            id=f'region_{cab.id}_{i}',
            node_type=LayoutNodeType.REGION,
            bbox=region_bbox,
            name=text,
            data={'source': 'text', 'keyword': kw},
        )
        regions.append(region)

    return regions


def _estimate_region_bbox(tx: float, ty: float, cab: LayoutNode) -> BBox:
    """Estimate the region bounding box from a keyword text position.

    The region spans from the text downward (or upward) to the cabinet
    edge or to half the cabinet height, whichever is smaller.
    """
    cab_b = cab.bbox
    # Region top is above the text label
    region_top = cab_b.y + cab_b.h
    # Region bottom extends downward (or toward text direction)
    region_bottom = max(cab_b.y, ty - cab_b.h * 0.6)
    return BBox(
        x=cab_b.x,
        y=region_bottom,
        w=cab_b.w,
        h=region_top - region_bottom,
    )


# ---------------------------------------------------------------------------
# Strategy 2: Group aggregation
# ---------------------------------------------------------------------------

def _detect_by_aggregation(cab: LayoutNode) -> list[LayoutNode]:
    """Split cabinet children into regions by group proximity gaps.

    Only triggers when there are actual GROUP nodes (not just PANEL_AREAs).
    """
    groups = [c for c in cab.children if c.node_type == LayoutNodeType.GROUP]
    if len(groups) < 2:
        return []
    # Also include standalone devices for gap analysis
    children = groups + [c for c in cab.children
                         if c.node_type == LayoutNodeType.DEVICE]
    if len(children) < 3:
        return []

    # Sort top-to-bottom: descending center_y (higher Y = higher in drawing)
    sorted_kids = sorted(children, key=lambda c: _bbox_center_y(c.bbox), reverse=True)

    # Find gaps between consecutive children
    clusters: list[list[LayoutNode]] = [[sorted_kids[0]]]
    for kid in sorted_kids[1:]:
        prev = clusters[-1][-1]
        gap = _bbox_gap(prev.bbox, kid.bbox)
        if gap <= _GAP_THRESHOLD:
            clusters[-1].append(kid)
        else:
            clusters.append([kid])

    # Filter: only clusters with ≥2 items become regions
    regions: list[LayoutNode] = []
    for ci, cluster in enumerate(clusters):
        if len(cluster) < 2:
            continue
        region_bbox = _merge_bboxes(c.bbox for c in cluster)
        region = LayoutNode(
            id=f'region_agg_{cab.id}_{ci}',
            node_type=LayoutNodeType.REGION,
            bbox=region_bbox,
            data={'source': 'aggregation'},
        )
        regions.append(region)

    return regions


# ---------------------------------------------------------------------------
# Reparenting
# ---------------------------------------------------------------------------

def _assign_children_to_regions(cab: LayoutNode,
                                 regions: list[LayoutNode]) -> None:
    """Move cab children that sit inside a region under that region node.

    Rules:
      - GROUP / DEVICE nodes that are direct cab children and sit inside
        a region → moved into the region.
      - PANEL_AREA nodes whose entire bbox sits inside a region → the
        whole AREA (with its existing children intact) is moved.
      - Children INSIDE a PANEL_AREA are never extracted — the AREA
        itself may be moved as a whole.
    """
    if not regions:
        return

    assigned: set[str] = set()

    for region in regions:
        for child in list(cab.children):
            if child.id in assigned:
                continue
            if child.node_type == LayoutNodeType.REGION:
                continue

            if child.node_type == LayoutNodeType.PANEL_AREA:
                # Move the whole AREA (with children intact)
                if _bbox_inside(child.bbox, region.bbox):
                    region.add_child(child)
                    assigned.add(child.id)
            else:
                # Move standalone GROUP / DEVICE
                if _bbox_inside(child.bbox, region.bbox):
                    region.add_child(child)
                    assigned.add(child.id)

    # Remove assigned children from cab
    cab.children = [c for c in cab.children if c.id not in assigned]

    # Re-add regions as cab children
    for region in regions:
        if region not in cab.children:
            cab.add_child(region)


def _name_region_by_children(region: LayoutNode) -> None:
    """Infer a region name from its child nodes' semantic types."""
    group_types = set()
    for c in region.children:
        if c.node_type == LayoutNodeType.GROUP:
            gs = c.data.get('group_semantic', {})
            st = gs.get('type', '')
            if st:
                group_types.add(st)
    name_parts = sorted(group_types)
    if name_parts:
        region.name = ' + '.join(name_parts)
    else:
        n_devices = sum(1 for c in region.children
                        if c.node_type == LayoutNodeType.DEVICE)
        region.name = f'{n_devices} devices'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_texts(doc: Document) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t or len(t) > 30:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is not None and ey is not None:
            out.append((ex, ey, t))
    return out


def _bbox_center_y(b: BBox) -> float:
    return b.y + b.h / 2


def _bbox_gap(a: BBox, b: BBox) -> float:
    """Return the gap between a (top, sorted earlier) and b (below).

    In CAD Y-up coordinates, the lower Y value is lower in the drawing.
    """
    a_lower = a.y                 # bottom edge of a (more negative)
    b_upper = b.y + b.h           # top edge of b (more positive)
    gap = a_lower - b_upper       # positive → gap; negative → overlap
    return max(0.0, gap)


def _bbox_inside(inner: BBox, outer: BBox) -> bool:
    return (inner.x >= outer.x
            and inner.x + inner.w <= outer.x + outer.w
            and inner.y >= outer.y
            and inner.y + inner.h <= outer.y + outer.h)


def _merge_bboxes(bboxes) -> BBox:
    bboxes = list(bboxes)
    xs = [b.x for b in bboxes] + [b.x + b.w for b in bboxes]
    ys = [b.y for b in bboxes] + [b.y + b.h for b in bboxes]
    return BBox(
        x=min(xs), y=min(ys),
        w=max(xs) - min(xs), h=max(ys) - min(ys),
    )


def _overlaps_any(region: LayoutNode, others: list[LayoutNode]) -> bool:
    for o in others:
        if _bboxes_overlap(region.bbox, o.bbox):
            return True
    return False


def _bboxes_overlap(a: BBox, b: BBox) -> bool:
    return (a.x < b.x + b.w and a.x + a.w > b.x
            and a.y < b.y + b.h and a.y + a.h > b.y)


def _covered_by_any(bbox: BBox, others: list[BBox]) -> bool:
    for o in others:
        if _bboxes_overlap(bbox, o):
            return True
    return False


__all__ = ['detect_regions']
