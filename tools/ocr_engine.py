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


def _patch_pytesseract_stdout_decoding() -> None:
    """Monkey-patch pytesseract's strict UTF-8 decode sites to use
    errors='replace' instead of 'strict'.

    Three-layer defense against the M5 Max bug
    ----------------------------------------
    User-reported bug (M5 Max macOS Apple Silicon, tesseract 5.5.2,
    pytesseract 0.3.13):

        1882/1882 PDFs failed with:
        'utf-8' codec can't decode byte 0x89 in position 270: invalid start byte

    The 0x89 byte is the PNG signature byte. On macOS M-series, tesseract's
    stdout (which pytesseract writes to a file and then .decode()s) can
    contain PNG header bytes, locale warnings with binary data, OMP
    messages, etc. — anything from the underlying C library (leptonica)
    that gets emitted to stdout instead of stderr.

    Why the v1 patch (commit 7d48c54) didn't work
    ------------------------------------------
    It wrapped pytesseract.image_to_string which returns str, not bytes.
    The wrapper's `isinstance(result, bytes)` check never fired.

    Why the v2 patch (commit 8952b40) didn't work
    ------------------------------------------
    On M5 Max the patch was applied (verified locally — _read_output
    is monkey-patched), but the user STILL gets the same error. The
    most likely explanation is that pytesseract on M5 Max is a version
    or build where:
      - _read_output doesn't exist (we saw this in 0.3.10), or
      - tesseract writes a different file (not the .txt we expect), or
      - the patch was applied to a different pytesseract instance via
        duplicate import paths, or
      - the user is hitting a code path that doesn't go through _read_output
        (e.g. the HOCR/PDF path at line 437).

    The v3 patch (this one): defense in depth
    -----------------------------------
    1. Patch _read_output if it exists (catches pytesseract 0.3.13's
       image_to_string → _read_output path).
    2. Patch run_and_get_output if it exists (catches pytesseract's
       list-langs / version paths that .decode() subprocess output).
    3. Wrap get_errors if it exists (catches the timeout_manager error
       decode).
    4. The real safety net: TesseractEngine.ocr() catches
       UnicodeDecodeError directly and returns ''. This works no matter
       what pytesseract does internally — if any decode fails, we
       return empty string and the worker marks the PDF as no_text
       (not as failed). The cable_match.py worker continues to the next
       PDF.

    Idempotent: guarded by pytesseract.pytesseract._tooly_patched_v3.
    """
    try:
        from pytesseract import pytesseract as _pt_inner
    except ImportError:
        return
    if getattr(_pt_inner, '_tooly_patched_v3', False):
        return

    _DEFAULT_ENCODING = getattr(_pt_inner, 'DEFAULT_ENCODING', 'utf-8')

    def _safe_decode(b):
        """Replace non-UTF-8 bytes with U+FFFD instead of raising."""
        if isinstance(b, bytes):
            return b.decode(_DEFAULT_ENCODING, errors='replace')
        return b

    # Patch 1: _read_output (pytesseract 0.3.13 main OCR path)
    if hasattr(_pt_inner, '_read_output'):
        def _safe_read_output(filename, return_bytes=False):
            with open(filename, 'rb') as output_file:
                data = output_file.read()
            if return_bytes:
                return data
            return _safe_decode(data)
        _pt_inner._read_output = _safe_read_output

    # Patch 2: run_and_get_output (pytesseract's list-langs / version paths)
    if hasattr(_pt_inner, 'run_and_get_output'):
        _orig_run_get = _pt_inner.run_and_get_output
        def _safe_run_and_get_output(*args, **kwargs):
            result = _orig_run_get(*args, **kwargs)
            return _safe_decode(result)
        _pt_inner.run_and_get_output = _safe_run_and_get_output

    # Patch 3: get_errors (timeout_manager error decode)
    if hasattr(_pt_inner, 'get_errors'):
        _orig_get_errors = _pt_inner.get_errors
        def _safe_get_errors(error_string):
            try:
                return _orig_get_errors(error_string)
            except UnicodeDecodeError:
                # Last-resort: try the bytes through 'replace' ourselves
                if isinstance(error_string, bytes):
                    return error_string.decode(_DEFAULT_ENCODING, errors='replace')
                return str(error_string)
        _pt_inner.get_errors = _safe_get_errors

    _pt_inner._tooly_patched_v3 = True


# Apply the patch at import time. Idempotent.
_patch_pytesseract_stdout_decoding()


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
        try:
            return pytesseract.image_to_string(pil_image, lang=self.lang, config=config) or ""
        except UnicodeDecodeError as e:
            # Defense-in-depth: even if the _read_output monkey-patch
            # above doesn't catch the error (e.g. pytesseract was upgraded
            # and the internals changed), we still recover gracefully
            # rather than failing the whole PDF. The OCR result is
            # already lost by this point, but we prevent the worker
            # from crashing on every PDF.
            #
            # Symptom (M5 Max macOS Apple Silicon, tesseract 5.5.2):
            #     UnicodeDecodeError: 'utf-8' codec can't decode byte 0x89
            #     in position 270: invalid start byte
            # The 0x89 byte is the PNG signature byte; it ends up in
            # tesseract's stdout because on some macOS builds, tesseract
            # emits PNG progress bytes to stdout when a graphics lib
            # fails to initialize cleanly (e.g. leptonica 1.87.0
            # version mismatch, locale warning with binary data, etc.).
            # The actual OCR text is successfully written; only the
            # trailing noise triggers the decode error.
            print(
                f'WARNING: pytesseract UnicodeDecodeError on '
                f'{getattr(pil_image, "filename", "<in-memory>")!r}: '
                f'{e}. Treating as empty OCR result (stderr noise '
                f'with non-UTF-8 bytes; actual OCR text already lost).',
                file=sys.stderr,
            )
            return ""

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

    PaddleOCR PP-OCRv3/v4 supports one language per model. The mapping is:
      - `chi_sim+eng` (Tesseract chieng, mixed) → `ch` model: handles Chinese
        characters + ASCII digits/letters well, which is what Chinese
        power-drawing PDFs need.
      - `chi_sim` (Tesseract chisim, Chinese only) → `en` model: deliberately
        DIFFERENT from chieng. Used in the 6-stage union to give PaddleOCR
        a second pass with an English-only OCR. May pick up Latin-only
        cable IDs (1F-151, 2F-151, GPS-1F) that the `ch` model drops as
        "noise". Will lose all Chinese text, so only useful as a complement,
        not a replacement.
      - `en` / `eng` → `en` model
      - anything else → `ch` (safe default)

    To opt OUT of this and force `ch` for both langs (e.g. when the en
    model is known to hurt recall on a particular PDF set), pass
    `engine_kwargs={'force_lang': 'ch'}` to `PaddleOCREngine`.
    """
    s = lang.lower()
    if 'chi_sim' in s and 'eng' in s:
        return 'ch'
    if 'chi_sim' in s:
        # chisim Tesseract = Chinese only; map PaddleOCR to en model
        # so chisim_paddle gives a different text than chieng_paddle
        return 'en'
    if 'en' in s.split('+') or 'eng' in s.split('+'):
        return 'en'
    return 'ch'  # safe default


def _parse_paddleocr_major_version(version_str: str) -> str:
    """Return 'v2' or 'v3' based on paddleocr package version.

    PaddleOCR 2.x (last release 2.10.0, pinned at 2.7.3) uses
    `PaddleOCR(use_gpu=, show_log=)` and `result[0][1][0]` text extraction.
    PaddleOCR 3.x (first release 3.0.0, current 3.7.0) uses PaddleX
    pipelines; `PaddleOCR(use_textline_orientation=, ...)` and
    `result['rec_texts']` text extraction.
    """
    try:
        major = int(version_str.split('.')[0])
    except (ValueError, IndexError):
        return 'unknown'
    if major >= 3:
        return 'v3'
    if major == 2:
        return 'v2'
    return 'unknown'


def _extract_text_v2(result) -> str:
    """Extract text from PaddleOCR 2.x result.

    Result shape: list with one entry per page; entry is list of line results;
    each line result is `[[box_points], (text, conf)]`.
    """
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


def _extract_text_v3(result) -> str:
    """Extract text from PaddleOCR 3.x result.

    Result shape: list of OCRResult objects (one per input page); each has
    `result['rec_texts']` returning a list of recognized line strings.
    """
    lines = []
    if not result:
        return ""
    for page_result in result:
        # page_result may be an OCRResult dataclass or dict-like
        rec_texts = None
        try:
            # dict access works on PaddleX BaseCVResult
            rec_texts = page_result['rec_texts']
        except (KeyError, TypeError):
            # Fallback: attribute access
            rec_texts = getattr(page_result, 'rec_texts', None)
        if rec_texts:
            lines.extend(t for t in rec_texts if t)
    return '\n'.join(lines)


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
        # Detected at init() time: 'v2' (paddleocr 2.x with PaddleOCR(use_gpu=...))
        # or 'v3' (paddleocr 3.x via PaddleX — no use_gpu/show_log kwargs).
        self._paddleocr_version = None

    def init(self) -> None:
        if self._ocr is not None:
            return
        try:
            import paddleocr as _paddleocr_pkg
        except ImportError:
            raise EngineNotAvailable(
                "PaddleOCR is not installed. To enable it, run:\n"
                "  pip install -r requirements-paddleocr.txt\n"
                "then `python -c \"from paddleocr import PaddleOCR\"` to download models on first use."
            )
        # Detect major version from the package's __version__ string.
        # 2.x → 2.7.3 (PP-OCRv3/v4, accepts use_gpu/show_log).
        # 3.x → 3.0+   (PP-OCRv5/v6, PaddleX-based, NO use_gpu/show_log;
        #                GPU via paddlepaddle-gpu + PaddleX device autodetect).
        self._paddleocr_version = _parse_paddleocr_major_version(_paddleocr_pkg.__version__)
        # If user explicitly asked for GPU, verify paddlepaddle was built
        # with CUDA before constructing PaddleOCR. Both 2.x and 3.x would
        # otherwise silently fall back to CPU (2.x) or autodetect-to-CPU
        # (3.x). HARD-FAIL here so the user sees a clear error instead of
        # an 8h CPU job.
        if self.use_gpu:
            try:
                import paddle
                if not paddle.device.is_compiled_with_cuda():
                    raise EngineNotAvailable(
                        "--use-gpu requested but paddlepaddle was installed WITHOUT CUDA support. "
                        "Install paddlepaddle-gpu instead:\n"
                        "  pip uninstall -y paddlepaddle\n"
                        "  pip install paddlepaddle-gpu==2.6.2 -f https://www.paddlepaddle.org.cn/whl/<linux|windows>/<cu117|cu118|cu123>[/noavx]\n"
                        "On macOS, paddlepaddle has no CUDA wheel; remove --use-gpu to run on CPU."
                    )
                if paddle.device.cuda.device_count() < 1:
                    raise EngineNotAvailable(
                        "--use-gpu requested but no CUDA device is visible. "
                        "Check `nvidia-smi` and CUDA_VISIBLE_DEVICES."
                    )
            except ImportError:
                raise EngineNotAvailable(
                    "--use-gpu requested but paddle is not importable."
                )
        # Suppress Paddle's chatty INFO logs.
        os.environ.setdefault('GLOG_v', '3')
        try:
            from paddleocr import PaddleOCR
            if self._paddleocr_version == 'v2':
                # 2.x path: PaddleOCR(use_angle_cls, lang, use_gpu, show_log).
                self._ocr = PaddleOCR(
                    use_angle_cls=self.use_angle_cls,
                    lang=self.paddle_lang,
                    use_gpu=self.use_gpu,
                    show_log=False,
                )
            elif self._paddleocr_version == 'v3':
                # 3.x path: PaddleX-based. NO use_gpu / show_log kwargs
                # (3.x removed them; GPU comes from paddlepaddle-gpu +
                # PaddleX device autodetect). `use_angle_cls` is replaced
                # by `use_textline_orientation`. doc-orientation/unwarping
                # are off by default — we don't need them for cable labels.
                self._ocr = PaddleOCR(
                    lang=self.paddle_lang,
                    use_textline_orientation=self.use_angle_cls,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                )
            else:
                raise EngineNotAvailable(
                    f"Unrecognized paddleocr version: {_paddleocr_pkg.__version__!r}. "
                    "Expected 2.x or 3.x. Please reinstall requirements-paddleocr.txt."
                )
        except Exception as e:
            raise EngineNotAvailable(
                f"PaddleOCR failed to initialize ({self._paddleocr_version}, "
                f"paddleocr=={_paddleocr_pkg.__version__}): {e}\n"
                "Common causes:\n"
                "  - First run downloads models (~100MB to ~/.paddlex/). Check network.\n"
                "  - paddlepaddle wheel mismatch.\n"
                "      2.x: pip install paddlepaddle==2.6.2\n"
                "      3.x: pip install paddlepaddle==3.0+\n"
                "  - On macOS Apple Silicon, use CPU only (PaddleOCR auto-detects)."
            )

    def shutdown(self) -> None:
        # PaddleOCR has no explicit cleanup API, but nulling the reference
        # lets GC release the model memory in the worker process.
        self._ocr = None

    def ocr(self, pil_image) -> str:
        if self._ocr is None:
            self.init()
        # PaddleOCR takes a numpy array (RGB) on 2.x, or a path/PIL/numpy
        # on 3.x. Passing the PIL Image directly works on both.
        try:
            if self._paddleocr_version == 'v2':
                import numpy as np
                img_array = np.array(pil_image)
                result = self._ocr.ocr(img_array, cls=self.use_angle_cls)
                return _extract_text_v2(result)
            else:  # v3
                result = self._ocr.predict(pil_image)
                return _extract_text_v3(result)
        except Exception as e:
            print(f'PaddleOCR ocr failed ({self._paddleocr_version}): {e}', file=sys.stderr)
            return ""

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
