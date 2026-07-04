"""cable_engine.cli — V6 topology-based pipeline.

  Loader            — DWG (PDF support deferred)
  TopologyStage     — builds cable_topology table (per-analyzer dispatch)

cable→conductor→terminal topology is pre-built at scan time and
stored in cable_topology. The viewer does a direct SQL lookup.

Usage:
  python -m cable_engine.cli scan --input <dir> [--db <cable.db>]
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
import time
from pathlib import Path
from typing import Any, Optional

from cable_engine.graph import TopologyStage
from cable_engine.ir import DocumentType
from cable_engine.loaders import get_loader_for
from cable_engine.pipeline import Context, Pipeline
from cable_engine.storage import CableStore, open_db, ensure_schema


DEFAULT_DB_FILENAME = 'cable.db'


def _discover_documents(input_dir: Path):
    """Yield (file_path, loader) for every supported DWG/DXF document
    under `input_dir`. PDFs are NOT supported in V5 P0."""
    skip_suffixes = {'.db', '.db-shm', '.db-wal', '.json', '.csv',
                     '.un~', '.swp', '.log', '.err',
                     '.pdf'}  # PDF support deferred
    # Skip names that look like sqlite journal files
    skip_names = {'cable.db-shm', 'cable.db-wal'}
    for p in sorted(input_dir.rglob('*')):
        if not p.is_file():
            continue
        if p.name.startswith('.'):
            continue
        if p.name in skip_names:
            continue
        if p.suffix.lower() in skip_suffixes:
            continue
        loader = get_loader_for(p)
        if loader is None:
            continue
        yield p, loader


def _pipeline_for(store: CableStore) -> Pipeline:
    """V6 pipeline: Loader (already done) -> TopologyStage.
    TopologyStage dispatches to the appropriate analyzer per doc type.
    """
    return Pipeline([
        TopologyStage(store),
    ])


# ---------------------------------------------------------------------------
# Per-document worker
# ---------------------------------------------------------------------------
def _process_one_document(
    document_path_str: str,
    db_path_str: str,
) -> dict:
    """Process one document in a worker process."""
    document_path = Path(document_path_str)
    loader = get_loader_for(document_path)
    if loader is None:
        return {
            'path': str(document_path),
            'error': f'no loader for {document_path.suffix}',
        }
    doc = loader.load(document_path)
    if doc is None or not doc.entities:
        return {
            'path': str(document_path),
            'content_hash': '',
            'source_type': 'unknown',
            'pages': 0,
            'entities': 0,
            'error': 'no entities loaded',
        }

    store = CableStore(open_db(Path(db_path_str), read_only=False))
    try:
        store.upsert_document(
            doc.content_hash, str(document_path),
            file_size=document_path.stat().st_size if document_path.exists() else None,
            file_mtime=document_path.stat().st_mtime if document_path.exists() else None,
            document_type=doc.document_type.value,
        )
        ctx = Context(
            document_path=document_path,
            content_hash=doc.content_hash,
            document=doc,
        )
        out = _pipeline_for(store).run(ctx)
        return {
            'path': str(document_path),
            'content_hash': doc.content_hash,
            'source_type': doc.document_type.value,
            'pages': len(doc.pages),
            'entities': len(doc.entities),
            'error': out.error_msg,
        }
    finally:
        store.close()


def _process_one_document_wrapper(item):
    return _process_one_document(*item)


# ---------------------------------------------------------------------------
# Main: scan subcommand
# ---------------------------------------------------------------------------
def cmd_scan(args: argparse.Namespace) -> int:
    input_dir = Path(args.input).expanduser()
    if not input_dir.is_dir():
        print(f'ERROR: input directory not found: {input_dir}', file=sys.stderr)
        return 1

    db_path = Path(args.db).expanduser() if args.db else input_dir / DEFAULT_DB_FILENAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_schema(open_db(db_path))

    print(f'Input:  {input_dir}', flush=True)
    print(f'DB:     {db_path}', flush=True)

    store = CableStore(open_db(db_path, read_only=False))
    store.set_state('started_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
    store.set_state('input', str(input_dir))
    store.close()

    documents = list(_discover_documents(input_dir))
    if not documents:
        print(f'No supported DWG/DXF documents found under {input_dir}', flush=True)
        return 0
    print(f'Discovered: {len(documents)} supported documents', flush=True)

    completed = 0
    start = time.time()
    print(f'Processing with {args.workers} workers...', flush=True)

    if args.workers <= 1:
        # Single-process — easier to debug
        for p, _ in documents:
            res = _process_one_document(str(p), str(db_path))
            completed += 1
            elapsed = time.time() - start
            if res.get('error'):
                print(f'[{completed}/{len(documents)}] {p.name} ({elapsed:.1f}s)  '
                      f'错误: {res["error"]}', flush=True)
            else:
                print(f'[{completed}/{len(documents)}] {p.name} ({elapsed:.1f}s)  '
                      f'[{res["source_type"]}] {res["pages"]}p/{res["entities"]}e',
                      flush=True)
    else:
        with mp.Pool(processes=args.workers) as pool:
            for result in pool.imap_unordered(
                _process_one_document_wrapper,
                [(str(p), str(db_path)) for p, _ in documents],
            ):
                completed += 1
                elapsed = time.time() - start
                if result.get('error'):
                    print(f'[{completed}/{len(documents)}] '
                          f'{Path(result["path"]).name} ({elapsed:.1f}s)  '
                          f'错误: {result["error"]}', flush=True)
                else:
                    print(f'[{completed}/{len(documents)}] '
                          f'{Path(result["path"]).name} ({elapsed:.1f}s)  '
                          f'[{result["source_type"]}] {result["pages"]}p/{result["entities"]}e',
                          flush=True)

    # Summary
    store = CableStore(open_db(db_path, read_only=True))
    s = store.stats()
    store.close()
    duration = time.time() - start
    print()
    print('=== 完成 ===')
    print(f'扫描: {s["documents"]} documents')
    print(f'  topology: {s["cable_topology"]} cable-conductor records')
    print(f'  strips: {s["terminal_strips"]} terminal strips')
    print(f'  (graph tables removed — topology is the primary store)')
    print(f'耗时: {duration:.1f}s')
    print(f'DB: {db_path}')
    print()
    print('Start the viewer with:')
    print(f'  python -m tools.cable_match_viewer.server --db {db_path}')
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------
def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='cable_engine v5: graph-based document intelligence',
    )
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('scan', help='Scan a directory tree and build graph')
    s.add_argument('--input', required=True, help='Input directory (DWG/DXF)')
    s.add_argument('--db', help='Output cable.db path (default: <input>/cable.db)')
    s.add_argument('--workers', type=int, default=1, help='Parallel workers (default: 1)')
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == 'scan':
        return cmd_scan(args)
    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())