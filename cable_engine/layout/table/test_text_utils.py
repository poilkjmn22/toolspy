"""Tests for cable_engine.layout.table.text_utils — shared text utilities."""

import re
from cable_engine.ir import Document, DocumentType
from cable_engine.ir.entities import BBox, TextEntity
from cable_engine.ir.geometry import AttributeEntity
from cable_engine.layout.table.text_utils import (
    collect_texts,
    count_texts_in,
    y_bucket_rows,
    find_header_row,
    map_column_roles,
    detect_gap_x,
)


def _doc_with_texts(*texts: tuple[float, float, str]) -> Document:
    doc = Document(document_type=DocumentType.DWG, document_path='/dev/null')
    for x, y, t in texts:
        te = TextEntity(id=f't_{len(doc.entities)}', source='test', page=1,
                        confidence=1.0, bbox=BBox(x, y, 1, 1), text=t)
        te.custom_fields = {'x': x, 'y': y}
        doc.entities.append(te)
    return doc


class TestCollectTexts:
    def test_basic(self):
        doc = _doc_with_texts((10, 20, 'hello'), (30, 40, 'world'))
        out = collect_texts(doc)
        assert len(out) == 2
        assert (10, 20, 'hello') in out

    def test_bbox_filter(self):
        doc = _doc_with_texts((10, 20, 'in'), (100, 200, 'out'))
        bbox = BBox(0, 0, 50, 50)
        out = collect_texts(doc, bbox)
        assert len(out) == 1
        assert out[0][2] == 'in'

    def test_max_len(self):
        doc = _doc_with_texts((0, 0, 'short'), (0, 0, 'x' * 60))
        out = collect_texts(doc, max_len=50)
        assert len(out) == 1
        assert out[0][2] == 'short'

    def test_empty_and_noise(self):
        doc = _doc_with_texts((0, 0, ''), (0, 0, '\\M+foo'), (0, 0, 'KKS:123'),
                              (0, 0, 'valid'))
        out = collect_texts(doc)
        assert len(out) == 1
        assert out[0][2] == 'valid'


class TestCountTextsIn:
    def test_count(self):
        doc = _doc_with_texts((10, 10, 'a'), (20, 20, 'b'), (100, 100, 'c'))
        assert count_texts_in(doc, BBox(0, 0, 50, 50)) == 2


class TestYBucketRows:
    def test_two_rows(self):
        texts = [(10, 100, 'H1'), (10, 100, 'H2'),
                 (10, 50, 'D1'), (10, 50, 'D2')]
        rows = y_bucket_rows(texts, tol=3.0)
        assert len(rows) == 2
        assert len(rows[0]) == 2  # top row
        assert len(rows[1]) == 2  # bottom row

    def test_single_row_returns_empty(self):
        texts = [(10, 100, 'only')]
        assert y_bucket_rows(texts) == []


class TestFindHeaderRow:
    def test_found(self):
        rows = [
            [(0, 'abc'), (0, 'def')],
            [(0, '编号'), (0, '名称')],
            [(0, 'A001'), (0, 'Device')],
        ]
        patterns = [(re.compile(r'编号'), 'index'),
                    (re.compile(r'名称'), 'name')]
        assert find_header_row(rows, patterns) == 1

    def test_not_found(self):
        rows = [[(0, 'abc')], [(0, 'def')]]
        patterns = [(re.compile(r'编号'), 'index')]
        assert find_header_row(rows, patterns) is None


class TestMapColumnRoles:
    def test_basic(self):
        header = [(0, '序号'), (1, '名称'), (2, '数量')]
        patterns = [(re.compile(r'序号'), 'index'),
                    (re.compile(r'名称'), 'name'),
                    (re.compile(r'数量'), 'qty')]
        roles = map_column_roles(header, patterns)
        assert roles == {0: 'index', 1: 'name', 2: 'qty'}

    def test_partial_match(self):
        header = [(0, '编号'), (2, '型号')]
        patterns = [(re.compile(r'编号'), 'index'),
                    (re.compile(r'型号'), 'model')]
        roles = map_column_roles(header, patterns)
        assert roles == {0: 'index', 1: 'model'}


class TestDetectGapX:
    def test_two_columns(self):
        header = [(0, '屏号'), (10, '名称'), (20, '数量'),
                  (100, '屏号'), (110, '名称'), (120, '数量')]
        roles = {0: 'cell_label', 1: 'equipment', 2: 'qty',
                 3: 'cell_label', 4: 'equipment', 5: 'qty'}
        gap = detect_gap_x(header, roles, 'cell_label')
        assert gap is not None
        assert 50 < gap < 70

    def test_single_column(self):
        header = [(0, '屏号'), (10, '名称')]
        roles = {0: 'cell_label', 1: 'equipment'}
        assert detect_gap_x(header, roles, 'cell_label') is None
