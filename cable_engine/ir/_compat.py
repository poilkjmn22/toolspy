"""ir/_compat.py — backward-compat shim for old IR class names.

The old cable_engine.ir had:
  - Point (geometry.py)
  - Line, Text, Symbol (geometry.py)
  - TextBox, PageText (text.py)
  - Page, PixelImage (pdf.py)
  - DocumentType (document.py)

After the refactor these are split:
  - Point stays in entities.py
  - Line, Text, Symbol → LineEntity/TextEntity/SymbolEntity (entities.py)
  - TextBox → TextEntity (entities.py)
  - PageText → deleted (Context.full_text already serves this purpose)
  - Page, PixelImage → kept in pdf.py (still PDF-specific; DWG doesn't
    need PixelImage because DWG is vector)
  - DocumentType → kept in document.py

This shim re-exports old names so any external import that
referenced the old classes continues to work.
"""

from .entities import (
    BBox, Entity, LineEntity, PolylineEntity, Point, SymbolEntity,
    TextEntity,
)
from .pdf import Page, PixelImage
from .document import DocumentType

# Old single-class names → new Entity subclasses. We don't subclass —
# we just point the name at the same dataclass. Any code that did
# `isinstance(x, Text)` will work with TextEntity instances.
Text = TextEntity
Line = LineEntity
Symbol = SymbolEntity

# Old ir.text.py used TextBox and PageText. TextBox was equivalent
# to a TextEntity (the old .text attribute maps to the new one).
TextBox = TextEntity
# PageText was a per-page wrapper around TextBox[]. It's no longer
# needed — call sites can iterate doc.pages[0].entities directly, or
# filter with [e for e in doc.entities if isinstance(e, TextEntity)].
# We keep the name as None so any import that did `from .text import
# PageText` gets a clear AttributeError-on-use rather than silent
# breakage.
class _RemovedPageText:
    def __init__(self, *a, **kw):
        raise TypeError(
            'PageText was removed in the IR refactor. Use '
            '`[e for e in doc.entities if isinstance(e, TextEntity)]` '
            'or `doc.pages[page_idx].entities` instead.'
        )
PageText = _RemovedPageText

__all__ = [
    'BBox', 'Entity', 'LineEntity', 'PolylineEntity', 'Point',
    'SymbolEntity', 'TextEntity', 'Page', 'PixelImage', 'DocumentType',
    'Text', 'Line', 'Symbol', 'TextBox', 'PageText',
]
