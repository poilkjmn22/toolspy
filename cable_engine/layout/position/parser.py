"""cable_engine.layout.position.parser — 屏屏用途一览表解析.

Legacy shim that delegates to ``table/parsers/usage.py``.
"""

from __future__ import annotations

from typing import Optional

from ...ir import Document
from ...ir.entities import BBox
from ..table.parsers.material import parse_material_table as _parse_material_table
from ..table.parsers.usage import parse_usage_table as _parse_usage_table
from .model import UsageTable, UsageTableRow


def parse_usage_table(doc: Document, room: BBox) -> Optional[UsageTable]:
    """Parse the usage table (屏屏用途一览表).

    Delegates to the unified table parser in ``table/parsers/usage.py``.
    """
    result = _parse_usage_table(doc, room)
    if result is None:
        return None
    rows = [
        UsageTableRow(
            cell_label=r['cell_label'],
            equipment=r['equipment'],
            qty=r['qty'],
            remark=r['remark'],
        )
        for r in result.get('rows', [])
    ]
    bbox_raw = result.get('bbox')
    if isinstance(bbox_raw, (list, tuple)):
        bbox = BBox(*bbox_raw)
    elif isinstance(bbox_raw, BBox):
        bbox = bbox_raw
    else:
        bbox = None
    return UsageTable(bbox=bbox, rows=rows)


def parse_material_table(doc: Document) -> Optional[dict]:
    """Parse 设备材料表. Delegates to ``table/parsers/material.py``."""
    return _parse_material_table(doc)


__all__ = ['parse_material_table', 'parse_usage_table']
