"""cable_engine.layout.associator — Text association for devices and groups.

Device text: texts inside the candidate bbox → name (topmost) + description (rest).
Group label: texts above the group bbox → group name.
"""

from __future__ import annotations

from typing import Optional

from ..ir.entities import BBox
from .candidate import DeviceCandidate
from .clustering import DeviceGroup


class TextAssociator:
    """Associate text entities with DeviceCandidates and DeviceGroups."""

    def associate_devices(
        self,
        candidates: list[DeviceCandidate],
        text_positions: list[tuple[float, float, str]],
    ) -> None:
        """Assign name (topmost) + description (remaining) to each candidate."""
        for c in candidates:
            inside: list[tuple[float, str]] = []
            for ex, ey, t in text_positions:
                if (c.bbox.x <= ex <= c.bbox.x + c.bbox.w
                        and c.bbox.y <= ey <= c.bbox.y + c.bbox.h):
                    inside.append((ey, t))
            if not inside:
                continue
            inside.sort(key=lambda x: -x[0])
            c.name = inside[0][1]
            if len(inside) > 1:
                c.description = [t for _, t in inside[1:]]

    def associate_groups(
        self,
        groups: list[DeviceGroup],
        text_positions: list[tuple[float, float, str]],
        cab_bbox: BBox,
        max_y_dist: float = 30.0,
    ) -> None:
        """Assign nearby text labels as group names (e.g. 左侧, 右侧)."""
        for g in groups:
            if g.name:
                continue
            gb = g.bbox
            cx = gb.x + gb.w / 2
            best_label = ''
            best_dy = 999.0
            for ex, ey, t in text_positions:
                if abs(ex - cx) > 50:
                    continue
                cy = gb.y + gb.h
                if ey < cy:
                    continue
                dy = ey - cy
                if dy > max_y_dist:
                    continue
                if dy < best_dy:
                    best_dy = dy
                    best_label = t
            if best_label:
                g.name = best_label


def collect_text_positions(
    texts: list[tuple[str, float, float]],
) -> list[tuple[float, float, str]]:
    """Normalise text tuples to (x, y, text)."""
    return [(x, y, t) for t, x, y in texts]


__all__ = [
    'TextAssociator', 'collect_text_positions',
]
