"""Tests for P4 SemanticScore — evidence sources and fusion engine."""

from __future__ import annotations

from cable_engine.ir.entities import BBox
from cable_engine.layout.model import LayoutNode, LayoutNodeType, LayoutGroupType
from .evidence import (
    UNKNOWN, TERMINAL_COLUMN, DEVICE_PANEL,
    MODULE_GROUP, METER_GROUP, RELAY_GROUP,
    METER_GRID, RELAY_GRID,
    LayoutShapeEvidence,
    NamePatternEvidence,
    DeviceAttrEvidence,
    TableInfoEvidence,
    default_evidence_sources,
)
from .fusion import SemanticScoreEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _group(group_type=None, children=None, **data) -> LayoutNode:
    node = LayoutNode(
        id='test_group', node_type=LayoutNodeType.GROUP,
        bbox=BBox(0, 0, 100, 100),
        group_type=group_type,
        data=data,
    )
    if children:
        for c in children:
            node.add_child(c)
    return node


def _device(name: str, attr_category: str = '') -> LayoutNode:
    d = LayoutNode(
        id=name, node_type=LayoutNodeType.DEVICE,
        bbox=BBox(0, 0, 10, 10), name=name,
    )
    if attr_category:
        d.data['attributes'] = {'category': attr_category}
    return d


# ---------------------------------------------------------------------------
# LayoutShapeEvidence
# ---------------------------------------------------------------------------

def test_shape_vertical_column():
    source = LayoutShapeEvidence()
    g = _group(group_type=LayoutGroupType.VERTICAL_COLUMN)
    s = source.score(g)
    assert s.get(TERMINAL_COLUMN) == 0.20


def test_shape_horizontal_row():
    source = LayoutShapeEvidence()
    g = _group(group_type=LayoutGroupType.HORIZONTAL_ROW)
    s = source.score(g)
    assert s.get(DEVICE_PANEL) == 0.10


def test_shape_grid():
    source = LayoutShapeEvidence()
    g = _group(group_type=LayoutGroupType.GRID)
    s = source.score(g)
    assert s.get(METER_GRID) == 0.20


def test_shape_freeform():
    source = LayoutShapeEvidence()
    g = _group(group_type=LayoutGroupType.FREEFORM)
    s = source.score(g)
    assert s == {}


def test_shape_no_type():
    source = LayoutShapeEvidence()
    g = _group(group_type=None)
    s = source.score(g)
    assert s == {}


# ---------------------------------------------------------------------------
# NamePatternEvidence
# ---------------------------------------------------------------------------

def test_name_terminal_column():
    source = NamePatternEvidence()
    g = _group(children=[_device('2D'), _device('4D'), _device('6D')])
    s = source.score(g)
    assert TERMINAL_COLUMN in s
    # 3/3 match → ratio 1.0 × weight 0.40
    assert s[TERMINAL_COLUMN] == 0.40


def test_name_meter_group():
    source = NamePatternEvidence()
    g = _group(children=[_device('DTZ178'), _device('DDZ28')])
    s = source.score(g)
    assert METER_GROUP in s
    assert s[METER_GROUP] == 0.30


def test_name_relay_group():
    source = NamePatternEvidence()
    g = _group(children=[_device('DK1'), _device('DK2')])
    s = source.score(g)
    assert RELAY_GROUP in s


def test_name_module_group():
    source = NamePatternEvidence()
    g = _group(children=[_device('FA1'), _device('FU2')])
    s = source.score(g)
    assert MODULE_GROUP in s


def test_name_meter_grid():
    source = NamePatternEvidence()
    g = _group(children=[_device('M1'), _device('M2'), _device('M3')])
    s = source.score(g)
    assert METER_GRID in s


def test_name_no_children():
    source = NamePatternEvidence()
    g = _group()
    s = source.score(g)
    assert s == {}


def test_name_no_match():
    source = NamePatternEvidence()
    g = _group(children=[_device('XYZ_001'), _device('ABC_002')])
    s = source.score(g)
    assert s == {}

# ---------------------------------------------------------------------------
# DeviceAttrEvidence
# ---------------------------------------------------------------------------

def test_attr_terminal():
    source = DeviceAttrEvidence()
    g = _group(children=[
        _device('D1', attr_category='TERMINAL'),
        _device('D2', attr_category='TERMINAL'),
    ])
    s = source.score(g)
    assert s.get(TERMINAL_COLUMN) == 0.20


def test_attr_meter():
    source = DeviceAttrEvidence()
    g = _group(children=[
        _device('M1', attr_category='METER'),
        _device('M2', attr_category='METER'),
    ])
    s = source.score(g)
    assert s.get(METER_GROUP) == 0.20


def test_attr_relay():
    source = DeviceAttrEvidence()
    g = _group(children=[
        _device('R1', attr_category='RELAY'),
        _device('R2', attr_category='RELAY'),
    ])
    s = source.score(g)
    assert s.get(RELAY_GROUP) == 0.20


def test_attr_mixed_below_threshold():
    source = DeviceAttrEvidence()
    g = _group(children=[
        _device('D1', attr_category='TERMINAL'),
        _device('M1', attr_category='METER'),
        _device('X1', attr_category='SWITCH'),
    ])
    s = source.score(g)
    # Each category is 1/3 ≈ 33%, below 50%
    assert s == {}


def test_attr_no_devices():
    source = DeviceAttrEvidence()
    g = _group()
    s = source.score(g)
    assert s == {}


# ---------------------------------------------------------------------------
# TableInfoEvidence
# ---------------------------------------------------------------------------

def test_table_meter_description():
    source = TableInfoEvidence()
    g = _group(table_info={'model': 'DTZ178', 'description': '电能表', 'qty': 2})
    s = source.score(g)
    assert METER_GROUP in s
    # 电能表 keyword → 0.25 + model startswith 'dtz' → 0.15 = 0.40
    assert abs(s[METER_GROUP] - 0.40) < 0.001


def test_table_relay():
    source = TableInfoEvidence()
    g = _group(table_info={'model': 'DK-2000', 'description': '继电器', 'qty': 1})
    s = source.score(g)
    assert RELAY_GROUP in s
    # 继电器 keyword → 0.25 + model startswith 'dk' → 0.15 = 0.40
    assert abs(s[RELAY_GROUP] - 0.40) < 0.001


def test_table_terminal():
    source = TableInfoEvidence()
    g = _group(table_info={'model': '', 'description': '端子排', 'qty': 5})
    s = source.score(g)
    assert TERMINAL_COLUMN in s
    # 端子 keyword → 0.25
    assert abs(s[TERMINAL_COLUMN] - 0.25) < 0.001


def test_table_no_info():
    source = TableInfoEvidence()
    g = _group()
    s = source.score(g)
    assert s == {}


def test_table_empty_info():
    source = TableInfoEvidence()
    g = _group(table_info={})
    s = source.score(g)
    assert s == {}


# ---------------------------------------------------------------------------
# Fusion engine
# ---------------------------------------------------------------------------

def test_fusion_terminal_column():
    """VERTICAL_COLUMN + 2D/4D/6D names → TERMINAL_COLUMN."""
    g = _group(
        group_type=LayoutGroupType.VERTICAL_COLUMN,
        children=[_device('2D'), _device('4D'), _device('6D')],
    )
    engine = SemanticScoreEngine()
    sem = engine.fuse(g)
    assert sem.semantic_type == TERMINAL_COLUMN
    assert sem.confidence >= 0.5


def test_fusion_unknown():
    """No signals → UNKNOWN."""
    g = _group()
    engine = SemanticScoreEngine()
    sem = engine.fuse(g)
    assert sem.semantic_type == UNKNOWN
    assert sem.confidence == 0.0


def test_fusion_non_group():
    engine = SemanticScoreEngine()
    n = LayoutNode(
        id='cab', node_type=LayoutNodeType.CABINET,
        bbox=BBox(0, 0, 100, 100),
    )
    sem = engine.fuse(n)
    assert sem.semantic_type == UNKNOWN


def test_fusion_table_dominates():
    """Table info (0.40) outweighs layout shape (0.20)."""
    g = _group(
        group_type=LayoutGroupType.GRID,
        children=[],
        table_info={'model': 'DTZ178', 'description': '电能表', 'qty': 2},
    )
    engine = SemanticScoreEngine()
    sem = engine.fuse(g)
    assert sem.semantic_type == METER_GROUP
    # Table 0.40 > Grid 0.20
    assert sem.confidence >= 0.39


def test_fusion_multiple_sources_combine():
    """VERTICAL_COLUMN + 2D/4D/6D + device attrs → fused TERMINAL_COLUMN."""
    g = _group(
        group_type=LayoutGroupType.VERTICAL_COLUMN,
        children=[
            _device('2D', attr_category='TERMINAL'),
            _device('4D', attr_category='TERMINAL'),
            _device('6D', attr_category='TERMINAL'),
        ],
    )
    engine = SemanticScoreEngine()
    sem = engine.fuse(g)
    assert sem.semantic_type == TERMINAL_COLUMN
    # Layout 0.20 + Name 0.40 + Attr 0.20 = 0.80
    assert abs(sem.confidence - 0.80) < 0.01


def test_fusion_tree_annotates_in_place():
    """fuse_tree sets group_semantic on GROUP nodes in tree."""
    g1 = _group(
        group_type=LayoutGroupType.VERTICAL_COLUMN,
        children=[_device('2D'), _device('4D')],
    )
    cab = LayoutNode(
        id='cab1', node_type=LayoutNodeType.CABINET,
        bbox=BBox(0, 0, 200, 200), children=[g1],
    )
    engine = SemanticScoreEngine()
    engine.fuse_tree(cab)
    assert 'group_semantic' in g1.data
    assert g1.data['group_semantic']['type'] == TERMINAL_COLUMN


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
