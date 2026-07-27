"""cable_engine.layout.position.builder — 构建 PANEL_POSITION LayoutTree."""

from __future__ import annotations

from uuid import uuid4
from typing import Optional

from ...ir import Document
from ..model import LayoutNode, LayoutNodeType, LayoutTree
from .crossref import cross_reference
from .detector import cluster_rows, detect_cells, detect_room, find_f_texts
from .model import PositionRow
from .parser import parse_usage_table


def _uid() -> str:
    return uuid4().hex[:12]


def build_position_tree(doc: Document) -> Optional[LayoutTree]:
    """Run the full PANEL_POSITION pipeline and return a LayoutTree.

    Phases:
      0. detect_room  → ROOM boundary
      1. detect_cells → POSITION_CELL[] (rects + F-number texts)
      2. cluster_rows → POSITION_ROW[]
      3. parse_usage_table → UsageTable (right-side table)
      4. cross_reference → merge equipment into cells

    Tree structure: ROOT → ROOM → POSITION_ROW[] → POSITION_CELL[]
    """
    f_texts = find_f_texts(doc)

    room = detect_room(doc, f_texts)
    if room is None:
        return None

    cells = detect_cells(doc, room, f_texts)
    if not cells:
        return None

    rows = cluster_rows(cells)
    if not rows:
        return None

    table = parse_usage_table(doc, room)
    if table is not None:
        cross_reference(rows, table)

    room_node = LayoutNode(id=_uid(), node_type=LayoutNodeType.ROOM, bbox=room)

    for row in rows:
        row_node = LayoutNode(
            id=_uid(),
            node_type=LayoutNodeType.POSITION_ROW,
            bbox=row.bbox,
        )
        row_node.data['row_index'] = row.row_index
        room_node.children.append(row_node)
        for cell in row.cells:
            cell_node = LayoutNode(
                id=_uid(),
                node_type=LayoutNodeType.POSITION_CELL,
                bbox=cell.bbox,
                name=cell.label,
            )
            cell_node.data['equipment'] = cell.equipment
            cell_node.data['qty'] = str(cell.qty)
            cell_node.data['remark'] = cell.remark
            cell_node.data['row_index'] = cell.row_index
            cell_node.data['col_index'] = cell.col_index
            cell_node.data['group_index'] = cell.group_index
            row_node.children.append(cell_node)

    tree = LayoutTree(roots=[room_node])
    if table is not None:
        tree.meta['usage_table'] = _table_to_dict(table)
    return tree


def _table_to_dict(table) -> dict:
    return {
        'bbox': [table.bbox.x, table.bbox.y, table.bbox.w, table.bbox.h],
        'rows': [
            {'cell_label': r.cell_label, 'equipment': r.equipment,
             'qty': r.qty, 'remark': r.remark}
            for r in table.rows
        ],
    }


__all__ = ['build_position_tree']
