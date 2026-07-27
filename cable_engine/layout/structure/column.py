"""ColumnAnalyzer — x-aligned devices → VERTICAL_COLUMN."""

from __future__ import annotations

import statistics
from typing import Optional

from ...ir.entities import BBox


_X_TOL = 4.0
_W_DIFF_TOL = 8.0
_H_DIFF_TOL = 6.0
_SPACING_STD_TOL = 5.0


class ColumnAnalyzer:
    """Score a set of device positions as a vertical column.

    Criteria (score contribution):
      - x alignment       (+0.30)
      - width consistency  (+0.15)
      - height consistency (+0.15)
      - even spacing       (+0.20)
      - device count       (+0.10)
      - left/right edge    (+0.10)

    Threshold: score >= 0.40 → qualifies as VERTICAL_COLUMN.
    """

    def analyze(
        self,
        cxs: list[float],
        cys: list[float],
        widths: list[float],
        heights: list[float],
        cab_bbox: BBox,
    ) -> tuple[float, list[str]]:
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

        sorted_by_y = sorted(zip(cys, cxs, widths, heights), key=lambda x: -x[0])
        gaps = [sorted_by_y[i][0] - sorted_by_y[i + 1][0]
                for i in range(len(sorted_by_y) - 1)]
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


__all__ = ['ColumnAnalyzer']
