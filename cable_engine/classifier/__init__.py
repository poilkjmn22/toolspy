"""cable_engine.classifier — multi-signal document classifier.

Replaces the simple keyword-based dispatch in TopologyStage with a
weighted composite of three signals:

  - KeywordClassifier  : text + ATTRIB-tag patterns
  - GeometryClassifier : line ratio, density, blockref ratio
  - LayoutClassifier   : title-block position + text-cluster centroid

Final output is a Classification with:
  - primary  : the best-fit business type
  - confidence : 0..1 (sum of weighted sub-scores)
  - secondary : the runner-up candidates with their scores
  - signals   : raw per-classifier scores for explainability

Business types:
  - circuit_loop        (回路图)
  - terminal_strip      (端子排图)
  - cable_schedule      (电缆清册 / 接线表 / 电缆联系图)
  - protection_diagram  (保护 / 测控信号回路图)
  - panel_layout        (屏位 / 屏柜布置图)
  - monitoring_system   (状态监测 / 通风控制 / SF6 监测)
  - unknown             (目录 / 封面 / 总说明 — no business value)
"""

from .base import (
    BusinessType,
    Classification,
    BaseClassifier,
    ALL_BUSINESS_TYPES,
)
from .keyword import KeywordClassifier
from .geometry import GeometryClassifier
from .layout import LayoutClassifier
from .composite import CompositeClassifier


def classify_document(doc) -> Classification:
    """Convenience entry point: returns CompositeClassifier().classify(doc)."""
    return CompositeClassifier().classify(doc)


__all__ = [
    'BusinessType',
    'Classification',
    'BaseClassifier',
    'ALL_BUSINESS_TYPES',
    'KeywordClassifier',
    'GeometryClassifier',
    'LayoutClassifier',
    'CompositeClassifier',
    'classify_document',
]