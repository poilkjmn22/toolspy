"""CabinetBuilder — ensures cabinet nodes are in the spatial index.

Cabinet nodes are already created by GeometryBuilder. This builder
is a no-op hook for future cabinet-related graph enhancements.
"""

from __future__ import annotations

from ..geometry_graph import GeometryGraph


class CabinetBuilder:
    """Post-processes cabinet nodes in the graph.

    Currently a no-op: GeometryBuilder already creates CABINET nodes
    and they are registered in the spatial index. Future enhancements
    may add containment edges or cabinet adjacency links.
    """

    def __init__(self, graph: GeometryGraph):
        self._graph = graph

    def run(self) -> None:
        pass
