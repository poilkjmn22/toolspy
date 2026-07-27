"""SpatialGraph — spatial relations between LayoutTree nodes.

SpatialGraph is a flat graph (not a tree) that captures geometric
relationships between layout nodes: containment, left/right/above/below
adjacency, vertical/horizontal alignment, and proximity.

Parallel to GeometryGraph (electrical/) but operates on layout semantics
rather than raw geometry entities. Strictly separate — never mix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ...ir.entities import BBox


class SpatialRelation(Enum):
    CONTAINS = 'CONTAINS'
    LEFT_OF = 'LEFT_OF'
    RIGHT_OF = 'RIGHT_OF'
    ABOVE = 'ABOVE'
    BELOW = 'BELOW'
    ALIGNED_VERT = 'ALIGNED_VERT'
    ALIGNED_HORZ = 'ALIGNED_HORZ'
    NEAR = 'NEAR'


@dataclass
class SpatialNode:
    """A node in the SpatialGraph, wrapping a LayoutTree node."""
    node_id: str
    node_type: str
    bbox: BBox
    name: str = ''
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpatialEdge:
    source_id: str
    target_id: str
    relation: SpatialRelation
    distance: float = 0.0
    confidence: float = 1.0

    def reversed(self) -> SpatialEdge:
        rev_map = {
            SpatialRelation.LEFT_OF: SpatialRelation.RIGHT_OF,
            SpatialRelation.RIGHT_OF: SpatialRelation.LEFT_OF,
            SpatialRelation.ABOVE: SpatialRelation.BELOW,
            SpatialRelation.BELOW: SpatialRelation.ABOVE,
        }
        return SpatialEdge(
            source_id=self.target_id,
            target_id=self.source_id,
            relation=rev_map.get(self.relation, self.relation),
            distance=self.distance,
            confidence=self.confidence,
        )


@dataclass
class SpatialGraph:
    nodes: dict[str, SpatialNode] = field(default_factory=dict)
    edges: list[SpatialEdge] = field(default_factory=list)

    def add_node(self, node: SpatialNode) -> None:
        self.nodes[node.node_id] = node

    def add_edge(self, edge: SpatialEdge) -> None:
        self.edges.append(edge)

    def neighbors(self, node_id: str) -> list[tuple[str, SpatialEdge]]:
        result: list[tuple[str, SpatialEdge]] = []
        for e in self.edges:
            if e.source_id == node_id:
                result.append((e.target_id, e))
        return result

    def relations_of(self, node_id: str,
                     relation: SpatialRelation) -> list[tuple[str, SpatialEdge]]:
        return [(t, e) for t, e in self.neighbors(node_id)
                if e.relation == relation]

    def query_bbox(self, bbox: BBox) -> list[SpatialNode]:
        return [
            n for n in self.nodes.values()
            if (n.bbox.x < bbox.x + bbox.w
                and n.bbox.x + n.bbox.w > bbox.x
                and n.bbox.y < bbox.y + bbox.h
                and n.bbox.y + n.bbox.h > bbox.y)
        ]

    def query_near(self, center: tuple[float, float],
                   radius: float) -> list[SpatialNode]:
        cx, cy = center
        result: list[SpatialNode] = []
        for n in self.nodes.values():
            nx = n.bbox.x + n.bbox.w / 2
            ny = n.bbox.y + n.bbox.h / 2
            if (nx - cx) ** 2 + (ny - cy) ** 2 <= radius ** 2:
                result.append(n)
        return result

    def dump(self) -> str:
        lines = [f'SpatialGraph ({len(self.nodes)} nodes, {len(self.edges)} edges)']
        for n in self.nodes.values():
            b = n.bbox
            lines.append(f'  {n.node_type} "{n.name}" ({b.x:.0f},{b.y:.0f}) {b.w:.0f}x{b.h:.0f}')
        for e in self.edges:
            lines.append(f'  {e.source_id} ─[{e.relation.value}]→ {e.target_id}  d={e.distance:.0f}')
        return '\n'.join(lines)


__all__ = [
    'SpatialNode', 'SpatialEdge', 'SpatialGraph', 'SpatialRelation',
]
