"""cable_match_viewer.viewer — load state.json + cache.db, build reverse indices, query API.

This module is the backend for the cable-match-viewer web UI. It loads a
single stage's outputs (state.json + cache.db) once at startup and exposes
query methods that the aiohttp handlers in server.py call to render the UI.

Data model
----------
state.json['matches']: {cable_id: [pdf_rel_path, ...]}    (cable → PDFs)
state.json['processed']: [pdf_rel_path, ...]             (every PDF scanned)
state.json['input']: str                                  (PDF root dir on disk)

cache.db ocr_cache: {content_hash: {ocr_text, ocr_engine, ocr_preprocess, ...}}

The viewer joins these so you can ask:
  - what cables does PDF X match?   (by_path[x]['cables'])
  - what PDFs match cable Y?        (by_cable[y] = list of {pdf, content_hash, ocr_text, ...})
  - get OCR text for a content_hash (returns all stage OCR rows for comparison)
"""

from __future__ import annotations

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Cache schema
# ---------------------------------------------------------------------------
# The canonical list of ocr_cache columns written by the latest cable_match.py.
# Older versions wrote only a subset (initially just content_hash + ocr_text;
# later versions added ocr_engine, ocr_preprocess, etc.). _open_cache() detects
# the live schema via PRAGMA table_info and the SELECT statements pick only
# columns that actually exist, so opening an old cache.db doesn't crash.
#
# IMPORTANT: keep this list in the order expected by _ocr_row_from_db's
# canonical-name lookup. New columns can be appended at the end.
OCR_CACHE_COLUMNS = (
    'content_hash',
    'ocr_text',
    'ocr_engine',
    'ocr_preprocess',
    'ocr_lang',
    'ocr_dpi',
    'ocr_psm',
    'ocr_oem',
    'ocr_rotation',
    'ocr_at',
    'pdf_size',
    'pdf_mtime',
    'actual_engine',
)


# ---------------------------------------------------------------------------
# OCR-text header parser
# ---------------------------------------------------------------------------
# text-extractor prepends a single metadata comment line to each PDF's OCR
# text. We parse it so the viewer can show "extracted with Tesseract chi_sim+eng
# at 300 DPI, preprocess=none" without re-running anything.
#
# Example header:
#   # Extracted from 10-W978-B768ⅡZ-D0202-33.pdf by text-extractor (OCR via tesseract, lang=chi_sim+eng, dpi=300, preprocess=none, psm=default, oem=default)
_OCR_HEADER_RE = re.compile(
    r'^# Extracted from (?P<pdf>.+?) by text-extractor '
    r'\(OCR via (?P<engine>\S+), '
    r'lang=(?P<lang>[^,]+), '
    r'dpi=(?P<dpi>\d+), '
    r'preprocess=(?P<preprocess>[^,]+), '
    r'psm=(?P<psm>[^,]+), '
    r'oem=(?P<oem>[^)]+)\)'
)


@dataclass
class OCRRow:
    """One OCR row from cache.db, optionally with parsed metadata."""
    content_hash: str
    ocr_text: str
    ocr_engine: str = 'tesseract'  # REQUESTED engine (matches cache_key suffix)
    actual_engine: str | None = None  # engine that actually ran (or None if unknown)
    ocr_preprocess: str = 'none'
    ocr_lang: str = 'chi_sim+eng'
    ocr_dpi: int | None = None
    ocr_psm: int | None = None
    ocr_oem: int | None = None
    ocr_rotation: int | None = None
    pdf_size: int | None = None
    pdf_mtime: float | None = None
    ocr_at: str | None = None  # ISO timestamp the row was written

    # Parsed from the OCR-text header line, if present:
    header_pdf: str | None = None  # raw filename mentioned in header

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v is not None}
        return d


def parse_ocr_header(text: str) -> tuple[dict, str]:
    """Parse the leading metadata comment line from text-extractor's OCR output.

    Returns (metadata_dict, body_text). metadata_dict is empty if no header
    is present (e.g. the row was written by an older cable_match version).
    """
    if not text:
        return {}, text
    first, _, rest = text.partition('\n')
    m = _OCR_HEADER_RE.match(first)
    if not m:
        return {}, text
    meta = m.groupdict()
    # Strip the blank separator line if there is one.
    body = rest
    if body.startswith('\n'):
        body = body[1:]
    return meta, body


# ---------------------------------------------------------------------------
# Per-PDF metadata built from cache.db + state.json
# ---------------------------------------------------------------------------
@dataclass
class PDFEntry:
    """One PDF that was processed in this stage."""
    pdf_rel_path: str       # path relative to state.json['input'], e.g. "10-W978-B768…D0202-33.pdf"
    content_hash: str       # full sha256 hex
    pdf_size: int | None = None
    pdf_mtime: float | None = None
    # Cables that this PDF was matched to (per state.json['matches'] inverse).
    # A PDF can match multiple cables (e.g. D0202-33 → 3B-228, 3B-229, …).
    cables: list[str] = field(default_factory=list)
    # All OCR rows for this content_hash. Each PDF gets ONE cache row in the
    # current pipeline (cache_key includes recipe dims), but the viewer is
    # defensive and keeps them as a list so future multi-row scenarios work.
    ocr_rows: list[OCRRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'pdf_rel_path': self.pdf_rel_path,
            'content_hash': self.content_hash,
            'pdf_size': self.pdf_size,
            'pdf_mtime': self.pdf_mtime,
            'cables': self.cables,
            # Only include the OCR text for the row that was actually used to
            # generate state['matches']; fall back to the first OCR row.
            'ocr_text': self._best_ocr_text(),
            'ocr_text_preview': (self._best_ocr_text() or '')[:1500],
            'ocr_rows': [r.to_dict() for r in self.ocr_rows],
        }

    def _best_ocr_text(self) -> str:
        if not self.ocr_rows:
            return ''
        # Prefer the row whose actual_engine is NOT tesseract_fallback; failing
        # that, the most recent.
        good = [r for r in self.ocr_rows if r.actual_engine != 'tesseract_fallback']
        candidate = (good or self.ocr_rows)[0]
        return candidate.ocr_text


# ---------------------------------------------------------------------------
# Main viewer class
# ---------------------------------------------------------------------------

def _natural_sort_key(cable: str) -> tuple:
    """Sort key that compares digit runs numerically, so '3B-101' sorts
    before '3B-1010'. Splits on digit boundaries.
    """
    parts = re.split(r'(\d+)', cable)
    return tuple(int(p) if p.isdigit() else p for p in parts)


class CableMatchViewer:
    """Loads a stage's state.json + cache.db once; serves queries thereafter."""

    def __init__(self, state_path: Path, cache_path: Path, input_root: Path | None = None):
        self.state_path = Path(state_path).expanduser().resolve()
        self.cache_path = Path(cache_path).expanduser().resolve()
        # The PDF root on disk. State.json stores it under 'input'; CLI overrides
        # take precedence.
        self._state = self._load_state(self.state_path)
        self.input_root = (
            Path(input_root).expanduser().resolve()
            if input_root else Path(self._state['input']).expanduser().resolve()
        )
        if not self.input_root.exists():
            raise FileNotFoundError(
                f'PDF input root does not exist: {self.input_root}\n'
                f'  (state.json "input" = {self._state.get("input")!r})\n'
                f'  Pass --input-root to override.'
            )

        self._conn = self._open_cache(self.cache_path)
        self.by_path: dict[str, PDFEntry] = {}   # pdf_rel_path → PDFEntry
        self.by_cable: dict[str, list[PDFEntry]] = {}  # cable → [PDFEntry, ...]
        self.cables: list[str] = []              # sorted list of all known cables
        # _ocr_select_cols is set by _open_cache() (it PRAGMA's the table to
        # learn which columns exist). Guard with empty default in case any
        # downstream method is called before _build_indices().
        if not hasattr(self, '_ocr_select_cols'):
            self._ocr_select_cols = []
        self._build_indices()

    # -- public API --------------------------------------------------------

    @property
    def stats(self) -> dict:
        return {
            'state_path': str(self.state_path),
            'cache_path': str(self.cache_path),
            'input_root': str(self.input_root),
            'total_cables': len(self.cables),
            'cables_with_matches': sum(1 for c in self.cables if self.by_cable.get(c)),
            'total_pdfs': len(self._state.get('processed', [])),
            'failed': len(self._state.get('failed', [])),
            'no_match': len(self._state.get('no_match', [])),
            'no_text': len(self._state.get('no_text', [])),
            'ocr_rows': sum(len(e.ocr_rows) for e in self.by_path.values()),
            'engine_used': self._state.get('engine_used') or self._state.get('engine'),
            'lang': self._state.get('lang'),
            'started_at': self._state.get('started_at'),
            'last_updated': self._state.get('last_updated'),
        }

    def get_cables(self) -> list[dict]:
        """Return [{cable, pdf_count, ocr_bytes}, ...] sorted by cable name."""
        out = []
        for cable in self.cables:
            entries = self.by_cable.get(cable, [])
            ocr_bytes = sum(len(e._best_ocr_text()) for e in entries)
            out.append({
                'cable': cable,
                'pdf_count': len(entries),
                'ocr_bytes': ocr_bytes,
            })
        return out

    def get_cable(self, cable: str) -> dict | None:
        """Return {cable, pdfs: [{pdf_rel_path, content_hash, cables, ocr_text_preview}]}."""
        entries = self.by_cable.get(cable)
        if not entries:
            return None
        return {
            'cable': cable,
            'pdf_count': len(entries),
            'pdfs': [
                {
                    'pdf_rel_path': e.pdf_rel_path,
                    'content_hash': e.content_hash,
                    'pdf_size': e.pdf_size,
                    'cables': e.cables,
                    'ocr_text_preview': (e._best_ocr_text() or '')[:1500],
                }
                for e in entries
            ],
        }

    def get_pdf(self, pdf_rel_path: str) -> dict | None:
        """Return full detail (OCR text + metadata + cables) for one PDF."""
        entry = self.by_path.get(pdf_rel_path)
        if not entry:
            return None
        return entry.to_dict()

    def get_ocr_text_for_hash(self, content_hash: str) -> list[dict] | None:
        """Return every OCR row for a content_hash (across stages via cache_key dims)."""
        rows = []
        try:
            if not self._ocr_select_cols:
                return None
            cols_csv = ', '.join(self._ocr_select_cols)
            cursor = self._conn.execute(
                f'SELECT {cols_csv} FROM ocr_cache WHERE content_hash LIKE ?',
                (f'{content_hash}%',),
            )
            for r in cursor.fetchall():
                rows.append(_ocr_row_from_db(r, self._ocr_select_cols).to_dict())
        except sqlite3.Error as e:
            return None
        return rows or None

    def resolve_pdf_path(self, pdf_rel_path: str) -> Path | None:
        """Resolve pdf_rel_path to an absolute disk path, with traversal defense."""
        if pdf_rel_path not in self.by_path:
            return None  # not in whitelist (i.e. not in state.json processed)
        # Normalise and resolve to defeat ../ tricks.
        candidate = (self.input_root / pdf_rel_path).resolve()
        try:
            candidate.relative_to(self.input_root)
        except ValueError:
            return None  # resolved outside input_root → reject
        if not candidate.is_file():
            return None
        return candidate

    # -- private: loaders / indexers --------------------------------------

    def _load_state(self, path: Path) -> dict:
        """Load state.json. Tries UTF-8 first, then GBK / cp936 / gb18030
        for state files written on Win11 PowerShell (default cp936)."""
        for enc in ('utf-8', 'cp936', 'gb18030'):
            try:
                with open(path, encoding=enc) as f:
                    return json.load(f)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        raise ValueError(f'Could not load state.json: {path}')

    def _open_cache(self, path: Path) -> sqlite3.Connection:
        if not path.exists():
            raise FileNotFoundError(f'cache.db not found: {path}')
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = None  # tuples
        # Detect which columns ocr_cache actually has. Earlier versions of
        # cable_match.py only wrote `content_hash` + `ocr_text` + a few
        # optional fields; later versions added `ocr_engine`, `ocr_preprocess`,
        # `ocr_lang`, `ocr_dpi`, `ocr_psm`, `ocr_oem`, `ocr_rotation`, `ocr_at`,
        # `pdf_size`, `pdf_mtime`, `actual_engine`. We only SELECT what's
        # present so opening an old cache.db doesn't crash with
        # `no such column: ocr_engine`.
        try:
            cols = [row[1] for row in conn.execute(
                "PRAGMA table_info(ocr_cache)"
            ).fetchall()]
        except sqlite3.Error as e:
            # Table missing entirely — most likely an empty/miscreated db.
            # Treat as no columns; index building will be a no-op.
            raise sqlite3.DatabaseError(
                f'cache.db is missing the ocr_cache table ({e}). '
                f'This file was probably not generated by cable_match.py — '
                f'double-check the path.'
            ) from e
        self._available_cols = set(cols)
        # Canonical column order used by `_ocr_row_from_db()`. We pick only
        # the subset that exists in this db; the row parser will fill defaults
        # for anything missing.
        self._ocr_select_cols = [c for c in OCR_CACHE_COLUMNS if c in self._available_cols]
        return conn

    def _build_indices(self) -> None:
        """Build by_path and by_cable from state.json + cache.db."""
        # Step 1: by_path from cache.db (each row → one PDFEntry; cache_key
        # collapses to the bare content_hash because all stage runs use
        # the same recipe params).
        if not self._ocr_select_cols:
            # Nothing to load from cache.db (older schema with only
            # content_hash + ocr_text won't trip _ocr_row_from_db, but
            # an empty column list still needs handling).
            return
        select_sql = (
            f'SELECT {", ".join(self._ocr_select_cols)} FROM ocr_cache'
        )
        rows = self._conn.execute(select_sql).fetchall()
        hash_to_entry: dict[str, PDFEntry] = {}
        for r in rows:
            row = _ocr_row_from_db(r, self._ocr_select_cols)
            entry = PDFEntry(
                pdf_rel_path='',       # filled in step 2
                content_hash=row.content_hash,
                pdf_size=row.pdf_size,
                pdf_mtime=row.pdf_mtime,
                ocr_rows=[row],
            )
            hash_to_entry[row.content_hash] = entry

        # Step 2: join cache.db pdf_size/pdf_mtime → state.json processed list
        # by content_hash. state.json does not store the hash, so we hash the
        # processed PDF file ourselves. This is a one-time O(N) cost.
        import hashlib
        for rel in self._state.get('processed', []):
            abs_path = self.input_root / rel
            if not abs_path.is_file():
                # File missing on disk; skip (it will still appear in
                # state.json['failed'] or state.json['no_match']/no_text).
                continue
            try:
                h = hashlib.sha256(abs_path.read_bytes()).hexdigest()
            except OSError:
                continue
            entry = hash_to_entry.get(h)
            if entry is None:
                # No OCR cache row (shouldn't happen if cache.db is consistent).
                entry = PDFEntry(
                    pdf_rel_path=rel,
                    content_hash=h,
                    pdf_size=abs_path.stat().st_size,
                    ocr_rows=[],
                )
                hash_to_entry[h] = entry
            else:
                entry.pdf_rel_path = rel
            self.by_path[rel] = entry

        # Step 3: reverse-index by cable from state.json['matches'].
        # state.json stores cable → [rel_path, ...]. Walk it, populate entry.cables
        # for each PDF, and build by_cable.
        for cable, paths in self._state.get('matches', {}).items():
            if not isinstance(paths, list):
                continue
            cable_entries = []
            for rel in paths:
                entry = self.by_path.get(rel)
                if entry is None:
                    # File missing or hash mismatch; skip but log via stats.
                    continue
                if cable not in entry.cables:
                    entry.cables.append(cable)
                cable_entries.append(entry)
            if cable_entries:
                self.by_cable[cable] = cable_entries

        # Build sorted cable list: matched cables first (natural-sort), then
        # unmatched cables (natural-sort). Natural-sort so "3B-101" sorts
        # before "3B-1010", "1F-151" before "2F-151", etc. — much more
        # readable than lexicographic when scanning the side panel.
        matched_set = set(self.by_cable.keys())
        all_cables = list(self._state.get('matches', {}).keys())
        matched = sorted(
            (c for c in all_cables if c in matched_set),
            key=_natural_sort_key,
        )
        unmatched = sorted(
            (c for c in all_cables if c not in matched_set),
            key=_natural_sort_key,
        )
        self.cables = matched + unmatched

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def _ocr_row_from_db(row: tuple, select_cols: list[str]) -> OCRRow:
    """Build an OCRRow from a sqlite3 ocr_cache row tuple.

    Looks up each field by column NAME (via zip(select_cols, row)) rather than
    by hard-coded positional index. This makes the parser robust against
    cache.db files written by older cable_match.py that have a subset of
    the current schema. Missing columns get sensible defaults:

      ocr_engine       -> 'tesseract'
      ocr_preprocess   -> 'none'
      ocr_lang         -> 'chi_sim+eng'
      ocr_dpi/psm/oem  -> None
      actual_engine    -> None
    """
    # Build a {col_name -> value} dict from the row.
    if select_cols:
        kv = dict(zip(select_cols, row))
    else:
        # Empty column list = empty row. Shouldn't happen with a real
        # cache.db but be defensive.
        kv = {}
    text = kv.get('ocr_text') or ''
    header, _body = parse_ocr_header(text)
    return OCRRow(
        content_hash=kv.get('content_hash') or '',
        ocr_text=text,
        ocr_engine=kv.get('ocr_engine') or 'tesseract',
        ocr_preprocess=kv.get('ocr_preprocess') or 'none',
        ocr_lang=kv.get('ocr_lang') or 'chi_sim+eng',
        ocr_dpi=kv.get('ocr_dpi'),
        ocr_psm=kv.get('ocr_psm'),
        ocr_oem=kv.get('ocr_oem'),
        ocr_rotation=kv.get('ocr_rotation'),
        ocr_at=kv.get('ocr_at'),
        pdf_size=kv.get('pdf_size'),
        pdf_mtime=kv.get('pdf_mtime'),
        actual_engine=kv.get('actual_engine'),
        header_pdf=header.get('pdf'),
    )