"""cable_engine.graph.cabinet — V6.6 Cabinet Region analyzer.

A "cabinet" in the source drawing is a dashed rectangle (typically
with linetype ACAD_ISO10W100 / HIDDEN / DASHED) that visually encloses
a group of terminals belonging to the same physical control box. The
name of the cabinet is the text label printed above the rectangle
(e.g. "11003.ZXW") often paired with a longer descriptive label below
it (e.g. "3 号主变 110kV 电压互感器端子箱").

This module:

  1. CabinetBoundary        — dataclass: an enclosed region (bbox +
                              corner points + boundary handle).
  2. CabinetRecord          — dataclass: boundary + matched name +
                              location + display name.
  3. CabinetRegionAnalyzer  — single-pass analyzer that walks the
                              Document's LineGeometry + TextEntity
                              entities, identifies dashed rectangles,
                              and pairs each rectangle with the
                              nearest cabinet-name text above it.

Linetype detection:
  The DWG Loader (V6.6) writes `custom_fields['ltype']` for every
  LineGeometry whose entity carries an explicit non-ByLayer linetype.
  We treat a closed polyline as a "dashed rectangle" when its points
  trace a near-axis-aligned rectangle AND its ltype is one of the
  known dashed/hidden families (ACAD_ISO10W100, HIDDEN, DASHED, ...).
  Pure Continuous lines never qualify.

Output ordering:
  Boundaries are returned sorted left-to-right then bottom-to-top so
  the resulting `cabinet_id`s are stable across rescans (helpful for
  the viewer's "柜体" tab + incremental re-scans).

Nest handling (V6.6):
  We currently return every detected dashed rectangle. Cabinets with
  a fully-nested bbox form a containment hierarchy; consumers can
  detect nesting post-hoc via bbox inclusion if they care.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..core.asset import SpatialContainer
from ..ir import Document, LineGeometry, TextEntity
from ..ir.entities import BBox, Point


# ---------------------------------------------------------------------------
# Linetype whitelist — Cabinet-boundary detector only fires on these
# ---------------------------------------------------------------------------
# In V6.6 we focus on the ltype names that design drawings in shengli
# actually use for cabinet-boundary boxes. Adding more is a one-line
# change once we see a new one in the field.
_DASHED_LTYPES: frozenset[str] = frozenset({
    # Core patterns — seen in practice on shengli DWGs
    'ACAD_ISO10W100',    # ISO long-dash-dot
    'HIDDEN',            # Standard "hidden line" dashed
    'DASHED',            # Generic dash
    'DASH',              # Alias
    'DOTTED',            # Dot pattern (sometimes used)
    'DASHDOT',           # Dash + dot
    # Scaled variants — common in Chinese CAD
    'DASHED2',           # 0.5× DASHED
    'HIDDEN2',           # 0.5× HIDDEN
    'DASHEDX2',          # 2× DASHED
    'HIDDENX2',          # 2× HIDDEN
    'DOT2',              # 0.5× DOTTED
    'DASHDOT2',          # 0.5× DASHDOT
    'DOT',               # DOT alias
    # ISO pattern family — all ISO 128 linetypes
    'ACAD_ISO02W100',    # ISO dot
    'ACAD_ISO03W100',    # ISO dash-dot
    'ACAD_ISO04W100',    # ISO long-dash
    'ACAD_ISO05W100',    # ISO long-dash-dot
    'ACAD_ISO06W100',    # ISO long-dash-double-dot
    'ACAD_ISO07W100',    # ISO double-dash-dot
    'ACAD_ISO08W100',    # ISO double-dash-double-dot
    'ACAD_ISO09W100',    # ISO dash-triple-dot
    # User-defined variants used in some project DWGs
    'BATTING',
    'BATTING2',
})


def is_dashed_ltype(ltype: str) -> bool:
    """True when `ltype` is a known dashed/hidden linetype name."""
    return bool(ltype) and ltype.upper() in {n.upper() for n in _DASHED_LTYPES}


# ---------------------------------------------------------------------------
# Cabinet detection result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CabinetBoundary:
    """A closed rectangle traced by a dashed polyline (or by 4 separate
    dashed LINE segments that meet at four corners).

    `points` is the input polyline's ordered vertices. `bbox` is the
    axis-aligned bounding box. `handle` is the DWG handle of the
    originating polyline (empty for multi-segment edges).
    """
    id: str
    document_hash: str
    bbox: BBox
    points: list[Point] = field(default_factory=list)
    layer: str = ''
    handle: str = ''
    ltype: str = ''
    closed: bool = True

    @property
    def width(self) -> float:
        return self.bbox.w

    @property
    def height(self) -> float:
        return self.bbox.h

    @property
    def area(self) -> float:
        return self.bbox.w * self.bbox.h

    def contains(self, x: float, y: float, pad: float = 0.0) -> bool:
        """True when (x, y) lies inside the bbox (optionally padded
        by `pad` to forgive tiny coordinate rounding differences)."""
        return (
            self.bbox.x - pad <= x <= self.bbox.x + self.bbox.w + pad
            and self.bbox.y - pad <= y <= self.bbox.y + self.bbox.h + pad
        )


@dataclass
class CabinetRecord:
    """One cabinet-boundary region with its matched textual identity.

    `name` is the short cabinet code (e.g. "ZXW" or "11003.ZXW").
    `location` is the prefix string to its left (e.g. "11003").
    `display_name` is the user-facing compound string with the
    optional longer descriptive label appended.

    `text_label` is the descriptive label found BELOW the boundary
    rectangle (e.g. "3号主变110kV电压互感器端子箱"), if any.
    """
    boundary: CabinetBoundary
    document_hash: str
    name: str = ''
    location: str = ''
    display_name: str = ''
    text_label: str = ''
    name_text_id: str = ''
    location_text_id: str = ''
    label_text_id: str = ''

    @property
    def id(self) -> str:
        return self.boundary.id

    @property
    def bbox(self) -> BBox:
        return self.boundary.bbox

    @property
    def container(self) -> SpatialContainer:
        """Unified spatial identity for cross-world queries."""
        return SpatialContainer(
            id=self.id,
            document_hash=self.document_hash,
            bbox=self.bbox,
            name=self.display_name or self.name,
            source='dwg_dashed_rect',
            layer=self.boundary.layer,
        )


# ---------------------------------------------------------------------------
# CabinetRegionAnalyzer
# ---------------------------------------------------------------------------
# Cabinet names look like:
#   - "11003.ZXW"  (location.name)
#   - "3号主变110kV电压互感器端子箱"  (descriptive label, contains 屏/柜/箱)
#   - "端子排图" (also acceptable, per V6.5 _find_cabinet)
_NAME_KEYWORDS: tuple[str, ...] = ('屏', '柜', '箱', '端子排图')

# Cabinet name regex. The short code is typically 2-6 letters/digits
# ending in a 1-6 alphanumeric tail (e.g. "ZXW", "G1Q", "A5-B3").
_SHORT_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._\-]{0,12}$')


@dataclass
class _TextRef:
    """Lightweight reference to a TextEntity for spatial matching."""
    text: str
    x: float
    y: float
    text_id: str


class CabinetRegionAnalyzer:
    """Single-pass analyzer producing CabinetRecord list per document.

    The workflow:
      1. Filter the document's LineGeometry entities to those whose
         ltype is in the dashed set AND whose points form a closed
         axis-aligned rectangle.
      2. Build a sorted CabinetBoundary list.
      3. For each boundary, search nearby TextEntity labels (above
         and below) and pick:
           - `name`        — the closest TextEntity whose text either
                              contains one of 屏/柜/箱 OR looks like a
                              short cabinet code, located above the
                              boundary.
           - `location`     — the closest TextEntity positioned to the
                              LEFT of the name on the same y row.
           - `text_label`   — the closest descriptive TextEntity
                              BELOW the boundary (typically 3-15 chars
                              and containing 屏/柜/箱).

    Output is the CabinetRecord list. The TopologyStage persists
    these rows into `cabinets` / `cabinet_terminals` tables.
    """

    # Tuning knobs — small enough that callers can override per-doc.
    NAME_ABOVE_DY: float = 60.0      # max vertical gap boundary→name
    LABEL_BELOW_DY: float = 40.0     # max vertical gap boundary→label
    LOCATION_NAME_DY: float = 3.0    # |y_diff| for "same line" pairing
    LOCATION_NAME_DX: float = 250.0  # max horizontal gap name↔location
    RECT_RTOL: float = 0.06          # 6% relative tolerance for axis alignment
    RECT_ABS_TOL: float = 3.0        # absolute tolerance for axis alignment
    MIN_RECT_SIDE: float = 8.0       # reject tiny shapes (< 8 units side)
    MAX_PTS_FOR_RECT: int = 8        # typical is 4 or 5 (close back)

    def analyze(self, doc: Document) -> list[CabinetRecord]:
        boundaries = self._find_dashed_rectangles(doc)
        # Stable id order: by (x_min, y_min) ascending.
        boundaries.sort(key=lambda b: (round(b.bbox.x, 2), round(b.bbox.y, 2)))
        # Re-assign ids in the sorted order so callers see a stable
        # list. V6.6: prefix with the document's content_hash so the
        # ids are globally unique across the database — the per-row
        # `cabinets.id PRIMARY KEY` lookup must not collide between
        # documents (cab_001 in DWG A is a different cabinet than
        # cab_001 in DWG B). The 12-char hash prefix leaves room for
        # the existing cab_NNN naming that user-facing tools rely on.
        doc_prefix = (doc.content_hash or '')[:12] or 'doc'
        for i, b in enumerate(boundaries):
            object.__setattr__(b, 'id', f'cab_{doc_prefix}_{i + 1:03d}')

        # Build text refs ONCE for the matcher pass.
        texts = self._collect_texts(doc)
        if not texts:
            return [CabinetRecord(boundary=b, document_hash=doc.content_hash) for b in boundaries]
        by_y: dict[int, list[_TextRef]] = {}
        for t in texts:
            by_y.setdefault(round(t.y * 2), []).append(t)

        records: list[CabinetRecord] = []
        for b in boundaries:
            rec = self._match_boundary_text(b, doc.content_hash, texts, by_y)
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # 1. Dashed-rectangle detection
    # ------------------------------------------------------------------
    def _find_dashed_rectangles(self, doc: Document) -> list[CabinetBoundary]:
        out: list[CabinetBoundary] = []
        for ent in doc.entities:
            if not isinstance(ent, LineGeometry):
                continue
            cf = getattr(ent, 'custom_fields', None) or {}
            ltype = cf.get('ltype', '')
            if not is_dashed_ltype(ltype):
                continue
            pts = list(ent.points or [])
            if len(pts) < 4 or len(pts) > self.MAX_PTS_FOR_RECT:
                continue
            bbox = self._rect_bbox(pts)
            if bbox is None:
                continue
            if bbox.w < self.MIN_RECT_SIDE or bbox.h < self.MIN_RECT_SIDE:
                continue
            # Closure check — accept any of:
            #   (a) the LWPOLYLINE has `closed=True` (rare in dwgread);
            #   (b) the first and last input points coincide (a 5th
            #       vertex equal to the 1st is the standard "closed
            #       polyline" representation);
            #   (c) the polyline has exactly 4 vertices that already
            #       form a closed axis-aligned rectangle (bypassing the
            #       "missing closing edge" dwgread artefact that several
            #       cabinet boxes exhibit on shengli DWGs).
            closed = bool(getattr(ent, 'closed', False))
            if not closed and pts:
                first, last = pts[0], pts[-1]
                closed = (abs(first.x - last.x) < self.RECT_ABS_TOL
                          and abs(first.y - last.y) < self.RECT_ABS_TOL)
            if not closed and len(pts) == 4:
                # 4 axis-aligned corners ⇒ implicit rectangle even when
                # dwgread omitted the closing point.
                closed = True
            if not closed:
                continue
            bid = ent.id or f'bbox_{len(out)}'
            out.append(CabinetBoundary(
                id=bid,
                document_hash=doc.content_hash,
                bbox=bbox,
                points=pts,
                layer=ent.layer or '',
                handle=ent.handle or '',
                ltype=ltype,
                closed=True,
            ))
        # Nesting: today we KEEP all detected boundaries (user confirmed
        # V6.6 only handles the outermost — but in practice the dwgread
        # output never had overlapping dashed rectangles because each
        # cabinet defines its own region. If nesting ever shows up, the
        # caller can post-filter via the contains() method).

        # --- Pass 2: multi-segment LINE rectangles --------------------
        # Some drawings represent cabinet boundaries as 4 separate LINE
        # entities (2 points each) instead of a single LWPOLYLINE. We
        # detect those here by grouping dashed 2-pt LINE segments that
        # form a closed 4-segment chain.
        self._find_multi_segment_rects(doc, out)

        return out

    def _find_multi_segment_rects(
        self,
        doc: Document,
        out: list[CabinetBoundary],
    ) -> None:
        """Append dashed-rectangle boundaries formed by 4 separate LINE
        segments to *out*."""
        _ROUND = 2

        def _r(pt: Point) -> tuple[float, float]:
            return (round(pt.x, _ROUND), round(pt.y, _ROUND))

        # Collect all dashed 2-pt LINE segments.
        segs: list[dict] = []
        for ent in doc.entities:
            if not isinstance(ent, LineGeometry):
                continue
            pts = list(ent.points or [])
            if len(pts) != 2:
                continue
            cf = getattr(ent, 'custom_fields', None) or {}
            ltype = cf.get('ltype', '')
            if not is_dashed_ltype(ltype):
                continue
            segs.append({
                'pts': pts,
                'layer': ent.layer or '',
                'handle': ent.handle or '',
                'ltype': ltype,
            })

        if len(segs) < 4:
            return

        # Build endpoint → segment index.
        ep_to_idxs: dict[tuple[float, float], list[int]] = defaultdict(list)
        for i, seg in enumerate(segs):
            for pt in seg['pts']:
                ep_to_idxs[_r(pt)].append(i)

        def other_key(seg_idx: int, key: tuple[float, float]) -> tuple[float, float]:
            seg = segs[seg_idx]
            k1, k2 = _r(seg['pts'][0]), _r(seg['pts'][1])
            return k2 if k1 == key else k1

        used: set[int] = set()
        for start_i in range(len(segs)):
            if start_i in used:
                continue
            seg0 = segs[start_i]
            p1, p2 = _r(seg0['pts'][0]), _r(seg0['pts'][1])
            if len(ep_to_idxs.get(p1, [])) != 2 or len(ep_to_idxs.get(p2, [])) != 2:
                continue

            # Trace forward from p1 → p2.
            chain = [start_i]
            cur_key = p2
            ok = True
            for _ in range(3):
                cands = ep_to_idxs.get(cur_key, [])
                nxt = next((c for c in cands if c not in chain), None)
                if nxt is None:
                    ok = False
                    break
                chain.append(nxt)
                cur_key = other_key(nxt, cur_key)
            if not ok or cur_key != p1:
                continue

            # Collect the 4 unique corner points.
            corners = [_r(segs[chain[0]]['pts'][0])]
            for idx in chain:
                k = _r(segs[idx]['pts'][1])
                if k != corners[-1]:
                    corners.append(k)
            if len(corners) != 4:
                continue

            corner_pts = [Point(x, y) for x, y in corners]
            bbox = self._rect_bbox(corner_pts)
            if bbox is None:
                continue

            bid = f'line_rect_{len(out)}'
            out.append(CabinetBoundary(
                id=bid,
                document_hash=doc.content_hash,
                bbox=bbox,
                points=corner_pts,
                layer=segs[start_i]['layer'],
                handle='',
                ltype=segs[start_i]['ltype'],
                closed=True,
            ))
            used.update(chain)

    def _rect_bbox(self, pts: list[Point]) -> Optional[BBox]:
        """Return the axis-aligned bbox of `pts` if and only if the
        points trace a near-axis-aligned rectangle. Tolerance is
        `min(self.RECT_ABS_TOL, max_dim * self.RECT_RTOL)`."""
        if len(pts) < 4:
            return None
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        w = x_max - x_min
        h = y_max - y_min
        if w < self.MIN_RECT_SIDE or h < self.MIN_RECT_SIDE:
            return None
        tol = min(self.RECT_ABS_TOL, max(w, h) * self.RECT_RTOL)
        # Each input vertex must coincide with one of the 4 bbox corners.
        corners = {(x_min, y_min), (x_min, y_max), (x_max, y_min), (x_max, y_max)}
        for p in pts:
            ok = False
            for (cx, cy) in corners:
                if abs(p.x - cx) <= tol and abs(p.y - cy) <= tol:
                    ok = True
                    break
            if not ok:
                return None
        return BBox(x=x_min, y=y_min, w=w, h=h)

    # ------------------------------------------------------------------
    # 2. Text collection
    # ------------------------------------------------------------------
    def _collect_texts(self, doc: Document) -> list[_TextRef]:
        """Collect positional references from every textual entity in
        the document. Includes `TextEntity` (TEXT/MTEXT) AND
        `AttributeEntity` (ATTRIB) — the latter is critical because
        cabinet names like `EquName` / `EQUNAME` are stored as block
        ATTRIBs in shengli 回路图 drawings, not as free-standing TEXT."""
        from ..ir import AttributeEntity
        out: list[_TextRef] = []
        for ent in doc.entities:
            if not isinstance(ent, (TextEntity, AttributeEntity)):
                continue
            cf = getattr(ent, 'custom_fields', None) or {}
            x = cf.get('x')
            y = cf.get('y')
            if x is None or y is None:
                continue
            t = (getattr(ent, 'text', '') or '').strip()
            if not t:
                continue
            out.append(_TextRef(text=t, x=float(x), y=float(y), text_id=ent.id))
        return out

    # ------------------------------------------------------------------
    # 3. Name / location / label matching
    # ------------------------------------------------------------------
    def _match_boundary_text(
        self,
        b: CabinetBoundary,
        doc_hash: str,
        texts: list[_TextRef],
        by_y: dict[int, list[_TextRef]],
    ) -> CabinetRecord:
        rec = CabinetRecord(boundary=b, document_hash=doc_hash)

        # Pre-filter candidates by y-range (above or near the boundary).
        bx_top = b.bbox.y + b.bbox.h
        bbox_cx = b.bbox.x + b.bbox.w / 2
        candidates_above: list[_TextRef] = []
        candidates_below: list[_TextRef] = []
        for t in texts:
            if b.bbox.x - 50 <= t.x <= b.bbox.x + b.bbox.w + 80:
                if t.y > bx_top and t.y - bx_top <= self.NAME_ABOVE_DY:
                    candidates_above.append(t)
                elif t.y < b.bbox.y and b.bbox.y - t.y <= self.LABEL_BELOW_DY + 200:
                    candidates_below.append(t)

        # --- name: prefer text above containing a keyword; else closest
        #     short-code-looking token above the boundary.
        #     Primary: vertical distance to boundary top (smaller = better).
        #     Tie-break: texts within SAME_ROW_TOL units vertically are
        #     considered same-row; pick the closer one horizontally.
        SAME_ROW_TOL = 3.0
        name_pick: Optional[_TextRef] = None
        name_dy = float('inf')
        name_dx = float('inf')
        for t in candidates_above:
            if any(kw in t.text for kw in _NAME_KEYWORDS):
                d = abs(t.y - bx_top)
                if d < name_dy - SAME_ROW_TOL or (abs(d - name_dy) <= SAME_ROW_TOL and abs(t.x - bbox_cx) < name_dx):
                    name_dy = d
                    name_dx = abs(t.x - bbox_cx)
                    name_pick = t
        if name_pick is None:
            name_dx = float('inf')
            name_dy = float('inf')
            for t in candidates_above:
                if _SHORT_NAME.match(t.text):
                    d = abs(t.y - bx_top)
                    if d < name_dy - SAME_ROW_TOL or (abs(d - name_dy) <= SAME_ROW_TOL and abs(t.x - bbox_cx) < name_dx):
                        name_dy = d
                        name_dx = abs(t.x - bbox_cx)
                        name_pick = t

        if name_pick is not None:
            rec.name = name_pick.text
            rec.name_text_id = name_pick.text_id

            # --- location: closest text to the left on the SAME y row.
            key = round(name_pick.y * 2)
            nearby = by_y.get(key) or by_y.get(key + 1) or by_y.get(key - 1) or []
            best_dx = float('inf')
            for t in nearby:
                if t.text_id == name_pick.text_id:
                    continue
                if t.x >= name_pick.x:
                    continue
                if abs(t.y - name_pick.y) > self.LOCATION_NAME_DY:
                    continue
                dx = name_pick.x - t.x
                if dx > self.LOCATION_NAME_DX:
                    continue
                if dx < best_dx:
                    best_dx = dx
                    rec.location = t.text
                    rec.location_text_id = t.text_id

            rec.display_name = (
                f'{rec.location}-{rec.name}' if rec.location else rec.name
            )

        # --- text_label: descriptive text BELOW the boundary.
        label_pick: Optional[_TextRef] = None
        label_dy = float('inf')
        for t in candidates_below:
            if not any(kw in t.text for kw in _NAME_KEYWORDS):
                continue
            if len(t.text) < 3 or len(t.text) > 30:
                continue
            d = b.bbox.y - t.y
            if d < 0:
                continue
            if d < label_dy:
                label_dy = d
                label_pick = t
        if label_pick is not None:
            rec.text_label = label_pick.text
            rec.label_text_id = label_pick.text_id
            if rec.name == '':
                # When no above-text was found, treat the below-label
                # as the canonical name (some drawings only label below).
                rec.name = label_pick.text
                rec.display_name = (
                    f'{rec.location}-{rec.name}' if rec.location else rec.name
                )
            elif not any(kw in rec.name for kw in _NAME_KEYWORDS):
                # Above-boundary name is a short code (not a keyword),
                # but we found a descriptive keyword text below the
                # boundary. Prefer the keyword text as the name.
                rec.name = label_pick.text
                rec.text_label = ''
                rec.label_text_id = ''
                rec.display_name = (
                    f'{rec.location}-{rec.name}' if rec.location else rec.name
                )

        return rec


# ---------------------------------------------------------------------------
# Containment helper — caller uses this to populate cabinet_terminals
# rows: given a list of CabinetRecord + a list of (terminal_x, terminal_y,
# terminal_id, terminal_kind) tuples, return the index of the cabinet
# each terminal belongs to (or -1 when none).
# ---------------------------------------------------------------------------
def assign_terminals_to_cabinets(
    records: list[CabinetRecord],
    terminals: Iterable[tuple[float, float, str, str]],
    pad: float = 0.0,
) -> list[tuple[str, str, str, float, float]]:
    """Return [(cabinet_id, terminal_id, kind, x, y)] for every
    containment match.  The same terminal_id may appear in multiple
    rows if the terminal lies inside several cabinet bboxes (e.g. a
    cabinet that is duplicated at different y positions, or overlapping
    cabinets).  The caller / storage layer deduplicates by
    (cabinet_id, terminal_id, kind) if needed."""
    recs = sorted(records, key=lambda r: r.boundary.area)
    out: list[tuple[str, str, str, float, float]] = []
    for x, y, tid, kind in terminals:
        for r in recs:
            if r.boundary.contains(x, y, pad=pad):
                out.append((r.id, tid, kind, x, y))
    return out


# ---------------------------------------------------------------------------
# Grid spatial index for fast cabinet lookup.
# ---------------------------------------------------------------------------
class CabinetGridIndex:
    """Flat-grid spatial index for cabinet bbox containment queries.

    Build once per document, then ``lookup(x, y)`` is O(1) — find which
    cabinet (if any) contains the given point, returning the smallest
    enclosing cabinet (matching ``_ws_in_cabinet`` semantics).

    Usage::

        idx = CabinetGridIndex(v66_cabinets, cell_size=50)
        cab_id = idx.lookup(x, y)          # → str | None
        cab_name = idx.lookup_name(x, y)   # → str | None

    The grid cell size should be roughly half the smallest cabinet
    dimension so that each bbox spans at least one full cell.
    """

    def __init__(
        self,
        cabinets: list[dict],
        cell_size: float = 50.0,
    ):
        self._cell_size = cell_size
        self._bboxes: dict[str, BBox] = {}  # cab_id → BBox
        self._names: dict[str, str] = {}     # cab_id → display_name
        # grid[ (cx, cy) ] = list of (cab_id, area) sorted ascending
        self._grid: dict[tuple[int, int], list[tuple[str, float]]] = {}

        for c in cabinets:
            cab_id: str = c['id']
            bbox: BBox = c['bbox']
            area = bbox.w * bbox.h
            self._bboxes[cab_id] = bbox
            self._names[cab_id] = c.get('display_name') or ''

            cx0 = int(bbox.x // cell_size)
            cy0 = int(bbox.y // cell_size)
            cx1 = int((bbox.x + bbox.w) // cell_size)
            cy1 = int((bbox.y + bbox.h) // cell_size)
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    self._grid.setdefault((cx, cy), []).append((cab_id, area))

        # Sort each cell's list by area — smallest first.
        for cell_list in self._grid.values():
            cell_list.sort(key=lambda x: x[1])

    def lookup(self, x: float, y: float) -> Optional[str]:
        """Return the **smallest** cabinet id whose bbox contains (x,y),
        or None when no cabinet covers that point. O(1) typical."""
        cx = int(x // self._cell_size)
        cy = int(y // self._cell_size)
        candidates = self._grid.get((cx, cy))
        if not candidates:
            return None
        for cab_id, _area in candidates:
            bbox = self._bboxes.get(cab_id)
            if bbox is None:
                continue
            if bbox.x <= x <= bbox.x + bbox.w and bbox.y <= y <= bbox.y + bbox.h:
                return cab_id
        return None

    def lookup_name(self, x: float, y: float) -> Optional[str]:
        """Like ``lookup`` but returns the cabinet's display_name."""
        cab_id = self.lookup(x, y)
        if cab_id is None:
            return None
        return self._names.get(cab_id) or None


__all__ = [
    'CabinetBoundary',
    'CabinetRecord',
    'CabinetRegionAnalyzer',
    'CabinetGridIndex',
    'is_dashed_ltype',
    'assign_terminals_to_cabinets',
]
