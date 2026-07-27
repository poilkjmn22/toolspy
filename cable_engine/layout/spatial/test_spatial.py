"""Tests for SpatialGraph lift and query.

Run: python -m cable_engine.layout.spatial.test_spatial
"""

from __future__ import annotations

from ...ir.entities import BBox
from ..model import LayoutNode, LayoutNodeType, LayoutGroupType, LayoutTree
from .bridge import lift
from .model import SpatialGraph, SpatialRelation


def _c_node(name: str, x: float, y: float, w: float, h: float,
            nid: str = '') -> LayoutNode:
    return LayoutNode(
        id=nid or f'cab_{name}',
        node_type=LayoutNodeType.CABINET,
        bbox=BBox(x, y, w, h),
        name=name,
    )


def _d_node(name: str, x: float, y: float, w: float, h: float,
            parent: LayoutNode, nid: str = '') -> LayoutNode:
    n = LayoutNode(
        id=nid or f'dev_{name}',
        node_type=LayoutNodeType.DEVICE,
        bbox=BBox(x, y, w, h),
        name=name,
    )
    parent.add_child(n)
    return n


def _g_node(name: str, x: float, y: float, w: float, h: float,
            parent: LayoutNode, nid: str = '') -> LayoutNode:
    n = LayoutNode(
        id=nid or f'grp_{name}',
        node_type=LayoutNodeType.GROUP,
        group_type=LayoutGroupType.VERTICAL_COLUMN,
        bbox=BBox(x, y, w, h),
        name=name,
    )
    parent.add_child(n)
    return n


def test_lift_contains_edges():
    """Parent-child containment is lifted as CONTAINS edges."""
    tree = LayoutTree()
    cab = _c_node('Cabinet_A', 0, 0, 200, 300, 'cab_a')
    _d_node('M1', 10, -30, 20, 10, cab, 'd1')
    _d_node('M2', 10, -60, 20, 10, cab, 'd2')
    tree.add_root(cab)

    graph = lift(tree)
    contains = [(e.source_id, e.target_id) for e in graph.edges
                if e.relation == SpatialRelation.CONTAINS]
    assert len(contains) == 2, f'expected 2 CONTAINS edges, got {len(contains)}'
    assert ('cab_a', 'd1') in contains
    assert ('cab_a', 'd2') in contains
    print('  ✓ test_lift_contains_edges')


def test_sibling_left_right():
    """Side-by-side siblings get LEFT_OF / RIGHT_OF."""
    tree = LayoutTree()
    cab = _c_node('Cab', 0, 0, 300, 200, 'cab')
    col1 = _g_node('Col1', 10, -10, 30, 100, cab, 'g1')
    _d_node('A', 15, -20, 10, 10, col1, 'd_a')
    col2 = _g_node('Col2', 100, -10, 30, 100, cab, 'g2')
    _d_node('B', 105, -20, 10, 10, col2, 'd_b')

    tree.add_root(cab)
    graph = lift(tree)

    left_of = [(e.source_id, e.target_id) for e in graph.edges
               if e.relation == SpatialRelation.LEFT_OF]
    right_of = [(e.source_id, e.target_id) for e in graph.edges
                if e.relation == SpatialRelation.RIGHT_OF]

    assert len(left_of) + len(right_of) >= 1, 'expected directional edges'
    print(f'  LEFT_OF: {left_of}, RIGHT_OF: {right_of}')
    print('  ✓ test_sibling_left_right')


def test_device_alignment_vertical():
    """Devices stacked vertically in same column → ALIGNED_VERT."""
    tree = LayoutTree()
    cab = _c_node('Cab', 0, 0, 100, 200, 'cab')
    _d_node('T1', 10, -30, 15, 8, cab, 't1')
    _d_node('T2', 10, -60, 15, 8, cab, 't2')
    _d_node('T3', 10, -90, 15, 8, cab, 't3')
    tree.add_root(cab)

    graph = lift(tree)
    aligned = [(e.source_id, e.target_id) for e in graph.edges
               if e.relation == SpatialRelation.ALIGNED_VERT]
    assert len(aligned) >= 2, f'expected ≥2 ALIGNED_VERT edges, got {len(aligned)}'
    print(f'  ALIGNED_VERT: {aligned}')
    print('  ✓ test_device_alignment_vertical')


def test_device_alignment_horizontal():
    """Devices in same row → ALIGNED_HORZ."""
    tree = LayoutTree()
    cab = _c_node('Cab', 0, 0, 200, 100, 'cab')
    _d_node('M1', 10, -20, 15, 8, cab, 'm1')
    _d_node('M2', 60, -20, 15, 8, cab, 'm2')
    _d_node('M3', 110, -20, 15, 8, cab, 'm3')
    tree.add_root(cab)

    graph = lift(tree)
    aligned = [(e.source_id, e.target_id) for e in graph.edges
               if e.relation == SpatialRelation.ALIGNED_HORZ]
    assert len(aligned) >= 2, f'expected ≥2 ALIGNED_HORZ edges, got {len(aligned)}'
    print(f'  ALIGNED_HORZ: {aligned}')
    print('  ✓ test_device_alignment_horizontal')


def test_device_near():
    """Close but unaligned devices → NEAR."""
    tree = LayoutTree()
    cab = _c_node('Cab', 0, 0, 100, 100, 'cab')
    _d_node('A', 10, -10, 10, 10, cab, 'a')
    _d_node('B', 30, -35, 10, 10, cab, 'b')  # diagonal, ~32u centroid dist < 40
    tree.add_root(cab)

    graph = lift(tree)
    near = [(e.source_id, e.target_id) for e in graph.edges
            if e.relation == SpatialRelation.NEAR]
    assert len(near) >= 1, f'expected ≥1 NEAR edge, got {len(near)}'
    print(f'  NEAR: {near}')
    print('  ✓ test_device_near')


def test_query_near():
    """query_near returns nodes within radius."""
    graph = SpatialGraph()
    graph.add_node(_mk_node('a', BBox(0, 0, 10, 10)))
    graph.add_node(_mk_node('b', BBox(20, 20, 10, 10)))
    graph.add_node(_mk_node('c', BBox(100, 100, 10, 10)))
    found = graph.query_near((5, 5), 30)
    assert len(found) == 2, f'expected 2 near (5,5), got {len(found)}'
    fids = {n.node_id for n in found}
    assert 'a' in fids and 'b' in fids
    print('  ✓ test_query_near')


def test_query_bbox():
    """query_bbox returns overlapping nodes."""
    graph = SpatialGraph()
    graph.add_node(_mk_node('a', BBox(0, 0, 50, 50)))
    graph.add_node(_mk_node('b', BBox(100, 100, 50, 50)))
    found = graph.query_bbox(BBox(20, 20, 30, 30))
    assert len(found) == 1
    assert found[0].node_id == 'a'
    print('  ✓ test_query_bbox')


def test_empty_tree():
    """Empty LayoutTree produces empty SpatialGraph."""
    tree = LayoutTree()
    graph = lift(tree)
    assert len(graph.nodes) == 0
    assert len(graph.edges) == 0
    print('  ✓ test_empty_tree')


def _mk_node(nid: str, bbox: BBox) -> ...:
    from .model import SpatialNode
    return SpatialNode(node_id=nid, node_type='DEVICE', bbox=bbox, name=nid)


if __name__ == '__main__':
    test_lift_contains_edges()
    test_sibling_left_right()
    test_device_alignment_vertical()
    test_device_alignment_horizontal()
    test_device_near()
    test_query_near()
    test_query_bbox()
    test_empty_tree()
    print('\nAll spatial tests passed!')
