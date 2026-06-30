"""cable_engine.loaders.base — abstract Document loader.

A loader takes a source file path and produces a `Document` IR node
(populated with Pages and/or entities). The rest of the pipeline
(OCR / Match / Persist) operates on Document uniformly regardless of
the source.

Why a class hierarchy:
  - DWGLoader.load() reads ezdxf and returns entities directly
    (no rasterization, no Page objects needed).
  - PDFLoader.load() returns Page objects containing PixelImage
    variants (the OCR Stage consumes these).
  - Future ScanLoader.load() would return Page objects with a single
    PixelImage variant from the input image.
  - DXFLoader (future) would behave like DWGLoader.

Each loader is a thin adapter. The IR is the contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from cable_engine.ir import Document, DocumentType


class BaseLoader(ABC):
    """Abstract base class. Subclasses must implement `load()`.

    Loaders are STATELESS (or at least worker-local): each call to
    `load(path)` is independent. The pipeline instantiates a loader
    once per process (cheap) and reuses it.
    """

    #: The DocumentType this loader produces. Set as a class attribute
    #: so the main loop can dispatch DocumentType -> Loader.
    document_type: DocumentType = DocumentType.PDF

    #: Human-readable name (for logging).
    name: str = 'base'

    @abstractmethod
    def load(self, document_path: Path) -> Document:
        """Read `document_path` and return a populated Document.

        Implementations should:
          - Set doc.content_hash to sha256 of the file bytes (for dedup).
          - Set doc.document_type to `self.document_type`.
          - For multi-page formats, populate doc.pages (one Page per
            page in the source).
          - Populate doc.entities with every entity extracted from the
            document (text runs, lines, polylines, symbols).
        """
        raise NotImplementedError

    @classmethod
    def can_load(cls, document_path: Path) -> bool:
        """Heuristic: can this loader handle the given path?

        Default impl: based on file extension. Loaders should override
        to be more precise (e.g. magic bytes).
        """
        suffix = document_path.suffix.lower()
        return suffix in _SUFFIXES_FOR_LOADER.get(
            cls.document_type, set(),
        )


# Registry of file-extension → DocumentType → loader-class
# (populated by each loader subclass below).
_SUFFIXES_FOR_LOADER: dict = {}


def _register_suffix(loader_cls, suffix: str) -> None:
    """Add a file extension to a loader's accepted suffixes."""
    _SUFFIXES_FOR_LOADER.setdefault(loader_cls.document_type, set()).add(
        suffix
    )


__all__ = ['BaseLoader', '_register_suffix']
