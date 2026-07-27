"""Table row/column/cell extraction for PANEL_LAYOUT equipment tables.

Parses text entities inside a detected table bounding box into
a structured ``TableArea`` with header detection and column typing.
"""

from __future__ import annotations

import re
from typing import Optional, Protocol

from ...ir import Document, AttributeEntity, TextEntity
from ...ir.entities import BBox
from .model import TableArea, TableCell, TableRow


_ROW_TOL = 3.0          # Y-tolerance for grouping into the same row

# Chinese column header keywords for equipment tables (设备表 / 材料表)
# (regex_pattern, column_role)
_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'序号|序號|编号|编[号號]|顺序'), 'index'),
    (re.compile(r'名称|名[称稱]|符号|符號|代号'), 'name'),
    (re.compile(r'型号|型[号號]|规格|规[格範]'), 'model'),
    (re.compile(r'说明|說[明]|描述|用途|备注'), 'desc'),
    (re.compile(r'数量|數[量]|件数'), 'qty'),
    (re.compile(r'安装|位置|柜体|安装位置'), 'position'),
]


def parse_table_at(doc: Document, table_bbox: BBox) -> Optional[TableArea]:
    """Parse text entities within *table_bbox* into a TableArea.

    Returns ``None`` if the text content does not form a valid table
    (fewer than 2 rows, no header detected, etc.).
    """
    texts = _collect_texts(doc, table_bbox)
    if len(texts) < 4:
        return None

    # Group texts by Y into row candidates
    rows_by_y: dict[float, list[tuple[float, str]]] = {}
    for ex, ey, t in texts:
        bucket = round(ey / _ROW_TOL) * _ROW_TOL
        rows_by_y.setdefault(bucket, []).append((ex, t))

    if len(rows_by_y) < 2:
        return None

    # Sort rows top-to-bottom (descending Y = CAD top)
    sorted_y = sorted(rows_by_y.keys(), reverse=True)
    row_list: list[TableRow] = []
    for yi, y_bucket in enumerate(sorted_y):
        cells = sorted(rows_by_y[y_bucket], key=lambda c: c[0])
        row = TableRow(y=y_bucket)
        for ci, (cx, text) in enumerate(cells):
            row.cells.append(TableCell(
                text=text, x=cx, y=y_bucket,
                col_index=ci, row_index=yi,
            ))
        row_list.append(row)

    # Detect header row — first row that contains Chinese column keywords
    header_idx = _find_header_row(row_list)
    if header_idx is None:
        return None

    header_row = row_list[header_idx]
    header_row.header = True
    header_columns = [c.text for c in header_row.cells]

    # Map columns by header text
    col_roles = _map_column_roles(header_columns)

    area = TableArea(
        bbox=table_bbox,
        rows=row_list,
        header_row=header_row,
        header_columns=header_columns,
        name_column_index=col_roles.get('name', -1),
        model_column_index=col_roles.get('model', -1),
        desc_column_index=col_roles.get('desc', -1),
        qty_column_index=col_roles.get('qty', -1),
    )
    return area


def _collect_texts(doc: Document, bbox: BBox) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t or len(t) > 30:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if bbox.x <= ex <= bbox.x + bbox.w and bbox.y <= ey <= bbox.y + bbox.h:
            out.append((ex, ey, t))
    return out


def _find_header_row(rows: list[TableRow]) -> Optional[int]:
    """Return the index of the first row with Chinese column keywords."""
    for i, row in enumerate(rows):
        joined = ' '.join(c.text for c in row.cells)
        for pat, _ in _HEADER_PATTERNS:
            if pat.search(joined):
                return i
    return None


def _map_column_roles(header_texts: list[str]) -> dict[str, int]:
    """Map header text → column role, returning {role: col_index}."""
    roles: dict[str, int] = {}
    for ci, text in enumerate(header_texts):
        for pat, role in _HEADER_PATTERNS:
            if pat.search(text):
                roles[role] = ci
                break
    return roles


__all__ = ['parse_table_at']
