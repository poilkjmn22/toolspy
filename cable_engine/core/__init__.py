"""cable_engine.core — Domain primitives shared across all sub-packages.

The core package holds base types and interfaces that both the
electrical (graph/topology) and physical (layout) worlds depend on.
No sub-package in cable_engine.core may import from cable_engine.graph
or cable_engine.layout — it is the bottom of the dependency stack.
"""

from .asset import SpatialContainer

__all__ = ['SpatialContainer']
