"""cable_engine.layout — LayoutTree: spatial-containment tree for panel layout.

Layers:
  types.py           — LayoutTree / LayoutNode / LayoutNodeType (legacy compat)
  model.py           — LayoutNode / LayoutNodeType / LayoutGroupType (canonical)
  grouping/          — DeviceSpatialGraph, detect_layout_groups (legacy sweep)
  semantics/         — DeviceSemanticResolver, GroupSemanticResolver
  candidate.py       — DeviceCandidate, CandidatePool (V8.1)
  clustering.py      — DBSCANClusterer (V8.1)
  associator.py      — TextAssociator (V8.1)
  detector.py        — build_layout_tree(doc) → LayoutTree from Document IR
  stage.py           — LayoutStage plugging into the pipeline

V8.1 adds CandidatePool + DBSCAN clustering:
  ClosedRectDetector  ─→ DeviceCandidate(0.95)
  OpenShapeDetector   ─→ DeviceCandidate(L=0.5, U=0.7)
  CircleDetector      ─→ SymbolCandidate → DeviceCandidate(0.60)
  TextDetector        ─→ DeviceCandidate(0.40)
          ↓
    CandidatePool.dedup()
          ↓
    DBSCANClusterer(eps=30, min_samples=2)
          ↓
    DeviceGroup (COLUMN / ROW / GRID / FREEFORM)

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
