"""cable_engine.layout.spatial — SpatialGraph: spatial relations between layout nodes.

Lifts the hierarchical LayoutTree into a flat graph with spatial relations:
  - CONTAINS        — parent→child containment
  - LEFT_OF / RIGHT_OF   — horizontal adjacency (siblings)
  - ABOVE / BELOW        — vertical adjacency (siblings)
  - ALIGNED_VERT         — devices sharing the same column
  - ALIGNED_HORZ         — devices sharing the same row
  - NEAR                 — close but not aligned

Usage::

    from cable_engine.layout.spatial import lift, SpatialGraph
    graph = lift(tree)
    print(graph.dump())
"""

from .model import SpatialNode, SpatialEdge, SpatialGraph, SpatialRelation
from .bridge import lift

__all__ = [
    'SpatialNode', 'SpatialEdge', 'SpatialGraph', 'SpatialRelation',
    'lift',
]
