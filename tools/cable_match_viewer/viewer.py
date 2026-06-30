from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


def _natural_sort_key(cable: str) -> tuple:
    parts = re.split(r'(\d+)', cable)
    return tuple(int(p) if p.isdigit() else p for p in parts)


class CableDbViewer:
    """Reads from a cable.db produced by cable_engine.cli scan.

    Works with both the old (state.json + cache.db) and new (single
    cable.db) format by reading the SQLite schema at init time.
    """

    def __init__(self, db_path: Path, input_root: Path | None = None):
        self.db_path = Path(db_path).expanduser().resolve()
        if not self.db_path.exists():
            raise FileNotFoundError(f'cable.db not found: {db_path}')

        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self.input_root: Path | None = None

        self._detect_tables()
        self._resolve_input_root(input_root)

    # ------------------------------------------------------------------
    # Schema detection
    # ------------------------------------------------------------------
    def _detect_tables(self):
        tables = {
            r[0] for r in
            self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self._has_documents = 'documents' in tables
        self._has_entities = 'entities' in tables
        self._has_matches = 'matches' in tables
        self._has_ocr_pages = 'ocr_pages' in tables
        self._has_graph_nodes = 'graph_nodes' in tables
        self._has_graph_edges = 'graph_edges' in tables
        self._has_relations = 'relations' in tables

    def _resolve_input_root(self, cli_root: str | None):
        if cli_root:
            self.input_root = Path(cli_root).expanduser().resolve()
            return
        from_state = self._conn.execute(
            "SELECT value FROM scan_state WHERE key='input'"
        ).fetchone()
        if from_state:
            try:
                val = json.loads(from_state[0])
            except Exception:
                val = from_state[0]
            p = Path(val).expanduser().resolve()
            if p.exists():
                self.input_root = p
                return
        if self._has_documents:
            row = self._conn.execute(
                'SELECT pdf_rel_path, source_file FROM documents LIMIT 1'
            ).fetchone()
            if row:
                rel = row['pdf_rel_path'] or row['source_file'] or ''
                if rel:
                    self.input_root = Path('/')

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        doc_count = self._conn.execute(
            'SELECT COUNT(*) FROM documents'
        ).fetchone()[0] if self._has_documents else 0
        entity_count = self._conn.execute(
            'SELECT COUNT(*) FROM entities'
        ).fetchone()[0] if self._has_entities else 0
        match_count = self._conn.execute(
            'SELECT COUNT(*) FROM matches'
        ).fetchone()[0] if self._has_matches else 0

        cables = self._get_cable_set()
        cables_with_matches = sum(
            1 for c in cables
            if self._conn.execute(
                'SELECT COUNT(*) FROM matches WHERE cable=?',
                (c,),
            ).fetchone()[0] > 0
        ) if self._has_matches else 0

        return {
            'db_path': str(self.db_path),
            'input_root': str(self.input_root or ''),
            'documents': doc_count,
            'entities': entity_count,
            'matches': match_count,
            'total_cables': len(cables),
            'cables_with_matches': cables_with_matches,
            'engine_used': self._get_state('engine_used') or 'mixed',
        }

    def get_documents(self) -> list[dict]:
        if not self._has_documents:
            return []
        rows = self._conn.execute(
            'SELECT * FROM documents ORDER BY pdf_rel_path'
        ).fetchall()
        return [dict(r) for r in rows]

    def get_document(self, content_hash: str) -> dict | None:
        row = self._conn.execute(
            'SELECT * FROM documents WHERE content_hash=?',
            (content_hash,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        if self._has_entities:
            result['entities'] = [
                dict(r) for r in self._conn.execute(
                    'SELECT * FROM entities WHERE content_hash=? ORDER BY entity_type, id',
                    (content_hash,),
                ).fetchall()
            ]
        if self._has_matches:
            result['matches'] = [
                dict(r) for r in self._conn.execute(
                    'SELECT * FROM matches WHERE content_hash=?',
                    (content_hash,),
                ).fetchall()
            ]
        if self._has_ocr_pages:
            result['ocr_pages'] = [
                dict(r) for r in self._conn.execute(
                    'SELECT * FROM ocr_pages WHERE content_hash=? ORDER BY page',
                    (content_hash,),
                ).fetchall()
            ]
        if self._has_graph_nodes:
            result['graph_nodes'] = [
                dict(r) for r in self._conn.execute(
                    'SELECT * FROM graph_nodes'
                ).fetchall()
            ]
        return result

    def get_cables(self) -> list[dict]:
        if not self._has_matches:
            return []
        rows = self._conn.execute(
            'SELECT cable, COUNT(*) as cnt FROM matches GROUP BY cable ORDER BY cable'
        ).fetchall()
        return [
            {
                'cable': r['cable'],
                'match_count': r['cnt'],
                'documents': [
                    dict(d) for d in self._conn.execute(
                        'SELECT d.* FROM documents d JOIN matches m ON d.content_hash=m.content_hash WHERE m.cable=?',
                        (r['cable'],),
                    ).fetchall()
                ],
            }
            for r in rows
        ]

    def get_cable(self, cable: str) -> dict | None:
        if not self._has_matches:
            return None
        rows = self._conn.execute(
            'SELECT COUNT(*) FROM matches WHERE cable=?',
            (cable,),
        ).fetchall()
        if not rows or rows[0][0] == 0:
            return None
        docs = [
            dict(d) for d in self._conn.execute(
                'SELECT d.* FROM documents d JOIN matches m ON d.content_hash=m.content_hash WHERE m.cable=?',
                (cable,),
            ).fetchall()
        ]
        return {
            'cable': cable,
            'match_count': len(docs),
            'documents': docs,
        }

    def resolve_document_path(self, content_hash_or_rel: str) -> Path | None:
        if self.input_root and self.input_root.exists():
            candidate = self.input_root / content_hash_or_rel
            if candidate.is_file():
                return candidate
        if self._has_documents:
            row = self._conn.execute(
                'SELECT pdf_rel_path, source_file FROM documents WHERE content_hash=? OR pdf_rel_path=?',
                (content_hash_or_rel, content_hash_or_rel),
            ).fetchone()
            if row:
                rel = row['source_file'] or row['pdf_rel_path']
                if rel and rel.startswith('"'):
                    try:
                        rel = json.loads(rel)
                    except Exception:
                        pass
                if rel:
                    candidate = Path(rel)
                    if candidate.is_file():
                        return candidate
                    if self.input_root:
                        candidate = self.input_root / rel
                        if candidate.is_file():
                            return candidate
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_cable_set(self) -> set[str]:
        if not self._has_matches:
            return set()
        return {
            r[0] for r in
            self._conn.execute('SELECT DISTINCT cable FROM matches')
        }

    def _get_state(self, key: str) -> str | None:
        try:
            row = self._conn.execute(
                'SELECT value FROM scan_state WHERE key=?', (key,)
            ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
