"""cable_engine.layout — LayoutTree: spatial-containment tree for panel layout.

Detection pipeline:
  doc.entities  →  detect_rectangles / detect_long_lines
               →  detect_cabinets  →  detect_areas_v2
               →  candidate.build_device_candidates  →  DBSCANClusterer
               →  TextAssociator  →  GROUP + DEVICE nodes

Candidate sources (pool → dedup → DBSCAN):
  detect_closed_rects  ─→ DeviceCandidate(0.95)
  detect_spine_devices ─→ DeviceCandidate(0.75)
  detect_open_shapes   ─→ DeviceCandidate(L=0.5, U=0.7)
  detect_circle_symbols─→ SymbolCandidate → DeviceCandidate(0.60)
  detect_text_devices  ─→ DeviceCandidate(0.40)

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
from .candidate import DeviceCandidate, SymbolCandidate, CandidatePool
from .clustering import DBSCANClusterer, DeviceGroup
from .associator import TextAssociator

__all__ = [
    'LayoutNode', 'LayoutNodeType', 'LayoutTree',
    'LayoutGroupType',
    'build_layout_tree', 'LayoutStage',
    'PhysicalCabinet', 'cabinets_from_tree',
]
