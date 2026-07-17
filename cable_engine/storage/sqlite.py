"""cable_engine.storage.sqlite — V5 unified SQLite I/O.

The V5 schema is built around the DocumentGraph + a minimal
rules-derived cable index. See the SCHEMA constant below for the
full schema.

Why a single SQLite:
  - One file → atomic checkpoints via single fsync, easier to ship /
    back up / inspect.
  - SQL queries: "find all terminals for cable 3B-463" = one
    `SELECT terminal_id FROM cable_terminals WHERE cable_id = ?`
    — no JSON parsing.
  - WAL mode gives concurrent reads while the worker writes.
  - `scan_state` is a generic key/value table for scan metadata.

Public API (cable_engine.storage.sqlite.CableStore):
  - ensure_schema(db_path)            -- create tables if missing
  - open(db_path, read_only=False)    -- returns a CableStore handle
  - close()
  - upsert_document(content_hash, rel_path, file_size, file_mtime, document_type)
  - list_cables()                     -- every cable id in the DB
  - list_terminals(cable_id=None)     -- every terminal, or those for one cable
  - list_loops(cable_id=None)         -- every loop, or those for one cable
  - upsert_cable(cable_id, document_hash)
  - upsert_terminal(terminal_id, document_hash)
  - upsert_loop(loop_id, document_hash)
  - upsert_cable_terminal(cable_id, terminal_id, document_hash, confidence, source_kind, properties)
  - upsert_cable_loop(cable_id, loop_id, document_hash, confidence)
  - upsert_graph_node(node, document_hash)  -- V5: from a GraphNode
  - upsert_graph_edge(edge, document_hash)  -- V5: from a GraphEdge
  - list_graph_nodes(document_hash, node_type=None) -> list[sqlite3.Row]
  - list_graph_edges(document_hash, edge_type=None) -> list[sqlite3.Row]
  - query_graph_neighbors(document_hash, node_id, edge_type=None) -> list[sqlite3.Row]
  - set_state(key, value)
  - get_state(key, default=None)
  - get_state_dict() -> dict
  - commit()
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


SCHEMA = """
-- ----------------------------------------------------------------------
-- Documents (one row per source file)
-- V6.5+: classification_primary + classification_confidence columns
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    content_hash    TEXT PRIMARY KEY,
    rel_path        TEXT NOT NULL,
    file_size       INTEGER,
    file_mtime      REAL,
    first_seen_at   REAL NOT NULL,
    document_type   TEXT NOT NULL DEFAULT 'pdf',
    source_file     TEXT,
    classification_primary     TEXT,             -- V6.5: e.g. 'terminal_strip'
    classification_confidence  REAL              -- V6.5: 0..1
);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(rel_path);
CREATE INDEX IF NOT EXISTS idx_documents_class ON documents(classification_primary);

-- ----------------------------------------------------------------------
-- Topology: the primary query table.
-- One row per cable conductor, built at scan time by the
-- TerminalStripAnalyzer (or, in the future, CircuitLoopAnalyzer).
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cable_topology (
    id              INTEGER PRIMARY KEY,
    cable_id        TEXT NOT NULL,
    conductor_no    INTEGER,
    strip_name      TEXT,
    terminal_no     INTEGER,
    terminal_no_remote TEXT,
    cabinet_name    TEXT,
    cabinet_name_remote TEXT,
    circuit_desc    TEXT,
    loop_id         TEXT,
    document_hash   TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'terminal_strip'
);
CREATE INDEX IF NOT EXISTS idx_ctop_cable ON cable_topology(cable_id);
CREATE INDEX IF NOT EXISTS idx_ctop_doc ON cable_topology(document_hash);

CREATE TABLE IF NOT EXISTS terminal_strips (
    strip_name      TEXT PRIMARY KEY,
    chinese_name    TEXT,
    document_hash   TEXT
);

-- ----------------------------------------------------------------------
-- V6.6: Cabinet regions detected from dashed rectangles.
-- One row per detected cabinet boundary in a single document.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cabinets (
    id              TEXT PRIMARY KEY,         -- "cab_NNN"
    document_hash   TEXT NOT NULL,
    name            TEXT,                     -- short cabinet code (e.g. "ZXW")
    location        TEXT,                     -- left-prefix (e.g. "11003")
    display_name    TEXT,                     -- "11003.ZXW" (composed)
    text_label      TEXT,                     -- descriptive long name
    bbox_x          REAL,
    bbox_y          REAL,
    bbox_w          REAL,
    bbox_h          REAL,
    layer           TEXT,
    boundary_handle TEXT,                     -- DWG handle of dashed polyline
    ltype           TEXT,                     -- dashed linetype name (e.g. ACAD_ISO10W100)
    points_json     TEXT                      -- JSON: [["x","y"], ...]
);
CREATE INDEX IF NOT EXISTS idx_cabinets_doc ON cabinets(document_hash);
CREATE INDEX IF NOT EXISTS idx_cabinets_display ON cabinets(display_name);

-- ----------------------------------------------------------------------
-- V6.6: Containment rows — which terminals (NO/ObjTerm.Name tags)
-- belong to which cabinet region.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cabinet_terminals (
    id              INTEGER PRIMARY KEY,
    cabinet_id      TEXT NOT NULL,
    document_hash   TEXT NOT NULL,
    terminal_id     TEXT NOT NULL,            -- "9D:13" (the NO tag value)
    terminal_kind   TEXT NOT NULL,            -- "NO" | "ObjTerm.Name"
    x               REAL,
    y               REAL
);
CREATE INDEX IF NOT EXISTS idx_cterm_cabinet ON cabinet_terminals(cabinet_id);
CREATE INDEX IF NOT EXISTS idx_cterm_doc ON cabinet_terminals(document_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cterm ON cabinet_terminals(cabinet_id, document_hash, terminal_id, terminal_kind);

-- ----------------------------------------------------------------------
-- V6.7+: Text entities for full-text search across documents.
-- Every TEXT/MTEXT/ATTRIB entity is persisted here during scanning.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS text_entities (
    id              INTEGER PRIMARY KEY,
    document_hash   TEXT NOT NULL,
    text            TEXT NOT NULL,
    entity_type     TEXT NOT NULL,             -- 'TEXT', 'MTEXT', 'ATTRIB'
    x               REAL,
    y               REAL,
    page            INTEGER DEFAULT 1,
    source          TEXT DEFAULT 'dwg'
);
CREATE INDEX IF NOT EXISTS idx_text_doc ON text_entities(document_hash);
CREATE INDEX IF NOT EXISTS idx_text_content ON text_entities(text);

-- ----------------------------------------------------------------------
-- V8: Cable-type info (model + cross-section, from WIRETYPE attribute)
-- One row per cable per document.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cable_info (
    cable_id        TEXT NOT NULL,
    document_hash   TEXT NOT NULL,
    wire_type       TEXT,                     -- e.g. 'ZBN-KYJYP2-23-1kV-4x6'
    PRIMARY KEY (cable_id, document_hash)
);
CREATE INDEX IF NOT EXISTS idx_cable_info_wire_type ON cable_info(wire_type);

-- ----------------------------------------------------------------------
-- Generic scan state bag (replaces state.json)
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_state (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""


# ---------------------------------------------------------------------------
# Schema / connection helpers
# ---------------------------------------------------------------------------
def _ensure_v65_columns(conn: sqlite3.Connection) -> None:
    """Add V6.5 classification columns to existing documents table
    if missing. Idempotent — safe to run on every open."""
    cur = conn.execute("PRAGMA table_info(documents)")
    cols = {row['name'] for row in cur.fetchall()}
    if 'classification_primary' not in cols:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN classification_primary TEXT"
        )
    if 'classification_confidence' not in cols:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN classification_confidence REAL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_class "
        "ON documents(classification_primary)"
    )


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables / indexes if they don't exist. Idempotent.

    V5 doesn't ship migrations from V4 — the schema is clean.
    If you have an old cable.db from V4, delete it and re-scan.

    V6.5: also runs `_ensure_v65_columns` so existing cable.db files
    get the classification columns without a manual migration.
    """
    conn.executescript(SCHEMA)
    _ensure_v65_columns(conn)
    conn.commit()


def open_db(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open (and create if missing) a SQLite connection. WAL mode by
    default so readers don't block writers.
    """
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        if db_path.exists():
            conn = sqlite3.connect(
                f'file:{db_path}?mode=ro', uri=True, timeout=30,
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
# CableStore — the high-level API the rest of cable_engine uses
# ---------------------------------------------------------------------------
class CableStore:
    """V5 unified store. All persistence flows through here."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        try:
            self._conn.commit()
        except Exception:
            pass
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    def upsert_document(
        self,
        content_hash: str,
        rel_path: str,
        file_size: Optional[int] = None,
        file_mtime: Optional[float] = None,
        document_type: str = 'dwg',
        source_file: Optional[str] = None,
        classification_primary: Optional[str] = None,
        classification_confidence: Optional[float] = None,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """INSERT INTO documents (content_hash, rel_path, file_size,
                                      file_mtime, first_seen_at,
                                      document_type, source_file,
                                      classification_primary,
                                      classification_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash) DO UPDATE SET
                   rel_path = excluded.rel_path,
                   file_size = excluded.file_size,
                   file_mtime = excluded.file_mtime,
                   classification_primary = excluded.classification_primary,
                   classification_confidence = excluded.classification_confidence""",
            (content_hash, rel_path, file_size, file_mtime,
             now, document_type, source_file,
             classification_primary, classification_confidence),
        )

    def list_documents(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            'SELECT * FROM documents ORDER BY rel_path'
        ).fetchall())

    def list_documents_by_classification(self) -> dict[str, int]:
        """Return {classification_primary: doc_count} for every
        classification that has at least one document. Includes
        documents with classification_primary IS NULL (as None)."""
        out: dict[str, int] = {}
        for r in self._conn.execute(
            """SELECT COALESCE(classification_primary, 'unclassified') AS cls,
                      COUNT(*) AS n
               FROM documents
               GROUP BY cls
               ORDER BY n DESC"""
        ).fetchall():
            out[r['cls']] = r['n']
        return out

    def list_unclassified_documents(
        self,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Documents whose classification has no analyzer yet — i.e.
        the analyzer dispatch in TopologyStage would skip them.

        Used by the viewer's "未分类图档" tab.

        Note: this is *not* the same as "no cable_topology rows".
        A circuit_loop drawing without topology rows might just have
        no cables (e.g. an empty template) — that's a different
        concern (see `unprocessed_documents` in stats).
        """
        return list(self._conn.execute(
            """SELECT d.content_hash, d.rel_path, d.classification_primary,
                      d.classification_confidence,
                      (SELECT COUNT(*) FROM cable_topology ct
                       WHERE ct.document_hash = d.content_hash) AS has_topology
               FROM documents d
               WHERE d.classification_primary IN (
                       'protection_diagram', 'panel_layout',
                       'monitoring_system', 'manufacturer_catalog',
                       'unknown'
                   )
                       OR d.classification_primary IS NULL
                   ORDER BY d.classification_primary, d.rel_path
               LIMIT ?""",
            (limit,),
        ).fetchall())

    def get_document(self, content_hash: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            'SELECT * FROM documents WHERE content_hash = ?',
            (content_hash,),
        ).fetchone()

    def get_document_path(self, content_hash: str) -> Optional[Path]:
        row = self.get_document(content_hash)
        if row is None:
            return None
        rp = row['rel_path']
        return Path(rp) if rp else None

    # ------------------------------------------------------------------
    # Topology (V6)
    # ------------------------------------------------------------------
    def upsert_cable_topology(
        self,
        cable_id: str,
        conductor_no: Optional[int],
        strip_name: Optional[str],
        terminal_no: Optional[int],
        terminal_no_remote: Optional[str] = None,
        cabinet_name: Optional[str] = None,
        cabinet_name_remote: Optional[str] = None,
        circuit_desc: Optional[str] = None,
        loop_id: Optional[str] = None,
        document_hash: str = '',
        source_type: str = 'terminal_strip',
    ) -> None:
        self._conn.execute(
            """INSERT INTO cable_topology
                   (cable_id, conductor_no, strip_name, terminal_no,
                    terminal_no_remote, cabinet_name, cabinet_name_remote,
                    circuit_desc, loop_id,
                    document_hash, source_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cable_id, conductor_no, strip_name, terminal_no,
             terminal_no_remote, cabinet_name, cabinet_name_remote,
             circuit_desc, loop_id,
             document_hash, source_type),
        )

    def bulk_upsert_cable_topology(
        self,
        rows: list[tuple],
    ) -> None:
        """Batch ``executemany`` equivalent of ``upsert_cable_topology``.
        Each tuple has 11 elements matching the column order::

            (cable_id, conductor_no, strip_name, terminal_no,
             terminal_no_remote, cabinet_name, cabinet_name_remote,
             circuit_desc, loop_id, document_hash, source_type)
        """
        self._conn.executemany(
            """INSERT INTO cable_topology
                   (cable_id, conductor_no, strip_name, terminal_no,
                    terminal_no_remote, cabinet_name, cabinet_name_remote,
                    circuit_desc, loop_id,
                    document_hash, source_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def list_cable_topology(
        self,
        cable_id: Optional[str] = None,
        document_hash: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        sql = 'SELECT * FROM cable_topology'
        params: list[Any] = []
        wheres: list[str] = []
        if cable_id is not None:
            wheres.append('cable_id = ?')
            params.append(cable_id)
        if document_hash is not None:
            wheres.append('document_hash = ?')
            params.append(document_hash)
        if wheres:
            sql += ' WHERE ' + ' AND '.join(wheres)
        sql += ' ORDER BY cable_id, conductor_no'
        return list(self._conn.execute(sql, params).fetchall())

    def search_cabinets(self, query: str) -> list[sqlite3.Row]:
        """Search cabinet names by keyword, return distinct results."""
        pattern = f'%{query}%'
        return list(self._conn.execute(
            """SELECT cabinet_name, cabinet_name_remote, document_hash,
                      COUNT(*) as conductor_count,
                      GROUP_CONCAT(DISTINCT cable_id) as cable_ids
               FROM cable_topology
               WHERE (cabinet_name LIKE ? OR cabinet_name_remote LIKE ?)
                 AND (cabinet_name IS NOT NULL OR cabinet_name_remote IS NOT NULL)
               GROUP BY cabinet_name, cabinet_name_remote, document_hash
               ORDER BY cabinet_name""",
            (pattern, pattern),
        ).fetchall())

    # ------------------------------------------------------------------
    # V6.7+: Text entities for full-text search
    # ------------------------------------------------------------------
    def bulk_upsert_text_entities(
        self,
        rows: list[tuple],
    ) -> None:
        """Batch insert text entities. Each tuple has 5 elements::

            (document_hash, text, entity_type, x, y)
        """
        self._conn.executemany(
            """INSERT INTO text_entities
                   (document_hash, text, entity_type, x, y)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )

    def delete_text_entities_for_document(self, document_hash: str) -> None:
        """Wipe every text entity row for a document."""
        self._conn.execute(
            "DELETE FROM text_entities WHERE document_hash = ?",
            (document_hash,),
        )

    def search_text(
        self,
        query: str,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        """Full-text search across all text entities using LIKE.

        Returns up to `limit` rows with matching text, joined with
        document metadata for display.
        """
        q = (query or '').strip().lower()
        if not q:
            return []
        pattern = f'%{q}%'
        return list(self._conn.execute(
            """SELECT te.document_hash, te.text, te.entity_type,
                      te.x, te.y, d.rel_path, d.classification_primary
               FROM text_entities te
               JOIN documents d ON d.content_hash = te.document_hash
               WHERE LOWER(te.text) LIKE ?
               ORDER BY te.document_hash, te.rowid
               LIMIT ?""",
            (pattern, limit),
        ).fetchall())

    def delete_topology_for_document(self, document_hash: str) -> None:
        self._conn.execute(
            'DELETE FROM cable_topology WHERE document_hash = ?',
            (document_hash,),
        )
        self._conn.execute(
            'DELETE FROM cable_info WHERE document_hash = ?',
            (document_hash,),
        )

    def bulk_upsert_cable_info(
        self,
        rows: list[tuple],
    ) -> None:
        """Batch insert cable_type info. Each tuple has 3 elements::

            (cable_id, wire_type, document_hash)
        """
        self._conn.executemany(
            """INSERT INTO cable_info (cable_id, document_hash, wire_type)
               VALUES (?, ?, ?)
               ON CONFLICT(cable_id, document_hash) DO UPDATE SET
                   wire_type = excluded.wire_type""",
            rows,
        )

    def delete_cable_info_for_document(self, document_hash: str) -> None:
        self._conn.execute(
            'DELETE FROM cable_info WHERE document_hash = ?',
            (document_hash,),
        )

    def upsert_terminal_strip(
        self,
        strip_name: str,
        chinese_name: Optional[str] = None,
        document_hash: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO terminal_strips (strip_name, chinese_name, document_hash)
               VALUES (?, ?, ?)
               ON CONFLICT(strip_name) DO UPDATE SET
                   chinese_name = COALESCE(terminal_strips.chinese_name,
                                           excluded.chinese_name)""",
            (strip_name, chinese_name, document_hash),
        )

    def bulk_upsert_terminal_strips(
        self,
        rows: list[tuple],
    ) -> None:
        """Batch ``executemany`` equivalent of ``upsert_terminal_strip``.
        Each tuple has 3 elements::

            (strip_name, chinese_name, document_hash)
        """
        self._conn.executemany(
            """INSERT INTO terminal_strips (strip_name, chinese_name, document_hash)
               VALUES (?, ?, ?)
               ON CONFLICT(strip_name) DO UPDATE SET
                   chinese_name = COALESCE(terminal_strips.chinese_name,
                                           excluded.chinese_name)""",
            rows,
        )

    def list_terminal_strips(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            'SELECT * FROM terminal_strips ORDER BY strip_name'
        ).fetchall())

    # ------------------------------------------------------------------
    # V6.6: Cabinet regions
    # ------------------------------------------------------------------
    def upsert_cabinet(
        self,
        cabinet_id: str,
        document_hash: str,
        name: Optional[str] = None,
        location: Optional[str] = None,
        display_name: Optional[str] = None,
        text_label: Optional[str] = None,
        bbox_x: Optional[float] = None,
        bbox_y: Optional[float] = None,
        bbox_w: Optional[float] = None,
        bbox_h: Optional[float] = None,
        layer: Optional[str] = None,
        boundary_handle: Optional[str] = None,
        ltype: Optional[str] = None,
        points_json: Optional[str] = None,
    ) -> None:
        """Idempotent insert for one cabinet region. `points_json` is a
        stringified JSON array like `[["x","y"], ...]` (kept as TEXT
        to avoid an extra geometry table for now)."""
        self._conn.execute(
            """INSERT INTO cabinets
                   (id, document_hash, name, location, display_name,
                    text_label, bbox_x, bbox_y, bbox_w, bbox_h,
                    layer, boundary_handle, ltype, points_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   document_hash = excluded.document_hash,
                   name = excluded.name,
                   location = excluded.location,
                   display_name = excluded.display_name,
                   text_label = excluded.text_label,
                   bbox_x = excluded.bbox_x,
                   bbox_y = excluded.bbox_y,
                   bbox_w = excluded.bbox_w,
                   bbox_h = excluded.bbox_h,
                   layer = excluded.layer,
                   boundary_handle = excluded.boundary_handle,
                   ltype = excluded.ltype,
                   points_json = excluded.points_json""",
            (cabinet_id, document_hash, name, location, display_name,
             text_label, bbox_x, bbox_y, bbox_w, bbox_h,
             layer, boundary_handle, ltype, points_json),
        )

    def bulk_upsert_cabinets(
        self,
        rows: list[tuple],
    ) -> None:
        """Batch ``executemany`` equivalent of ``upsert_cabinet``.
        Each tuple has 14 elements matching the column order::

            (cabinet_id, document_hash, name, location, display_name,
             text_label, bbox_x, bbox_y, bbox_w, bbox_h,
             layer, boundary_handle, ltype, points_json)
        """
        self._conn.executemany(
            """INSERT INTO cabinets
                   (id, document_hash, name, location, display_name,
                    text_label, bbox_x, bbox_y, bbox_w, bbox_h,
                    layer, boundary_handle, ltype, points_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   document_hash = excluded.document_hash,
                   name = excluded.name,
                   location = excluded.location,
                   display_name = excluded.display_name,
                   text_label = excluded.text_label,
                   bbox_x = excluded.bbox_x,
                   bbox_y = excluded.bbox_y,
                   bbox_w = excluded.bbox_w,
                   bbox_h = excluded.bbox_h,
                   layer = excluded.layer,
                   boundary_handle = excluded.boundary_handle,
                   ltype = excluded.ltype,
                   points_json = excluded.points_json""",
            rows,
        )

    def upsert_cabinet_terminal(
        self,
        cabinet_id: str,
        document_hash: str,
        terminal_id: str,
        terminal_kind: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> None:
        """Idempotent insert for one cabinet→terminal containment row."""
        self._conn.execute(
            """INSERT INTO cabinet_terminals
                   (cabinet_id, document_hash, terminal_id,
                    terminal_kind, x, y)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(cabinet_id, document_hash, terminal_id, terminal_kind)
               DO UPDATE SET x = excluded.x, y = excluded.y""",
            (cabinet_id, document_hash, terminal_id,
             terminal_kind, x, y),
        )

    def bulk_upsert_cabinet_terminals(
        self,
        rows: list[tuple],
    ) -> None:
        """Batch ``executemany`` equivalent of ``upsert_cabinet_terminal``.
        Each tuple has 6 elements::

            (cabinet_id, document_hash, terminal_id,
             terminal_kind, x, y)
        """
        self._conn.executemany(
            """INSERT INTO cabinet_terminals
                   (cabinet_id, document_hash, terminal_id,
                    terminal_kind, x, y)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(cabinet_id, document_hash, terminal_id, terminal_kind)
               DO UPDATE SET x = excluded.x, y = excluded.y""",
            rows,
        )

    def delete_cabinets_for_document(self, document_hash: str) -> None:
        """Wipe every cabinet + terminal-containment row for a document.
        Called by TopologyStage before re-persisting."""
        self._conn.execute(
            'DELETE FROM cabinet_terminals WHERE document_hash = ?',
            (document_hash,),
        )
        self._conn.execute(
            'DELETE FROM cabinets WHERE document_hash = ?',
            (document_hash,),
        )

    def list_cabinets(
        self,
        document_hash: Optional[str] = None,
        display_name_query: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """List detected cabinet regions, optionally filtered by
        document_hash and/or a LIKE match on `display_name`."""
        sql = 'SELECT * FROM cabinets'
        params: list[Any] = []
        wheres: list[str] = []
        if document_hash:
            wheres.append('document_hash = ?')
            params.append(document_hash)
        if display_name_query:
            wheres.append('display_name LIKE ?')
            params.append(f'%{display_name_query}%')
        if wheres:
            sql += ' WHERE ' + ' AND '.join(wheres)
        sql += ' ORDER BY document_hash, bbox_x, bbox_y'
        return list(self._conn.execute(sql, params).fetchall())

    def get_cabinet_terminals(self, cabinet_id: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            """SELECT * FROM cabinet_terminals
               WHERE cabinet_id = ?
               ORDER BY y, x""",
            (cabinet_id,),
        ).fetchall())

    def count_cabinets(self) -> int:
        row = self._conn.execute(
            'SELECT COUNT(*) AS n FROM cabinets'
        ).fetchone()
        return row['n'] if row else 0

    # ------------------------------------------------------------------
    # Scan state (key/value bag)
    # ------------------------------------------------------------------
    def set_state(self, key: str, value: Any) -> None:
        self._conn.execute(
            """INSERT INTO scan_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, json.dumps(value)),
        )

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            'SELECT value FROM scan_state WHERE key = ?',
            (key,),
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row['value'])
        except (TypeError, ValueError):
            return default

    def get_state_dict(self) -> dict:
        out: dict = {}
        for row in self._conn.execute('SELECT key, value FROM scan_state').fetchall():
            try:
                out[row['key']] = json.loads(row['value'])
            except (TypeError, ValueError):
                out[row['key']] = row['value']
        return out

    def append_state_list(self, key: str, value: Any) -> None:
        cur = self.get_state(key, [])
        if not isinstance(cur, list):
            cur = []
        if value in cur:
            return
        cur.append(value)
        self.set_state(key, cur)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        out: dict = {}

        # Scan metadata
        out['scan_input'] = self.get_state('input', '--')
        out['started_at'] = self.get_state('started_at', '--')

        # Basic row counts (same as before)
        for table in ('documents', 'cable_topology', 'terminal_strips'):
            try:
                row = self._conn.execute(
                    f'SELECT COUNT(*) AS n FROM {table}'
                ).fetchone()
                out[table] = row['n']
            except sqlite3.OperationalError:
                out[table] = 0

        # Distinct cables
        try:
            row = self._conn.execute(
                'SELECT COUNT(DISTINCT cable_id) AS n FROM cable_topology'
            ).fetchone()
            out['distinct_cables'] = row['n']
        except sqlite3.OperationalError:
            out['distinct_cables'] = 0

        # Total file size of all documents
        try:
            row = self._conn.execute(
                'SELECT COALESCE(SUM(file_size), 0) AS total FROM documents'
            ).fetchone()
            out['total_file_size'] = row['total']
        except sqlite3.OperationalError:
            out['total_file_size'] = 0

        # Document type breakdown
        out['documents_by_type'] = {}
        try:
            for r in self._conn.execute(
                'SELECT document_type, COUNT(*) AS n FROM documents GROUP BY document_type ORDER BY n DESC'
            ).fetchall():
                out['documents_by_type'][r['document_type']] = r['n']
        except sqlite3.OperationalError:
            pass

        # Business source type breakdown (from cable_topology)
        out['topology_by_source_type'] = {}
        try:
            for r in self._conn.execute(
                'SELECT source_type, COUNT(*) AS n FROM cable_topology GROUP BY source_type ORDER BY n DESC'
            ).fetchall():
                out['topology_by_source_type'][r['source_type']] = r['n']
        except sqlite3.OperationalError:
            pass

        # Distinct cables by source type
        out['cables_by_source_type'] = {}
        try:
            for r in self._conn.execute(
                'SELECT source_type, COUNT(DISTINCT cable_id) AS n FROM cable_topology GROUP BY source_type ORDER BY n DESC'
            ).fetchall():
                out['cables_by_source_type'][r['source_type']] = r['n']
        except sqlite3.OperationalError:
            pass

        # Unprocessed documents (in documents table but NOT in cable_topology)
        try:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM documents d
                   WHERE NOT EXISTS (
                       SELECT 1 FROM cable_topology ct
                       WHERE ct.document_hash = d.content_hash
                   )"""
            ).fetchone()
            out['unprocessed_documents'] = row['n']
        except sqlite3.OperationalError:
            out['unprocessed_documents'] = 0

        # V6.5: classification breakdown (replaces the rough
        # "unprocessed" view with the business-type-aware view).
        try:
            out['documents_by_classification'] = self.list_documents_by_classification()
        except sqlite3.OperationalError:
            out['documents_by_classification'] = {}

        # V6.5: documents whose classification has no analyzer.
        # (matches what the viewer's "未分类图档" tab shows.)
        try:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM documents
                    WHERE classification_primary IN (
                        'protection_diagram', 'panel_layout',
                        'monitoring_system', 'manufacturer_catalog',
                        'unknown'
                    )
                      OR classification_primary IS NULL"""
            ).fetchone()
            out['unmatched_documents'] = row['n']
        except sqlite3.OperationalError:
            out['unmatched_documents'] = 0

        # Empty-terminal cables (cables where all conductors have no terminal)
        out['cables_without_terminals'] = 0
        out['cables_with_terminals'] = 0
        try:
            row = self._conn.execute(
                """SELECT
                    COUNT(DISTINCT CASE WHEN terminal_no IS NOT NULL THEN cable_id END) AS with_t,
                    COUNT(DISTINCT cable_id) - COUNT(DISTINCT CASE WHEN terminal_no IS NOT NULL THEN cable_id END) AS without_t
                   FROM cable_topology"""
            ).fetchone()
            if row:
                out['cables_with_terminals'] = row['with_t']
                out['cables_without_terminals'] = row['without_t']
        except sqlite3.OperationalError:
            pass

        # Unique cabinets
        try:
            row = self._conn.execute(
                """SELECT COUNT(DISTINCT COALESCE(cabinet_name, cabinet_name_remote))
                   FROM cable_topology
                   WHERE cabinet_name IS NOT NULL OR cabinet_name_remote IS NOT NULL"""
            ).fetchone()
            out['distinct_cabinets'] = row[0]
        except sqlite3.OperationalError:
            out['distinct_cabinets'] = 0

        # V6.6: detected cabinet-region count (from the `cabinets` table).
        # Distinct from `distinct_cabinets` above, which counts names
        # in the cable_topology rows. The spatial analysis yields spatial
        # regions even for documents that produce no cable_topology rows.
        out['v66_cabinet_regions'] = 0
        out['v66_cabinet_terminal_rows'] = 0
        out['v66_documents_with_cabinets'] = 0
        try:
            r1 = self._conn.execute(
                'SELECT COUNT(*) AS n FROM cabinets'
            ).fetchone()
            out['v66_cabinet_regions'] = r1['n'] if r1 else 0
        except sqlite3.OperationalError:
            pass
        try:
            r2 = self._conn.execute(
                'SELECT COUNT(*) AS n FROM cabinet_terminals'
            ).fetchone()
            out['v66_cabinet_terminal_rows'] = r2['n'] if r2 else 0
        except sqlite3.OperationalError:
            pass
        try:
            r3 = self._conn.execute(
                'SELECT COUNT(DISTINCT document_hash) AS n FROM cabinets'
            ).fetchone()
            out['v66_documents_with_cabinets'] = r3['n'] if r3 else 0
        except sqlite3.OperationalError:
            pass

        # Total conductors (terminal records)
        try:
            row = self._conn.execute(
                'SELECT COUNT(*) AS n FROM cable_topology WHERE conductor_no IS NOT NULL'
            ).fetchone()
            out['conductors'] = row['n']
        except sqlite3.OperationalError:
            out['conductors'] = 0

        return out


__all__ = ['CableStore', 'open_db', 'ensure_schema', 'SCHEMA']