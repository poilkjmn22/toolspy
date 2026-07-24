"""Tests for the LayoutTree detector using synthetic Document IR.

Run: python -m cable_engine.layout.test_detector
"""

from __future__ import annotations

from pathlib import Path

from ..ir import BBox, Document, DocumentType
from ..ir.geometry import BlockRef, LineGeometry
from ..ir.entities import Point, TextEntity
from .detector import (
    build_layout_tree,
    detect_cabinets,
    detect_long_lines,
    detect_rectangles,
)
from .model import LayoutNode, LayoutNodeType, LayoutGroupType
from .grouping import DeviceSpatialGraph, detect_layout_groups
from .grouping.clustering import (
    _detect_grids, _detect_columns, _detect_rows,
    _connected_components,
)
from .candidate import DeviceCandidate, SymbolCandidate
from .associator import TextAssociator
from .clustering import DBSCANClusterer


def _line_geom(pts: list[Point], id_: str = '') -> LineGeometry:
    ent = LineGeometry(
        id=id_, source='dwg', page=1, confidence=1.0,
        handle=id_, points=pts,
    )
    ent.custom_fields = {}
    return ent


def _text(text: str, x: float, y: float, id_: str = '') -> TextEntity:
    ent = TextEntity(
        id=id_, source='dwg', page=1, confidence=1.0, text=text,
    )
    ent.custom_fields = {'x': x, 'y': y}
    return ent


def _block_ref(name: str, x: float, y: float, id_: str = '') -> BlockRef:
    return BlockRef(
        id=id_, source='dwg', page=1, confidence=1.0,
        handle=id_, name=name, insert_point=Point(x, y),
    )


def _device_node(name: str, x: float, y: float, w: float, h: float,
                 did: str = '') -> LayoutNode:
    return LayoutNode(
        id=did or f'dev_{name}',
        node_type=LayoutNodeType.DEVICE,
        bbox=BBox(x, y, w, h),
        name=name,
    )


def test_rectangle_from_polyline():
    doc = Document(document_path=Path('/fake'), content_hash='test1', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([
        Point(0, 0), Point(100, 0), Point(100, 50), Point(0, 50), Point(0, 0),
    ], 'h1'))
    rects = detect_rectangles(doc)
    assert len(rects) == 1, f'expected 1 rect, got {len(rects)}'
    r = rects[0]
    assert r.bbox.x == 0 and r.bbox.y == 0
    assert r.bbox.w == 100 and r.bbox.h == 50
    assert r.source_type == 'polyline'
    print('  ✓ test_rectangle_from_polyline')


def test_rectangle_from_4seg():
    doc = Document(document_path=Path('/fake'), content_hash='test2', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(0, 0), Point(100, 0)], 's1'))
    doc.add_entity(_line_geom([Point(100, 0), Point(100, 50)], 's2'))
    doc.add_entity(_line_geom([Point(100, 50), Point(0, 50)], 's3'))
    doc.add_entity(_line_geom([Point(0, 50), Point(0, 0)], 's4'))
    rects = detect_rectangles(doc)
    assert len(rects) == 1, f'expected 1 rect, got {len(rects)}'
    r = rects[0]
    assert r.bbox.x == 0 and r.bbox.y == 0
    assert r.bbox.w == 100 and r.bbox.h == 50
    assert r.source_type == '4seg'
    print('  ✓ test_rectangle_from_4seg')


def test_cabinet_from_paired_verticals():
    """Cabinet formed by 2 long vertical lines (no top/bottom)."""
    doc = Document(document_path=Path('/fake'), content_hash='test_cab', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(-166, -107), Point(-166, 200)], 'v1'))
    doc.add_entity(_line_geom([Point(12, -107), Point(12, 200)], 'v2'))
    doc.add_entity(_text('保护屏 220kV线路A', -77, 210, 't1'))

    rects = detect_rectangles(doc)
    assert len(rects) == 0

    verts, hors = detect_long_lines(doc, min_length=50.0)
    assert len(verts) == 2, f'expected 2 verticals, got {len(verts)}'
    assert verts[0].length == 307

    cabinets = detect_cabinets(doc, rects, verts, hors)
    assert len(cabinets) == 1, f'expected 1 cabinet, got {len(cabinets)}'
    cab = cabinets[0]
    print(f'  Cabinet: {cab.name} bbox=({cab.bbox.x:.0f},{cab.bbox.y:.0f}) '
          f'{cab.bbox.w:.0f}x{cab.bbox.h:.0f}')
    assert cab.bbox.w == 178 and cab.bbox.h == 307
    print('  ✓ test_cabinet_from_paired_verticals')


def test_areas_from_horizontal_dividers():
    """Cabinet with horizontal dividers → panel areas (thin areas filtered)."""
    doc = Document(document_path=Path('/fake'), content_hash='test_area', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(-166, -107), Point(-166, 200)], 'v1'))
    doc.add_entity(_line_geom([Point(12, -107), Point(12, 200)], 'v2'))
    doc.add_entity(_line_geom([Point(-166, 60), Point(12, 60)], 'h1'))
    doc.add_entity(_line_geom([Point(-166, 10), Point(12, 10)], 'h2'))

    tree = build_layout_tree(doc)
    cab = tree.roots[0]
    print(f'  Cabinets: {len(tree.roots)}, areas: {len(cab.children)}')
    assert len(cab.children) == 3, f'expected 3 areas, got {len(cab.children)}'
    for area in cab.children:
        assert area.node_type == LayoutNodeType.PANEL_AREA
        print(f'    Area {area.id}: y={area.bbox.y:.0f} h={area.bbox.h:.0f}')
    print('  ✓ test_areas_from_horizontal_dividers')


def test_devices_in_cabinet():
    """Devices (small rectangles) inside a cabinet."""
    doc = Document(document_path=Path('/fake'), content_hash='test_dev', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(-166, -107), Point(-166, 200)], 'v1'))
    doc.add_entity(_line_geom([Point(12, -107), Point(12, 200)], 'v2'))
    doc.add_entity(_line_geom([Point(-166, 40), Point(12, 40)], 'h1'))

    doc.add_entity(_line_geom([
        Point(-155, 80), Point(-138, 80),
        Point(-138, 108), Point(-155, 108), Point(-155, 80),
    ], 'dev1'))
    doc.add_entity(_line_geom([
        Point(-130, 80), Point(-113, 80),
        Point(-113, 108), Point(-130, 108), Point(-130, 80),
    ], 'dev2'))

    doc.add_entity(_text('M1', -146, 95, 'tm1'))
    doc.add_entity(_text('DK1', -122, 95, 'tdk1'))

    tree = build_layout_tree(doc)
    cab = tree.roots[0]
    print(f'  Cabinet children ({len(cab.children)}):')
    for c in cab.children:
        print(f'    {c.node_type.value} "{c.name}" ({len(c.children)} children)')
        for cc in c.children or []:
            print(f'      {cc.node_type.value} "{cc.name}"')
    assert len(cab.children) == 2, f'expected 2 areas, got {len(cab.children)}'
    area_top = cab.children[1]
    print(f'  Top area: {area_top.bbox.h:.0f}u, children: {len(area_top.children)}')
    # V8.1: 2 devices clustered into 1 GROUP node
    assert len(area_top.children) == 1
    group = area_top.children[0]
    assert group.node_type == LayoutNodeType.GROUP
    assert len(group.children) == 2
    for dev in group.children:
        assert dev.node_type == LayoutNodeType.DEVICE
        print(f'    Device "{dev.name}" ({dev.bbox.w:.0f}x{dev.bbox.h:.0f})')
    print('  ✓ test_devices_in_cabinet')


def test_device_with_blockref():
    """Device as BlockRef with anonymous block + expanded geometry."""
    doc = Document(document_path=Path('/fake'), content_hash='test_br', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(-166, -107), Point(-166, 200)], 'v1'))
    doc.add_entity(_line_geom([Point(12, -107), Point(12, 200)], 'v2'))
    doc.add_entity(_line_geom([
        Point(-155, 80), Point(-138, 80),
        Point(-138, 108), Point(-155, 108), Point(-155, 80),
    ], 'dev_frame'))
    doc.add_entity(_block_ref('A$C49E661ED', -146, 94, 'br1'))

    tree = build_layout_tree(doc)
    assert len(tree.roots) == 1
    cab = tree.roots[0]
    print(f'  Cabinet children: {len(cab.children)}')
    for child in cab.children:
        if child.node_type == LayoutNodeType.DEVICE:
            print(f'    Device "{child.name}" src={child.data.get("source")}')
    assert len(cab.children) >= 1
    assert any(c.node_type == LayoutNodeType.DEVICE for c in cab.children)
    print('  ✓ test_device_with_blockref')


def test_empty_doc():
    """Empty document → empty tree."""
    doc = Document(document_path=Path('/fake'), content_hash='empty', document_type=DocumentType.DWG)
    tree = build_layout_tree(doc)
    assert len(tree.roots) == 0
    print('  ✓ test_empty_doc')


# ---------------------------------------------------------------------------
# Grouping tests
# ---------------------------------------------------------------------------


def test_text_device_detection():
    """Text-only DEVICE nodes created when no rectangle surrounds them."""
    doc = Document(document_path=Path('/fake'), content_hash='test_txt', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(-166, -107), Point(-166, 200)], 'v1'))
    doc.add_entity(_line_geom([Point(12, -107), Point(12, 200)], 'v2'))
    doc.add_entity(_text('DH1', -150, 180, 'tdh1'))
    doc.add_entity(_text('DH2', -150, 165, 'tdh2'))
    doc.add_entity(_text('端子排', -80, 100, 'ttag'))  # skipped: >20 chars? No, "端子排" = 3 chars

    tree = build_layout_tree(doc)
    assert len(tree.roots) == 1
    cab = tree.roots[0]
    print(f'  Cabinet children: {len(cab.children)}')

    def find_devices(node):
        result = []
        if node.node_type == LayoutNodeType.DEVICE:
            result.append(node.name)
        for c in node.children or []:
            result.extend(find_devices(c))
        return result

    dev_names = find_devices(cab)
    print(f'  Device names: {dev_names}')
    assert 'DH1' in dev_names, f'DH1 not found in {dev_names}'
    assert 'DH2' in dev_names, f'DH2 not found in {dev_names}'
    print('  ✓ test_text_device_detection')


def test_grid_detection():
    """4 devices in 2×2 grid (M1, M2, DH1, DH2) → GRID group."""
    devs = [
        _device_node('M1', 10, 100, 20, 15, 'm1'),
        _device_node('M2', 60, 100, 20, 15, 'm2'),
        _device_node('DH1', 10, 60, 20, 15, 'dh1'),
        _device_node('DH2', 60, 60, 20, 15, 'dh2'),
    ]
    cab = BBox(0, 0, 200, 300)
    used: set[str] = set()
    grids = _detect_grids(devs, cab, used)
    assert len(grids) == 1, f'expected 1 grid, got {len(grids)}'
    grid = grids[0]
    assert grid.group_type == LayoutGroupType.GRID
    assert len(grid.children) == 4
    assert grid.data['grid_dims'] == {'cols': 2, 'rows': 2}
    print(f'  GRID: dims={grid.data["grid_dims"]}, score={grid.data["score"]}')
    print('  ✓ test_grid_detection')


def test_horizontal_row_detection():
    """Devices in a horizontal row at the top → HORIZONTAL_ROW group."""
    cab_bbox = BBox(0, 0, 200, 300)
    devs = [
        _device_node('DK1', 10, 280, 30, 15, 'dk1'),
        _device_node('DK2', 50, 280, 30, 15, 'dk2'),
        _device_node('DK4', 90, 280, 30, 15, 'dk4'),
        _device_node('DK3', 130, 280, 30, 15, 'dk3'),
        _device_node('ZDK', 170, 280, 30, 15, 'zdk'),
        _device_node('ZDF', 210, 280, 30, 15, 'zdf'),
    ]
    used: set[str] = set()
    rows = _detect_rows(devs, cab_bbox, used)
    assert len(rows) == 1, f'expected 1 row, got {len(rows)}'
    row = rows[0]
    assert row.group_type == LayoutGroupType.HORIZONTAL_ROW
    assert len(row.children) == 6
    assert 'top' in row.data.get('position', ''), f'position={row.data.get("position")}'
    print(f'  Row: score={row.data["score"]}, pos={row.data["position"]}')
    print('  ✓ test_horizontal_row_detection')


def test_gap_splitting_excludes_distant_device():
    """GZ11 far from 1D-5D column → split into separate groups (or GZ11 excluded)."""
    cab_bbox = BBox(0, 0, 200, 300)
    # Tight column of 5 terminals
    devs = [
        _device_node('1D', 160, 200, 20, 15, 'd1'),
        _device_node('3D', 160, 170, 20, 15, 'd3'),
        _device_node('5D', 160, 140, 20, 15, 'd5'),
        _device_node('GZ11', 160, 40, 25, 20, 'gz'),  # far below, gap > 40
    ]
    used: set[str] = set()
    cols = _detect_columns(devs, cab_bbox, used)
    assert len(cols) == 1, f'expected 1 column (1D-5D), got {len(cols)}'
    col = cols[0]
    names = [c.name for c in col.children]
    assert 'GZ11' not in names, f'GZ11 should be excluded, children={names}'
    assert len(col.children) == 3
    print(f'  Column children: {names}')
    print('  ✓ test_gap_splitting_excludes_distant_device')


def test_device_spatial_graph():
    """DeviceSpatialGraph indexes devices by centroid."""
    devs = [
        _device_node('A', 10, 100, 20, 10, 'da'),
        _device_node('B', 10, 80, 20, 10, 'db'),
        _device_node('C', 10, 60, 20, 10, 'dc'),
    ]
    g = DeviceSpatialGraph(devs, cell_size=50)
    assert len(g.devices) == 3
    assert g.center(devs[0]) == (20, 105)
    print('  ✓ test_device_spatial_graph')


def test_vertical_column_detection():
    """6 devices in a vertical column (x-aligned, evenly spaced)."""
    devs = [
        _device_node('2D', 10, 180, 20, 10, 'd2'),
        _device_node('4D', 10, 150, 20, 10, 'd4'),
        _device_node('6D', 10, 120, 20, 10, 'd6'),
        _device_node('8D', 10, 90, 20, 10, 'd8'),
        _device_node('10D', 10, 60, 20, 10, 'd10'),
        _device_node('12D', 10, 30, 20, 10, 'd12'),
    ]
    cab = BBox(0, 0, 200, 300)
    used: set[str] = set()
    cols = _detect_columns(devs, cab, used)
    assert len(cols) == 1, f'expected 1 column, got {len(cols)}'
    col = cols[0]
    assert col.node_type == LayoutNodeType.GROUP
    assert col.group_type == LayoutGroupType.VERTICAL_COLUMN
    assert len(col.children) == 6
    assert col.data['score'] >= 0.40
    print(f'  Column: score={col.data["score"]}, evidence={col.data["evidence"]}')
    print('  ✓ test_vertical_column_detection')


def test_no_group_for_scattered_devices():
    """Scattered devices with different x positions → no group."""
    devs = [
        _device_node('A', 10, 100, 20, 10, 'da'),
        _device_node('B', 50, 80, 20, 10, 'db'),
        _device_node('C', 90, 60, 20, 10, 'dc'),
    ]
    cab = BBox(0, 0, 200, 300)
    used: set[str] = set()
    cols = _detect_columns(devs, cab, used)
    assert len(cols) == 0, f'expected 0 columns, got {len(cols)}'
    print('  ✓ test_no_group_for_scattered_devices')


def test_grouping_integration():
    """Full pipeline: cabinet + devices → LayoutGroup nodes in tree."""
    doc = Document(document_path=Path('/fake'), content_hash='test_grp', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(-166, -107), Point(-166, 200)], 'v1'))
    doc.add_entity(_line_geom([Point(12, -107), Point(12, 200)], 'v2'))
    doc.add_entity(_line_geom([Point(-166, 40), Point(12, 40)], 'h1'))

    # 6 terminals in a column + 1 standalone device
    for i, y in enumerate([180, 155, 130, 105, 80, 55]):
        tag = f'{2 * (i + 1)}D'
        did = f'dev_{tag}'
        doc.add_entity(_line_geom([
            Point(-155, y), Point(-135, y),
            Point(-135, y + 18), Point(-155, y + 18), Point(-155, y),
        ], did))
        doc.add_entity(_text(tag, -145, y + 5, f't_{tag}'))

    # GZ11 standalone
    doc.add_entity(_line_geom([
        Point(-100, 10), Point(-80, 10),
        Point(-80, 35), Point(-100, 35), Point(-100, 10),
    ], 'dev_gz'))
    doc.add_entity(_text('GZ11', -90, 18, 't_gz'))

    tree = build_layout_tree(doc)
    assert len(tree.roots) == 1
    cab = tree.roots[0]
    print(f'  Cabinet children: {len(cab.children)}')
    for c in cab.children:
        print(f'    {c.node_type.value} "{c.name}" ({len(c.children)} children)')
        for cc in c.children or []:
            print(f'      {cc.node_type.value} "{cc.name}" ({len(cc.children)} children)')

    # Find a GROUP somewhere in the tree
    def find_groups(node: LayoutNode) -> list[LayoutNode]:
        result = []
        if node.node_type == LayoutNodeType.GROUP:
            result.append(node)
        for c in node.children or []:
            result.extend(find_groups(c))
        return result

    groups = find_groups(cab)
    print(f'  Groups found: {len(groups)}')
    for g in groups:
        print(f'    GROUP [{g.group_type}] score={g.data.get("score")} '
              f'children={len(g.children)} '
              f'semantic={g.data.get("group_semantic", {})}')
    assert len(groups) == 1, f'expected 1 GROUP node, got {len(groups)}'
    col = groups[0]
    assert col.group_type == LayoutGroupType.VERTICAL_COLUMN
    assert len(col.children) == 6
    # Check group is under PANEL_AREA, not CABINET
    assert col.parent is not None
    assert col.parent.node_type == LayoutNodeType.PANEL_AREA
    # Check semantic annotation
    sem = col.data.get('group_semantic', {})
    assert sem.get('type') == 'TERMINAL_COLUMN', f'expected TERMINAL_COLUMN, got {sem}'
    print('  ✓ test_grouping_integration')


# ---------------------------------------------------------------------------
# V8.1 Candidate + DBSCAN tests
# ---------------------------------------------------------------------------


def test_closed_rect_candidate():
    """Closed rectangle → DeviceCandidate(0.95)."""
    from .candidate import detect_closed_rects
    doc = Document(document_path=Path('/fake'), content_hash='cr1', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(10, 10), Point(50, 10), Point(50, 30), Point(10, 30), Point(10, 10)], 'r1'))
    container = BBox(0, 0, 200, 300)
    cands = detect_closed_rects(doc, container)
    assert len(cands) == 1
    assert cands[0].score == 0.95
    assert cands[0].source == 'closed_rect'
    assert cands[0].bbox.w == 40 and cands[0].bbox.h == 20
    print('  ✓ test_closed_rect_candidate')


def test_L_shape_candidate():
    """L-shape (2 segments, 90°) → DeviceCandidate(0.5)."""
    from .candidate import detect_open_shapes
    doc = Document(document_path=Path('/fake'), content_hash='ls1', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(10, 10), Point(50, 10)], 's1'))
    doc.add_entity(_line_geom([Point(50, 10), Point(50, 40)], 's2'))
    container = BBox(0, 0, 200, 300)
    cands = detect_open_shapes(doc, container)
    assert len(cands) == 1, f'expected 1 L-shape, got {len(cands)}'
    assert cands[0].score == 0.5
    assert cands[0].source == 'L_shape'
    print('  ✓ test_L_shape_candidate')


def test_U_shape_candidate():
    """U-shape (3 segments, parallel ends) → DeviceCandidate(0.7)."""
    from .candidate import detect_open_shapes
    doc = Document(document_path=Path('/fake'), content_hash='us1', document_type=DocumentType.DWG)
    doc.add_entity(_line_geom([Point(10, 10), Point(10, 40)], 's1'))
    doc.add_entity(_line_geom([Point(10, 10), Point(40, 10)], 's2'))
    doc.add_entity(_line_geom([Point(40, 10), Point(40, 40)], 's3'))
    container = BBox(0, 0, 200, 300)
    cands = detect_open_shapes(doc, container)
    assert len(cands) == 1, f'expected 1 U-shape, got {len(cands)}'
    assert cands[0].score == 0.7
    assert cands[0].source == 'U_shape'
    print(f'  U-shape bbox: {cands[0].bbox}')
    print('  ✓ test_U_shape_candidate')


def test_circle_symbol_candidate():
    """Circle with text inside → SymbolCandidate(0.60)."""
    from .candidate import detect_circle_symbols
    from ..ir import CircleGeometry
    doc = Document(document_path=Path('/fake'), content_hash='cir1', document_type=DocumentType.DWG)
    doc.add_entity(CircleGeometry(
        id='cir1', source='dwg', page=1, confidence=1.0,
        handle='cir1', center=Point(50, 50), radius=8,
    ))
    doc.add_entity(_text('GZ11', 50, 50, 'tgz'))
    container = BBox(0, 0, 200, 300)
    syms = detect_circle_symbols(doc, container)
    assert len(syms) == 1, f'expected 1 symbol, got {len(syms)}'
    assert syms[0].score == 0.60
    assert syms[0].texts[0][0] == 'GZ11'
    print('  ✓ test_circle_symbol_candidate')


def test_candidate_pool_dedup():
    """Overlapping candidates → high-score wins; Symbol→Device promotion."""
    from .candidate import CandidatePool
    pool = CandidatePool()
    pool.add_device(DeviceCandidate(id='a', bbox=BBox(0, 0, 50, 20), score=0.95, source='closed_rect'))
    pool.add_device(DeviceCandidate(id='b', bbox=BBox(5, 2, 40, 16), score=0.50, source='L_shape'))
    pool.add_symbol(SymbolCandidate(id='c', bbox=BBox(100, 100, 20, 20),
                                     center=Point(110, 110), radius=10, score=0.60))
    result = pool.dedup()
    # 'b' overlaps 'a' → dropped; 'c' is standalone → promoted to Device
    assert len(result) == 2, f'expected 2 after dedup, got {len(result)}'
    ids = [d.id for d in result]
    assert 'a' in ids
    assert 'b' not in ids  # dropped
    assert 'c' in ids       # promoted from Symbol
    print('  ✓ test_candidate_pool_dedup')


def test_text_associator_name_description():
    """Text inside candidate bbox → name (topmost) + description (rest)."""
    from .associator import TextAssociator
    cand = DeviceCandidate(id='d1', bbox=BBox(100, 100, 50, 40))
    texts = [
        (100, 130, 'DTZ178'),    # y=130 → description
        (120, 140, 'M1'),         # y=140 → name (topmost)
        (110, 120, '张北I线'),     # y=120 → description
    ]
    TextAssociator().associate_devices([cand], texts)
    assert cand.name == 'M1', f'expected M1, got {cand.name!r}'
    assert cand.description == ['DTZ178', '张北I线'], f'got {cand.description}'
    print('  ✓ test_text_associator_name_description')


def test_dbscan_column_cluster():
    """6 x-aligned devices → DBSCAN → 1 VERTICAL_COLUMN group."""
    from .clustering import DBSCANClusterer
    devs = [
        DeviceCandidate(id='d1', bbox=BBox(10, 180, 20, 10)),
        DeviceCandidate(id='d2', bbox=BBox(10, 150, 20, 10)),
        DeviceCandidate(id='d3', bbox=BBox(10, 120, 20, 10)),
        DeviceCandidate(id='d4', bbox=BBox(10, 90, 20, 10)),
        DeviceCandidate(id='d5', bbox=BBox(10, 60, 20, 10)),
        DeviceCandidate(id='d6', bbox=BBox(10, 30, 20, 10)),
    ]
    cab = BBox(0, 0, 200, 300)
    groups = DBSCANClusterer(eps=30, min_samples=2).cluster(devs, cab)
    assert len(groups) == 1, f'expected 1 group, got {len(groups)}'
    g = groups[0]
    assert g.group_type == LayoutGroupType.VERTICAL_COLUMN
    assert len(g.devices) == 6
    print(f'  Column: score={g.score}')
    print('  ✓ test_dbscan_column_cluster')


def test_dbscan_noise_standalone():
    """Scattered devices → DBSCAN noise → no groups."""
    from .clustering import DBSCANClusterer
    devs = [
        DeviceCandidate(id='a', bbox=BBox(10, 100, 20, 10)),
        DeviceCandidate(id='b', bbox=BBox(100, 80, 20, 10)),
        DeviceCandidate(id='c', bbox=BBox(200, 60, 20, 10)),
    ]
    cab = BBox(0, 0, 300, 300)
    groups = DBSCANClusterer(eps=30, min_samples=2).cluster(devs, cab)
    assert len(groups) == 0, f'expected 0 groups, got {len(groups)}'
    print('  ✓ test_dbscan_noise_standalone')


def test_full_candidate_pipeline():
    """End-to-end: doc → build_device_candidates → name + cluster."""
    from .candidate import build_device_candidates
    from .associator import TextAssociator
    from .clustering import DBSCANClusterer
    doc = Document(document_path=Path('/fake'), content_hash='pipe', document_type=DocumentType.DWG)
    # Column: 3 closed rect devices
    for i, y in enumerate([100, 70, 40]):
        tag = f'{i * 2 + 2}D'
        doc.add_entity(_line_geom([
            Point(10, y), Point(30, y), Point(30, y + 18), Point(10, y + 18), Point(10, y),
        ], f'r{i}'))
        doc.add_entity(_text(tag, 20, y + 5, f't{i}'))
    container = BBox(0, 0, 200, 300)
    cands = build_device_candidates(doc, container)
    assert len(cands) == 3, f'expected 3 candidates, got {len(cands)}'
    texts = [(x, y, t) for t, x, y in
             [('2D', 20, 105), ('4D', 20, 75), ('6D', 20, 45)]]
    TextAssociator().associate_devices(cands, texts)
    assert cands[0].name == '2D'
    cab = BBox(0, 0, 200, 300)
    groups = DBSCANClusterer(eps=30, min_samples=2).cluster(cands, cab)
    assert len(groups) == 1
    g = groups[0]
    assert g.group_type == LayoutGroupType.VERTICAL_COLUMN
    assert len(g.devices) == 3
    print(f'  Pipeline: {g.group_type}, score={g.score}, devices={len(g.devices)}')
    print('  ✓ test_full_candidate_pipeline')


def test_front_back_cabinets():
    """Left cabinet → front face, right cabinet → back face."""
    cab_front = LayoutNode(id='cab_f', node_type=LayoutNodeType.CABINET,
                           bbox=BBox(0, 100, 100, 200), name='')
    cab_back = LayoutNode(id='cab_b', node_type=LayoutNodeType.CABINET,
                          bbox=BBox(200, 300, 100, 200), name='')
    from .detector import _identify_front_back
    _identify_front_back([cab_front, cab_back])
    assert cab_front.data.get('face') == 'front'
    assert cab_front.name == '正面'
    assert cab_back.data.get('face') == 'back'
    assert cab_back.name == '背面'
    print('  ✓ test_front_back_cabinets')


def test_group_label_assignment_right():
    """Text label 右侧 above the right column → group name = 右侧."""
    from .grouping.clustering import detect_layout_groups
    devs = [
        _device_node('1D', 160, 200, 20, 15, 'd1'),
        _device_node('3D', 160, 170, 20, 15, 'd3'),
        _device_node('5D', 160, 140, 20, 15, 'd5'),
    ]
    text_positions = [(160, 240, '右侧')]
    cab = BBox(0, 0, 300, 300)
    groups = detect_layout_groups(devs, cab, text_positions)
    assert len(groups) == 1, f'expected 1 group, got {len(groups)}'
    g = groups[0]
    assert g.group_type == LayoutGroupType.VERTICAL_COLUMN
    assert g.name == '右侧', f'expected name=右侧, got {g.name!r}'
    print(f'  Group: {g.name} [{g.group_type.value}]')
    print('  ✓ test_group_label_assignment_right')


def test_eyebrow_row_all_together():
    """6 devices in a top row with 50-70u gaps → single ROW group (ROW_GAP_MAX=120)."""
    from .grouping.clustering import _detect_rows
    devs = [
        _device_node('ZDK', 0, 280, 30, 15, 'zdk'),
        _device_node('ZDF', 50, 280, 30, 15, 'zdf'),
        _device_node('DK1', 120, 280, 30, 15, 'dk1'),
        _device_node('DK2', 160, 280, 30, 15, 'dk2'),
        _device_node('DK3', 200, 280, 30, 15, 'dk3'),
        _device_node('DK4', 240, 280, 30, 15, 'dk4'),
    ]
    cab = BBox(0, 0, 300, 300)
    used: set[str] = set()
    rows = _detect_rows(devs, cab, used)
    assert len(rows) == 1, f'expected 1 row, got {len(rows)}'
    row = rows[0]
    assert row.group_type == LayoutGroupType.HORIZONTAL_ROW
    assert len(row.children) == 6
    print(f'  Row: score={row.data["score"]}, children={len(row.children)}')
    print('  ✓ test_eyebrow_row_all_together')


if __name__ == '__main__':
    print('LayoutTree detector tests:')
    test_empty_doc()
    test_rectangle_from_polyline()
    test_rectangle_from_4seg()
    test_cabinet_from_paired_verticals()
    test_areas_from_horizontal_dividers()
    test_devices_in_cabinet()
    test_device_with_blockref()
    test_text_device_detection()
    test_device_spatial_graph()
    test_vertical_column_detection()
    test_no_group_for_scattered_devices()
    test_gap_splitting_excludes_distant_device()
    test_grid_detection()
    test_horizontal_row_detection()
    test_grouping_integration()
    test_front_back_cabinets()
    test_group_label_assignment_right()
    test_eyebrow_row_all_together()
    # V8.1 tests
    test_closed_rect_candidate()
    test_L_shape_candidate()
    test_U_shape_candidate()
    test_circle_symbol_candidate()
    test_candidate_pool_dedup()
    test_text_associator_name_description()
    test_dbscan_column_cluster()
    test_dbscan_noise_standalone()
    test_full_candidate_pipeline()
    print()
    print('All tests passed.')
