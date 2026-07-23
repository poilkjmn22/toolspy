"""cable_engine.layout.semantics.device_type — Device type classification.

Produces ``DeviceAttributes`` (a weak-semantic annotation) for each
DEVICE LayoutNode. The classification is *not* a hard type assignment —
it's a scored hint with evidence, meant to be consumed downstream.

Classification signals (weighted):
  - BlockRef name match (e.g. ``METER_BLOCK`` → METER)
  - Text pattern match (e.g. ``DTZ`` prefix → METER)
  - Positional context (inside a labelled sub-group)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..model import LayoutNode, LayoutNodeType


# Well-known device categories
METER = 'METER'
RELAY = 'RELAY'
TERMINAL = 'TERMINAL'
SWITCH = 'SWITCH'
MODULE = 'MODULE'
UNKNOWN = 'UNKNOWN'


@dataclass
class DeviceAttributes:
    category: str = UNKNOWN
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# BlockRef name → category mapping
# ---------------------------------------------------------------------------
_BLOCK_CATEGORIES: dict[str, str] = {
    'METER_BLOCK': METER,
    'DTZ178_SYMBOL': METER,
    'RELAY_SYMBOL': RELAY,
    'TERMINAL_SYMBOL': TERMINAL,
    'SWITCH_SYMBOL': SWITCH,
    'MODULE_SYMBOL': MODULE,
    'FUSE_BLOCK': MODULE,
    'CABLE_MARKER': TERMINAL,
}

# ---------------------------------------------------------------------------
# Text-prefix → category mapping (all lowercase for matching)
# ---------------------------------------------------------------------------
_TEXT_CATEGORIES: list[tuple[tuple[str, ...], str]] = [
    (('dtz', 'ddz', 'dsz', 'dssd'), METER),           # 电能表型号
    (('wh', 'varh', 'vah'), METER),                     # 电能表单位
    (('dk', 'zdk', 'zdf', 'gz'), RELAY),               # 继电器/控制
    (('xd', 'xdt'), RELAY),                             # 信号继电器
    (('lp', 'zlp'), TERMINAL),                          # 端子排
    (('qd', 'zqd'), SWITCH),                            # 切换开关
    (('fa', 'fu'), MODULE),                             # 熔断器/保险
    (('zk', 'zkz'), SWITCH),                            # 空气开关
    (('ya', 'ha', 'hw'), SWITCH),                       # 按钮/信号灯
    (('1d', '2d', '3d', '4d', '5d', '6d'), TERMINAL),  # 端子编号
    (('m1', 'm2', 'm3', 'm4', 'm5'), METER),           # 铭牌编号
]


class DeviceSemanticResolver:
    """Score-based device type resolver.

    Usage::

        resolver = DeviceSemanticResolver()
        attrs = resolver.resolve(device_node)
        print(attrs.category, attrs.confidence)
    """

    def resolve(
        self,
        node: LayoutNode,
        block_name: str = '',
        parent_name: str = '',
    ) -> DeviceAttributes:
        if node.node_type != LayoutNodeType.DEVICE:
            return DeviceAttributes()

        scores: dict[str, float] = {}
        evidence: list[str] = []

        # Signal 1: BlockRef name match (weight 0.4)
        if block_name:
            cat = _BLOCK_CATEGORIES.get(block_name)
            if cat:
                scores[cat] = scores.get(cat, 0.0) + 0.4
                evidence.append(f'block:{block_name}')

        # Signal 2: Text prefix match (weight 0.3)
        name = (node.name or '').lower()
        if name:
            for prefixes, cat in _TEXT_CATEGORIES:
                for prefix in prefixes:
                    if name.startswith(prefix):
                        scores[cat] = scores.get(cat, 0.0) + 0.3
                        evidence.append(f'text:{node.name}')
                        break

        # Signal 3: Parent context (weight 0.1)
        if parent_name:
            pn = parent_name.lower()
            for prefixes, cat in _TEXT_CATEGORIES:
                for prefix in prefixes:
                    if pn.startswith(prefix):
                        scores[cat] = scores.get(cat, 0.0) + 0.1
                        evidence.append(f'parent:{parent_name}')
                        break

        if not scores:
            return DeviceAttributes()

        best_cat = max(scores, key=scores.get)
        return DeviceAttributes(
            category=best_cat,
            confidence=scores[best_cat],
            evidence=evidence,
        )

    def resolve_tree(
        self,
        root: LayoutNode,
    ) -> None:
        """Walk a LayoutTree and annotate all DEVICE nodes in-place.

        Sets ``node.data['attributes']`` to a DeviceAttributes dict
        (serializable). Non-device nodes are ignored.
        """
        for child in root.children or []:
            if child.node_type == LayoutNodeType.DEVICE:
                parent_name = root.name if root.node_type != LayoutNodeType.CABINET else ''
                attrs = self.resolve(
                    child,
                    block_name=child.data.get('source_block', ''),
                    parent_name=parent_name,
                )
                child.data['attributes'] = {
                    'category': attrs.category,
                    'confidence': attrs.confidence,
                    'evidence': attrs.evidence,
                }
            else:
                self.resolve_tree(child)


def annotate_tree(tree) -> None:
    """Convenience: annotate every root in a LayoutTree in-place."""
    resolver = DeviceSemanticResolver()
    for root in tree.roots:
        resolver.resolve_tree(root)


__all__ = [
    'DeviceAttributes', 'DeviceSemanticResolver',
    'annotate_tree',
    'METER', 'RELAY', 'TERMINAL', 'SWITCH', 'MODULE', 'UNKNOWN',
]
