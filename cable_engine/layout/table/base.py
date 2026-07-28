"""BaseTableParser — template-method skeleton for CAD table parsing.

Subclass and configure:
  - HEADER_PATTERNS:  list of (regex, role_name)
  - ROW_TOL / MAX_TEXT_LEN / noise filter

Override one or more of:
  - detect_bboxes()   — how to find the table region
  - extract_data()    — convert parsed rows → domain output

Detection strategies (tried in order in :meth:`detect_bboxes`):
  A — DBSCAN grid clustering on rectangle centroids (high confidence)
  B — Single large rectangle + text count (medium confidence)
  C — Title text ending with "表" + offset bbox (low confidence)
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

import numpy as np
from sklearn.cluster import DBSCAN

from cable_engine.ir import Document
from cable_engine.ir.entities import BBox
from cable_engine.ir import TextEntity, AttributeEntity
from ..primitives.rectangle import detect_rectangles
from .text_utils import (
    NoiseFilter,
    _default_noise,
    collect_texts,
    count_texts_in,
    detect_gap_x,
    find_header_row,
    map_column_roles,
    y_bucket_rows,
    y_bucket_rows_with_labels,
)


_CELL_EPS = 30.0
_MIN_GRID_RECTS = 6
_MIN_TABLE_W = 60.0
_MIN_TABLE_H = 80.0
_MIN_TEXTS = 4


def _is_grid_like(cxs: list[float], cys: list[float], tol: float = 8.0) -> bool:
    """Check whether centroids form a grid-like arrangement.

    More lenient than GridAnalyzer: groups centroids into row/column
    clusters and checks that the majority of cells fit in a cols×rows
    matrix.  Does NOT require exact fill.
    """
    if len(cxs) < 4:
        return False
    ux = set()
    uy = set()
    for cx in cxs:
        b = round(cx / tol) * tol
        ux.add(b)
    for cy in cys:
        b = round(cy / tol) * tol
        uy.add(b)
    if len(ux) < 2 or len(uy) < 2:
        return False
    expected = len(ux) * len(uy)
    actual = len(cxs)
    fill_ratio = actual / expected if expected > 0 else 0
    return fill_ratio >= 0.5


def detect_table_clusters(doc: Document) -> list[tuple[BBox, float, Optional[tuple[int, int]]]]:
    """Strategy A: DBSCAN on rectangle centroids → grid validation.

    Returns list of (bbox, confidence, grid_dims) for detected table clusters.
    """
    results: list[tuple[BBox, float, Optional[tuple[int, int]]]] = []
    rects = detect_rectangles(doc)
    small = [r for r in rects if r.bbox.w < 200 and r.bbox.h < 200]
    if len(small) < _MIN_GRID_RECTS:
        return results

    centroids = np.array([
        [r.bbox.x + r.bbox.w / 2, r.bbox.y + r.bbox.h / 2]
        for r in small
    ])
    labels = DBSCAN(eps=_CELL_EPS, min_samples=_MIN_GRID_RECTS).fit_predict(centroids)

    clusters: dict[int, list] = {}
    for rect, label in zip(small, labels):
        if label == -1:
            continue
        clusters.setdefault(int(label), []).append(rect)

    for cluster_rects in clusters.values():
        if len(cluster_rects) < _MIN_GRID_RECTS:
            continue
        xs = [r.bbox.x for r in cluster_rects]
        ys = [r.bbox.y for r in cluster_rects]
        xe = [r.bbox.x + r.bbox.w for r in cluster_rects]
        ye = [r.bbox.y + r.bbox.h for r in cluster_rects]
        bbox = BBox(min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))

        cxs = [r.bbox.x + r.bbox.w / 2 for r in cluster_rects]
        cys = [r.bbox.y + r.bbox.h / 2 for r in cluster_rects]
        if not _is_grid_like(cxs, cys):
            continue

        text_count = count_texts_in(doc, bbox)
        if text_count < _MIN_TEXTS:
            continue

        ux = len(set(round(c / 8) * 8 for c in cxs))
        uy = len(set(round(c / 8) * 8 for c in cys))
        dims = (ux, uy) if ux >= 2 and uy >= 2 else None
        results.append((bbox, 0.9, dims))

    return results


def detect_table_bbox_rect(doc: Document, container: Optional[BBox] = None) -> list[BBox]:
    """Strategy B: large rectangles with sufficient text inside."""
    candidates: list[BBox] = []
    for r in detect_rectangles(doc):
        bb = r.bbox
        if container is not None:
            if not (container.x <= bb.x <= container.x + container.w and
                    container.y <= bb.y <= container.y + container.h):
                continue
        if bb.w < _MIN_TABLE_W or bb.h < _MIN_TABLE_H:
            continue
        if count_texts_in(doc, bb) >= _MIN_TEXTS:
            candidates.append(bb)
    return candidates


def detect_table_bbox_title(doc: Document) -> Optional[BBox]:
    """Strategy C: find text ending with 表, return offset bbox."""
    candidates: list[tuple[float, float]] = []
    for e in doc.entities:
        if not isinstance(e, (TextEntity, AttributeEntity)):
            continue
        raw = (e.text or '').strip()
        if not raw or not raw.replace(' ', '').endswith('表'):
            continue
        cf = getattr(e, 'custom_fields', None) or {}
        ex = cf.get('x')
        ey = cf.get('y')
        if ex is None or ey is None:
            continue
        candidates.append((float(ex), float(ey)))
    if not candidates:
        return None
    ex, ey = max(candidates, key=lambda c: c[0])
    return BBox(ex - 200, ey - 350, 350, 450)


class BaseTableParser(ABC):
    """Template-method base class for CAD table parsers.

    Usage::

        class MyParser(BaseTableParser):
            HEADER_PATTERNS = [...]
            ROW_TOL = 3.0

            def extract_data(self, rows, col_roles, ..., doc):
                return MyDomainOutput(...)

        parser = MyParser()
        result = parser.parse(doc)
    """

    HEADER_PATTERNS: list[tuple[re.Pattern, str]] = []
    ROW_TOL: float = 3.0
    MAX_TEXT_LEN: int = 50
    LABEL_PATTERN: Optional[re.Pattern] = None

    def noise_filter(self, text: str) -> bool:
        return _default_noise(text)

    # ------------------------------------------------------------------
    # Detection — override one or more
    # ------------------------------------------------------------------

    def detect_bboxes(self, doc: Document, **kwargs) -> list[tuple[BBox, float]]:
        """Try strategies A → B → C, returning (bbox, confidence) list.

        Override entirely if the table type needs a custom strategy.
        """
        results: list[tuple[BBox, float]] = []

        for bbox, conf, _ in detect_table_clusters(doc):
            results.append((bbox, conf))

        container = kwargs.get('container')
        for bbox in detect_table_bbox_rect(doc, container):
            if not any(abs(r[0].x - bbox.x) < 1 and abs(r[0].y - bbox.y) < 1 for r in results):
                results.append((bbox, 0.6))

        title_bbox = detect_table_bbox_title(doc)
        if title_bbox is not None:
            if not any(abs(r[0].x - title_bbox.x) < 1 and abs(r[0].y - title_bbox.y) < 1 for r in results):
                results.append((title_bbox, 0.4))

        results.sort(key=lambda r: -r[1])
        return results

    # ------------------------------------------------------------------
    # Row anchoring — selectable mode
    # ------------------------------------------------------------------

    def _anchor_rows(
        self,
        texts: list[tuple[float, float, str]],
        mode: str = 'y_bucket',
    ) -> list[list[tuple[float, str]]]:
        """Row detection: 'y_bucket' (pure Y-bucketing) or 'label_centered'."""
        if mode == 'label_centered':
            if self.LABEL_PATTERN is None:
                raise ValueError('label_centered mode requires LABEL_PATTERN')
            return y_bucket_rows_with_labels(texts, self.LABEL_PATTERN, self.ROW_TOL)
        return y_bucket_rows(texts, self.ROW_TOL)

    # ------------------------------------------------------------------
    # Extract — subclass must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def extract_data(
        self,
        rows: list[list[tuple[float, str]]],
        col_roles: dict[int, str],
        header_cells: list[tuple[float, str]],
        bbox: BBox,
        doc: Document,
        **kwargs,
    ) -> Any:
        """Convert parsed rows + column roles into domain-specific output."""
        ...

    # ------------------------------------------------------------------
    # Parse at a known bbox
    # ------------------------------------------------------------------

    def parse_at(self, doc: Document, bbox: BBox, **kwargs) -> Optional[Any]:
        """Parse table at a known bounding box."""
        return self._parse_at(doc, bbox, 1.0, **kwargs)

    # ------------------------------------------------------------------
    # Template method
    # ------------------------------------------------------------------

    def parse(self, doc: Document, **kwargs) -> Optional[Any]:
        """Full parse pipeline with detection. Returns ``None`` if parsing fails."""
        bboxes = self.detect_bboxes(doc, **kwargs)
        if not bboxes:
            return None

        for table_bbox, confidence in bboxes:
            result = self._parse_at(doc, table_bbox, confidence, **kwargs)
            if result is not None:
                return result
        return None

    def _find_header_via_ybucket(
        self, texts: list[tuple[float, float, str]],
    ) -> Optional[tuple[int, list[tuple[float, str]], dict[int, str]]]:
        """Find header row and column roles via pure Y-bucketing.

        Used when the row mode is not suitable for header detection
        (e.g. label_centered where headers don't have labels).
        Returns (header_idx, header_cells, col_roles) or None.
        """
        y_rows = y_bucket_rows(texts, self.ROW_TOL)
        if len(y_rows) < 2:
            return None
        hdr_idx = find_header_row(y_rows, self.HEADER_PATTERNS)
        if hdr_idx is None:
            return None
        hdr_cells = y_rows[hdr_idx]
        roles = map_column_roles(hdr_cells, self.HEADER_PATTERNS)
        if not roles:
            return None
        return hdr_idx, hdr_cells, roles

    def _parse_at(
        self,
        doc: Document,
        table_bbox: BBox,
        confidence: float,
        **kwargs,
    ) -> Optional[Any]:
        texts = collect_texts(
            doc, table_bbox,
            max_len=self.MAX_TEXT_LEN,
            is_noise=self.noise_filter,
        )
        if len(texts) < _MIN_TEXTS:
            return None

        row_mode = kwargs.get('row_mode', 'y_bucket')

        if row_mode == 'label_centered':
            header_info = self._find_header_via_ybucket(texts)
            if header_info is None:
                return None
            header_idx, header_cells, col_roles = header_info
            rows_mode = kwargs.get('row_mode', 'y_bucket')
            rows = [header_cells]
        else:
            rows = self._anchor_rows(texts, mode=row_mode)
            if len(rows) < 2:
                return None
            header_idx = find_header_row(rows, self.HEADER_PATTERNS)
            if header_idx is None:
                return None
            header_cells = rows[header_idx]
            col_roles = map_column_roles(header_cells, self.HEADER_PATTERNS)
            if not col_roles:
                return None

        header_x_min = min(x for x, _ in header_cells)
        header_x_max = max(x for x, _ in header_cells)
        margin = 30.0
        texts = [(ex, ey, t) for ex, ey, t in texts
                 if header_x_min - margin <= ex <= header_x_max + margin]

        gap_x = detect_gap_x(header_cells, col_roles, kwargs.get('gap_role', 'cell_label'))

        return self.extract_data(
            rows=rows,
            col_roles=col_roles,
            header_cells=header_cells,
            bbox=table_bbox,
            doc=doc,
            gap_x=gap_x,
            texts=texts,
            header_idx=header_idx,
            confidence=confidence,
            **kwargs,
        )


__all__ = [
    'BaseTableParser',
    'detect_table_clusters',
    'detect_table_bbox_rect',
    'detect_table_bbox_title',
]
