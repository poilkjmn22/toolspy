"""Usage table parser for PANEL_POSITION (屏位布置图屏屏用途一览表).

Detects the usage table region by locating a title ending with ``表``,
then extracts rows by anchoring on screen-number texts (``\\d+[CF]``)
and collecting sibling cells within a Y‑neighbourhood.  Role assignment
uses relative column index within each detected column group (left/right
half for two‑column layouts, whole header for single‑column layouts).
"""

from __future__ import annotations

import re
from typing import Optional

from cable_engine.ir import Document
from cable_engine.ir.entities import BBox
from ..base import BaseTableParser
from ..text_utils import collect_texts, find_header_row, map_column_roles, detect_gap_x


_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'屏[\s]*号'), 'cell_label'),
    (re.compile(r'名[\s]*称'), 'equipment'),
    (re.compile(r'数[\s]*量'), 'qty'),
    (re.compile(r'备[\s]*注'), 'remark'),
]

_LABEL_PATTERN = re.compile(r'^\d+[CF]$')
_ROW_TOL = 6.0


def _build_half_roles(
    header_cells: list[tuple[float, str]],
    col_roles: dict[int, str],
    gap_x: Optional[float],
) -> tuple[list[list[str]], list[Optional[int]]]:
    if gap_x is not None:
        left = sorted([c for c in header_cells if c[0] < gap_x], key=lambda c: c[0])
        halves = [left, sorted([c for c in header_cells if c[0] >= gap_x], key=lambda c: c[0])]
    else:
        halves = [sorted(header_cells, key=lambda c: c[0])]

    result: list[list[str]] = []
    label_positions: list[Optional[int]] = []
    for half in halves:
        roles: list[str] = []
        label_pos: Optional[int] = None
        for ci, (_, text) in enumerate(half):
            role = col_roles.get(ci, '')
            roles.append(role)
            if role == 'cell_label':
                label_pos = ci
        result.append(roles)
        label_positions.append(label_pos)
    return result, label_positions


def _extract_one_row(
    ax: float, ay: float, label: str,
    texts: list[tuple[float, float, str]],
    half_roles: list[list[str]],
    label_positions: list[Optional[int]],
    gap_x: Optional[float],
) -> Optional[dict]:
    gap_left = gap_x - ax if gap_x is not None else None
    half_idx = 1 if gap_left is not None and ax >= gap_x else 0
    h_roles = half_roles[half_idx]
    label_pos = label_positions[half_idx]
    if label_pos is None:
        return None

    nearby = [
        (ex, t) for ex, ey, t in texts
        if abs(ey - ay) <= _ROW_TOL
        and t != label
        and not _LABEL_PATTERN.match(t)
        and (gap_x is None or (half_idx == 0 and ex < gap_x) or (half_idx == 1 and ex >= gap_x))
    ]
    nearby.sort(key=lambda c: c[0])

    equipment = ''
    qty = 0
    remark = ''

    for ri, (ex, t) in enumerate(nearby):
        gh = label_pos
        role_idx = ri if ri < gh else ri + 1
        if role_idx >= len(h_roles):
            continue
        role = h_roles[role_idx]
        if role == 'equipment' and not equipment:
            equipment = t
        elif role == 'qty':
            try:
                qty += int(t)
            except ValueError:
                pass
        elif role == 'remark':
            if t not in remark:
                remark = f'{remark} / {t}' if remark else t

    return {'cell_label': label, 'equipment': equipment, 'qty': qty, 'remark': remark}


def _num(label: str) -> int:
    m = re.match(r'(\d+)', label)
    return int(m.group(1)) if m else 0


class UsageTableParser(BaseTableParser):
    """Parser for PANEL_POSITION usage tables (屏屏用途一览表)."""

    HEADER_PATTERNS = _HEADER_PATTERNS
    ROW_TOL = _ROW_TOL
    MAX_TEXT_LEN = 50
    LABEL_PATTERN = _LABEL_PATTERN

    def detect_bboxes(self, doc, **kwargs):
        """Strategy C only (title-text-based) — usage table rarely has cell rects."""
        bbox = _find_table_bbox(doc)
        if bbox is not None:
            return [(bbox, 0.4)]
        return []

    def extract_data(
        self,
        rows, col_roles, header_cells, bbox, doc,
        gap_x=None, texts=None, header_idx=None, confidence=0.0,
        **kwargs,
    ) -> Optional[dict]:
        half_roles, label_positions = _build_half_roles(header_cells, col_roles, gap_x)

        anchors = [(ex, ey, t) for ex, ey, t in (texts or [])
                   if _LABEL_PATTERN.match(t)]
        if not anchors:
            return None

        result_rows: list[dict] = []
        for ax, ay, label in sorted(anchors, key=lambda a: _num(a[2])):
            row = _extract_one_row(ax, ay, label, texts or [],
                                   half_roles, label_positions, gap_x)
            if row is not None:
                result_rows.append(row)

        result_rows.sort(key=lambda r: _num(r['cell_label']))
        if not result_rows:
            return None

        return {'bbox': bbox, 'rows': result_rows}


def _find_table_bbox(doc: Document) -> Optional[BBox]:
    candidates: list[tuple[float, float]] = []
    for e in doc.entities:
        from cable_engine.ir import TextEntity, AttributeEntity
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        raw = (e.text or '').strip()
        if not raw or not raw.replace(' ', '').endswith('表'):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        candidates.append((float(ex), float(ey)))
    if not candidates:
        return None
    ex, ey = max(candidates, key=lambda c: c[0])
    return BBox(ex - 200, ey - 350, 350, 450)


def parse_usage_table(doc: Document, room: BBox) -> Optional[dict]:
    """Legacy-compatible wrapper. Returns dict with 'bbox' and 'rows'.

    The caller (position/builder.py) converts this to UsageTable domain model.
    """
    parser = UsageTableParser()
    return parser.parse(doc, row_mode='label_centered')


__all__ = ['UsageTableParser', 'parse_usage_table']
