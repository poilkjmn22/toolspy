"""cable_engine.layout.clustering — DBSCAN-based device clustering.

DeviceClusterer (abstract)
  └─ DBSCANClusterer

Feature vector: (cx, cy, w * 0.1, h * 0.1)
Post-processing classifies group type (COLUMN / ROW / GRID / FREEFORM).
label = -1 → noise (standalone device).
"""

from __future__ import annotations

import math
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from sklearn.cluster import DBSCAN

from ..ir.entities import BBox
from .candidate import DeviceCandidate
from .model import LayoutGroupType


_X_TOL = 4.0
_Y_TOL = 4.0
_W_DIFF_TOL = 8.0
_H_DIFF_TOL = 6.0
_SPACING_STD_TOL = 5.0


@dataclass
class DeviceGroup:
    bbox: BBox
    group_type: LayoutGroupType
    devices: list[DeviceCandidate] = field(default_factory=list)
    score: float = 0.0
    name: str = ''
    features: dict = field(default_factory=dict)


class DeviceClusterer(ABC):
    @abstractmethod
    def cluster(self, candidates: list[DeviceCandidate],
                cab_bbox: BBox) -> list[DeviceGroup]:
        ...


class DBSCANClusterer(DeviceClusterer):
    def __init__(self, eps: float = 30.0, min_samples: int = 2):
        self._eps = eps
        self._min_samples = min_samples

    def cluster(self, candidates: list[DeviceCandidate],
                cab_bbox: BBox) -> list[DeviceGroup]:
        if len(candidates) < self._min_samples:
            return []

        X = np.array([
            [d.cx, d.cy, d.bbox.w * 0.1, d.bbox.h * 0.1]
            for d in candidates
        ])
        labels = DBSCAN(eps=self._eps, min_samples=self._min_samples).fit_predict(X)

        groups: dict[int, list[DeviceCandidate]] = {}
        for cand, label in zip(candidates, labels):
            groups.setdefault(label, []).append(cand)

        result: list[DeviceGroup] = []
        for label, devs in groups.items():
            if label == -1:
                continue
            g = _classify_group(devs, cab_bbox)
            if g:
                result.append(g)

        return result


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def _classify_group(devices: list[DeviceCandidate],
                    cab_bbox: BBox) -> Optional[DeviceGroup]:
    if len(devices) < 2:
        return None

    bbox = _union_bbox(devices)
    cxs = [d.cx for d in devices]
    cys = [d.cy for d in devices]
    widths = [d.bbox.w for d in devices]
    heights = [d.bbox.h for d in devices]

    x_range = max(cxs) - min(cxs)
    y_range = max(cys) - min(cys)

    if x_range <= _X_TOL * 2:
        # Column: x-aligned
        score, evidence = _score_column(cxs, cys, widths, heights, cab_bbox)
        if score >= 0.4:
            return DeviceGroup(
                bbox=bbox,
                group_type=LayoutGroupType.VERTICAL_COLUMN,
                devices=sorted(devices, key=lambda d: -d.cy),
                score=score,
                features={'evidence': evidence},
            )

    if y_range <= _Y_TOL * 2:
        # Row: y-aligned
        score, evidence = _score_row(cxs, cys, widths, heights, cab_bbox)
        if score >= 0.4:
            return DeviceGroup(
                bbox=bbox,
                group_type=LayoutGroupType.HORIZONTAL_ROW,
                devices=sorted(devices, key=lambda d: d.cx),
                score=score,
                features={'evidence': evidence},
            )

    is_grid, grid_dims = _check_grid(cxs, cys, devices)
    if is_grid:
        return DeviceGroup(
            bbox=bbox,
            group_type=LayoutGroupType.GRID,
            devices=sorted(devices, key=lambda d: -d.cy),
            score=0.8,
            features={'grid_dims': grid_dims, 'evidence': [f'grid_{grid_dims["cols"]}x{grid_dims["rows"]}']},
        )

    return DeviceGroup(
        bbox=bbox,
        group_type=LayoutGroupType.FREEFORM,
        devices=devices,
        score=0.0,
        features={'evidence': ['connected']},
    )


def _score_column(cxs, cys, widths, heights, cab_bbox) -> tuple[float, list[str]]:
    score, evidence = 0.0, []
    if max(cxs) - min(cxs) <= _X_TOL:
        score += 0.3
        evidence.append('x_align')
    if max(widths) - min(widths) <= _W_DIFF_TOL:
        score += 0.15
        evidence.append('w_consist')
    if max(heights) - min(heights) <= _H_DIFF_TOL:
        score += 0.15
        evidence.append('h_consist')

    gaps = [cys[i] - cys[i + 1] for i in range(len(cys) - 1)]
    if gaps and all(g > 2.0 for g in gaps):
        if len(gaps) >= 2:
            s = statistics.stdev(gaps) if len(gaps) >= 2 else 0.0
            if s <= _SPACING_STD_TOL:
                score += 0.2
                evidence.append(f'spacing_std:{s:.1f}')
        score += 0.1
        evidence.append(f'count:{len(cxs)}')

    avg_cx = statistics.mean(cxs)
    cw = cab_bbox.w if cab_bbox.w > 0 else 1
    if cab_bbox.x > 0 and (avg_cx - cab_bbox.x) / cw < 0.15:
        score += 0.1
        evidence.append('left_edge')
    elif (cab_bbox.x + cw - avg_cx) / cw < 0.15:
        score += 0.1
        evidence.append('right_edge')

    return score, evidence


def _score_row(cxs, cys, widths, heights, cab_bbox) -> tuple[float, list[str]]:
    score, evidence = 0.0, []
    if max(cys) - min(cys) <= _Y_TOL:
        score += 0.3
        evidence.append('y_align')
    if max(heights) - min(heights) <= _H_DIFF_TOL:
        score += 0.15
        evidence.append('h_consist')
    if max(widths) - min(widths) <= _W_DIFF_TOL:
        score += 0.15
        evidence.append('w_consist')

    gaps = [cxs[i + 1] - cxs[i] for i in range(len(cxs) - 1)]
    if gaps and all(g > 2.0 for g in gaps):
        if len(gaps) >= 2:
            s = statistics.stdev(gaps) if len(gaps) >= 2 else 0.0
            if s <= _SPACING_STD_TOL:
                score += 0.2
                evidence.append(f'spacing_std:{s:.1f}')
        score += 0.1
        evidence.append(f'count:{len(cxs)}')

    ch = cab_bbox.h if cab_bbox.h > 0 else 1
    if (cab_bbox.y + ch - statistics.mean(cys)) / ch < 0.1:
        score += 0.1
        evidence.append('top_edge')

    return score, evidence


def _check_grid(cxs, cys, devices) -> tuple[bool, dict]:
    if len(devices) < 4:
        return False, {}
    ux = sorted(set(round(c, 1) for c in cxs))
    uy = sorted(set(round(c, 1) for c in cys), reverse=True)
    nx, ny = len(ux), len(uy)
    if nx < 2 or ny < 2:
        return False, {}
    if nx * ny != len(devices):
        return False, {}
    return True, {'cols': nx, 'rows': ny}


def _union_bbox(devices: list[DeviceCandidate]) -> BBox:
    xs = [d.bbox.x for d in devices]
    ys = [d.bbox.y for d in devices]
    xe = [d.bbox.x + d.bbox.w for d in devices]
    ye = [d.bbox.y + d.bbox.h for d in devices]
    return BBox(min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))


__all__ = [
    'DeviceClusterer', 'DBSCANClusterer', 'DeviceGroup',
]
