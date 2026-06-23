#!/usr/bin/env python3
"""cable_match v2: multi-target PDF organizer with SQLite cache, state.json, multiprocessing.

v2 improvements over v1:
  1. SQLite OCR cache — re-runs are instant for unchanged PDFs (skip OCR)
  2. Single top-level _matches.csv (not per-target)
  3. state.json auto-saved every 30s + on SIGTERM (crash recovery)
  4. --resume <state.json|auto> (built-in resume, no external scripts)
  5. multiprocessing (true parallel, 4-6x speedup vs ThreadPoolExecutor)

Usage:
  python scripts/cable_match.py --csv <path> --input <folder> [--output <root>] [--list]
                                  [--workers N] [--rotation 0|90|180|270]
                                  [--preprocess none|gauss_otsu]
                                  [--resume <state.json|auto>] [--no-cache]
"""
import argparse
import csv
import datetime
import hashlib
import json
import multiprocessing as mp
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageFilter, ImageOps

# Auto-detect Tesseract binary on Windows. macOS / Linux usually have tesseract
# on PATH via brew / apt; Windows installers (UB-Mannheim) put it under
# `C:\Program Files\Tesseract-OCR\tesseract.exe` which is NOT on PATH by default
# for Python's child process unless explicitly configured.
if sys.platform == 'win32' and not pytesseract.pytesseract.tesseract_cmd:
    for candidate in (
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
    ):
        if Path(candidate).exists():
            pytesseract.pytesseract.tesseract_cmd = candidate
            break
    else:
        print('Warning: Tesseract not found in standard Windows paths. '
              'Install from https://github.com/UB-Mannheim/tesseract/wiki '
              'or set pytesseract.pytesseract.tesseract_cmd manually.',
              file=sys.stderr)

# === Project root resolution ===
# Make the toolspy project importable regardless of where this script is run from
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if not (_PROJECT_ROOT / "myenv").exists() and not (_PROJECT_ROOT / "tools").exists():
    _PROJECT_ROOT = Path.home() / "Documents" / "WebDev" / "toolspy"
sys.path.insert(0, str(_PROJECT_ROOT))


# === Constants ===
DB_FILENAME = '.cable_match_cache.db'
STATE_FILENAME = '.cable_match_state.json'
MATCHES_CSV_FILENAME = '_matches.csv'
STATE_FLUSH_INTERVAL = 30  # seconds
OCR_HEADER_TEMPLATE = (
    "# Extracted from {name} by text-extractor "
    "(OCR via Tesseract, lang={lang}, dpi={dpi}, preprocess={preprocess}, "
    "psm={psm}, oem={oem})\n"
)

# === Module-level worker globals (set by _worker_init) ===
_WORKER_TARGETS = None
_WORKER_DPI = None
_WORKER_LANG = None
_WORKER_ROTATION = None
_WORKER_PREPROCESS = None
_WORKER_PSM = None
_WORKER_OEM = None
_WORKER_USE_LEVENSHTEIN = False


# === Image preprocessing (improves OCR recall on technical drawings) ===

def _otsu_threshold(gray_pil):
    """Compute Otsu's threshold on a grayscale PIL image. Pure PIL, no numpy."""
    hist = gray_pil.histogram()
    total = sum(hist)
    if total == 0:
        return 128
    s = sum(i * c for i, c in enumerate(hist))
    sB = 0
    wB = 0
    var_max = 0
    thr = 0
    for t in range(256):
        wB += hist[t]
        if wB == 0:
            continue
        wF = total - wB
        if wF == 0:
            break
        sB += t * hist[t]
        mB = sB / wB
        mF = (s - sB) / wF
        v = wB * wF * (mB - mF) ** 2
        if v > var_max:
            var_max = v
            thr = t
    return thr


def _preprocess_for_ocr(pil, recipe):
    """Apply image preprocessing before Tesseract.

    Recipes:
      - 'none': return PIL as-is
      - 'gauss_otsu': grayscale + Gaussian blur (r=1) + Otsu threshold with -5 offset.
        This combo recovered 3B-463 (and kept 228/229/464/465/466) on the D0202-33 page
        where the default pipeline dropped 463 entirely. See cable_match_guide.md
        "Troubleshooting" for the test matrix.
    """
    if recipe == 'none':
        return pil
    if recipe == 'gauss_otsu':
        gray = ImageOps.grayscale(pil)
        blurred = gray.filter(ImageFilter.GaussianBlur(1))
        threshold = _otsu_threshold(gray) - 5
        return blurred.point(lambda v: 255 if v > threshold else 0)
    raise ValueError(f"Unknown preprocess recipe: {recipe}")


def _pick_better_text(text_a, text_b):
    """Pick the OCR variant with more Chinese chars (proxy for cleaner recognition).
    Returns (text, used_b). Prefers B only if it has 30%+ more Chinese AND >50 chars.
    """
    def cn_count(s):
        return sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
    ca = cn_count(text_a)
    cb = cn_count(text_b)
    if cb > ca * 1.3 and cb > 50:
        return (text_b, True)
    return (text_a, False)


def _build_tesseract_config(psm=None, oem=None):
    """Build Tesseract config string from --psm and --oem flags.
    None = use Tesseract's default (psm=3, oem=3). Empty string means no
    config override, which lets Tesseract pick its own defaults.
    """
    parts = []
    if psm is not None:
        parts.append(f'--psm {psm}')
    if oem is not None:
        parts.append(f'--oem {oem}')
    return ' '.join(parts)


def _render_and_ocr(pdf_path, dpi, lang, rotation, preprocess, psm=None, oem=None):
    """Render PDF pages and OCR with optional preprocessing and Tesseract config.
    Mirrors text_extractor._ocr_extract but applies preprocessing per page variant.
    """
    config = _build_tesseract_config(psm, oem)
    parts = [OCR_HEADER_TEMPLATE.format(
        name=pdf_path.name, lang=lang, dpi=dpi, preprocess=preprocess,
        psm=psm if psm is not None else 'default',
        oem=oem if oem is not None else 'default',
    )]
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        scale = dpi / 72.0
        for i, page in enumerate(pdf, 1):
            parts.append(f"\n=== Page {i} ===\n\n")
            pil = page.render(scale=scale).to_pil()
            try:
                if rotation is not None:
                    if rotation != 0:
                        pil = pil.rotate(-rotation, expand=True)
                    pil_pre = _preprocess_for_ocr(pil, preprocess)
                    text = pytesseract.image_to_string(pil_pre, lang=lang, config=config) or ""
                    if rotation != 0:
                        parts.append(f"# OCR rotated {rotation}° CW (forced)\n")
                    parts.append(text)
                else:
                    pil_pre = _preprocess_for_ocr(pil, preprocess)
                    text_default = pytesseract.image_to_string(pil_pre, lang=lang, config=config) or ""
                    pil_rot = pil_pre.rotate(-90, expand=True)
                    try:
                        text_rotated = pytesseract.image_to_string(pil_rot, lang=lang, config=config) or ""
                    finally:
                        pil_rot.close()
                    text, used_rot = _pick_better_text(text_default, text_rotated)
                    if used_rot:
                        parts.append("# OCR auto-rotated 90° CW (text was vertical in original)\n")
                    parts.append(text)
            finally:
                pil.close()
    finally:
        pdf.close()
    return "".join(parts)


def _render_and_ocr_both(pdf_path, dpi, lang, rotation, psm=None, oem=None):
    """Run OCR with both 'none' and 'gauss_otsu' preprocess recipes.
    Returns (text_none, text_gauss_otsu). Each text is independently cacheable
    (different preprocess key) and will be concatenated by the caller for
    cable-ID matching, taking the union of both pipelines' findings.
    """
    text_none = _render_and_ocr(pdf_path, dpi=dpi, lang=lang, rotation=rotation,
                                preprocess='none', psm=psm, oem=oem)
    text_gauss = _render_and_ocr(pdf_path, dpi=dpi, lang=lang, rotation=rotation,
                                 preprocess='gauss_otsu', psm=psm, oem=oem)
    return text_none, text_gauss


# === Cable ID normalization + fuzzy matching ===
#
# Four-tier recall-boosted matching, in order of cost:
#   1. exact        — target string appears in raw OCR text as-is
#   2. normalized   — after uppercasing + unifying separators (_ . space — etc.)
#   3. confusion    — 1-char substitution using CONFUSION table (3↔8, B↔8, 0↔O, ...)
#   4. levenshtein  — Levenshtein distance ≤ 1 on the normalized text
#
# Every match is tagged with its tier so users can filter for review in
# _matches.csv (`匹配方式` column).

# Character-level OCR confusions observed in D0202 cable drawings:
#   "3" ↔ "8"  — number-digit swap (e.g. 3B-463 → 38-463, very common)
#   "3" ↔ "J"  — when '3' is at the start of a label (e.g. 3B-463 → JB-463)
#   "8" ↔ "B"  — round-shape confusion
#   "0" ↔ "O"  — zero/letter-O
#   "1" ↔ "I", "L", "7"  — 1 confused with vertical strokes
#   "5" ↔ "S"  — s/5 round-shape
#   "B" ↔ "8"  — B vs round 8
#   "G" ↔ "6"  — G/6 round-shape
#   "-" ↔ "_", ".", " ", ""  — separator normalization
CONFUSION = {
    "3": ["8", "J"],
    "8": ["3", "B"],
    "0": ["O", "Q"],
    "O": ["0"],
    "1": ["I", "L", "7"],
    "I": ["1"],
    "5": ["S"],
    "S": ["5"],
    "G": ["6"],
    "B": ["8"],
    "-": ["_", ".", " ", ""],
}


def normalize_cable_text(s: str) -> str:
    """Normalize a cable label string for matching: uppercase + unify separators.

    Replaces underscores, dots, spaces, em/en dashes, middle dot, Chinese 一
    with ASCII hyphen, then collapses repeated hyphens. This handles the common
    OCR variants like 3B_463 / 3B.463 / 3B 463 / 3B—463 that all mean 3B-463.
    """
    s = s.upper()
    for sep in ['_', '.', ' ', '—', '—', '·', '一']:
        s = s.replace(sep, '-')
    while '--' in s:
        s = s.replace('--', '-')
    return s.strip('-')


def expand_variants(target: str) -> set:
    """Generate all 1-character-substitution variants of target using CONFUSION.

    Each variant is then normalized so different separator choices collapse to
    the same form (e.g. "3B_463" and "3B 463" both become "3B-463" — and we
    also keep the original "3B-463" as the canonical form).
    """
    seen = {target}
    for i, ch in enumerate(target):
        if ch in CONFUSION:
            for sub in CONFUSION[ch]:
                if sub == ch:
                    continue
                seen.add(target[:i] + sub + target[i+1:])
    return {normalize_cable_text(v) for v in seen if v}


def _levenshtein_match_one(target: str, norm_text: str, max_dist: int = 1) -> bool:
    """Check if any length-preserving window of norm_text is within edit
    distance ≤ max_dist of target. Cheap early-reject by char-set Hamming
    distance for short targets."""
    import Levenshtein
    n = len(target)
    if n < 4 or len(norm_text) < n:
        return False
    target_set = set(target)
    for i in range(len(norm_text) - n + 1):
        window = norm_text[i:i + n]
        if abs(len(set(window) ^ target_set)) > 2:
            continue
        if Levenshtein.distance(target, window) <= max_dist:
            return True
    return False


def find_matches(combined_text: str, targets: list, use_levenshtein: bool = False) -> dict:
    """Find which target cable IDs appear in the OCR text using up to 4 tiers.

    Returns dict: target -> match_type ('exact' | 'normalized' | 'confusion' | 'levenshtein').
    Tiers run in cost order; later tiers only process the targets not yet matched.

    WARNING: The Levenshtein tier is OFF by default because in D0202 testing it produced
    many false positives (e.g. "3B-B41" in "3B-B4111" terminal-pin text matched "3B-241",
    "3B-431" matched "3B-441"). Cable numbers in the 3B-4xx series are only 1-2 chars
    apart, so edit-distance-1 catches too many unrelated IDs. Enable explicitly with
    use_levenshtein=True (cable_match.py --levenshtein) for testing.
    """
    matches = {}
    remaining = list(targets)

    # --- Tier 1: exact (raw substring) ---
    not_matched = []
    for t in remaining:
        if t and t in combined_text:
            matches[t] = 'exact'
        else:
            not_matched.append(t)
    remaining = not_matched
    if not remaining:
        return matches

    # --- Tier 2: normalized (uppercase + unified separators) ---
    norm_text = normalize_cable_text(combined_text)
    not_matched = []
    for t in remaining:
        tn = normalize_cable_text(t)
        if tn and tn in norm_text:
            matches[t] = 'normalized'
        else:
            not_matched.append(t)
    remaining = not_matched
    if not remaining:
        return matches

    # --- Tier 3: confusion table (1-char substitution) ---
    target_variants = {}  # target -> set of normalized variants
    all_variants = set()
    for t in remaining:
        variants = expand_variants(t)
        target_variants[t] = variants
        all_variants.update(variants)
    if all_variants:
        # Sort by length desc, then alpha — gives deterministic regex order.
        sorted_variants = sorted(all_variants, key=lambda x: (-len(x), x))
        pattern = r'(?:^|[^A-Z0-9])(' + '|'.join(re.escape(v) for v in sorted_variants) + r')(?:[^A-Z0-9]|$)'
        try:
            conf_re = re.compile(pattern)
            variant_to_target = {}
            for t, variants in target_variants.items():
                for v in variants:
                    variant_to_target.setdefault(v, t)
            for m in conf_re.finditer(norm_text):
                matched = m.group(1)
                t = variant_to_target.get(matched)
                if t and t not in matches:
                    matches[t] = 'confusion'
        except re.error:
            # Regex too complex (unlikely with 346 targets × ~20 variants).
            # Fall back to per-target search.
            for t in remaining:
                if t in matches:
                    continue
                for v in target_variants[t]:
                    if v in norm_text:
                        matches[t] = 'confusion'
                        break
    remaining = [t for t in remaining if t not in matches]
    if not remaining or not use_levenshtein:
        return matches

    # --- Tier 4: Levenshtein (edit distance ≤ 1) on the normalized text ---
    # EXPERIMENTAL: off by default. See docstring warning.
    for t in remaining:
        tn = normalize_cable_text(t)
        if tn and _levenshtein_match_one(tn, norm_text, max_dist=1):
            matches[t] = 'levenshtein'

    return matches


# === SQLite cache ===

def _cache_key(content_hash: str, preprocess: str = 'none', psm=None, oem=None) -> str:
    """Derive a cache row key that distinguishes (content_hash, preprocess, psm, oem) tuples.
    For default config (preprocess='none', psm in (None, 3), oem in (None, 3)) we use the
    raw hash (backward compatible with rows written before the psm/oem columns existed).
    Other recipes get a suffix that captures the non-default dimensions.
    """
    psm_part = '' if psm in (None, 3) else f'__psm{psm}'
    oem_part = '' if oem in (None, 3) else f'__oem{oem}'
    if preprocess == 'none' and not psm_part and not oem_part:
        return content_hash
    return f"{content_hash}::{preprocess}{psm_part}{oem_part}"


def init_db(db_path: Path):
    """Create the cache DB if it doesn't exist. Migrate schema for new columns."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ocr_cache (
                content_hash TEXT PRIMARY KEY,
                ocr_text TEXT NOT NULL,
                ocr_dpi INTEGER,
                ocr_lang TEXT,
                ocr_rotation INTEGER,
                ocr_preprocess TEXT,
                ocr_psm INTEGER,
                ocr_oem INTEGER,
                ocr_at TEXT,
                pdf_size INTEGER,
                pdf_mtime REAL
            )
        ''')
        cols = {row[1] for row in conn.execute('PRAGMA table_info(ocr_cache)').fetchall()}
        if 'ocr_preprocess' not in cols:
            conn.execute('ALTER TABLE ocr_cache ADD COLUMN ocr_preprocess TEXT')
        if 'ocr_psm' not in cols:
            conn.execute('ALTER TABLE ocr_cache ADD COLUMN ocr_psm INTEGER')
        if 'ocr_oem' not in cols:
            conn.execute('ALTER TABLE ocr_cache ADD COLUMN ocr_oem INTEGER')
        # WAL mode for concurrent reads/writes
        conn.execute('PRAGMA journal_mode=WAL')
        conn.commit()
    finally:
        conn.close()


def get_cached_text(db_path: Path, content_hash: str, preprocess: str = 'none',
                    psm=None, oem=None) -> str:
    """Retrieve cached OCR text matching content hash + preprocess + psm + oem.
    Returns None on miss (including: cached with different params, or DB error).
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            row = conn.execute(
                'SELECT ocr_text FROM ocr_cache WHERE content_hash = ?',
                (_cache_key(content_hash, preprocess, psm, oem),)
            ).fetchone()
            if row:
                return row[0]
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return None


def put_cached_text(db_path: Path, content_hash: str, text: str,
                    dpi: int, lang: str, rotation,
                    preprocess: str, psm=None, oem=None,
                    pdf_size: int = 0, pdf_mtime: float = 0.0):
    """Store OCR text in cache. Key is (content_hash, preprocess, psm, oem) — a PDF cached
    with one param set does not serve as cache for a different param set.
    Uses a derived key (`hash::recipe__psmN__oemN`) so the same PDF can have multiple
    cache rows when run with different param combinations."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            conn.execute('''
                INSERT OR REPLACE INTO ocr_cache
                (content_hash, ocr_text, ocr_dpi, ocr_lang, ocr_rotation,
                 ocr_preprocess, ocr_psm, ocr_oem, ocr_at, pdf_size, pdf_mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (_cache_key(content_hash, preprocess, psm, oem), text, dpi, lang, rotation,
                  preprocess, psm, oem, datetime.datetime.now().isoformat(),
                  pdf_size, pdf_mtime))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"  Warning: failed to write cache: {e}", file=sys.stderr)


# === Target loading ===

def load_targets(csv_path: Path) -> list:
    targets = []
    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = (row.get('电缆编号') or '').strip()
            if t and t not in targets:
                targets.append(t)
    return targets


# === PDF discovery ===

def discover_pdfs(input_path: Path, target_set: set) -> list:
    """Discover all unique input PDFs, with content-hash dedup."""
    pdfs = []
    seen = set()
    for p in sorted(input_path.rglob('*.pdf')):
        if not p.is_file():
            continue
        rel = p.relative_to(input_path)
        # Skip if any ancestor dir name matches a target cable number
        if any(part in target_set for part in rel.parts[:-1]):
            continue
        try:
            with open(p, 'rb') as f:
                h = hashlib.sha256(f.read()).hexdigest()
        except (OSError, PermissionError):
            continue
        if h in seen:
            continue
        seen.add(h)
        pdfs.append(p)
    return pdfs


# === Worker function (for multiprocessing) ===

def _worker_init(targets, dpi, lang, rotation, preprocess, psm=None, oem=None,
                  use_levenshtein=False):
    """Initialize worker globals. Called once per worker process."""
    global _WORKER_TARGETS, _WORKER_DPI, _WORKER_LANG, _WORKER_ROTATION, _WORKER_PREPROCESS
    global _WORKER_PSM, _WORKER_OEM, _WORKER_USE_LEVENSHTEIN
    _WORKER_TARGETS = targets
    _WORKER_DPI = dpi
    _WORKER_LANG = lang
    _WORKER_ROTATION = rotation
    _WORKER_PREPROCESS = preprocess
    _WORKER_PSM = psm
    _WORKER_OEM = oem
    _WORKER_USE_LEVENSHTEIN = use_levenshtein


def _process_pdf(item):
    """Process a single PDF. Called in worker process.

    item: (pdf_path_str, content_hash, db_path_str_or_None)
    Returns: dict with results.

    For --preprocess=both, two OCR passes are made (none + gauss_otsu), each
    cached independently. Cable matching uses the concatenation of both texts
    so the union of findings is reported (dedup at the (cable, hash) level
    happens in the main process when writing _matches.csv).
    """
    pdf_path_str, content_hash, db_path_str = item
    pdf_path = Path(pdf_path_str)
    db_path = Path(db_path_str) if db_path_str else None

    texts = {}  # preprocess -> text
    cache_hit = False
    error_msg = None

    if _WORKER_PREPROCESS == 'both':
        recipes = ('none', 'gauss_otsu')
    else:
        recipes = (_WORKER_PREPROCESS,)

    # Try cache for each recipe
    if db_path:
        for prep in recipes:
            cached = get_cached_text(db_path, content_hash, prep,
                                     _WORKER_PSM, _WORKER_OEM)
            if cached is not None:
                texts[prep] = cached
                cache_hit = True

    # OCR anything not cached
    if not all(r in texts for r in recipes):
        try:
            missing = [r for r in recipes if r not in texts]
            if missing == ['none']:
                texts['none'] = _render_and_ocr(
                    pdf_path, dpi=_WORKER_DPI, lang=_WORKER_LANG,
                    rotation=_WORKER_ROTATION, preprocess='none',
                    psm=_WORKER_PSM, oem=_WORKER_OEM)
            elif missing == ['gauss_otsu']:
                texts['gauss_otsu'] = _render_and_ocr(
                    pdf_path, dpi=_WORKER_DPI, lang=_WORKER_LANG,
                    rotation=_WORKER_ROTATION, preprocess='gauss_otsu',
                    psm=_WORKER_PSM, oem=_WORKER_OEM)
            else:  # both missing — run the dual pass
                t_none, t_gauss = _render_and_ocr_both(
                    pdf_path, dpi=_WORKER_DPI, lang=_WORKER_LANG,
                    rotation=_WORKER_ROTATION,
                    psm=_WORKER_PSM, oem=_WORKER_OEM)
                texts['none'] = t_none
                texts['gauss_otsu'] = t_gauss

            # Write each newly-OCR'd variant to its own cache row
            if db_path:
                try:
                    pdf_stat = pdf_path.stat()
                    for prep, t in texts.items():
                        if prep in missing and t:
                            put_cached_text(
                                db_path, content_hash, t,
                                _WORKER_DPI, _WORKER_LANG, _WORKER_ROTATION,
                                prep, _WORKER_PSM, _WORKER_OEM,
                                pdf_stat.st_size, pdf_stat.st_mtime)
                except Exception:
                    pass
        except Exception as e:
            error_msg = str(e)

    if error_msg:
        return {'path': pdf_path_str, 'hash': content_hash,
                'matches': [], 'cache_hit': cache_hit,
                'no_text': True, 'error': error_msg}

    if not texts or not any(texts.values()):
        return {'path': pdf_path_str, 'hash': content_hash, 'matches': [],
                'cache_hit': cache_hit, 'no_text': True}

    # For matching, concatenate all OCR outputs (union strategy for 'both')
    combined = '\n'.join(t for t in texts.values() if t)
    norm = re.sub(r'\s+', ' ', combined).strip()
    if not norm:
        return {'path': pdf_path_str, 'hash': content_hash, 'matches': [],
                'match_types': {}, 'cache_hit': cache_hit, 'no_text': True}

    match_types = find_matches(combined, _WORKER_TARGETS, use_levenshtein=_WORKER_USE_LEVENSHTEIN)
    return {'path': pdf_path_str, 'hash': content_hash,
            'matches': list(match_types.keys()),
            'match_types': match_types,
            'cache_hit': cache_hit, 'no_text': False}


# === State I/O ===

def write_state(state: dict, state_path: Path):
    """Atomically write state.json."""
    state['last_updated'] = datetime.datetime.now().isoformat()
    tmp_path = state_path.with_suffix('.tmp')
    try:
        with open(tmp_path, 'w') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, state_path)
    except Exception as e:
        print(f"Warning: failed to write state: {e}", file=sys.stderr)


def load_state(state_path: Path) -> dict:
    """Load state.json if it exists."""
    if not state_path.exists():
        return None
    try:
        with open(state_path) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: failed to load state.json: {e}", file=sys.stderr)
        return None


# === Main process ===

def main():
    parser = argparse.ArgumentParser(
        description='cable_match v2: multi-target PDF organizer with SQLite cache, state.json, multiprocessing.',
    )
    parser.add_argument('--csv', required=True, help='CSV file with 电缆编号 column')
    parser.add_argument('--input', required=True, help='Folder to scan recursively for PDFs')
    parser.add_argument('--output', help='Output root (default: same as --input)')
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--lang', default='chi_sim+eng')
    parser.add_argument('--rotation', type=int, default=None, choices=[0, 90, 180, 270],
                        help='Force PDF page rotation (CW=positive). Default: auto-detect.')
    parser.add_argument('--preprocess', default='none', choices=['none', 'gauss_otsu', 'both'],
                        help='Image preprocessing before OCR. Default "none" preserves the '
                             'baseline pipeline (best for most pages). "gauss_otsu" applies '
                             'grayscale + Gaussian blur (r=1) + Otsu threshold (-5) and helps '
                             'recover labels the raw pipeline drops on dense technical '
                             'drawings (e.g. 3B-463 on D0202-33) — but can regress recall on '
                             'other pages. "both" runs both recipes per PDF and takes the '
                             'union of matches (2x OCR time, best recall, recommended when '
                             'missing critical cable IDs is worse than re-OCR cost).')
    parser.add_argument('--psm', type=int, default=None,
                        help='Tesseract page segmentation mode. Default: 3 (fully auto). '
                             '6 = single uniform text block (good for tech drawings). '
                             '11 = sparse text. See https://tesseract-ocr.github.io/tessdoc/PSM.html')
    parser.add_argument('--oem', type=int, default=None,
                        help='Tesseract OCR engine mode. Default: 3 (auto). '
                             '1 = LSTM only, 0 = legacy only. '
                             'See https://tesseract-ocr.github.io/tessdoc/OEM.html')
    parser.add_argument('--workers', type=int, default=4, help='Parallel workers (default: 4)')
    parser.add_argument('--list', action='store_true', help='Dry-run: show matches, no copying')
    parser.add_argument('--resume', nargs='?', const='auto', default=None,
                        help='Resume from state file. Pass path or "auto" (use default state.json).')
    parser.add_argument('--no-cache', action='store_true', help='Disable SQLite OCR cache')
    parser.add_argument('--no-state', action='store_true', help='Disable state.json writing (no resume possible)')
    parser.add_argument('--levenshtein', action='store_true',
                        help='EXPERIMENTAL: enable Levenshtein-distance-1 tier in addition '
                             'to exact/normalized/confusion. OFF by default because in '
                             'D0202 testing it produced many false positives (cable numbers '
                             'in the 3B-4xx series are only 1-2 chars apart, so edit-distance-1 '
                             'catches too many unrelated IDs). Use --levenshtein to opt in.')
    args = parser.parse_args()

    # Paths
    csv_path = Path(args.csv).expanduser()
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser() if args.output else input_path
    output_path.mkdir(parents=True, exist_ok=True)

    db_path = None if args.no_cache else (output_path / DB_FILENAME)
    state_path = output_path / STATE_FILENAME
    matches_csv = output_path / MATCHES_CSV_FILENAME

    # Init DB
    if db_path:
        init_db(db_path)

    # Load targets
    targets = load_targets(csv_path)
    target_set = set(targets)
    print(f"Loaded {len(targets)} unique targets from CSV", flush=True)
    print(f"Input: {input_path}", flush=True)
    print(f"Output: {output_path}", flush=True)
    print(f"OCR: Tesseract ({args.lang}, {args.dpi} dpi, {args.workers} workers, "
          f"rotation={args.rotation or 'auto'}, preprocess={args.preprocess}, "
          f"psm={args.psm if args.psm is not None else 'default'}, "
          f"oem={args.oem if args.oem is not None else 'default'}, "
          f"fuzzy={'lev+conf' if args.levenshtein else 'conf'})", flush=True)
    if db_path:
        print(f"Cache DB: {db_path}", flush=True)
    if not args.no_state:
        print(f"State file: {state_path}", flush=True)

    # Determine resume path
    resume_path = None
    if args.resume == 'auto':
        resume_path = state_path
    elif args.resume:
        resume_path = Path(args.resume).expanduser()

    # Load state
    processed = set()
    state = {
        'started_at': datetime.datetime.now().isoformat(),
        'csv': str(csv_path),
        'input': str(input_path),
        'output': str(output_path),
        'dpi': args.dpi,
        'lang': args.lang,
        'rotation': args.rotation,
        'preprocess': args.preprocess,
        'psm': args.psm,
        'oem': args.oem,
        'total': 0,
        'processed': [],
        'no_match': [],
        'no_text': [],
        'failed': [],
        'matches': {t: [] for t in targets},
        'match_type_counts': {'exact': 0, 'normalized': 0, 'confusion': 0, 'levenshtein': 0},
    }
    if resume_path and resume_path.exists():
        prev = load_state(resume_path)
        if prev:
            processed = set(prev.get('processed', []))
            # Initialize state['processed'] with previously processed list
            # so the persisted state.json shows the full history.
            state['processed'] = list(processed)
            for t, paths in prev.get('matches', {}).items():
                if t in state['matches']:
                    state['matches'][t] = list(paths)
            print(f"Resumed: {len(processed)} PDFs already processed, "
                  f"{sum(len(v) for v in state['matches'].values())} matches loaded", flush=True)

    # Discover PDFs
    pdfs = discover_pdfs(input_path, target_set)
    state['total'] = len(pdfs)
    print(f"Discovered: {len(pdfs)} unique PDFs", flush=True)

    # Filter out already-processed
    todo = []
    for p in pdfs:
        rel = str(p.relative_to(input_path))
        if rel not in processed:
            todo.append(p)

    print(f"To process: {len(todo)} (skipped {len(pdfs) - len(todo)} already done)", flush=True)
    print(flush=True)

    if not todo:
        print("Nothing to do.")
        if not args.no_state:
            write_state(state, state_path)
        return

    # Init matches.csv (header if not exists)
    # Load existing (cable, hash) pairs to avoid duplicates on re-runs
    existing_pairs = set()  # set of (cable, hash[:16])
    if not args.list and matches_csv.exists():
        try:
            with open(matches_csv, 'r', encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cable = row.get('电缆编号', '').strip()
                    h = row.get('内容hash前16', '').strip()
                    if cable and h:
                        existing_pairs.add((cable, h))
            if existing_pairs:
                print(f"_matches.csv: {len(existing_pairs)} existing entries (will skip duplicates)", flush=True)
        except Exception as e:
            print(f"Warning: failed to load existing _matches.csv: {e}", file=sys.stderr)
    if not args.list and not matches_csv.exists():
        with open(matches_csv, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['电缆编号', 'PDF文件名', '源相对路径', '匹配时间', '内容hash前16', '匹配方式'])

    # Setup SIGTERM/SIGINT handler (must be in main thread)
    state_ref = state
    state_path_ref = state_path
    args_ref = args

    def sigterm_handler(signum, frame):
        print("\n\n[SIGTERM/SIGINT] saving state and exiting gracefully...", flush=True)
        if not args_ref.no_state:
            write_state(state_ref, state_path_ref)
        sys.exit(0)

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    # Pre-compute content hashes
    print("Pre-hashing PDFs...", flush=True)
    pdf_hashes = {}
    for p in todo:
        try:
            with open(p, 'rb') as f:
                pdf_hashes[str(p)] = hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            print(f"  Warning: cannot hash {p}: {e}", file=sys.stderr)
    print(f"Hashed: {len(pdf_hashes)}/{len(todo)}", flush=True)
    print(flush=True)

    # Process with multiprocessing
    start_time = time.time()
    last_state_save = start_time
    completed = 0
    total = len(todo)

    initargs = (targets, args.dpi, args.lang, args.rotation, args.preprocess, args.psm, args.oem, args.levenshtein)

    print(f"Processing {total} PDFs with {args.workers} workers (multiprocessing)...", flush=True)

    with mp.Pool(processes=args.workers, initializer=_worker_init, initargs=initargs) as pool:
        work_items = [
            (str(p), pdf_hashes.get(str(p), ''), str(db_path) if db_path else None)
            for p in todo
        ]

        try:
            for result in pool.imap_unordered(_process_pdf, work_items):
                completed += 1
                pdf_path = Path(result['path'])
                rel = str(pdf_path.relative_to(input_path))
                content_hash = result['hash']
                matches = result.get('matches', [])
                cache_hit = result.get('cache_hit', False)

                elapsed = time.time() - start_time
                if result.get('error'):
                    print(f"[{completed}/{total}] {rel}: 错误: {result['error']}", flush=True)
                    state['failed'].append(rel)
                elif result.get('no_text'):
                    print(f"[{completed}/{total}] {rel}: 无文本", flush=True)
                    state['no_text'].append(rel)
                elif matches:
                    match_types = result.get('match_types', {})
                    cache_marker = " [缓存]" if cache_hit else ""
                    print(f"[{completed}/{total}] {rel}  ({elapsed:.0f}s)  匹配 {', '.join(matches)}{cache_marker}", flush=True)
                    for t in matches:
                        # Dedup: skip if (cable, content_hash) already in _matches.csv
                        pair = (t, content_hash[:16])
                        if pair in existing_pairs:
                            # Already processed in a previous run; just record in state
                            state['matches'].setdefault(t, []).append(rel)
                            continue
                        existing_pairs.add(pair)
                        state['matches'].setdefault(t, []).append(rel)
                        if not args.list:
                            mt = match_types.get(t, 'exact')
                            state['match_type_counts'][mt] = state['match_type_counts'].get(mt, 0) + 1
                            with open(matches_csv, 'a', encoding='utf-8', newline='') as f:
                                w = csv.writer(f)
                                w.writerow([t, pdf_path.name, rel,
                                            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                            content_hash[:16],
                                            mt])
                            dest_dir = output_path / t
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            dest = dest_dir / pdf_path.name
                            n = 1
                            while dest.exists():
                                dest = dest_dir / f"{pdf_path.stem}_{n}{pdf_path.suffix}"
                                n += 1
                            shutil.copy2(str(pdf_path), dest)
                else:
                    print(f"[{completed}/{total}] {rel}  ({elapsed:.0f}s)  不匹配", flush=True)
                    state['no_match'].append(rel)

                # Update state
                state['processed'].append(rel)

                # Periodic state save
                if not args.no_state and time.time() - last_state_save > STATE_FLUSH_INTERVAL:
                    write_state(state, state_path)
                    last_state_save = time.time()
        except KeyboardInterrupt:
            print("\n\n[KeyboardInterrupt] saving state and exiting gracefully...", flush=True)
            if not args.no_state:
                write_state(state, state_path)
            sys.exit(0)

    # Final state save
    if not args.no_state:
        write_state(state, state_path)

    # Cleanup empty target dirs
    if not args.list:
        for t in targets:
            d = output_path / t
            if d.exists() and not any(d.iterdir()):
                d.rmdir()

    # Summary
    total_matches = sum(len(v) for v in state['matches'].values())
    duration = time.time() - start_time
    print()
    print("=== 完成 ===")
    print(f"扫描: {total} (skip {len(pdfs) - total} already done)")
    print(f"总匹配 (含历史): {total_matches}")
    mtc = state.get('match_type_counts', {})
    if mtc and sum(mtc.values()) > 0:
        print(f"匹配方式分布:")
        for tier in ('exact', 'normalized', 'confusion', 'levenshtein'):
            n = mtc.get(tier, 0)
            if n > 0:
                print(f"  {tier:<12} {n}")
    print(f"耗时: {duration:.0f}s ({duration/60:.1f} min)")
    if db_path:
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                n_cached = conn.execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0]
                print(f"OCR cache: {n_cached} entries in {db_path.name}")
            finally:
                conn.close()
        except Exception:
            pass
    if not args.no_state:
        print(f"State: {state_path}")


if __name__ == '__main__':
    main()
