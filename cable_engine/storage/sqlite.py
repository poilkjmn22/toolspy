"""cable_engine.storage.sqlite — unified SQLite I/O for cable-match.

Replaces the previous 3-file storage scheme (state.json + cache.db +
_matches.csv) with a single SQLite database file. Schema:

  documents        (content_hash, pdf_rel_path, pdf_size, pdf_mtime,
                   first_seen_at)
  ocr_pages        (content_hash, page, text, dpi, lang, rotation,
                   preprocess, psm, oem, engine, actual_engine, ocr_at)
  matches          (content_hash, cable, tier, matched_at)
  scan_state       (key, value)   -- replaces state.json's per-section dict

Why a single SQLite:
  - One file → atomic checkpoints via single fsync, easier to ship /
    back up / inspect.
  - SQL queries: "find all PDFs that matched cable 3B-507" = one
    `SELECT pdf_rel_path FROM documents JOIN matches USING (content_hash)
     WHERE matches.cable = ?` — no JSON parsing.
  - WAL mode gives concurrent reads while the worker writes, with no
    torn writes.
  - `scan_state` is a generic key/value table; the JSON's per-section
    structure (processed list, failed list, etc.) maps 1:1.

Public API (cable_engine.storage.sqlite.CableStore):
  - ensure_schema(db_path)         -- create tables if missing
  - open(db_path, read_only=False) -- returns a CableStore handle
  - close()
  - upsert_document(content_hash, pdf_rel_path, pdf_size, pdf_mtime)
  - upsert_ocr_page(content_hash, page, text, **meta)
  - has_ocr_page(content_hash, page, **meta_recipe) -- cache hit check
  - get_ocr_pages(content_hash)      -- all OCR rows for one PDF
  - upsert_match(content_hash, cable, tier, matched_at)
  - get_cables()                     -- distinct cable ids
  - get_cable_pdfs(cable)            -- all PDFs matching cable
  - get_pdf_cables(content_hash)      -- all cables matching PDF
  - set_state(key, value)            -- replaces state.json's {key: value}
  - get_state(key, default=None)
  - get_state_dict()                 -- all key/value
  - commit()

Backward compat: this module does NOT load legacy state.json /
cache.db / _matches.csv. Per user request, those are no longer
supported. To migrate from a previous run, re-run cable_engine
from scratch.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    content_hash    TEXT PRIMARY KEY,
    pdf_rel_path    TEXT NOT NULL,
    pdf_size        INTEGER,
    pdf_mtime       REAL,
    first_seen_at   REAL NOT NULL,
    document_type   TEXT NOT NULL DEFAULT 'pdf',
    source_file     TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(pdf_rel_path);

CREATE TABLE IF NOT EXISTS pages(
    page_id INTEGER PRIMARY KEY,
    content_hash TEXT,
    page_number INTEGER,
    width INTEGER,
    height INTEGER,
    page_type TEXT
);

CREATE TABLE IF NOT EXISTS ocr_pages (
    content_hash    TEXT NOT NULL,
    page            INTEGER NOT NULL,
    text            TEXT NOT NULL,
    dpi             INTEGER,
    lang            TEXT,
    rotation        INTEGER,
    preprocess     TEXT,
    psm             INTEGER,
    oem             INTEGER,
    engine          TEXT,
    actual_engine   TEXT,
    ocr_at          REAL NOT NULL,
    PRIMARY KEY (content_hash, page, dpi, lang, rotation,
                 preprocess, psm, oem, engine)
);
CREATE INDEX IF NOT EXISTS idx_ocr_engine ON ocr_pages(engine);
CREATE INDEX IF NOT EXISTS idx_ocr_hash ON ocr_pages(content_hash);

CREATE TABLE IF NOT EXISTS entities(
    id INTEGER PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    page            INTEGER NOT NULL,
    source_type     TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    text            TEXT,
    confidence      REAL DEFAULT 1.0,
    x               REAL,
    y               REAL,
    w               REAL,
    h               REAL,
    layer           TEXT,
    raw_handle      TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    content_hash    TEXT NOT NULL,
    cable           TEXT NOT NULL,
    tier            TEXT NOT NULL,
    matched_at      REAL NOT NULL,
    PRIMARY KEY (content_hash, cable)
);
CREATE INDEX IF NOT EXISTS idx_match_cable ON matches(cable);
CREATE INDEX IF NOT EXISTS idx_match_hash ON matches(content_hash);

CREATE TABLE IF NOT EXISTS relations(
    id INTEGER PRIMARY KEY,
    src_entity_id INTEGER,
    dst_entity_id INTEGER,
    relation_type TEXT,
    confidence REAL
);

CREATE TABLE IF NOT EXISTS graph_nodes(
    id INTEGER PRIMARY KEY,
    page INTEGER,
    x REAL,
    y REAL,
    node_type TEXT
);

CREATE TABLE IF NOT EXISTS graph_edges(
    src INTEGER,
    dst INTEGER
);

CREATE TABLE IF NOT EXISTS scan_state (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""


# ---------------------------------------------------------------------------
# Schema / connection helpers
# ---------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables / indexes if they don't exist. Idempotent.

    Also runs lightweight ALTER TABLE migrations for older cable.db
    files that predate the multi-source refactor (added source_type
    column, etc.).
    """
    conn.executescript(SCHEMA)
    # ---- Migrations: add columns that newer code expects. ----
    # SQLite doesn't support ADD COLUMN IF NOT EXISTS before 3.35,
    # so we wrap each migration in a try/except (PRAGMA inspect
    # first, then ALTER).
    _migrate_add_column_if_missing(conn, 'documents', 'document_type',
                                      "TEXT NOT NULL DEFAULT 'pdf'")
    _migrate_add_column_if_missing(conn, 'entities', 'source_type',
                                      "TEXT NOT NULL DEFAULT 'pdf'")
    _migrate_add_column_if_missing(conn, 'entities', 'confidence',
                                      "REAL DEFAULT 1.0")
    _migrate_add_column_if_missing(conn, 'entities', 'layer', 'TEXT')
    _migrate_add_column_if_missing(conn, 'entities', 'raw_handle', 'TEXT')
    conn.commit()


def _migrate_add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_def: str,
) -> None:
    """ALTER TABLE table ADD COLUMN col_def — silently skip if the
    column already exists. SQLite raises a generic OperationalError
    if the column is there; we use PRAGMA table_info to detect first.
    """
    cols = [row[1] for row in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()]
    if column in cols:
        return
    try:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {col_def}')
    except sqlite3.OperationalError:
        pass


def open_db(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open (and create if missing) a SQLite connection. WAL mode by
    default so readers don't block writers.
    """
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        # read_only via URI mode is the canonical way; falls back to plain
        # open if the file doesn't exist yet (can't open a non-existent
        # file in read-only mode).
        if db_path.exists():
            conn = sqlite3.connect(
                f'file:{db_path}?mode=ro', uri=True, timeout=30
            )
        else:
            conn = sqlite3.connect(str(db_path), timeout=30)
    else:
        conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    if not read_only:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
    return conn


# ---------------------------------------------------------------------------
# CableStore — high-level facade
# ---------------------------------------------------------------------------
class CableStore:
    """Unified storage for cable-match runs.

    A single CableStore wraps one SQLite connection and provides typed
    accessors for the four core tables (documents, ocr_pages, matches,
    scan_state). All write methods are auto-committed.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ----- lifecycle -----
    @classmethod
    def open(cls, db_path: Path, *, read_only: bool = False) -> 'CableStore':
        conn = open_db(db_path, read_only=read_only)
        if not read_only:
            ensure_schema(conn)
        return cls(conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def commit(self) -> None:
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ----- documents -----
    def upsert_document(self, content_hash: str, pdf_rel_path: str,
                        pdf_size: int | None, pdf_mtime: float | None,
                        source_type: str = 'pdf') -> None:
        """Insert/update one document.

        `source_type` is one of 'pdf', 'dwg', 'scan', etc. Defaults to
        'pdf' for backwards compatibility (older callers omit it).
        """
        now = time.time()
        self._conn.execute(
            """INSERT INTO documents (content_hash, pdf_rel_path, pdf_size,
                                     pdf_mtime, first_seen_at, document_type)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash) DO UPDATE SET
                   pdf_rel_path = excluded.pdf_rel_path,
                   pdf_size = COALESCE(excluded.pdf_size, documents.pdf_size),
                   pdf_mtime = COALESCE(excluded.pdf_mtime, documents.pdf_mtime),
                   document_type = excluded.document_type""",
            (content_hash, pdf_rel_path, pdf_size, pdf_mtime, now, source_type),
        )
        self._conn.commit()

    def get_document(self, content_hash: str) -> sqlite3.Row | None:
        cur = self._conn.execute(
            'SELECT * FROM documents WHERE content_hash = ?', (content_hash,)
        )
        return cur.fetchone()

    def get_all_documents(self) -> list[sqlite3.Row]:
        cur = self._conn.execute('SELECT * FROM documents')
        return cur.fetchall()

    def get_documents_by_cable(self, cable: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            """SELECT d.* FROM documents d
                 JOIN matches m ON m.content_hash = d.content_hash
                WHERE m.cable = ?
                ORDER BY d.pdf_rel_path""",
            (cable,),
        )
        return cur.fetchall()

    def get_all_paths(self) -> set[str]:
        """Return all known pdf_rel_path values (for dedup in discovery)."""
        cur = self._conn.execute('SELECT pdf_rel_path FROM documents')
        return {row['pdf_rel_path'] for row in cur.fetchall()}

    def get_all_hashes(self) -> set[str]:
        cur = self._conn.execute('SELECT content_hash FROM documents')
        return {row['content_hash'] for row in cur.fetchall()}

    # ----- ocr_pages (cache) -----
    def upsert_ocr_page(
        self,
        content_hash: str,
        page: int,
        text: str,
        *,
        dpi: int | None = None,
        lang: str | None = None,
        rotation: int | None = None,
        preprocess: str | None = None,
        psm: int | None = None,
        oem: int | None = None,
        engine: str | None = None,
        actual_engine: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO ocr_pages
                 (content_hash, page, text, dpi, lang, rotation,
                  preprocess, psm, oem, engine, actual_engine, ocr_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, page, dpi, lang, rotation,
                            preprocess, psm, oem, engine) DO UPDATE SET
                   text = excluded.text,
                   actual_engine = COALESCE(excluded.actual_engine,
                                            ocr_pages.actual_engine),
                   ocr_at = excluded.ocr_at""",
            (content_hash, page, text, dpi, lang, rotation, preprocess,
             psm, oem, engine, actual_engine, time.time()),
        )
        self._conn.commit()

    def has_ocr_page(
        self,
        content_hash: str,
        page: int,
        *,
        dpi: int | None = None,
        lang: str | None = None,
        rotation: int | None = None,
        preprocess: str | None = None,
        psm: int | None = None,
        oem: int | None = None,
        engine: str | None = None,
    ) -> sqlite3.Row | None:
        cur = self._conn.execute(
            """SELECT text FROM ocr_pages
                WHERE content_hash = ? AND page = ?
                  AND COALESCE(dpi, -1) = COALESCE(?, -1)
                  AND COALESCE(lang, '') = COALESCE(?, '')
                  AND COALESCE(rotation, -1) = COALESCE(?, -1)
                  AND COALESCE(preprocess, '') = COALESCE(?, '')
                  AND COALESCE(psm, -1) = COALESCE(?, -1)
                  AND COALESCE(oem, -1) = COALESCE(?, -1)
                  AND COALESCE(engine, '') = COALESCE(?, '')""",
            (content_hash, page, dpi, lang, rotation, preprocess, psm, oem, engine),
        )
        return cur.fetchone()

    def get_ocr_pages(self, content_hash: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            'SELECT * FROM ocr_pages WHERE content_hash = ? ORDER BY page',
            (content_hash,),
        )
        return cur.fetchall()

    # ----- entities (multi-source) -----
    def upsert_entity(
        self,
        content_hash: str,
        page: int,
        source_type: str,
        entity_type: str,
        *,
        text: str | None = None,
        confidence: float = 1.0,
        x: float | None = None,
        y: float | None = None,
        w: float | None = None,
        h: float | None = None,
        layer: str | None = None,
        raw_handle: str | None = None,
    ) -> None:
        """Insert/update one IR entity (text run, line, polyline, symbol).

        Multi-source: a DWG line and a PDF-OCR text run with the same
        content_hash coexist as separate entity rows. The Fusion Stage
        (future) decides how to merge them.
        """
        self._conn.execute(
            """INSERT INTO entities (content_hash, page, source_type,
                                     entity_type, text, confidence,
                                     x, y, w, h, layer, raw_handle)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (content_hash, page, source_type, entity_type, text,
             confidence, x, y, w, h, layer, raw_handle),
        )
        self._conn.commit()

    def get_entities(self, content_hash: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            'SELECT * FROM entities WHERE content_hash = ? ORDER BY page, id',
            (content_hash,),
        )
        return cur.fetchall()

    # ----- matches (cable → PDF) -----
    def upsert_match(
        self, content_hash: str, cable: str, tier: str = 'exact'
    ) -> None:
        self._conn.execute(
            """INSERT INTO matches (content_hash, cable, tier, matched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(content_hash, cable) DO UPDATE SET
                   tier = excluded.tier,
                   matched_at = excluded.matched_at""",
            (content_hash, cable, tier, time.time()),
        )
        self._conn.commit()

    def get_cables(self) -> list[str]:
        cur = self._conn.execute(
            'SELECT DISTINCT cable FROM matches ORDER BY cable'
        )
        return [row['cable'] for row in cur.fetchall()]

    def get_cable_pdfs(self, cable: str) -> list[str]:
        cur = self._conn.execute(
            """SELECT d.pdf_rel_path
                 FROM documents d
                 JOIN matches m ON m.content_hash = d.content_hash
                WHERE m.cable = ?
                ORDER BY d.pdf_rel_path""",
            (cable,),
        )
        return [row['pdf_rel_path'] for row in cur.fetchall()]

    def get_pdf_cables(self, content_hash: str) -> list[tuple[str, str]]:
        cur = self._conn.execute(
            'SELECT cable, tier FROM matches WHERE content_hash = ?',
            (content_hash,),
        )
        return [(row['cable'], row['tier']) for row in cur.fetchall()]

    def has_match(self, content_hash: str, cable: str) -> bool:
        cur = self._conn.execute(
            'SELECT 1 FROM matches WHERE content_hash = ? AND cable = ? LIMIT 1',
            (content_hash, cable),
        )
        return cur.fetchone() is not None

    def match_count(self) -> int:
        cur = self._conn.execute('SELECT COUNT(*) FROM matches')
        return cur.fetchone()[0]

    def cable_match_count(self) -> dict[str, int]:
        cur = self._conn.execute(
            'SELECT cable, COUNT(*) FROM matches GROUP BY cable'
        )
        return {row['cable']: row[1] for row in cur}

    # ----- scan_state (replaces state.json's per-section dict) -----
    def set_state(self, key: str, value: Any) -> None:
        """Store any JSON-serializable value under `key`."""
        self._conn.execute(
            'INSERT OR REPLACE INTO scan_state (key, value) VALUES (?, ?)',
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        cur = self._conn.execute(
            'SELECT value FROM scan_state WHERE key = ?', (key,)
        )
        row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row['value'])
        except (json.JSONDecodeError, TypeError):
            return default

    def get_state_dict(self) -> dict[str, Any]:
        cur = self._conn.execute('SELECT key, value FROM scan_state')
        out: dict[str, Any] = {}
        for row in cur.fetchall():
            try:
                out[row['key']] = json.loads(row['value'])
            except (json.JSONDecodeError, TypeError):
                out[row['key']] = row['value']
        return out

    def append_state_list(self, key: str, value: Any) -> None:
        """Append `value` to a list stored under `key` (creates if absent)."""
        existing = self.get_state(key, default=[])
        if not isinstance(existing, list):
            existing = [existing]
        existing.append(value)
        self.set_state(key, existing)

    def increment_state_counter(self, key: str, delta: int = 1) -> int:
        cur = self._conn.execute(
            'SELECT value FROM scan_state WHERE key = ?', (key,)
        )
        row = cur.fetchone()
        current = 0
        if row is not None:
            try:
                current = int(json.loads(row['value']))
            except (json.JSONDecodeError, TypeError, ValueError):
                current = 0
        current += delta
        self.set_state(key, current)
        return current

    # ----- derived queries (cross-table) -----
    def stats(self) -> dict[str, Any]:
        """Top-level stats for the run summary."""
        n_docs = self._conn.execute(
            'SELECT COUNT(*) FROM documents'
        ).fetchone()[0]
        n_ocr = self._conn.execute(
            'SELECT COUNT(*) FROM ocr_pages'
        ).fetchone()[0]
        n_matches = self.match_count()
        n_cables_matched = self._conn.execute(
            'SELECT COUNT(DISTINCT cable) FROM matches'
        ).fetchone()[0]
        processed = self.get_state('processed', [])
        failed = self.get_state('failed', [])
        no_match = self.get_state('no_match', [])
        no_text = self.get_state('no_text', [])
        return {
            'documents': n_docs,
            'ocr_pages': n_ocr,
            'matches': n_matches,
            'cables_matched': n_cables_matched,
            'processed': len(processed) if isinstance(processed, list) else 0,
            'failed': len(failed) if isinstance(failed, list) else 0,
            'no_match': len(no_match) if isinstance(no_match, list) else 0,
            'no_text': len(no_text) if isinstance(no_text, list) else 0,
        }


__all__ = ['CableStore', 'open_db', 'ensure_schema', 'SCHEMA']
