"""cable_engine.layout.candidate — Unified DeviceCandidate generation.

V8.1 Candidate Pipeline:

  ClosedRectDetector  ──→ DeviceCandidate(0.95)
  OpenShapeDetector   ──→ DeviceCandidate(L=0.5, U=0.7)
  CircleDetector      ──→ SymbolCandidate  ──(promote)──→ DeviceCandidate(0.60)
  TextDetector        ──→ DeviceCandidate(0.40)
           │
           ▼
     CandidatePool.dedup()
           │
           ▼
     list[DeviceCandidate]
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..ir import (
    AttributeEntity, BlockRef, CircleGeometry, Document,
    LineGeometry, Point, TextEntity,
)
from ..ir.entities import BBox
from .primitives.bbox import bbox_contains_center, bbox_overlap_ratio
from .primitives.rectangle import detect_rectangles


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DeviceCandidate:
    id: str
    bbox: BBox
    texts: list[tuple[str, float, float]] = field(default_factory=list)
    score: float = 0.0
    source: str = ''
    name: str = ''
    description: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)

    @property
    def cx(self) -> float:
        return self.bbox.x + self.bbox.w / 2

    @property
    def cy(self) -> float:
        return self.bbox.y + self.bbox.h / 2


@dataclass
class SymbolCandidate:
    id: str
    bbox: BBox
    center: Point
    radius: float
    texts: list[tuple[str, float, float]] = field(default_factory=list)
    score: float = 0.0


class CandidatePool:
    """Collect and deduplicate device/symbol candidates from all sources."""

    def __init__(self) -> None:
        self._devices: list[DeviceCandidate] = []
        self._symbols: list[SymbolCandidate] = []

    def add_device(self, cand: DeviceCandidate) -> None:
        self._devices.append(cand)

    def add_symbol(self, sym: SymbolCandidate) -> None:
        self._symbols.append(sym)

    def dedup(self) -> list[DeviceCandidate]:
        accepted: list[DeviceCandidate] = []
        for d in sorted(self._devices, key=lambda x: -x.score):
            dup = False
            for a in accepted:
                if bbox_overlap_ratio(d.bbox, a.bbox) > 0.4:
                    dup = True
                    break
            if not dup:
                accepted.append(d)
        for s in self._symbols:
            covered = False
            for a in accepted:
                if bbox_overlap_ratio(s.bbox, a.bbox) > 0.5:
                    covered = True
                    break
            if not covered:
                name = s.texts[0][0] if s.texts else ''
                accepted.append(DeviceCandidate(
                    id=s.id,
                    bbox=s.bbox,
                    texts=s.texts,
                    score=s.score,
                    source='symbol',
                    name=name,
                ))
        return accepted


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def detect_closed_rects(doc: Document, container: BBox,
                        ) -> list[DeviceCandidate]:
    """Closed 4-point rectangles → DeviceCandidate(0.95)."""
    rects = detect_rectangles(doc)
    out: list[DeviceCandidate] = []
    for i, r in enumerate(rects):
        if not bbox_contains_center(container, r.bbox):
            continue
        if r.bbox.w < 3 or r.bbox.h < 3 or r.bbox.w > 150 or r.bbox.h > 150:
            continue
        out.append(DeviceCandidate(
            id=f'cr_{i}', bbox=r.bbox, score=0.95, source='closed_rect',
        ))
    return out


def detect_open_shapes(doc: Document, container: BBox,
                       ) -> list[DeviceCandidate]:
    """L/U shaped open enclosures.

    L-shape (2 segments, 90° joined) → 0.5
    U-shape (3 segments, parallel ends) → 0.7
    """
    segs = _collect_2pt_segments(doc, container)
    out: list[DeviceCandidate] = []
    used: set[int] = set()
    n = len(segs)

    # U-shapes first (more specific, 3-segment chains)
    all_indices = list(range(n))
    _detect_U_shapes(segs, all_indices, used, out)

    # L-shapes from remaining segments
    for i in range(n):
        if i in used:
            continue
        for j in range(i + 1, n):
            if j in used:
                continue
            shared, a_far, b_far = _shared_endpoint(
                segs[i], segs[j], tol=2.0)
            if shared is None:
                continue
            angle = _angle_between(
                (a_far[0] - shared[0], a_far[1] - shared[1]),
                (b_far[0] - shared[0], b_far[1] - shared[1]),
            )
            if angle < 85 or angle > 95:
                continue
            xs = [shared[0], a_far[0], b_far[0]]
            ys = [shared[1], a_far[1], b_far[1]]
            bbox = BBox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
            if bbox.w >= 3 and bbox.h >= 3:
                used.add(i)
                used.add(j)
                out.append(DeviceCandidate(
                    id=f'ls_{len(out)}', bbox=bbox,
                    score=0.5, source='L_shape',
                ))
            break

    return out


def detect_circle_symbols(doc: Document, container: BBox,
                          ) -> list[SymbolCandidate]:
    """Circles with text inside → SymbolCandidate(0.60)."""
    out: list[SymbolCandidate] = []
    for i, e in enumerate(doc.entities):
        if not isinstance(e, CircleGeometry):
            continue
        c = getattr(e, 'center', None)
        if c is None:
            continue
        r = getattr(e, 'radius', None) or 0
        if r < 2 or r > 30:
            continue
        approx = BBox(c.x - r, c.y - r, r * 2, r * 2)
        if not bbox_contains_center(container, approx, pad=0):
            continue
        texts = _texts_near(doc, c.x, c.y, r * 1.5)
        if not texts:
            continue
        out.append(SymbolCandidate(
            id=f'cir_{i}', bbox=approx,
            center=c, radius=r, texts=texts, score=0.60,
        ))
    return out


def detect_text_devices(doc: Document, container: BBox,
                        existing: list[DeviceCandidate],
                        ) -> list[DeviceCandidate]:
    """Standalone text w/o enclosing shape → DeviceCandidate(0.40)."""
    existing_bboxes = [d.bbox for d in existing]
    out: list[DeviceCandidate] = []
    for i, e in enumerate(doc.entities):
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t or len(t) > 20:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        if not (container.x <= ex <= container.x + container.w and
                container.y <= ey <= container.y + container.h):
            continue
        dummy = BBox(ex - 1, ey - 1, 2, 2)
        dup = False
        for eb in existing_bboxes:
            if bbox_overlap_ratio(dummy, eb) > 0.2:
                dup = True
                break
        if dup:
            continue
        tw = max(len(t) * 10.0, 20.0)
        bbox = BBox(ex - tw / 2, ey - 7.5, tw, 15.0)
        out.append(DeviceCandidate(
            id=f'txt_{i}', bbox=bbox,
            texts=[(t, ex, ey)],
            score=0.40, source='text',
        ))
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def build_device_candidates(doc: Document, container: BBox,
                            ) -> list[DeviceCandidate]:
    """Run all detectors → deduplicated DeviceCandidates."""
    pool = CandidatePool()
    for d in detect_closed_rects(doc, container):
        pool.add_device(d)
    for d in detect_open_shapes(doc, container):
        pool.add_device(d)
    for s in detect_circle_symbols(doc, container):
        pool.add_symbol(s)
    accepted = pool.dedup()
    for d in detect_text_devices(doc, container, accepted):
        pool.add_device(d)
    return pool.dedup()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_2pt_segments(doc: Document, container: BBox,
                          margin: float = 5.0) -> list[dict]:
    segs: list[dict] = []
    for e in doc.entities:
        if not isinstance(e, LineGeometry):
            continue
        pts = list(e.points or [])
        if len(pts) != 2:
            continue
        p1, p2 = pts[0], pts[-1]
        sx, sy = min(p1.x, p2.x), min(p1.y, p2.y)
        ex, ey = max(p1.x, p2.x), max(p1.y, p2.y)
        if (sx < container.x - margin or ex > container.x + container.w + margin
                or sy < container.y - margin or ey > container.y + container.h + margin):
            continue
        segs.append({
            'p1': (p1.x, p1.y),
            'p2': (p2.x, p2.y),
            'handle': e.handle or '',
        })
    return segs


def _shared_endpoint(a: dict, b: dict, tol: float,
                     ) -> tuple[Optional[tuple[float, float]],
                                Optional[tuple[float, float]],
                                Optional[tuple[float, float]]]:
    for a_pt in (a['p1'], a['p2']):
        for b_pt in (b['p1'], b['p2']):
            if abs(a_pt[0] - b_pt[0]) <= tol and abs(a_pt[1] - b_pt[1]) <= tol:
                a_far = a['p2'] if a_pt == a['p1'] else a['p1']
                b_far = b['p2'] if b_pt == b['p1'] else b['p1']
                return (a_pt, a_far, b_far)
    return (None, None, None)


def _angle_between(v1: tuple[float, float],
                   v2: tuple[float, float]) -> float:
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    m1 = math.hypot(v1[0], v1[1])
    m2 = math.hypot(v2[0], v2[1])
    if m1 == 0 or m2 == 0:
        return 0
    cos_a = max(-1, min(1, dot / (m1 * m2)))
    return math.degrees(math.acos(cos_a))


def _detect_U_shapes(
    segs: list[dict],
    global_indices: list[int],
    used_global: set[int],
    out: list[DeviceCandidate],
) -> None:
    ep_map: dict[tuple[float, float], list[int]] = defaultdict(list)
    for idx, s in enumerate(segs):
        for pt in (s['p1'], s['p2']):
            ep_map[(_r(pt[0]), _r(pt[1]))].append(idx)

    for start_idx, s in enumerate(segs):
        gidx = global_indices[start_idx]
        if gidx in used_global:
            continue
        chain = _chain_3(segs, start_idx, ep_map, set())
        if chain is None:
            continue
        p1 = segs[chain[0]]['p1']
        p2 = segs[chain[0]]['p2']
        p3 = segs[chain[1]]['p2']
        p4 = segs[chain[2]]['p2']

        def _is_vert(pt1, pt2):
            return abs(pt1[0] - pt2[0]) < 1

        def _is_hor(pt1, pt2):
            return abs(pt1[1] - pt2[1]) < 1

        v1 = _is_vert(p1, p2) or _is_hor(p1, p2)
        v2 = _is_vert(segs[chain[2]]['p1'], segs[chain[2]]['p2']) or \
            _is_hor(segs[chain[2]]['p1'], segs[chain[2]]['p2'])
        if not (v1 and v2):
            continue
        all_pts = [p1, p2, p3, p4]
        xs = [pt[0] for pt in all_pts]
        ys = [pt[1] for pt in all_pts]
        bbox = BBox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        if bbox.w < 3 or bbox.h < 3:
            continue
        for ci in chain:
            used_global.add(global_indices[ci])
        out.append(DeviceCandidate(
            id=f'us_{len(out)}', bbox=bbox,
            score=0.7, source='U_shape',
        ))


def _chain_3(segs, start_idx, ep_map, used_set) -> Optional[list[int]]:
    chain = [start_idx]
    cur = start_idx
    for _ in range(2):
        s = segs[cur]
        candidates = []
        for pt in (s['p1'], s['p2']):
            key = (_r(pt[0]), _r(pt[1]))
            for idx in ep_map.get(key, []):
                if idx != cur and idx not in chain:
                    candidates.append(idx)
        if not candidates:
            return None
        cur = candidates[0]
        chain.append(cur)
    return chain if len(chain) == 3 else None


def _r(v: float) -> float:
    return round(v, 1)


def _texts_near(doc: Document, x: float, y: float, radius: float,
                ) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        t = (e.text or '').strip()
        if not t:
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        d = math.hypot(ex - x, ey - y)
        if d <= radius:
            out.append((t, ex, ey))
    out.sort(key=lambda item: math.hypot(item[1] - x, item[2] - y))
    return out


__all__ = [
    'DeviceCandidate', 'SymbolCandidate', 'CandidatePool',
    'detect_closed_rects', 'detect_open_shapes',
    'detect_circle_symbols', 'detect_text_devices',
    'build_device_candidates',
]
