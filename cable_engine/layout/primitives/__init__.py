"""cable_engine.layout.primitives — Low-level geometry primitives.

Each module extracts one family of geometry from the Document IR:

  rectangle.py — DetectedRect + closed-rectangle detection from polylines/segments
  line.py      — LongLine + long vertical/horizontal line detection
  bbox.py      — Pure bbox arithmetic (overlap, containment, extents)
"""
