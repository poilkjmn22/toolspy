"""GridAnalyzer — cols×rows layout → GRID."""

from __future__ import annotations

from typing import Optional


class GridAnalyzer:
    """Check whether a set of device positions forms a regular grid.

    A grid requires:
      - At least 4 devices
      - At least 2 unique x-clusters (cols) and 2 unique y-clusters (rows)
      - Exact fill: cols × rows == device count

    Returns (is_grid, grid_dims) where grid_dims = {'cols': N, 'rows': M}
    or ({}, None) when the set does not form a grid.
    """

    def analyze(
        self,
        cxs: list[float],
        cys: list[float],
        n_devices: int,
    ) -> tuple[bool, dict]:
        if n_devices < 4:
            return False, {}
        ux = sorted(set(round(c, 1) for c in cxs))
        uy = sorted(set(round(c, 1) for c in cys), reverse=True)
        nx, ny = len(ux), len(uy)
        if nx < 2 or ny < 2:
            return False, {}
        if nx * ny != n_devices:
            return False, {}
        return True, {'cols': nx, 'rows': ny}


__all__ = ['GridAnalyzer']
