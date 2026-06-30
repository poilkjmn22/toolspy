from __future__ import annotations

import hashlib
import json
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


_ENTITY_RE = re.compile(r'"entity":\s*"(\w+)"')

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

        suffix = document_path.suffix.lower()
        if suffix == '.dwg':
            try:
                import ezdxf
                _ = ezdxf  # silence unused
            except ImportError:
                pass
            loaded = self._open_via_dwgread(document_path, doc)
            if loaded:
                return doc

        try:
            import ezdxf
        except ImportError as e:
            doc.entities.append(TextEntity(
                id='err-import', source='dwg', page=1, confidence=0.0,
                text=f'<ezdxf import error: {e}. Install: pip install ezdxf fonttools>'
            ))
            return doc

        dwg = self._open_document_via_ezdxf(document_path, doc)
        if dwg and dwg is not True:
            msp = dwg.modelspace()
            for e in msp:
                try:
                    self._consume_entity(e, doc)
                except Exception:
                    pass
        return doc

    def _open_document_via_ezdxf(self, path: Path, doc: Document):
        import ezdxf
        from ezdxf.lldxf.const import DXFStructureError

        for attempt in range(2):
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

        result = self._open_via_dwgread(path, doc)
        return result

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
        insert_count = blocks_raw.count('\n  0\nINSERT\n')
        seqend_count = blocks_raw.count('\n  0\nSEQEND\n')
        diff = seqend_count - insert_count
        if diff <= 0:
            return None
        lines = blocks_raw.split('\n')
        fixed_lines = []
        seen_insert = False
        for line in lines:
            if line.strip() == 'INSERT':
                seen_insert = True
                fixed_lines.append(line)
            elif line.strip() == 'SEQEND':
                if seen_insert:
                    seen_insert = False
                    fixed_lines.append(line)
            else:
                fixed_lines.append(line)
        dst = path.parent / (path.stem + '_blocks_fixed.dxf')
        dst.write_text(txt[:start] + '\n'.join(fixed_lines) + txt[end:], encoding='utf-8')
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

    # ------------------------------------------------------------------
    # Comprehensive dwgread JSON parser
    # ------------------------------------------------------------------
    def _open_via_dwgread(self, path: Path, doc: Document) -> bool:
        try:
            r = subprocess.run(
                ['dwgread', '-O', 'JSON', str(path)],
                capture_output=True, timeout=30,
            )
            if r.returncode != 0:
                return False
            raw = r.stdout.decode('utf-8', errors='replace')
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

        self._parse_json_entities(raw, doc)
        return True

    def _parse_json_entities(self, raw: str, doc: Document) -> None:
        entity_id = [0]
        text_seen: set[str] = set()

        for m in _ENTITY_RE.finditer(raw):
            etype = m.group(1)
            idx = m.start()
            start = raw.rfind('{', max(0, idx - 2000), idx)
            if start < 0:
                continue
            depth = 0
            end = start
            for i in range(start, len(raw)):
                if raw[i] == '{':
                    depth += 1
                elif raw[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            block = raw[start:end]

            try:
                if etype in ('TEXT', 'MTEXT'):
                    self._parse_text_entity(block, etype, doc, entity_id, text_seen)
                elif etype == 'ATTRIB':
                    self._parse_attrib_entity(block, doc, entity_id, text_seen)
                elif etype == 'LINE':
                    self._parse_line_entity(block, doc, entity_id)
                elif etype == 'LWPOLYLINE':
                    self._parse_lwpolyline_entity(block, doc, entity_id)
                elif etype == 'CIRCLE':
                    self._parse_circle_entity(block, doc, entity_id)
                elif etype == 'INSERT':
                    self._parse_insert_entity(block, doc, entity_id)
                elif etype == 'SPLINE':
                    self._parse_spline_entity(block, doc, entity_id)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Individual entity parsers
    # ------------------------------------------------------------------
    def _json_field(self, block: str, *keys: str) -> str | None:
        for key in keys:
            p = f'"{key}":'
            i = block.find(p)
            if i >= 0:
                val_start = i + len(p)
                line_end = block.find('\n', val_start)
                val = block[val_start:line_end].strip().rstrip(',')
                return val
        return None

    def _json_point(self, block: str, key: str) -> tuple[float, float] | None:
        p = f'"{key}":'
        i = block.find(p)
        if i < 0:
            return None
        val_start = block.find('[', i) + 1
        if val_start <= 0:
            return None
        val_end = block.find(']', val_start)
        if val_end < 0:
            return None
        parts = block[val_start:val_end].split(',')
        try:
            return (float(parts[0].strip()), float(parts[1].strip()))
        except (ValueError, IndexError):
            return None

    def _json_points_array(self, block: str, key: str) -> list[tuple[float, float]]:
        p = f'"{key}":'
        i = block.find(p)
        if i < 0:
            return []
        arr_start = block.find('[', i) + 1
        if arr_start <= 0:
            return []
        depth = 0
        arr_end = arr_start
        for j in range(arr_start, len(block)):
            ch = block[j]
            if ch == '[':
                depth += 1
            elif ch == ']':
                if depth == 0:
                    arr_end = j
                    break
                depth -= 1
        arr_raw = block[arr_start:arr_end]
        pts = []
        for m in re.finditer(r'\{\s*"x":\s*([^,]+),\s*"y":\s*([^}]+)\s*\}', arr_raw):
            try:
                pts.append((float(m.group(1)), float(m.group(2))))
            except ValueError:
                pass
        return pts

    def _json_first_float(self, block: str, *keys: str) -> float | None:
        v = self._json_field(block, *keys)
        if v:
            try:
                return float(v.strip())
            except ValueError:
                pass
        return None

    def _json_str(self, block: str, *keys: str) -> str:
        v = self._json_field(block, *keys)
        if v:
            return v.strip().strip('"')
        return ''

    def _parse_text_entity(self, block: str, etype: str, doc: Document,
                           eid: list, text_seen: set) -> None:
        text = self._json_str(block, 'text_value', 'default_value')
        if not text or not text.strip():
            return
        text = text.strip()
        if text in text_seen and len(text) < 60:
            return
        text_seen.add(text) if len(text) < 60 else None
        pt = self._json_point(block, 'ins_pt')
        rot = self._json_first_float(block, 'rotation') or 0.0
        height = self._json_first_float(block, 'height')
        layer = self._json_str(block, 'layer')
        eid[0] += 1
        ent = TextEntity(
            id=f'dwg_txt_{eid[0]}',
            source='dwg', page=1, confidence=1.0,
            text=text,
        )
        ent.custom_fields = {
                'x': pt[0] if pt else None,
                'y': pt[1] if pt else None,
                'rotation': rot,
                'height': height,
                'layer': layer,
            }
        doc.add_entity(ent)

    def _parse_attrib_entity(self, block: str, doc: Document,
                              eid: list, text_seen: set) -> None:
        text = self._json_str(block, 'text_value', 'default_value')
        tag = self._json_str(block, 'tag')
        if not text or not text.strip():
            return
        text = text.strip()
        tag_upper = tag.upper().strip()
        pt = self._json_point(block, 'ins_pt')
        rel = tag_upper in _DWG_TEXT_ATTR_KEYS or any(
            tag_upper.endswith(s) for s in ('NO', 'NAME', 'CODE', 'TYPE')
        )
        if rel or text not in text_seen:
            if len(text) < 60:
                text_seen.add(text)
            eid[0] += 1
            ent = TextEntity(
                id=f'dwg_att_{eid[0]}',
                source='dwg', page=1, confidence=1.0,
                text=text,
            )
            ent.custom_fields = {
                    'attrib_tag': tag_upper,
                    'x': pt[0] if pt else None,
                    'y': pt[1] if pt else None,
                }
            doc.add_entity(ent)

    def _parse_line_entity(self, block: str, doc: Document, eid: list) -> None:
        start = self._json_point(block, 'start')
        end = self._json_point(block, 'end')
        if not start or not end:
            return
        layer = self._json_str(block, 'layer')
        eid[0] += 1
        doc.add_entity(LineEntity(
            id=f'dwg_line_{eid[0]}',
            source='dwg', page=1, confidence=1.0,
            layer=layer,
            points=[Point(start[0], start[1]), Point(end[0], end[1])],
        ))

    def _parse_lwpolyline_entity(self, block: str, doc: Document, eid: list) -> None:
        pts = self._json_points_array(block, 'points')
        if not pts:
            return
        layer = self._json_str(block, 'layer')
        eid[0] += 1
        point_objs = [Point(x, y) for x, y in pts]
        if len(pts) == 2:
            doc.add_entity(LineEntity(
                id=f'dwg_line_{eid[0]}',
                source='dwg', page=1, confidence=1.0,
                layer=layer, points=point_objs,
            ))
        else:
            doc.add_entity(PolylineEntity(
                id=f'dwg_poly_{eid[0]}',
                source='dwg', page=1, confidence=1.0,
                layer=layer, points=point_objs,
            ))

    def _parse_circle_entity(self, block: str, doc: Document, eid: list) -> None:
        center = self._json_point(block, 'center')
        radius = self._json_first_float(block, 'radius')
        if not center or not radius:
            return
        layer = self._json_str(block, 'layer')
        eid[0] += 1
        pts = []
        for angle_deg in range(0, 360, 10):
            rad = angle_deg * 3.14159 / 180
            pts.append(Point(
                center[0] + radius * __import__('math').cos(rad),
                center[1] + radius * __import__('math').sin(rad),
            ))
        doc.add_entity(PolylineEntity(
            id=f'dwg_circle_{eid[0]}',
            source='dwg', page=1, confidence=1.0,
            layer=layer, points=pts,
        ))

    def _parse_insert_entity(self, block: str, doc: Document, eid: list) -> None:
        name = self._json_str(block, 'name')
        pt = self._json_point(block, 'ins_pt')
        rotation = self._json_first_float(block, 'rotation') or 0.0
        layer = self._json_str(block, 'layer')

        if not name and pt and abs(pt[0]) < 2 and abs(pt[1]) < 2:
            return

        eid[0] += 1
        syment = SymbolEntity(
            id=f'dwg_ins_{eid[0]}',
            source='dwg', page=1, confidence=1.0,
            layer=layer, name=name,
        )
        syment.custom_fields = {
            'insert_x': pt[0] if pt else None,
            'insert_y': pt[1] if pt else None,
            'rotation': rotation,
        }
        doc.add_entity(syment)

    def _parse_spline_entity(self, block: str, doc: Document, eid: list) -> None:
        pts = self._json_points_array(block, 'control_points', 'fit_pts')
        if not pts:
            return
        layer = self._json_str(block, 'layer')
        eid[0] += 1
        doc.add_entity(PolylineEntity(
            id=f'dwg_spline_{eid[0]}',
            source='dwg', page=1, confidence=1.0,
            layer=layer,
            points=[Point(x, y) for x, y in pts],
        ))

    # ------------------------------------------------------------------
    # ezdxf entity consumer (fallback for clean DXF files)
    # ------------------------------------------------------------------
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
            text = (e.plain_text() if typ == 'MTEXT' else e.dxf.text) or ''
            doc.add_entity(TextEntity(
                id=handle, source='dwg', page=1, confidence=1.0,
                layer=layer, text=text,
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
                        layer=layer, text=val.strip(),
                        custom_fields={
                            'attrib_tag': tag_upper,
                            'insert_x': float(x), 'insert_y': float(y),
                            'block': name,
                        },
                    ))


_register_suffix(DWGLoader, '.dwg')
_register_suffix(DWGLoader, '.dxf')

__all__ = ['DWGLoader']
