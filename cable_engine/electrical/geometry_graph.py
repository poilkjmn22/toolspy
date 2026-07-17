"""cable_engine.electrical.geometry_graph — GeometryGraph + GeometryBuilder.

Pure geometry-layer graph: knows nodes, edges, spatial index, and trace().
Zero business semantics — no Terminal, Cabinet, Device, Loop.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from ..ir import (
    AttributeEntity, CircleGeometry, Document, LineGeometry,
    TextEntity, BBox,
)
from ..ir import CabinetRegion as _CabReg_pre
from .graph_path import GraphPath, TraceStopReason


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------

class GeoNodeType(Enum):
    WIRE_VERTEX = 'WIRE_VERTEX'
    CIRCLE = 'CIRCLE'
    TAG = 'TAG'
    CABINET = 'CABINET'
    TEXT = 'TEXT'


@dataclass
class GeoNode:
    id: int
    x: float
    y: float
    node_type: GeoNodeType

    # TAG-specific
    tag_name: str = ''
    tag_text: str = ''

    # CABINET-specific
    bbox: Optional[BBox] = None
    display_name: str = ''


# ---------------------------------------------------------------------------
# Edge types
# ---------------------------------------------------------------------------

class GeoEdgeType(Enum):
    SEGMENT = 'SEGMENT'         # raw line/polyline connectivity
    CONDUCTING = 'CONDUCTING'   # electrical wire (marked by WireBuilder)


@dataclass
class GeoEdge:
    id: int
    node_a: int
    node_b: int
    edge_type: GeoEdgeType
    length: float = 0.0


# ---------------------------------------------------------------------------
# Spatial index (grid-based)
# ---------------------------------------------------------------------------

class SpatialIndex:
    """Grid-based spatial index. Internal member of GeometryGraph."""

    def __init__(self, graph: GeometryGraph, cell_size: float = 50.0):
        self._graph = graph
        self._cell_size = cell_size
        self._cells: dict[tuple[int, int], list[int]] = {}

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return (int(math.floor(x / self._cell_size)),
                int(math.floor(y / self._cell_size)))

    def insert(self, node_id: int, x: float, y: float) -> None:
        self._cells.setdefault(self._cell(x, y), []).append(node_id)

    def _node_ids_in_radius(self, x: float, y: float,
                            radius: float) -> list[int]:
        cx, cy = self._cell(x, y)
        span = int(math.ceil(radius / self._cell_size)) + 1
        seen: set[int] = set()
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for nid in self._cells.get((cx + dx, cy + dy), []):
                    if nid not in seen:
                        seen.add(nid)
        return list(seen)

    def lookup(
        self, x: float, y: float, radius: float,
        filter_fn: Optional[Callable[[GeoNode], bool]] = None,
    ) -> list[GeoNode]:
        result: list[GeoNode] = []
        for nid in self._node_ids_in_radius(x, y, radius):
            node = self._graph.nodes.get(nid)
            if node is None:
                continue
            d = math.hypot(node.x - x, node.y - y)
            if d <= radius and (filter_fn is None or filter_fn(node)):
                result.append(node)
        return result

    def nearest(
        self, x: float, y: float, radius: float,
        filter_fn: Optional[Callable[[GeoNode], bool]] = None,
    ) -> Optional[GeoNode]:
        best: Optional[GeoNode] = None
        best_d = float('inf')
        for nid in self._node_ids_in_radius(x, y, radius):
            node = self._graph.nodes.get(nid)
            if node is None:
                continue
            d = math.hypot(node.x - x, node.y - y)
            if d <= radius and d < best_d and (filter_fn is None or filter_fn(node)):
                best_d = d
                best = node
        return best

    def cabinet_at(self, x: float, y: float) -> Optional[GeoNode]:
        """Find the cabinet whose bbox contains (x, y).

        First tries spatial lookup (fast for cabinets near query point).
        Falls back to linear scan of all CABINET nodes (handles tall
        cabinets whose center is far from the bbox).
        """
        # Quick spatial lookup
        for nid in self._node_ids_in_radius(x, y, self._cell_size):
            node = self._graph.nodes.get(nid)
            if node is None or node.node_type != GeoNodeType.CABINET:
                continue
            if node.bbox is None:
                continue
            if (node.bbox.x <= x <= node.bbox.x + node.bbox.w
                    and node.bbox.y <= y <= node.bbox.y + node.bbox.h):
                return node
        # Linear scan fallback for tall cabinets
        for node in self._graph.nodes.values():
            if node.node_type != GeoNodeType.CABINET:
                continue
            if node.bbox is None:
                continue
            if (node.bbox.x <= x <= node.bbox.x + node.bbox.w
                    and node.bbox.y <= y <= node.bbox.y + node.bbox.h):
                return node
        return None

    def cabinets_containing(self, x: float, y: float) -> list[GeoNode]:
        result: list[GeoNode] = []
        for nid in self._node_ids_in_radius(x, y, self._cell_size):
            node = self._graph.nodes.get(nid)
            if node is None or node.node_type != GeoNodeType.CABINET:
                continue
            if node.bbox is None:
                continue
            if (node.bbox.x <= x <= node.bbox.x + node.bbox.w
                    and node.bbox.y <= y <= node.bbox.y + node.bbox.h):
                result.append(node)
        return result


# ---------------------------------------------------------------------------
# Visitor interface
# ---------------------------------------------------------------------------

@dataclass
class VisitDecision:
    """Returned by Visitor.visit() — guides the trace search."""
    stop: bool = False


class Visitor:
    """Interface for graph traversal consumers.

    Subclass and override visit() to implement custom stop logic.
    """

    def start(self, node: GeoNode) -> None:
        """Called once before traversal begins."""
        pass

    def visit(self, node: GeoNode, depth: int) -> VisitDecision:
        """Called for each visited node. Return stop=True to halt."""
        return VisitDecision(stop=False)


# ---------------------------------------------------------------------------
# GeometryGraph
# ---------------------------------------------------------------------------

class GeometryGraph:
    """Pure geometry graph — nodes, edges, spatial index, traversal.

    Build via GeometryBuilder. Enhance via WireBuilder / CabinetBuilder.
    """

    def __init__(self):
        self.nodes: dict[int, GeoNode] = {}
        self.edges: dict[int, GeoEdge] = {}
        self.adj: dict[int, list[tuple[int, int]]] = {}
        # key: node_id → [(neighbor_id, edge_id)]
        self.spatial = SpatialIndex(self)
        self._next_id: int = 0
        self._next_edge_id: int = 0

    # -- Node management ------------------------------------------------

    def next_node_id(self) -> int:
        nid = self._next_id
        self._next_id += 1
        return nid

    def add_node(self, x: float, y: float, node_type: GeoNodeType,
                 **kw: Any) -> int:
        nid = self.next_node_id()
        self.nodes[nid] = GeoNode(id=nid, x=x, y=y, node_type=node_type, **kw)
        self.spatial.insert(nid, x, y)
        return nid

    def node(self, nid: int) -> Optional[GeoNode]:
        return self.nodes.get(nid)

    # -- Edge management ------------------------------------------------

    def add_edge(self, a: int, b: int, edge_type: GeoEdgeType,
                 **kw: Any) -> int:
        eid = self._next_edge_id
        self._next_edge_id += 1
        dx = self.nodes[a].x - self.nodes[b].x
        dy = self.nodes[a].y - self.nodes[b].y
        length = kw.pop('length', math.hypot(dx, dy))
        self.edges[eid] = GeoEdge(
            id=eid, node_a=a, node_b=b, edge_type=edge_type,
            length=length, **kw,
        )
        self.adj.setdefault(a, []).append((b, eid))
        self.adj.setdefault(b, []).append((a, eid))
        return eid

    def neighbors(self, nid: int) -> list[tuple[int, int]]:
        """Return [(neighbor_id, edge_id), ...] for the given node."""
        return list(self.adj.get(nid, []))

    # -- Merge close nodes ----------------------------------------------

    def merge_close_nodes(self, tolerance: float = 0.5) -> None:
        """Merge nodes within tolerance into the existing lower-id node."""
        merged: dict[int, int] = {}
        all_ids = sorted(self.nodes.keys())
        for i, a in enumerate(all_ids):
            if a in merged:
                continue
            na = self.nodes[a]
            for b in all_ids[i + 1:]:
                if b in merged:
                    continue
                nb = self.nodes[b]
                if abs(na.x - nb.x) <= tolerance and abs(na.y - nb.y) <= tolerance:
                    merged[b] = a
        if not merged:
            return
        old_to_new = merged
        # Rewire edges
        for eid, edge in list(self.edges.items()):
            na = old_to_new.get(edge.node_a, edge.node_a)
            nb = old_to_new.get(edge.node_b, edge.node_b)
            if na == nb:
                del self.edges[eid]
                continue
            edge.node_a = na
            edge.node_b = nb
        # Rebuild adjacency
        self.adj.clear()
        for eid, edge in self.edges.items():
            self.adj.setdefault(edge.node_a, []).append((edge.node_b, eid))
            self.adj.setdefault(edge.node_b, []).append((edge.node_a, eid))
        # Remove merged nodes
        for old, new in old_to_new.items():
            if old != new:
                self.nodes.pop(old, None)

    # -- Traversal ------------------------------------------------------

    def trace(
        self,
        start_id: int,
        visitor: Visitor,
        max_steps: int = 20,
    ) -> GraphPath:
        """BFS from start_id, calling visitor at each node.

        Returns a semantic-free GraphPath describing the traversal result.
        """
        start_node = self.nodes.get(start_id)
        if start_node is None:
            return GraphPath(reason=TraceStopReason.NO_PATH)

        visitor.start(start_node)

        visited: set[int] = {start_id}
        parent: dict[int, tuple[int, int]] = {}  # child → (parent, edge_id)
        queue: deque[tuple[int, int]] = deque()  # (node_id, depth)

        for nid, eid in self.neighbors(start_id):
            if nid not in visited:
                visited.add(nid)
                parent[nid] = (start_id, eid)
                queue.append((nid, 1))

        while queue:
            nid, depth = queue.popleft()
            node = self.nodes[nid]
            if node is None:
                continue

            decision = visitor.visit(node, depth)
            if decision.stop:
                return self._build_path(start_id, nid, parent,
                                        TraceStopReason.VISITOR_STOP)

            if depth >= max_steps:
                return self._build_path(start_id, nid, parent,
                                        TraceStopReason.MAX_DEPTH)

            for nb, eid in self.neighbors(nid):
                if nb not in visited:
                    visited.add(nb)
                    parent[nb] = (nid, eid)
                    queue.append((nb, depth + 1))

        # Exhausted the component
        if parent:
            last = nid  # type: ignore
            return self._build_path(start_id, last, parent,
                                    TraceStopReason.DEAD_END)
        return GraphPath(
            nodes=[start_id],
            stop_node=start_id,
            reason=TraceStopReason.DEAD_END,
        )

    def _build_path(
        self,
        start_id: int,
        stop_id: int,
        parent: dict[int, tuple[int, int]],
        reason: TraceStopReason,
    ) -> GraphPath:
        nodes: list[int] = []
        edges: list[int] = []
        cost = 0.0
        cur = stop_id
        while cur in parent:
            par, eid = parent[cur]
            nodes.append(cur)
            edges.append(eid)
            edge = self.edges.get(eid)
            if edge is not None:
                cost += edge.length
            cur = par
        nodes.append(start_id)
        nodes.reverse()
        edges.reverse()
        return GraphPath(
            nodes=nodes,
            edges=edges,
            cost=cost,
            stop_node=stop_id,
            reason=reason,
        )

    # -- Wire lookup ----------------------------------------------------

    def nearest_wire_node(
        self, x: float, y: float, tol: float = 30.0,
    ) -> Optional[int]:
        """Find the closest node on a conducting or segment edge."""
        best: Optional[int] = None
        best_d = float('inf')
        for nid in self.spatial._node_ids_in_radius(x, y, tol):
            node = self.nodes.get(nid)
            if node is None:
                continue
            # Must have at least one SEGMENT or CONDUCTING edge
            has_wire = any(
                self.edges.get(eid, GeoEdge(0, 0, 0, GeoEdgeType.SEGMENT)).edge_type
                in (GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING)
                for _, eid in self.adj.get(nid, [])
            )
            if not has_wire:
                continue
            d = math.hypot(node.x - x, node.y - y)
            if d < best_d and d <= tol:
                best_d = d
                best = nid
        return best

    def find_wire_near(
        self, x: float, y: float, tol: float = 30.0,
        context_tags: Optional[list[tuple[float, float]]] = None,
    ) -> Optional[int]:
        """Find the best wire node near (x, y).

        Edge-based: primary criterion is y-match (dy); x-outside-span is
        a weak tiebreaker (weighted at 0.1). This lets WS columns that
        overflow a short bus span still find the correct horizontal bus.

        When context_tags (WIRECODE/WIRETYPE positions) are provided, edges
        whose x-span contains a context tag get a strong score bonus,
        disambiguating cases where the WS is roughly equidistant from two
        buses (e.g. 5071-506 at x=-349 between left/right buses at y=-36).
        Falls back to node-based nearest_wire_node when no edge matches.
        """
        best_edge_nid: Optional[int] = None
        best_edge_score = float('inf')
        best_edge_dy = float('inf')
        for eid, edge in self.edges.items():
            if edge.edge_type not in (
                    GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING):
                continue
            na = self.nodes.get(edge.node_a)
            nb = self.nodes.get(edge.node_b)
            if na is None or nb is None:
                continue
            y_mean = (na.y + nb.y) / 2
            if abs(y_mean - y) > tol:
                continue
            dy = abs(y_mean - y)
            x_min = min(na.x, nb.x)
            x_max = max(na.x, nb.x)
            x_outside = 0.0
            if x < x_min:
                x_outside = x_min - x
            elif x > x_max:
                x_outside = x - x_max
            score = dy + x_outside * 0.1
            # Strong bonus when a WIRECODE/WIRETYPE tag falls inside
            # this edge's x-span — the cable's text block is positioned
            # above the bus it belongs to. The y-threshold of 40 covers
            # the full vertical span of all bus lines for a cable (the
            # tag is ~30-40 units above the deepest bus line).
            if context_tags:
                for tx, ty in context_tags:
                    if x_min <= tx <= x_max and abs(ty - y_mean) < 40:
                        score -= 3.0
                        break
            if score < best_edge_score:
                best_edge_score = score
                best_edge_dy = dy
                d_to_a = math.hypot(na.x - x, na.y - y)
                d_to_b = math.hypot(nb.x - x, nb.y - y)
                # Prefer non-CIRCLE endpoints so find_wire_near returns a
                # bus node, not the terminal icon itself.
                a_is_circle = na.node_type == GeoNodeType.CIRCLE
                b_is_circle = nb.node_type == GeoNodeType.CIRCLE
                if a_is_circle and not b_is_circle:
                    best_edge_nid = edge.node_b
                elif b_is_circle and not a_is_circle:
                    best_edge_nid = edge.node_a
                else:
                    best_edge_nid = edge.node_a if d_to_a <= d_to_b else edge.node_b

        # Node-based fallback: only when no edge matched.
        # When both exist, the edge result is always preferred — it found a
        # horizontal bus line by y-proximity, which is the semantically
        # correct entity to walk on. The node fallback typically captures
        # nearby vertical wire segments that are not useful for bus walking.
        if best_edge_nid is not None:
            return best_edge_nid
        return self.nearest_wire_node(x, y, tol)

    def wire_endpoint(
        self, start_id: int, direction: str,
    ) -> int:
        """Walk along a wire chain to the endpoint in the given direction.

        direction='left': x-decreasing. direction='right': x-increasing.
        Stops at CIRCLE nodes (terminal icons).
        """
        current = start_id
        visited: set[int] = set()
        while True:
            visited.add(current)
            if self.nodes[current].node_type == GeoNodeType.CIRCLE:
                return current
            cx = self.nodes[current].x
            candidates: list[tuple[int, int, float]] = []
            for nb, eid in self.adj.get(current, []):
                if nb in visited:
                    continue
                edge = self.edges.get(eid)
                if edge is None or edge.edge_type not in (
                        GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING):
                    continue
                nb_x = self.nodes[nb].x
                if direction == 'left' and nb_x < cx:
                    candidates.append((nb, eid, nb_x))
                elif direction == 'right' and nb_x > cx:
                    candidates.append((nb, eid, nb_x))

            if not candidates:
                return current

            candidates.sort(key=lambda t: t[2], reverse=(direction == 'right'))
            current = candidates[0][0]

    def walk_to_endpoint(
        self, start_id: int, direction: str,
    ) -> int:
        """Walk along degree-2 wire chain to endpoint, handling junctions.

        Stops at junctions (degree != 2 in wire edges), at CIRCLE nodes,
        and returns that node.
        """
        current = start_id
        visited: set[int] = set()
        while True:
            visited.add(current)
            if self.nodes[current].node_type == GeoNodeType.CIRCLE:
                return current
            cx = self.nodes[current].x
            wire_nbs: list[tuple[int, int, float]] = []
            for nb, eid in self.adj.get(current, []):
                if nb in visited:
                    continue
                edge = self.edges.get(eid)
                if edge is None or edge.edge_type not in (
                        GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING):
                    continue
                nb_x = self.nodes[nb].x
                if direction == 'left' and nb_x < cx:
                    wire_nbs.append((nb, eid, nb_x))
                elif direction == 'right' and nb_x > cx:
                    wire_nbs.append((nb, eid, nb_x))

            if len(wire_nbs) > 1:
                return current
            if not wire_nbs:
                return current
            current = wire_nbs[0][0]


# ---------------------------------------------------------------------------
# GeometryBuilder — builds GeometryGraph from Document IR
# ---------------------------------------------------------------------------

class GeometryBuilder:
    """Builds a pure geometry graph from Document IR entities."""

    def __init__(self):
        self._graph = GeometryGraph()
        self._cab_handles: set[str] = set()
        self._cab_y_edges: list[tuple[float, float, float]] = []

    def build(self, doc: Document) -> GeometryGraph:
        g = self._graph

        # Pass 1: collect cabinet boundary info
        for e in doc.entities:
            if not isinstance(e, _CabReg_pre):
                continue
            if e.bbox is None:
                continue
            if e.boundary_handle:
                self._cab_handles.add(e.boundary_handle)
            self._cab_y_edges.append(
                (e.bbox.y, e.bbox.x, e.bbox.x + e.bbox.w)
            )
            self._cab_y_edges.append(
                (e.bbox.y + e.bbox.h, e.bbox.x, e.bbox.x + e.bbox.w)
            )

        # Pass 2: add CabinetRegion as CABINET nodes
        for e in doc.entities:
            if not isinstance(e, _CabReg_pre):
                continue
            if e.bbox is None:
                continue
            cx = e.bbox.x + e.bbox.w / 2
            cy = e.bbox.y + e.bbox.h / 2
            g.add_node(
                cx, cy, GeoNodeType.CABINET,
                bbox=e.bbox,
                display_name=e.display_name,
            )

        # Pass 3: process geometry entities
        for e in doc.entities:
            if isinstance(e, LineGeometry):
                self._process_line(e)
            elif isinstance(e, CircleGeometry):
                self._process_circle(e)
            elif isinstance(e, AttributeEntity):
                self._process_attribute(e)
            elif isinstance(e, TextEntity):
                self._process_text(e)

        # Merge close nodes
        g.merge_close_nodes(tolerance=0.5)

        # Connect nodes with wire edges to nearby CIRCLE nodes (terminal icons)
        # Must run AFTER merge_close_nodes: merged WIRE_VERTEX→TAG nodes
        # still have wire edges and need CIRCLE connectivity.
        for nid in list(g.nodes.keys()):
            node = g.nodes[nid]
            if node is None or node.node_type == GeoNodeType.CIRCLE:
                continue
            if not any(
                    g.edges.get(eid, GeoEdge(0, 0, 0, GeoEdgeType.SEGMENT)).edge_type
                    in (GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING)
                    for _, eid in g.adj.get(nid, [])):
                continue
            for c_node in g.spatial.lookup(
                    node.x, node.y, 2.0,
                    filter_fn=lambda n: n.node_type == GeoNodeType.CIRCLE):
                if any(nb == c_node.id for nb, _ in g.neighbors(nid)):
                    continue
                g.add_edge(nid, c_node.id, GeoEdgeType.SEGMENT)

        # Connect CIRCLE nodes that lie along SEGMENT edges (pass-through).
        # When a wire segment passes through a terminal circle (not at an
        # endpoint), split the segment at the closest point and connect.
        self._connect_circles_through_segments(g)

        return g

    def _connect_circles_through_segments(self, g: GeometryGraph) -> None:
        tol = 2.0
        for cid, c_node in list(g.nodes.items()):
            if c_node.node_type != GeoNodeType.CIRCLE:
                continue
            # Skip if CIRCLE already has a wire connection (endpoint case)
            if any(
                    g.edges.get(eid, GeoEdge(0, 0, 0, GeoEdgeType.SEGMENT)).edge_type
                    in (GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING)
                    for _, eid in g.adj.get(cid, [])):
                continue

            for eid, edge in list(g.edges.items()):
                if edge.edge_type not in (
                        GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING):
                    continue
                na = g.nodes.get(edge.node_a)
                nb = g.nodes.get(edge.node_b)
                if na is None or nb is None:
                    continue

                ax, ay = na.x, na.y
                bx, by = nb.x, nb.y
                px, py = c_node.x, c_node.y

                # Point-to-segment distance
                vx, vy = bx - ax, by - ay
                length_sq = vx * vx + vy * vy
                if length_sq < 1e-12:
                    continue
                t = ((px - ax) * vx + (py - ay) * vy) / length_sq
                t_clamped = max(0.0, min(1.0, t))
                cx = ax + t_clamped * vx
                cy = ay + t_clamped * vy
                dist = math.hypot(px - cx, py - cy)

                if dist > tol:
                    continue
                # Skip endpoint cases (already handled by the loop above)
                if t_clamped < 0.01 or t_clamped > 0.99:
                    continue

                # Split the segment: insert WIRE_VERTEX at closest point
                new_nid = g.add_node(cx, cy, GeoNodeType.WIRE_VERTEX)
                g.add_edge(edge.node_a, new_nid, GeoEdgeType.SEGMENT)
                g.add_edge(new_nid, edge.node_b, GeoEdgeType.SEGMENT)
                g.add_edge(new_nid, cid, GeoEdgeType.SEGMENT)
                # Remove old edge
                for a_id in (edge.node_a, edge.node_b):
                    g.adj[a_id] = [(nb, eid2)
                                   for nb, eid2 in g.adj.get(a_id, [])
                                   if eid2 != eid]
                del g.edges[eid]
                # Continue checking other segments for this CIRCLE

    def _is_cabinet_edge(self, y: float, x_min: float, x_max: float) -> bool:
        for ey, ex_min, ex_max in self._cab_y_edges:
            if abs(y - ey) > 0.5:
                continue
            if abs(x_min - ex_min) > 2:
                continue
            if abs(x_max - ex_max) > 2:
                continue
            return True
        return False

    def _process_line(self, e: LineGeometry) -> None:
        pts = list(e.points or [])
        if len(pts) < 2:
            return

        if e.handle in self._cab_handles:
            return

        g = self._graph
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            dx = abs(p1.x - p2.x)
            dy = abs(p1.y - p2.y)

            # Horizontal segment (bus wire) or vertical segment (connector)
            if not ((dy <= 3 and dx > 2) or (dx <= 3 and dy > 2)):
                continue

            # Skip cabinet bbox edges
            if self._is_cabinet_edge(
                    p1.y if dy <= 3 else (p1.y + p2.y) / 2,
                    min(p1.x, p2.x), max(p1.x, p2.x)):
                continue

            nids: list[int] = []
            for pt in (p1, p2):
                existing = g.spatial.lookup(
                    pt.x, pt.y, 0.5,
                    filter_fn=lambda n: n.node_type == GeoNodeType.WIRE_VERTEX,
                )
                if existing:
                    nids.append(existing[0].id)
                else:
                    nids.append(g.add_node(pt.x, pt.y, GeoNodeType.WIRE_VERTEX))

            if nids[0] != nids[1]:
                g.add_edge(nids[0], nids[1], GeoEdgeType.SEGMENT)

    def _process_circle(self, e: CircleGeometry) -> None:
        if e.center is None:
            return
        self._graph.add_node(
            e.center.x, e.center.y, GeoNodeType.CIRCLE,
        )

    def _process_attribute(self, e: AttributeEntity) -> None:
        cf = getattr(e, 'custom_fields', None) or {}
        x = cf.get('x')
        y = cf.get('y')
        if x is None or y is None:
            return
        if e.confidence < 0.5:
            return
        text = (e.text or '').strip()
        self._graph.add_node(
            x, y, GeoNodeType.TAG,
            tag_name=e.tag or '',
            tag_text=text,
        )

    def _process_text(self, e: TextEntity) -> None:
        if e.confidence < 0.5:
            return
        cf = getattr(e, 'custom_fields', None) or {}
        x = cf.get('x')
        y = cf.get('y')
        if x is None or y is None:
            return
        self._graph.add_node(
            x, y, GeoNodeType.TEXT,
            tag_text=(e.text or '').strip(),
        )
