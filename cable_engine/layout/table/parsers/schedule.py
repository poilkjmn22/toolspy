"""Cable schedule table parser (电缆清册 / 接线表 / 电缆联系图).

These drawings are typically tabular: each row is a cable, columns
carry cable_id, conductor_no, terminal_from, terminal_to, etc.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from cable_engine.ir import Document
from cable_engine.ir.entities import BBox
from ..base import BaseTableParser
from ..text_utils import collect_texts, y_bucket_rows, find_header_row, map_column_roles


_CABLE_ID_LIKE = re.compile(r'\b([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})\b')

_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'电缆编号|电缆编[号碼]|编[号號]|序号|序號|电缆(?:名称|ID|编号)'), 'cable_id'),
    (re.compile(r'电缆型号|型号|规格|电[缆线]型号'), 'circuit_desc'),
    (re.compile(r'起点(?:柜|端子)?|起始|始端|来源|來[源渊]|本端'), 'strip_name'),
    (re.compile(r'终点(?:柜|端子)?|終点|末端|目标|目的|对端'), 'terminal_no_remote'),
    (re.compile(r'芯数|线芯|线[芯心]数|芯线数|缆芯'), 'conductor_no'),
    (re.compile(r'回路(?:编号|编[号號])?|回[路線]编号'), 'loop_id'),
    (re.compile(r'备注|注|说明|說[明文]'), 'circuit_desc'),
    (re.compile(r'柜体|机柜|安装位置|所在柜|所属柜'), 'cabinet_name'),
]

_TOPOLOGY_FIELDS = [
    'cable_id', 'conductor_no', 'strip_name', 'terminal_no',
    'terminal_no_remote', 'cabinet_name', 'cabinet_name_remote',
    'circuit_desc', 'loop_id',
]


class ScheduleParser(BaseTableParser):
    """Parser for cable schedule tables (电缆清册)."""

    HEADER_PATTERNS = _HEADER_PATTERNS
    ROW_TOL = 3.0
    MAX_TEXT_LEN = 80

    def detect_bboxes(self, doc, **kwargs):
        """Cable schedule: no bbox — process all entities."""
        return [(None, 0.5)]

    def extract_data(
        self,
        rows, col_roles, header_cells, bbox, doc,
        gap_x=None, texts=None, header_idx=None, confidence=0.0,
        **kwargs,
    ) -> Optional[list[dict]]:
        ordered_roles = sorted(col_roles.items(), key=lambda x: x[0])

        cid_col = next((ci for ci, role in ordered_roles if role == 'cable_id'), None)
        if cid_col is None:
            return None

        records: list[dict] = []
        data_rows = rows[header_idx + 1:] if header_idx is not None else []

        for row in data_rows:
            cell_by_x = {ci: t for ci, (_, t) in enumerate(row)}
            cid = cell_by_x.get(cid_col, '').strip()
            if not cid or not _CABLE_ID_LIKE.fullmatch(cid):
                continue

            rec = self._empty_record()
            rec['cable_id'] = cid
            rec['source_type'] = 'cable_schedule'

            for ci, role in ordered_roles:
                if role == 'cable_id':
                    continue
                val = cell_by_x.get(ci, '').strip()
                if not val:
                    continue
                if role == 'conductor_no':
                    try:
                        rec['conductor_no'] = int(val)
                    except ValueError:
                        rec['circuit_desc'] = (rec.get('circuit_desc') or '') + f' {val}'
                elif role == 'circuit_desc':
                    existing = rec.get('circuit_desc') or ''
                    rec['circuit_desc'] = (existing + ' ' + val).strip()
                elif role in rec:
                    rec[role] = val

            records.append(rec)

        return records if records else None

    @staticmethod
    def _empty_record() -> dict:
        return {f: None for f in _TOPOLOGY_FIELDS}


def extract_cable_ids_fallback(texts: list[tuple[float, float, str]]) -> list[dict]:
    """Fallback: extract distinct cable IDs from text when table parse fails."""
    seen: set[str] = set()
    for _, _, t in texts:
        for m in _CABLE_ID_LIKE.finditer(t):
            seen.add(m.group(1))
    return [
        {f: (cid if f == 'cable_id' else None) for f in _TOPOLOGY_FIELDS}
        | {'source_type': 'cable_schedule'}
        for cid in sorted(seen)
    ]


def parse_schedule_table(doc: Document) -> Optional[list[dict]]:
    """Parse cable schedule table, return list of topology records."""
    parser = ScheduleParser()
    result = parser.parse(doc)
    if result is not None:
        return result
    texts = collect_texts(doc, max_len=80)
    if texts:
        return extract_cable_ids_fallback(texts)
    return None


__all__ = ['ScheduleParser', 'parse_schedule_table', 'extract_cable_ids_fallback']
