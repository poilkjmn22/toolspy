"""cable_engine.layout.structure — Spatial structure analyzers.

Each analyzer classifies a set of DeviceCandidate positions into
a spatial pattern. The analyzers are called by DBSCANClusterer's
post-classification step (``_classify_group``).

Available analyzers:

  ColumnAnalyzer — x-aligned devices → VERTICAL_COLUMN
  RowAnalyzer    — y-aligned devices → HORIZONTAL_ROW
  GridAnalyzer   — cols×rows layout  → GRID

Usage::

    from .structure import ColumnAnalyzer, RowAnalyzer, GridAnalyzer

    score, evidence = ColumnAnalyzer().analyze(cxs, cys, widths, heights, cab_bbox)
    is_grid, dims = GridAnalyzer().analyze(cxs, cys, len(devs))
"""

from .column import ColumnAnalyzer
from .grid import GridAnalyzer
from .row import RowAnalyzer

__all__ = [
    'ColumnAnalyzer',
    'RowAnalyzer',
    'GridAnalyzer',
]
