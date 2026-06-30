"""cable_engine.pipeline — Stage / Context pipeline for cable-match.

Pattern borrowed from FFmpeg: each Stage takes a Context, mutates it,
and returns it. The pipeline is a list of Stages. Each Stage is
independently testable and swappable (e.g. swap Tesseract for
PaddleOCR by changing one Stage class).

Why this abstraction:
  - The original cable_match.py had `_render_and_ocr` (a 60-line
    function) doing 4 things at once: PDF render, preprocess, OCR,
    cache write. Changing any of those 4 things meant reading the whole
    function. With Stages, each is a 5-15 line class with one
    responsibility.
  - The multiprocessing worker is a `Pipeline.run(context)` call.
    Adding a new step (e.g. YOLO detector) means adding one class, not
    editing a 1000-line monolith.
  - Tests are tiny: mock one Stage, assert its side effects.

When NOT to use it:
  - The pipeline is purely linear (no branching, no parallel
    branches, no event subscription). Don't add an Event Bus here
    until there's a second consumer of stage output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Context — the data passed between Stages
# ---------------------------------------------------------------------------
@dataclass
class Context:
    """Everything one Stage needs to know / produces for the next Stage.

    Mutated in place by each Stage. Stages should only read/write fields
    that are documented as their input/output contract.
    """
    # ---------- document ----------
    document_path: Path                    # legacy alias (compat)
    content_hash: str
    document_type: str = "pdf"
    pdf_size: int = 0
    pdf_mtime: float = 0.0

    # ---------- multi-source document (set by Loader before Pipeline) ----------
    # The current Document under processing. DWG and PDF both produce
    # a Document; the pipeline reads/writes through it.
    document: Any = None                     # cable_engine.ir.Document

    # ---------- OCR config ----------
    dpi: int = 300
    lang: str = "chi_sim+eng"
    rotation: int = 0
    preprocess: str = "none"
    psm: Optional[int] = None
    oem: Optional[int] = None
    engine_name: str = "tesseract"
    use_levenshtein: bool = False
    use_gpu: bool = False
    no_state: bool = False

    # ---------- runtime (PDF only; ignored for DWG) ----------
    page_number: int = 1
    pixel_image: Any = None

    text_boxes: list = field(default_factory=list)
    full_text: str = ""

    matches: dict = field(default_factory=dict)

    # ---------- result ----------
    error_msg: Optional[str] = None
    no_text: bool = False

    def has_error(self) -> bool:
        return self.error_msg is not None

# ---------------------------------------------------------------------------
# Stage — one step in the pipeline
# ---------------------------------------------------------------------------
class Stage(ABC):
    """Abstract base class. Subclass and implement `run`.

    A Stage is a single-responsibility unit. Examples:
      - RenderStage       (PDF -> PixelImage)
      - PreprocessStage   (PixelImage -> PixelImage, applied transformations)
      - OCRStage          (PixelImage -> TextBox list)
      - MatchStage        (text -> matches)
      - PersistStage      (matches -> writes to cable_engine.storage)

    Each Stage's `run` method:
      - Reads from self.reads or ctx (whichever's clearer)
      - Writes to ctx.<field>
      - Returns ctx (for chaining)
    """

    #: Human-readable name (used in pipeline.run() logging).
    name: str = '<unnamed>'

    @abstractmethod
    def run(self, ctx: Context) -> Context:
        """Process `ctx`, mutate it, return it."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Pipeline — a list of Stages
# ---------------------------------------------------------------------------
@dataclass
class Pipeline:
    """A linear sequence of Stages. `run(ctx)` walks the list in order."""
    stages: list = field(default_factory=list)

    def run(self, ctx: Context) -> Context:
        for stage in self.stages:
            ctx = stage.run(ctx)
            if ctx.has_error():
                # Short-circuit on error. Subsequent stages get a chance
                # to clean up (e.g. close PDF handles) if they want, by
                # checking ctx.error_msg themselves.
                break
        return ctx

    def __len__(self) -> int:
        return len(self.stages)

    def __repr__(self) -> str:
        return ' -> '.join(s.name for s in self.stages)


__all__ = ['Context', 'Stage', 'Pipeline']
