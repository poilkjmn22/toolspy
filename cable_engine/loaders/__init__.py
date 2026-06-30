"""cable_engine.loaders — pluggable Document loaders.

Each loader turns a source file (PDF / DWG / DXF / future scan) into a
`Document` IR node. The rest of the pipeline is source-agnostic.

Add a new loader:
  1. Subclass BaseLoader
  2. Set `document_type` class attr
  3. Set `name` class attr
  4. Implement `load(path) -> Document`
  5. Call `_register_suffix(YourLoader, '.ext')` for each file
     extension you accept
  6. Import it here so the registry picks it up
"""

from .base import BaseLoader
from .dwg_loader import DWGLoader
from .pdf_loader import PDFLoader


# Default dispatch order: PDF first (most common), DWG second.
# Callers can extend this list at runtime by appending to it.
_LOADERS = [PDFLoader, DWGLoader]


def get_loader_for(document_path):
    """Return an instantiated loader for the given path.

    Picks the first loader whose `can_load(path)` returns True. If no
    loader matches, falls back to PDFLoader (best-guess: any
    extension with a DWG loader on it would have been registered
    above, so falling back to PDF is only for unrecognised types).
    """
    for cls in _LOADERS:
        if cls.can_load(document_path):
            return cls()
    return PDFLoader()


__all__ = [
    'BaseLoader', 'PDFLoader', 'DWGLoader', 'get_loader_for', '_LOADERS',
]
