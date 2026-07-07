"""cable_engine.graph.builder — TopologyStage.

V6 replaces GraphBuilderStage with a business-object pipeline:

  Document IR (entities + EED)  →  TopologyStage
                                     ├─ classify document type
                                     ├─ dispatch to Analyzer
                                     │   ├─ TerminalStripAnalyzer (端子排图)
                                     │   └─ CircuitLoopAnalyzer (回路图)
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
from ..classifier import CompositeClassifier, BusinessType, Classification
from ..pipeline.stage import Context, Stage


# ---------------------------------------------------------------------------
# Cable ID pattern — the first segment of EED values like
#   "GY6-136:10F,左@49F,左"  →  GY6-136
#   "11003-132:70F,右"      →  11003-132
# ---------------------------------------------------------------------------
_CABLE_ID_IN_EED = re.compile(
    r'^([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})'
)
# Matches "ZL-307ZF(1)" → group1=ZL-307ZF, group2=1
_WIRE_SERIAL = re.compile(
    r'^([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})\((\d+)\)$'
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
# Geometry helpers (used by TerminalStripAnalyzer)
# ---------------------------------------------------------------------------
_CORRIDOR = 3.0


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
        if direction > 0 and ey < to_y - 0.1:
            continue
        if direction < 0 and ey > to_y + 0.1:
            continue
        dist = abs(ey - to_y)
        if dist < 0.1:
            continue
        label = (e.text or '').strip()
        if not label:
            continue
        candidates.append({'dist': dist, 'y': ey, 'x': ex, 'label': label})
    candidates.sort(key=lambda r: r['dist'])
    return candidates


def _find_strip_name(
    terminal_x: float,
    terminal_y: float,
    all_texts: list,
) -> Optional[str]:
    ones: list[tuple[float, float]] = []
    for e in all_texts:
        if not isinstance(e, TextEntity):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if (e.text or '').strip() == '1' and abs(ey - terminal_y) < 40:
            ones.append((ex, ey))
    closest_one: Optional[tuple[float, float]] = None
    closest_dist = float('inf')
    for ox, oy in ones:
        if ox < terminal_x - 1.0:
            d = terminal_x - ox
            if d < closest_dist:
                closest_dist = d
                closest_one = (ox, oy)
    # If no "1" strictly left, fallback: use the closest "1"
    # (handles terminal_no=1 where terminal IS the strip start)
    if closest_one is None:
        for ox, oy in ones:
            d = abs(ox - terminal_x)
            if d < closest_dist:
                closest_dist = d
                closest_one = (ox, oy)
    if closest_one is None:
        return None
    ox, oy = closest_one
    _STRIP_NAME_RE = re.compile(
        r'^(\d{1,2}[A-Za-z]{1,4})$'
        r'|^([A-Za-z]{1,2}\d{1,4})$'
        r'|^([A-Za-z]{1,4})$'
        r'|^(\+?[A-Za-z]{1,3}\d{1,4})$'
        r'|^(\d{1,2}-[A-Za-z]{1,4})$'
    )
    best: Optional[str] = None
    best_dist = float('inf')
    for e in all_texts:
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if ex < ox - 3 and abs(ey - oy) < 38:
            t = (e.text or '').strip()
            if t and _STRIP_NAME_RE.match(t):
                d = ox - ex
                if d < best_dist:
                    best_dist = d
                    best = t
    return best


_LOOP_LIKE = re.compile(r'^[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}$')
_LOOP_ALPHA_DIGIT = re.compile(r'^[A-Za-z]\d{1,4}$')


def _classify_column_text(
    texts: list[dict],
) -> tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    circuit_desc: Optional[str] = None
    loop_id: Optional[str] = None
    terminal_no: Optional[int] = None
    unknown_busi: Optional[str] = None
    for item in texts:
        label = item['label']
        if label.isdigit():
            tn = int(label)
            if terminal_no is None:
                terminal_no = tn
            continue
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
        if any(c.isalpha() for c in label):
            if circuit_desc is None:
                circuit_desc = label
            elif unknown_busi is None:
                unknown_busi = label
            continue
        if unknown_busi is None:
            unknown_busi = label
        elif circuit_desc is None:
            circuit_desc = label
    return circuit_desc, terminal_no, loop_id, unknown_busi


# ===================================================================
# TerminalStripAnalyzer
# ===================================================================
class TerminalStripAnalyzer:
    """Analyze a terminal-strip DWG and emit cable_topology records."""
    MIN_VERTICAL_LENGTH = 20.0

    def _find_cabinet_name(self, texts: list) -> Optional[str]:
        """Find cabinet name from text containing '端子排图'."""
        for e in texts:
            txt = (e.text or '').strip()
            if '端子排图' in txt:
                name = txt.replace('端子排图', '').strip()
                return name if name else txt
        return None

    def analyze(self, doc: Document) -> list[dict]:
        self._cabinet_name = None
        emitted: list[dict] = []
        lines: list[LineGeometry] = []
        texts: list = []
        for e in doc.entities:
            if isinstance(e, LineGeometry):
                lines.append(e)
            elif isinstance(e, (TextEntity, AttributeEntity)):
                texts.append(e)
        self._cabinet_name = self._find_cabinet_name(texts)
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
        for cable_id, groups in cable_groups.items():
            records = self._analyze_one_cable(
                cable_id, groups['horiz'], groups['vert'], texts,
            )
            emitted.extend(records)
        return emitted

    def _find_conductor_no(self, vx: float, h_y: float, texts: list) -> Optional[int]:
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

    def _find_remote_cabinet(self, h_y: float, texts: list) -> Optional[str]:
        """Find remote cabinet name from ATTRIB with tag='EQUNAME'
        near the horizontal line y (to the right of cable labels)."""
        candidates = []
        for e in texts:
            if not isinstance(e, AttributeEntity):
                continue
            if (e.tag or '').upper() != 'EQUNAME':
                continue
            cf = getattr(e, 'custom_fields', None) or {}
            ex = cf.get('x')
            ey = cf.get('y')
            if ex is None or ey is None:
                continue
            if abs(ey - h_y) > 10:
                continue
            val = (e.text or '').strip()
            if val:
                candidates.append((ex, val))
        if not candidates:
            return None
        # Pick the rightmost EQUNAME (farthest from cable labels on the left)
        candidates.sort(key=lambda c: -c[0])
        return candidates[0][1]

    def _analyze_one_cable(self, cable_id, horizontals, verticals, texts):
        if not horizontals:
            return []
        h = horizontals[0]
        hpts = list(h.points or [])
        h_y = hpts[0].y if hpts else 0.0
        cabinet_name_remote = self._find_remote_cabinet(h_y, texts)
        records: list[dict] = []
        for v in verticals:
            pts = list(v.points or [])
            if len(pts) < 2:
                continue
            vx = pts[0].x
            dy = pts[-1].y - pts[0].y
            if abs(dy) < self.MIN_VERTICAL_LENGTH:
                continue
            d0 = abs(pts[0].y - h_y)
            d1 = abs(pts[-1].y - h_y)
            if d0 < d1:
                corner_y = pts[0].y
                end_y = pts[-1].y
            else:
                corner_y = pts[-1].y
                end_y = pts[0].y
            conductor_no = self._find_conductor_no(vx, h_y, texts)
            column_texts = _collect_texts_along_vertical(vx, corner_y, end_y, texts)
            circuit_desc, terminal_no, loop_id, _ = _classify_column_text(column_texts)
            strip_name: Optional[str] = None
            if terminal_no is not None:
                for item in column_texts:
                    if item['label'] == str(terminal_no):
                        strip_name = _find_strip_name(item['x'], item['y'], texts)
                        break
            records.append({
                'cable_id': cable_id,
                'conductor_no': conductor_no,
                'strip_name': strip_name,
                'terminal_no': terminal_no,
                'terminal_no_remote': None,
                'cabinet_name': self._cabinet_name,
                'cabinet_name_remote': cabinet_name_remote,
                'circuit_desc': circuit_desc,
                'loop_id': loop_id,
                'source_type': 'terminal_strip',
            })
        return records


# ===================================================================
# CircuitLoopAnalyzer (回路图)
# ===================================================================
class CircuitLoopAnalyzer:
    """Analyze a circuit-loop DWG and emit cable_topology records.

    Circuit loop drawings use block-attribute pairs where each core
    of a cable is a horizontal line with ATTRIB tags carrying:

      WireSerial    —  cable_id(core_no)  e.g. "ZL-307ZF(1)"
      WireDescription —  description       e.g. "直流电源+"
      LoopCode      —  loop code          e.g. "+KZ1"
      NO            —  left/right terminal e.g. "X2:1" / "9D:1"
      WIRENO        —  core number        e.g. "1", "2"
      InOut         —  direction
    """

    @staticmethod
    def _find_cabinet(
        tx: float, ty: float, texts: list[dict],
    ) -> Optional[str]:
        """Find cabinet display name for terminal at (tx, ty) in a 回路图.

        Business logic (from user spec):
          1. The terminal is enclosed by a cabinet (dashed rectangle).
          2. The cabinet name text block is ABOVE the terminal (higher y,
             since DWG origin is bottom-left).
          3. Among all such blocks, pick the one with minimum dx+dy from
             the terminal.
          4. The name must contain at least one of 屏/柜/箱.
          5. Text to the LEFT of the cabinet name (same y range) is the
             cabinet location, appended as "location name".

        Returns "location name" or "name" (if no location found), or
        None.
        """
        candidates: list[tuple[float, float, str, float, float]] = []
        for t in texts:
            val = t['val']
            if any(kw in val for kw in ('屏', '柜', '箱')):
                ex = t['x']
                ey = t['y']
                if ex is not None and ey is not None and ey > ty:
                    dx = abs(ex - tx)
                    dy = abs(ey - ty)
                    candidates.append((dx, dy, val, ex, ey))

        if not candidates:
            return None

        # Pick the cabinet name with smallest dx; if tied, smaller dy.
        candidates.sort(key=lambda r: (r[0], r[1]))
        _, _, cab_name, cab_x, cab_y = candidates[0]

        # Find location text to the left of cabinet name (same line)
        _LOCATION_MAX_DX = 200.0
        _LOCATION_DY_TOLERANCE = 3.0
        location = None
        best_dx = float('inf')
        for t in texts:
            val = t['val']
            if not val:
                continue
            ex = t['x']
            ey = t['y']
            if ex is not None and ey is not None and ex < cab_x:
                if abs(ey - cab_y) < _LOCATION_DY_TOLERANCE and val != cab_name:
                    dx = cab_x - ex
                    if dx < best_dx and dx < _LOCATION_MAX_DX:
                        best_dx = dx
                        location = val

        if location:
            return f"{location}-{cab_name}"
        return cab_name

    def analyze(self, doc: Document) -> list[dict]:
        attribs: list[dict] = []
        for e in doc.entities:
            if isinstance(e, (AttributeEntity, TextEntity)):
                cf = getattr(e, 'custom_fields', None) or {}
                x = cf.get('x')
                y = cf.get('y')
                if x is None or y is None:
                    continue
                tag = e.tag if isinstance(e, AttributeEntity) else ''
                attribs.append({
                    'tag': tag,
                    'val': (e.text or '').strip(),
                    'x': x,
                    'y': y,
                })

        # Detect cables from WireSerial entries
        cable_cores: dict[str, dict[int, dict]] = {}
        for a in attribs:
            if a['tag'] == 'WireSerial':
                m = _WIRE_SERIAL.match(a['val'])
                if m:
                    cid = m.group(1)
                    core = int(m.group(2))
                    if cid not in cable_cores:
                        cable_cores[cid] = {}
                    cable_cores[cid][core] = {
                        'x': a['x'],
                        'y': a['y'],
                    }

        # Pre-scan: collect horizontal LINE/LWPOLYLINE for core-line detection
        core_lines: list[dict] = []
        for e in doc.entities:
            if not isinstance(e, LineGeometry):
                continue
            pts = list(e.points or [])
            if len(pts) < 2:
                continue
            ys = [p.y for p in pts]
            xs = [p.x for p in pts]
            if max(ys) - min(ys) > 3:
                continue
            core_lines.append({
                'y': ys[0],
                'x_min': min(xs),
                'x_max': max(xs),
            })

        # Group NO tags and cable-level ATTRIB by y position.
        # Store (x, y, val) triples so we know terminal position for cabinet search.
        no_tags_by_y: dict[int, list[tuple[float, float, str]]] = {}
        cable_attribs: dict[str, list[dict]] = {}
        for a in attribs:
            if a['tag'] == 'NO' and ':' in a['val']:
                key = round(a['y'] * 2)
                no_tags_by_y.setdefault(key, []).append((a['x'], a['y'], a['val']))
            elif a['tag'] in ('WireDescription', 'LoopCode', 'WIRECODE'):
                cable_attribs.setdefault(a['tag'], []).append(a)

        records: list[dict] = []
        for cid in sorted(cable_cores.keys()):
            cores = cable_cores[cid]
            core_order = sorted(cores.items(), key=lambda kv: -kv[1]['y'])

            # Cabinet info is the same for all cores of a cable.
            # Compute once using the first core that has valid terminals.
            cabinet_local: Optional[str] = None
            cabinet_remote: Optional[str] = None

            for i, (core, info) in enumerate(core_order):
                wx = info['x']
                wy = info['y']

                # Step 1: find the core line — closest horizontal line
                # within ±30mm of WireSerial y. Prefer a line that
                # spans wx (typical WS-on-left layout); if none,
                # fall back to the closest line in the core area
                # (x_min >= 250) — this picks the main core line
                # even when WS is placed to its right
                # (e.g. 110037-381(5) at x=449 vs line x_max=414)
                # while excluding narrow formatting lines.
                _CORE_LINE_TOLERANCE = 30.0
                core_y = wy
                best_cl: Optional[dict] = None
                best_dy = float('inf')
                # Pass 1: find closest line that spans wx
                for cl in core_lines:
                    if cl['x_min'] <= wx <= cl['x_max']:
                        dy = abs(cl['y'] - wy)
                        if dy < best_dy and dy <= _CORE_LINE_TOLERANCE:
                            best_dy = dy
                            best_cl = cl
                # Pass 2: if no spanning line, find closest line that
                # is both in the core x-region (x_min >= 200) and has
                # a substantial span (>= 50mm). This picks the main
                # core line when WS is placed to its right, while
                # excluding left-side formatting lines (x < 200) and
                # right-side tick marks (span < 50mm).
                if best_cl is None:
                    for cl in core_lines:
                        if (
                            cl['x_min'] >= 200
                            and cl['x_max'] - cl['x_min'] >= 50.0
                        ):
                            dy = abs(cl['y'] - wy)
                            if dy < best_dy and dy <= _CORE_LINE_TOLERANCE:
                                best_dy = dy
                                best_cl = cl
                core_line_x_min = best_cl['x_min'] if best_cl else None
                core_line_x_max = best_cl['x_max'] if best_cl else None
                if best_cl is not None:
                    core_y = best_cl['y']

                # Step 2: find NO tags by matching y position (same row).
                # Terminals sit on both sides of the WireSerial in a
                # [left terminal] [WS] [right terminal] layout.
                # Pick the closest tag on each side of WS. When WS
                # is at the far right (no tags to its right), use
                # the largest x-gap in the bucket to separate local
                # and remote terminal groups.
                key = round(wy * 2)
                bucket_tags: list[tuple[float, float, str]] = []
                for dk in (-2, -1, 0, 1, 2):
                    bucket_tags.extend(no_tags_by_y.get(key + dk, []))

                _MIN_GAP = 50.0
                split_x: Optional[float] = None
                if len(bucket_tags) >= 2:
                    st = sorted(bucket_tags, key=lambda t: t[0])
                    mg = 0.0
                    for j in range(1, len(st)):
                        g = st[j][0] - st[j-1][0]
                        if g > mg:
                            mg = g
                            split_x = (st[j][0] + st[j-1][0]) / 2
                    if mg < _MIN_GAP:
                        split_x = None

                left_candidate: Optional[tuple[float, float, str]] = None
                right_candidate: Optional[tuple[float, float, str]] = None
                if bucket_tags:
                    left_of_ws = [t for t in bucket_tags if t[0] < wx]
                    right_of_ws = [t for t in bucket_tags if t[0] > wx]

                    # Left side: apply split filter to exclude
                    # remote-area tags when WS is on the right
                    if left_of_ws:
                        pool = left_of_ws
                        if split_x is not None:
                            pool = [t for t in pool if t[0] < split_x]
                        if pool:
                            left_candidate = min(
                                pool, key=lambda t: abs(t[0] - wx)
                            )

                    # Right side: closest tag, no filter needed
                    if right_of_ws:
                        right_candidate = min(
                            right_of_ws, key=lambda t: abs(t[0] - wx)
                        )
                    elif left_candidate and split_x is not None and not right_of_ws:
                        # WS at far right — pick the remote-side tag
                        remote_side = [
                            t for t in bucket_tags if t[0] > split_x
                        ]
                        if remote_side:
                            right_candidate = min(
                                remote_side, key=lambda t: abs(t[0] - split_x)
                            )

                left_terminal = left_candidate[2] if left_candidate else None
                left_terminal_x = left_candidate[0] if left_candidate else None
                left_terminal_y = left_candidate[1] if left_candidate else None

                right_terminal = right_candidate[2] if right_candidate else None
                right_terminal_x = right_candidate[0] if right_candidate else None
                right_terminal_y = right_candidate[1] if right_candidate else None

                # Step 2a: Cabinet detection — once per cable on the
                # first valid core. Search upward from each terminal
                # position for text containing 屏/柜/箱.
                if i == 0 and left_terminal_y is not None and cabinet_local is None:
                    cabinet_local = self._find_cabinet(
                        left_terminal_x, left_terminal_y, attribs,
                    )
                if i == 0 and right_terminal_y is not None and cabinet_remote is None:
                    cabinet_remote = self._find_cabinet(
                        right_terminal_x, right_terminal_y, attribs,
                    )

                # Step 3: circuit_desc/loop_id — find the nearest
                # WireDescription and LoopCode ATTRIB to the cable's
                # y range (scanning within 80mm y of core_y).
                circuit_desc = None
                loop_id = None
                for tag in ('WireDescription', 'LoopCode'):
                    candidates = cable_attribs.get(tag, [])
                    best = None
                    best_dy = float('inf')
                    for a in candidates:
                        dy = abs(a['y'] - core_y)
                        if dy < best_dy and dy < 80:
                            best_dy = dy
                            best = a['val']
                    if tag == 'WireDescription':
                        circuit_desc = best
                    else:
                        loop_id = best

                # Extract strip_name:terminal_no from left terminal
                strip_name = None
                terminal_no = None
                if left_terminal and ':' in left_terminal:
                    parts = left_terminal.split(':', 1)
                    strip_name = parts[0].strip()
                    try:
                        terminal_no = int(parts[1].strip())
                    except ValueError:
                        pass

                # Remote terminal (full ID string, e.g. "9D:1")
                terminal_no_remote = right_terminal

                records.append({
                    'cable_id': cid,
                    'conductor_no': core,
                    'strip_name': strip_name,
                    'terminal_no': terminal_no,
                    'terminal_no_remote': terminal_no_remote,
                    'cabinet_name': cabinet_local,
                    'cabinet_name_remote': cabinet_remote,
                    'circuit_desc': circuit_desc,
                    'loop_id': loop_id,
                    'source_type': 'circuit_loop',
                })
        return records


# ===================================================================
# CableScheduleAnalyzer (电缆清册 / 接线表) — minimal stub for V6.5.1
# ===================================================================
class CableScheduleAnalyzer:
    """Analyzer for cable schedules (电缆清册 / 接线表 / 电缆联系图).

    These drawings are typically tabular: each row is a cable, columns
    carry cable_id, conductor_no, terminal_from, terminal_to, etc.
    The V6.5.1 release ships a *minimal* stub: it counts how many
    cable-id-shaped strings the document contains so the classification
    rate improves (even if no rows are persisted). The full table
    parser lands in V6.6.
    """

    #: Matches strings like "11003-311", "GY6-136", "ZL-307ZF"
    _CABLE_ID_LIKE = re.compile(r'\b([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})\b')

    def analyze(self, doc: Document) -> list[dict]:
        # Stub: identify distinct cable IDs visible in the document text.
        # Persist a synthetic "cable-level" record per unique ID so the
        # viewer surfaces it (with terminal_no=None to indicate no
        # detailed analysis yet).
        seen: set[str] = set()
        for e in doc.entities:
            if not isinstance(e, (TextEntity, AttributeEntity)):
                continue
            t = (e.text or '').strip()
            for m in self._CABLE_ID_LIKE.finditer(t):
                seen.add(m.group(1))
        return [
            {
                'cable_id': cid,
                'conductor_no': None,
                'strip_name': None,
                'terminal_no': None,
                'terminal_no_remote': None,
                'cabinet_name': None,
                'cabinet_name_remote': None,
                'circuit_desc': None,
                'loop_id': None,
                'source_type': 'cable_schedule',
            }
            for cid in sorted(seen)
        ]


# ===================================================================
# TopologyStage (replaces GraphBuilderStage)
# ===================================================================
# V6.5: dispatch by BusinessType. Each type maps to a single Analyzer
# (or None for types we haven't built yet — those count as "unmatched").
_ANALYZERS_BY_TYPE: dict[BusinessType, Any] = {
    BusinessType.CIRCUIT_LOOP: CircuitLoopAnalyzer,
    BusinessType.TERMINAL_STRIP: TerminalStripAnalyzer,
    BusinessType.CABLE_SCHEDULE: CableScheduleAnalyzer,
    # PROTECTION_DIAGRAM / PANEL_LAYOUT / MONITORING_SYSTEM / UNKNOWN
    # don't have analyzers yet — they're surfaced in the "unmatched" bucket.
}


class TopologyStage(Stage):
    """Build cable topology from Document IR.

    Run AFTER the Loader stage.
    Inputs:  ctx.document (Document IR)
    Outputs: rows in cable_topology (+ terminal_strips).

    V6.5: classifies each document with CompositeClassifier
    (keyword + geometry + layout signals) before dispatching.
    Documents whose classification has no analyzer land in the
    "unmatched_documents" view in the viewer.
    """

    name = 'topology_builder'

    def __init__(self, store, classifier: Optional[CompositeClassifier] = None) -> None:
        self._store = store
        self._classifier = classifier or CompositeClassifier()

    def run(self, ctx: Context) -> Context:
        doc = ctx.document
        if doc is None:
            ctx.error_msg = 'no document to build topology from'
            return ctx

        # V6.5: classify via the ensemble (replaces _classify_document).
        classification = self._classifier.classify(doc)
        doc.classification = classification
        doc_type = classification.primary.value
        ctx.document_type = doc_type
        ctx.classification = classification

        self._store.delete_topology_for_document(doc.content_hash)

        AnalyzerCls = _ANALYZERS_BY_TYPE.get(classification.primary)
        if AnalyzerCls is None:
            records = []
        else:
            analyzer = AnalyzerCls()
            records = analyzer.analyze(doc)

        for rec in records:
            self._store.upsert_cable_topology(
                cable_id=rec['cable_id'],
                conductor_no=rec['conductor_no'],
                strip_name=rec['strip_name'],
                terminal_no=rec['terminal_no'],
                terminal_no_remote=rec.get('terminal_no_remote'),
                cabinet_name=rec.get('cabinet_name'),
                cabinet_name_remote=rec.get('cabinet_name_remote'),
                circuit_desc=rec['circuit_desc'],
                loop_id=rec['loop_id'],
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
            'classification_primary': doc_type,
            'classification_confidence': classification.confidence,
            'classification_secondary': [
                {'type': bt.value, 'score': s}
                for bt, s in classification.secondary
            ],
            'unmatched': AnalyzerCls is None,
        }
        return ctx


__all__ = ['TopologyStage']
