"""cable_engine.classifier.base — common types for classifiers.

BusinessType is the canonical enum of business categories. Each
constant's `.value` is the lowercase snake-case id used throughout
storage and the viewer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ir import Document


class BusinessType(str, Enum):
    CIRCUIT_LOOP = 'circuit_loop'        # 回路图
    TERMINAL_STRIP = 'terminal_strip'    # 端子排图
    CABLE_SCHEDULE = 'cable_schedule'    # 电缆清册 / 接线表
    PROTECTION_DIAGRAM = 'protection_diagram'  # 保护 / 测控回路
    PANEL_LAYOUT = 'panel_layout'        # 屏位 / 屏柜布置图
    MONITORING_SYSTEM = 'monitoring_system'  # 状态监测 / 通风 / SF6
    UNKNOWN = 'unknown'                  # 目录 / 封面 / 总说明


ALL_BUSINESS_TYPES: tuple[BusinessType, ...] = (
    BusinessType.CIRCUIT_LOOP,
    BusinessType.TERMINAL_STRIP,
    BusinessType.CABLE_SCHEDULE,
    BusinessType.PROTECTION_DIAGRAM,
    BusinessType.PANEL_LAYOUT,
    BusinessType.MONITORING_SYSTEM,
    BusinessType.UNKNOWN,
)


@dataclass
class Classification:
    """Result of classifying a single Document."""
    primary: BusinessType
    confidence: float                     # 0..1
    secondary: list[tuple[BusinessType, float]] = field(default_factory=list)
    signals: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'primary': self.primary.value,
            'confidence': round(self.confidence, 4),
            'secondary': [
                {'type': bt.value, 'score': round(s, 4)}
                for bt, s in self.secondary
            ],
            'signals': {
                k: {kk: round(vv, 4) for kk, vv in v.items()}
                for k, v in self.signals.items()
            },
        }


class BaseClassifier:
    """Subclass-friendly contract for a single classifier.

    `name` is the key used in `Classification.signals`.
    `score(doc)` returns a dict mapping BusinessType -> 0..1 score.
    """

    name: str = 'base'

    def score(self, doc: 'Document') -> dict[BusinessType, float]:
        raise NotImplementedError


__all__ = ['BusinessType', 'Classification', 'BaseClassifier', 'ALL_BUSINESS_TYPES']