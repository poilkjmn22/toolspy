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
        if ox < terminal_x:
            d = terminal_x - ox
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

    def _analyze_one_cable(self, cable_id, horizontals, verticals, texts):
        if not horizontals:
            return []
        h = horizontals[0]
        hpts = list(h.points or [])
        h_y = hpts[0].y if hpts else 0.0
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
            circuit_desc, terminal_no, loop_id, unknown_busi = _classify_column_text(column_texts)
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
                'terminal_no_right': None,
                'cabinet_name': self._cabinet_name,
                'circuit_desc': circuit_desc,
                'loop_id': loop_id,
                'unknown_busi': unknown_busi,
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

    def analyze(self, doc: Document) -> list[dict]:
        # Collect all ATTRIB entities with position (x, y)
        attribs: list[dict] = []
        for e in doc.entities:
            if isinstance(e, AttributeEntity):
                cf = getattr(e, 'custom_fields', None) or {}
                x = cf.get('x')
                y = cf.get('y')
                if x is None or y is None:
                    continue
                attribs.append({
                    'tag': e.tag or '',
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
                        'row_y': a['y'],
                    }

        # For each core, find OTHER ATTRIB entities with same cable
        # by matching y within CORE_TOLERANCE
        _CORE_TOLERANCE = 2.0  # mm — max y diff between texts in same core row
        for cid, cores in cable_cores.items():
            for core, info in cores.items():
                cy = info['y']
                for a in attribs:
                    if a['tag'] == 'WireSerial':
                        continue
                    if abs(a['y'] - cy) > _CORE_TOLERANCE:
                        continue
                    tag = a['tag']
                    val = a['val']
                    if tag == 'WireDescription' and not info.get('circuit_desc'):
                        info['circuit_desc'] = val
                    elif tag == 'LoopCode' and not info.get('loop_id'):
                        info['loop_id'] = val
                    elif tag == 'NO':
                        if a['x'] < 100 and not info.get('left_terminal'):
                            info['left_terminal'] = val
                        elif a['x'] >= 100 and not info.get('right_terminal'):
                            info['right_terminal'] = val
                    elif tag == 'InOut' and not info.get('direction'):
                        info['direction'] = val
                    elif tag == 'ToOtherEqu' and not info.get('equipment'):
                        info['equipment'] = val

        # Build records
        records: list[dict] = []
        for cid, cores in sorted(cable_cores.items()):
            for core in sorted(cores.keys()):
                info = cores[core]
                # Circuit_desc from WireDescription
                circuit_desc = info.get('circuit_desc')
                loop_id = info.get('loop_id')
                # Extract strip_name:terminal_no from left terminal (closest to cable)
                strip_name: Optional[str] = None
                terminal_no: Optional[int] = None
                lt = info.get('left_terminal', '')
                if ':' in lt:
                    parts = lt.split(':', 1)
                    strip_name = parts[0].strip()
                    try:
                        terminal_no = int(parts[1].strip())
                    except ValueError:
                        pass

                # Right terminal (full ID string, e.g. "9D:1")
                terminal_no_right = info.get('right_terminal')

                records.append({
                    'cable_id': cid,
                    'conductor_no': core,
                    'strip_name': strip_name,
                    'terminal_no': terminal_no,
                    'terminal_no_right': terminal_no_right,
                    'cabinet_name': None,
                    'circuit_desc': circuit_desc,
                    'loop_id': loop_id,
                    'unknown_busi': None,
                    'source_type': 'circuit_loop',
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

        self._store.delete_topology_for_document(doc.content_hash)

        if doc_type == 'terminal_strip':
            analyzer = TerminalStripAnalyzer()
            records = analyzer.analyze(doc)
        elif doc_type == 'circuit_loop':
            analyzer = CircuitLoopAnalyzer()
            records = analyzer.analyze(doc)
        else:
            records = []

        for rec in records:
            self._store.upsert_cable_topology(
                cable_id=rec['cable_id'],
                conductor_no=rec['conductor_no'],
                strip_name=rec['strip_name'],
                terminal_no=rec['terminal_no'],
                terminal_no_right=rec.get('terminal_no_right'),
                cabinet_name=rec.get('cabinet_name'),
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

        ctx.result = {'cable_topology_count': len(records)}
        return ctx


__all__ = ['TopologyStage']
