"""cable_engine.classifier.layout — layout-signature classifier.

Different drawing types place their text differently:

  - circuit_loop       : text spread across full canvas (terminals on
                        both sides, core lines horizontal across)
  - terminal_strip     : text on left/right margins (terminal labels) +
                        bottom (cabinet header)
  - cable_schedule     : text in tabular grid (column-aligned)
  - protection_diagram : similar to circuit_loop but with text in a
                        tighter y-range
  - panel_layout       : text concentrated in title block area
                        (bottom-right of drawing)
  - monitoring_system  : text spread evenly, with a few large label blocks

The classifier uses simple bounds + density signatures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..ir import TextEntity, AttributeEntity

from .base import BaseClassifier, BusinessType

if TYPE_CHECKING:
    from ..ir import Document


def _collect_xy(doc: 'Document') -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        x = cf.get('x')
        y = cf.get('y')
        if x is not None and y is not None:
            pts.append((float(x), float(y)))
    return pts


class LayoutClassifier(BaseClassifier):
    name = 'layout'

    def score(self, doc: 'Document') -> dict[BusinessType, float]:
        pts = _collect_xy(doc)
        if len(pts) < 5:
            return {bt: 0.0 for bt in BusinessType}

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        x_span = max(x_max - x_min, 1e-3)
        y_span = max(y_max - y_min, 1e-3)

        # Quadrant occupancy: which quadrant has most text?
        # Origin convention: bottom-left (DWG), so upper-right = high x, high y
        n_q = [0, 0, 0, 0]  # NE, NW, SE, SW
        for x, y in pts:
            cx = x > (x_min + x_span / 2)
            cy = y > (y_min + y_span / 2)
            if cx and cy:
                n_q[0] += 1
            elif not cx and cy:
                n_q[1] += 1
            elif cx and not cy:
                n_q[2] += 1
            else:
                n_q[3] += 1
        total = sum(n_q) or 1

        # Bottom-right concentration -> title block (panel_layout)
        # We measure the fraction in SE quadrant vs others.
        se_frac = n_q[2] / total
        # If more than 50% in SE -> very title-block-heavy
        panel_layout = 0.0
        if se_frac > 0.45:
            panel_layout = min((se_frac - 0.45) / 0.3, 1.0)

        # Spread across all 4 quadrants (within ±10% of 25% each)
        ne_frac = n_q[0] / total
        nw_frac = n_q[1] / total
        sw_frac = n_q[3] / total
        balance = 1.0 - (
            abs(ne_frac - 0.25) + abs(nw_frac - 0.25)
            + abs(se_frac - 0.25) + abs(sw_frac - 0.25)
        )
        # circuit_loop / monitoring_system / protection_diagram tend to spread
        circuit_loop = 0.0
        monitoring_system = 0.0
        protection_diagram = 0.0
        if balance > 0.7:
            circuit_loop = min((balance - 0.7) / 0.3, 1.0)
            monitoring_system = min((balance - 0.7) / 0.5, 0.6)
            protection_diagram = min((balance - 0.7) / 0.5, 0.5)

        # Tabular layout: text aligned in rows. Compute y-cluster density.
        # Count unique y-buckets (rounded to 1mm) vs total text count.
        y_buckets: dict[int, int] = {}
        for _, y in pts:
            key = round(y)
            y_buckets[key] = y_buckets.get(key, 0) + 1
        n_rows = len(y_buckets)
        rows_per_text = n_rows / total
        # Tabular layouts have many rows, each with few text (3-5 text/row)
        cable_schedule = 0.0
        if rows_per_text > 0.5 and len(pts) > 20:
            avg_per_row = total / n_rows
            if 1.5 < avg_per_row < 8.0:
                cable_schedule = min(
                    0.5 * min(rows_per_text, 1.0)
                    + 0.5 * min(n_rows / 50.0, 1.0),
                    1.0,
                )

        # terminal_strip: text concentrated in left + right margins (vertical lines)
        # Measure: extreme left/right 20% of x_span has high text density
        left_band = sum(1 for x, _ in pts if x < x_min + 0.2 * x_span)
        right_band = sum(1 for x, _ in pts if x > x_max - 0.2 * x_span)
        margin_frac = (left_band + right_band) / total
        terminal_strip = 0.0
        if margin_frac > 0.55 and rows_per_text < 0.4:
            terminal_strip = min(
                0.5 * min((margin_frac - 0.55) / 0.3, 1.0)
                + 0.5 * min(n_rows / 20.0, 1.0),
                1.0,
            )

        # unknown: very concentrated in a single quadrant
        unknown = 0.0
        max_q = max(n_q) / total
        if max_q > 0.7:
            unknown = min((max_q - 0.7) / 0.2, 1.0)

        return {
            BusinessType.CIRCUIT_LOOP: circuit_loop,
            BusinessType.TERMINAL_STRIP: terminal_strip,
            BusinessType.CABLE_SCHEDULE: cable_schedule,
            BusinessType.PROTECTION_DIAGRAM: protection_diagram,
            BusinessType.PANEL_LAYOUT: panel_layout,
            BusinessType.MONITORING_SYSTEM: monitoring_system,
            BusinessType.MANUFACTURER_CATALOG: 0.0,
            BusinessType.UNKNOWN: unknown,
        }


__all__ = ['LayoutClassifier']