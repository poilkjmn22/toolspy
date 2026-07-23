"""cable_engine.layout — LayoutTree: spatial-containment tree for panel layout.

Layers:
  types.py           — LayoutTree / LayoutNode / LayoutNodeType (legacy compat)
  model.py           — LayoutNode / LayoutNodeType / LayoutGroupType (canonical)
  grouping/          — DeviceSpatialGraph, detect_layout_groups
  semantics/         — DeviceSemanticResolver, GroupSemanticResolver
  detector.py        — build_layout_tree(doc) → LayoutTree from Document IR
  stage.py           — LayoutStage plugging into the pipeline

V8.2 adds LayoutGroup:
  GROUP nodes sit between PANEL_AREA / CABINET and DEVICE nodes,
  representing spatial clusters (e.g. a vertical column of terminals).
  They carry a group_type (VERTICAL_COLUMN / HORIZONTAL_ROW / GRID / FREEFORM)
  and a semantic type (TERMINAL_COLUMN / DEVICE_PANEL / etc.).

Quick start:
    from cable_engine.layout import build_layout_tree, LayoutTree, LayoutNodeType
    tree = build_layout_tree(doc)
    print(tree.dump())
"""

from .cabinet import PhysicalCabinet, cabinets_from_tree
from .types import LayoutNode, LayoutNodeType, LayoutTree
from .model import LayoutGroupType
from .detector import build_layout_tree
from .stage import LayoutStage

__all__ = [
    'LayoutNode', 'LayoutNodeType', 'LayoutTree',
    'LayoutGroupType',
    'build_layout_tree', 'LayoutStage',
    'PhysicalCabinet', 'cabinets_from_tree',
]
