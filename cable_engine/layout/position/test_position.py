"""Tests for cable_engine.layout.position — 屏位布置图解析器."""

from __future__ import annotations

from pathlib import Path

from cable_engine.ir import Document
from cable_engine.ir.document import DocumentType
from cable_engine.ir.entities import BBox, TextEntity
from cable_engine.ir.geometry import LineGeometry, Point
from cable_engine.layout.position.detector import detect_room, detect_cells, cluster_rows, find_f_texts
from cable_engine.layout.position.model import PositionCell, PositionRow, UsageTableRow
from cable_engine.layout.position.crossref import cross_reference
from cable_engine.layout.position.parser import parse_usage_table
from cable_engine.layout.position.builder import build_position_tree
from cable_engine.layout.model import LayoutNode, LayoutNodeType, LayoutTree


def _make_doc():
    return Document(
        document_type=DocumentType.DWG,
        document_path=Path('/fake/test.dwg'),
    )

_COUNTER = 0

def _make_line(x1, y1, x2, y2, handle='L1'):
    global _COUNTER
    _COUNTER += 1
    pts = [Point(x1, y1), Point(x2, y2)]
    line = LineGeometry(id=f'{_COUNTER}', source='dwg', page=1)
    line.handle = handle
    line.points = pts
    return line


def _make_text(x, y, text, handle='T1'):
    global _COUNTER
    _COUNTER += 1
    t = TextEntity(id=f'{_COUNTER}', source='dwg', page=1)
    t.handle = handle
    t.text = text
    t.custom_fields = {'x': x, 'y': y}
    return t


def _rect_bbox(x, y, w, h):
    """Simulate a rectangle detected by detect_rectangles as 4 lines."""
    lines = []
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    for i in range(4):
        lines.append(_make_line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], f'R{i}'))
    return lines


class TestDetectRoom:
    """detect_room — long lines → room bbox."""

    def test_basic_room(self):
        doc = _make_doc()
        doc.entities = [
            # Room boundary: 2 horizontals + 2 verticals
            _make_line(-370, -44, -160, -44, 'H1'),    # bottom
            _make_line(-370, 62, -160, 62, 'H2'),      # top
            _make_line(-373, -44, -373, 62, 'V1'),     # left
            _make_line(-160, -44, -160, 62, 'V2'),     # right
            # Internal row divider
            _make_line(-340, 24, -190, 24, 'H3'),
            # Internal column divider
            _make_line(-290, -44, -290, 62, 'V3'),
        ]
        room = detect_room(doc)
        assert room is not None
        assert abs(room.x - (-373)) < 5
        assert abs(room.y - (-44)) < 5
        assert room.w > 200
        assert room.h > 100

    def test_no_lines(self):
        doc = _make_doc()
        doc.entities = []
        assert detect_room(doc) is None


class TestFindFTexts:
    """find_f_texts — scan for F-number patterns."""

    def test_finds_f_texts(self):
        doc = _make_doc()
        doc.entities = [
            _make_text(10, 20, '1F'),
            _make_text(30, 40, '2F'),
            _make_text(50, 60, 'ABC'),  # not F-format
        ]
        result = find_f_texts(doc)
        assert len(result) == 2
        assert result[0][2] == '1F'
        assert result[1][2] == '2F'

    def test_empty_doc(self):
        doc = _make_doc()
        assert find_f_texts(doc) == []

    def test_no_f_patterns(self):
        doc = _make_doc()
        doc.entities = [_make_text(10, 20, 'ABC'), _make_text(30, 40, '12G')]
        assert find_f_texts(doc) == []


class TestDetectRoomWithFTexts:
    """detect_room with F-texts hint."""

    def test_with_f_texts_hint(self):
        doc = _make_doc()
        doc.entities = [
            _make_line(-370, -44, -160, -44, 'H1'),    # bottom
            _make_line(-370, 62, -160, 62, 'H2'),      # top
            _make_line(-373, -44, -373, 62, 'V1'),     # left
            _make_line(-160, -44, -160, 62, 'V2'),     # right
        ]
        # F-texts serve as hint
        doc.entities.append(_make_text(-340, 10, '1F'))
        doc.entities.append(_make_text(-300, 10, '2F'))
        f_texts = find_f_texts(doc)
        assert len(f_texts) == 2
        room = detect_room(doc, f_texts)
        assert room is not None
        assert abs(room.x - (-373)) < 5
        assert room.w < 230  # tighter than global extreme approach

    def test_no_f_texts_fallback(self):
        doc = _make_doc()
        doc.entities = [
            _make_line(-370, -44, -160, -44, 'H1'),    # bottom
            _make_line(-370, 62, -160, 62, 'H2'),      # top
        ]
        # No F-texts, should fall back to global extremes
        room = detect_room(doc)
        assert room is not None
        assert abs(room.y - (-44)) < 5


class TestDetectCells:
    """detect_cells — rectangles + F-number texts."""

    def _make_doc_with_cells(self):
        doc = _make_doc()
        # 4 cells in a 2×2 grid (each ~10×5)
        cell_w, cell_h = 10, 5
        base_x, base_y = -340, -12
        gaps_x, gaps_y = 12, 7
        cells_bboxes = [
            BBox(base_x, base_y, cell_w, cell_h),                        # (0,0)
            BBox(base_x + gaps_x, base_y, cell_w, cell_h),               # (0,1)
            BBox(base_x, base_y + gaps_y, cell_w, cell_h),               # (1,0)
            BBox(base_x + gaps_x, base_y + gaps_y, cell_w, cell_h),      # (1,1)
        ]
        for b in cells_bboxes:
            doc.entities.extend(_rect_bbox(b.x, b.y, b.w, b.h))

        # F-number texts near each cell
        doc.entities.append(_make_text(base_x + cell_w / 2, base_y + cell_h / 2, '1F'))
        doc.entities.append(_make_text(base_x + gaps_x + cell_w / 2, base_y + cell_h / 2, '2F'))
        doc.entities.append(_make_text(base_x + cell_w / 2, base_y + gaps_y + cell_h / 2, '3F'))
        doc.entities.append(_make_text(base_x + gaps_x + cell_w / 2, base_y + gaps_y + cell_h / 2, '4F'))

        room = BBox(-350, -20, 200, 80)
        return doc, room, cells_bboxes

    def test_detect_cells(self):
        doc, room, _ = self._make_doc_with_cells()
        cells = detect_cells(doc, room)
        assert len(cells) == 4
        labels = {c.label for c in cells}
        assert labels == {'1F', '2F', '3F', '4F'}

    def test_empty_room_no_cells(self):
        doc = _make_doc()
        room = BBox(0, 0, 100, 100)
        cells = detect_cells(doc, room)
        assert cells == []


class TestClusterRows:
    """cluster_rows — Y-proximity grouping."""

    def test_two_rows(self):
        cells = [
            PositionCell(label='1F', bbox=BBox(0, 10, 10, 5)),
            PositionCell(label='2F', bbox=BBox(15, 10, 10, 5)),
            PositionCell(label='3F', bbox=BBox(0, 20, 10, 5)),
            PositionCell(label='4F', bbox=BBox(15, 20, 10, 5)),
        ]
        rows = cluster_rows(cells)
        assert len(rows) == 2
        # Row 0 is top (y=25), row 1 is bottom (y=12.5)
        assert len(rows[0].cells) == 2
        assert len(rows[1].cells) == 2
        # Cells within a row are sorted left-to-right
        assert [c.label for c in rows[0].cells] == ['3F', '4F']
        assert [c.label for c in rows[1].cells] == ['1F', '2F']

    def test_single_cell(self):
        cells = [PositionCell(label='1F', bbox=BBox(0, 0, 10, 5))]
        rows = cluster_rows(cells)
        assert len(rows) == 1
        assert len(rows[0].cells) == 1

    def test_empty(self):
        assert cluster_rows([]) == []


class TestCrossRef:
    """cross_reference — merge usage table into cells."""

    def test_match(self):
        rows = [
            PositionRow(cells=[
                PositionCell(label='1F'),
                PositionCell(label='2F'),
            ]),
        ]
        table = type('UT', (), {'rows': [
            UsageTableRow(cell_label='1F', equipment='励磁柜', qty=1),
            UsageTableRow(cell_label='2F', equipment='调节柜', qty=1),
        ]})()
        cross_reference(rows, table)
        assert rows[0].cells[0].equipment == '励磁柜'
        assert rows[0].cells[0].qty == 1
        assert rows[0].cells[1].equipment == '调节柜'

    def test_no_table_rows(self):
        rows = [PositionRow(cells=[PositionCell(label='1F')])]
        table = type('UT', (), {'rows': []})()
        cross_reference(rows, table)
        assert rows[0].cells[0].equipment == ''

    def test_no_match(self):
        rows = [PositionRow(cells=[PositionCell(label='99F')])]
        table = type('UT', (), {'rows': [
            UsageTableRow(cell_label='1F', equipment='励磁柜'),
        ]})()
        cross_reference(rows, table)
        assert rows[0].cells[0].equipment == ''


class TestBuildPositionTree:
    """build_position_tree — full pipeline integration."""

    def test_happy_path(self):
        doc = _make_doc()
        # Room: 2 horizontals + 2 verticals
        doc.entities.extend([
            _make_line(-370, -44, -160, -44, 'H1'),
            _make_line(-370, 62, -160, 62, 'H2'),
            _make_line(-373, -44, -373, 62, 'V1'),
            _make_line(-160, -44, -160, 62, 'V2'),
        ])
        # 1 cell (simplified rectangle)
        doc.entities.extend(_rect_bbox(-340, -12, 10, 5))
        doc.entities.extend(_rect_bbox(-325, -12, 10, 5))
        doc.entities.extend(_rect_bbox(-340, -3, 10, 5))
        doc.entities.extend(_rect_bbox(-325, -3, 10, 5))
        # F-texts
        doc.entities.append(_make_text(-335, -9.5, '1F'))
        doc.entities.append(_make_text(-320, -9.5, '2F'))
        doc.entities.append(_make_text(-335, -0.5, '3F'))
        doc.entities.append(_make_text(-320, -0.5, '4F'))

        tree = build_position_tree(doc)
        assert tree is not None
        assert len(tree.roots) == 1
        rooms = [r for r in tree.roots if r.node_type == LayoutNodeType.ROOM]
        assert len(rooms) == 1

        rows = rooms[0].children
        assert len(rows) >= 1
        all_cells = []
        for row in rows:
            all_cells.extend(row.children)
        cell_labels = {c.name for c in all_cells}
        assert '1F' in cell_labels
        assert '2F' in cell_labels

    def test_empty_doc(self):
        doc = _make_doc()
        tree = build_position_tree(doc)
        assert tree is None

    def test_no_room(self):
        doc = _make_doc()
        doc.entities = [_make_line(0, 0, 10, 0, 'H1')]
        tree = build_position_tree(doc)
        assert tree is None
