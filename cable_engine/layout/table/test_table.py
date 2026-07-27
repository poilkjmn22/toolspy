"""Tests for the table parser using synthetic Document IR.

Run: python -m cable_engine.layout.table.test_table
"""

from __future__ import annotations

from pathlib import Path

from ...ir import BBox, Document, DocumentType
from ...ir.entities import TextEntity, Point
from ...ir.geometry import LineGeometry
from ..primitives.rectangle import detect_rectangles
from .detector import detect_table_regions
from .parser import parse_table_at
from .matcher import match_to_devices
from .model import TableArea, TableRow


def _text(text: str, x: float, y: float, id_: str = '') -> TextEntity:
    ent = TextEntity(id=id_, source='dwg', page=1, confidence=1.0, text=text)
    ent.custom_fields = {'x': x, 'y': y}
    return ent


def _line_geom(pts: list[Point], id_: str = '') -> LineGeometry:
    ent = LineGeometry(id=id_, source='dwg', page=1, confidence=1.0, handle=id_, points=pts)
    ent.custom_fields = {}
    return ent


def _make_doc(entities: list) -> Document:
    doc = Document(document_path=Path('/fake'), content_hash='table_test', document_type=DocumentType.DWG)
    for e in entities:
        doc.add_entity(e)
    return doc


def test_detect_table_region():
    """A large rectangle with >=4 texts inside → detected as table."""
    doc = _make_doc([
        # Table outer border (60w × 80h)
        _line_geom([
            Point(200, 0), Point(260, 0), Point(260, -80), Point(200, -80), Point(200, 0),
        ], 'tbl_border'),
        # 6 text cells
        _text('序号', 210, -5, 'h1'),
        _text('名称', 230, -5, 'h2'),
        _text('型号', 250, -5, 'h3'),
        _text('1', 210, -20, 'd1'),
        _text('M1', 230, -20, 'd2'),
        _text('ABC-123', 250, -20, 'd3'),
        _text('2', 210, -35, 'd4'),
        _text('M2', 230, -35, 'd5'),
        _text('DEF-456', 250, -35, 'd6'),
    ])
    container = BBox(190, -90, 80, 100)
    bboxes = detect_table_regions(doc, container)
    assert len(bboxes) == 1, f'expected 1 table region, got {len(bboxes)}'
    tb = bboxes[0]
    assert tb.w >= 60 and tb.h >= 80
    print('  ✓ test_detect_table_region')


def test_detect_table_region_too_small():
    """Rectangles smaller than 60w × 80h are not table regions."""
    doc = _make_doc([
        _line_geom([
            Point(0, 0), Point(40, 0), Point(40, -20), Point(0, -20), Point(0, 0),
        ], 'small'),
        _text('A', 5, -2),
        _text('B', 20, -2),
    ])
    bboxes = detect_table_regions(doc, BBox(-10, -30, 60, 40))
    assert len(bboxes) == 0
    print('  ✓ test_detect_table_region_too_small')


def test_parse_equipment_table():
    """Parse a 4-column equipment table with header detection."""
    doc = _make_doc([
        _line_geom([
            Point(300, 0), Point(500, 0), Point(500, -120), Point(300, -120), Point(300, 0),
        ], 'table'),
        # Header row
        _text('序号', 310, -5, 'h1'),
        _text('名称', 350, -5, 'h2'),
        _text('型号', 400, -5, 'h3'),
        _text('数量', 450, -5, 'h4'),
        # Data row 1
        _text('1', 310, -25, 'd1'),
        _text('M1', 350, -25, 'd2'),
        _text('DTZ-300', 400, -25, 'd3'),
        _text('1', 450, -25, 'd4'),
        # Data row 2
        _text('2', 310, -45, 'd5'),
        _text('M2', 350, -45, 'd6'),
        _text('DTZ-600', 400, -45, 'd7'),
        _text('2', 450, -45, 'd8'),
        # Data row 3
        _text('3', 310, -65, 'd9'),
        _text('DH1', 350, -65, 'd10'),
        _text('JXD-200', 400, -65, 'd11'),
        _text('3', 450, -65, 'd12'),
    ])
    table_bbox = BBox(300, -120, 200, 120)
    table = parse_table_at(doc, table_bbox)
    assert table is not None, 'expected a parsed table'
    assert table.name_column_index >= 0, 'name column not detected'
    assert table.header_row is not None
    assert any('名称' in c.text for c in table.header_row.cells)
    assert len(table.data_rows) == 3, f'expected 3 data rows, got {len(table.data_rows)}'
    print(f'  Header columns: {table.header_columns}')
    print(f'  Name col idx: {table.name_column_index}')
    print(f'  Data rows: {len(table.data_rows)}')
    print('  ✓ test_parse_equipment_table')


def test_parse_table_no_header():
    """Table without Chinese header keywords returns None."""
    doc = _make_doc([
        _line_geom([
            Point(100, 0), Point(200, 0), Point(200, -60), Point(100, -60), Point(100, 0),
        ], 'table'),
        _text('ColA', 110, -5),
        _text('ColB', 150, -5),
        _text('val1', 110, -25),
        _text('val2', 150, -25),
        _text('val3', 110, -45),
        _text('val4', 150, -45),
    ])
    table = parse_table_at(doc, BBox(100, -60, 100, 60))
    assert table is None, 'expected None for table without Chinese header'
    print('  ✓ test_parse_table_no_header')


def test_match_to_devices():
    """Match table rows to DeviceCandidates by name column."""
    from ..candidate import DeviceCandidate
    table = TableArea(
        bbox=BBox(0, 0, 100, 80),
        header_columns=['序号', '名称', '型号', '数量'],
        name_column_index=1,
        model_column_index=2,
        qty_column_index=3,
    )
    rows = [
        TableRow(y=-20, header=False),
        TableRow(y=-40, header=False),
        TableRow(y=-60, header=False),
    ]
    from .model import TableCell
    rows[0].cells = [
        TableCell(text='1', x=10, y=-20, col_index=0, row_index=0),
        TableCell(text='M1', x=30, y=-20, col_index=1, row_index=0),
        TableCell(text='DTZ-300', x=60, y=-20, col_index=2, row_index=0),
        TableCell(text='1', x=90, y=-20, col_index=3, row_index=0),
    ]
    rows[1].cells = [
        TableCell(text='2', x=10, y=-40, col_index=0, row_index=1),
        TableCell(text='M2', x=30, y=-40, col_index=1, row_index=1),
        TableCell(text='DTZ-600', x=60, y=-40, col_index=2, row_index=1),
        TableCell(text='2', x=90, y=-40, col_index=3, row_index=1),
    ]
    rows[2].cells = [
        TableCell(text='3', x=10, y=-60, col_index=0, row_index=2),
        TableCell(text='DH1', x=30, y=-60, col_index=1, row_index=2),
        TableCell(text='JXD-200', x=60, y=-60, col_index=2, row_index=2),
        TableCell(text='3', x=90, y=-60, col_index=3, row_index=2),
    ]
    table.rows = rows

    candidates = [
        DeviceCandidate(id='d1', name='M1', bbox=BBox(20, -25, 10, 5)),
        DeviceCandidate(id='d2', name='M2', bbox=BBox(20, -45, 10, 5)),
        DeviceCandidate(id='d3', name='DH1', bbox=BBox(20, -70, 10, 5)),
    ]
    count = match_to_devices(table, candidates)
    assert count == 3, f'expected 3 matches, got {count}'
    for c in candidates:
        info = c.features.get('table_info', {})
        assert 'model' in info, f'{c.name} missing model'
        print(f'  {c.name} → model={info.get("model")} qty={info.get("qty")}')
    print('  ✓ test_match_to_devices')


def test_match_to_devices_no_match():
    """Rows not matching any candidate produce 0 matches."""
    from ..candidate import DeviceCandidate
    table = TableArea(
        bbox=BBox(0, 0, 100, 40),
        header_columns=['序号', '名称'],
        name_column_index=1,
    )
    row = TableRow(y=-20, header=False)
    from .model import TableCell
    row.cells = [
        TableCell(text='99', x=10, y=-20, col_index=0, row_index=0),
        TableCell(text='ZZ_FAKE', x=30, y=-20, col_index=1, row_index=0),
    ]
    table.rows = [row]

    candidates = [
        DeviceCandidate(id='d1', name='M1', bbox=BBox(20, -25, 10, 5)),
        DeviceCandidate(id='d2', name='M2', bbox=BBox(20, -45, 10, 5)),
    ]
    count = match_to_devices(table, candidates)
    assert count == 0, f'expected 0 matches, got {count}'
    print('  ✓ test_match_to_devices_no_match')


if __name__ == '__main__':
    test_detect_table_region()
    test_detect_table_region_too_small()
    test_parse_equipment_table()
    test_parse_table_no_header()
    test_match_to_devices()
    test_match_to_devices_no_match()
    print('\nAll table tests passed!')
