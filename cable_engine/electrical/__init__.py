"""cable_engine.electrical — V8 Geometry Graph + Electrical Query Layer.

GeometryGraph is the pure geometry-layer graph. ElectricalQuery
composes graph traversal + resolvers for business queries.
"""

from .geometry_graph import (
    GeoEdge, GeoEdgeType, GeometryBuilder, GeometryGraph, GeoNode,
    GeoNodeType, SpatialIndex, Visitor, VisitDecision,
)
from .graph_path import GraphPath, TraceStopReason
from .query import ElectricalQuery
from .resolvers import TerminalResolver, TerminalResult
from .visitors import CabinetEntryVisitor
from .builders import WireBuilder, CabinetBuilder

__all__ = [
    'GeoNode', 'GeoNodeType', 'GeoEdge', 'GeoEdgeType',
    'GeometryGraph', 'GeometryBuilder', 'SpatialIndex',
    'Visitor', 'VisitDecision',
    'GraphPath', 'TraceStopReason',
    'ElectricalQuery',
    'TerminalResolver', 'TerminalResult',
    'CabinetEntryVisitor',
    'WireBuilder', 'CabinetBuilder',
]
