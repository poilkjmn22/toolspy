"""cable_engine.ir — Intermediate Representation for cable-match.

The IR is the data passed between Stages. It is intentionally minimal
(only what the current pipeline actually consumes) but designed
source-agnostic: PDF, DWG, and future formats (DXF, scans) all produce
the same Document / Page / Entity shapes.

Source-specific bits:
  - ir.pdf           -- Page, PixelImage (PDF rasterization layer)
  - ir.document      -- Document, DocumentType (top-level container)

Source-agnostic base types (used by every source):
  - ir.entities      -- Entity + TextEntity/LineEntity/PolylineEntity/
                          SymbolEntity + Point + BBox
"""

from .entities import (
    BBox, Entity, LineEntity, Point, PolylineEntity, SymbolEntity,
    TextEntity,
)
from .pdf import Page, PixelImage
from .document import Document, DocumentType

# Backward-compat shim (old geometry.py + text.py class names). Imported
# for side effects so old names (Text, Line, Symbol, TextBox) get
# registered on this module.
from . import _compat  # noqa: F401
from ._compat import Text, Line, Symbol, TextBox  # noqa: F401


__all__ = [
    # Source-agnostic
    'Entity', 'TextEntity', 'LineEntity', 'PolylineEntity',
    'SymbolEntity', 'Point', 'BBox',
    # Document-level
    'Document', 'DocumentType',
    # PDF-specific
    'Page', 'PixelImage',
    # Backward compat
    'Text', 'Line', 'Symbol', 'TextBox',
]

