"""cable_engine.layout.grouping — Device spatial clustering.

Takes DEVICE LayoutNodes and groups them by spatial patterns:
  VERTICAL_COLUMN — vertically-aligned, regularly-spaced column
  HORIZONTAL_ROW — horizontally-aligned, regularly-spaced row
  GRID — 2D grid arrangement
  FREEFORM — fallback
"""

from .spatial_graph import DeviceSpatialGraph
from .clustering import detect_layout_groups

__all__ = [
    'DeviceSpatialGraph',
    'detect_layout_groups',
]
