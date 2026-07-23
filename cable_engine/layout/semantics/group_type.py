"""cable_engine.layout.semantics.group_type — LayoutGroup semantic classification.

Assigns a semantic type (TERMINAL_COLUMN, DEVICE_PANEL, MODULE_GROUP, etc.)
to a GROUP LayoutNode, based on its children's properties.

Score-based, like DeviceSemanticResolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..model import LayoutNode, LayoutNodeType, LayoutGroupType


UNKNOWN = 'UNKNOWN'
TERMINAL_COLUMN = 'TERMINAL_COLUMN'
DEVICE_PANEL = 'DEVICE_PANEL'
MODULE_GROUP = 'MODULE_GROUP'
METER_GROUP = 'METER_GROUP'
RELAY_GROUP = 'RELAY_GROUP'
METER_GRID = 'METER_GRID'
RELAY_GRID = 'RELAY_GRID'


@dataclass
class GroupSemantic:
    semantic_type: str = UNKNOWN
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


# Text-prefix patterns for group semantic inference
# (prefix_tuple, semantic_type, weight)
_SEMANTIC_PATTERNS: list[tuple[tuple[str, ...], str, float]] = [
    # Terminal columns — names like 2D, 4D, 6D, 10D, 12D
    (('1d', '2d', '3d', '4d', '5d', '6d', '7d', '8d', '9d', '10d', '11d', '12d',
      '13d', '14d', '15d', '16d', '17d', '18d', '19d', '20d'), TERMINAL_COLUMN, 0.4),
    # Meter panels — DTZ, DDZ, DSZ prefix
    (('dtz', 'ddz', 'dsz', 'dssd'), METER_GROUP, 0.3),
    # Relay panels — DK, ZDK, ZDF, GZ prefix
    (('dk', 'zdk', 'zdf', 'gz', 'xd'), RELAY_GROUP, 0.3),
    # Module groups — FA, FU prefix
    (('fa', 'fu'), MODULE_GROUP, 0.2),
    # GRID: M + digit — meter grid
    (('m1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7', 'm8'), METER_GRID, 0.3),
    # GRID: DH + digit — terminal grid (D 接线盒/端子盒)
    (('dh1', 'dh2', 'dh3', 'dh4', 'dh5', 'dh6'), METER_GRID, 0.2),
]


class GroupSemanticResolver:
    """Score-based group type resolver.

    Usage::

        resolver = GroupSemanticResolver()
        sem = resolver.resolve(group_node)
        print(sem.semantic_type, sem.confidence)
    """

    def resolve(self, node: LayoutNode) -> GroupSemantic:
        if node.node_type != LayoutNodeType.GROUP:
            return GroupSemantic()

        scores: dict[str, float] = {}
        evidence: list[str] = []

        # Signal 1: Layout pattern type
        if node.group_type == LayoutGroupType.VERTICAL_COLUMN:
            scores[TERMINAL_COLUMN] = scores.get(TERMINAL_COLUMN, 0.0) + 0.2
            evidence.append('layout:VERTICAL_COLUMN')
        elif node.group_type == LayoutGroupType.HORIZONTAL_ROW:
            scores[DEVICE_PANEL] = scores.get(DEVICE_PANEL, 0.0) + 0.1
            evidence.append('layout:HORIZONTAL_ROW')
        elif node.group_type == LayoutGroupType.GRID:
            scores[METER_GRID] = scores.get(METER_GRID, 0.0) + 0.2
            evidence.append('layout:GRID')

        # Signal 2: Children name patterns
        child_names = [
            (c.name or '').lower()
            for c in node.children
            if c.node_type == LayoutNodeType.DEVICE and c.name
        ]
        if child_names:
            for prefixes, sem_type, weight in _SEMANTIC_PATTERNS:
                match_count = sum(
                    1 for name in child_names
                    if any(name.startswith(p) for p in prefixes)
                )
                if match_count > 0:
                    ratio = match_count / len(child_names)
                    contribution = weight * ratio
                    scores[sem_type] = scores.get(sem_type, 0.0) + contribution
                    if ratio >= 0.5:
                        evidence.append(f'{sem_type}:{match_count}/{len(child_names)}')

        # Signal 3: Children device attributes (if already annotated)
        child_cats = [
            c.data.get('attributes', {}).get('category', '')
            for c in node.children
            if c.node_type == LayoutNodeType.DEVICE
        ]
        if child_cats:
            from .device_type import TERMINAL as DEV_TERMINAL
            terminal_ratio = sum(1 for cat in child_cats if cat == DEV_TERMINAL) / len(child_cats)
            meter_ratio = sum(1 for cat in child_cats if cat == 'METER') / len(child_cats)
            relay_ratio = sum(1 for cat in child_cats if cat == 'RELAY') / len(child_cats)
            if terminal_ratio >= 0.5:
                scores[TERMINAL_COLUMN] = scores.get(TERMINAL_COLUMN, 0.0) + 0.2
                evidence.append(f'dev_terminal:{terminal_ratio:.0%}')
            if meter_ratio >= 0.5:
                scores[METER_GROUP] = scores.get(METER_GROUP, 0.0) + 0.2
                evidence.append(f'dev_meter:{meter_ratio:.0%}')
            if relay_ratio >= 0.5:
                scores[RELAY_GROUP] = scores.get(RELAY_GROUP, 0.0) + 0.2
                evidence.append(f'dev_relay:{relay_ratio:.0%}')

        if not scores:
            return GroupSemantic(
                semantic_type=UNKNOWN,
                confidence=0.0,
                evidence=evidence,
            )

        best = max(scores, key=scores.get)
        return GroupSemantic(
            semantic_type=best,
            confidence=round(scores[best], 2),
            evidence=evidence,
        )

    def resolve_tree(self, root: LayoutNode) -> None:
        """Walk tree and annotate all GROUP nodes in-place.

        Sets ``node.data['group_semantic']`` to a dict.
        """
        for child in root.children or []:
            if child.node_type == LayoutNodeType.GROUP:
                sem = self.resolve(child)
                child.data['group_semantic'] = {
                    'type': sem.semantic_type,
                    'confidence': sem.confidence,
                    'evidence': sem.evidence,
                }
            else:
                self.resolve_tree(child)


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
