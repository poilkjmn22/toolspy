"""cable_engine.layout.clustering — DBSCAN-based device proximity clustering.

DBSCAN (eps=30, min_samples=2) groups nearby devices into candidate clusters.
Post-classification delegates to :mod:`structure` analyzers for pattern
recognition (VERTICAL_COLUMN / HORIZONTAL_ROW / GRID / FREEFORM).

Key principle (V9):
  DBSCAN only answers "which devices are near each other".
  Structure analyzers answer "what spatial pattern are they in".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from sklearn.cluster import DBSCAN

from ..ir.entities import BBox
from .candidate import DeviceCandidate
from .model import LayoutGroupType
from .structure import ColumnAnalyzer, GridAnalyzer, RowAnalyzer


_X_TOL = 4.0
_Y_TOL = 4.0


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
    """DBSCAN-based proximity clusterer.

    This is intentionally simple — it finds "what is near what".
    Pattern classification is delegated to structure analyzers.
    """

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
# Post-classification — delegates to structure analyzers
# ---------------------------------------------------------------------------


def _classify_group(devices: list[DeviceCandidate],
                    cab_bbox: BBox) -> Optional[DeviceGroup]:
    """Classify a DBSCAN cluster into a DeviceGroup with pattern type.

    Tries structure analyzers in order: GRID → COLUMN → ROW → FREEFORM.
    """
    if len(devices) < 2:
        return None

    bbox = _union_bbox(devices)
    cxs = [d.cx for d in devices]
    cys = [d.cy for d in devices]
    widths = [d.bbox.w for d in devices]
    heights = [d.bbox.h for d in devices]

    x_range = max(cxs) - min(cxs)
    y_range = max(cys) - min(cys)

    # GRID — requires clean cols×rows, checked first
    is_grid, grid_dims = GridAnalyzer().analyze(cxs, cys, len(devices))
    if is_grid:
        return DeviceGroup(
            bbox=bbox,
            group_type=LayoutGroupType.GRID,
            devices=sorted(devices, key=lambda d: -d.cy),
            score=0.8,
            features={
                'grid_dims': grid_dims,
                'evidence': [f'grid_{grid_dims["cols"]}x{grid_dims["rows"]}'],
            },
        )

    # VERTICAL_COLUMN — x-aligned
    if x_range <= _X_TOL * 3:
        score, evidence = ColumnAnalyzer().analyze(cxs, cys, widths, heights, cab_bbox)
        if score >= 0.4:
            return DeviceGroup(
                bbox=bbox,
                group_type=LayoutGroupType.VERTICAL_COLUMN,
                devices=sorted(devices, key=lambda d: -d.cy),
                score=score,
                features={'evidence': evidence},
            )

    # HORIZONTAL_ROW — y-aligned
    if y_range <= _Y_TOL * 2:
        score, evidence = RowAnalyzer().analyze(cxs, cys, widths, heights, cab_bbox)
        if score >= 0.4:
            return DeviceGroup(
                bbox=bbox,
                group_type=LayoutGroupType.HORIZONTAL_ROW,
                devices=sorted(devices, key=lambda d: d.cx),
                score=score,
                features={'evidence': evidence},
            )

    # FREEFORM — fallback
    return DeviceGroup(
        bbox=bbox,
        group_type=LayoutGroupType.FREEFORM,
        devices=devices,
        score=0.0,
        features={'evidence': ['connected']},
    )


def _union_bbox(devices: list[DeviceCandidate]) -> BBox:
    xs = [d.bbox.x for d in devices]
    ys = [d.bbox.y for d in devices]
    xe = [d.bbox.x + d.bbox.w for d in devices]
    ye = [d.bbox.y + d.bbox.h for d in devices]
    return BBox(min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))


__all__ = [
    'DeviceClusterer', 'DBSCANClusterer', 'DeviceGroup',
]
