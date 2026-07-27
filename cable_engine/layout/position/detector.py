"""cable_engine.layout.position.detector — 房间 / 屏位格子 / 行聚类.

Phase 0: find_f_texts(doc) → list of (x, y, label)
Phase 1: detect_room(doc, f_texts) → BBox
   利用 F 编号文本范围找到包含它的房间边界。
Phase 2: detect_cells(doc, room, f_texts) → list[PositionCell]
   房间内等大矩形 → 匹配 F 编号文本。
Phase 3: cluster_rows(cells) → list[PositionRow]
   按 Y 聚类分组行，每行按 X 排序。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from ...ir import Document
from ...ir.entities import BBox, TextEntity
from ...ir.geometry import AttributeEntity, LineGeometry
from ..primitives.rectangle import detect_rectangles

from .model import PositionCell, PositionRow

_F_PATTERN = re.compile(r'^(\d+)F$')
_ROW_Y_TOL_FACTOR = 0.6

# ── Phase 0: Find F-number texts ────────────────────────────────────────


def find_f_texts(doc: Document) -> list[tuple[float, float, str]]:
    """Scan all text/attribute entities for F-number pattern (e.g. 1F, 76F)."""
    result: list[tuple[float, float, str]] = []
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not _F_PATTERN.match(t):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        x = cf.get('x')
        y = cf.get('y')
        if x is None or y is None:
            continue
        result.append((float(x), float(y), t))
    return result


# ── Phase 1: Room detection ────────────────────────────────────────────


def detect_room(doc: Document,
                f_texts: list[tuple[float, float, str]] = None
                ) -> Optional[BBox]:
    """Find the room boundary using F-text extent and long lines.

    Strategy:
      1. Use F-text bounding box as a hint for where the room is.
      2. Find horizontals that span across the F-text x-range.
      3. The topmost/bottommost such horizontals are the room top/bottom.
      4. Find verticals that span the room height → left/right walls.
    """
    if f_texts is None:
        f_texts = find_f_texts(doc)

    # Collect long horizontals
    lines_h: list[tuple[float, float, float, float]] = []
    lines_v: list[tuple[float, float, float, float]] = []

    for e in doc.entities:
        if not isinstance(e, LineGeometry):
            continue
        pts = list(e.points or [])
        if len(pts) < 2:
            continue
        x1, y1 = pts[0].x, pts[0].y
        x2, y2 = pts[-1].x, pts[-1].y
        if abs(y1 - y2) < 2.0:
            length = abs(x2 - x1)
            if length >= 50:
                lines_h.append((min(x1, x2), (y1 + y2) / 2, max(x1, x2), length))
        elif abs(x1 - x2) < 2.0:
            length = abs(y2 - y1)
            if length >= 50:
                lines_v.append((min(x1, x2), min(y1, y2), max(y1, y2), length))

    if not lines_h:
        return None

    # Cluster F-texts by X — pick the largest/densest group.
    # Some drawings have F-texts in both the cell grid AND the usage
    # table area; using only the dominant cluster avoids a too-wide bbox.
    if f_texts and len(f_texts) > 10:
        sorted_f = sorted(f_texts, key=lambda t: t[0])
        clusters: list[list[tuple[float, float, str]]] = [[sorted_f[0]]]
        for i in range(1, len(sorted_f)):
            if sorted_f[i][0] - sorted_f[i - 1][0] > 50:
                clusters.append([])
            clusters[-1].append(sorted_f[i])
        # Prefer larger cluster; break ties by smaller Y-span (denser)
        f_texts = max(clusters, key=lambda c: (
            len(c),
            -(max(y for _, y, _ in c) - min(y for _, y, _ in c)),
        ))

    # Compute F-text bounding box as a hint
    if f_texts:
        fx_min = min(x for x, _, _ in f_texts)
        fx_max = max(x for x, _, _ in f_texts)
        fy_min = min(y for _, y, _ in f_texts)
        fy_max = max(y for _, y, _ in f_texts)
        margin = max((fy_max - fy_min) * 0.3, 20.0)
    else:
        # No F-texts — fall back to global line extremes
        h_by_y_all: dict[str, list] = {}
        for x1, y, x2, length in lines_h:
            key = f'{round(y * 2) / 2:.1f}'
            h_by_y_all.setdefault(key, []).append((x1, x2, length))
        h_sorted = sorted(
            [(float(k), max(v, key=lambda t: t[2])) for k, v in h_by_y_all.items()],
            key=lambda t: t[0],
        )
        if len(h_sorted) < 2:
            return None
        by, (bx1, bx2, _) = h_sorted[0]
        ty, (tx1, tx2, _) = h_sorted[-1]
        return BBox(min(bx1, tx1), by, max(bx2, tx2) - min(bx1, tx1), ty - by)

    # Filter horizontals: must span across at least 60% of F-text x-range.
    # Group by Y first — some drawings (e.g. D0227‑03) use paired segments
    # (left half + right half with a gap) so individual lines fail the span
    # check but their UNION does not.
    f_width = fx_max - fx_min
    min_span_x = fx_min + f_width * 0.2
    max_span_x = fx_max - f_width * 0.2
    target_span = (fx_max - fx_min) * 0.6

    h_by_y_all: dict[float, list[tuple[float, float, float]]] = {}
    for x1, y, x2, length in lines_h:
        key = round(y * 2) / 2
        h_by_y_all.setdefault(key, []).append((x1, x2, length))

    h_near_cells: list[tuple[float, float, float, float]] = []
    for y_key, segs in h_by_y_all.items():
        union_x1 = min(s[0] for s in segs)
        union_x2 = max(s[1] for s in segs)
        union_len = union_x2 - union_x1
        if union_x1 <= min_span_x and union_x2 >= max_span_x and union_len >= target_span:
            best = max(segs, key=lambda s: s[2])
            h_near_cells.append((best[0], y_key, best[1], best[2]))

    if len(h_near_cells) < 2:
        # Fallback: use any horizontals near F-text y-range
        for x1, y, x2, length in lines_h:
            if fy_min - margin <= y <= fy_max + margin:
                h_near_cells.append((x1, y, x2, length))
        if len(h_near_cells) < 2:
            return None

    # Group by y, keep longest per y
    h_by_y: dict[float, tuple[float, float, float]] = {}
    for x1, y, x2, length in h_near_cells:
        key = round(y * 2) / 2
        if key not in h_by_y or length > h_by_y[key][2]:
            h_by_y[key] = (x1, x2, length)

    h_ys = sorted(h_by_y.items(), key=lambda kv: kv[0])
    bottom_y = h_ys[0][0]
    top_y = h_ys[-1][0]
    room_xmin = min(h_ys[0][1][0], h_ys[-1][1][0])
    room_xmax = max(h_ys[0][1][1], h_ys[-1][1][1])

    # Find verticals that span most of the room height AND are near the room's x span
    room_h = top_y - bottom_y
    room_x_span = room_xmax - room_xmin
    min_v_span = room_h * 0.6
    v_by_x: dict[float, list[tuple[float, float]]] = {}
    for x, y1, y2, length in lines_v:
        if length < min_v_span:
            continue
        # Only accept verticals within or near the room's horizontal span
        if x < room_xmin - room_x_span * 0.2 or x > room_xmax + room_x_span * 0.2:
            continue
        key = round(x * 2) / 2
        v_by_x.setdefault(key, []).append((y1, y2))

    if v_by_x:
        v_keys = sorted(v_by_x.keys())
        # Update room x bounds only to the nearest vertical outside the current span
        if v_keys[0] < room_xmin:
            room_xmin = v_keys[0]
        if v_keys[-1] > room_xmax:
            room_xmax = v_keys[-1]

    return BBox(room_xmin, bottom_y, room_xmax - room_xmin, top_y - bottom_y)


# ── Phase 2: Cell detection ────────────────────────────────────────────


def _collect_texts(doc: Document) -> dict[int, list[tuple[float, float, str]]]:
    out: dict[int, list[tuple[float, float, str]]] = {}
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        x = cf.get('x')
        y = cf.get('y')
        if x is None or y is None:
            continue
        key = round(float(y) * 2)
        out.setdefault(key, []).append((float(x), float(y), t))
    return out


def detect_cells(doc: Document, room: BBox,
                 f_texts: list[tuple[float, float, str]] = None
                 ) -> list[PositionCell]:
    """Find equal-sized rectangles inside *room*, match F-number texts."""
    if f_texts is None:
        f_texts = find_f_texts(doc)

    rects = detect_rectangles(doc)
    cells = []
    for r in rects:
        b = r.bbox
        if not (room.x < b.x + b.w / 2 < room.x + room.w and
                room.y < b.y + b.h / 2 < room.y + room.h):
            continue
        cells.append(b)

    if not cells:
        return []

    size_counts: Counter[tuple[int, int]] = Counter()
    for b in cells:
        size_counts[(round(b.w), round(b.h))] += 1
    if not size_counts:
        return []

    dominant_size = size_counts.most_common(1)[0][0]
    dw, dh = dominant_size

    cells = [b for b in cells
             if abs(b.w - dw) <= 3 and abs(b.h - dh) <= 3]

    # Filter F-texts to room x range
    f_in_room = [(x, y, t) for x, y, t in f_texts
                 if room.x <= x <= room.x + room.w]

    result: list[PositionCell] = []
    for b in cells:
        label = ''
        best_dist = float('inf')
        for tx, ty, t in f_in_room:
            cx, cy = b.x + b.w / 2, b.y + b.h / 2
            d = (tx - cx) ** 2 + (ty - cy) ** 2
            if d < best_dist:
                best_dist = d
                label = t
        accept_dist = max(b.w, b.h) * 0.8
        if best_dist > accept_dist ** 2:
            label = ''
        result.append(PositionCell(label=label, bbox=b))

    return result


# ── Phase 2: Row clustering ────────────────────────────────────────────


def _make_row(cells: list[PositionCell]) -> PositionRow:
    xs = [c.bbox.x for c in cells if c.bbox]
    ys = [c.bbox.y for c in cells if c.bbox]
    xe = [c.bbox.x + c.bbox.w for c in cells if c.bbox]
    ye = [c.bbox.y + c.bbox.h for c in cells if c.bbox]
    bbox = BBox(min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys)) if xs else None
    return PositionRow(cells=cells, bbox=bbox)


def _detect_groups(cells: list[PositionCell]) -> int:
    """Assign *group_index* based on X gaps ≥ 1.5× median gap. Returns number of groups."""
    if len(cells) < 2:
        for c in cells:
            c.group_index = 0
        return 1

    gaps = [cells[i + 1].bbox.x - cells[i].bbox.x
            for i in range(len(cells) - 1) if cells[i].bbox and cells[i + 1].bbox]
    if not gaps:
        for c in cells:
            c.group_index = 0
        return 1

    median_gap = sorted(gaps)[len(gaps) // 2]
    threshold = median_gap * 1.5

    group = 0
    cells[0].group_index = group
    for i in range(1, len(cells)):
        if cells[i - 1].bbox and cells[i].bbox:
            if cells[i].bbox.x - cells[i - 1].bbox.x > threshold:
                group += 1
        cells[i].group_index = group
    return group + 1


def cluster_rows(cells: list[PositionCell]) -> list[PositionRow]:
    """Group cells into rows by Y proximity, sort each row by X.

    Within each row X gaps ≥ 1.5× median gap are treated as column‑group
    boundaries (e.g. 1F‑11F | 12F‑21F).  *col_index* is set per group.
    """
    if not cells:
        return []

    heights = [c.bbox.h for c in cells if c.bbox is not None]
    if not heights:
        return []
    median_h = sorted(heights)[len(heights) // 2]
    y_tol = median_h * _ROW_Y_TOL_FACTOR

    sorted_cells = sorted(
        cells,
        key=lambda c: -(c.bbox.y + c.bbox.h / 2) if c.bbox else 0,
    )

    rows: list[PositionRow] = []
    current: list[PositionCell] = []
    current_y = None

    for c in sorted_cells:
        if c.bbox is None:
            continue
        cy = (c.bbox.y + c.bbox.h / 2)
        if current_y is None:
            current_y = cy
            current = [c]
        elif abs(cy - current_y) <= y_tol:
            current.append(c)
        else:
            rows.append(_make_row(current))
            current = [c]
            current_y = cy

    if current:
        rows.append(_make_row(current))

    for row in rows:
        row.cells.sort(key=lambda c: c.bbox.x if c.bbox else 0)
        num_groups = _detect_groups(row.cells)
        # col_index per group
        group_counter: dict[int, int] = {}
        for c in row.cells:
            gi = c.group_index
            idx = group_counter.get(gi, 0)
            c.col_index = idx
            group_counter[gi] = idx + 1

    for i, row in enumerate(rows):
        row.row_index = i
        for c in row.cells:
            c.row_index = i

    return rows


__all__ = ['detect_room', 'detect_cells', 'cluster_rows']
