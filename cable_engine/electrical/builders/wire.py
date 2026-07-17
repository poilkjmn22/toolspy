"""WireBuilder — marks all SEGMENT edges as CONDUCTING.

In V8 the "which wire is the right one" question is answered by
walk_to_endpoint + trace, not by filtering. Every geometry line
that reached GeometryBuilder is a real wire — let the graph query
pick the correct path.
"""

from __future__ import annotations

from ..geometry_graph import GeoEdgeType, GeometryGraph


class WireBuilder:
    """Marks every SEGMENT edge in the graph as CONDUCTING.

    No span/region filter — those heuristics belonged to V7's manual
    core_line selection and are replaced by graph traversal.
    """

    def __init__(self, graph: GeometryGraph):
        self._graph = graph

    def run(self) -> None:
        g = self._graph
        for eid, edge in list(g.edges.items()):
            if edge.edge_type == GeoEdgeType.SEGMENT:
                edge.edge_type = GeoEdgeType.CONDUCTING
