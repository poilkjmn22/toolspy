"""cable_engine.layout.position.model — 屏位布置图数据模型.

PositionCell:  一个屏位格子（矩形 bbox + F 编号）
PositionRow:   一行屏位格子（Y 聚类结果）
PositionTable: 右侧屏屏用途一览表解析结果
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...ir.entities import BBox


@dataclass
class PositionCell:
    label: str = ''           # "1F", "2F", …
    bbox: Optional[BBox] = None
    row_index: int = -1
    col_index: int = -1
    group_index: int = -1     # 列组编号（同一行中有间距时分组）
    equipment: str = ''       # 从表格关联的设备名称
    qty: int = 0               # 数量
    remark: str = ''           # 备注


@dataclass
class PositionRow:
    cells: list[PositionCell] = field(default_factory=list)
    bbox: Optional[BBox] = None
    row_index: int = -1


@dataclass
class UsageTableRow:
    cell_label: str = ''       # 屏号 (1F)
    equipment: str = ''        # 名称
    qty: int = 0               # 数量
    remark: str = ''           # 备注


@dataclass
class UsageTable:
    bbox: Optional[BBox] = None
    rows: list[UsageTableRow] = field(default_factory=list)


__all__ = ['PositionCell', 'PositionRow', 'UsageTable', 'UsageTableRow']
