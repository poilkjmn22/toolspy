"""cable_engine.ir.text — Text IR for cable-match (legacy / compat).

In the refactor, all text-derived IR nodes are TextEntity (in
entities.py). This module is kept as a thin compat shim so existing
imports like `from cable_engine.ir.text import TextBox` keep working
while pointing at the new Entity subclasses.

The old `PageText` (a per-page wrapper around a list of TextBox)
was removed: callers can use `doc.pages[i].entities` or filter
`doc.entities` by `isinstance(e, TextEntity)` instead.
"""

from .entities import TextEntity

# Old alias — keep the name available for compat.
TextBox = TextEntity


__all__ = ['TextBox', 'TextEntity']
