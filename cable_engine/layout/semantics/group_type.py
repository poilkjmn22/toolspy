"""cable_engine.layout.semantics.group_type — LayoutGroup semantic classification.

Thin convenience layer over :mod:`evidence` + :mod:`fusion`.

Assigns a semantic type (TERMINAL_COLUMN, DEVICE_PANEL, MODULE_GROUP, etc.)
to a GROUP LayoutNode.  Delegates actual scoring to the engine.
"""

from __future__ import annotations

from ..model import LayoutNode, LayoutNodeType
from .evidence import (
    UNKNOWN, TERMINAL_COLUMN, DEVICE_PANEL,
    MODULE_GROUP, METER_GROUP, RELAY_GROUP,
    METER_GRID, RELAY_GRID,
    default_evidence_sources,
)
from .fusion import GroupSemantic, SemanticScoreEngine


class GroupSemanticResolver:
    """Convenience wrapper around :class:`SemanticScoreEngine`.

    Usage::

        resolver = GroupSemanticResolver()
        sem = resolver.resolve(group_node)
        print(sem.semantic_type, sem.confidence)
    """

    def __init__(self, sources=None):
        self._engine = SemanticScoreEngine(
            sources if sources is not None else default_evidence_sources(),
        )

    def resolve(self, node: LayoutNode) -> GroupSemantic:
        return self._engine.fuse(node)

    def resolve_tree(self, root: LayoutNode) -> None:
        self._engine.fuse_tree(root)


def annotate_groups(tree) -> None:
    """Convenience: annotate every GROUP node in a LayoutTree."""
    resolver = GroupSemanticResolver()
    for root in tree.roots:
        resolver.resolve_tree(root)


__all__ = [
    'GroupSemantic', 'GroupSemanticResolver', 'annotate_groups',
    'UNKNOWN', 'TERMINAL_COLUMN', 'DEVICE_PANEL',
    'MODULE_GROUP', 'METER_GROUP', 'RELAY_GROUP',
    'METER_GRID', 'RELAY_GRID',
]
