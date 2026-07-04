"""cable_viewer.store — read-only wrapper over CableStore.

V6 philosophy: cable_topology is pre-built at scan time by the
TerminalStripAnalyzer. The viewer does a direct SQL lookup:

    SELECT * FROM cable_topology WHERE cable_id = ?

No BFS, no graph traversal, no on-demand computation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from cable_engine.storage import CableStore


class CableViewer:
    """Read-only facade over CableStore for the HTTP layer."""

    def __init__(self, store: CableStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Cable index (from cable_topology)
    # ------------------------------------------------------------------
    def list_cables(self) -> list[dict]:
        """Return distinct cable IDs from cable_topology."""
        rows = self._store.list_cable_topology()
        seen: dict[str, list[str]] = {}
        for r in rows:
            cid = r['cable_id']
            dh = r['document_hash']
            if cid not in seen:
                seen[cid] = []
            if dh not in seen[cid]:
                seen[cid].append(dh)
        out = []
        for cid, doc_hashes in sorted(seen.items()):
            out.append({
                'cable_id': cid,
                'occurrence_count': len(doc_hashes),
                'document_count': len(doc_hashes),
                'document_hashes': doc_hashes,
            })
        return out

    # ------------------------------------------------------------------
    # Cable detail — direct SQL, no BFS
    # ------------------------------------------------------------------
    def get_cable(self, cable_id: str) -> Optional[dict]:
        rows = self._store.list_cable_topology(cable_id=cable_id)
        if not rows:
            return None

        conductors = []
        doc_hashes: set[str] = set()
        for r in rows:
            conductors.append({
                'conductor_no': r['conductor_no'],
                'strip_name': r['strip_name'],
                'terminal_no': r['terminal_no'],
                'terminal_no_right': r['terminal_no_right'],
                'cabinet_name': r['cabinet_name'],
                'circuit_desc': r['circuit_desc'],
                'loop_id': r['loop_id'],
                'source_type': r['source_type'],
            })
            dh = r['document_hash']
            if dh:
                doc_hashes.add(dh)

        return {
            'cable_id': cable_id,
            'conductor_count': len(conductors),
            'conductors': conductors,
            'documents': [
                {
                    'content_hash': h,
                    'document': self._document_brief(h),
                }
                for h in sorted(doc_hashes)
            ],
        }

    # ------------------------------------------------------------------
    # Terminal strips
    # ------------------------------------------------------------------
    def list_terminal_strips(self) -> list[dict]:
        out = []
        for r in self._store.list_terminal_strips():
            out.append({
                'strip_name': r['strip_name'],
                'chinese_name': r['chinese_name'],
            })
        return out

    # ------------------------------------------------------------------
    # Document detail
    # ------------------------------------------------------------------
    def _document_brief(self, content_hash: str) -> Optional[dict]:
        row = self._store.get_document(content_hash)
        if row is None:
            return None
        return {
            'content_hash': row['content_hash'],
            'rel_path': row['rel_path'],
            'document_type': row['document_type'],
            'file_size': row['file_size'],
        }

    def get_document(self, content_hash: str) -> Optional[dict]:
        row = self._store.get_document(content_hash)
        if row is None:
            return None
        return {
            'content_hash': row['content_hash'],
            'rel_path': row['rel_path'],
            'document_type': row['document_type'],
            'file_size': row['file_size'],
            'file_mtime': row['file_mtime'],
        }

    def resolve_document_path(self, content_hash: str) -> Optional[Path]:
        row = self._store.get_document(content_hash)
        if row is None:
            return None
        rp = row['rel_path']
        if not rp:
            return None
        return Path(rp).expanduser()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return self._store.stats()


__all__ = ['CableViewer']
