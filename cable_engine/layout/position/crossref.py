"""cable_engine.layout.position.crossref — F 编号 ↔ 屏屏用途一览表交叉关联."""

from __future__ import annotations

from .model import PositionRow, UsageTable


def cross_reference(rows: list[PositionRow], table: UsageTable) -> None:
    """Merge usage table equipment info into PositionCell.equipment.

    Matches by F-number (cell_label ↔ PositionCell.label).
    Mutates PositionCell objects in-place.
    """
    if not table.rows:
        return

    lookup = {r.cell_label: r for r in table.rows if r.cell_label}

    for row in rows:
        for cell in row.cells:
            if cell.label and cell.label in lookup:
                utr = lookup[cell.label]
                cell.equipment = utr.equipment
                cell.qty = utr.qty
                cell.remark = utr.remark


__all__ = ['cross_reference']
