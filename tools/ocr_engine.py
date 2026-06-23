#!/usr/bin/env python3
"""ocr_engine.py — pluggable OCR engine abstraction.

Defines a base `OCREngine` class plus two concrete implementations:
  - `TesseractEngine` (default, requires `brew install tesseract` system binary)
  - `PaddleOCREngine` (optional, requires `pip install -r requirements-paddleocr.txt`)

Factory:
  `get_engine(name, lang, **kwargs)` returns the right engine instance, with a
  helpful error if the requested engine's deps are missing.

Why this exists:
  Tesseract handles ~70-80% recall on Chinese power-drawing PDFs. PaddleOCR
  (PP-OCRv3/v4) typically reaches ~85-95% on the same inputs — better on
  small fonts and dense terminal blocks. The two engines are kept as options
  because Tesseract is ~700 MB of system deps vs PaddleOCR ~250 MB of pip +
  small model; non-electrical-industry PDFs (typed documents) may prefer
  Tesseract's simpler dependency.

Tesseract is the default. All existing scripts, caches, and behaviors are
preserved unless the user passes `--engine paddleocr`.
"""
import os
import sys
from typing import Tuple


class EngineNotAvailable(Exception):
    """Raised when an engine's dependencies aren't installed."""


class OCREngine:
    """Abstract OCR engine. Subclasses must implement `ocr(pil)`.

    The `init()` method handles heavy model loading (called once per worker
    process by `cable_match._worker_init`). The `shutdown()` method is a
    cleanup hook called when a worker exits (rarely needed but here for
    PaddleOCR's GPU memory release).
    """

    name: str = 'abstract'

    def __init__(self, lang: str = 'chi_sim+eng', **kwargs):
        self.lang = lang
        self.kwargs = kwargs

    def init(self) -> None:
        """Load any heavy models / state. Called once per worker process."""
        pass

    def shutdown(self) -> None:
        """Release any held resources."""
        pass

    def ocr(self, pil_image) -> str:
        """Run OCR on a PIL image. Returns plain text (no bbox/confidence)."""
        raise NotImplementedError

    def detect_rotation(self, pil_image) -> Tuple[float, int]:
        """Return (confidence, degrees). 0/0 means "no rotation needed".

        PaddleOCR handles rotation internally via `use_angle_cls=True`, so
        its default returns (1.0, 0) — caller should not pre-rotate.
        Tesseract uses its OSD subsystem.
        """
        return (0.0, 0)


# === Tesseract ===

class TesseractEngine(OCREngine):
    """Tesseract via `pytesseract`. Default engine.

    Requires the system `tesseract` binary on PATH (or set
    `pytesseract.pytesseract.tesseract_cmd` explicitly). For Chinese,
    install `tesseract-lang` (macOS) or `tesseract-ocr-chi-sim` (Linux).
    """

    name = 'tesseract'

    def __init__(self, lang: str = 'chi_sim+eng', psm=None, oem=None, **kwargs):
        super().__init__(lang, **kwargs)
        self.psm = psm
        self.oem = oem

    def _build_config(self) -> str:
        parts = []
        if self.psm is not None:
            parts.append(f'--psm {self.psm}')
        if self.oem is not None:
            parts.append(f'--oem {self.oem}')
        return ' '.join(parts)

    def ocr(self, pil_image) -> str:
        import pytesseract
        config = self._build_config()
        return pytesseract.image_to_string(pil_image, lang=self.lang, config=config) or ""

    def detect_rotation(self, pil_image) -> Tuple[float, int]:
        try:
            import pytesseract
            osd = pytesseract.image_to_osd(pil_image)
            rot_deg, rot_conf = 0, 0.0
            for line in osd.split('\n'):
                if 'Orientation in degrees' in line or 'Rotate:' in line:
                    try:
                        rot_deg = int(line.split(':')[-1].strip())
                    except (ValueError, IndexError):
                        pass
                elif 'Orientation confidence' in line:
                    try:
                        rot_conf = float(line.split(':')[-1].strip())
                    except (ValueError, IndexError):
                        pass
            return (rot_conf, rot_deg)
        except Exception:
            return (0.0, 0)


# === PaddleOCR ===

def _map_tesseract_lang_to_paddle(lang: str) -> str:
    """Map Tesseract-style lang string to PaddleOCR's single lang code.

    PaddleOCR PP-OCRv3/v4 supports one language per model. For Chinese
    electrical drawings, `ch` (Simplified Chinese) handles ASCII digits and
    most latin characters well; English-only text should use `en`.
    """
    s = lang.lower()
    if 'chi_sim' in s or 'chinese' in s or 'ch' in s.split('+'):
        return 'ch'
    if 'en' in s.split('+') or 'eng' in s.split('+'):
        return 'en'
    return 'ch'


class PaddleOCREngine(OCREngine):
    """PaddleOCR (PP-OCRv3 default). Optional engine — install with
    `pip install -r requirements-paddleocr.txt`.

    Why PaddleOCR over Tesseract:
      - Better small-font recall (Chinese 5-8 pt cable labels)
      - Better on dense terminal-block layouts
      - Built-in text-angle classifier (no separate OSD pass)
      - Slightly slower on CPU (no GPU assumed by default here)

    Caveats:
      - First call loads the model (~10s); done in `init()` not per-call
      - Each worker process holds its own model copy (~250 MB)
      - On Mac CPU, ~50ms/page; on Win11 GPU, ~10ms/page
      - PaddleOCR's text quality is sometimes less consistent than Tesseract
        on typed text / forms — Tesseract remains the default for that case
    """

    name = 'paddleocr'

    def __init__(self, lang: str = 'chi_sim+eng',
                 use_angle_cls: bool = True,
                 use_gpu: bool = False,
                 **kwargs):
        super().__init__(lang, **kwargs)
        self.paddle_lang = _map_tesseract_lang_to_paddle(lang)
        self.use_angle_cls = use_angle_cls
        self.use_gpu = use_gpu
        self._ocr = None

    def init(self) -> None:
        if self._ocr is not None:
            return
        try:
            from paddleocr import PaddleOCR  # noqa: F401
        except ImportError:
            raise EngineNotAvailable(
                "PaddleOCR is not installed. To enable it, run:\n"
                "  pip install -r requirements-paddleocr.txt\n"
                "then `python -c \"from paddleocr import PaddleOCR\"` to download models on first use."
            )
        # Suppress Paddle's chatty INFO logs.
        os.environ.setdefault('GLOG_v', '3')
        try:
            from paddleocr import PaddleOCR
            # PaddleOCR 3.x API: only `lang` + `use_angle_cls` accepted.
            # 2.x also accepted `use_gpu` / `show_log` (no longer supported).
            self._ocr = PaddleOCR(
                use_angle_cls=self.use_angle_cls,
                lang=self.paddle_lang,
            )
        except Exception as e:
            raise EngineNotAvailable(
                f"PaddleOCR failed to initialize: {e}\n"
                "Common causes:\n"
                "  - First run downloads models (~100MB). Check network.\n"
                "  - paddlepaddle wheel mismatch. Try `pip install paddlepaddle==2.6.2`.\n"
                "  - On macOS Apple Silicon, use CPU only (PaddleOCR auto-detects)."
            )

    def shutdown(self) -> None:
        # PaddleOCR has no explicit cleanup API, but nulling the reference
        # lets GC release the model memory in the worker process.
        self._ocr = None

    def ocr(self, pil_image) -> str:
        if self._ocr is None:
            self.init()
        # PaddleOCR takes a numpy array (BGR). PIL is RGB; paddleocr handles
        # both but the convention is to convert explicitly.
        import numpy as np
        img_array = np.array(pil_image)
        try:
            result = self._ocr.ocr(img_array, cls=self.use_angle_cls)
        except Exception as e:
            print(f'PaddleOCR ocr failed: {e}', file=sys.stderr)
            return ""
        # Result format (v3): list with one entry per page; entry is a list
        # of line results; each line result is [[box_points], (text, conf)].
        # We extract just the text, joined by newlines.
        lines = []
        if not result:
            return ""
        page_result = result[0] if isinstance(result[0], list) else result
        if not page_result:
            return ""
        for line_result in page_result:
            if not line_result or len(line_result) < 2:
                continue
            text_conf = line_result[1]
            if not text_conf or not isinstance(text_conf, (tuple, list)):
                continue
            text = text_conf[0]
            if text:
                lines.append(text)
        return '\n'.join(lines)

    def detect_rotation(self, pil_image) -> Tuple[float, int]:
        # PaddleOCR with use_angle_cls=True handles rotation internally
        # by routing lines through the angle classifier. The returned
        # text is already upright, so caller should NOT pre-rotate.
        # We return a sentinel confidence=1.0 to signal "trust the engine".
        return (1.0, 0)


# === Factory ===

def get_engine(name: str, lang: str = 'chi_sim+eng', **kwargs) -> OCREngine:
    """Return an OCR engine instance by name. Default is 'tesseract'.

    Examples:
        eng = get_engine('tesseract', lang='chi_sim+eng', psm=6)
        eng = get_engine('paddleocr', lang='chi_sim+eng', use_gpu=True)
    """
    name = (name or 'tesseract').lower()
    if name in ('tesseract', 'tess'):
        return TesseractEngine(lang=lang, **kwargs)
    if name in ('paddleocr', 'paddle', 'ppocr'):
        return PaddleOCREngine(lang=lang, **kwargs)
    raise ValueError(f"Unknown OCR engine: {name!r}. Supported: 'tesseract', 'paddleocr'.")


def check_engine(name: str) -> Tuple[bool, str]:
    """Return (ok, message) for the named engine. Used by CLI tools to
    pre-flight check before starting a long batch."""
    name = (name or 'tesseract').lower()
    if name in ('tesseract', 'tess'):
        try:
            import pytesseract
            import shutil
            if shutil.which('tesseract') is None:
                return False, (
                    "Tesseract 未安装。请运行:\n"
                    "  macOS:  brew install tesseract tesseract-lang\n"
                    "  Linux:  apt install tesseract-ocr tesseract-ocr-chi-sim"
                )
            ver = pytesseract.get_tesseract_version()
            return True, f"Tesseract {ver}"
        except Exception as e:
            return False, f"Tesseract 检查失败: {e}"
    if name in ('paddleocr', 'paddle', 'ppocr'):
        try:
            import paddleocr  # noqa: F401
            return True, f"PaddleOCR {paddleocr.__version__}"
        except ImportError:
            return False, (
                "PaddleOCR 未安装。请运行:\n"
                "  pip install -r requirements-paddleocr.txt"
            )
    return False, f"Unknown engine: {name!r}"
