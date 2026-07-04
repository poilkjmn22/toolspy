"""cable_engine.graph.builder — TopologyStage.

V6 replaces GraphBuilderStage with a business-object pipeline:

  Document IR (entities + EED)  →  TopologyStage
                                     ├─ classify document type
                                     ├─ dispatch to Analyzer
                                     │   ├─ TerminalStripAnalyzer (端子排图)
                                     │   └─ (future) CircuitLoopAnalyzer
                                     └─ persist cable_topology rows

The viewer queries cable_topology directly — no BFS needed.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Optional

from ..ir import (
    ArcGeometry, AttributeEntity, BlockRef, CircleGeometry, Document,
    DocumentType, GeometryEntity, LineGeometry, TextEntity,
)
from ..ir.entities import BBox, Point
from ..pipeline.stage import Context, Stage


# ---------------------------------------------------------------------------
# Cable ID pattern — the first segment of EED values like
#   "GY6-136:10F,左@49F,左"  →  GY6-136
#   "11003-132:70F,右"      →  11003-132
# ---------------------------------------------------------------------------
_CABLE_ID_IN_EED = re.compile(
    r'^([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})'
)


def _cable_id_from_eed(eed: list[str]) -> Optional[str]:
    for v in eed:
        m = _CABLE_ID_IN_EED.match(v)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Document-type classification (by filename + text content keywords)
# ---------------------------------------------------------------------------
def _classify_document(path: str, doc: Optional[Document] = None) -> str:
    low = path.lower()
    if '端子排' in low:
        return 'terminal_strip'
    if '回路图' in low:
        return 'circuit_loop'
    # Check text entities inside the document
    if doc is not None:
        for e in doc.entities:
            if isinstance(e, TextEntity) or isinstance(e, AttributeEntity):
                txt = (e.text or '').lower()
                if '端子排图' in txt:
                    return 'terminal_strip'
                if '回路图' in txt:
                    return 'circuit_loop'
    return 'unknown'


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
_CORRIDOR = 3.0       # x tolerance for vertical-column text


def _is_horizontal(entity: GeometryEntity) -> bool:
    if not isinstance(entity, LineGeometry):
        return False
    pts = list(entity.points or [])
    if len(pts) < 2:
        return False
    ys = [p.y for p in pts]
    return max(ys) - min(ys) < 1.0


def _is_vertical(entity: GeometryEntity) -> bool:
    if not isinstance(entity, LineGeometry):
        return False
    pts = list(entity.points or [])
    if len(pts) < 2:
        return False
    xs = [p.x for p in pts]
    return max(xs) - min(xs) < 1.0


def _collect_texts_along_vertical(
    vx: float,
    from_y: float,
    to_y: float,
    texts: list,
) -> list[dict]:
    """Collect text entities along the vertical corridor at vx.

    Starting from the far endpoint (to_y), continuing in the same
    direction past it. Returns records sorted by distance from to_y.
    """
    dy = to_y - from_y
    direction = 1 if dy > 0 else -1

    candidates: list[dict] = []
    for e in texts:
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if abs(ex - vx) > _CORRIDOR:
            continue
        # Must be past the far endpoint (continued direction)
        if direction > 0 and ey < to_y - 0.1:
            continue
        if direction < 0 and ey > to_y + 0.1:
            continue
        dist = abs(ey - to_y)
        if dist < 0.1:
            continue  # skip text at the exact endpoint
        label = (e.text or '').strip()
        if not label:
            continue
        candidates.append({
            'dist': dist,
            'y': ey,
            'x': ex,
            'label': label,
        })

    candidates.sort(key=lambda r: r['dist'])
    return candidates


def _find_strip_name(
    terminal_x: float,
    terminal_y: float,
    all_texts: list,
) -> Optional[str]:
    """Find terminal strip code name.

    Rule: locate the numbering-section start ("1") to the LEFT of
    terminal_x, then find the closest short alphanumeric text
    further left — that is the strip name (e.g. 21GD, 21QD).

    The strip name may be above or below the terminal number row
    (in some drawings it sits at the terminal-strip-top row, y≈115,
    while the "1" is at y≈140). We use a wide y tolerance.
    """
    ones: list[tuple[float, float]] = []
    for e in all_texts:
        if not isinstance(e, TextEntity):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        # Y tolerance: "1" can be in the terminal row (≈140) or
        # the header row — accept anything within 40mm
        if (e.text or '').strip() == '1' and abs(ey - terminal_y) < 40:
            ones.append((ex, ey))

    closest_one: Optional[tuple[float, float]] = None
    closest_dist = float('inf')
    for ox, oy in ones:
        if ox < terminal_x:
            d = terminal_x - ox
            if d < closest_dist:
                closest_dist = d
                closest_one = (ox, oy)

    if closest_one is None:
        return None

    ox, oy = closest_one
    best: Optional[str] = None
    best_dist = float('inf')
    # Strip name pattern: e.g. "10D", "12D", "21GD", "J831", "YD",
    #                      "A631", "1-4CD", "I", "+GPS1"
    _STRIP_NAME_RE = re.compile(
        r'^(\d{1,2}[A-Za-z]{1,4})$'          # 10D, 12D, 21GD, 21QD
        r'|^([A-Za-z]{1,2}\d{1,4})$'          # J831, A631, YD
        r'|^([A-Za-z]{1,4})$'                 # I, UB, YD
        r'|^(\+?[A-Za-z]{1,3}\d{1,4})$'       # +GPS1, +GPS3
        r'|^(\d{1,2}-[A-Za-z]{1,4})$'         # 1-4CD
    )
    for e in all_texts:
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        # Must be at least 3 units left of "1" (reject markers at the same x)
        if ex < ox - 3 and abs(ey - oy) < 38:
            t = (e.text or '').strip()
            if t and _STRIP_NAME_RE.match(t):
                d = ox - ex
                if d < best_dist:
                    best_dist = d
                    best = t
    return best


_LOOP_LIKE = re.compile(r'^[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}$')
_LOOP_ALPHA_DIGIT = re.compile(r'^[A-Za-z]\d{1,4}$')  # J701, A631, L630


def _classify_column_text(
    texts: list[dict],
) -> tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    """Classify ordered column texts into (circuit_desc, terminal_no,
    loop_id, unknown_busi).

    Heuristic (validated against D0210-35/38 terminal strip data):
      - appears like 'CABLE-ID' (XXX-XXX) or 'J701'?   → loop_id
      - pure digit?                                      → terminal_no
      - contains alpha?                                  → circuit_desc (first) or unknown_busi (second)
      - other (punctuation, numbers with -)?              → unknown_busi (first) or circuit_desc (second)
    """
    circuit_desc: Optional[str] = None
    loop_id: Optional[str] = None
    terminal_no: Optional[int] = None
    unknown_busi: Optional[str] = None

    for item in texts:
        label = item['label']

        # Pure digit = terminal number
        if label.isdigit():
            tn = int(label)
            if terminal_no is None:
                terminal_no = tn
            continue

        # Cable-ID-like or letter+digits = loop
        # (WG3-J903, 3T-YW-C-, J701, Ⅴ-J911)
        if (_LOOP_LIKE.match(label)
                or _LOOP_ALPHA_DIGIT.match(label)
                or ('-' in label and len(label) >= 4)):
            if loop_id is None:
                loop_id = label
            elif circuit_desc is None:
                circuit_desc = label
            elif unknown_busi is None:
                unknown_busi = label
            continue

        # Has alphabetic chars = Chinese or English description
        if any(c.isalpha() for c in label):
            if circuit_desc is None:
                circuit_desc = label
            elif unknown_busi is None:
                unknown_busi = label
            continue

        # Other (e.g. "-OF-12", "-17")
        if unknown_busi is None:
            unknown_busi = label
        elif circuit_desc is None:
            circuit_desc = label

    return circuit_desc, terminal_no, loop_id, unknown_busi


# ===================================================================
# Analyzers
# ===================================================================
class TerminalStripAnalyzer:
    """Analyze a terminal-strip DWG and emit cable_topology records."""

    MIN_VERTICAL_LENGTH = 20.0  # minimum vertical drop to consider

    def analyze(self, doc: Document) -> list[dict]:
        emitted: list[dict] = []

        # Separate entities by type
        lines: list[LineGeometry] = []
        texts: list = []
        for e in doc.entities:
            if isinstance(e, LineGeometry):
                lines.append(e)
            elif isinstance(e, (TextEntity, AttributeEntity)):
                texts.append(e)

        # Group lines by cable ID (from EED)
        cable_groups: dict[str, dict] = {}
        for line in lines:
            cf = getattr(line, 'custom_fields', None) or {}
            eed = cf.get('eed', [])
            cid = _cable_id_from_eed(eed)
            if cid is None:
                continue
            if cid not in cable_groups:
                cable_groups[cid] = {'horiz': [], 'vert': []}
            if _is_horizontal(line):
                cable_groups[cid]['horiz'].append(line)
            elif _is_vertical(line):
                cable_groups[cid]['vert'].append(line)

        # Process each cable
        for cable_id, groups in cable_groups.items():
            records = self._analyze_one_cable(
                cable_id, groups['horiz'], groups['vert'], texts,
            )
            emitted.extend(records)

        return emitted

    def _find_conductor_no(self, vx: float, h_y: float, texts: list) -> Optional[int]:
        """Find conductor number (NO tag) near the corner of a
        vertical drop. Searches for ATTRIB with tag='NO' within 5mm
        x and 10mm y of the corner."""
        for e in texts:
            if not isinstance(e, AttributeEntity):
                continue
            if (e.tag or '').upper() != 'NO':
                continue
            cf = getattr(e, 'custom_fields', None) or {}
            ex = cf.get('x')
            ey = cf.get('y')
            if ex is None or ey is None:
                continue
            if abs(ex - vx) <= 5 and abs(ey - h_y) <= 10:
                txt = (e.text or '').strip()
                if txt.isdigit():
                    return int(txt)
        return None

    def _analyze_one_cable(
        self,
        cable_id: str,
        horizontals: list[LineGeometry],
        verticals: list[LineGeometry],
        texts: list,
    ) -> list[dict]:
        if not horizontals:
            return []

        # Use the first horizontal as reference
        h = horizontals[0]
        hpts = list(h.points or [])
        h_y = hpts[0].y if hpts else 0.0

        records: list[dict] = []

        for v in verticals:
            pts = list(v.points or [])
            if len(pts) < 2:
                continue
            vx = pts[0].x

            # Determine which end is connected to the horizontal
            # (corner) and which is the far endpoint
            dy = pts[-1].y - pts[0].y
            if abs(dy) < self.MIN_VERTICAL_LENGTH:
                continue

            # Find the end closest to the horizontal y
            d0 = abs(pts[0].y - h_y)
            d1 = abs(pts[-1].y - h_y)
            if d0 < d1:
                corner_y = pts[0].y
                end_y = pts[-1].y
            else:
                corner_y = pts[-1].y
                end_y = pts[0].y

            # Find conductor number from NO ATTRIB near corner
            conductor_no = self._find_conductor_no(vx, h_y, texts)

            # Collect texts along the vertical direction past the endpoint
            column_texts = _collect_texts_along_vertical(
                vx, corner_y, end_y, texts,
            )

            circuit_desc, terminal_no, loop_id, unknown_busi = (
                _classify_column_text(column_texts)
            )

            # Find strip name
            strip_name: Optional[str] = None
            if terminal_no is not None:
                for item in column_texts:
                    if item['label'] == str(terminal_no):
                        strip_name = _find_strip_name(
                            item['x'], item['y'], texts,
                        )
                        break

            records.append({
                'cable_id': cable_id,
                'conductor_no': conductor_no,
                'strip_name': strip_name,
                'terminal_no': terminal_no,
                'circuit_desc': circuit_desc,
                'loop_id': loop_id,
                'unknown_busi': unknown_busi,
                'source_type': 'terminal_strip',
            })

        return records


# ===================================================================
# TopologyStage (replaces GraphBuilderStage)
# ===================================================================
class TopologyStage(Stage):
    """Build cable topology from Document IR.

    Run AFTER the Loader stage.
    Inputs:  ctx.document (Document IR)
    Outputs: rows in cable_topology (+ terminal_strips).
    """

    name = 'topology_builder'

    def __init__(self, store) -> None:
        self._store = store

    def run(self, ctx: Context) -> Context:
        doc = ctx.document
        if doc is None:
            ctx.error_msg = 'no document to build topology from'
            return ctx

        doc_type = _classify_document(str(ctx.document_path), doc)
        ctx.document_type = doc_type

        # Wipe any prior topology rows for this document
        self._store.delete_topology_for_document(doc.content_hash)

        if doc_type == 'terminal_strip':
            analyzer = TerminalStripAnalyzer()
            records = analyzer.analyze(doc)
        else:
            # Fallback — no analyzer yet, skip
            records = []

        # Persist
        for rec in records:
            self._store.upsert_cable_topology(
                cable_id=rec['cable_id'],
                conductor_no=rec['conductor_no'],
                strip_name=rec['strip_name'],
                terminal_no=rec['terminal_no'],
                circuit_desc=rec['circuit_desc'],
                loop_id=rec['loop_id'],
                unknown_busi=rec['unknown_busi'],
                document_hash=doc.content_hash,
                source_type=rec['source_type'],
            )
            if rec['strip_name']:
                self._store.upsert_terminal_strip(
                    rec['strip_name'],
                    document_hash=doc.content_hash,
                )

        ctx.result = {
            'cable_topology_count': len(records),
        }
        return ctx


__all__ = ['TopologyStage']
