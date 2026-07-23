"""clustering.py — LayoutGroup detection via sweep-based pattern matching.

Pipeline (sweep-based, no connected components for initial pass):
  1. GRID detection  — try on all ungrouped devices (Nx×Ny = total)
  2. COLUMN sweep    — x-aligned, regularly spaced, min 2 devices
  3. ROW sweep       — y-aligned, regularly spaced, min 2 devices
  4. FREEFORM        — connected-component fallback for remaining
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

from ...ir.entities import BBox
from ..model import LayoutNode, LayoutNodeType, LayoutGroupType


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

X_TOL = 4.0
Y_TOL = 4.0
W_DIFF_TOL = 8.0
H_DIFF_TOL = 6.0
SPACING_STD_TOL = 5.0
MIN_COUNT = 2
GRID_MIN_DIM = 2
GAP_MAX = 40.0
PROXIMITY_RADIUS = 50.0
SCORE_THRESHOLD = 0.40


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_layout_groups(
    devices: list[LayoutNode],
    cab_bbox: BBox,
) -> list[LayoutNode]:
    if len(devices) < MIN_COUNT:
        return []

    used: set[str] = set()
    groups: list[LayoutNode] = []

    # Phase 1 — GRID (on all ungrouped)
    grids = _detect_grids(devices, cab_bbox, used)
    groups.extend(grids)

    # Phase 2 — Columns (remaining)
    remaining = [d for d in devices if d.id not in used]
    cols = _detect_columns(remaining, cab_bbox, used)
    groups.extend(cols)

    # Phase 3 — Rows (remaining)
    remaining = [d for d in devices if d.id not in used]
    rows = _detect_rows(remaining, cab_bbox, used)
    groups.extend(rows)

    # Phase 4 — FREEFORM (remaining, must be spatially connected)
    remaining = [d for d in devices if d.id not in used]
    for comp in _connected_components(remaining, PROXIMITY_RADIUS):
        if len(comp) >= MIN_COUNT:
            g = _build_freeform(comp, cab_bbox)
            if g:
                for d in comp:
                    used.add(d.id)
                groups.append(g)

    return groups


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _cluster_values(values: list[float], tol: float) -> list[float]:
    if not values:
        return []
    sv = sorted(values)
    clusters: list[list[float]] = [[sv[0]]]
    for v in sv[1:]:
        if abs(v - clusters[-1][-1]) <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [statistics.mean(c) for c in clusters]


def _edge_position(cab_bbox: BBox, cx: float, cy: float) -> str:
    cw = cab_bbox.w if cab_bbox.w > 0 else 1
    ch = cab_bbox.h if cab_bbox.h > 0 else 1
    parts = []
    if (cx - cab_bbox.x) / cw < 0.15:
        parts.append('left')
    if (cab_bbox.x + cw - cx) / cw < 0.15:
        parts.append('right')
    if (cab_bbox.y + ch - cy) / ch < 0.1:
        parts.append('top')
    if (cy - cab_bbox.y) / ch < 0.1:
        parts.append('bottom')
    return '_'.join(parts) if parts else 'center'


def _group_bbox(devices: list[LayoutNode]) -> BBox:
    xs = [d.bbox.x for d in devices]
    ys = [d.bbox.y for d in devices]
    xe = [d.bbox.x + d.bbox.w for d in devices]
    ye = [d.bbox.y + d.bbox.h for d in devices]
    return BBox(min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))


# ---------------------------------------------------------------------------
# Phase 1 — GRID
# ---------------------------------------------------------------------------


def _detect_grids(
    devices: list[LayoutNode], cab_bbox: BBox, used: set[str],
) -> list[LayoutNode]:
    grids: list[LayoutNode] = []
    for cand in _find_all_grids(devices):
        g = _score_grid(cand, cab_bbox)
        if g:
            for d in cand:
                used.add(d.id)
            grids.append(g)
    return grids


def _find_all_grids(devices: list[LayoutNode]) -> list[list[LayoutNode]]:
    if len(devices) < 4:
        return []

    pts = [(d, d.bbox.x + d.bbox.w / 2, d.bbox.y + d.bbox.h / 2) for d in devices]
    ungrouped = set(d.id for d in devices)
    result: list[list[LayoutNode]] = []

    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            _, cxi, cyi = pts[i]
            _, cxj, cyj = pts[j]
            dx = abs(cxi - cxj)
            dy = abs(cyi - cyj)
            if dx < 10 or dy < 10:
                continue
            g = _match_grid(devices, dx, dy, cxi, cyi, only_ids=ungrouped)
            if g and len(g) >= 4:
                for d in g:
                    ungrouped.discard(d.id)
                result.append(g)

    return result


def _match_grid(
    devices: list[LayoutNode],
    col_spacing: float, row_spacing: float,
    anchor_x: float, anchor_y: float,
    only_ids: Optional[set[str]] = None,
) -> list[LayoutNode]:
    eps = max(col_spacing, row_spacing) * 0.15
    matched: list[LayoutNode] = []
    for d in devices:
        if only_ids is not None and d.id not in only_ids:
            continue
        cx = d.bbox.x + d.bbox.w / 2
        cy = d.bbox.y + d.bbox.h / 2
        ix = round((cx - anchor_x) / col_spacing)
        iy = round((cy - anchor_y) / row_spacing)
        ex = anchor_x + ix * col_spacing
        ey = anchor_y + iy * row_spacing
        if abs(cx - ex) <= eps and abs(cy - ey) <= eps:
            matched.append(d)
    return matched


def _score_grid(
    devices: list[LayoutNode], cab_bbox: BBox,
) -> Optional[LayoutNode]:
    if len(devices) < 4:
        return None
    cxs = _cluster_values([d.bbox.x + d.bbox.w / 2 for d in devices], X_TOL)
    cys = _cluster_values([d.bbox.y + d.bbox.h / 2 for d in devices], Y_TOL)
    nx, ny = len(cxs), len(cys)
    if nx < GRID_MIN_DIM or ny < GRID_MIN_DIM:
        return None
    if nx * ny != len(devices):
        return None

    widths = [d.bbox.w for d in devices]
    heights = [d.bbox.h for d in devices]

    score, evidence = 0.0, []
    cols = sorted(cxs)
    rows = sorted(cys, reverse=True)
    cg = [cols[i + 1] - cols[i] for i in range(len(cols) - 1)]
    rg = [rows[i] - rows[i + 1] for i in range(len(rows) - 1)]
    cs = statistics.stdev(cg) if len(cg) >= 2 else 0.0
    rs = statistics.stdev(rg) if len(rg) >= 2 else 0.0
    if cs <= SPACING_STD_TOL and rs <= SPACING_STD_TOL:
        score += 0.4
    elif cs <= SPACING_STD_TOL or rs <= SPACING_STD_TOL:
        score += 0.2
    if max(widths) - min(widths) <= W_DIFF_TOL:
        score += 0.15
    if max(heights) - min(heights) <= H_DIFF_TOL:
        score += 0.15
    score += 0.2
    evidence.append(f'grid_{nx}x{ny}')

    if score < SCORE_THRESHOLD:
        return None

    avg_cx = statistics.mean(cols)
    avg_cy = statistics.mean(rows) if rows else 0.0
    position = _edge_position(cab_bbox, avg_cx, avg_cy)

    g = LayoutNode(
        id='group_g',
        node_type=LayoutNodeType.GROUP,
        group_type=LayoutGroupType.GRID,
        bbox=_group_bbox(devices), name='',
        data={
            'score': round(score, 2), 'evidence': evidence,
            'position': position,
            'grid_dims': {'cols': nx, 'rows': ny},
        },
    )
    for d in sorted(devices, key=lambda x: -(x.bbox.y + x.bbox.h / 2)):
        g.add_child(d)
    return g


# ---------------------------------------------------------------------------
# Phase 2 — COLUMN sweep
# ---------------------------------------------------------------------------


def _detect_columns(
    devices: list[LayoutNode], cab_bbox: BBox, used: set[str],
) -> list[LayoutNode]:
    groups: list[LayoutNode] = []
    for aligned in _x_sweep(devices, X_TOL):
        if len(aligned) < MIN_COUNT:
            continue
        for part in _split_gap_y(aligned, GAP_MAX):
            if len(part) < MIN_COUNT:
                continue
            g = _score_column(part, cab_bbox)
            if g:
                for d in part:
                    used.add(d.id)
                groups.append(g)
    return groups


def _x_sweep(
    devices: list[LayoutNode], tol: float,
) -> list[list[LayoutNode]]:
    with_x = [(d, d.bbox.x + d.bbox.w / 2) for d in devices]
    with_x.sort(key=lambda t: t[1])
    cur: list[LayoutNode] = []
    cur_cx = 0.0
    clusters: list[list[LayoutNode]] = []
    for d, cx in with_x:
        if not cur:
            cur, cur_cx = [d], cx
        elif abs(cx - cur_cx) <= tol:
            cur.append(d)
        else:
            if len(cur) >= MIN_COUNT:
                clusters.append(cur)
            cur, cur_cx = [d], cx
    if len(cur) >= MIN_COUNT:
        clusters.append(cur)
    return clusters


def _split_gap_y(
    devices: list[LayoutNode], max_gap: float,
) -> list[list[LayoutNode]]:
    sd = sorted(devices, key=lambda d: -(d.bbox.y + d.bbox.h / 2))
    cur = [sd[0]]
    out: list[list[LayoutNode]] = []
    for i in range(1, len(sd)):
        pc = sd[i - 1].bbox.y + sd[i - 1].bbox.h / 2
        cc = sd[i].bbox.y + sd[i].bbox.h / 2
        if pc - cc > max_gap:
            out.append(cur)
            cur = []
        cur.append(sd[i])
    if cur:
        out.append(cur)
    return out


def _score_column(
    devices: list[LayoutNode], cab_bbox: BBox,
) -> Optional[LayoutNode]:
    sd = sorted(devices, key=lambda d: -(d.bbox.y + d.bbox.h / 2))
    widths = [d.bbox.w for d in sd]
    heights = [d.bbox.h for d in sd]
    cx = [d.bbox.x + d.bbox.w / 2 for d in sd]
    cy = [d.bbox.y + d.bbox.h / 2 for d in sd]

    score, evidence = 0.0, []
    if max(cx) - min(cx) <= X_TOL:
        score += 0.3
        evidence.append('x_align')
    if max(widths) - min(widths) <= W_DIFF_TOL:
        score += 0.15
        evidence.append('w_consist')
    if max(heights) - min(heights) <= H_DIFF_TOL:
        score += 0.15
        evidence.append('h_consist')
    gaps = [cy[i] - cy[i + 1] for i in range(len(cy) - 1)]
    if gaps and all(g > 2.0 for g in gaps):
        if len(gaps) >= 2:
            gs = statistics.stdev(gaps)
            if gs <= SPACING_STD_TOL:
                score += 0.2
                evidence.append('spacing_std:' + f'{gs:.1f}')
        score += 0.1
        evidence.append('count:' + str(len(sd)))

    avg_cx = statistics.mean(cx)
    avg_cy = statistics.mean(cy)
    position = _edge_position(cab_bbox, avg_cx, avg_cy)

    cw = cab_bbox.w if cab_bbox.w > 0 else 1
    if (avg_cx - cab_bbox.x) / cw < 0.15:
        score += 0.1
        evidence.append('left_edge')
    elif (cab_bbox.x + cw - avg_cx) / cw < 0.15:
        score += 0.1
        evidence.append('right_edge')

    if score < SCORE_THRESHOLD:
        return None

    g = LayoutNode(
        id='group_v',
        node_type=LayoutNodeType.GROUP,
        group_type=LayoutGroupType.VERTICAL_COLUMN,
        bbox=_group_bbox(sd), name='',
        data={'score': round(score, 2), 'evidence': evidence, 'position': position},
    )
    for d in sd:
        g.add_child(d)
    return g


# ---------------------------------------------------------------------------
# Phase 3 — ROW sweep
# ---------------------------------------------------------------------------


def _detect_rows(
    devices: list[LayoutNode], cab_bbox: BBox, used: set[str],
) -> list[LayoutNode]:
    groups: list[LayoutNode] = []
    for aligned in _y_sweep(devices, Y_TOL):
        if len(aligned) < MIN_COUNT:
            continue
        for part in _split_gap_x(aligned, GAP_MAX):
            if len(part) < MIN_COUNT:
                continue
            g = _score_row(part, cab_bbox)
            if g:
                for d in part:
                    used.add(d.id)
                groups.append(g)
    return groups


def _y_sweep(
    devices: list[LayoutNode], tol: float,
) -> list[list[LayoutNode]]:
    with_y = [(d, d.bbox.y + d.bbox.h / 2) for d in devices]
    with_y.sort(key=lambda t: t[1])
    cur: list[LayoutNode] = []
    cur_cy = 0.0
    clusters: list[list[LayoutNode]] = []
    for d, cy in with_y:
        if not cur:
            cur, cur_cy = [d], cy
        elif abs(cy - cur_cy) <= tol:
            cur.append(d)
        else:
            if len(cur) >= MIN_COUNT:
                clusters.append(cur)
            cur, cur_cy = [d], cy
    if len(cur) >= MIN_COUNT:
        clusters.append(cur)
    return clusters


def _split_gap_x(
    devices: list[LayoutNode], max_gap: float,
) -> list[list[LayoutNode]]:
    sd = sorted(devices, key=lambda d: d.bbox.x + d.bbox.w / 2)
    cur = [sd[0]]
    out: list[list[LayoutNode]] = []
    for i in range(1, len(sd)):
        pc = sd[i - 1].bbox.x + sd[i - 1].bbox.w / 2
        cc = sd[i].bbox.x + sd[i].bbox.w / 2
        if cc - pc > max_gap:
            out.append(cur)
            cur = []
        cur.append(sd[i])
    if cur:
        out.append(cur)
    return out


def _score_row(
    devices: list[LayoutNode], cab_bbox: BBox,
) -> Optional[LayoutNode]:
    sd = sorted(devices, key=lambda d: d.bbox.x + d.bbox.w / 2)
    widths = [d.bbox.w for d in sd]
    heights = [d.bbox.h for d in sd]
    cx = [d.bbox.x + d.bbox.w / 2 for d in sd]
    cy = [d.bbox.y + d.bbox.h / 2 for d in sd]

    score, evidence = 0.0, []
    if max(cy) - min(cy) <= Y_TOL:
        score += 0.3
        evidence.append('y_align')
    if max(heights) - min(heights) <= H_DIFF_TOL:
        score += 0.15
        evidence.append('h_consist')
    if max(widths) - min(widths) <= W_DIFF_TOL:
        score += 0.15
        evidence.append('w_consist')
    gaps = [cx[i + 1] - cx[i] for i in range(len(cx) - 1)]
    if gaps and all(g > 2.0 for g in gaps):
        if len(gaps) >= 2:
            gs = statistics.stdev(gaps)
            if gs <= SPACING_STD_TOL:
                score += 0.2
                evidence.append('spacing_std:' + f'{gs:.1f}')
        score += 0.1
        evidence.append('count:' + str(len(sd)))

    avg_cx = statistics.mean(cx)
    avg_cy = statistics.mean(cy)
    position = _edge_position(cab_bbox, avg_cx, avg_cy)

    ch = cab_bbox.h if cab_bbox.h > 0 else 1
    if (cab_bbox.y + ch - avg_cy) / ch < 0.1:
        score += 0.1
        evidence.append('top_edge')

    if score < SCORE_THRESHOLD:
        return None

    g = LayoutNode(
        id='group_h',
        node_type=LayoutNodeType.GROUP,
        group_type=LayoutGroupType.HORIZONTAL_ROW,
        bbox=_group_bbox(sd), name='',
        data={'score': round(score, 2), 'evidence': evidence, 'position': position},
    )
    for d in sd:
        g.add_child(d)
    return g


# ---------------------------------------------------------------------------
# Phase 4 — FREEFORM fallback (connected components)
# ---------------------------------------------------------------------------


def _connected_components(
    devices: list[LayoutNode], radius: float,
) -> list[list[LayoutNode]]:
    centroids = {
        d.id: (d.bbox.x + d.bbox.w / 2, d.bbox.y + d.bbox.h / 2)
        for d in devices
    }
    adj: dict[str, list[str]] = {d.id: [] for d in devices}
    for i in range(len(devices)):
        for j in range(i + 1, len(devices)):
            a, b = devices[i], devices[j]
            ca, cb = centroids[a.id], centroids[b.id]
            if math.hypot(ca[0] - cb[0], ca[1] - cb[1]) <= radius:
                adj[a.id].append(b.id)
                adj[b.id].append(a.id)

    visited: set[str] = set()
    lookup = {d.id: d for d in devices}
    components: list[list[LayoutNode]] = []
    for d in devices:
        if d.id in visited:
            continue
        comp: list[LayoutNode] = []
        stack = [d.id]
        while stack:
            nid = stack.pop()
            if nid in visited:
                continue
            visited.add(nid)
            comp.append(lookup[nid])
            for nb in adj.get(nid, []):
                if nb not in visited:
                    stack.append(nb)
        components.append(comp)
    return components


def _build_freeform(
    devices: list[LayoutNode], cab_bbox: BBox,
) -> Optional[LayoutNode]:
    if len(devices) < 2:
        return None
    cx = statistics.mean([d.bbox.x + d.bbox.w / 2 for d in devices])
    cy = statistics.mean([d.bbox.y + d.bbox.h / 2 for d in devices])
    position = _edge_position(cab_bbox, cx, cy)
    g = LayoutNode(
        id='group_f',
        node_type=LayoutNodeType.GROUP,
        group_type=LayoutGroupType.FREEFORM,
        bbox=_group_bbox(devices), name='',
        data={'score': 0.0, 'evidence': ['connected'], 'position': position},
    )
    for d in sorted(devices, key=lambda x: -(x.bbox.y + x.bbox.h / 2)):
        g.add_child(d)
    return g


__all__ = [
    'detect_layout_groups',
]
