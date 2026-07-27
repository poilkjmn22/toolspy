"""cable_engine.layout.semantics.fusion — Multi-evidence fusion engine.

:class:`SemanticScoreEngine` collects scores from multiple :class:`EvidenceSource`
instances, normalizes, and selects the best-matching semantic type.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import LayoutNode, LayoutNodeType
from .evidence import (
    UNKNOWN, TERMINAL_COLUMN, DEVICE_PANEL,
    MODULE_GROUP, METER_GROUP, RELAY_GROUP,
    METER_GRID, RELAY_GRID,
    EvidenceSource,
    default_evidence_sources,
)


@dataclass
class GroupSemantic:
    semantic_type: str = UNKNOWN
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


class SemanticScoreEngine:
    """Fuse evidence sources into a scored GroupSemantic.

    Usage::

        engine = SemanticScoreEngine()
        sem = engine.fuse(group_node)
        print(sem.semantic_type, sem.confidence, sem.evidence)
    """

    def __init__(self, sources: list[EvidenceSource] | None = None):
        self._sources = sources if sources is not None else default_evidence_sources()

    def fuse(self, node: LayoutNode) -> GroupSemantic:
        if node.node_type != LayoutNodeType.GROUP:
            return GroupSemantic()

        scores: dict[str, float] = {}
        trail: list[str] = []

        for source in self._sources:
            contribs = source.score(node)
            if not contribs:
                trail.append(f'{source.name}: —')
                continue
            for sem_type, score in contribs.items():
                weighted = score * source.weight
                scores[sem_type] = scores.get(sem_type, 0.0) + weighted
            trail.append(f'{source.name}: {_fmt_contribs(contribs)}')

        if not scores:
            return GroupSemantic(UNKNOWN, 0.0, trail)

        best = max(scores, key=scores.get)
        return GroupSemantic(
            semantic_type=best,
            confidence=round(scores[best], 3),
            evidence=trail,
        )

    def fuse_tree(self, root: LayoutNode) -> None:
        """Walk tree and annotate all GROUP nodes in-place.

        Sets ``node.data['group_semantic']`` to a dict with
        ``type``, ``confidence``, ``evidence``.
        """
        for child in root.children or []:
            if child.node_type == LayoutNodeType.GROUP:
                sem = self.fuse(child)
                child.data['group_semantic'] = {
                    'type': sem.semantic_type,
                    'confidence': sem.confidence,
                    'evidence': sem.evidence,
                }
            else:
                self.fuse_tree(child)


def _fmt_contribs(contribs: dict[str, float]) -> str:
    parts = [f'{k}+{v:.2f}' for k, v in sorted(contribs.items(), key=lambda x: -x[1])]
    return ','.join(parts)


__all__ = [
    'GroupSemantic',
    'SemanticScoreEngine',
]
