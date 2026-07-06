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
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    content_hash    TEXT PRIMARY KEY,
    rel_path        TEXT NOT NULL,
    file_size       INTEGER,
    file_mtime      REAL,
    first_seen_at   REAL NOT NULL,
    document_type   TEXT NOT NULL DEFAULT 'pdf',
    source_file     TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(rel_path);

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
def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables / indexes if they don't exist. Idempotent.

    V5 doesn't ship migrations from V4 — the schema is clean.
    If you have an old cable.db from V4, delete it and re-scan.
    """
    conn.executescript(SCHEMA)
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
    ) -> None:
        now = time.time()
        self._conn.execute(
            """INSERT INTO documents (content_hash, rel_path, file_size,
                                      file_mtime, first_seen_at,
                                      document_type, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash) DO UPDATE SET
                   rel_path = excluded.rel_path,
                   file_size = excluded.file_size,
                   file_mtime = excluded.file_mtime""",
            (content_hash, rel_path, file_size, file_mtime,
             now, document_type, source_file),
        )

    def list_documents(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            'SELECT * FROM documents ORDER BY rel_path'
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

    def delete_topology_for_document(self, document_hash: str) -> None:
        self._conn.execute(
            'DELETE FROM cable_topology WHERE document_hash = ?',
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

    def list_terminal_strips(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            'SELECT * FROM terminal_strips ORDER BY strip_name'
        ).fetchall())

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