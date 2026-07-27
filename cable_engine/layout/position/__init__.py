"""cable_engine.layout.position — PANEL_POSITION 屏位布置图解析器.

Pipeline:
  build_position_tree(doc) → Optional[LayoutTree]
    ├── detect_room(doc)              → BBox
    ├── detect_cells(doc, room)       → PositionCell[]
    ├── cluster_rows(cells)           → PositionRow[]
    ├── parse_usage_table(doc, room)  → UsageTable
    └── cross_reference(rows, table)  → cell.equipment
"""

from .builder import build_position_tree
from .detector import find_f_texts

__all__ = ['build_position_tree', 'find_f_texts']
