"""cable_engine.loaders.dwg_loader — DWG/DXF → Document.

Uses ezdxf to read vector entities from a DWG file. The DWG Loader
produces a Document whose entities are LineEntity / PolylineEntity /
TextEntity / SymbolEntity — these are inserted directly into
`doc.entities` (no Page / PixelImage layer, since DWG is vector and
the OCR Stage is bypassed).

Why no Page for DWG:
  DWG is conventionally single-page (the entire drawing is on one
  "sheet"). A future multi-sheet DWG (xref'd layouts) would extend
  this to one Page per layout, but for now a single synthetic Page
  with page_number=1 and no PixelImage suffices to satisfy the
  pipeline's iteration contract.

Why TextEntity for DWG text (not raw text string):
  Fusion logic downstream treats text uniformly — `for e in
  doc.iter_text(): ...` works whether the text came from OCR or from
  DWG's stored text. This is the whole point of the source-agnostic
  IR.

ezdxf is imported lazily: this module-level import is wrapped so
that DWGLoader can be imported even when ezdxf isn't installed. The
actual `import ezdxf` happens inside `load()`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from cable_engine.ir import (
    Document, DocumentType, LineEntity, Page, Point, PolylineEntity,
    SymbolEntity, TextEntity,
)

from .base import BaseLoader, _register_suffix


def _content_hash(p: Path) -> str:
    """sha256 of the file contents (for cross-run dedup)."""
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class DWGLoader(BaseLoader):
    """DWG/DXF → Document with vector entities (no rasterization)."""

    document_type: DocumentType = DocumentType.DWG
    name: str = 'dwg'

    def load(self, document_path: Path) -> Document:
        doc = Document(
            document_type=DocumentType.DWG,
            document_path=document_path,
            content_hash=_content_hash(document_path) if document_path.exists()
                         else '',
        )
        # DWG is single-page; create a synthetic Page so downstream
        # code can iterate uniformly.
        doc.pages.append(Page(
            pdf_path=document_path,
            content_hash=doc.content_hash,
            page_number=1,
            source='dwg',
        ))

        # If file doesn't exist, return early with error text entity
        if not document_path.exists():
            doc.entities.append(TextEntity(
                id='err-missing', source='dwg', page=1, confidence=0.0,
                text=f'<dwg file not found: {document_path}>',
            ))
            return doc

        # Lazy-import ezdxf: keeps the module importable when ezdxf
        # isn't installed (e.g. when the user only uses PDF). The full
        # ezdxf dependency tree pulls in numpy/fontTools/pyparsing;
        # we avoid forcing all of that on the PDF-only path.
        try:
            import ezdxf
        except ImportError as e:
            doc.entities.append(TextEntity(
                id='err-import', source='dwg', page=1, confidence=0.0,
                text=f'<ezdxf not installed; install with: pip install ezdxf> '
                     f'(underlying error: {e})',
            ))
            return doc

        try:
            dwg = ezdxf.readfile(str(document_path))
        except Exception as e:
            doc.entities.append(TextEntity(
                id='err-open', source='dwg', page=1, confidence=0.0,
                text=f'<dwg open error: {e}>',
            ))
            return doc

        msp = dwg.modelspace()
        for e in msp:
            try:
                self._consume_entity(e, doc)
            except Exception:
                pass
        return doc

    def _consume_entity(self, e, doc: Document) -> None:
        """Translate one ezdxf entity into one or more IR entities and
        append to doc.entities. Skips entities we don't know how to
        handle (we add new mappings as new source types appear)."""
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
            doc.add_entity(SymbolEntity(
                id=handle, source='dwg', page=1, confidence=1.0,
                layer=layer,
                name=getattr(e.dxf, 'name', ''),
            ))


# Register accepted suffixes (even if ezdxf isn't installed, the
# suffix dispatch still routes here; load() handles the missing-dep case)
_register_suffix(DWGLoader, '.dwg')
_register_suffix(DWGLoader, '.dxf')


__all__ = ['DWGLoader']

