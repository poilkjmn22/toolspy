"""cable_engine.layout.semantics — Weak-semantic annotation layer.

This package enriches LayoutNodes with *interpreted* (not extracted)
information: device type classification, text association, etc.

The semantics layer is explicitly "weak" — it produces confidence-rated
hints (GroupSemantic / DeviceAttributes) rather than hard type assignments.
Downstream consumers choose whether to trust them.

Architecture (P4):

  SemanticScoreEngine
    ├── LayoutShapeEvidence   — group_type enum
    ├── NamePatternEvidence   — device name prefix matching
    ├── DeviceAttrEvidence    — child node data['attributes']
    ├── TableInfoEvidence     — table_info metadata
    └── SpatialEvidence       — SpatialGraph (stub)

Each EvidenceSource returns {semantic_type: score_contribution}.
The engine fuses via weighted sum, then selects the best type.
"""

from .group_type import (
    GroupSemantic, GroupSemanticResolver, annotate_groups,
    UNKNOWN, TERMINAL_COLUMN, DEVICE_PANEL,
    MODULE_GROUP, METER_GROUP, RELAY_GROUP,
    METER_GRID, RELAY_GRID,
)
from .device_type import (
    DeviceAttributes, DeviceSemanticResolver, annotate_tree,
    METER, RELAY, TERMINAL, SWITCH, MODULE, UNKNOWN as DEV_UNKNOWN,
)
from .evidence import (
    EvidenceSource,
    LayoutShapeEvidence,
    NamePatternEvidence,
    DeviceAttrEvidence,
    TableInfoEvidence,
    SpatialEvidence,
    default_evidence_sources,
)
from .fusion import SemanticScoreEngine

__all__ = [
    'GroupSemantic', 'GroupSemanticResolver', 'annotate_groups',
    'UNKNOWN', 'TERMINAL_COLUMN', 'DEVICE_PANEL',
    'MODULE_GROUP', 'METER_GROUP', 'RELAY_GROUP',
    'METER_GRID', 'RELAY_GRID',
    'DeviceAttributes', 'DeviceSemanticResolver', 'annotate_tree',
    'METER', 'RELAY', 'TERMINAL', 'SWITCH', 'MODULE',
    'EvidenceSource',
    'LayoutShapeEvidence', 'NamePatternEvidence',
    'DeviceAttrEvidence', 'TableInfoEvidence', 'SpatialEvidence',
    'default_evidence_sources',
    'SemanticScoreEngine',
]
