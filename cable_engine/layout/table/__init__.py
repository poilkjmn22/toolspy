"""cable_engine.layout.table — Unified table parsing for all document types.

Pipeline::

    detect_table_regions(doc, bbox) → list[BBox]
    parse_table_at(doc, bbox)       → TableArea (equipment tables)
    parse_usage_table(doc)          → UsageTable (position tables)
    parse_schedule_table(doc)       → list[dict] (cable schedule records)
    match_to_devices(table, devices)→ int (inject metadata into candidates)
"""

from .model import TableArea, TableRow, TableCell
from .detector import detect_table_regions
from .parser import parse_table_at
from .matcher import match_to_devices
from .parsers.usage import parse_usage_table
from .parsers.schedule import parse_schedule_table
from .text_utils import collect_texts, y_bucket_rows, find_header_row, map_column_roles

__all__ = [
    'TableArea', 'TableRow', 'TableCell',
    'detect_table_regions',
    'parse_table_at',
    'match_to_devices',
    'parse_usage_table',
    'parse_schedule_table',
    'collect_texts', 'y_bucket_rows', 'find_header_row', 'map_column_roles',
]
