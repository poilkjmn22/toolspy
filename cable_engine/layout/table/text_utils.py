"""Shared text utilities for table parsing across document types.

Eliminates 6× repeated entity-iteration + coordinate-extraction + bbox-clip
patterns and 3× repeated Y-bucketing + header-detection + role-mapping patterns.
"""

from __future__ import annotations

import re
from typing import Optional, Protocol

from cable_engine.ir import Document, AttributeEntity, TextEntity
from cable_engine.ir.entities import BBox


class NoiseFilter(Protocol):
    def __call__(self, text: str) -> bool: ...


def _default_noise(text: str) -> bool:
    if not text:
        return True
    if text.startswith('\\M+') or text.startswith('KKS'):
        return True
    return False


def collect_texts(
    doc: Document,
    bbox: Optional[BBox] = None,
    max_len: int = 50,
    is_noise: Optional[NoiseFilter] = None,
) -> list[tuple[float, float, str]]:
    """Collect (x, y, text) tuples from a document, filtered by bbox and noise.

    Unified replacement for 6 near-identical implementations across:
      - table/parser.py, table/detector.py
      - position/parser.py
      - graph/builder.py (CableScheduleAnalyzer)
    """
    out: list[tuple[float, float, str]] = []
    noise = is_noise or _default_noise
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t or len(t) > max_len:
            continue
        if noise(t):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if bbox is not None:
            if not (bbox.x <= ex <= bbox.x + bbox.w and bbox.y <= ey <= bbox.y + bbox.h):
                continue
        out.append((float(ex), float(ey), t))
    return out


def count_texts_in(doc: Document, bbox: BBox) -> int:
    """Count text entities inside a bbox. Fast equivalent of len(collect_texts(...))."""
    count = 0
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if bbox.x <= ex <= bbox.x + bbox.w and bbox.y <= ey <= bbox.y + bbox.h:
            count += 1
    return count


def y_bucket_rows(
    texts: list[tuple[float, float, str]],
    tol: float = 3.0,
) -> list[list[tuple[float, str]]]:
    """Group texts by Y-coordinate buckets, sorted top-to-bottom.

    Each bucket is a list of (x, text) sorted left-to-right.
    Returns a list of rows sorted by descending Y (CAD top-first).
    """
    rows_by_y: dict[float, list[tuple[float, str]]] = {}
    for ex, ey, t in texts:
        bucket = round(ey / tol) * tol
        rows_by_y.setdefault(bucket, []).append((ex, t))
    if len(rows_by_y) < 2:
        return []
    sorted_y = sorted(rows_by_y.keys(), reverse=True)
    return [sorted(rows_by_y[y], key=lambda c: c[0]) for y in sorted_y]


def _num(label: str) -> int:
    m = re.match(r'(\d+)', label)
    return int(m.group(1)) if m else 0


def y_bucket_rows_with_labels(
    texts: list[tuple[float, float, str]],
    label_pattern: re.Pattern,
    tol: float = 3.0,
) -> list[list[tuple[float, str]]]:
    """Group texts by Y-coordinate buckets, anchoring on label positions.

    Labels matching *label_pattern* define row Y positions.  Non-label
    texts within *tol* of a label's Y are folded into that row.
    This is more robust than pure Y-bucketing when text within a row
    sits at varying Y coordinates.
    """
    anchors = [(ex, ey, t) for ex, ey, t in texts if label_pattern.match(t)]
    if not anchors:
        return y_bucket_rows(texts, tol)

    anchors.sort(key=lambda a: _num(a[2]))
    rows: list[list[tuple[float, str]]] = []
    used: set[int] = set()
    text_items = list(enumerate(texts))

    for ax, ay, label in anchors:
        row: list[tuple[float, str]] = [(ax, label)]
        for i, (ex, ey, t) in text_items:
            if i in used:
                continue
            if t == label:
                continue
            if label_pattern.match(t):
                continue
            if abs(ey - ay) <= tol:
                row.append((ex, t))
                used.add(i)
        row.sort(key=lambda c: c[0])
        rows.append(row)

    rows.sort(key=lambda r: _num(r[0][1]))
    return rows


def find_header_row(
    rows: list[list[tuple[float, str]]],
    patterns: list[tuple[re.Pattern, str]],
) -> Optional[int]:
    """Return the index of the first row whose combined text matches any header pattern."""
    for i, row in enumerate(rows):
        joined = ' '.join(t for _, t in row)
        for pat, _ in patterns:
            if pat.search(joined):
                return i
    return None


def map_column_roles(
    header_cells: list[tuple[float, str]],
    patterns: list[tuple[re.Pattern, str]],
) -> dict[int, str]:
    """Map column index → role name by matching header cell text against patterns.

    Returns {col_index: role, ...} for matched columns.
    """
    roles: dict[int, str] = {}
    for ci, (_, text) in enumerate(header_cells):
        for pat, role in patterns:
            if pat.search(text):
                roles[ci] = role
                break
    return roles


def detect_gap_x(
    header_cells: list[tuple[float, str]],
    col_roles: dict[int, str],
    gap_role: str = 'cell_label',
) -> Optional[float]:
    """Detect the X-gap between column halves in a two-column split layout.

    Returns the midpoint X between the last column before the second
    occurrence of *gap_role* and the cell at that role.
    Returns None if fewer than 2 columns have the gap_role.
    """
    indices = [ci for ci, role in col_roles.items() if role == gap_role]
    if len(indices) <= 1:
        return None
    gap = (header_cells[indices[1] - 1][0] + header_cells[indices[1]][0]) / 2
    return gap


__all__ = [
    'collect_texts', 'count_texts_in',
    'y_bucket_rows', 'y_bucket_rows_with_labels',
    'find_header_row', 'map_column_roles', 'detect_gap_x',
    'NoiseFilter', '_default_noise',
]
