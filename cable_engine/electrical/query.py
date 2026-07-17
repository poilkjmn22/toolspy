"""cable_engine.electrical.query — ElectricalQuery.

Composes GeometryGraph + Visitors + Resolvers into business-oriented queries.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from .geometry_graph import GeoEdgeType, GeoNodeType, GeometryGraph
from .resolvers import TerminalResolver, TerminalResult


class ElectricalQuery:
    """High-level query API that combines graph traversal + resolution.

    This is the layer CircuitLoopAnalyzer calls — it does NOT know
    about GeometryGraph internals.
    """

    def __init__(self, graph: GeometryGraph):
        self._graph = graph
        self._terminal_resolver = TerminalResolver(graph)

    def _get_context_tags(self, cable_id: str) -> Optional[list[tuple[float, float]]]:
        """Find WIRECODE/WIRETYPE tag positions for a cable.

        Used as context_tags in find_wire_near to disambiguate buses
        when the WS is roughly equidistant from two different buses.
        """
        tags: list[tuple[float, float]] = []
        for node in self._graph.nodes.values():
            if node.node_type != GeoNodeType.TAG:
                continue
            if node.tag_name == 'WIRECODE' and node.tag_text == cable_id:
                tags.append((node.x, node.y))
                wc_x, wc_y = node.x, node.y
                break
        if not tags:
            return None
        # Find nearby WIRETYPE tag
        wc_x, wc_y = tags[0]
        for node in self._graph.nodes.values():
            if node.node_type == GeoNodeType.TAG and node.tag_name == 'WIRETYPE':
                if abs(node.x - wc_x) < 50 and abs(node.y - wc_y) < 50:
                    tags.append((node.x, node.y))
                    break
        return tags

    def find_terminal(
        self, wx: float, wy: float, side: str,
        max_steps: int = 30,
        cable_id: Optional[str] = None,
    ) -> Optional[TerminalResult]:
        """Find the terminal on the specified side of (wx, wy).

        Algorithm:
          1. find_wire_near — locate the bus edge covering (wx, wy)
          2. walk_to_endpoint — follow the wire chain to the endpoint
          3. If endpoint is a CIRCLE node → resolve directly
          4. Otherwise, DFS outward with direction constraint
             until a CIRCLE node is found
        """
        context_tags = self._get_context_tags(cable_id) if cable_id else None
        wire_id = self._graph.find_wire_near(wx, wy, context_tags=context_tags)
        if wire_id is None:
            return None

        endpoint = self._graph.walk_to_endpoint(wire_id, side)
        end_node = self._graph.node(endpoint)
        if end_node is None:
            return None

        # If endpoint is a CIRCLE (terminal icon), resolve directly
        if end_node.node_type == GeoNodeType.CIRCLE:
            return self._terminal_resolver.resolve_at(end_node.x, end_node.y)

        # Direction-constrained DFS from endpoint to find CIRCLE
        result = self._dfs_to_terminal(endpoint, side, max_steps)
        if result is not None:
            return result

        # Fallback: resolve at the endpoint position
        return self._terminal_resolver.resolve_at(end_node.x, end_node.y)

    def _dfs_to_terminal(
        self, start_id: int, side: str, max_steps: int,
    ) -> Optional[TerminalResult]:
        """BFS outward from start_id, direction-constrained, stop at CIRCLE."""
        start_node = self._graph.node(start_id)
        if start_node is None:
            return None

        start_x = start_node.x
        visited: set[int] = {start_id}
        queue: deque[tuple[int, float, int]] = deque()
        queue.append((start_id, start_x, 0))

        while queue:
            nid, current_x, depth = queue.popleft()
            node = self._graph.node(nid)
            if node is None:
                continue

            if node.node_type == GeoNodeType.CIRCLE:
                result = self._terminal_resolver.resolve_at(node.x, node.y)
                if result is not None and result.number:
                    return result

            if depth >= max_steps:
                continue

            for nb, eid in self._graph.neighbors(nid):
                if nb in visited:
                    continue
                edge = self._graph.edges.get(eid)
                if edge is None or edge.edge_type not in (
                        GeoEdgeType.SEGMENT, GeoEdgeType.CONDUCTING):
                    continue
                nb_node = self._graph.node(nb)
                if nb_node is None:
                    continue

                nb_x = nb_node.x
                if side == 'left' and nb_x > current_x + 5:
                    continue
                if side == 'right' and nb_x < current_x - 5:
                    continue

                visited.add(nb)
                new_x = (min(current_x, nb_x) if side == 'left'
                         else max(current_x, nb_x))
                queue.append((nb, new_x, depth + 1))

        return None
