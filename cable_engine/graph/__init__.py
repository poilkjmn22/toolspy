"""cable_engine.graph — V6 Topology Builder.

The graph package contains the TopologyStage, which dispatches to
analyzers that build cable_topology rows at scan time.
"""

from .builder import TopologyStage

__all__ = [
    'TopologyStage',
]
