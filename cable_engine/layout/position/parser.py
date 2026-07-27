"""cable_engine.layout.position.parser — 屏屏用途一览表解析.

Detects the usage table region by locating a title ending with ``表``,
  then extracts rows by anchoring on screen-number texts (``\\d+[CF]``) and
collecting sibling cells within a Y‑neighbourhood.  Role assignment uses
relative column index within each detected column group (left/right half
for two‑column layouts, whole header for single‑column layouts).

Row layout detection:
  - Single‑column (D0201‑07) : 数量 | 备注 | 屏号 | 名称 | 数量 | 备注
  - Two‑column split (D0201‑05) : 屏号 | 名称 | 数量 | 备注 ‖ 屏号 | 名称 | 数量 | 备注

CAD text entities within a single table row often sit at slightly
different Y coordinates, so pure Y‑bucketing loses cells.  Label‑centered
anchoring (searching outward from each screen‑number label) is robust
against this.
"""

from __future__ import annotations

import re
from typing import Optional

from ...ir import Document
from ...ir.entities import BBox, TextEntity
from ...ir.geometry import AttributeEntity

from .model import UsageTable, UsageTableRow

_ROW_TOL = 6.0

_USAGE_HEADER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'屏[\s]*号'), 'cell_label'),
    (re.compile(r'名[\s]*称'), 'equipment'),
    (re.compile(r'数[\s]*量'), 'qty'),
    (re.compile(r'备[\s]*注'), 'remark'),
]

_LABEL_PATTERN = re.compile(r'^\d+[CF]$')

_SKIP_TEXTS = frozenset()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _find_table_bbox(doc: Document) -> Optional[BBox]:
    candidates: list[tuple[float, float]] = []
    for e in doc.entities:
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
        candidates.append((ex, ey))
    if not candidates:
        return None
    ex, ey = max(candidates, key=lambda c: c[0])
    return BBox(ex - 200, ey - 350, 350, 450)


def _collect_texts(doc: Document, bbox: BBox) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t or len(t) > 50:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if bbox.x <= ex <= bbox.x + bbox.w and bbox.y <= ey <= bbox.y + bbox.h:
            out.append((ex, ey, t))
    return out


def _is_noise(text: str) -> bool:
    if not text:
        return True
    if text in _SKIP_TEXTS:
        return True
    if text.startswith('\\M+') or text.startswith('KKS'):
        return True
    return False


def _is_header_row(cells: list[tuple[float, str]]) -> bool:
    joined = ' '.join(c[1] for c in cells)
    for pat, _ in _USAGE_HEADER_PATTERNS:
        if pat.search(joined):
            return True
    return False


def _map_column_roles(header_cells: list[tuple[float, str]]) -> dict[int, str]:
    roles: dict[int, str] = {}
    for ci, (_, text) in enumerate(header_cells):
        for pat, role in _USAGE_HEADER_PATTERNS:
            if pat.search(text):
                roles[ci] = role
                break
    return roles


def _detect_gap_x(header_cells: list[tuple[float, str]],
                  col_roles: dict[int, str]) -> Optional[float]:
    num_screen = sum(1 for role in col_roles.values() if role == 'cell_label')
    if num_screen <= 1:
        return None
    screen_indices = [ci for ci, role in col_roles.items() if role == 'cell_label']
    gap = (header_cells[screen_indices[1] - 1][0] + header_cells[screen_indices[1]][0]) / 2
    return gap


def _build_half_roles(header_cells: list[tuple[float, str]],
                      col_roles: dict[int, str],
                      gap_x: Optional[float]) -> tuple[list[str], Optional[int]]:
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
    return result, label_positions  # type: ignore[return-value]


def _num(label: str) -> int:
    m = re.match(r'(\d+)', label)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# row extraction
# ---------------------------------------------------------------------------

def _extract_rows(
    texts: list[tuple[float, float, str]],
    half_roles: list[list[str]],
    label_positions: list[Optional[int]],
    gap_x: Optional[float],
) -> list[UsageTableRow]:
    anchors = [(ex, ey, t) for ex, ey, t in texts
               if not _is_noise(t) and _LABEL_PATTERN.match(t)]
    if not anchors:
        return []

    result: list[UsageTableRow] = []

    for ax, ay, label in sorted(anchors, key=lambda a: _num(a[2])):
        row = _extract_one_row(ax, ay, label, texts,
                               half_roles, label_positions, gap_x)
        if row is not None:
            result.append(row)

    result.sort(key=lambda r: _num(r.cell_label))
    return result


def _extract_one_row(
    ax: float, ay: float, label: str,
    texts: list[tuple[float, float, str]],
    half_roles: list[list[str]],
    label_positions: list[Optional[int]],
    gap_x: Optional[float],
) -> Optional[UsageTableRow]:
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
        and not _is_noise(t)
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

    return UsageTableRow(cell_label=label, equipment=equipment, qty=qty, remark=remark)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def parse_usage_table(doc: Document, room: BBox) -> Optional[UsageTable]:
    """Parse the usage table (屏屏用途一览表)."""
    table_bbox = _find_table_bbox(doc)
    if table_bbox is None:
        return None

    texts = _collect_texts(doc, table_bbox)
    if not texts:
        return None

    # Y‑bucket only to locate the header row
    rows_by_y: dict[float, list[tuple[float, str]]] = {}
    for ex, ey, t in texts:
        if _is_noise(t):
            continue
        bucket = round(ey / _ROW_TOL) * _ROW_TOL
        rows_by_y.setdefault(bucket, []).append((ex, t))

    if len(rows_by_y) < 2:
        return None

    sorted_y = sorted(rows_by_y.keys(), reverse=True)
    header_cells: Optional[list] = None
    for yi, y_bucket in enumerate(sorted_y):
        cells = sorted(rows_by_y[y_bucket], key=lambda c: c[0])
        if _is_header_row(cells):
            header_cells = cells
            break

    if header_cells is None:
        return None

    col_roles = _map_column_roles(header_cells)
    gap_x = _detect_gap_x(header_cells, col_roles)
    half_roles, label_positions = _build_half_roles(header_cells, col_roles, gap_x)

    header_x_min = min(x for x, _ in header_cells)
    header_x_max = max(x for x, _ in header_cells)
    margin = 30.0
    texts = [(ex, ey, t) for ex, ey, t in texts
             if header_x_min - margin <= ex <= header_x_max + margin]

    rows = _extract_rows(texts, half_roles, label_positions, gap_x)
    if not rows:
        return None

    return UsageTable(bbox=table_bbox, rows=rows)


__all__ = ['parse_usage_table']
