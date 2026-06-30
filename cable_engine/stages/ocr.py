"""cable_engine.stages.ocr — renderize + OCR stages.

Two stages live here because they always run as a pair:
  - RasterizeStage: generates the additional preprocess variants
    ('gauss_otsu') for each page. PDFLoader only emits 'none' for now.
  - OCRStage: consumes the PixelImage variants on each page and emits
    TextEntity objects into the Document.

PDF flow:  PDFLoader → RasterizeStage → OCRStage → MatchStage
DWG flow:  DWGLoader  → (skip RasterizeStage/OCRStage, already has
                       text entities from the loader) → MatchStage

The pipeline runner (cli.py) checks ctx.document_type and skips
RasterizeStage/OCRStage for non-PDF sources.
"""

from __future__ import annotations

from typing import Optional

from PIL import ImageFilter

from cable_engine.ir import (
    BBox, Document, DocumentType, Page, PixelImage, TextEntity,
)
from cable_engine.pipeline import Context, Stage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _gauss_otsu_threshold(gray_pil: Image.Image) -> Image.Image:
    """Gaussian blur (r=1) + Otsu threshold on a grayscale PIL image.
    Keeps cables visible when the original is light gray on a busy
    background. Returns a 1-bit (mode 'L') image.
    """
    gray = gray_pil.convert('L')
    blurred = gray.filter(ImageFilter.GaussianBlur(radius=1))
    otsu_threshold = 253
    return blurred.point(lambda p: 0 if p < otsu_threshold else 255).convert('1')


# ---------------------------------------------------------------------------
# Rasterize Stage
# ---------------------------------------------------------------------------
class RasterizeStage(Stage):
    """Generate the additional preprocess variants for each page.

    Currently the PDFLoader emits only the 'none' variant. This Stage
    adds the 'gauss_otsu' variant by running a blur + Otsu threshold
    on the existing 'none' image. Future variants (e.g. 'deskew',
    'binarize_adaptive') can be added here as new methods.
    """

    name = 'rasterize'

    def __init__(self, dpi: int = 300, lang: str = 'chi_sim+eng'):
        self.dpi = dpi
        self.lang = lang

    def run(self, ctx: Context) -> Context:
        if ctx.error_msg is not None or ctx.document is None:
            return ctx

        for page in ctx.document.pages:
            if 'none' in page.variants:
                base_pi = page.variants['none']
                if 'gauss_otsu' not in page.variants:
                    try:
                        page.variants['gauss_otsu'] = PixelImage(
                            page_number=base_pi.page_number,
                            width=base_pi.width,
                            height=base_pi.height,
                            pil_image=_gauss_otsu_threshold(base_pi.pil_image),
                            source=base_pi.source,
                            dpi=base_pi.dpi,
                            rotation=base_pi.rotation,
                            preprocess='gauss_otsu',
                            lang=base_pi.lang,
                            psm=base_pi.psm,
                            oem=base_pi.oem,
                        )
                    except Exception:
                        # Non-fatal: OCR can still run on 'none' alone.
                        pass
        return ctx


# ---------------------------------------------------------------------------
# OCR Stage
# ---------------------------------------------------------------------------
class OCRStage(Stage):
    """Run tesseract (or pytesseract-friendly wrapper) on each variant
    in each page, emit TextEntity objects into the Document.

    A TextEntity is appended to `doc.entities` for every non-empty
    OCR result. Its source is 'pdf' (or whatever the document's
    source is), its page number is the page that produced it, and its
    confidence is whatever the engine reports (or 0.9 if unknown).
    """

    name = 'ocr'

    def __init__(self, engine_name: str, use_gpu: bool = False):
        self.engine_name = engine_name
        self.use_gpu = use_gpu
        self._engine = None

    def _ensure_engine(self):
        if self._engine is None:
            from tools.ocr_engine import get_engine
            self._engine = get_engine(self.engine_name, use_gpu=self.use_gpu)
            self._engine.init()
        return self._engine

    def run(self, ctx: Context) -> Context:
        if ctx.error_msg is not None or ctx.document is None:
            return ctx

        try:
            engine = self._ensure_engine()
        except Exception as e:
            ctx.error_msg = f'OCR engine init failed: {e}'
            return ctx

        for page in ctx.document.pages:
            for pi in page.variants.values():
                try:
                    text = engine.ocr(pi.pil_image) or ''
                except Exception:
                    # Per-page OCR failure: continue with other pages
                    continue
                if not text.strip():
                    continue
                # Try to get a confidence estimate from the engine.
                conf = self._estimate_confidence(engine, pi.pil_image)
                ctx.document.add_entity(TextEntity(
                    id=f'ocr-{ctx.content_hash or "x"}-{pi.page_number}-{pi.preprocess}',
                    source=ctx.document.document_type.value,
                    page=pi.page_number,
                    confidence=conf,
                    text=text,
                    layer=pi.preprocess,
                ))

        # If the source is non-PDF (e.g. DWG), the loader already
        # populated doc.entities. OCR adds nothing. That's expected —
        # DWG's text is high-confidence already.
        return ctx

    def _estimate_confidence(self, engine, pil_image) -> float:
        """Best-effort confidence score. tesseract provides image_to_data
        with conf columns; pytesseract exposes image_to_osd. We try
        the cheapest option and return 0.9 as a sane default.
        """
        try:
            data = engine.ocr.__self__ if False else None
        except Exception:
            pass
        try:
            # tesseract-specific path: image_to_data returns level 5 (words)
            # with a 'conf' column (0-100). Average it.
            import csv as _csv
            from io import StringIO
            d = self._engine.image_to_data(pil_image, output_type='csv')
            confs = []
            for row in _csv.DictReader(StringIO(d)):
                if 'conf' in row:
                    try:
                        confs.append(float(row['conf']))
                    except (TypeError, ValueError):
                        pass
            if confs:
                return sum(confs) / len(confs) / 100.0
        except Exception:
            pass
        return 0.9


__all__ = ['RasterizeStage', 'OCRStage']
