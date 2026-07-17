"""TerminalResolver — converts a GraphPath endpoint into a Terminal."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..geometry_graph import GeoNodeType, GeometryGraph, GraphPath


@dataclass
class TerminalResult:
    """A resolved terminal — the business object at the end of a graph path.

    Attributes:
        number:     Terminal ID (e.g. "X2:1").
        x, y:       Position of the NO tag (or path endpoint if no tag).
        cabinet:    Cabinet display name (e.g. "11003-ZXW").
    """
    number: str = ''
    x: float = 0.0
    y: float = 0.0
    cabinet: str = ''


class TerminalResolver:
    """Resolves the endpoint of a GraphPath into a Terminal.

    Clustering is query-time (not build-time):
      1. Find the path endpoint node (or a given coordinate)
      2. Spatial-lookup nearby NO/ObjTerm.Name tags (radius 2.0)
      3. Spatial-lookup nearby CircleGeometry (radius 2.0)
      4. Combine into a TerminalResult
    """

    def __init__(self, graph: GeometryGraph, cluster_radius: float = 3.0):
        self._graph = graph
        self._radius = cluster_radius

    def resolve(self, path: GraphPath) -> Optional[TerminalResult]:
        end_id = path.stop_node
        end_node = self._graph.node(end_id)
        if end_node is None:
            return None
        return self.resolve_at(end_node.x, end_node.y)

    def resolve_at(self, x: float, y: float) -> Optional[TerminalResult]:
        # Step 1: Find the closest CIRCLE (terminal icon marker) within 8 units
        best_circle: Optional[GeoNode] = None
        best_cd = float('inf')
        for c_node in self._graph.spatial.lookup(
                x, y, 8.0,
                filter_fn=lambda n: n.node_type == GeoNodeType.CIRCLE):
            d = math.hypot(c_node.x - x, c_node.y - y)
            if d < best_cd:
                best_cd = d
                best_circle = c_node
        anchor_x = best_circle.x if best_circle else x
        anchor_y = best_circle.y if best_circle else y

        # Pre-compute cabinet bbox for tag filtering (avoids picking up
        # tags from neighboring cabinets on the same drawing)
        cab_bbox: Optional[tuple[float, float, float, float]] = None
        cab_node = self._graph.spatial.cabinet_at(anchor_x, anchor_y)
        if cab_node is not None and cab_node.bbox is not None:
            cab_bbox = (cab_node.bbox.x, cab_node.bbox.y,
                        cab_node.bbox.x + cab_node.bbox.w,
                        cab_node.bbox.y + cab_node.bbox.h)

        # Step 2: Find the closest NO/ObjTerm.Name tag near the anchor
        # (within 12 units, optionally restricted to cabinet bbox)
        best_tag: Optional[GeoNode] = None
        best_d = float('inf')
        for tag_node in self._graph.spatial.lookup(
                anchor_x, anchor_y, 12.0,
                filter_fn=lambda n: (
                    n.node_type == GeoNodeType.TAG
                    and n.tag_name in ('NO', 'ObjTerm.Name')
                    and ':' in n.tag_text
                )):
            if cab_bbox is not None:
                tx, ty = tag_node.x, tag_node.y
                cx0, cy0, cx1, cy1 = cab_bbox
                if not (cx0 <= tx <= cx1 and cy0 <= ty <= cy1):
                    continue
            d = math.hypot(tag_node.x - anchor_x, tag_node.y - anchor_y)
            if d < best_d:
                best_d = d
                best_tag = tag_node

        cabinet_name = cab_node.display_name if cab_node is not None else ''

        return TerminalResult(
            number=best_tag.tag_text if best_tag else '',
            x=best_tag.x if best_tag else anchor_x,
            y=best_tag.y if best_tag else anchor_y,
            cabinet=cabinet_name,
        )
