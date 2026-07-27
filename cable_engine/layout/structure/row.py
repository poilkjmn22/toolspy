"""RowAnalyzer — y-aligned devices → HORIZONTAL_ROW."""

from __future__ import annotations

import statistics
from typing import Optional

from ...ir.entities import BBox


_Y_TOL = 4.0
_W_DIFF_TOL = 8.0
_H_DIFF_TOL = 6.0
_SPACING_STD_TOL = 5.0


class RowAnalyzer:
    """Score a set of device positions as a horizontal row.

    Criteria (score contribution):
      - y alignment        (+0.30)
      - height consistency  (+0.15)
      - width consistency   (+0.15)
      - even spacing        (+0.20)
      - device count        (+0.10)
      - top edge            (+0.10)

    Threshold: score >= 0.40 → qualifies as HORIZONTAL_ROW.
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

        if max(cys) - min(cys) <= _Y_TOL:
            score += 0.3
            evidence.append('y_align')
        if max(heights) - min(heights) <= _H_DIFF_TOL:
            score += 0.15
            evidence.append('h_consist')
        if max(widths) - min(widths) <= _W_DIFF_TOL:
            score += 0.15
            evidence.append('w_consist')

        sorted_by_x = sorted(zip(cxs, cys, widths, heights), key=lambda x: x[0])
        gaps = [sorted_by_x[i + 1][0] - sorted_by_x[i][0]
                for i in range(len(sorted_by_x) - 1)]
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


__all__ = ['RowAnalyzer']
