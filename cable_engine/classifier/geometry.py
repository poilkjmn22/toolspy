"""cable_engine.classifier.geometry — geometry-signature classifier.

Different business types have characteristic geometry signatures:

  - circuit_loop       : dense short horizontal core-lines, many ATTRIBs
                        (one per terminal), sparse verticals
  - terminal_strip     : long verticals (terminal columns), short horizontals
                        (one per cable row), EED on LINEs
  - cable_schedule     : mostly TEXT in tabular layout, few/no LINEs
  - protection_diagram : dense LINEs + ATTRIBS + circular/symbol geometry
  - panel_layout       : mostly BlockRef (representing equipment), sparse LINEs
  - monitoring_system  : mixed, often BlockRef-heavy
  - unknown            : sparse geometry, mostly title-block TEXT

Each rule is a heuristic ratio. The classifier returns a 0..1 score
per business type.
"""

from __future__ import annotations

from math import log1p
from typing import TYPE_CHECKING

from ..ir import (
    ArcGeometry, AttributeEntity, BlockRef, CircleGeometry,
    GeometryEntity, LineGeometry, TextEntity,
)

from .base import BaseClassifier, BusinessType

if TYPE_CHECKING:
    from ..ir import Document


class GeometryClassifier(BaseClassifier):
    name = 'geometry'

    def score(self, doc: 'Document') -> dict[BusinessType, float]:
        # ----- entity counts -----
        n_text = n_attrib = n_line = n_h = n_v = 0
        n_arc = n_circ = n_block = 0
        total_eed = 0
        for e in doc.entities:
            if isinstance(e, TextEntity):
                n_text += 1
            elif isinstance(e, AttributeEntity):
                n_attrib += 1
                # count terminal-like tags
            elif isinstance(e, LineGeometry):
                n_line += 1
                pts = list(e.points or [])
                if len(pts) >= 2:
                    ys = [p.y for p in pts]
                    xs = [p.x for p in pts]
                    if max(ys) - min(ys) < 1.0:
                        n_h += 1
                    elif max(xs) - min(xs) < 1.0:
                        n_v += 1
                cf = getattr(e, 'custom_fields', None) or {}
                eed = cf.get('eed') or []
                if eed:
                    total_eed += 1
            elif isinstance(e, ArcGeometry):
                n_arc += 1
            elif isinstance(e, CircleGeometry):
                n_circ += 1
            elif isinstance(e, BlockRef):
                n_block += 1

        total_geom = n_text + n_attrib + n_line + n_arc + n_circ + n_block
        # ----- per-business signature scores (0..1) -----

        # circuit_loop: lots of horizontals (core lines) + lots of ATTRIBs
        # (terminals) + few verticals (no terminal columns)
        circuit_loop = 0.0
        if n_line > 0:
            h_ratio = n_h / n_line
            attrib_density = n_attrib / max(n_line, 1)
            # ATTRIB:core_line near 1.0 is typical for circuit_loop
            if h_ratio > 0.7 and 0.3 < attrib_density <= 4.0:
                circuit_loop = min(
                    0.4 * h_ratio
                    + 0.4 * min(attrib_density / 3.0, 1.0)
                    + 0.2 * min(n_attrib / 30.0, 1.0),
                    1.0,
                )

        # terminal_strip: many long verticals + EED on LINEs + many text labels
        terminal_strip = 0.0
        if n_line > 0:
            v_ratio = n_v / n_line
            eed_ratio = total_eed / n_line
            if v_ratio > 0.5 and eed_ratio > 0.5:
                terminal_strip = min(
                    0.4 * v_ratio
                    + 0.4 * eed_ratio
                    + 0.2 * min(n_v / 20.0, 1.0),
                    1.0,
                )

        # cable_schedule: text-dominant, very few geometric entities.
        # Require at least 20 entities — otherwise single error-entity
        # docs (e.g. PDFium "data format error") trivially match.
        cable_schedule = 0.0
        if total_geom >= 20:
            text_ratio = n_text / total_geom
            line_ratio = n_line / total_geom
            if text_ratio > 0.85 and line_ratio < 0.05:
                cable_schedule = min(text_ratio, 1.0)

        # protection_diagram: similar to circuit_loop but with more ARC/CIRCLE
        # (relay coils, CT/PT symbols) and slightly fewer ATTRIBs
        protection_diagram = 0.0
        if n_line > 0:
            arc_circ_density = (n_arc + n_circ) / max(n_line, 1)
            if 0.05 < arc_circ_density < 0.5 and n_attrib > 0:
                protection_diagram = min(
                    0.5 * min(arc_circ_density * 4.0, 1.0)
                    + 0.3 * min(n_attrib / 20.0, 1.0)
                    + 0.2 * min(n_line / 100.0, 1.0),
                    1.0,
                )

        # panel_layout (屏面布置图): BlockRef-heavy, sparse lines (equipment blocks)
        # Content: devices + tables inside a panel face layout
        panel_layout = 0.0
        if total_geom > 0:
            block_ratio = n_block / total_geom
            line_ratio = n_line / total_geom
            if block_ratio > 0.3 and line_ratio < 0.4:
                panel_layout = min(
                    0.5 * min(block_ratio * 2.0, 1.0)
                    + 0.3 * min(n_block / 30.0, 1.0)
                    + 0.2 * min(n_text / 50.0, 1.0),
                    1.0,
                )

        # panel_position (屏位布置图): floor plan / cabinet position layout
        # Signature: high line ratio (cabinet rectangles), moderate text
        # (labels), no EED (no cable wiring), no circles (no terminal markers),
        # horizontals dominate (rows of cabinets).
        panel_position = 0.0
        if n_line > 0 and total_geom >= 50:
            line_ratio = n_line / total_geom
            text_ratio = n_text / total_geom
            h_ratio = n_h / n_line
            no_eed = total_eed == 0
            few_circles = n_circ < 5
            # Typical: line_ratio 0.5-0.9, text_ratio 0.05-0.3,
            # h_ratio > 0.5, no EED, few circles
            if (line_ratio > 0.5 and text_ratio > 0.05
                    and h_ratio > 0.5 and no_eed and few_circles):
                panel_position = min(
                    0.35 * min(line_ratio * 1.2, 1.0)
                    + 0.25 * min(text_ratio * 3.0, 1.0)
                    + 0.25 * min(h_ratio, 1.0)
                    + 0.15 * (1.0 - min(n_v / n_line, 1.0)),
                    1.0,
                )

        # monitoring_system: mixed — high blockref + medium line
        monitoring_system = 0.0
        if total_geom > 0:
            block_ratio = n_block / total_geom
            line_ratio = n_line / total_geom
            text_ratio = n_text / total_geom
            # typical: 30-50% block, 20-40% line, rest text
            if (0.15 < block_ratio < 0.5
                    and 0.15 < line_ratio < 0.5
                    and 0.1 < text_ratio < 0.6):
                balance = 1.0 - abs(block_ratio - 0.3) - abs(line_ratio - 0.3)
                monitoring_system = min(
                    0.5 * max(balance, 0)
                    + 0.5 * min((n_block + n_line + n_text) / 50.0, 1.0),
                    1.0,
                )

        # unknown: very sparse everything
        unknown = 0.0
        if total_geom < 30 and n_text > 5:
            # Low total geometry, but some text — likely a cover/TOC
            unknown = min(
                0.5 + 0.5 * (1.0 - min(total_geom / 30.0, 1.0)),
                1.0,
            )

        return {
            BusinessType.CIRCUIT_LOOP: circuit_loop,
            BusinessType.TERMINAL_STRIP: terminal_strip,
            BusinessType.CABLE_SCHEDULE: cable_schedule,
            BusinessType.PROTECTION_DIAGRAM: protection_diagram,
            BusinessType.PANEL_LAYOUT: panel_layout,
            BusinessType.PANEL_POSITION: panel_position,
            BusinessType.MONITORING_SYSTEM: monitoring_system,
            BusinessType.MANUFACTURER_CATALOG: 0.0,
            BusinessType.UNKNOWN: unknown,
        }


__all__ = ['GeometryClassifier']