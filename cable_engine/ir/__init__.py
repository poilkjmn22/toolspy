"""cable_engine.ir — V5 Intermediate Representation.

Source-agnostic: PDF, DWG, and future formats all produce the same
Document / Page / Entity shapes.

Source-specific bits:
  - ir.pdf           -- Page, PixelImage (PDF rasterization layer)
  - ir.document      -- Document, DocumentType (top-level container)

Source-agnostic base types:
  - ir.entities      -- Entity + TextEntity + Point + BBox (core)
  - ir.geometry      -- V5 geometry entities (LineGeometry,
                          CircleGeometry, ArcGeometry, BlockRef,
                          AttributeEntity). The Graph Builder works on
                          these primarily.
"""

from .entities import (
    BBox, Entity, LineEntity, Point, PolylineEntity, SymbolEntity,
    TextEntity,
)
from .geometry import (
    GeometryEntity, LineGeometry, CircleGeometry, ArcGeometry,
    BlockRef, AttributeEntity,
)
from .pdf import Page, PixelImage
from .document import Document, DocumentType


__all__ = [
    # Source-agnostic
    'Entity', 'TextEntity', 'LineEntity', 'PolylineEntity',
    'SymbolEntity', 'Point', 'BBox',
    # Source-agnostic (V5 geometry)
    'GeometryEntity', 'LineGeometry', 'CircleGeometry', 'ArcGeometry',
    'BlockRef', 'AttributeEntity',
    # Document-level
    'Document', 'DocumentType',
    # PDF-specific
    'Page', 'PixelImage',
]

