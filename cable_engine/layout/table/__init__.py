"""cable_engine.layout.table — Equipment table parser for PANEL_LAYOUT.

Parses device tables (设备表 / 材料表) found on the right side of panel
layout drawings. Each table row corresponds to one device in the layout,
carrying real business metadata (model, description, quantity).

Pipeline::

    detect_tables(doc, cabinet_bbox)  →  list[BBox]  (table regions)
    parse_table_at(doc, table_bbox)   →  TableArea   (structured rows)
    match_to_devices(table, devices)  →  dict[str, TableRow] (name → row)

Integration into build_layout_tree::

    table = parse_equipment_table(doc, cabinet_bbox)
    if table:
        match_to_devices(table, device_candidates)
        # table info flows into DeviceCandidate.features['table_info']
"""

from .model import TableArea, TableRow, TableCell
from .detector import detect_table_regions
from .parser import parse_table_at
from .matcher import match_to_devices

__all__ = [
    'TableArea', 'TableRow', 'TableCell',
    'detect_table_regions',
    'parse_table_at',
    'match_to_devices',
]
