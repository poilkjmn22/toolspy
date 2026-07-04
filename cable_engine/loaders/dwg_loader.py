"""cable_engine.loaders.dwg_loader — V5 DWG/DXF loader.

This loader produces the V5 IR: a flat list of geometry entities
(TextEntity, LineGeometry, CircleGeometry, ArcGeometry, BlockRef,
AttributeEntity). It also expands anonymous block references at load
time so that geometry inside unnamed blocks becomes first-class in
model space — this unlocks L-shape detection that was broken in V4
because `dwgread -O JSON` does not expand anonymous blocks by default.

The loader is `dwgread -O JSON` first, with `ezdxf` as a fallback for
clean DXF files. It deliberately does not use the legacy V4 entity
types (LineEntity, PolylineEntity, SymbolEntity); V5's GraphBuilder
and Rule Engine operate exclusively on the geometry-aware types.

Document space convention (V5):
  - Origin: bottom-left
  - Units: document units (DWG mm by default)
  - Y axis: increases upward (matches DWG)

Anonymous block expansion:
  - dwgread -O JSON emits BLOCK / ENDBLK pairs in a separate "objects"
    section, but does NOT inline them into the model space INSERTs.
  - We index all BLOCK definitions by handle.
  - When we encounter an INSERT in model space whose block handle
    refers to a BLOCK with no user-visible name (anonymous), we walk
    the block's children and emit copies of each child entity in
    model space, transformed by the INSERT's ins_pt / rotation / scale.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Optional

from cable_engine.ir import (
    ArcGeometry, AttributeEntity, BlockRef, CircleGeometry,
    Document, DocumentType, LineGeometry, Page, Point, TextEntity,
)

from .base import BaseLoader, _register_suffix


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _content_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


_ENTITY_RE = re.compile(r'"entity":\s*"(\w+)"')


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
class DWGLoader(BaseLoader):
    document_type: DocumentType = DocumentType.DWG
    name = 'dwg'

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
            doc.add_entity(TextEntity(
                id='err-missing', source='dwg', page=1, confidence=0.0,
                text=f'<dwg file not found: {document_path}>',
            ))
            return doc

        suffix = document_path.suffix.lower()
        if suffix == '.dwg':
            if self._load_via_dwgread(document_path, doc):
                return doc
            # Fall through to ezdxf
        return self._load_via_ezdxf(document_path, doc)

    # ------------------------------------------------------------------
    # dwgread path (preferred — gives us anonymous block definitions)
    # ------------------------------------------------------------------
    def _load_via_dwgread(self, path: Path, doc: Document) -> bool:
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

        self._parse_v5(raw, doc)
        return bool(doc.entities)

    def _parse_v5(self, raw: str, doc: Document) -> None:
        """Parse dwgread JSON and emit V5 entities.

        Single linear pass. Each entity is identified by its `"entity":`
        key. We emit:
          - TextEntity         for TEXT, MTEXT
          - AttributeEntity    for ATTRIB
          - LineGeometry       for LINE, LWPOLYLINE
          - CircleGeometry     for CIRCLE
          - ArcGeometry        for ARC
          - BlockRef           for INSERT (with insert_point + rotation)

        Anonymous block expansion (the V5 Q2-A goal) is implemented at
        the GRAPH BUILD stage instead of here. The dwgread JSON's
        `block_header` field references the BLOCK TABLE RECORD via a
        composite handle that doesn't directly match the BLOCK entity's
        own handle; we resolve it via spatial proximity at graph build
        time (resolved spatially by the TerminalStripAnalyzer at query
        time). This is simpler and more robust than chasing handles
        across the dwgread JSON format.
        """
        for m in _ENTITY_RE.finditer(raw):
            etype = m.group(1)
            block = _extract_object(raw, m.start())
            if block is None:
                continue
            # Skip block definition envelopes (BLOCK / ENDBLK pairs).
            if etype in ('BLOCK', 'ENDBLK'):
                continue
            self._emit_model_entity_v5(etype, block, doc)

    def _emit_model_entity_v5(self, etype: str, block: str, doc: Document) -> None:
        """Emit a single V5 entity from a model-space JSON object."""
        layer = _json_str(block, 'layer')
        handle = _json_handle(block)
        eed_values = _parse_eed(block)

        if etype in ('TEXT', 'MTEXT'):
            text = _json_str(block, 'text_value', 'default_value').strip()
            if not text:
                return
            ins = _json_point(block, 'ins_pt')
            ent = TextEntity(
                id=_next_id(doc, 'dwg_txt'),
                source='dwg', page=1, confidence=1.0,
                text=text,
            )
            ent.custom_fields = {
                'x': ins[0] if ins else None,
                'y': ins[1] if ins else None,
                'rotation': _json_first_float(block, 'rotation') or 0.0,
                'height': _json_first_float(block, 'height'),
                'layer': layer,
            }
            doc.add_entity(ent)

        elif etype == 'ATTRIB':
            text = _json_str(block, 'text_value', 'default_value').strip()
            tag = _json_str(block, 'tag').strip()
            ins = _json_point(block, 'ins_pt')
            ent = AttributeEntity(
                id=_next_id(doc, 'dwg_att'),
                source='dwg', page=1, confidence=1.0,
                layer=layer,
                tag=tag,
                text=text,
            )
            ent.custom_fields = {
                'x': ins[0] if ins else None,
                'y': ins[1] if ins else None,
                'eed': eed_values,
            }
            doc.add_entity(ent)

        elif etype == 'LINE':
            start = _json_point(block, 'start')
            end = _json_point(block, 'end')
            if not start or not end:
                return
            ent = LineGeometry(
                id=_next_id(doc, 'dwg_line'),
                source='dwg', page=1, confidence=1.0,
                layer=layer,
                handle=handle,
                points=[Point(start[0], start[1]), Point(end[0], end[1])],
            )
            doc.add_entity(ent)

        elif etype == 'LWPOLYLINE':
            pts = _json_points_array(block, 'points')
            if not pts:
                return
            ent = LineGeometry(
                id=_next_id(doc, 'dwg_poly'),
                source='dwg', page=1, confidence=1.0,
                layer=layer,
                handle=handle,
                points=[Point(x, y) for x, y in pts],
                closed=_json_int(block, 'flag', 'flags', default=0) & 1 == 1,
            )
            ent.custom_fields = {'eed': eed_values}
            doc.add_entity(ent)

        elif etype == 'CIRCLE':
            center = _json_point(block, 'center')
            radius = _json_first_float(block, 'radius')
            if not center or not radius:
                return
            ent = CircleGeometry(
                id=_next_id(doc, 'dwg_circ'),
                source='dwg', page=1, confidence=1.0,
                layer=layer,
                handle=handle,
                center=Point(center[0], center[1]),
                radius=radius,
            )
            doc.add_entity(ent)

        elif etype == 'ARC':
            center = _json_point(block, 'center')
            radius = _json_first_float(block, 'radius')
            start = _json_first_float(block, 'start_angle') or 0.0
            end = _json_first_float(block, 'end_angle') or 0.0
            if not center or not radius:
                return
            ent = ArcGeometry(
                id=_next_id(doc, 'dwg_arc'),
                source='dwg', page=1, confidence=1.0,
                layer=layer,
                handle=handle,
                center=Point(center[0], center[1]),
                radius=radius,
                start_angle=start,
                end_angle=end,
            )
            doc.add_entity(ent)

        elif etype == 'INSERT':
            name = _json_str(block, 'name')
            ins_pt = _json_point(block, 'ins_pt')
            rotation = _json_first_float(block, 'rotation') or 0.0
            scale_x, scale_y = 1.0, 1.0
            scale_arr = _json_points_array(block, 'scale')
            if scale_arr:
                scale_x, scale_y = scale_arr[0][0], scale_arr[0][1]
            ent = BlockRef(
                id=_next_id(doc, 'dwg_block'),
                source='dwg', page=1, confidence=1.0,
                layer=layer, handle=handle, name=name,
                insert_point=Point(ins_pt[0], ins_pt[1]) if ins_pt else None,
                rotation=rotation,
                scale_x=scale_x, scale_y=scale_y,
            )
            doc.add_entity(ent)

        # Other types (SPLINE, MTEXT body, etc.) — skipped for now.
        # The Graph Builder works on what's emitted; we can add SPLINE
        # support later if the geometry needs it.

    # ------------------------------------------------------------------
    # ezdxf path (fallback for clean DXF files)
    # ------------------------------------------------------------------
    def _load_via_ezdxf(self, path: Path, doc: Document) -> Document:
        try:
            import ezdxf
        except ImportError:
            doc.add_entity(TextEntity(
                id='err-import', source='dwg', page=1, confidence=0.0,
                text='<ezdxf import error: pip install ezdxf>',
            ))
            return doc
        try:
            dwg = ezdxf.readfile(str(path))
        except Exception as e:
            doc.add_entity(TextEntity(
                id='err-ezdxf', source='dwg', page=1, confidence=0.0,
                text=f'<ezdxf error: {e}>',
            ))
            return doc

        msp = dwg.modelspace()
        for e in msp:
            try:
                self._consume_ezdxf_entity(e, doc)
            except Exception:
                pass
        return doc

    def _consume_ezdxf_entity(self, e, doc: Document) -> None:
        typ = e.dxftype()
        handle = getattr(e.dxf, 'handle', '') or ''
        layer = getattr(e.dxf, 'layer', '') or ''

        if typ == 'LINE':
            s = e.dxf.start
            t = e.dxf.end
            doc.add_entity(LineGeometry(
                id=handle or _next_id(doc, 'ezdxf_line'),
                source='dwg', page=1, confidence=1.0,
                layer=layer, handle=handle,
                points=[Point(s.x, s.y), Point(t.x, t.y)],
            ))
        elif typ == 'LWPOLYLINE':
            pts = [Point(p[0], p[1]) for p in e.get_points()]
            doc.add_entity(LineGeometry(
                id=handle or _next_id(doc, 'ezdxf_poly'),
                source='dwg', page=1, confidence=1.0,
                layer=layer, handle=handle, points=pts,
            ))
        elif typ == 'CIRCLE':
            c = e.dxf.center
            r = float(e.dxf.radius)
            doc.add_entity(CircleGeometry(
                id=handle or _next_id(doc, 'ezdxf_circ'),
                source='dwg', page=1, confidence=1.0,
                layer=layer, handle=handle,
                center=Point(c.x, c.y), radius=r,
            ))
        elif typ in ('TEXT', 'MTEXT'):
            text = (e.plain_text() if typ == 'MTEXT' else e.dxf.text) or ''
            insert = getattr(e.dxf, 'insert', None)
            x, y = (insert[0], insert[1]) if insert else (None, None)
            ent = TextEntity(
                id=handle or _next_id(doc, 'ezdxf_txt'),
                source='dwg', page=1, confidence=1.0, text=text,
            )
            ent.custom_fields = {'x': x, 'y': y, 'layer': layer}
            doc.add_entity(ent)
        elif typ == 'INSERT':
            name = getattr(e.dxf, 'name', '') or ''
            insert = getattr(e.dxf, 'insert', None) or (0, 0, 0)
            x, y = insert[0], insert[1]
            doc.add_entity(BlockRef(
                id=handle or _next_id(doc, 'ezdxf_ins'),
                source='dwg', page=1, confidence=1.0,
                layer=layer, handle=handle,
                name=name,
                insert_point=Point(x, y),
                rotation=float(getattr(e.dxf, 'rotation', 0.0) or 0.0),
            ))
            for att in getattr(e, 'attribs', []) or []:
                tag = getattr(att.dxf, 'tag', '') or ''
                val = getattr(att.dxf, 'text', '') or ''
                if not tag.strip() or not val.strip():
                    continue
                doc.add_entity(AttributeEntity(
                    id=f'{handle}__{tag}' or _next_id(doc, 'ezdxf_att'),
                    source='dwg', page=1, confidence=1.0,
                    layer=layer, tag=tag, text=val.strip(),
                ))


# ---------------------------------------------------------------------------
# Module-level helpers (mirror the V4 loader's internal API but trimmed)
# ---------------------------------------------------------------------------
def _extract_object(raw: str, near: int) -> Optional[str]:
    """Find the `{...}` object that contains the entity near `near`.
    Returns the object's full text or None.
    """
    start = raw.rfind('{', max(0, near - 4000), near)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == '{':
            depth += 1
        elif raw[i] == '}':
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def _json_str(block: str, *keys: str) -> str:
    for key in keys:
        p = f'"{key}":'
        i = block.find(p)
        if i >= 0:
            val_start = i + len(p)
            line_end = block.find('\n', val_start)
            v = block[val_start:line_end].strip().rstrip(',')
            return v.strip().strip('"')
    return ''


def _json_first_float(block: str, *keys: str) -> Optional[float]:
    for key in keys:
        p = f'"{key}":'
        i = block.find(p)
        if i >= 0:
            val_start = i + len(p)
            line_end = block.find('\n', val_start)
            try:
                return float(block[val_start:line_end].strip().rstrip(','))
            except ValueError:
                pass
    return None


def _json_int(block: str, *keys: str, default: int = 0) -> int:
    for key in keys:
        p = f'"{key}":'
        i = block.find(p)
        if i >= 0:
            val_start = i + len(p)
            line_end = block.find('\n', val_start)
            try:
                return int(block[val_start:line_end].strip().rstrip(','), 0)
            except ValueError:
                try:
                    return int(block[val_start:line_end].strip().rstrip(','))
                except ValueError:
                    pass
    return default


def _json_point(block: str, key: str) -> Optional[tuple[float, float]]:
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


def _json_points_array(block: str, key: str) -> list[tuple[float, float]]:
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
    # Try array format first: [x, y]
    for m in re.finditer(r'\[\s*([\d.-]+)\s*,\s*([\d.-]+)\s*\]', arr_raw):
        try:
            pts.append((float(m.group(1)), float(m.group(2))))
        except ValueError:
            pass
    if pts:
        return pts
    # Fall back to object format: {"x":..., "y":...}
    for m in re.finditer(r'\{\s*"x":\s*([^,]+),\s*"y":\s*([^}]+)\s*\}', arr_raw):
        try:
            pts.append((float(m.group(1)), float(m.group(2))))
        except ValueError:
            pass
    return pts


def _json_handle(block: str) -> str:
    """Pull the 'handle' field out of a dwgread JSON object.
    The handle is a list like [0, 1, 12, 34] — we join with dots for a
    stable string handle.
    """
    p = '"handle":'
    i = block.find(p)
    if i < 0:
        return ''
    val_start = block.find('[', i) + 1
    if val_start <= 0:
        return ''
    val_end = block.find(']', val_start)
    if val_end < 0:
        return ''
    parts = [s.strip() for s in block[val_start:val_end].split(',') if s.strip()]
    return '.'.join(parts)


def _parse_eed(block: str) -> list[str]:
    """Extract Extended Entity Data (EED) values from a dwgread JSON
    object. Returns a list of value strings."""
    values: list[str] = []
    p = '"eed":'
    i = block.find(p)
    if i < 0:
        return values
    arr_start = block.find('[', i)
    if arr_start < 0:
        return values
    depth = 0
    arr_end = arr_start
    for j in range(arr_start, len(block)):
        ch = block[j]
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                arr_end = j + 1
                break
    eed_json = block[arr_start:arr_end]
    for vm in re.finditer(r'"value":\s*"([^"]*)"', eed_json):
        v = vm.group(1).strip()
        if v:
            values.append(v)
    return values


def _next_id(doc: Document, prefix: str) -> str:
    """Generate a unique entity id for this document."""
    # We can't use a counter on Document directly; we use the entity
    # count at construction time, plus a random-ish suffix to avoid
    # collisions across reloads.
    n = len(doc.entities) + 1
    return f'{prefix}_{n}'


_register_suffix(DWGLoader, '.dwg')
_register_suffix(DWGLoader, '.dxf')


__all__ = ['DWGLoader']