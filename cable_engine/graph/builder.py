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

    # ------------------------------------------------------------------
    # V6.7 Geometry path tracing — finds terminals by following 90° wire
    # paths from the WS position along the U-top, ray-casting for
    # terminal circles and ignoring cabinet-boundary crossings.
    # ------------------------------------------------------------------
    @staticmethod
    def _nearest_tag_near(
        tx: float, ty: float,
        no_tags_by_y: dict[int, list[tuple[float, float, str, str]]],
        side: str, ws_x: float,
        y_range: int = 30,
        x_tol: float = 50.0,
    ) -> Optional[tuple[float, float, str, str]]:
        """Find the nearest terminal text tag near position (tx, ty).

        Searches ±y_range keys (±15 y-units with default 2x key)* and
        returns the closest NO tag within x_tol of tx. Only returns tags
        on the correct side of wx.
        """
        best: Optional[tuple[float, float, str, str]] = None
        best_dist = float('inf')
        key = round(ty * 2)
        for dk in range(-y_range, y_range + 1):
            for tag in no_tags_by_y.get(key + dk, []):
                tag_x, tag_y, tag_val, tag_type = tag
                if ':' not in tag_val:
                    continue
                if side == 'left' and tag_x >= ws_x:
                    continue
                if side == 'right' and tag_x <= ws_x:
                    continue
                dx = abs(tag_x - tx)
                if dx >= x_tol:
                    continue
                dist = dx * dx + (tag_y - ty) * (tag_y - ty)
                if dist < best_dist:
                    best_dist = dist
                    best = tag
        return best

    @staticmethod
    def _find_terminal_icon(
        x: float, y: float,
        circles_by_y: dict[int, list[tuple[float, float, float]]],
        terminal_icons_by_y: dict[int, list[tuple[float, float, str]]],
        max_dist: float = 2.0,
    ) -> tuple[float, float, bool]:
        """Check for a terminal icon (CircleGeometry or TERNO/BL/BR) near (x,y).

        Returns (icon_x, icon_y, found).
        """
        best_dist = max_dist
        best_x, best_y = x, y
        found = False
        key = round(y * 2)
        for dk in range(-4, 5):
            for cx, cy, cr in circles_by_y.get(key + dk, []):
                d = abs(cx - x) + abs(cy - y)
                if d < best_dist:
                    best_dist = d
                    best_x, best_y = cx, cy
                    found = True
        for dk in range(-4, 5):
            for ix, iy, itag in terminal_icons_by_y.get(key + dk, []):
                d = abs(ix - x) + abs(iy - y)
                if d < best_dist:
                    best_dist = d
                    best_x, best_y = ix, iy
                    found = True
        return best_x, best_y, found

    @staticmethod
    def _cabinet_path_trace(
        wx: float, wy: float,
        cabinets: list[dict],
        core_lines: list[dict],
        core_ys: list[float],
        circles_by_y: dict[int, list[tuple[float, float, float]]],
        terminal_icons_by_y: dict[int, list[tuple[float, float, str]]],
        no_tags_by_y: dict[int, list[tuple[float, float, str, str]]],
        verticals: list[dict],
        side: str,
    ) -> Optional[tuple[float, float, str, str]]:
        """Cabinet-based path tracing to find a terminal for the given side.

        Algorithm:
        1. Find horizontal wires near wy that cross a cabinet vertical edge
        2. Trace the wire endpoint (side opposite the cabinet) to find
           a terminal icon (CircleGeometry or TERNO/BL/BR ATTRIB)
        3. If no icon at the direct endpoint, follow 90° turns (vertical
           segment → horizontal segment) looking for the terminal icon
        4. Find the nearest NO/ObjTerm.Name tag near the terminal icon
        5. Return (x, y, text, tag_type) or None

        Returns None when no terminal is found (caller records icon-only
        or empty).
        """
        import bisect as _bisect

        _Y_TOL = 30.0
        _CROSS_TOL = 2.0
        _ENDPOINT_TOL = 30.0  # max x-distance from WS to wire endpoint

        lo = _bisect.bisect_left(core_ys, wy - _Y_TOL)
        hi = _bisect.bisect_right(core_ys, wy + _Y_TOL)
        near_lines = core_lines[lo:hi]
        if not near_lines:
            return None

        # Collect cabinet vertical edges at this y-level
        crossing_edges: list[float] = []
        for c in cabinets:
            b = c['bbox']
            if not (b.y - 1 <= wy <= b.y + b.h + 1):
                continue
            if side == 'left' and b.x < wx:
                crossing_edges.append(b.x + b.w)
            elif side == 'right' and b.x + b.w > wx:
                crossing_edges.append(b.x)

        # Candidate selection — four-tier priority:
        #   1. Spans wx AND crosses a cabinet edge
        #   2. Endpoint near wx AND crosses a cabinet edge
        #   3. Spans wx (no cabinet constraint)
        #   4. Endpoint near wx (no cabinet constraint)
        def _crosses_cabinet(cl: dict) -> bool:
            return any(
                cl['x_min'] - _CROSS_TOL <= ex <= cl['x_max'] + _CROSS_TOL
                for ex in crossing_edges
            )

        best: Optional[dict] = None
        best_dy = float('inf')
        for cl in near_lines:
            spans = cl['x_min'] <= wx <= cl['x_max']
            near_end = (
                abs(cl['x_min'] - wx) <= _ENDPOINT_TOL
                or abs(cl['x_max'] - wx) <= _ENDPOINT_TOL
            )
            if not spans and not near_end:
                continue
            crosses = _crosses_cabinet(cl)
            # Priority encoding: (w_cabinet, w_span, dy)
            # Higher priority = more likely to serve as the correct wire.
            score = (1 if crosses else 0, 1 if spans else 0, -abs(cl['y'] - wy))
            if best is None:
                best = cl
                best_dy = abs(cl['y'] - wy)
                best_score = score
            elif score > best_score:
                best = cl
                best_dy = abs(cl['y'] - wy)
                best_score = score

        if best is None:
            return None

        # Wire endpoint on the specified side
        ep_x = best['x_min'] if side == 'left' else best['x_max']
        ep_y = best['y']

        # Find terminal icon at or near the endpoint
        icon_x, icon_y, icon_found = CircuitLoopAnalyzer._find_terminal_icon(
            ep_x, ep_y, circles_by_y, terminal_icons_by_y,
        )

        if not icon_found:
            # Follow 90° turn: find a vertical segment at the endpoint
            vert: Optional[dict] = None
            for v in verticals:
                if abs(v['x'] - ep_x) > 0.5:
                    continue
                if not (v['y1'] - 0.5 <= ep_y <= v['y2'] + 0.5):
                    continue
                at_endpoint = (
                    abs(ep_y - v['y1']) <= 0.5 or abs(ep_y - v['y2']) <= 0.5
                )
                if not at_endpoint:
                    continue
                vert = v
                break

            if vert is not None:
                # Follow vertical to its other end
                other_y = (
                    vert['y1'] if abs(vert['y2'] - ep_y) <= 0.5
                    else vert['y2']
                )
                icon_x, icon_y, icon_found = CircuitLoopAnalyzer._find_terminal_icon(
                    vert['x'], other_y, circles_by_y, terminal_icons_by_y,
                )

                if not icon_found:
                    # Check for a horizontal segment at the vertical's other end
                    hlo = _bisect.bisect_left(core_ys, other_y - 0.5)
                    hhi = _bisect.bisect_right(core_ys, other_y + 0.5)
                    for cl in core_lines[hlo:hhi]:
                        if abs(cl['y'] - other_y) > 0.5:
                            continue
                        if not (cl['x_min'] <= vert['x'] <= cl['x_max']):
                            continue
                        h_ep = cl['x_min'] if side == 'left' else cl['x_max']
                        icon_x, icon_y, icon_found = (
                            CircuitLoopAnalyzer._find_terminal_icon(
                                h_ep, other_y, circles_by_y,
                                terminal_icons_by_y,
                            )
                        )
                        break

        if icon_found:
            tag = CircuitLoopAnalyzer._nearest_tag_near(
                icon_x, icon_y, no_tags_by_y, side, wx,
            )
            if tag is not None:
                return tag
            return (icon_x, icon_y, '', 'ICON_ONLY')

        tag = CircuitLoopAnalyzer._nearest_tag_near(
            ep_x, ep_y, no_tags_by_y, side, wx,
        )
        if tag is not None:
            return tag
        return None

    def analyze(self, doc: Document) -> list[dict]:
        attribs: list[dict] = []
        for e in doc.entities:
            if not isinstance(e, (AttributeEntity, TextEntity)):
                continue
            # Skip low-confidence entities (block-local text from
            # Phase 2b expansion — coordinates are not in model space).
            if e.confidence < 0.5:
                continue
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
                    if core not in cable_cores[cid]:
                        cable_cores[cid][core] = {
                            'x': a['x'],
                            'y': a['y'],
                        }

        # Collect cabinet boundary handles + bbox edges so we can
        # filter them out of core_lines. Cabinet boundary edges at
        # the WS y-level act as fake U-tops — the path tracer follows
        # them (no terminals) and returns None for both sides.
        _cab_handles: set[str] = set()
        _cab_y_edges: list[tuple[float, float, float]] = []
        for _ce in doc.entities:
            if not isinstance(_ce, _CabReg_pre):
                continue
            if _ce.bbox is None:
                continue
            if _ce.boundary_handle:
                _cab_handles.add(_ce.boundary_handle)
            _cab_y_edges.append(
                (_ce.bbox.y, _ce.bbox.x, _ce.bbox.x + _ce.bbox.w)
            )
            _cab_y_edges.append(
                (_ce.bbox.y + _ce.bbox.h, _ce.bbox.x,
                 _ce.bbox.x + _ce.bbox.w)
            )

        # Pre-scan: collect horizontal LINE/LWPOLYLINE for core-line detection.
        # V6.7: pre-sort by y so each per-core search is O(log L + window)
        # instead of O(L).
        import bisect as _bisect
        core_lines: list[dict] = []
        for e in doc.entities:
            if not isinstance(e, LineGeometry):
                continue
            if e.handle in _cab_handles:
                continue  # skip cabinet boundary LWPOLYLINE
            pts = list(e.points or [])
            if len(pts) < 2:
                continue
            ys = [p.y for p in pts]
            xs = [p.x for p in pts]
            if max(ys) - min(ys) > 3:
                continue
            dx = max(xs) - min(xs)
            dy = max(ys) - min(ys)
            if dx > 2 and dy > 2:
                continue  # skip short diagonal annotations
            # Skip horizontal lines that match a cabinet bbox top or
            # bottom edge in both y and x-span. Catches cases where
            # the cabinet boundary is drawn as 4 separate LINE entities
            # (each edge a 2-point LINE, not a single LWPOLYLINE).
            _cl_y = ys[0]
            _cl_xmin = min(xs)
            _cl_xmax = max(xs)
            _is_cab_edge = False
            for _ey, _ex_min, _ex_max in _cab_y_edges:
                if abs(_cl_y - _ey) > 0.5:
                    continue
                if abs(_cl_xmin - _ex_min) > 2:
                    continue
                if abs(_cl_xmax - _ex_max) > 2:
                    continue
                _is_cab_edge = True
                break
            if _is_cab_edge:
                continue
            core_lines.append({
                'y': _cl_y,
                'x_min': _cl_xmin,
                'x_max': _cl_xmax,
            })
        core_lines.sort(key=lambda cl: cl['y'])
        _core_ys = [cl['y'] for cl in core_lines]

        # V6.7: Pre-compute circles_by_y and verticals for geometry path tracing.
        circles_by_y: dict[int, list[tuple[float, float, float]]] = {}
        verticals: list[dict] = []  # {x, y1, y2} sorted by x
        for e in doc.entities:
            if isinstance(e, CircleGeometry):
                c = e.center
                if c is not None:
                    key = round(c.y * 2)
                    circles_by_y.setdefault(key, []).append(
                        (c.x, c.y, e.radius or 1.0)
                    )
            elif isinstance(e, LineGeometry):
                pts = list(e.points or [])
                if len(pts) < 2:
                    continue
                xs = [p.x for p in pts]
                ys = [p.y for p in pts]
                dx = max(xs) - min(xs)
                dy = max(ys) - min(ys)
                if dx < 0.5 and dy > 5:  # near-vertical, substantial span
                    verticals.append({
                        'x': (xs[0] + xs[-1]) / 2,
                        'y1': min(ys),
                        'y2': max(ys),
                    })
        verticals.sort(key=lambda v: v['x'])

        # Group NO tags and cable-level ATTRIB by y position.
        no_tags_by_y: dict[int, list[tuple[float, float, str, str]]] = {}
        cable_attribs: dict[str, list[dict]] = {}
        # Terminal ICON positions (TERNO/BL/BR with empty text) grouped
        # by y-key. These mark the physical icon location.
        terminal_icons_by_y: dict[int, list[tuple[float, float, str]]] = {}
        _TERMINAL_LABEL_RE = re.compile(r'^[A-Za-z0-9]+:[A-Za-z0-9]+$')
        for a in attribs:
            if a['tag'] in ('NO', 'ObjTerm.Name') and ':' in a['val']:
                key = round(a['y'] * 2)
                no_tags_by_y.setdefault(key, []).append((a['x'], a['y'], a['val'], a['tag']))
            elif not a['tag'] and _TERMINAL_LABEL_RE.match(a['val']):
                key = round(a['y'] * 2)
                no_tags_by_y.setdefault(key, []).append((a['x'], a['y'], a['val'], 'NO'))
            elif a['tag'] in ('TERNO', 'BL', 'BR') and not a['val']:
                key = round(a['y'] * 2)
                terminal_icons_by_y.setdefault(key, []).append((a['x'], a['y'], a['tag']))
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
                # (x_min >= 200 and span >= 50mm).
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
                _ = core_y  # keep reference

                left_candidate: Optional[tuple[float, float, str, str]] = None
                right_candidate: Optional[tuple[float, float, str, str]] = None

                # Cabinet-based path tracing (V7.0).
                # Replaces all V6.5–V6.12 fallback methods with a single
                # algorithm: find horizontal wires that cross cabinet
                # vertical edges, trace endpoints to terminal icons,
                # follow 90° turns, and find NO/ObjTerm.Name tags.
                if left_candidate is None:
                    left_candidate = CircuitLoopAnalyzer._cabinet_path_trace(
                        wx, wy, v66_cabinets, core_lines, _core_ys,
                        circles_by_y, terminal_icons_by_y, no_tags_by_y,
                        verticals, 'left',
                    )
                if right_candidate is None:
                    right_candidate = CircuitLoopAnalyzer._cabinet_path_trace(
                        wx, wy, v66_cabinets, core_lines, _core_ys,
                        circles_by_y, terminal_icons_by_y, no_tags_by_y,
                        verticals, 'right',
                    )

                left_terminal = left_candidate[2] if left_candidate else None
                left_terminal_x = left_candidate[0] if left_candidate else None
                left_terminal_y = left_candidate[1] if left_candidate else None

                right_terminal = right_candidate[2] if right_candidate else None
                right_terminal_x = right_candidate[0] if right_candidate else None
                right_terminal_y = right_candidate[1] if right_candidate else None

                # Step 2a: Cabinet detection — once per cable on the
                # first core that has a valid terminal. Uses spatial
                # cabinet lookup (terminal position inside a detected
                # dashed-rectangle cabinet bbox → use its display name);
                # falls back to text-search when no cabinet covers the
                # terminal.
                cab_lookup_x = left_terminal_x
                cab_lookup_y = left_terminal_y
                if cab_lookup_y is not None and cabinet_local is None:
                    cabinet_local = cabinet_grid.lookup_name(
                        cab_lookup_x, cab_lookup_y,
                    ) or self._find_cabinet(
                        cab_lookup_x, cab_lookup_y, attribs,
                    )
                right_cabinet_x = right_terminal_x
                right_cabinet_y = right_terminal_y
                if right_cabinet_y is not None and cabinet_remote is None:
                    cabinet_remote = cabinet_grid.lookup_name(
                        right_cabinet_x, right_cabinet_y,
                    ) or self._find_cabinet(
                        right_cabinet_x, right_cabinet_y, attribs,
                    )

                # Step 3: circuit_desc/loop_id — find the nearest
                # WireDescription and LoopCode ATTRIB to the cable's
                # y range (scanning within 80mm y of wy).
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
        # Build set of terminal IDs actually used by cables, so we can
        # filter out floating NO labels (e.g. TA3:N in 25G/24G protection
        # cabinets) that are spatially inside a cabinet bbox but don't
        # belong to any cable.
        _used_terminal_ids: set[str] = set()
        for rec in records:
            strip = (rec.get('strip_name') or '')
            tn = (rec.get('terminal_no') or '')
            tnr = (rec.get('terminal_no_remote') or '')
            if strip and tn:
                _used_terminal_ids.add(f"{strip}:{tn}")
            if tnr and tnr != 'None' and ':' in tnr:
                # Remote terminal ID is already in combined format (e.g. "IV:31",
                # "22ID:1"). Only add if it has a colon to avoid matching random
                # bare numbers.
                _used_terminal_ids.add(tnr)
        _ct_rows: list[tuple] = [
            (cab_id, doc.content_hash, tid, kind, x, y)
            for cab_id, tid, kind, x, y in cabinet_terminal_rows_pre
            if kind not in ("NO", "ObjTerm.Name")
               or tid in _used_terminal_ids
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
        if tag not in ("NO", "ObjTerm.Name", "TERNO", "BL", "BR"):
            continue
        cf = getattr(ent, "custom_fields", None) or {}
        x = cf.get("x")
        y = cf.get("y")
        if x is None or y is None:
            continue
        tid = (getattr(ent, "text", "") or "").strip()
        if tag in ("TERNO", "BL", "BR"):
            # Icon-only terminal: use a unique position-based ID
            # even without text, so the cabinet containment check
            # includes all terminal icons regardless of label status.
            tid = tid or f'ICON_{tag}@{x:.0f}_{y:.0f}'
        if not tid or (tag in ("NO", "ObjTerm.Name") and ":" not in tid):
            continue
        terminals.append((float(x), float(y), tid, tag))

    if not cabinet_records or not terminals:
        return []

    return assign_terminals_to_cabinets(cabinet_records, terminals)


# ---------------------------------------------------------------------------
# (V6.6 helpers _ws_in_cabinet / _terminal_in_cab / _cabinet_for_terminal
# have been replaced by CabinetGridIndex for O(1) lookup, Q3 2025.)
# ---------------------------------------------------------------------------
