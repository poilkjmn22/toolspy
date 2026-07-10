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
import json
import math
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

        Three-pass approach:
          1. Build an ltype-handle → ltype-name map from all LTYPE objects.
          2. Build a BLOCK_HEADER entity map to determine which entities are
             inside block definitions (via ownerhandle → BLOCK_HEADER).
          3. Linear pass over `"entity"` rows. Model-space entities are
             emitted directly. Entities belonging to any BLOCK_HEADER are
             buffered by BLOCK_HEADER handle, NOT emitted.
             INSERT entities pick up their block name from a pre-built
             block_header → block_name lookup (dwgread JSON does not
             include `"name"` in INSERT entities).
          4. Post-pass: for each INSERT (BlockRef) referencing an anonymous
             block, resolve its BLOCK_HEADER and expand the buffered
             entities into model space with coordinates transformed by the
             INSERT's ins_pt / rotation / scale and the BLOCK_HEADER's
             base_pt offset.
        """
        ltype_map = _build_ltype_map(raw)
        block_name_map = _build_block_header_name_map(raw)

        # Build BLOCK_HEADER entity map to know which entities are block-internal
        bh_entity_map = _build_block_header_entity_map(raw)
        block_internal_handles: set[int] = set()
        entity_to_bh: dict[int, int] = {}
        for bh_handle, bh_data in bh_entity_map.items():
            bh_name = bh_data['name']
            if bh_name in ('*Model_Space', '*Paper_Space'):
                continue
            for h in bh_data['entities']:
                block_internal_handles.add(h)
                entity_to_bh[h] = bh_handle

        # Phase 2: buffer anonymous block entities, emit model-space entities
        block_buffers: dict[int, list[tuple[str, str]]] = {}

        for m in _ENTITY_RE.finditer(raw):
            etype = m.group(1)

            if etype in ('BLOCK', 'ENDBLK'):
                continue

            block = _extract_object(raw, m.start())
            if block is None:
                continue

            # Inject block name into INSERT JSON for model-space entities
            if etype == 'INSERT' and block_name_map:
                try:
                    blk_obj = json.loads(block)
                    bh = blk_obj.get('block_header', [])
                    if len(bh) >= 3:
                        bbytes = bh[1]
                        handle_key = tuple(bh[2:2 + bbytes])
                        if handle_key in block_name_map:
                            blk_obj['name'] = block_name_map[handle_key]
                            block = json.dumps(blk_obj, indent=2)
                except (json.JSONDecodeError, IndexError):
                    pass

            # Check if this entity belongs to a block definition
            handle = _json_handle_value(block)
            if handle in block_internal_handles:
                bh_handle = entity_to_bh[handle]
                block_buffers.setdefault(bh_handle, []).append((etype, block))
                continue

            self._emit_model_entity_v5(etype, block, doc, ltype_map)

        # Phase 3: expand anonymous block references via BLOCK_HEADER
        inserts: list[BlockRef] = [
            e for e in doc.entities
            if isinstance(e, BlockRef) and e.name
            and e.name.startswith('*U') and e.insert_point is not None
        ]

        # Build reverse mapping: BLOCK entity handle → BLOCK_HEADER handle
        be_to_bh: dict[int, int] = {}
        for bh_handle, bh_data in bh_entity_map.items():
            be = bh_data['block_entity_handle']
            if be:
                be_to_bh[be] = bh_handle

        block_name_to_handle = _build_block_name_handle_map(raw)

        for ins in inserts:
            blk_handle = block_name_to_handle.get(ins.name, 0)
            bh_handle = be_to_bh.get(blk_handle, 0)
            if not bh_handle:
                continue
            buf = block_buffers.get(bh_handle, [])
            if not buf:
                continue
            base_pt = bh_entity_map[bh_handle]['base_pt']
            self._emit_anonymous_block(ins, buf, doc, ltype_map, base_pt)

    def _emit_anonymous_block(
        self,
        ins: BlockRef,
        buf: list[tuple[str, str]],
        doc: Document,
        ltype_map: dict[str, str],
        base_pt: list[float] | None = None,
    ) -> None:
        """Expand one anonymous block reference into model-space entities.

        The BLOCK_HEADER's base_pt defines the block's origin. Block-local
        entity coordinates are relative to this base_pt, so we subtract it
        before applying the INSERT transform.
        """
        ix = ins.insert_point.x
        iy = ins.insert_point.y
        cos_r = math.cos(ins.rotation)
        sin_r = math.sin(ins.rotation)
        sx = ins.scale_x
        sy = ins.scale_y
        bx0 = base_pt[0] if base_pt else 0.0
        by0 = base_pt[1] if base_pt else 0.0

        def tx(bx: float, by: float) -> tuple[float, float]:
            dx = bx - bx0
            dy = by - by0
            mx = ix + dx * sx * cos_r - dy * sy * sin_r
            my = iy + dx * sx * sin_r + dy * sy * cos_r
            return (mx, my)

        for etype, orig_block in buf:
            self._emit_transformed_block(etype, orig_block, doc, ltype_map, tx)

    def _emit_transformed_block(
        self,
        etype: str,
        orig_block: str,
        doc: Document,
        ltype_map: dict[str, str],
        tx: object,  # Callable[[float, float], tuple[float, float]]
    ) -> None:
        """Emit one block-internal entity with coordinates transformed."""
        if etype in ('INSERT', 'BLOCK', 'ENDBLK', 'ATTDEF'):
            return

        obj = json.loads(orig_block)

        def transform_point(bx: float, by: float) -> list[float]:
            mx, my = tx(bx, by)  # type: ignore[operator]
            return [mx, my]

        def transform_dict_point(p: dict) -> None:
            if 'x' in p and 'y' in p:
                mx, my = tx(float(p['x']), float(p['y']))
                p['x'] = mx
                p['y'] = my

        for key in ('ins_pt', 'start', 'end', 'center'):
            val = obj.get(key)
            if isinstance(val, list) and len(val) >= 2:
                obj[key] = transform_point(float(val[0]), float(val[1]))

        if 'points' in obj:
            pts = obj['points']
            for i, pt in enumerate(pts):
                if isinstance(pt, list) and len(pt) >= 2:
                    pts[i] = transform_point(float(pt[0]), float(pt[1]))
                elif isinstance(pt, dict):
                    transform_dict_point(pt)

        new_block = json.dumps(obj, indent=2)
        self._emit_model_entity_v5(etype, new_block, doc, ltype_map)

    def _emit_model_entity_v5(
        self, etype: str, block: str, doc: Document,
        ltype_map: Optional[dict[str, str]] = None,
    ) -> None:
        """Emit a single V5 entity from a model-space JSON object."""
        layer = _json_str(block, 'layer')
        handle = _json_handle(block)
        eed_values = _parse_eed(block)
        if ltype_map is None:
            ltype_map = {}

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
            _maybe_set_ltype(ent, block, ltype_map)
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
            _maybe_set_ltype(ent, block, ltype_map)
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


# ---------------------------------------------------------------------------
# Linetype helpers (V6.6 — cabinet boundary detection needs to know which
# lines are dashed / hidden). dwgread stores linetypes as standalone objects
# (`"object": "LTYPE"` with `"name": "ACAD_ISO10W100"` etc.) plus a separate
# LTYPE_CONTROL object whose `entries` field maps name handles to LTYPE
# object handles. Entities (LINE / LWPOLYLINE) reference their linetype via
# the `"ltype"` field — a handle array like `[5,2,808,808]` — together with
# `"ltype_flags"` (0 = BYLAYER, 1 = BYBLOCK, 2 = continuous, 3 = explicit).
# ---------------------------------------------------------------------------
def _json_handle_array(block: str, key: str) -> Optional[str]:
    """Read a JSON `[a, b, c, d]` array field from `block` and return it
    as a dot-joined string (e.g. `"5.2.808.808"`). Returns None if the
    field is missing or unparsable."""
    p = f'"{key}":'
    i = block.find(p)
    if i < 0:
        return None
    arr_start = block.find('[', i)
    if arr_start < 0:
        return None
    arr_end = block.find(']', arr_start)
    if arr_end < 0:
        return None
    parts = [s.strip() for s in block[arr_start+1:arr_end].split(',')]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return '.'.join(parts)


def _build_ltype_map(raw: str) -> dict[str, str]:
    """Walk the dwgread JSON once to collect every LTYPE object and
    return a ltype-handle-dot-string → ltype-name map keyed by the SAME
    format entities use in their `"ltype"` field.

    dwgread quirk: LTYPE objects emit `"handle": [a, b, c]` (3-element)
    while entity references use `"ltype": [a, b, c, d]` (4-element),
    so we can't trivially key on bytes. Instead we walk them in the
    order dwgread emits them (which matches the LTYPE_CONTROL.entries
    / byblock / bylayer order) and pair entries to objects
    positionally. The LTYPE_CONTROL.entries arrays are 4-element, so
    they DO match what entities store — and we use them as the keys.
    """
    # 1. Read LTYPE objects (ordered) — carry (dot_handle_3, name).
    ltype_objects: list[tuple[str, str]] = []
    obj_re = re.compile(r'"object":\s*"LTYPE"')
    for m in obj_re.finditer(raw):
        start = raw.rfind('{', 0, m.start())
        if start < 0:
            continue
        depth = 0
        end = None
        for j in range(start, len(raw)):
            if raw[j] == '{':
                depth += 1
            elif raw[j] == '}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            continue
        block = raw[start:end + 1]
        handle_arr = _json_handle_array(block, 'handle')
        name = _json_str(block, 'name')
        if handle_arr and name:
            ltype_objects.append((handle_arr, name))

    if not ltype_objects:
        return {}

    # 2. Read LTYPE_CONTROL's reference list in the same order dwgread
    #    emits LTYPE objects: byblock, bylayer, then entries[0..n-1].
    ctl_re = re.compile(r'"object":\s*"LTYPE_CONTROL"')
    cm = ctl_re.search(raw)
    ref_handles: list[str] = []
    if cm:
        ctl_start = raw.rfind('{', 0, cm.start())
        if ctl_start >= 0:
            depth = 0
            ctl_end = None
            for j in range(ctl_start, len(raw)):
                if raw[j] == '{':
                    depth += 1
                elif raw[j] == '}':
                    depth -= 1
                    if depth == 0:
                        ctl_end = j
                        break
            if ctl_end is not None:
                ctl_block = raw[ctl_start:ctl_end + 1]
                for key in ('byblock', 'bylayer'):
                    h = _json_handle_array(ctl_block, key)
                    if h:
                        ref_handles.append(h)
                # entries: JSON array of [a,b,c,d] handle refs.
                # Parse the entire substring between outer brackets
                # by walking character-by-character.
                entries_idx = ctl_block.find('"entries":')
                if entries_idx >= 0:
                    # Find the opening '[' of the entries array (skip
                    # through optional whitespace).
                    j = entries_idx
                    while j < len(ctl_block) and ctl_block[j] != '[':
                        j += 1
                    if j < len(ctl_block):
                        entries_open = j
                        # Walk to matching close bracket.
                        depth = 0
                        entries_close = -1
                        for k in range(entries_open, len(ctl_block)):
                            ch = ctl_block[k]
                            if ch == '[':
                                depth += 1
                            elif ch == ']':
                                depth -= 1
                                if depth == 0:
                                    entries_close = k
                                    break
                        if entries_close > entries_open:
                            # Parse the inner array content.
                            inner_content = ctl_block[entries_open + 1:entries_close]
                            depth = 0
                            cur: list[int] = []
                            for ch in inner_content:
                                if ch == '[':
                                    depth += 1
                                    cur = []
                                elif ch == ']':
                                    if cur:
                                        ref_handles.append('.'.join(str(x) for x in cur))
                                    cur = []
                                    depth -= 1
                                elif ch == ',' or ch.isspace():
                                    continue
                                elif ch.isdigit() or (ch == '-' and not cur):
                                    # Start a new int (negative allowed)
                                    num_start = len(cur)
                                    pass
                                else:
                                    pass
                            # Simpler: extract just numbers between brackets
                            depth = 0
                            current_digits = ''
                            for ch in inner_content:
                                if ch == '[':
                                    depth += 1
                                    current_digits = ''
                                elif ch == ']':
                                    if depth == 1 and current_digits:
                                        # current_digits may have multiple numbers
                                        nums = re.findall(r'-?\d+', current_digits)
                                        if nums:
                                            ref_handles.append('.'.join(nums))
                                    depth -= 1
                                    current_digits = ''
                                else:
                                    if depth == 1:
                                        current_digits += ch

    # 3. Pair LTYPE objects to ref handles positionally.
    out: dict[str, str] = {}
    for i, ref_h in enumerate(ref_handles):
        if i >= len(ltype_objects):
            break
        _, name = ltype_objects[i]
        out[ref_h] = name
    return out


def _build_block_header_name_map(raw: str) -> dict[tuple[int, ...], str]:
    """Build a map from INSERT.block_header handle-value → block name.

    dwgread JSON does not include a `"name"` key in INSERT entities.
    Instead each INSERT carries a `"block_header"` handle reference
    (e.g. `[5, 2, 990, 990]`). We resolve it via the BLOCK_CONTROL
    object, whose `entries` array lists the same handles positioned in
    the same order as the corresponding BLOCK entities appear in the
    JSON output (after in-model-space *Model_Space / *Paper_Space).

    Returns a dict keyed by the trailing handle bytes (e.g. `(990, 990)`)
    so callers can look up `block_header[2:2+block_header[1]]`.
    """
    # 1. Read BLOCK_CONTROL entries.
    obj_re = re.compile(r'"object":\s*"BLOCK_CONTROL"')
    entries: list[list[int]] = []
    for m in obj_re.finditer(raw):
        start = raw.rfind('{', 0, m.start())
        if start < 0:
            continue
        depth = 0
        end = None
        for j in range(start, len(raw)):
            if raw[j] == '{':
                depth += 1
            elif raw[j] == '}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            continue
        obj = json.loads(raw[start:end + 1])
        entries = obj.get('entries', [])
        break

    if not entries:
        return {}

    # 2. Collect BLOCK names in appearance order, skipping *Model_Space
    #    and *Paper_Space (which the `entries` array does NOT reference).
    block_names: list[str] = []
    for m in _ENTITY_RE.finditer(raw):
        if m.group(1) != 'BLOCK':
            continue
        start = raw.rfind('{', 0, m.start())
        if start < 0:
            continue
        depth = 0
        end = None
        for j in range(start, len(raw)):
            if raw[j] == '{':
                depth += 1
            elif raw[j] == '}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            continue
        obj = json.loads(raw[start:end + 1])
        name = obj.get('name', '')
        if name not in ('*Model_Space', '*Paper_Space'):
            block_names.append(name)

    # 3. Pair entries to block names positionally, keyed by handle
    #    value (last `bytes` elements from each 4-element entry).
    out: dict[tuple[int, ...], str] = {}
    for i, entry in enumerate(entries):
        if len(entry) < 3:
            continue
        ebytes = entry[1]
        handle_key = tuple(entry[2:2 + ebytes])
        if i < len(block_names):
            out[handle_key] = block_names[i]
    return out


def _json_handle_value(block: str) -> int:
    """Extract the numeric handle value (last element of handle array) from a JSON block string."""
    p = '"handle":'
    i = block.find(p)
    if i < 0:
        return 0
    val_start = block.find('[', i)
    if val_start < 0:
        return 0
    val_end = block.find(']', val_start)
    if val_end < 0:
        return 0
    parts = block[val_start + 1:val_end].split(',')
    try:
        return int(parts[-1].strip())
    except (ValueError, IndexError):
        return 0


def _build_block_header_entity_map(raw: str) -> dict[int, dict]:
    """Build a map from BLOCK_HEADER handle to its entity metadata.

    dwgread JSON stores block-internal entities in the OBJECTS array with
    an `ownerhandle` pointing to the BLOCK_HEADER, NOT nested between
    BLOCK/ENDBLK markers. This function reads those BLOCK_HEADER objects
    and returns each handle's entity list.

    Returns: {handle: {entities: [int, ...], hasattrs: bool,
                       base_pt: [x,y,z], block_entity_handle: int, name: str}}
    """
    out: dict[int, dict] = {}
    obj_re = re.compile(r'"object":\s*"BLOCK_HEADER"')
    for m in obj_re.finditer(raw):
        start = raw.rfind('{', max(0, m.start() - 4000), m.start())
        if start < 0:
            continue
        depth = 0
        end = None
        for j in range(start, len(raw)):
            if raw[j] == '{':
                depth += 1
            elif raw[j] == '}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            continue
        block = raw[start:end + 1]
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        handle_arr = obj.get('handle', [])
        if len(handle_arr) < 3:
            continue
        bh_handle = handle_arr[-1]

        entity_handles: list[int] = []
        for ent_ref in obj.get('entities', []):
            if isinstance(ent_ref, list) and ent_ref:
                entity_handles.append(ent_ref[-1])

        be_ref = obj.get('block_entity', [])
        be_handle = be_ref[-1] if isinstance(be_ref, list) and be_ref else 0

        out[bh_handle] = {
            'entities': entity_handles,
            'hasattrs': bool(obj.get('hasattrs', 0)),
            'base_pt': obj.get('base_pt', [0, 0, 0]),
            'block_entity_handle': be_handle,
            'name': obj.get('name', ''),
        }
    return out


def _build_block_name_handle_map(raw: str) -> dict[str, int]:
    """Build a map from BLOCK entity name to its handle value.

    Used in Phase 3 to resolve an INSERT's block name (e.g. '*U28') to
    the BLOCK entity's handle, which is then mapped to a BLOCK_HEADER.
    """
    out: dict[str, int] = {}
    for m in _ENTITY_RE.finditer(raw):
        if m.group(1) != 'BLOCK':
            continue
        block = _extract_object(raw, m.start())
        if block is None:
            continue
        try:
            obj = json.loads(block)
            name = obj.get('name', '')
            handle_arr = obj.get('handle', [])
            if name and len(handle_arr) >= 3:
                out[name] = handle_arr[-1]
        except json.JSONDecodeError:
            pass
    return out


def _maybe_set_ltype(
    ent: LineGeometry,
    block: str,
    ltype_map: dict[str, str],
) -> None:
    """If `block` carries a non-default linetype, attach the resolved
    ltype name + raw flags onto `ent.custom_fields`.

    The dwgread JSON shape is roughly:
        "ltype": [5, 2, 808, 808],   // handle (type-code + bytes)
        "ltype_flags": 3              // 0 BYLAYER | 1 BYBLOCK | 3 explicit

    `ltype_flags` in {2, 3} means the entity references an explicit
    linetype. We look up the handle in `ltype_map`, but the dwgread
    type-code byte at the start of the array varies between entity
    references and LTYPE_CONTROL.entries (e.g. [5,2,...] vs [2,2,...]),
    so we compare on the trailing bytes too.
    """
    flags = _json_int(block, 'ltype_flags', default=0)
    handle_arr = _json_handle_array(block, 'ltype')
    if not (handle_arr and flags in (2, 3)):
        return
    # Exact-match first, then fall back to a trailing-bytes match
    # (drop the type-code byte at the front of each 4-element form).
    ltype_name = ltype_map.get(handle_arr, '')
    if not ltype_name:
        def _tail(h: str) -> tuple:
            parts = h.split('.')
            return tuple(int(p) for p in parts[1:])
        target = _tail(handle_arr)
        for k, v in ltype_map.items():
            if _tail(k) == target:
                ltype_name = v
                break
    if not ltype_name:
        return
    cf = getattr(ent, 'custom_fields', None) or {}
    cf['ltype'] = ltype_name
    cf['ltype_handle'] = handle_arr
    cf['ltype_flags'] = flags
    ent.custom_fields = cf


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