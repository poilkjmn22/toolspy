"""cable_engine.cli — main entry point for the multi-source pipeline.

This replaces the old scripts/cable_match.py's argparse+main() with a
cleaner entry that:
  1. Discovers documents by file extension (PDF / DWG / DXF)
  2. Dispatches to the right Loader (PDFLoader, DWGLoader)
  3. Builds a Pipeline per source type (DWG skips OCR stages)
  4. Persists everything to one cable.db

Usage:
  cable_engine.cli scan <input_dir> --csv <cables.csv> --output <dir>

Note: this module is the canonical entry. The old scripts/cable_match.py
remains as a thin wrapper for backwards compatibility with run_union.sh
during the transition; new code should use cable_engine.cli directly.
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cable_engine.ir import Document, DocumentType
from cable_engine.loaders import get_loader_for
from cable_engine.match import find_matches
from cable_engine.pipeline import Context, Pipeline
from cable_engine.stages import (
    CopyStage, MatchStage, OCRStage, PersistStage, RasterizeStage,
)
from cable_engine.storage import CableStore


DEFAULT_DB_FILENAME = 'cable.db'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_targets(csv_path: Path) -> list[str]:
    """Load cable IDs from CSV column '电缆编号' (Chinese: cable number)."""
    targets: list[str] = []
    seen: set[str] = set()
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            t = (row.get('电缆编号') or '').strip()
            if t and t not in seen:
                seen.add(t)
                targets.append(t)
    return targets


def _discover_documents(input_dir: Path):
    """Yield (file_path, loader) for every supported document under
    `input_dir`. The loader is instantiated by get_loader_for(path).
    Skips files that begin with '.' (hidden) and common non-document
    files (.db, .json, .csv, .un~, .swp)."""
    skip_suffixes = {'.db', '.json', '.csv', '.un~', '.swp', '.log', '.err'}
    for p in sorted(input_dir.rglob('*')):
        if not p.is_file():
            continue
        if p.name.startswith('.'):
            continue
        if p.suffix.lower() in skip_suffixes:
            continue
        loader = get_loader_for(p)
        yield p, loader


# ---------------------------------------------------------------------------
# Pipeline construction per source type
# ---------------------------------------------------------------------------
def _pipeline_for(doc_type: DocumentType, ctx: Context, targets: list[str],
                  store: CableStore, input_root: Path,
                  output_root: Optional[Path] = None) -> Pipeline:
    """Build the per-document Pipeline.

    PDF flow:  Renderize -> OCR -> Match -> Persist -> (optional Copy)
    DWG flow:  Match -> Persist                       (DWG already has text)

    The `output_root` controls whether CopyStage runs (PDF only). If None,
    no copies are made; the matches land in cable.db only.
    """
    stages = []
    if doc_type == DocumentType.PDF:
        stages.append(RasterizeStage(
            dpi=ctx.dpi, lang=ctx.lang,
        ))
        stages.append(OCRStage(
            engine_name=ctx.engine_name,
            use_gpu=ctx.use_gpu,
        ))
    # DWG: skip Rasterize/OCR — loader already populated doc.entities.

    stages.append(MatchStage(
        targets=targets,
        use_levenshtein=ctx.use_levenshtein,
    ))
    stages.append(PersistStage(
        store=store, input_root=input_root,
        no_state=ctx.no_state,
    ))

    if output_root is not None and doc_type == DocumentType.PDF:
        stages.append(CopyStage(
            output_root=output_root,
            input_root=input_root,
        ))

    return Pipeline(stages=stages)


# ---------------------------------------------------------------------------
# Per-document worker (for multiprocessing.Pool)
# ---------------------------------------------------------------------------
def _worker_init_worker_state():
    """No-op placeholder. Per-doc work doesn't need shared state
    (each worker opens its own store and closes at the end)."""
    pass


def _process_one_document(
    document_path_str: str,
    targets: list[str],
    input_root_str: str,
    output_root_str: Optional[str],
    db_path_str: str,
    dpi: int, lang: str, rotation: int, preprocess: str,
    psm: Optional[int], oem: Optional[int], engine_name: str,
    use_gpu: bool, use_levenshtein: bool, no_state: bool,
) -> dict:
    """Process one document in a worker process. Returns a dict
    suitable for logging and for the main process's stats."""
    document_path = Path(document_path_str)
    input_root = Path(input_root_str)
    output_root = Path(output_root_str) if output_root_str else None

    loader = get_loader_for(document_path)
    doc = loader.load(document_path)

    # Build a fresh store handle per worker (sqlite3 connections can't
    # be shared across processes; --check_same_thread=False wouldn't be
    # enough for fork()-spawned workers).
    store = CableStore.open(Path(db_path_str), read_only=False)

    ctx = Context(
        document_path=document_path,
        content_hash=doc.content_hash,
        document=doc,
        engine_name=engine_name,
        use_levenshtein=use_levenshtein,
        dpi=dpi, lang=lang, rotation=rotation, preprocess=preprocess,
        psm=psm, oem=oem,
        use_gpu=use_gpu, no_state=no_state,
        no_text=False,
    )

    pipeline = _pipeline_for(
        doc.document_type, ctx, targets, store, input_root, output_root,
    )
    out = pipeline.run(ctx)
    store.close()

    return {
        'path': str(document_path),
        'content_hash': doc.content_hash,
        'source_type': doc.document_type.value,
        'pages': len(doc.pages),
        'entities': len(doc.entities),
        'matches': dict(out.matches),
        'error': out.error_msg,
    }


# ---------------------------------------------------------------------------
# Main: scan subcommand
# ---------------------------------------------------------------------------
def _process_one_document_wrapper(item):
    """Module-level wrapper for multiprocessing (pickle-safe)."""
    return _process_one_document(*item)


def cmd_scan(args: argparse.Namespace) -> int:
    """Run the multi-source pipeline over `args.input_dir`."""
    input_dir = Path(args.input).expanduser()
    if not input_dir.is_dir():
        print(f'ERROR: input directory not found: {input_dir}', file=sys.stderr)
        return 1

    output_root = (
        Path(args.output).expanduser() if args.output else input_dir
    )
    output_root.mkdir(parents=True, exist_ok=True)

    db_path = (
        output_root / (args.db_name or DEFAULT_DB_FILENAME)
        if not args.no_db
        else None
    )
    if db_path is None:
        # Use a temp file path so worker function can still pass it;
        # we'll just not commit anything.
        import tempfile
        db_path = Path(tempfile.mkdtemp()) / 'cable.db'

    targets = _load_targets(Path(args.csv).expanduser())
    if not targets:
        print(f'ERROR: no targets loaded from {args.csv}', file=sys.stderr)
        return 1
    print(f'Loaded {len(targets)} unique targets from CSV', flush=True)
    print(f'Input:  {input_dir}', flush=True)
    print(f'Output: {output_root}', flush=True)
    print(f'DB:     {db_path}', flush=True)

    # Main-process store: used for scan_state and global progress
    store = CableStore.open(db_path, read_only=False)
    store.set_state('started_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
    store.set_state('input', str(input_dir))
    store.set_state('output', str(output_root))
    store.set_state('csv', str(args.csv))
    store.set_state('dpi', args.dpi)
    store.set_state('lang', args.lang)
    store.set_state('engine', args.engine)
    store.set_state('use_gpu', args.use_gpu)
    store.set_state('preprocess', args.preprocess)
    store.set_state('use_levenshtein', args.levenshtein)

    # Discover
    documents = list(_discover_documents(input_dir))
    if not documents:
        print(f'No supported documents found under {input_dir}', flush=True)
        store.close()
        return 0
    print(f'Discovered: {len(documents)} supported documents', flush=True)

    if args.resume:
        # Skip already-processed docs (cable.db stores their rel paths)
        done = set(store.get_state('processed', []) or [])
    else:
        done = set()

    todo = [(p, loader) for p, loader in documents
            if str(p.relative_to(input_dir)) not in done]
    if not todo:
        print(f'All {len(documents)} docs already processed (use --no-resume to force re-run).', flush=True)
        store.close()
        return 0
    print(f'To process: {len(todo)} (skipped {len(done)} already done)', flush=True)

    # Run via multiprocessing.Pool
    initargs = (
        targets, str(input_dir), str(output_root) if output_root else None,
        str(db_path), args.dpi, args.lang, args.rotation, args.preprocess,
        args.psm, args.oem, args.engine, args.use_gpu, args.levenshtein,
        args.no_state,
    )
    completed = 0
    start = time.time()
    print(f'Processing {len(todo)} documents with {args.workers} workers...', flush=True)

    with mp.Pool(processes=args.workers, initializer=_worker_init_worker_state) as pool:
        try:
            for result in pool.imap_unordered(
                _process_one_document_wrapper,
                [(str(p), *initargs) for p, _ in todo],
            ):
                completed += 1
                rel = str(Path(result['path']).relative_to(input_dir))
                elapsed = time.time() - start
                if result.get('error'):
                    print(f'[{completed}/{len(todo)}] {rel} ({elapsed:.0f}s)  '
                          f'错误: {result["error"]}', flush=True)
                    store.append_state_list('failed', rel)
                else:
                    matches = result.get('matches', {})
                    n_matches = len(matches)
                    pages = result.get('pages', 0)
                    n_ents = result.get('entities', 0)
                    src = result.get('source_type', '?')
                    if n_matches:
                        print(f'[{completed}/{len(todo)}] {rel} ({elapsed:.0f}s)  '
                              f'[{src}] {pages}p/{n_ents}e 匹配 '
                              f'{", ".join(matches)}', flush=True)
                    else:
                        print(f'[{completed}/{len(todo)}] {rel} ({elapsed:.0f}s)  '
                              f'[{src}] {pages}p/{n_ents}e 不匹配', flush=True)
                    store.append_state_list('processed', rel)
                    # Track match_type_counts
                    counts = store.get_state('match_type_counts', {}) or {}
                    for tier in matches.values():
                        counts[tier] = counts.get(tier, 0) + 1
                    store.set_state('match_type_counts', counts)
                    store.set_state('last_updated', time.strftime('%Y-%m-%dT%H:%M:%S'))
        except KeyboardInterrupt:
            print('\n\n[KeyboardInterrupt] saving state and exiting...', flush=True)

    # Summary
    store.set_state('last_updated', time.strftime('%Y-%m-%dT%H:%M:%S'))
    duration = time.time() - start
    s = store.stats()
    print()
    print('=== 完成 ===')
    print(f'扫描: {s["documents"]} documents, '
          f'{s["ocr_pages"]} OCR pages, {s["matches"]} matches '
          f'across {s["cables_matched"]} cables')
    print(f'耗时: {duration:.0f}s ({duration/60:.1f} min)')
    print(f'DB: {db_path}')

    # Remove empty cable dirs
    if output_root and not args.list_mode:
        for t in targets:
            d = output_root / t
            if d.exists() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass

    store.close()
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------
def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='cable_engine: multi-source document intelligence scanner',
    )
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('scan', help='Scan a directory tree for cable IDs')
    s.add_argument('--input', required=True, help='Input directory to scan recursively')
    s.add_argument('--csv', required=True, help='CSV file with 电缆编号 column')
    s.add_argument('--output', help='Output root for matched PDFs (default: same as --input)')
    s.add_argument('--db-name', help='Cable DB filename (default: cable.db)')
    s.add_argument('--no-db', action='store_true', help='Skip writing to cable.db (debug only)')
    s.add_argument('--workers', type=int, default=4, help='Parallel workers (default: 4)')
    s.add_argument('--resume', action='store_true', help='Skip already-processed documents')
    s.add_argument('--no-state', action='store_true', help='Skip writing scan_state (no resume)')
    s.add_argument('--list', dest='list_mode', action='store_true', help='Dry-run; do not copy PDFs')
    s.add_argument('--dpi', type=int, default=300)
    s.add_argument('--lang', default='chi_sim+eng')
    s.add_argument('--rotation', type=int, default=0, choices=[0, 90, 180, 270])
    s.add_argument('--preprocess', default='none', choices=['none', 'gauss_otsu', 'both'])
    s.add_argument('--engine', default='tesseract', choices=['tesseract', 'paddleocr'])
    s.add_argument('--use-gpu', action='store_true')
    s.add_argument('--psm', type=int, default=None)
    s.add_argument('--oem', type=int, default=None)
    s.add_argument('--levenshtein', action='store_true')
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
