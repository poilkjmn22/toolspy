"""cable_viewer.store — read-only wrapper over CableStore.

V6 philosophy: cable_topology is pre-built at scan time by the
TerminalStripAnalyzer. The viewer does a direct SQL lookup:

    SELECT * FROM cable_topology WHERE cable_id = ?

No BFS, no graph traversal, no on-demand computation.
"""

from __future__ import annotations

import os
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
                'terminal_no_remote': r['terminal_no_remote'],
                'cabinet_name': r['cabinet_name'],
                'cabinet_name_remote': r['cabinet_name_remote'],
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
    # Cabinet search
    # ------------------------------------------------------------------
    def search_cabinets(self, query: str) -> list[dict]:
        rows = self._store.search_cabinets(query)
        out = []
        for r in rows:
            doc = self._document_brief(r['document_hash']) if r['document_hash'] else None
            out.append({
                'cabinet_name': r['cabinet_name'],
                'cabinet_name_remote': r['cabinet_name_remote'],
                'conductor_count': r['conductor_count'],
                'cable_ids': (r['cable_ids'] or '').split(','),
                'document': doc,
            })
        return out


    # ------------------------------------------------------------------
    # V6.6: Cabinet regions (read from `cabinets` table)
    # ------------------------------------------------------------------
    def list_cabinets(
        self,
        document_hash=None,
        display_name_query=None,
        limit: int = 1000,
    ) -> dict:
        """Return detected cabinet regions. Optionally filtered by
        document_hash and/or a LIKE match on `display_name`."""
        rows = self._store.list_cabinets(
            document_hash=document_hash,
            display_name_query=display_name_query,
        )
        if limit and len(rows) > limit:
            rows = rows[:limit]

        flat = []
        for r in rows:
            terms = self._store.get_cabinet_terminals(r['id'])
            doc = self._document_brief(r['document_hash']) if r['document_hash'] else None
            flat.append({
                'cabinet_id': r['id'],
                'document_hash': r['document_hash'],
                'name': r['name'] or '',
                'location': r['location'] or '',
                'display_name': r['display_name'] or '',
                'text_label': r['text_label'] or '',
                'bbox_x': r['bbox_x'],
                'bbox_y': r['bbox_y'],
                'bbox_w': r['bbox_w'],
                'bbox_h': r['bbox_h'],
                'layer': r['layer'] or '',
                'boundary_handle': r['boundary_handle'] or '',
                'ltype': r['ltype'] or '',
                'document': doc,
                'terminals': [
                    {
                        'terminal_id': t['terminal_id'],
                        'terminal_kind': t['terminal_kind'],
                        'x': t['x'],
                        'y': t['y'],
                    }
                    for t in terms
                ],
                'terminal_count': len(terms),
            })
        return {
            'total_cabinets': len(flat),
            'documents_with_cabinets': len({c['document_hash'] for c in flat}),
            'cabinets': flat,
        }

    def get_cabinet(self, cabinet_id: str):
        """Return one cabinet region with its terminals, or None."""
        rows = self._store.list_cabinets()
        for r in rows:
            if r['id'] == cabinet_id:
                terms = self._store.get_cabinet_terminals(cabinet_id)
                doc = self._document_brief(r['document_hash'])
                return {
                    'cabinet_id': r['id'],
                    'document_hash': r['document_hash'],
                    'name': r['name'] or '',
                    'location': r['location'] or '',
                    'display_name': r['display_name'] or '',
                    'text_label': r['text_label'] or '',
                    'bbox_x': r['bbox_x'],
                    'bbox_y': r['bbox_y'],
                    'bbox_w': r['bbox_w'],
                    'bbox_h': r['bbox_h'],
                    'layer': r['layer'] or '',
                    'boundary_handle': r['boundary_handle'] or '',
                    'ltype': r['ltype'] or '',
                    'points_json': r['points_json'] or '[]',
                    'document': doc,
                    'terminals': [
                        {
                            'terminal_id': t['terminal_id'],
                            'terminal_kind': t['terminal_kind'],
                            'x': t['x'],
                            'y': t['y'],
                        }
                        for t in terms
                    ],
                    'terminal_count': len(terms),
                }
        return None

    # ------------------------------------------------------------------
    # V8.5: Panel layout tree (屏面布置图)
    # ------------------------------------------------------------------
    def get_document_layout(self, content_hash: str) -> Optional[dict]:
        """Return the panel layout tree for a document, if available.
        Returns a dict with 'roots' (list of cabinet → area → device)."""
        import json
        raw = self._store.get_panel_layout(content_hash)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Text search (V6.7+)
    # ------------------------------------------------------------------
    def search_document_text(self, query: str, limit: int = 200) -> list[dict]:
        """Full-text search across all stored text entities.

        Returns a flat list of matching text snippets, each with
        document info. The viewer groups them by document_hash.
        """
        rows = self._store.search_text(query, limit=limit)
        out: list[dict] = []
        seen_hashes: set[str] = set()
        for r in rows:
            dh = r['document_hash']
            seen_hashes.add(dh)
            out.append({
                'document_hash': dh,
                'rel_path': self._strip_scan_root(r['rel_path'] or '') or r['rel_path'],
                'text': r['text'],
                'entity_type': r['entity_type'],
                'x': r['x'],
                'y': r['y'],
                'classification_primary': r['classification_primary'] or '',
            })
        return out

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return self._store.stats()

    # ------------------------------------------------------------------
    # Unclassified documents (V6.5 — viewer's "未分类图档" tab)
    # ------------------------------------------------------------------
    def list_unclassified_documents(self, limit: int = 500) -> list[dict]:
        """Documents whose classification has no analyzer, OR with no
        cable_topology rows at all. Returned in a viewer-friendly shape."""
        out = []
        for r in self._store.list_unclassified_documents(limit=limit):
            out.append({
                'content_hash': r['content_hash'],
                'rel_path': r['rel_path'],
                'classification_primary': r['classification_primary'] or '',
                'classification_confidence': r['classification_confidence'] or 0.0,
                'has_topology': bool(r['has_topology']),
            })
        return out

# ------------------------------------------------------------------
    # Document tree (V6.5.3 — viewer's "图纸" tab)
    # ------------------------------------------------------------------
    def _scan_root_prefix(self) -> str:
        """Return the `--input` directory stored in scan_state,
        normalized as `path/` prefix for trimming, or '' if unset."""
        scan_root = self._store.get_state('input', '') or ''
        scan_root = scan_root.rstrip('/')
        return scan_root + '/' if scan_root else ''

    def _strip_scan_root(self, path: str) -> str:
        """Trim the --input prefix from `path` so the tree shows only
        paths under the input directory."""
        prefix = self._scan_root_prefix()
        if not prefix or not path:
            return path
        if path.startswith(prefix):
            return path[len(prefix):]
        try:
            common = os.path.commonpath([prefix.rstrip('/'), path]) if path else ''
            if common and common == prefix.rstrip('/'):
                return path[len(prefix):]
        except (ValueError, OSError):
            pass
        try:
            abs_root = str(Path(prefix.rstrip('/')).resolve())
            abs_path = str(Path(path).resolve())
            if abs_path.startswith(abs_root + '/'):
                return abs_path[len(abs_root) + 1:]
        except (ValueError, OSError):
            pass
        return path

    def list_documents_tree(self, query: str = "") -> dict:
        """Return a tree of documents grouped by directory, optionally
        filtered by a fuzzy match on `rel_path`.

        All paths are trimmed of the `--input` prefix (the input
        directory itself is the root of the tree).

        Response shape:
          {
            'total_documents': int,
            'matching_documents': int,
            'with_topology': int,
            'scan_root': <--input prefix>,
            'tree': [
              {
                'name': <dir-name>,
                'type': 'directory',
                'children': [
                  {
                    'name': <filename>,
                    'type': 'file',
                    'content_hash': ...,
                    'rel_path': <path relative to --input>,
                    ...
                  },
                  ...
                ],
              },
              ...
            ],
          }
        """
        docs = self._store.list_documents()
        q = (query or '').strip().lower()
        scan_root = self._scan_root_prefix()

        total_documents = len(docs)
        with_topology = 0
        file_nodes: list[dict] = []

        for r in docs:
            rel_path = r['rel_path'] or ''
            content_hash = r['content_hash']
            display_path = self._strip_scan_root(rel_path) or rel_path

            topology_rows = self._store.list_cable_topology(document_hash=content_hash)
            cable_ids = {row['cable_id'] for row in topology_rows}
            source_types = sorted({row['source_type'] for row in topology_rows if row['source_type']})
            if cable_ids:
                with_topology += 1

            file_node = {
                'name': Path(display_path).name if display_path else content_hash,
                'type': 'file',
                'content_hash': content_hash,
                'rel_path': display_path,
                'document_type': r['document_type'] or '',
                'classification_primary': r['classification_primary'] or '',
                'classification_confidence': float(r['classification_confidence'] or 0.0),
                'cable_count': len(cable_ids),
                'conductor_count': len(topology_rows),
                'source_types': source_types,
            }

            if q and q not in display_path.lower():
                continue

            file_nodes.append(file_node)

        root: dict = {'name': '', 'type': 'directory', 'children': []}
        for node in file_nodes:
            rel = node['rel_path']
            parts = [p for p in rel.split('/') if p]
            if len(parts) <= 1:
                node['_leaf'] = True
                root['children'].append(node)
                continue
            dir_parts = parts[:-1]
            leaf_name = parts[-1]
            cur = root
            for d in dir_parts:
                child = next((c for c in cur['children'] if c.get('type') == 'directory' and c['name'] == d), None)
                if child is None:
                    child = {'name': d, 'type': 'directory', 'children': []}
                    cur['children'].append(child)
                cur = child
            node['name'] = leaf_name
            node['_leaf'] = True
            cur['children'].append(node)

        return {
            'total_documents': total_documents,
            'matching_documents': len(file_nodes),
            'with_topology': with_topology,
            'scan_root': scan_root.rstrip('/'),
            'tree': root['children'],
        }

    # ------------------------------------------------------------------
    # Document topology (V6.5.3 — viewer's "图纸" tab detail pane)
    # ------------------------------------------------------------------
    def get_document_topology(self, content_hash: str) -> Optional[dict]:
        """All cable_topology rows under one document, plus the
        document's own metadata. `rel_path` is trimmed of the
        `--input` prefix. V6.6 also returns detected cabinet regions
        for this document (each with bbox + contained terminals)."""
        doc = self._document_brief(content_hash)
        if doc is None:
            return None
        if doc.get('rel_path'):
            doc['rel_path'] = self._strip_scan_root(doc['rel_path']) or doc['rel_path']
        rows = self._store.list_cable_topology(document_hash=content_hash)
        conductors = []
        cable_ids: set[str] = set()
        source_types: set[str] = set()
        for r in rows:
            conductors.append({
                'cable_id': r['cable_id'],
                'conductor_no': r['conductor_no'],
                'strip_name': r['strip_name'],
                'terminal_no': r['terminal_no'],
                'terminal_no_remote': r['terminal_no_remote'],
                'cabinet_name': r['cabinet_name'],
                'cabinet_name_remote': r['cabinet_name_remote'],
                'circuit_desc': r['circuit_desc'],
                'loop_id': r['loop_id'],
                'source_type': r['source_type'],
            })
            cable_ids.add(r['cable_id'])
            if r['source_type']:
                source_types.add(r['source_type'])

        # V6.6: spatial cabinet regions for this document.
        cabinets_data = self.list_cabinets(document_hash=content_hash)
        cabinets = [
            {
                'cabinet_id': c['cabinet_id'],
                'name': c['name'],
                'location': c['location'],
                'display_name': c['display_name'],
                'text_label': c['text_label'],
                'bbox_x': c['bbox_x'],
                'bbox_y': c['bbox_y'],
                'bbox_w': c['bbox_w'],
                'bbox_h': c['bbox_h'],
                'boundary_handle': c['boundary_handle'],
                'ltype': c['ltype'],
                'terminal_count': c['terminal_count'],
                'terminals': c['terminals'],
            }
            for c in cabinets_data['cabinets']
        ]

        return {
            'document': doc,
            'cable_count': len(cable_ids),
            'conductor_count': len(conductors),
            'source_types': sorted(source_types),
            'conductors': sorted(conductors, key=lambda c: (c['cable_id'] or '', c['conductor_no'] or 0)),
            'cabinet_regions': cabinets,
            'cabinet_region_count': len(cabinets),
        }


__all__ = ['CableViewer']
