"""LayoutTree → SpatialGraph bridge.

Lifts the hierarchical LayoutTree into a flat SpatialGraph with
spatial relations computed between layout nodes.

Algorithm:
  1. Walk LayoutTree, create a SpatialNode for every node.
  2. Add CONTAINS edges for parent→child.
  3. For sibling pairs under the same parent, compute
     LEFT_OF / RIGHT_OF / ABOVE / BELOW based on bbox overlap.
  4. For all device-type nodes within the same cabinet, compute
     ALIGNED_VERT / ALIGNED_HORZ / NEAR relations.
"""

from __future__ import annotations

from ...ir.entities import BBox
from ..model import LayoutNode, LayoutTree, LayoutNodeType
from .model import SpatialGraph, SpatialNode, SpatialEdge, SpatialRelation


# Configuration
_OVERLAP_THRESHOLD = 0.3       # minimum overlap ratio for directional relations
_ALIGN_TOLERANCE = 8.0         # max center difference (units) for alignment
_NEAR_THRESHOLD = 40.0         # centroid distance for NEAR relation
_MAX_DEVICE_PAIRS = 2000       # cap O(N²) for large cabinets


def lift(tree: LayoutTree) -> SpatialGraph:
    """Build a SpatialGraph from a LayoutTree."""
    graph = SpatialGraph()
    all_nodes: list[LayoutNode] = _flatten(tree)

    # 1. Create SpatialNodes
    for ln in all_nodes:
        graph.add_node(SpatialNode(
            node_id=ln.id,
            node_type=ln.node_type.value,
            bbox=ln.bbox,
            name=ln.name,
            data=dict(ln.data),
        ))

    # 2. CONTAINS edges from parent → child
    for ln in all_nodes:
        for child in ln.children:
            _add_edge(graph, SpatialRelation.CONTAINS,
                      ln.id, child.id, distance=0.0)

    # 3. Sibling spatial relations
    _add_sibling_relations(graph, all_nodes)

    # 4. Alignment + NEAR for devices within the same cabinet
    _add_device_relations(graph, all_nodes)

    return graph


def _flatten(tree: LayoutTree) -> list[LayoutNode]:
    """Return all nodes in the tree in breadth-first order."""
    result: list[LayoutNode] = []
    stack = list(tree.roots)
    while stack:
        node = stack.pop(0)
        result.append(node)
        stack.extend(node.children)
    return result


# ---------------------------------------------------------------------------
# Sibling relations
# ---------------------------------------------------------------------------

def _add_sibling_relations(graph: SpatialGraph,
                            all_nodes: list[LayoutNode]) -> None:
    """Compute LEFT_OF / RIGHT_OF / ABOVE / BELOW for sibling pairs."""
    _id_map = {n.id: n for n in all_nodes}
    processed: set[tuple[str, str]] = set()

    for node in all_nodes:
        siblings = [c for c in (node.parent.children if node.parent else [])
                    if c.id != node.id]
        for sib in siblings:
            pair = (node.id, sib.id) if node.id < sib.id else (sib.id, node.id)
            if pair in processed:
                continue
            processed.add(pair)

            rel, dist = _directional_relation(node.bbox, sib.bbox)
            if rel is not None:
                _add_edge(graph, rel, node.id, sib.id, distance=dist)


def _directional_relation(
    a: BBox, b: BBox,
) -> tuple[Optional[SpatialRelation], float]:
    """Determine directional relation between two sibling bboxes.

    Returns (relation, distance) or (None, 0) if no clear direction.
    """
    a_cx, a_cy = a.x + a.w / 2, a.y + a.h / 2
    b_cx, b_cy = b.x + b.w / 2, b.y + b.h / 2

    # Horizontal overlap check for vertical relations
    hor_overlap = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    hor_overlap_ratio = hor_overlap / min(a.w, b.w) if min(a.w, b.w) > 0 else 0

    # Vertical overlap check for horizontal relations
    ver_overlap = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    ver_overlap_ratio = ver_overlap / min(a.h, b.h) if min(a.h, b.h) > 0 else 0

    if hor_overlap_ratio >= _OVERLAP_THRESHOLD:
        # Vertical alignment — ABOVE or BELOW
        if a_cy > b_cy:
            return SpatialRelation.ABOVE, a_cy - b_cy - (b.h + a.h) / 2
        else:
            return SpatialRelation.BELOW, b_cy - a_cy - (a.h + b.h) / 2

    if ver_overlap_ratio >= _OVERLAP_THRESHOLD:
        # Horizontal alignment — LEFT_OF or RIGHT_OF
        if a_cx < b_cx:
            return SpatialRelation.LEFT_OF, b_cx - a_cx - (a.w + b.w) / 2
        else:
            return SpatialRelation.RIGHT_OF, a_cx - b_cx - (b.w + a.w) / 2

    return None, 0.0


# ---------------------------------------------------------------------------
# Device alignment + proximity (within same cabinet)
# ---------------------------------------------------------------------------

def _add_device_relations(graph: SpatialGraph,
                           all_nodes: list[LayoutNode]) -> None:
    """Compute ALIGNED_VERT / ALIGNED_HORZ / NEAR for device pairs."""
    _id_map = {n.id: n for n in all_nodes}
    cabinets = [n for n in all_nodes
                if n.node_type == LayoutNodeType.CABINET]
    processed: set[tuple[str, str]] = set()

    for cab in cabinets:
        devices = _collect_devices(cab)
        if len(devices) < 2:
            continue
        if len(devices) * (len(devices) - 1) / 2 > _MAX_DEVICE_PAIRS:
            continue

        for i, da in enumerate(devices):
            da_ln = _id_map.get(da)
            if da_ln is None:
                continue
            for db in devices[i + 1:]:
                db_ln = _id_map.get(db)
                if db_ln is None:
                    continue
                pair = (da, db) if da < db else (db, da)
                if pair in processed:
                    continue
                processed.add(pair)
                _add_alignment_or_near(graph, da_ln, db_ln)


def _collect_devices(cab: LayoutNode) -> list[str]:
    """Recursively collect DEVICE node ids under a cabinet."""
    result: list[str] = []
    stack = [cab]
    while stack:
        node = stack.pop()
        if node.node_type == LayoutNodeType.DEVICE:
            result.append(node.id)
        stack.extend(node.children)
    return result


def _add_alignment_or_near(graph: SpatialGraph,
                            a: LayoutNode, b: LayoutNode) -> None:
    """Add an ALIGNED or NEAR edge between two layout nodes."""
    a_cx, a_cy = a.bbox.x + a.bbox.w / 2, a.bbox.y + a.bbox.h / 2
    b_cx, b_cy = b.bbox.x + b.bbox.w / 2, b.bbox.y + b.bbox.h / 2

    # Vertical alignment — same column
    if abs(a_cx - b_cx) <= _ALIGN_TOLERANCE:
        dist = abs(a_cy - b_cy) - (a.bbox.h + b.bbox.h) / 2
        rel = SpatialRelation.ALIGNED_VERT
        _add_edge(graph, rel, a.id, b.id, distance=max(0, dist))
        return

    # Horizontal alignment — same row
    if abs(a_cy - b_cy) <= _ALIGN_TOLERANCE:
        dist = abs(a_cx - b_cx) - (a.bbox.w + b.bbox.w) / 2
        rel = SpatialRelation.ALIGNED_HORZ
        _add_edge(graph, rel, a.id, b.id, distance=max(0, dist))
        return

    # NEAR — close but not aligned
    centroid_dist = ((a_cx - b_cx) ** 2 + (a_cy - b_cy) ** 2) ** 0.5
    if centroid_dist <= _NEAR_THRESHOLD:
        _add_edge(graph, SpatialRelation.NEAR, a.id, b.id,
                  distance=centroid_dist)


def _add_edge(graph: SpatialGraph, rel: SpatialRelation,
              src: str, dst: str, distance: float = 0.0) -> None:
    graph.add_edge(SpatialEdge(
        source_id=src, target_id=dst,
        relation=rel, distance=round(distance, 1),
    ))


__all__ = ['lift']
