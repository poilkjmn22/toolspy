"""Material table parser (设备材料表 / 材料表).

Parses bill-of-materials tables found on PANEL_POSITION drawings.
Columns: 序号 / 名称 / 图例 / 单位 / 数量 / 备注.
(Title text "设  备  材  料  表".)
"""

from __future__ import annotations

import re
from typing import Optional

from cable_engine.ir import Document, TextEntity, AttributeEntity
from cable_engine.ir.entities import BBox
from ..base import BaseTableParser
from ..text_utils import _default_noise, collect_texts, find_header_row, map_column_roles, y_bucket_rows


_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'名[\s]*称|名[称稱]'), 'name'),
    (re.compile(r'单位|單[位]'), 'unit'),
    (re.compile(r'数量|數[量]'), 'qty'),
    (re.compile(r'备[\s]*注|備[注]|说明|說明'), 'remark'),
    (re.compile(r'序号|序號|顺序|順[序]'), 'index'),
    (re.compile(r'图例'), 'legend'),
]

_MATERIAL_PATTERN = re.compile(r'材料表')


def _find_material_table_bbox(doc: Document) -> Optional[BBox]:
    """Find the 设备材料表 title text and return an offset bbox."""
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        raw = (e.text or '').strip()
        if not raw or not _MATERIAL_PATTERN.search(raw.replace(' ', '')):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        return BBox(float(ex) - 140, float(ey) - 170, 300, 200)
    return None


class MaterialTableParser(BaseTableParser):
    """Parser for 设备材料表 (equipment/material bill-of-materials table)."""

    HEADER_PATTERNS = _HEADER_PATTERNS
    ROW_TOL = 5.0
    MAX_TEXT_LEN = 60

    def noise_filter(self, text: str) -> bool:
        if _default_noise(text):
            return True
        if re.match(r'^[A-Z]$', text.strip()):
            return True
        return False

    def detect_bboxes(self, doc, **kwargs):
        bbox = _find_material_table_bbox(doc)
        if bbox is not None:
            return [(bbox, 0.4)]
        return []

    def extract_data(
        self,
        rows, col_roles, header_cells, bbox, doc,
        gap_x=None, texts=None, header_idx=None, confidence=0.0,
        **kwargs,
    ) -> Optional[dict]:
        if header_idx is None:
            return None
        header_xs = [x for x, _ in header_cells]

        result_rows: list[dict] = []
        for row in rows[header_idx + 1:]:
            cells_by_header: dict[str, str] = {}
            for cx, t in row:
                if not t.strip():
                    continue
                distances = [abs(cx - hx) for hx in header_xs]
                best = min(range(len(distances)), key=lambda i: distances[i])
                ci, dist = best, distances[best]
                if dist > 50:
                    continue
                role = col_roles.get(ci, '')
                if not role:
                    continue
                cur = cells_by_header.get(role, '')
                cells_by_header[role] = f'{cur} {t}'.strip() if cur else t

            if 'name' not in cells_by_header and 'qty' not in cells_by_header:
                continue

            name = cells_by_header.get('name', '')
            unit = cells_by_header.get('unit', '')
            qty_raw = cells_by_header.get('qty', '0')
            try:
                qty = int(qty_raw)
            except ValueError:
                qty = 0
            remark = cells_by_header.get('remark', '')
            index = cells_by_header.get('index', '')

            result_rows.append({
                'index': index,
                'name': name,
                'unit': unit,
                'qty': qty,
                'remark': remark,
            })

        if not result_rows:
            return None
        return {'bbox': bbox, 'rows': result_rows}


def parse_material_table(doc: Document) -> Optional[dict]:
    """Legacy-compatible wrapper. Returns dict with 'bbox' and 'rows'."""
    parser = MaterialTableParser()
    return parser.parse(doc)


__all__ = ['MaterialTableParser', 'parse_material_table']
