"""cable_engine.graph.spatial — Uniform-grid spatial index.

Used lazily by DocumentGraph.nodes_within() when the graph is large
(>500 nodes). The grid cell size is set to the query radius so each
query touches only cells within `radius` of the center.

Returns NODE IDS (strings); the caller resolves them to GraphNodes via
the DocumentGraph's own index. This avoids cross-module cache state.
"""

from __future__ import annotations

from typing import Iterable

from ..ir.entities import Point


class SpatialIndex:
    """Uniform-grid spatial index keyed by node id.

    Stores `(x, y)` per id; the caller does the id -> GraphNode lookup.
    """

    def __init__(self, nodes: Iterable, cell_size: float = 50.0) -> None:
        self._cell_size = cell_size
        self._cells: dict[tuple[int, int], list[str]] = {}
        self._positions: dict[str, Point] = {}
        for n in nodes:
            x = getattr(n, 'x', None)
            y = getattr(n, 'y', None)
            if x is None or y is None:
                continue
            nid = getattr(n, 'id', None)
            if nid is None:
                continue
            self._positions[nid] = Point(x, y)
            cx, cy = self._cell_coords(x, y)
            self._cells.setdefault((cx, cy), []).append(nid)

    def _cell_coords(self, x: float, y: float) -> tuple[int, int]:
        s = self._cell_size
        return (int(x // s), int(y // s))

    def query_ids(self, center: Point, radius: float) -> list[str]:
        """Return ids of nodes whose position might be within `radius`
        of `center`. Coarse filter — the caller does the precise check.
        """
        rs = self._cell_size
        cells_to_check: set[tuple[int, int]] = set()
        cx0, cy0 = self._cell_coords(center.x, center.y)
        span = int(radius // rs) + 2
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                cells_to_check.add((cx0 + dx, cy0 + dy))
        ids: list[str] = []
        rsq = radius * radius
        for cell in cells_to_check:
            for nid in self._cells.get(cell, ()):
                p = self._positions.get(nid)
                if p is None:
                    continue
                ddx = p.x - center.x
                ddy = p.y - center.y
                if ddx * ddx + ddy * ddy <= rsq:
                    ids.append(nid)
        return ids


__all__ = ['SpatialIndex']