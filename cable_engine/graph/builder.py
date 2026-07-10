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

        # V6.6: cabinet-region index from CabinetRegion IR entities
        # (TopologyStage populates them BEFORE invoking us). When the
        # array is empty (e.g. an old rescan before V6.6), the
        # cabinet-restricted filter is a no-op and the bucket +
        # V6.5.3 200-unit-threshold logic still applies.
        from ..ir import CabinetRegion as _CabReg_pre
        from .cabinet import CabinetGridIndex
        v66_cabinets: list[dict] = []
        for ent in doc.entities:
            if not isinstance(ent, _CabReg_pre):
                continue
            if ent.bbox is None:
                continue
            v66_cabinets.append({
                'id': ent.id,
                'name': ent.name,
                'location': ent.location,
                'display_name': ent.display_name,
                'text_label': ent.text_label,
                'bbox': ent.bbox,
            })
        # V6.7: spatial grid index for O(1) cabinet lookup.
        cabinet_grid = CabinetGridIndex(v66_cabinets)

        # Map each NO/ObjTerm.Name ATTRIB → its cabinet id (based on
        # bbox containment). Attribs without a containing cabinet
        # are NOT in this map — the bucket filter still considers them.
        v66_terminal_cab: dict = {}
        for a in attribs:
            if a['tag'] not in ('NO', 'ObjTerm.Name'):
                continue
            if ':' not in a['val']:
                continue
            cab_id = cabinet_grid.lookup(a['x'], a['y'])
            if cab_id is not None:
                v66_terminal_cab[(a['x'], a['y'])] = cab_id

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

        # Pre-scan: collect horizontal LINE/LWPOLYLINE for core-line detection.
        # V6.7: pre-sort by y so each per-core search is O(log L + window)
        # instead of O(L).
        import bisect as _bisect
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
        core_lines.sort(key=lambda cl: cl['y'])
        _core_ys = [cl['y'] for cl in core_lines]

        # Group NO tags and cable-level ATTRIB by y position.
        # Store (x, y, val, tag) tuples so we know the tag type for the
        # XB-ownership filter (ObjTerm.Name tags can only be claimed by
        # the WS closest to them at the same y).
        no_tags_by_y: dict[int, list[tuple[float, float, str, str]]] = {}
        cable_attribs: dict[str, list[dict]] = {}
        # WireSerial x positions grouped by y-key, used by the
        # ObjTerm.Name ownership filter below.
        ws_x_by_y: dict[int, set[float]] = {}
        for a in attribs:
            if a['tag'] in ('NO', 'ObjTerm.Name') and ':' in a['val']:
                key = round(a['y'] * 2)
                no_tags_by_y.setdefault(key, []).append((a['x'], a['y'], a['val'], a['tag']))
            elif a['tag'] in ('WireDescription', 'LoopCode', 'WIRECODE'):
                cable_attribs.setdefault(a['tag'], []).append(a)
            elif a['tag'] == 'WireSerial':
                key = round(a['y'] * 2)
                ws_x_by_y.setdefault(key, set()).add(a['x'])

        # V6.5.3: Pre-compute NO tag ownership — for every NO tag,
        # store (closest_ws_dist, second_closest_ws_dist) across all
        # WSs at the same y-bucket. Tags where the 2nd-closest WS is
        # within _NO_TAG_SHARE_DISTANCE are "shared" between the
        # closest and 2nd-closest WSs (e.g. VI:1 between 11037-384
        # and 11003-387). Tags with a far-away 2nd-closest WS are
        # owned exclusively by their closest WS (e.g. X5:7 from
        # 11037-383, X4:20 owned by 11037-384 only). This filter
        # prevents the gap-split from picking noise tags like X5:7
        # / III:29 when 3 WSs share the same row (e.g. 11037-387
        # cores 3-4 in D0210-16, where the noise tags are ~600
        # units from the target WS but only ~50 units from another
        # WS in the row).
        _NO_TAG_SHARE_DISTANCE = 200.0
        no_tag_ownership: dict[tuple[float, float, str], tuple[Optional[float], Optional[float]]] = {}
        for tag_key, tag_list in no_tags_by_y.items():
            for tag in tag_list:
                tx, ty, tval, ttype = tag
                if ttype != 'NO':
                    continue
                closest_d: Optional[float] = None
                second_d: Optional[float] = None
                for dk in (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5):
                    for ws_x in ws_x_by_y.get(tag_key + dk, set()):
                        d = abs(tx - ws_x)
                        if closest_d is None or d < closest_d:
                            second_d = closest_d
                            closest_d = d
                        elif second_d is None or d < second_d:
                            second_d = d
                no_tag_ownership[(tx, ty, tval)] = (closest_d, second_d)

        records: list[dict] = []
        for cid in sorted(cable_cores.keys()):
            cores = cable_cores[cid]
            core_order = sorted(cores.items(), key=lambda kv: -kv[1]['y'])

            # Cabinet info is the same for all cores of a cable.
            # Compute once using the first core that has valid terminals.
            cabinet_local: Optional[str] = None
            cabinet_remote: Optional[str] = None
            # V6.6.2: per-cable per-side cabinet tracking. Two facts:
            #   1. Every terminal on a given side (LEFT or RIGHT of WS)
            #      of a cable belongs to the same cabinet.
            #   2. WS itself is "sandwiched between" the two cabinets
            #      and does NOT belong to either one — it just connects
            #      them. (This is why V6.6 Phase 8's "WS in same cabinet
            #      as terminal" gate was wrong: WS is in neither.)
            # Once the first core finds a left/right terminal, the
            # cabinet for that side is locked. Subsequent cores of the
            # same cable on the same side must find their terminal in
            # the SAME cabinet. This prevents the y-bucket search from
            # picking up a left terminal from a neighboring cable when
            # V6.5.3's 200-unit ownership threshold doesn't cleanly
            # separate them (e.g. several cables on the same row whose
            # left X4:N tags are all clustered within 200 units of
            # each other in the same cabinet).
            left_side_cabinet: Optional[str] = None
            right_side_cabinet: Optional[str] = None

            for i, (core, info) in enumerate(core_order):
                wx = info['x']
                wy = info['y']

                # Step 1: find the core line — closest horizontal line
                # within ±30mm of WireSerial y. Prefer a line that
                # spans wx (typical WS-on-left layout); if none,
                # fall back to the closest line in the core area
                # (x_min >= 200 and span >= 50mm). V6.7: uses bisect
                # to avoid scanning every core_line per core.
                _CORE_LINE_TOLERANCE = 30.0
                core_y = wy
                best_cl: Optional[dict] = None
                best_dy = float('inf')
                lo = _bisect.bisect_left(_core_ys, wy - _CORE_LINE_TOLERANCE)
                hi = _bisect.bisect_right(_core_ys, wy + _CORE_LINE_TOLERANCE)
                window = core_lines[lo:hi]
                # Pass 1: find closest line that spans wx
                for cl in window:
                    if cl['x_min'] <= wx <= cl['x_max']:
                        dy = abs(cl['y'] - wy)
                        if dy < best_dy:
                            best_dy = dy
                            best_cl = cl
                # Pass 2: if no spanning line, find closest line that
                # is both in the core x-region (x_min >= 200) and has
                # a substantial span (>= 50mm). This picks the main
                # core line when WS is placed to its right, while
                # excluding left-side formatting lines (x < 200) and
                # right-side tick marks (span < 50mm).
                if best_cl is None:
                    for cl in window:
                        if (
                            cl['x_min'] >= 200
                            and cl['x_max'] - cl['x_min'] >= 50.0
                        ):
                            dy = abs(cl['y'] - wy)
                            if dy < best_dy:
                                best_dy = dy
                                best_cl = cl
                core_line_x_min = best_cl['x_min'] if best_cl else None
                core_line_x_max = best_cl['x_max'] if best_cl else None
                # V6.5.2 fix: do NOT update core_y to the line's y.
                # When multiple cores share a single bus line (e.g.
                # 11003-381 cores 4-6 all connect to a y=355 bus),
                # best_cl['y'] is the SAME for all of them, which
                # caused every core to receive the SAME description
                # (the one closest to the bus y). The
                # WireDescription / LoopCode ATTRIBs are placed on
                # each individual core's row at WS y, not the bus
                # y, so the ATTRIB search MUST use wy (the WS y).
                # (previously: `if best_cl is not None: core_y = ...`)
                _ = core_y  # keep reference for any downstream code

                # Step 2: find NO tags by matching y position (same row).
                # Terminals sit on both sides of the WireSerial in a
                # [left terminal] [WS] [right terminal] layout.
                # Pick the closest tag on each side of WS. When WS
                # is at the far right (no tags to its right), use
                # the largest x-gap in the bucket to separate local
                # and remote terminal groups.
                # V6.5.2: bucket widened from ±2 to ±5 keys (=±2.5mm
                # instead of ±1mm) to catch shallow L-shape drops
                # where the terminal sits a couple of mm below the
                # WS row. Deeper L/Z-shapes still need V6.5.4.
                # V6.5.3: ObjTerm.Name tags (XB column, past the line
                # end inside anonymous blocks) are "owned" by the WS
                # at the same y whose x is closest to the tag. This
                # excludes the XB terminal from a left-side WS that
                # shares the row with a right-side WS (e.g. 11037-384
                # sharing y=395 with 11003-387: XB:2 belongs to
                # 11003-387, not 11037-384). NO tags (X4:20, VI:1)
                # remain shared between cables at the same y.
                # V6.6 cabinet-restricted bucket (Phase 8): REMOVED in
                # V6.6.1 because legitimate cross-cabinet wire pairs
                # were being filtered out. Example: D0210-15 has WS
                # `11003-311(1)` at x=286 (in cab_002); its left
                # terminal `21CD:1` at x=186 is in cab_001 — a
                # DIFFERENT cabinet from the WS, but legitimately the
                # wire's left endpoint. The filter rejected 21CD:1,
                # leaving the cable with an empty left terminal.
                #
                # V6.5.3's distance-based NO-tag ownership (200-unit
                # share threshold) is sufficient for noise removal —
                # when multiple WSs share a row, NO tags with both
                # closest + 2nd-closest WS within 200 units are
                # correctly marked as shared; tags with only one WS
                # within 200 units are owned by that WS alone.
                #
                # The V6.6 spatial data is still used elsewhere:
                # - cabinet_name / cabinet_name_remote lookups (more
                #   reliable than the V6.5 _find_cabinet text search)
                # - viewer UI cabinet-aware tabs
                # - V6.6.2 same-side cabinet constraint (this section)
                # - future V6.7 WireTracer
                #
                # V6.6.2 reintroduces a CABINET constraint — but per
                # cable per side, NOT per WS. Per user axioms:
                #   1. Every terminal on a given side (LEFT or RIGHT
                #      of WS) of a cable belongs to the same cabinet.
                #   2. WS is sandwiched between the two cabinets and
                #      belongs to neither.
                # So tracking is per-cable, per-side: the first core
                # that finds a left terminal locks the cable's
                # left-side cabinet id; subsequent cores of the same
                # cable on the same side must have their terminals in
                # that cabinet. This is implemented just below the
                # `_tag_owned_by_current` filter (see the
                # `if left_side_cabinet is not None and left_of_ws`
                # block) and prevents the y-bucket from picking up a
                # neighboring cable's left terminal when V6.5.3's
                # 200-unit ownership threshold doesn't cleanly
                # separate them. Empty records are preferred over
                # wrong ones per user requirement.
                key = round(wy * 2)
                bucket_tags: list[tuple[float, float, str, str]] = []
                for dk in (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5):
                    bucket_tags.extend(no_tags_by_y.get(key + dk, []))

                # Collect every WS x position that lives in this y-bucket
                # (a cable's WS at the same y belongs to the same row).
                ws_x_at_y: set[float] = set()
                for dk in (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5):
                    ws_x_at_y.update(ws_x_by_y.get(key + dk, set()))

                def _tag_owned_by_current(
                    tag_x: float, tag_y: float, tag_val: str, tag_type: str,
                ) -> bool:
                    """ObjTerm.Name tags (XB column, past line end): owned
                    exclusively by the WS at this y whose x is closest
                    to the tag.

                    NO tags (X4:N, VI:N, etc.): shared between cables if
                    both the closest WS and the 2nd-closest WS at the
                    same y are within _NO_TAG_SHARE_DISTANCE of the tag
                    (e.g. VI:1 between 11037-384 and 11003-387 at
                    ~30 and ~120 units — both within 200 → shared).
                    Otherwise the tag is owned exclusively by its closest
                    WS (e.g. X4:20 has 2nd-closest WS at ~220 > 200,
                    so 11003-387 cannot claim it; X5:7 has 2nd-closest
                    at ~620 → only 11037-383 can claim it)."""
                    if tag_type == 'ObjTerm.Name':
                        if len(ws_x_at_y) <= 1:
                            return True
                        closest = min(ws_x_at_y, key=lambda w: abs(w - tag_x))
                        return closest == wx
                    # NO tag: use pre-computed ownership
                    ownership = no_tag_ownership.get((tag_x, tag_y, tag_val))
                    if ownership is None:
                        return False
                    closest_d, second_d = ownership
                    if closest_d is None:
                        return False
                    current_d = abs(tag_x - wx)
                    if second_d is not None and second_d <= _NO_TAG_SHARE_DISTANCE:
                        # Shared between closest and 2nd-closest WSs
                        return current_d == closest_d or current_d == second_d
                    # Exclusive — only the closest WS can claim
                    return current_d == closest_d

                _MIN_GAP = 50.0
                left_candidate: Optional[tuple[float, float, str, str]] = None
                right_candidate: Optional[tuple[float, float, str, str]] = None
                if bucket_tags:
                    left_of_ws = [
                        t for t in bucket_tags
                        if t[0] < wx and _tag_owned_by_current(t[0], t[1], t[2], t[3])
                    ]
                    right_of_ws = [
                        t for t in bucket_tags
                        if t[0] > wx and _tag_owned_by_current(t[0], t[1], t[2], t[3])
                    ]

                    # V6.6.2: same-side cabinet constraint. Once any
                    # core of this cable has established the cabinet
                    # for the LEFT or RIGHT side, restrict subsequent
                    # same-side terminal candidates to that cabinet.
                    # Cross-side is unaffected (left cabinet != right
                    # cabinet by user axiom 2), so cross-cabinet wire
                    # pairs still match. The WS itself is OUTSIDE any
                    # single cabinet (it's between the two endpoint
                    # cabinets), which is why per-cable per-side is
                    # the right granularity — not "WS's cabinet".
                    #
                    # When the side cabinet is unknown (first core, or
                    # no terminal found on this side yet), no filter
                    # is applied — fall through to the V6.5.3 closest-
                    # ownership logic. This means:
                    #   - The first core of every cable uses the
                    #     existing V6.5.3 logic (no degradation).
                    #   - Subsequent cores of the same cable get an
                    #     extra cabinet filter on top of V6.5.3.
                    #   - L/Z-shaped terminals at a different y
                    #     (already unfindable by the ±5 y-bucket) get
                    #     term=None — empty records are preferred
                    #     over wrong ones per user requirement.
                    #
                    # IMPORTANT: the filter is applied per-side AFTER
                    # the gap-split (in the gap-split branch), not on
                    # the raw left_of_ws pool. Reason: in the gap-
                    # split case, the same pool contains both LOCAL
                    # (will become left) and REMOTE (will become
                    # right) candidates. Filtering the whole pool
                    # against `left_side_cabinet` would wrongly remove
                    # candidates that are actually right-side terminals
                    # in a different cabinet (e.g. core 5 of 11003-381
                    # at x=449 has X1:10 in left_of_ws at x=429 — gap-
                    # split promotes it to right, but a left-cabinet
                    # filter on the raw pool would drop it).
                    #
                    # V6.6.2b tolerance: a candidate whose cabinet is
                    # UNKNOWN (`_ws_in_cabinet` returns None because
                    # it sits in a geometric gap between detected
                    # cabinet bboxes) is ACCEPTED, not filtered. The
                    # V6.6 cabinet detection is geometrically imperfect
                    # — terminal labels often sit in narrow gaps
                    # between two adjacent cabinet rectangles. Filtering
                    # those candidates against a specific cabinet id
                    # would falsely reject valid terminals (e.g.
                    # 11003-383 core 4's X5:8 at y=156 sits 9 units
                    # above cab_007's top edge and not in any other
                    # cabinet). Accepting the unknown-cabinet candidate
                    # is safer than rejecting it, per user preference
                    # ("empty > wrong" — but here we don't know which
                    # outcome is "wrong", so accept and let downstream
                    # use it).
                    def _passes_cab_filter(
                        tag_x: float, tag_y: float, side_cab: Optional[str],
                    ) -> bool:
                        if side_cab is None:
                            return True
                        tag_cab = cabinet_grid.lookup(tag_x, tag_y)
                        if tag_cab is None:
                            # Cabinet detection didn't see this point.
                            # Don't filter — accept.
                            return True
                        return tag_cab == side_cab
                    if right_of_ws:
                        # Standard layout: closest on each side.
                        # Apply cabinet filter per-side on the already-
                        # split left/right pools.
                        if left_of_ws:
                            left_of_ws = [
                                t for t in left_of_ws
                                if _passes_cab_filter(
                                    t[0], t[1], left_side_cabinet,
                                )
                            ]
                        if right_of_ws:
                            right_of_ws = [
                                t for t in right_of_ws
                                if _passes_cab_filter(
                                    t[0], t[1], right_side_cabinet,
                                )
                            ]
                        if left_of_ws:
                            left_candidate = min(
                                left_of_ws, key=lambda t: abs(t[0] - wx)
                            )
                        if right_of_ws:
                            right_candidate = min(
                                right_of_ws, key=lambda t: abs(t[0] - wx)
                            )
                    else:
                        # WS at far right: split left tags by largest
                        # x-gap into a local cluster (closer to WS)
                        # and a remote cluster (further away). Local
                        # = left terminal; remote = right terminal.
                        # V6.6.2: apply cabinet filters SEPARATELY to
                        # local_side and remote_side (after the split),
                        # not on the raw left_of_ws pool — see the
                        # comment above for why.
                        if len(left_of_ws) >= 2:
                            st = sorted(left_of_ws, key=lambda t: t[0])
                            mg = 0.0
                            mg_idx = 0
                            for j in range(1, len(st)):
                                g = st[j][0] - st[j-1][0]
                                if g > mg:
                                    mg = g
                                    mg_idx = j
                            if mg >= _MIN_GAP:
                                split_x = (st[mg_idx][0] + st[mg_idx-1][0]) / 2
                                local_side = [t for t in st if t[0] <= split_x]
                                remote_side = [t for t in st if t[0] > split_x]
                                # Per-side cabinet filter
                                if local_side:
                                    local_side = [
                                        t for t in local_side
                                        if _passes_cab_filter(
                                            t[0], t[1], left_side_cabinet,
                                        )
                                    ]
                                if remote_side:
                                    remote_side = [
                                        t for t in remote_side
                                        if _passes_cab_filter(
                                            t[0], t[1], right_side_cabinet,
                                        )
                                    ]
                                if local_side:
                                    left_candidate = min(
                                        local_side, key=lambda t: abs(t[0] - wx)
                                    )
                                if remote_side:
                                    right_candidate = min(
                                        remote_side, key=lambda t: abs(t[0] - split_x)
                                    )
                            else:
                                # Gap too small to split: just pick
                                # the closest left tag.
                                left_candidate = min(
                                    left_of_ws, key=lambda t: abs(t[0] - wx)
                                )
                        elif left_of_ws:
                            left_candidate = min(
                                left_of_ws, key=lambda t: abs(t[0] - wx)
                            )

                # V6.6.2: record side cabinet from the first terminal
                # found on that side. Once established, it gates all
                # subsequent same-side terminal searches for this cable.
                # Only recorded when the candidate is non-None AND the
                # side cabinet is still unknown (first core wins).
                if left_candidate is not None and left_side_cabinet is None:
                    left_side_cabinet = cabinet_grid.lookup(
                        left_candidate[0], left_candidate[1],
                    )
                if right_candidate is not None and right_side_cabinet is None:
                    right_side_cabinet = cabinet_grid.lookup(
                        right_candidate[0], right_candidate[1],
                    )

                left_terminal = left_candidate[2] if left_candidate else None
                left_terminal_x = left_candidate[0] if left_candidate else None
                left_terminal_y = left_candidate[1] if left_candidate else None

                right_terminal = right_candidate[2] if right_candidate else None
                right_terminal_x = right_candidate[0] if right_candidate else None
                right_terminal_y = right_candidate[1] if right_candidate else None

                # Step 2a: Cabinet detection — once per cable on the
                # first core that has a valid terminal. V6.6.2:
                # updated to accept ANY core with a valid terminal,
                # not just i==0 (if core 1's left terminal was empty
                # but core 2's wasn't, cabinet_local was previously
                # stuck at None). V6.6: prefer the spatial cabinet
                # lookup (the terminal position is inside a detected
                # dashed-rectangle cabinet bbox → use its display
                # name); fall back to the V6.5 text-search when no
                # spatial cabinet covers the terminal.
                if left_terminal_y is not None and cabinet_local is None:
                    cabinet_local = cabinet_grid.lookup_name(
                        left_terminal_x, left_terminal_y,
                    ) or self._find_cabinet(
                        left_terminal_x, left_terminal_y, attribs,
                    )
                if right_terminal_y is not None and cabinet_remote is None:
                    cabinet_remote = cabinet_grid.lookup_name(
                        right_terminal_x, right_terminal_y,
                    ) or self._find_cabinet(
                        right_terminal_x, right_terminal_y, attribs,
                    )

                # Step 3: circuit_desc/loop_id — find the nearest
                # WireDescription and LoopCode ATTRIB to the cable's
                # y range (scanning within 80mm y of wy).
                # V6.5.2: ATTRIB search uses wy (WS y) instead of
                # core_y (line y). The previous `core_y` was
                # overwritten to the bus line's y when cores shared a
                # bus, which collapsed all of them onto the same
                # description (the one closest to the bus row).
                # V6.5.2: x-distance to WS is used as a tie-breaker
                # when multiple ATTRIBs at the same y belong to
                # different cables' description columns.
                circuit_desc = None
                loop_id = None
                for tag in ('WireDescription', 'LoopCode'):
                    candidates = cable_attribs.get(tag, [])
                    best = None
                    best_dy = float('inf')
                    best_dx = float('inf')
                    for a in candidates:
                        dy = abs(a['y'] - wy)
                        if dy > 80:
                            continue
                        dx = abs(a['x'] - wx)
                        # Pick the closest in y first; if y tie, pick
                        # the closer in x to the WS column.
                        if dy < best_dy or (dy == best_dy and dx < best_dx):
                            best_dy = dy
                            best_dx = dx
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
# CableScheduleAnalyzer (电缆清册 / 接线表) — V6.7 enhanced
# ===================================================================
class CableScheduleAnalyzer:
    """Analyzer for cable schedules (电缆清册 / 接线表 / 电缆联系图).

    These drawings are typically tabular: each row is a cable, columns
    carry cable_id, conductor_no, terminal_from, terminal_to, etc.

    Strategy (V6.7):
      1. Group text entities by Y-coordinate (row detection).
      2. Within each row, sort by X (column detection).
      3. Detect header row via known Chinese keywords.
      4. Parse data rows into topology records.

    If table parsing fails (no header detected), falls back to simple
    cable-ID extraction.
    """

    #: Cable-ID regex for fallback
    _CABLE_ID_LIKE = re.compile(r'\b([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})\b')

    #: Y-tolerance for grouping into the same row (document units)
    _ROW_TOL = 3.0

    #: Known Chinese column headers in cable schedule tables.
    #: Each entry: (keyword, target_field, is_cable_id, is_conductor)
    _HEADERS: list[tuple[re.Pattern, str, bool, bool]] = [
        # 电缆编号 / 编号 / 序号 -> cable_id
        (re.compile(r'电缆编号|电缆编[号碼]|编[号號]|序号|序號|电缆(?:名称|ID|编号)'), 'cable_id', True, False),
        # 电缆型号 / 型号 / 规格 -> cable_type (stored as circuit_desc)
        (re.compile(r'电缆型号|型号|规格|电[缆线]型号'), 'circuit_desc', False, False),
        # 起点 / 起点柜 / 起点端子 -> strip_name
        (re.compile(r'起点(?:柜|端子)?|起始|始端|来源|來[源渊]|本端'), 'strip_name', False, False),
        # 终点 / 终点柜 / 终点端子 -> terminal_no_remote
        (re.compile(r'终点(?:柜|端子)?|終点|末端|目标|目的|对端'), 'terminal_no_remote', False, False),
        # 芯数 / 线芯 -> conductor_no
        (re.compile(r'芯数|线芯|线[芯心]数|芯线数|缆芯'), 'conductor_no', False, True),
        # 回路编号 / 回路 -> loop_id
        (re.compile(r'回路(?:编号|编[号號])?|回[路線]编号'), 'loop_id', False, False),
        # 备注 -> circuit_desc (if not cable_type)
        (re.compile(r'备注|注|说明|說[明文]'), 'circuit_desc', False, False),
        # 柜体 / 柜 / 机柜 -> cabinet_name
        (re.compile(r'柜体|机柜|安装位置|所在柜|所属柜'), 'cabinet_name', False, False),
    ]

    def analyze(self, doc: Document) -> list[dict]:
        text_entities = [
            e for e in doc.entities
            if isinstance(e, (TextEntity, AttributeEntity)) and (e.text or '').strip()
        ]
        if not text_entities:
            return []

        # Step 1: Try table-based parsing
        records = self._parse_table(text_entities)
        if records:
            return records

        # Step 2: Fallback — extract distinct cable IDs
        return self._extract_cable_ids(text_entities)

    def _parse_table(self, entities: list) -> list[dict]:
        """Attempt to detect and parse a cable schedule table.

        Returns records if successful, empty list otherwise.
        """
        # Group entities by Y coordinate
        rows: dict[float, list] = {}
        for e in entities:
            x, y = self._entity_xy(e)
            if x is None or y is None:
                continue
            bucket = round(y / self._ROW_TOL) * self._ROW_TOL
            rows.setdefault(bucket, []).append((x, y, e.text.strip()))

        if not rows:
            return []

        # Sort rows by Y (descending = top to bottom in CAD), sort cells by X
        sorted_rows: list[list[tuple[float, str]]] = []
        for y_bucket in sorted(rows.keys(), reverse=True):
            cells = sorted(rows[y_bucket], key=lambda c: c[0])
            sorted_rows.append([(c[0], c[2]) for c in cells])

        if len(sorted_rows) < 2:
            return []

        # Detect header row — look for Chinese column keywords
        header_idx = None
        header_by_x: list[str] = []
        for i, row in enumerate(sorted_rows):
            texts = [t for _, t in row]
            joined = ' '.join(texts)
            # Check if any header keyword matches
            for pat, field, _, _ in self._HEADERS:
                if pat.search(joined):
                    header_idx = i
                    header_by_x = [t for _, t in row]
                    break
            if header_idx is not None:
                break

        if header_idx is None:
            return []  # No table header detected

        # Map column positions to topology fields
        col_map: list[tuple[int, str, bool, bool]] = []  # (col_idx, field, is_cable_id, is_conductor)
        for ci, header_text in enumerate(header_by_x):
            for pat, field, is_cid, is_cond in self._HEADERS:
                if pat.search(header_text):
                    col_map.append((ci, field, is_cid, is_cond))
                    break

        if not col_map:
            return []

        # Find cable_id column — required
        cid_col = next((c for c in col_map if c[2]), None)
        if cid_col is None:
            return []

        # Parse data rows (everything after header)
        records: list[dict] = []
        for row in sorted_rows[header_idx + 1:]:
            cell_texts = [t for _, t in row]
            cid = cell_texts[cid_col[0]] if cid_col[0] < len(cell_texts) else ''
            cid = cid.strip()
            if not cid or not self._CABLE_ID_LIKE.fullmatch(cid):
                continue

            rec: dict = {
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

            for col_idx, field, is_cid, is_cond in col_map:
                if is_cid:
                    continue  # already set
                val = cell_texts[col_idx].strip() if col_idx < len(cell_texts) else ''
                if not val:
                    continue
                if is_cond:
                    # Try to parse as integer
                    try:
                        rec['conductor_no'] = int(val)
                    except ValueError:
                        rec['circuit_desc'] = (rec.get('circuit_desc') or '') + f' {val}'
                elif field == 'strip_name':
                    rec['strip_name'] = val
                elif field == 'terminal_no_remote':
                    rec['terminal_no_remote'] = val
                elif field == 'cabinet_name':
                    rec['cabinet_name'] = val
                elif field == 'loop_id':
                    rec['loop_id'] = val
                elif field == 'circuit_desc':
                    existing = rec.get('circuit_desc') or ''
                    rec['circuit_desc'] = (existing + ' ' + val).strip()

            records.append(rec)

        return records

    def _extract_cable_ids(self, entities: list) -> list[dict]:
        """Fallback: extract distinct cable IDs from text."""
        seen: set[str] = set()
        for e in entities:
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

    @staticmethod
    def _entity_xy(e) -> tuple[Optional[float], Optional[float]]:
        cf = getattr(e, 'custom_fields', None) or {}
        x = cf.get('x') if isinstance(cf, dict) else None
        y = cf.get('y') if isinstance(cf, dict) else None
        if x is not None and y is not None:
            return (float(x), float(y))
        return (None, None)


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
    Outputs: rows in cable_topology (+ terminal_strips), plus V6.6
              cabinet regions in `cabinets` / `cabinet_terminals`.

    V6.5: classifies each document with CompositeClassifier
    (keyword + geometry + layout signals) before dispatching.
    Documents whose classification has no analyzer land in the
    "unmatched_documents" view in the viewer.

    V6.6: ALWAYS runs CabinetRegionAnalyzer regardless of business
    classification — cabinet boundaries are positional and present
    in many 回路图 drawings regardless of the analyzer dispatch.
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

        self._store.delete_cabinets_for_document(doc.content_hash)

        cabinet_records_pre: list = []
        cabinet_terminal_rows_pre: list = []

        AnalyzerCls = _ANALYZERS_BY_TYPE.get(classification.primary)
        if AnalyzerCls is None:
            records: list = []
        else:
            # V6.7: cabinet analysis runs BEFORE the analyzer so the
            # CircuitLoopAnalyzer sees CabinetRegion IR entities with
            # spatial containment info. Only 回路图 use cabinet rules;
            # other document types skip it entirely for performance.
            if classification.primary == BusinessType.CIRCUIT_LOOP:
                cabinet_records_pre = _run_cabinet_analyzer(doc)
                cabinet_terminal_rows_pre = _assign_cabinet_terminals(
                    doc, cabinet_records_pre,
                )

                # Inject CabinetRegion IR entities into the document.
                from ..ir import CabinetRegion as _CabRegion
                _cab_terminals_by_id: dict[str, list[str]] = {}
                for cab_id, tid, _k, _x, _y in cabinet_terminal_rows_pre:
                    _cab_terminals_by_id.setdefault(cab_id, []).append(tid)
                for cr in cabinet_records_pre:
                    bb = cr.boundary.bbox
                    cab_entity = _CabRegion(
                        id=cr.id,
                        source='dwg', page=1, confidence=1.0,
                        bbox=bb,
                        layer=cr.boundary.layer or '',
                        name=cr.name,
                        location=cr.location,
                        display_name=cr.display_name,
                        text_label=cr.text_label,
                        boundary_handle=cr.boundary.handle or '',
                        ltype=cr.boundary.ltype or '',
                        contained_terminal_ids=list(
                            _cab_terminals_by_id.get(cr.id, [])
                        ),
                    )
                    doc.add_entity(cab_entity)

            analyzer = AnalyzerCls()
            records = analyzer.analyze(doc)

        # V6.7: batch all writes via executemany.
        import json as _json
        _topo_rows: list[tuple] = []
        _strip_rows: list[tuple] = []
        for rec in records:
            _topo_rows.append((
                rec['cable_id'], rec['conductor_no'], rec['strip_name'],
                rec['terminal_no'], rec.get('terminal_no_remote'),
                rec.get('cabinet_name'), rec.get('cabinet_name_remote'),
                rec['circuit_desc'], rec['loop_id'],
                doc.content_hash, rec['source_type'],
            ))
            if rec['strip_name']:
                _strip_rows.append((
                    rec['strip_name'], None, doc.content_hash,
                ))
        if _topo_rows:
            self._store.bulk_upsert_cable_topology(_topo_rows)
        if _strip_rows:
            self._store.bulk_upsert_terminal_strips(_strip_rows)

        # V6.6: persist the cabinet-region rows computed BEFORE the
        # analyzer ran (so the analyzer could already use them).
        _cab_rows: list[tuple] = []
        for cr in cabinet_records_pre:
            bb = cr.boundary.bbox
            _cab_rows.append((
                cr.id,
                doc.content_hash,
                cr.name or None,
                cr.location or None,
                cr.display_name or None,
                cr.text_label or None,
                bb.x, bb.y, bb.w, bb.h,
                cr.boundary.layer or None,
                cr.boundary.handle or None,
                cr.boundary.ltype or None,
                _json.dumps(
                    [[p.x, p.y] for p in (cr.boundary.points or [])]
                ),
            ))
        if _cab_rows:
            self._store.bulk_upsert_cabinets(_cab_rows)
        _ct_rows: list[tuple] = [
            (cab_id, doc.content_hash, tid, kind, x, y)
            for cab_id, tid, kind, x, y in cabinet_terminal_rows_pre
        ]
        if _ct_rows:
            self._store.bulk_upsert_cabinet_terminals(_ct_rows)

        # V6.7+: persist text entities for full-text search
        self._store.delete_text_entities_for_document(doc.content_hash)
        _text_rows: list[tuple] = []
        for e in doc.entities:
            if isinstance(e, TextEntity):
                cf = getattr(e, 'custom_fields', {}) or {}
                _text_rows.append((
                    doc.content_hash, e.text, 'TEXT',
                    cf.get('x'), cf.get('y'),
                ))
            elif isinstance(e, AttributeEntity):
                cf = getattr(e, 'custom_fields', {}) or {}
                _text_rows.append((
                    doc.content_hash, e.text, 'ATTRIB',
                    cf.get('x'), cf.get('y'),
                ))
        if _text_rows:
            self._store.bulk_upsert_text_entities(_text_rows)

        ctx.result = {
            'cable_topology_count': len(records),
            'cabinet_count': len(cabinet_records_pre),
            'cabinet_terminal_count': len(cabinet_terminal_rows_pre),
            'classification_primary': doc_type,
            'classification_confidence': classification.confidence,
            'classification_secondary': [
                {'type': bt.value, 'score': s}
                for bt, s in classification.secondary
            ],
            'unmatched': AnalyzerCls is None,
        }
        # V6.6: explicit commit so a concurrent reader (or a sibling
        # worker with its own connection) sees cabinet + topology rows
        # before this process's connection closes. Without this,
        # WAL-mode SQLite occasionally drops the row grouping when
        # the next worker opens its own connection right after.
        try:
            self._store.commit()
        except Exception:
            pass
        return ctx


__all__ = ['TopologyStage']


# ---------------------------------------------------------------------------
# V6.6: cabinet-region wiring
# ---------------------------------------------------------------------------
def _run_cabinet_analyzer(doc) -> list:
    """Run CabinetRegionAnalyzer on doc. Returns a list of CabinetRecord
    with stable ids. Drops micro-rectangles < 4 units per side."""
    from .cabinet import CabinetRegionAnalyzer
    try:
        analyzer = CabinetRegionAnalyzer()
        records = analyzer.analyze(doc)
        keep: list = []
        for r in records:
            bb = r.boundary.bbox
            if bb.w < 4 or bb.h < 4:
                continue
            keep.append(r)
        return keep
    except Exception:
        return []


def _assign_cabinet_terminals(doc, cabinet_records) -> list:
    """For every cabinet, find NO / ObjTerm.Name ATTRIBs whose point
    falls inside its bbox. Returns a flat list of (cabinet_id,
    terminal_id, terminal_kind, x, y) tuples.

    The flat list preserves ALL containment relationships: the same
    terminal_id may appear at multiple positions (e.g. duplicate
    cabinet copies at different y), producing one row per occurrence.
    The caller / storage layer deduplicates by
    (cabinet_id, document_hash, terminal_id, terminal_kind)."""
    from .cabinet import assign_terminals_to_cabinets
    from ..ir import AttributeEntity

    terminals: list = []
    for ent in doc.entities:
        if not isinstance(ent, AttributeEntity):
            continue
        tag = getattr(ent, "tag", "") or ""
        if tag not in ("NO", "ObjTerm.Name"):
            continue
        cf = getattr(ent, "custom_fields", None) or {}
        x = cf.get("x")
        y = cf.get("y")
        if x is None or y is None:
            continue
        tid = (getattr(ent, "text", "") or "").strip()
        if not tid or ":" not in tid:
            continue
        terminals.append((float(x), float(y), tid, tag))

    if not cabinet_records or not terminals:
        return []

    return assign_terminals_to_cabinets(cabinet_records, terminals)


# ---------------------------------------------------------------------------
# (V6.6 helpers _ws_in_cabinet / _terminal_in_cab / _cabinet_for_terminal
# have been replaced by CabinetGridIndex for O(1) lookup, Q3 2025.)
# ---------------------------------------------------------------------------
