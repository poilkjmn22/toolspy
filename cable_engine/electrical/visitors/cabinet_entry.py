"""CabinetEntryVisitor — stops when traversal enters a cabinet."""

from __future__ import annotations

from ..geometry_graph import GeoNode, GeometryGraph, VisitDecision, Visitor


class CabinetEntryVisitor(Visitor):
    """Stops when the search crosses from outside a cabinet to inside.

    Tracks the cabinet at each visited node. The first time a node is
    found inside a cabinet (while the previous was outside), it stops.
    """

    def __init__(self, graph: GeometryGraph):
        self._graph = graph
        self._cab_before: GeoNode | None = None

    def start(self, node: GeoNode) -> None:
        self._cab_before = self._graph.spatial.cabinet_at(node.x, node.y)

    def visit(self, node: GeoNode, depth: int) -> VisitDecision:
        cab_now = self._graph.spatial.cabinet_at(node.x, node.y)
        if cab_now is not None and cab_now is not self._cab_before:
            return VisitDecision(stop=True)
        self._cab_before = cab_now
        return VisitDecision(stop=False)
