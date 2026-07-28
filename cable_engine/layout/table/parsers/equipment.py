"""Equipment table parser for PANEL_LAYOUT (屏面布置图设备表).

Parses device tables (设备表 / 材料表) found on the right side of panel
layout drawings. Each table row corresponds to one device in the layout,
carrying real business metadata (model, description, quantity).
"""

from __future__ import annotations

import re
from typing import Optional

from cable_engine.ir import Document
from cable_engine.ir.entities import BBox
from ..base import BaseTableParser
from ..model import TableArea, TableCell, TableRow
from ..text_utils import collect_texts, find_header_row, map_column_roles, y_bucket_rows


_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'序号|序號|编[号號]|顺序'), 'index'),
    (re.compile(r'名[\s]*称|名[称稱]|符号|符號|代号'), 'name'),
    (re.compile(r'型[\s]*号|型[号號]|规[\s]*格|规[格範]'), 'model'),
    (re.compile(r'说[\s]*明|說明|描[\s]*述|用[\s]*途|备[\s]*注|備[注]'), 'desc'),
    (re.compile(r'数[\s]*量|數量|件数'), 'qty'),
    (re.compile(r'安[\s]*装|位[\s]*置|柜[\s]*体|安装位置'), 'position'),
]


class EquipmentTableParser(BaseTableParser):
    """Parser for PANEL_LAYOUT equipment tables (设备表 / 材料表)."""

    HEADER_PATTERNS = _HEADER_PATTERNS
    ROW_TOL = 5.0
    MAX_TEXT_LEN = 30

    def extract_data(
        self,
        rows, col_roles, header_cells, bbox, doc,
        gap_x=None, texts=None, header_idx=None, confidence=0.0,
        **kwargs,
    ) -> Optional[TableArea]:
        row_list: list[TableRow] = []
        for yi, row in enumerate(rows):
            tr = TableRow(y=0.0)
            for ci, (cx, t) in enumerate(row):
                cell = TableCell(text=t, x=cx, y=0.0, col_index=ci, row_index=yi)
                tr.cells.append(cell)
            row_list.append(tr)

        if header_idx is None or header_idx >= len(row_list):
            return None
        header_row = row_list[header_idx]
        header_row.header = True
        header_columns = [c.text for c in header_row.cells]

        col_map = {role: idx for idx, role in col_roles.items()}

        return TableArea(
            bbox=bbox,
            rows=row_list,
            header_row=header_row,
            header_columns=header_columns,
            name_column_index=col_map.get('name', -1),
            model_column_index=col_map.get('model', -1),
            desc_column_index=col_map.get('desc', -1),
            qty_column_index=col_map.get('qty', -1),
        )


def _is_noise_cell(t: str) -> bool:
    txt = t.strip()
    if not txt:
        return True
    if re.match(r'^[A-Za-z\-.]+$', txt) and len(txt) <= 8:
        return True
    if txt in ('右侧', '左侧', '正面', '背面', '设  备  表', '设备表', '材料表', '设备材料表', '附件材料表'):
        return True
    return False


def _assign_by_position(
    row: list[tuple[float, str]],
    col_count: int,
    first_col_x: float,
    hdr_xs: Optional[list[float]] = None,
) -> list[str]:
    """Assign texts to columns by nearest-midpoint matching.

    For each text (X-sorted), find the header column whose X is closest.
    Columns are consumed left-to-right: once a column is passed, earlier
    columns can no longer match.  Unmatched columns stay empty.
    """
    sorted_row = sorted(row, key=lambda c: c[0])
    cells: list[str] = ['' for _ in range(col_count)]
    ci = 0
    for cx, t in sorted_row:
        if cx < first_col_x - 15:
            continue
        if hdr_xs:
            best = ci
            best_dist = abs(cx - hdr_xs[ci])
            for j in range(ci, min(ci + 3, col_count)):
                d = abs(cx - hdr_xs[j])
                if d < best_dist - 5:
                    best = j
                    best_dist = d
            ci = best
            if ci < col_count:
                cells[ci] = t
            ci += 1
        else:
            if ci < col_count:
                cells[ci] = t
                ci += 1
            else:
                cells[-1] = t
    return cells


def parse_table_at(doc: Document, table_bbox: BBox) -> Optional[TableArea]:
    """Legacy-compatible wrapper.  Parses equipment table at *table_bbox*.

    This is the function imported by ``cable_engine.layout.detector``
    for the PANEL_LAYOUT pipeline.
    """
    parser = EquipmentTableParser()
    texts = collect_texts(doc, table_bbox, max_len=parser.MAX_TEXT_LEN)
    if len(texts) < 4:
        return None
    rows = y_bucket_rows(texts, parser.ROW_TOL)
    if len(rows) < 2:
        return None
    header_idx = find_header_row(rows, parser.HEADER_PATTERNS)
    if header_idx is None:
        return None
    col_roles = map_column_roles(rows[header_idx], parser.HEADER_PATTERNS)
    if not col_roles:
        return None

    hdr_row = rows[header_idx]
    hdr_xs = [cx for cx, _ in hdr_row]
    first_col_x = hdr_xs[0]
    col_count = len(hdr_row)

    row_list: list[TableRow] = []
    for yi, row in enumerate(rows):
        if yi == header_idx:
            tr = TableRow(y=0.0)
            for ci, (cx, t) in enumerate(row):
                tr.cells.append(TableCell(text=t, x=cx, y=0.0, col_index=ci, row_index=yi))
            tr.header = True
            row_list.append(tr)
        else:
            cells = _assign_by_position(row, col_count, first_col_x, hdr_xs)
            if all(_is_noise_cell(c) for c in cells):
                continue
            tr = TableRow(y=0.0)
            for ci, t in enumerate(cells):
                tr.cells.append(TableCell(text=t, x=0.0, y=0.0, col_index=ci, row_index=yi))
            row_list.append(tr)

    if len(row_list) < 2:
        return None

    col_map = {role: idx for idx, role in col_roles.items()}
    header_row = next((r for r in row_list if r.header), row_list[0] if row_list else None)
    if header_row is None:
        return None
    return TableArea(
        bbox=table_bbox,
        rows=row_list,
        header_row=header_row,
        header_columns=[c.text for c in header_row.cells],
        name_column_index=col_map.get('name', -1),
        model_column_index=col_map.get('model', -1),
        desc_column_index=col_map.get('desc', -1),
        qty_column_index=col_map.get('qty', -1),
    )


__all__ = ['EquipmentTableParser', 'parse_table_at']
