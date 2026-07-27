"""Table data model for PANEL_LAYOUT equipment tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...ir.entities import BBox


@dataclass
class TableCell:
    text: str
    x: float
    y: float
    col_index: int
    row_index: int


@dataclass
class TableRow:
    cells: list[TableCell] = field(default_factory=list)
    y: float = 0.0
    header: bool = False

    def cell_text(self, col_index: int) -> str:
        for c in self.cells:
            if c.col_index == col_index:
                return c.text
        return ''


@dataclass
class TableArea:
    bbox: BBox
    rows: list[TableRow] = field(default_factory=list)
    header_row: Optional[TableRow] = None
    header_columns: list[str] = field(default_factory=list)
    name_column_index: int = -1
    model_column_index: int = -1
    desc_column_index: int = -1
    qty_column_index: int = -1

    @property
    def data_rows(self) -> list[TableRow]:
        return [r for r in self.rows if not r.header]


__all__ = ['TableCell', 'TableRow', 'TableArea']
