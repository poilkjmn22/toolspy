"""cable_engine.ir.pdf — PDF-specific IR nodes.

The PDF IR models what a single page looks like after rasterization.
DWG files don't have pages in the same sense (DWG is single-page
vector), so PDF-specific types live here and DWG-specific types live
in cable_engine.loaders.dwg_loader. The shared base types (Entity,
Document) live in entities.py and document.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PixelImage:
    """A rasterized image of a single PDF page (PIL Image, RGB or L).

    PixelImage only exists for PDF — DWG entities are vector-native and
    don't need a raster. The `source` field is always 'pdf' for now
    but kept here for the same-source-variant pattern that other IR
    nodes use (see ir.entities for the design rationale).
    """
    page_number: int
    width: int
    height: int
    pil_image: object                     # PIL.Image.Image (lazy import)
    source: str = 'pdf'                   # always 'pdf' for now
    # OCR recipe context (so cache lookups are reproducible):
    dpi: int = 300
    rotation: int = 0                     # 0 / 90 / 180 / 270
    preprocess: str = 'none'              # 'none' | 'gauss_otsu' | 'both'
    lang: str = 'chi_sim+eng'
    psm: Optional[int] = None
    oem: Optional[int] = None

    @property
    def cache_key(self) -> tuple:
        """Tuple suitable for hashing / dedup. Two images of the same
        page with the same recipe should produce the same cache hit."""
        return (self.page_number, self.dpi, self.lang, self.rotation,
                self.preprocess, self.psm, self.oem)


@dataclass
class Page:
    """One page of a PDF: original path + a list of rendered variants
    (one per preprocess recipe).

    `source` is always 'pdf' here; the source-agnostic equivalent lives
    in `cable_engine.ir.document.Document` (which contains one or more
    Pages + the raw entities extracted from a DWG/PDF/scan).
    """
    pdf_path: Path
    content_hash: str                     # sha256 of pdf_path file bytes
    page_number: int                      # 1-indexed
    source: str = 'pdf'                   # always 'pdf' here
    # Each preprocess recipe ('none', 'gauss_otsu', 'both') gets its own
    # PixelImage. Keyed by recipe name for easy lookup.
    variants: dict[str, PixelImage] = field(default_factory=dict)

    def get_variant(self, recipe: str = 'none') -> Optional[PixelImage]:
        return self.variants.get(recipe)

    def has_variant(self, recipe: str = 'none') -> bool:
        return recipe in self.variants


__all__ = ['PixelImage', 'Page']
