"""cable_engine.electrical.graph_path — GraphPath + TraceStopReason.

Pure path data structure returned by GeometryGraph.trace().
Carries zero business semantics — only "how the search ended".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TraceStopReason(Enum):
    """Why the graph trace stopped — semantic-free, describes the search."""

    VISITOR_STOP = 'VISITOR_STOP'
    """The visitor returned stop=True at this node."""

    DEAD_END = 'DEAD_END'
    """No unvisited neighbors reachable (exhausted the component)."""

    MAX_DEPTH = 'MAX_DEPTH'
    """Reached max_steps before finding a stop condition."""

    NO_PATH = 'NO_PATH'
    """Start node not found or trace not initiated."""


@dataclass
class GraphPath:
    """A path through the GeometryGraph — pure topology, no business meaning.

    Attributes:
        nodes:  Node IDs along the path (start → stop).
        edges:  Edge IDs traversed (same length as nodes − 1 for a valid path).
        cost:   Sum of edge lengths.
        stop_node:  Node ID where the trace stopped.
        reason: Why the trace ended.
    """

    nodes: list[int] = field(default_factory=list)
    edges: list[int] = field(default_factory=list)
    cost: float = 0.0
    stop_node: int = -1
    reason: TraceStopReason = TraceStopReason.NO_PATH

    @property
    def end_node(self) -> int:
        return self.stop_node if self.stop_node >= 0 else (self.nodes[-1] if self.nodes else -1)
