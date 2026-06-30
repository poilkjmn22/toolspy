from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Optional

from cable_engine.ir import (
    Document, DocumentType, LineEntity, Page, Point, PolylineEntity,
    SymbolEntity, TextEntity,
)

from .base import BaseLoader, _register_suffix


def _content_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


_DWG_TEXT_ATTR_KEYS = frozenset([
    'WIRECODE', 'EQUCODE', 'EQUNAME', 'NO', 'ObjTerm.Name',
    'DESC', 'TERNO', 'CODE', 'DWGNO0', 'DIC1', 'DIC2', 'WireSerial',
])


class DWGLoader(BaseLoader):
    document_type: DocumentType = DocumentType.DWG
    name: str = 'dwg'

    def load(self, document_path: Path) -> Document:
        doc = Document(
            document_type=DocumentType.DWG,
            document_path=document_path,
            content_hash=_content_hash(document_path) if document_path.exists()
                         else '',
        )
        doc.pages.append(Page(
            pdf_path=document_path,
            content_hash=doc.content_hash,
            page_number=1,
            source='dwg',
        ))

        if not document_path.exists():
            doc.entities.append(TextEntity(
                id='err-missing', source='dwg', page=1, confidence=0.0,
                text=f'<dwg file not found: {document_path}>',
            ))
            return doc

        try:
            import ezdxf
        except ImportError as e:
            doc.entities.append(TextEntity(
                id='err-import', source='dwg', page=1, confidence=0.0,
                text=f'<ezdxf not installed; install with: pip install ezdxf> '
                     f'(underlying error: {e})',
            ))
            return doc

        dwg = self._open_document(document_path, doc)
        if dwg is None:
            return doc
        if dwg is True:
            return doc

        msp = dwg.modelspace()
        for e in msp:
            try:
                self._consume_entity(e, doc)
            except Exception:
                pass
        return doc

    def _open_document(self, path: Path, doc: Document):
        import ezdxf
        from ezdxf.lldxf.const import DXFStructureError

        suffix = path.suffix.lower()

        # .dwg files: try dwgread JSON first (more reliable for libredwg)
        # then fall back to ezdxf
        if suffix == '.dwg':
            result = self._open_via_dwgread(path, doc)
            if result is not None:
                return result

        # .dxf files: try ezdxf first
        for _ in range(2):
            try:
                return ezdxf.readfile(str(path))
            except DXFStructureError as e:
                msg = str(e)
                if 'INSERT or SEQEND' in msg:
                    fixed = self._fix_dxf_blocks(path)
                    if fixed:
                        path = fixed
                        continue
                else:
                    fixed = self._fix_dxf_utf8(path)
                    if fixed:
                        path = fixed
                        continue
            except Exception:
                pass
            break

        # Last resort: try dwgread JSON for .dxf files too
        result = self._open_via_dwgread(path, doc)
        if result is not None:
            return result

        return None

    def _fix_dxf_blocks(self, path: Path) -> Path | None:
        try:
            txt = path.read_text(errors='replace')
        except Exception:
            return None
        start = txt.find('\n  0\nSECTION\n  2\nBLOCKS\n')
        if start < 0:
            return None
        end = txt.find('\n  0\nENDSEC\n', start + 1)
        if end < 0:
            return None
        blocks_raw = txt[start:end]
        seqend_count = blocks_raw.count('\n  0\nSEQEND\n')
        insert_count = blocks_raw.count('\n  0\nINSERT\n')
        diff = seqend_count - insert_count
        if diff <= 0:
            return None
        lines = blocks_raw.split('\n')
        fixed_lines = []
        seen_insert = False
        removed = 0
        for line in lines:
            if line.strip() == 'INSERT':
                seen_insert = True
                fixed_lines.append(line)
            elif line.strip() == 'SEQEND':
                if seen_insert:
                    seen_insert = False
                    fixed_lines.append(line)
                else:
                    removed += 1
            else:
                fixed_lines.append(line)
        if removed == 0:
            return None
        patched = '\n'.join(fixed_lines)
        dst = path.parent / (path.stem + '_blocks_fixed.dxf')
        dst.write_text(txt[:start] + patched + txt[end:], encoding='utf-8')
        return dst

    def _fix_dxf_utf8(self, path: Path) -> Path | None:
        try:
            txt = path.read_text(errors='replace')
        except Exception:
            return None
        lines = txt.split('\n')
        clean = []
        changed = False
        for line in lines:
            try:
                clean.append(line)
            except UnicodeEncodeError:
                clean.append(line.encode('utf-8', errors='replace').decode('utf-8'))
                changed = True
        if not changed:
            return None
        dst = path.parent / (path.stem + '_utf8.dxf')
        dst.write_text('\n'.join(clean), encoding='utf-8')
        return dst

    def _open_via_dwgread(self, path: Path, doc: Document):
        try:
            r = subprocess.run(
                ['dwgread', '-O', 'JSON', str(path)],
                capture_output=True, timeout=30,
            )
            if r.returncode != 0:
                return None
            raw = r.stdout.decode('utf-8', errors='replace')
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        text_values = []
        try:
            import ijson
            with open(path.with_suffix('.json'), 'wb') as f:
                f.write(r.stdout)
        except ImportError:
            text_values = re.findall(r'"text_value":\s*"([^"]*)"', raw)

        if not text_values:
            text_values = re.findall(r'"text_value":\s*"([^"]*)"', raw)

        for v in text_values:
            clean = v.replace('\\n', ' ').strip()
            if not clean:
                continue
            doc.add_entity(TextEntity(
                id=f'dwgread_{hash(clean) & 0xffffff:06x}',
                source='dwg', page=1, confidence=1.0,
                text=clean,
            ))

        return True  # signal: entities added, no ezdxf Document needed

    def _consume_entity(self, e, doc: Document) -> None:
        typ = e.dxftype()
        handle = getattr(e.dxf, 'handle', '')
        layer = getattr(e.dxf, 'layer', '')

        if typ == 'LINE':
            start = e.dxf.start
            end = e.dxf.end
            doc.add_entity(LineEntity(
                id=handle, source='dwg', page=1,
                confidence=1.0,
                layer=layer,
                points=[Point(start.x, start.y), Point(end.x, end.y)],
            ))

        elif typ == 'LWPOLYLINE':
            pts = [Point(p[0], p[1]) for p in e.get_points()]
            if len(pts) == 2:
                doc.add_entity(LineEntity(
                    id=handle, source='dwg', page=1, confidence=1.0,
                    layer=layer, points=pts,
                ))
            else:
                doc.add_entity(PolylineEntity(
                    id=handle, source='dwg', page=1, confidence=1.0,
                    layer=layer, points=pts,
                ))

        elif typ in ('TEXT', 'MTEXT'):
            text = (
                e.plain_text() if typ == 'MTEXT'
                else e.dxf.text
            )
            doc.add_entity(TextEntity(
                id=handle, source='dwg', page=1, confidence=1.0,
                layer=layer, text=text or '',
            ))

        elif typ == 'INSERT':
            name = getattr(e.dxf, 'name', '') or ''
            insert = e.dxf.insert if hasattr(e.dxf, 'insert') else (0, 0, 0)
            x, y = insert[0], insert[1]

            if not name and abs(x) < 2 and abs(y) < 2:
                return

            syment = SymbolEntity(
                id=handle, source='dwg', page=1, confidence=1.0,
                layer=layer, name=name,
            )
            syment.custom_fields['insert_x'] = float(x)
            syment.custom_fields['insert_y'] = float(y)
            doc.add_entity(syment)

            for att in getattr(e, 'attribs', []):
                tag = getattr(att.dxf, 'tag', '') or ''
                val = getattr(att.dxf, 'text', '') or ''
                tag_upper = tag.upper().strip()
                if not tag_upper or not val.strip():
                    continue
                if tag_upper in _DWG_TEXT_ATTR_KEYS or any(
                    tag_upper.endswith(s) for s in ('NO', 'NAME', 'CODE', 'TYPE')
                ):
                    doc.add_entity(TextEntity(
                        id=f'{handle}__{tag}',
                        source='dwg', page=1, confidence=1.0,
                        layer=layer,
                        text=val.strip(),
                        custom_fields={
                            'attrib_tag': tag_upper,
                            'insert_x': float(x),
                            'insert_y': float(y),
                            'block': name,
                        },
                    ))


_register_suffix(DWGLoader, '.dwg')
_register_suffix(DWGLoader, '.dxf')

__all__ = ['DWGLoader']
