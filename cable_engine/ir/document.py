"""cable_engine.ir.document — Document container.

A Document is the top-level IR node. It bundles:
  - the source type (PDF | DWG | scan | ...)
  - the source path (or URL / blob ref — for now just a Path)
  - one or more Pages (PDF semantics) or a single synthetic Page
    (DWG semantics — see below)
  - all extracted entities (text, lines, polylines, symbols)
  - V6.5+: business classification (set by TopologyStage)

The Document is what gets passed to the rest of the pipeline (OCR
Stage, Match Stage, Persist Stage). It is the smallest unit that
makes sense to process — a DWG file is one Document, a multi-page
PDF is one Document with many Pages.

DWG semantics:
  - DWG files are conventionally single-page. We still wrap them in
    a Document with one synthetic Page (page_number=1, no PixelImage)
    so the rest of the pipeline can iterate uniformly.
  - All entities from the DWG Loader land in `doc.entities` directly
    (NOT in `doc.pages[0].entities`); Pages are only for the rasterized
    pixel layer.

PDF semantics:
  - The PDF Loader creates one Page per page in the PDF, each Page
    carrying a set of variants (PixelImage per preprocess recipe).
  - The OCR Stage writes TextEntity instances into `doc.entities`.
  - Geometry stages (future) will write LineEntity etc. from CV.

This asymmetry is intentional: PDFs need Page for the rasterization
step, DWG files do not. The Document-level APIs (`doc.entities`,
`doc.iter_text()`, `doc.find_text('3B-507')`) are uniform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

from .entities import Entity, TextEntity
from .pdf import Page

if TYPE_CHECKING:
    from ..classifier import Classification


class DocumentType(str, Enum):
    PDF = 'pdf'
    DWG = 'dwg'
    SCAN = 'scan'      # future: scanned images (no PDF/DWG container)
    # Extension is easy: add DXF / IFC / whatever here.


@dataclass
class Document:
    """Source-agnostic top-level container.

    `pages` is a list because PDFs can have many; DWG files wrap in
    a single synthetic Page so the pipeline can iterate uniformly.

    `entities` is the FLAT list of every entity extracted from the
    document (text, line, polyline, symbol, future YOLO detections).

    `classification` is set by ClassificationStage (pipeline stage).
    None until the classification stage runs.
    """
    document_type: DocumentType
    document_path: Path
    content_hash: str = ''                 # sha256 of file bytes (DEDUP)
    pages: list[Page] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    classification: Optional['Classification'] = None  # V6.5+

    # ------------------------------------------------------------------
    # Convenience accessors (uniform across source types)
    # ------------------------------------------------------------------
    def iter_text(self) -> Iterator[TextEntity]:
        """Yield every TextEntity in the document (any source, any page)."""
        return (e for e in self.entities if isinstance(e, TextEntity))

    def find_text(self, needle: str) -> list[TextEntity]:
        """All TextEntities whose text contains `needle` (substring)."""
        return [e for e in self.entities if isinstance(e, TextEntity)
                and e.contains(needle)]

    @property
    def text(self) -> str:
        """Joined text of every TextEntity, separated by newlines.
        This is the canonical string the Match Stage operates on
        (replaces the old `combined = '\n'.join(...)` in cable_match.py)."""
        return '\n'.join(e.text for e in self.iter_text() if e.text)

    def add_entity(self, entity: Entity) -> None:
        """Append an entity (used by the OCR Stage and DWG Loader)."""
        self.entities.append(entity)

    def get_page(self, page_number: int) -> Optional[Page]:
        for p in self.pages:
            if p.page_number == page_number:
                return p
        return None


__all__ = ['Document', 'DocumentType']
