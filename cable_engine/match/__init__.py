"""cable_engine.match — 4-tier cable ID match logic.

Extracted from scripts/cable_match.py::find_matches. The match logic is
pure (no I/O, no PDF rendering) and is the same regardless of which
OCR engine produced the text.

Four tiers, in cost order:
  1. exact        — raw substring (cable in text as-is)
  2. normalized   — uppercase + unified separators (_ . space —)
  3. confusion    — 1-char substitution using CONFUSION table
                     (3↔8, B↔8, 0↔O, 1↔I/L/7, 5↔S, G↔6, B↔8)
  4. levenshtein  — Levenshtein distance ≤ 1 on the normalized text
                     (OFF by default; produces FPs on 3B-4xx series)

Why a separate module:
  - It's pure logic; can be unit-tested without pytesseract / pdfium
  - The same 4 tiers are useful for any cable-ID search (UI, batch, API)
  - Future "plugins" can add their own tier (e.g. exact + regex)
    without touching the Stage pipeline
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable


# Character-level OCR confusions observed in D0202 cable drawings.
# Source: scripts/cable_match.py::CONFUSION — keep in sync.
CONFUSION: dict[str, list[str]] = {
    '3': ['8', 'J'],
    '8': ['3', 'B'],
    '0': ['O', 'Q'],
    'O': ['0'],
    '1': ['I', 'L', '7'],
    'I': ['1'],
    '5': ['S'],
    'S': ['5'],
    'G': ['6'],
    'B': ['8'],
    '-': ['_', '.', ' ', ''],
}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_cable_text(s: str) -> str:
    """Normalize for substring matching.

    Rules:
      - NFKC normalize (catches full-width / half-width / weird unicode)
      - uppercase
      - separators (->-_. ,;:) are normalized to a single space
      - whitespace collapsed to single spaces and stripped
    """
    if not s:
        return ''
    s = unicodedata.normalize('NFKC', s).upper()
    s = re.sub(r'[\-_\.,;:·・／/]+', ' ', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def expand_variants(target: str) -> set[str]:
    """Generate all 1-char-substitution variants of a target string using
    the CONFUSION table. The original target is always included.

    Example: expand_variants('3B-507') includes '8B-507', 'JB-507',
    '38-507', '33-507', '3B-5O7', '3B-S07', '3B-50T' (and many more).
    """
    if not target:
        return set()
    out = {target}
    for i, ch in enumerate(target):
        if ch in CONFUSION:
            for repl in CONFUSION[ch]:
                out.add(target[:i] + repl + target[i+1:])
    return out


# ---------------------------------------------------------------------------
# Levenshtein (kept small; max_dist=1 by design)
# ---------------------------------------------------------------------------
def _levenshtein_match_one(target: str, norm_text: str, max_dist: int = 1) -> bool:
    """Is `target` (or any window of same length) within edit-distance
    `max_dist` of `norm_text`?
    """
    n = len(target)
    if n < 4 or len(norm_text) < n:
        return False
    if max_dist == 1:
        for i in range(len(norm_text) - n + 1):
            window = norm_text[i:i + n]
            diffs = sum(1 for a, b in zip(target, window) if a != b)
            if diffs <= max_dist:
                return True
        return False
    # General case (slower) — kept for completeness.
    for i in range(len(norm_text) - n + 1):
        window = norm_text[i:i + n]
        if _levenshtein_le(target, window) <= max_dist:
            return True
    return False


def _levenshtein_le(a: str, b: str) -> int:
    """Levenshtein distance, capped at 2 (we only care if it's <= 1)."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 1:
        return 2
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y)
    # one-char insertion / deletion
    if len(a) > len(b):
        a, b = b, a
    for i in range(len(b)):
        if b[:i] + b[i+1:] == a:
            return 1
    return 2


# ---------------------------------------------------------------------------
# 4-tier match orchestrator
# ---------------------------------------------------------------------------
def find_matches(
    combined_text: str,
    targets: Iterable[str],
    use_levenshtein: bool = False,
) -> dict[str, str]:
    """Find which target cable IDs appear in `combined_text`.

    Returns dict: target -> tier ('exact' | 'normalized' | 'confusion' | 'levenshtein').
    Tiers run in cost order; later tiers only process targets not yet matched.

    Why tiers run in cost order:
      Tier 1 (exact substring) is the cheapest. Tier 4 (Levenshtein
      window scan) is the most expensive. Running them in order means
      most matches are found in tier 1-3 and never reach tier 4.
    """
    matches: dict[str, str] = {}
    remaining = [t for t in targets if t]
    if not remaining:
        return matches
    if not combined_text:
        return matches

    # --- Tier 1: exact (raw substring) ---
    not_matched: list[str] = []
    for t in remaining:
        if t in combined_text:
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

    # --- Tier 3: confusion (1-char substitution) ---
    not_matched = []
    for t in remaining:
        tn = normalize_cable_text(t)
        if not tn:
            not_matched.append(t)
            continue
        variants = expand_variants(tn)
        if any(v in norm_text for v in variants):
            matches[t] = 'confusion'
        else:
            not_matched.append(t)
    remaining = not_matched
    if not remaining:
        return matches

    # --- Tier 4: levenshtein (OFF by default; produces FPs) ---
    if use_levenshtein:
        not_matched = []
        for t in remaining:
            tn = normalize_cable_text(t)
            if tn and _levenshtein_match_one(tn, norm_text, max_dist=1):
                matches[t] = 'levenshtein'
            else:
                not_matched.append(t)
        # remaining: ignored, those don't match

    return matches


__all__ = [
    'CONFUSION', 'normalize_cable_text', 'expand_variants',
    'find_matches',
]
