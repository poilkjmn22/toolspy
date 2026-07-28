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

from ..core.asset import SpatialContainer
from ..ir import (
    ArcGeometry, AttributeEntity, BlockRef, CircleGeometry, Document,
    DocumentType, GeometryEntity, LineGeometry, TextEntity,
)
from ..ir.entities import BBox, Point
from ..classifier import BusinessType
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
        # V8: Build GeometryGraph + ElectricalQuery
        from ..electrical import (
            GeometryBuilder, GeometryGraph, WireBuilder, CabinetBuilder,
            ElectricalQuery,
        )
        geo_graph: GeometryGraph = GeometryBuilder().build(doc)
        WireBuilder(geo_graph).run()
        CabinetBuilder(geo_graph).run()
        query = ElectricalQuery(geo_graph)

        attribs: list[dict] = []
        for e in doc.entities:
            if not isinstance(e, (AttributeEntity, TextEntity)):
                continue
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

        # Group cable-level ATTRIB by tag for circuit_desc/loop_id matching
        cable_attribs: dict[str, list[dict]] = {}
        for a in attribs:
            if a['tag'] in ('WireDescription', 'LoopCode', 'WIRECODE', 'WIRETYPE'):
                cable_attribs.setdefault(a['tag'], []).append(a)

        # Pre-compute cable_type per cable: find WIRETYPE nearest to each
        # cable's WIRECODE attribute.
        cable_wire_type: dict[str, Optional[str]] = {}
        for wa in cable_attribs.get('WIRECODE', []):
            wc_x, wc_y = wa['x'], wa['y']
            best_d = 100.0
            best = None
            for wta in cable_attribs.get('WIRETYPE', []):
                d = abs(wta['x'] - wc_x) + abs(wta['y'] - wc_y)
                if d < best_d:
                    best_d = d
                    best = wta['val']
            cable_wire_type[wa['val']] = best

        records: list[dict] = []
        for cid in sorted(cable_cores.keys()):
            cores = cable_cores[cid]
            core_order = sorted(cores.items(), key=lambda kv: -kv[1]['y'])

            wire_type = cable_wire_type.get(cid)
            cabinet_local: Optional[str] = None
            cabinet_remote: Optional[str] = None

            for i, (core, info) in enumerate(core_order):
                wx = info['x']
                wy = info['y']

                left_result = query.find_terminal(wx, wy, 'left', cable_id=cid)
                right_result = query.find_terminal(wx, wy, 'right', cable_id=cid)

                left_terminal = left_result.number if left_result else None
                left_terminal_x = left_result.x if left_result else None
                left_terminal_y = left_result.y if left_result else None

                right_terminal = right_result.number if right_result else None
                right_terminal_x = right_result.x if right_result else None
                right_terminal_y = right_result.y if right_result else None

                # Deduplicate: if left and right resolve to the same terminal
                # (short wire stub, same cabinet), clear the remote
                if (left_terminal and right_terminal
                        and left_terminal == right_terminal
                        and left_result and right_result
                        and left_result.cabinet == right_result.cabinet):
                    right_terminal = None
                    right_terminal_x = None
                    right_terminal_y = None

                # Cabinet detection — once per cable on first valid terminal
                if left_terminal_x is not None and cabinet_local is None:
                    cabinet_local = (
                        left_result.cabinet
                        or self._find_cabinet(
                            left_terminal_x, left_terminal_y, attribs,
                        )
                    )
                if right_terminal_x is not None and cabinet_remote is None:
                    cabinet_remote = (
                        right_result.cabinet
                        or self._find_cabinet(
                            right_terminal_x, right_terminal_y, attribs,
                        )
                    )

                # circuit_desc/loop_id — find the nearest
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
                    'wire_type': wire_type,
                })
        return records


# ===================================================================
# CableScheduleAnalyzer (电缆清册 / 接线表) — V6.7 enhanced
# ===================================================================
class CableScheduleAnalyzer:
    """Analyzer for cable schedules (电缆清册 / 接线表 / 电缆联系图).

    Delegates table parsing to :class:`table.parsers.schedule.ScheduleParser`,
    keeping cable-ID fallback for drawings that don't have a structured table.
    """

    def analyze(self, doc: Document) -> list[dict]:
        from ..layout.table.parsers.schedule import (
            extract_cable_ids_fallback,
            parse_schedule_table,
        )

        result = parse_schedule_table(doc)
        if result is not None:
            return result

        texts = [
            (e.text or '').strip()
            for e in doc.entities
            if isinstance(e, (TextEntity, AttributeEntity)) and (e.text or '').strip()
        ]
        return extract_cable_ids_fallback(
            [(0.0, 0.0, t) for t in texts]
        )


# ===================================================================
# TopologyStage (replaces GraphBuilderStage)
# ===================================================================
# V6.5: dispatch by BusinessType. Each type maps to a single Analyzer
# (or None for types we haven't built yet — those count as "unmatched").
_ANALYZERS_BY_TYPE: dict[BusinessType, Any] = {
    BusinessType.CIRCUIT_LOOP: CircuitLoopAnalyzer,
    BusinessType.TERMINAL_STRIP: TerminalStripAnalyzer,
    BusinessType.CABLE_SCHEDULE: CableScheduleAnalyzer,
    # PROTECTION_DIAGRAM / PANEL_LAYOUT / PANEL_POSITION /
    # MONITORING_SYSTEM / UNKNOWN don't have analyzers yet.
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

    def __init__(self, store) -> None:
        self._store = store

    def run(self, ctx: Context) -> Context:
        doc = ctx.document
        if doc is None:
            ctx.error_msg = 'no document to build topology from'
            return ctx

        classification = ctx.classification
        if classification is None:
            ctx.error_msg = 'classification not set — run ClassificationStage first'
            return ctx
        doc_type = classification.primary.value
        ctx.document_type = doc_type

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

        # V8: persist cable_type info (one row per cable)
        _seen_cable: set[str] = set()
        _cable_info_rows: list[tuple] = []
        for rec in records:
            wt = rec.get('wire_type')
            if wt and rec['cable_id'] not in _seen_cable:
                _seen_cable.add(rec['cable_id'])
                _cable_info_rows.append((
                    rec['cable_id'], doc.content_hash, wt,
                ))
        if _cable_info_rows:
            self._store.bulk_upsert_cable_info(_cable_info_rows)

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
