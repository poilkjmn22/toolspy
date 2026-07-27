"""cable_engine.layout.model — LayoutTree node types.

LayoutTree is a pure spatial-containment tree, parallel to GeometryGraph.
It describes where things are and what contains what.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..ir.entities import BBox


class LayoutNodeType(Enum):
    SHEET = 'SHEET'
    CABINET = 'CABINET'
    REGION = 'REGION'
    PANEL_AREA = 'PANEL_AREA'
    DEVICE = 'DEVICE'
    GROUP = 'GROUP'
    TEXT_BLOCK = 'TEXT_BLOCK'
    TABLE = 'TABLE'
    TITLE_BLOCK = 'TITLE_BLOCK'


class LayoutGroupType(Enum):
    """Spatial arrangement pattern of a GROUP node."""
    VERTICAL_COLUMN = 'VERTICAL_COLUMN'
    HORIZONTAL_ROW = 'HORIZONTAL_ROW'
    GRID = 'GRID'
    FREEFORM = 'FREEFORM'


@dataclass
class LayoutNode:
    id: str
    node_type: LayoutNodeType
    bbox: BBox
    name: str = ''
    children: list[LayoutNode] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    parent: Optional[LayoutNode] = None
    group_type: Optional[LayoutGroupType] = None

    def add_child(self, child: LayoutNode) -> None:
        child.parent = self
        self.children.append(child)

    def dump(self, indent: int = 0) -> str:
        pfx = '  ' * indent
        b = self.bbox
        extra = ''
        if self.group_type:
            extra += f' [{self.group_type.value}]'
        if self.data:
            extra += f' data={self.data}'
        lines = [f'{pfx}{self.node_type.value} "{self.name}" ({b.x:.0f},{b.y:.0f}) {b.w:.0f}x{b.h:.0f}{extra}']
        for c in self.children:
            lines.append(c.dump(indent + 1))
        return '\n'.join(lines)


@dataclass
class LayoutTree:
    roots: list[LayoutNode] = field(default_factory=list)
    _by_id: dict[str, LayoutNode] = field(default_factory=dict)

    def add_root(self, node: LayoutNode) -> None:
        self.roots.append(node)
        self._index(node)

    def _index(self, node: LayoutNode) -> None:
        self._by_id[node.id] = node
        for c in node.children:
            self._index(c)

    def find(self, node_id: str) -> Optional[LayoutNode]:
        return self._by_id.get(node_id)

    def find_by_name(self, name: str) -> list[LayoutNode]:
        result: list[LayoutNode] = []
        for n in self._by_id.values():
            if n.name == name:
                result.append(n)
        return result

    def dump(self) -> str:
        parts = [f'LayoutTree ({len(self._by_id)} nodes)']
        for r in self.roots:
            parts.append(r.dump(1))
        return '\n'.join(parts)


__all__ = [
    'LayoutNode', 'LayoutNodeType', 'LayoutTree',
    'LayoutGroupType',
]
