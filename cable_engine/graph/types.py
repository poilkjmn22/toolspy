"""cable_engine.graph.types — V5 DocumentGraph data types.

The DocumentGraph is the FIRST-CLASS artifact of V5. It is built ONCE
by the GraphBuilder stage and then queried by the Rule Engine.

Design points:
  - Binary edges only (src, dst, type). Higher-arity relationships
    are encoded by properties on the edge or by intermediate nodes.
  - Indices are pre-built at construction time so that rule queries
    are O(degree) instead of O(N).
  - Graph is per-document. Cross-document merging happens lazily
    in the Knowledge Service at query time (Q4).
  - The graph mirrors V4's `graph_nodes` / `graph_edges` tables in
    spirit, but with a richer schema (typed nodes, typed edges, bbox,
    layer). Persisted to `graph_nodes_v5` / `graph_edges_v5`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..ir.entities import BBox, Point


# ---------------------------------------------------------------------------
# Edge type vocabulary (closed set — extend only by adding new constants)
# ---------------------------------------------------------------------------
class EdgeType:
    """Closed set of edge types emitted by the GraphBuilder.

    The Rule Engine queries by edge type. New edge types are added by
    adding a new constant here AND emitting them in the GraphBuilder.
    """
    NEAR = 'near'                      # spatial proximity (text-text, text-line, etc.)
    SHARES_ENDPOINT = 'shares_endpoint'  # two lines share a point (within r_corner)
    ATTRIB_OF = 'attrib_of'             # AttributeEntity belongs to BlockRef
    INSIDE = 'inside'                   # entity is geometrically inside another (block bbox)
    ON_LAYER = 'on_layer'               # entity is on a layer (degenerate; usually denormalized)
    BETWEEN = 'between'                 # a terminal sits between two lines on the same x/y
    CONTAINS = 'contains'               # V6.6: cabinet → terminal / device (containment by bbox)


# ---------------------------------------------------------------------------
# Node type vocabulary
# ---------------------------------------------------------------------------
class NodeType:
    """Closed set of node types stored in the graph.

    V6.6 added `CABINET` for the dashed-rectangle-detected cabinet
    regions. Each CABINET node carries its full bbox in `properties`
    so spatial queries don't need a separate index."""
    TEXT = 'text'
    LINE = 'line'
    CIRCLE = 'circle'
    ARC = 'arc'
    BLOCK = 'block'
    ATTRIB = 'attrib'
    CABINET = 'cabinet'


# ---------------------------------------------------------------------------
# GraphNode
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GraphNode:
    """A node in the DocumentGraph.

    Mirrors an entity in the Document IR, but is addressable (has a
    stable id within the document), indexable, and persistable.

    `properties` carries type-specific extras (DWG handle, layer,
    raw text, attributes, etc.).
    """
    id: str                              # stable within document
    document_hash: str
    page: int
    node_type: str                       # NodeType.* value
    label: Optional[str] = None          # for text/attrib: the rendered string
    x: Optional[float] = None            # canonical position (start for lines)
    y: Optional[float] = None
    bbox: Optional[BBox] = None
    layer: str = ''
    properties: dict = field(default_factory=dict)

    @property
    def pos(self) -> Optional[Point]:
        if self.x is None or self.y is None:
            return None
        return Point(self.x, self.y)


# ---------------------------------------------------------------------------
# GraphEdge
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GraphEdge:
    """A typed, weighted relationship between two nodes.

    Edges are directed. For symmetric relationships (e.g. NEAR) we emit
    two edges (A->B and B->A) so traversals in both directions are O(1).
    """
    src: str                             # GraphNode.id
    dst: str
    edge_type: str                       # EdgeType.* value
    confidence: float = 1.0
    properties: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# DocumentGraph
# ---------------------------------------------------------------------------
class DocumentGraph:
    """First-class graph artifact. Built once at IR time, queried many
    times by Rule Engine and Knowledge Service.

    All public mutation methods (`add_node`, `add_edge`) are O(1) and
    update the indices. Query methods (`neighbors`, `by_type`, etc.) are
    O(degree) or O(1).
    """

    def __init__(self, document_hash: str) -> None:
        self.document_hash = document_hash
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []
        # Indices
        self._by_type: dict[str, list[str]] = {}
        self._by_layer: dict[str, list[str]] = {}
        # outgoing_neighbors[node_id] = [(neighbor_id, edge_type, props)]
        self._out_neighbors: dict[str, list[tuple[str, str, dict]]] = {}
        # incoming_neighbors for symmetry-based queries
        self._in_neighbors: dict[str, list[tuple[str, str, dict]]] = {}
        # spatial index (lazy) for radius queries
        self._spatial: Optional['_SpatialIndex'] = None

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def add_node(self, node: GraphNode) -> None:
        if node.id in self._nodes:
            return
        self._nodes[node.id] = node
        self._by_type.setdefault(node.node_type, []).append(node.id)
        if node.layer:
            self._by_layer.setdefault(node.layer, []).append(node.id)
        self._spatial = None  # invalidate

    def add_edge(self, edge: GraphEdge) -> None:
        # Symmetric NEAR: emit two directed edges so traversal is O(1)
        # in both directions. Caller still passes the canonical (src, dst)
        # which is what gets persisted; we add the reverse ourselves.
        self._edges.append(edge)
        self._out_neighbors.setdefault(edge.src, []).append(
            (edge.dst, edge.edge_type, edge.properties)
        )
        self._in_neighbors.setdefault(edge.dst, []).append(
            (edge.src, edge.edge_type, edge.properties)
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def nodes(self) -> Iterator[GraphNode]:
        return iter(self._nodes.values())

    @property
    def edges(self) -> Iterator[GraphEdge]:
        return iter(self._edges)

    def get(self, node_id: str) -> Optional[GraphNode]:
        return self._nodes.get(node_id)

    def by_node_type(self, node_type: str) -> list[GraphNode]:
        return [self._nodes[i] for i in self._by_type.get(node_type, [])]

    def by_layer(self, layer: str) -> list[GraphNode]:
        return [self._nodes[i] for i in self._by_layer.get(layer, [])]

    def neighbors(
        self,
        node_id: str,
        edge_type: Optional[str] = None,
    ) -> list[tuple[GraphNode, str, dict]]:
        """Return (neighbor_node, edge_type, edge_properties) tuples for
        outgoing edges from `node_id`. If `edge_type` is given, filter
        by edge type.
        """
        out: list[tuple[GraphNode, str, dict]] = []
        for nid, et, props in self._out_neighbors.get(node_id, []):
            if edge_type is not None and et != edge_type:
                continue
            n = self._nodes.get(nid)
            if n is not None:
                out.append((n, et, props))
        return out

    def incoming(
        self,
        node_id: str,
        edge_type: Optional[str] = None,
    ) -> list[tuple[GraphNode, str, dict]]:
        """Like neighbors() but follow incoming edges."""
        out: list[tuple[GraphNode, str, dict]] = []
        for nid, et, props in self._in_neighbors.get(node_id, []):
            if edge_type is not None and et != edge_type:
                continue
            n = self._nodes.get(nid)
            if n is not None:
                out.append((n, et, props))
        return out

    def nodes_within(
        self,
        center: Point,
        radius: float,
        node_type: Optional[str] = None,
    ) -> list[GraphNode]:
        """Spatial query: return nodes within `radius` of `center`.

        Uses a simple uniform-grid spatial index built lazily on first
        call. For our documents (a few thousand nodes) the brute-force
        scan is also fast enough; we use the grid only when the
        document is large.
        """
        if self._spatial is None and len(self._nodes) > 500:
            from .spatial import SpatialIndex
            self._spatial = SpatialIndex(self._nodes.values())
        if self._spatial is not None:
            ids = self._spatial.query_ids(center, radius)
        else:
            ids = list(self._nodes.keys())
        out: list[GraphNode] = []
        for nid in ids:
            n = self._nodes.get(nid)
            if n is None:
                continue
            if n.x is None or n.y is None:
                continue
            if node_type is not None and n.node_type != node_type:
                continue
            out.append(n)
        return out

    def edges_by_type(self, edge_type: str) -> list[GraphEdge]:
        return [e for e in self._edges if e.edge_type == edge_type]

    # ------------------------------------------------------------------
    # Bulk access for persistence
    # ------------------------------------------------------------------
    def all_nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())

    def all_edges(self) -> list[GraphEdge]:
        return list(self._edges)


__all__ = [
    'EdgeType', 'NodeType',
    'GraphNode', 'GraphEdge',
    'DocumentGraph',
]