"""cable_engine.loaders.pdf_loader — PDF → Document.

Uses pypdfium2 to rasterize pages (returns PixelImage variants per
preprocess recipe). The OCR Stage is downstream of this loader and
consumes the PixelImages.

Why one Page per Page (not a list of pages) returned separately:
  A Document with 50 Pages is one IR node, not 50. The pipeline runs
  the OCR Stage once per Document and the Stage iterates
  `doc.pages` internally. This matches the "PDF is a single source"
  mental model: one document → one cable match result set.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import pypdfium2 as pdfium

from cable_engine.ir import Document, DocumentType, Page, PixelImage

from .base import BaseLoader, _register_suffix


def _content_hash(p: Path) -> str:
    """sha256 of the file contents (for cross-run dedup)."""
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class PDFLoader(BaseLoader):
    """Rasterize every page of a PDF into a Document with one Page per page.

    Each Page carries a single PixelImage variant ('none' preprocess).
    The OCR Stage can render additional variants (gauss_otsu) on
    demand. The Loader's job is to give the OCR Stage a starting
    Image to operate on; further variant generation is the Stage's
    responsibility.
    """

    document_type: DocumentType = DocumentType.PDF
    name: str = 'pdf'

    def __init__(self, dpi: int = 300, lang: str = 'chi_sim+eng'):
        self.dpi = dpi
        self.lang = lang

    def load(self, document_path: Path) -> Document:
        doc = Document(
            document_type=DocumentType.PDF,
            document_path=document_path,
            content_hash=_content_hash(document_path) if document_path.exists()
                         else '',
        )

        if not document_path.exists():
            doc.entities.append(_ErrorEntity(source='pdf', page=0,
                                              text=f'<pdf file not found: {document_path}>'))
            return doc

        try:
            pdf = pdfium.PdfDocument(str(document_path))
        except Exception as e:
            # Failed to open: return an empty Document with an error
            # marker (downstream stages will see no pages and report
            # no_text). Better than raising and killing the worker.
            doc.entities.append(_ErrorEntity(source='pdf', page=0,
                                              text=f'<pdf open error: {e}>'))
            return doc

        scale = self.dpi / 72.0
        for i in range(len(pdf)):
            try:
                pil = pdf[i].render(scale=scale).to_pil()
            except Exception as e:
                # Skip this page (Page is left empty; OCR will skip)
                continue
            page = Page(
                pdf_path=document_path,
                content_hash=doc.content_hash,
                page_number=i + 1,
            )
            page.variants['none'] = PixelImage(
                page_number=i + 1,
                width=pil.width, height=pil.height,
                pil_image=pil, dpi=self.dpi, rotation=0,
                preprocess='none', lang=self.lang,
            )
            doc.pages.append(page)
        return doc


# Register accepted suffixes
_register_suffix(PDFLoader, '.pdf')


# ---------------------------------------------------------------------------
# Internal: a fake TextEntity used to surface PDF open errors in the OCR
# text. This lets the Match Stage and downstream still produce a result
# (just no matches) instead of crashing on a bad PDF.
# ---------------------------------------------------------------------------
from cable_engine.ir import TextEntity as _TextEntity


def _ErrorEntity(*, source: str, page: int, text: str) -> _TextEntity:
    return _TextEntity(
        id=f'err-{page}-{hash(text) & 0xffffff:x}',
        source=source, page=page, text=text,
        confidence=0.0,
    )


__all__ = ['PDFLoader']
