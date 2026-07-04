"""cable_engine.ir.entities — Source-agnostic IR entities.

The IR is the data passed between Stages. It is intentionally minimal:
we only model the parts that the current pipeline actually consumes.
Future stages (graph, geometry, YOLO) will add new Entity subclasses,
not modify the existing ones.

Entity is the unified base class for everything we extract from a
document. The source field ('pdf' | 'dwg' | 'scan' | ...) lets Fusion
distinguish where each entity came from — DWG entities get confidence
1.0 by default (structurally precise), PDF-OCR entities start lower
(confidence from the OCR engine, propagated up by the OCR Stage).

Why a base class with a single field override pattern:
  - Cable IR (Cable/Terminal/Connection) wants to consume all entities
    regardless of source. A `for e in doc.entities: if e.text: ...` works
    for TextEntity, LineEntity, SymbolEntity alike (only TextEntity has
    .text, but the others have other fields).
  - Future graph stages can downcast: `if isinstance(e, LineEntity)`.
  - YOLO detections will be a new class (DetectedSymbolEntity) that
    subclasses SymbolEntity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Point:
    """2D point in document coordinates (origin convention depends on
    the source: PDF uses top-left y-down, DWG uses bottom-left y-up; the
    consumer of the IR is responsible for normalizing if needed)."""
    x: float
    y: float


@dataclass
class BBox:
    """Axis-aligned bounding box in document coordinates. Used for PDF
    text boxes and YOLO detections. None means unknown (e.g. an entity
    from a DWG file might not have a tight bounding box until we run
    a render-and-detect pass)."""
    x: float
    y: float
    w: float
    h: float


# ---------------------------------------------------------------------------
# Entity base class + concrete subclasses
# ---------------------------------------------------------------------------
@dataclass
class Entity:
    """Base class for every IR entity. Source-agnostic.

    Fields:
        id:          stable handle within a single document. For DWG
                     entities this is the DXF handle (hex string). For
                     PDF text boxes this is a synthetic "<page>:<idx>".
        source:      one of 'pdf', 'dwg', 'scan', etc. Tells Fusion where
                     the entity came from.
        page:        1-indexed page in the source document. DWG files
                     conventionally have page=1 (DWG is single-page).
        confidence:  0.0–1.0. DWG defaults to 1.0; PDF-OCR inherits
                     the OCR engine's reported confidence (or 0.9 if
                     not reported); YOLO inherits its score.
        bbox:        bounding box in document units, if known.
        layer:       DWG layer name, or PDF inferred region (e.g. page).
                     Free-form string; may be empty.
    """
    id: str
    source: str
    page: int
    confidence: float = 1.0
    bbox: Optional[BBox] = None
    layer: str = ''


@dataclass
class TextEntity(Entity):
    """A text run in the source document.

    For PDF: produced by the OCR Stage from a tesseract / paddleocr
    output (one per page-recipe-pair, then optionally merged).

    For DWG: produced by the DWG Loader from TEXT/MTEXT/ATTRIB entities
    (confidence 1.0 since DWG stores text precisely).
    """
    text: str = ''

    def contains(self, needle: str) -> bool:
        """True if `needle` is a substring of this text (case-sensitive)."""
        return bool(needle) and needle in self.text


@dataclass
class LineEntity(Entity):
    """A straight line (DWG LINE) or polyline (DWG LWPOLYLINE)."""
    points: list[Point] = field(default_factory=list)


@dataclass
class PolylineEntity(Entity):
    """A multi-segment line (kept distinct from LineEntity in case the
    geometry stage wants to treat them differently — e.g. only polylines
    get wire-detection)."""
    points: list[Point] = field(default_factory=list)


@dataclass
class SymbolEntity(Entity):
    """A block reference (DWG INSERT) or a detected symbol (future: YOLO).

    `name` is the block name in DWG (e.g. "CABLE_MARKER") or the
    class label from a CV detector.
    """
    name: str = ''


# ---------------------------------------------------------------------------
__all__ = [
    'Point', 'BBox',
    'Entity',
    'TextEntity', 'LineEntity', 'PolylineEntity', 'SymbolEntity',
]
