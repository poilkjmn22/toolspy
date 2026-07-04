"""cable_engine.ir.geometry — V5 geometry entity types.

V4's IR (`entities.py`) models LineEntity + PolylineEntity + SymbolEntity.
V5 extends this with full geometry semantics needed by the Graph Builder:

  - GeometryEntity    -- base for anything with spatial position + bbox
  - LineGeometry      -- LINE, LWPOLYLINE, SPLINE (any 2D polyline)
  - CircleGeometry    -- CIRCLE (approximated as polyline at graph-build time)
  - ArcGeometry       -- ARC (approximated as polyline at graph-build time)
  - BlockRef          -- DWG INSERT (block instance with transform)
  - AttributeEntity   -- DWG ATTRIB (text anchored to a block)

These types are first-class citizens of the V5 Document IR. The V4
types (LineEntity, PolylineEntity, SymbolEntity, TextEntity) remain
for backward compatibility but the Graph Builder works primarily on
the V5 types.

Document space convention (V5):
  - Origin: bottom-left of the document
  - Units:  document units (DWG mm by default; PDF pages are scaled
            to DWG mm at load time when their physical dimensions are
            known, otherwise left in pixel space at 300 DPI).
  - Y-axis: increases upward (DWG convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .entities import BBox, Entity, Point


# ---------------------------------------------------------------------------
# Geometry entity base
# ---------------------------------------------------------------------------
@dataclass
class GeometryEntity(Entity):
    """Anything that has a spatial position and (typically) a bbox.

    Adds `handle` for stable DWG identification (the DXF handle, a hex
    string that survives across dwgread invocations).
    """
    handle: str = ''


# ---------------------------------------------------------------------------
# Linear geometry
# ---------------------------------------------------------------------------
@dataclass
class LineGeometry(GeometryEntity):
    """A LINE / LWPOLYLINE / SPLINE in the source drawing.

    `points` is the polyline representation (≥2 points). CIRCLE and ARC
    are also stored as LineGeometry after being approximated as polylines
    at load time, so the Graph Builder doesn't need a separate code path
    for curved geometry.

    `closed` is True when the source polyline was closed (or when the
    source was a CIRCLE).
    """
    points: list[Point] = field(default_factory=list)
    closed: bool = False


# ---------------------------------------------------------------------------
# Circular / arc geometry — preserved as separate types for richer queries
# ---------------------------------------------------------------------------
@dataclass
class CircleGeometry(GeometryEntity):
    """A CIRCLE entity."""
    center: Optional[Point] = None
    radius: float = 0.0


@dataclass
class ArcGeometry(GeometryEntity):
    """An ARC entity."""
    center: Optional[Point] = None
    radius: float = 0.0
    start_angle: float = 0.0
    end_angle: float = 0.0


# ---------------------------------------------------------------------------
# Block references
# ---------------------------------------------------------------------------
@dataclass
class BlockRef(GeometryEntity):
    """A DWG INSERT — a block instance placed at a specific position.

    The `name` is the block name. `insert_point` + `rotation` + `scale`
    define the transform applied to all entities inside the block to
    place them in model space.

    In V5 the Graph Builder eagerly resolves anonymous block references:
    the block's internal entities (LINE / TEXT / ATTRIB) are emitted
    as top-level entities in document space, with their parent
    BlockRef id attached. This unlocks the L-shape detection that
    failed in V4 because anonymous blocks weren't expanded.
    """
    name: str = ''
    insert_point: Optional[Point] = None
    rotation: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0


# ---------------------------------------------------------------------------
# Block attributes
# ---------------------------------------------------------------------------
@dataclass
class AttributeEntity(GeometryEntity):
    """A DWG ATTRIB — text with a tag, anchored to its parent block.

    The `tag` is the attribute tag (e.g. "CABLE_ID", "TERMINAL_A").
    `parent_block_id` is set by the Graph Builder when the parent
    BlockRef is known, linking the attribute to its block.

    `text` carries the actual rendered value (may differ from `tag`).
    """
    tag: str = ''
    text: str = ''
    parent_block_id: Optional[str] = None


__all__ = [
    'GeometryEntity',
    'LineGeometry', 'CircleGeometry', 'ArcGeometry',
    'BlockRef', 'AttributeEntity',
]