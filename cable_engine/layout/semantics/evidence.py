"""cable_engine.layout.semantics.evidence — Pluggable evidence sources for group semantic scoring.

Each :class:`EvidenceSource` inspects a GROUP LayoutNode and returns
a dict mapping semantic_type → score contribution.  Scores are fused
by :class:`SemanticScoreEngine` (see :mod:`fusion`).

Current sources:
  - LayoutShapeEvidence   — group_type enum (VERTICAL_COLUMN → TERMINAL_COLUMN)
  - NamePatternEvidence   — device name prefix matching
  - DeviceAttrEvidence    — child node data['attributes']['category']
  - TableInfoEvidence     — match_to_devices table_info metadata
  - SpatialEvidence       — SpatialGraph adjacency (stub)
"""

from __future__ import annotations

from typing import Optional

from ..model import LayoutNode, LayoutNodeType, LayoutGroupType

# ---------------------------------------------------------------------------
# Constants — shared with group_type.py
# ---------------------------------------------------------------------------
UNKNOWN = 'UNKNOWN'
TERMINAL_COLUMN = 'TERMINAL_COLUMN'
DEVICE_PANEL = 'DEVICE_PANEL'
MODULE_GROUP = 'MODULE_GROUP'
METER_GROUP = 'METER_GROUP'
RELAY_GROUP = 'RELAY_GROUP'
METER_GRID = 'METER_GRID'
RELAY_GRID = 'RELAY_GRID'

# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class EvidenceSource:
    name: str = ''
    weight: float = 1.0

    def score(self, node: LayoutNode) -> dict[str, float]:
        """Return {semantic_type: score_contribution} for *node*."""
        return {}


# ---------------------------------------------------------------------------
# 1. Layout shape evidence
# ---------------------------------------------------------------------------

_LAYOUT_SIGNALS: dict[LayoutGroupType, list[tuple[str, float]]] = {
    LayoutGroupType.VERTICAL_COLUMN: [(TERMINAL_COLUMN, 0.20)],
    LayoutGroupType.HORIZONTAL_ROW:  [(DEVICE_PANEL, 0.10)],
    LayoutGroupType.GRID:            [(METER_GRID, 0.20)],
    LayoutGroupType.FREEFORM:        [],
}

class LayoutShapeEvidence(EvidenceSource):
    name = 'layout'
    weight = 1.0

    def score(self, node: LayoutNode) -> dict[str, float]:
        if node.group_type is None:
            return {}
        result: dict[str, float] = {}
        for type_name, contrib in _LAYOUT_SIGNALS.get(node.group_type, []):
            result[type_name] = result.get(type_name, 0.0) + contrib
        return result


# ---------------------------------------------------------------------------
# 2. Device name prefix patterns
# ---------------------------------------------------------------------------

_SEMANTIC_PATTERNS: list[tuple[tuple[str, ...], str, float]] = [
    (('1d', '2d', '3d', '4d', '5d', '6d', '7d', '8d', '9d', '10d',
      '11d', '12d', '13d', '14d', '15d', '16d', '17d', '18d', '19d', '20d'),
     TERMINAL_COLUMN, 0.40),
    (('dtz', 'ddz', 'dsz', 'dssd'), METER_GROUP, 0.30),
    (('dk', 'zdk', 'zdf', 'gz', 'xd'), RELAY_GROUP, 0.30),
    (('fa', 'fu'), MODULE_GROUP, 0.20),
    (('m1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7', 'm8'), METER_GRID, 0.30),
    (('dh1', 'dh2', 'dh3', 'dh4', 'dh5', 'dh6'), METER_GRID, 0.20),
]

class NamePatternEvidence(EvidenceSource):
    name = 'name'
    weight = 1.0

    def score(self, node: LayoutNode) -> dict[str, float]:
        child_names = [
            (c.name or '').lower()
            for c in node.children
            if c.node_type == LayoutNodeType.DEVICE and c.name
        ]
        if not child_names:
            return {}
        result: dict[str, float] = {}
        for prefixes, sem_type, weight in _SEMANTIC_PATTERNS:
            match_count = sum(
                1 for name in child_names
                if any(name.startswith(p) for p in prefixes)
            )
            if match_count > 0:
                ratio = match_count / len(child_names)
                result[sem_type] = result.get(sem_type, 0.0) + weight * ratio
        return result


# ---------------------------------------------------------------------------
# 3. Child device attribute evidence
# ---------------------------------------------------------------------------

_ATTR_SIGNALS: dict[str, list[tuple[str, float]]] = {
    'TERMINAL': [(TERMINAL_COLUMN, 0.20)],
    'METER':    [(METER_GROUP, 0.20)],
    'RELAY':    [(RELAY_GROUP, 0.20)],
}

class DeviceAttrEvidence(EvidenceSource):
    name = 'attr'
    weight = 1.0

    def score(self, node: LayoutNode) -> dict[str, float]:
        child_cats: list[str] = []
        for c in node.children:
            if c.node_type != LayoutNodeType.DEVICE:
                continue
            cat = c.data.get('attributes', {}).get('category', '')
            if cat:
                child_cats.append(cat)
        if not child_cats:
            return {}
        result: dict[str, float] = {}
        n = len(child_cats)
        for cat, signals in _ATTR_SIGNALS.items():
            ratio = sum(1 for c in child_cats if c == cat) / n
            for type_name, contrib in signals:
                if ratio >= 0.5:
                    result[type_name] = result.get(type_name, 0.0) + contrib
        return result


# ---------------------------------------------------------------------------
# 4. Table info evidence
# ---------------------------------------------------------------------------

_TABLE_SIGNALS: dict[str, list[tuple[str, float]]] = {
    '电表':  [(METER_GROUP, 0.25)],
    '电能表': [(METER_GROUP, 0.25)],
    '继电器': [(RELAY_GROUP, 0.25)],
    '端子':  [(TERMINAL_COLUMN, 0.25)],
    '开关':  [(DEVICE_PANEL, 0.20)],
}

class TableInfoEvidence(EvidenceSource):
    name = 'table'
    weight = 1.0

    def score(self, node: LayoutNode) -> dict[str, float]:
        ti = node.data.get('table_info')
        if not ti:
            return {}
        result: dict[str, float] = {}
        desc = (ti.get('description', '') or '').strip()
        if desc:
            for keyword, signals in _TABLE_SIGNALS.items():
                if keyword in desc:
                    for type_name, contrib in signals:
                        result[type_name] = result.get(type_name, 0.0) + contrib
        model = (ti.get('model', '') or '').strip().lower()
        if model:
            if model.startswith('dtz'):
                result[METER_GROUP] = result.get(METER_GROUP, 0.0) + 0.15
            elif model.startswith('dk'):
                result[RELAY_GROUP] = result.get(RELAY_GROUP, 0.0) + 0.15
        return result


# ---------------------------------------------------------------------------
# 5. Spatial evidence (stub — ready for SpatialGraph integration)
# ---------------------------------------------------------------------------

class SpatialEvidence(EvidenceSource):
    name = 'spatial'
    weight = 1.0

    def __init__(self, spatial_graph=None):
        self.spatial_graph = spatial_graph

    def score(self, node: LayoutNode) -> dict[str, float]:
        if self.spatial_graph is None:
            return {}
        return {}


# ---------------------------------------------------------------------------
# Default source set
# ---------------------------------------------------------------------------

def default_evidence_sources(spatial_graph=None) -> list[EvidenceSource]:
    return [
        LayoutShapeEvidence(),
        NamePatternEvidence(),
        DeviceAttrEvidence(),
        TableInfoEvidence(),
        SpatialEvidence(spatial_graph=spatial_graph),
    ]


__all__ = [
    'EvidenceSource',
    'LayoutShapeEvidence',
    'NamePatternEvidence',
    'DeviceAttrEvidence',
    'TableInfoEvidence',
    'SpatialEvidence',
    'default_evidence_sources',
    'UNKNOWN', 'TERMINAL_COLUMN', 'DEVICE_PANEL',
    'MODULE_GROUP', 'METER_GROUP', 'RELAY_GROUP',
    'METER_GRID', 'RELAY_GRID',
]
