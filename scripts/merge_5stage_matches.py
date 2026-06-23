#!/usr/bin/env python3
"""merge_5stage_matches.py — merge _matches.csv (and PDFs) from all 6 union stages.

Stages (each writes to its own output dir under WUHAN_DIR):
    .stage_chieng_tess/_matches.csv           (Tesseract chi_sim+eng, none)
    .stage_chieng_tess_gauss/_matches.csv     (Tesseract chi_sim+eng, gauss_otsu)
    .stage_chisim_tess/_matches.csv           (Tesseract chi_sim, none)
    .stage_chisim_tess_gauss/_matches.csv     (Tesseract chi_sim, gauss_otsu)
    .stage_chieng_paddle/_matches.csv         (PaddleOCR ch model, none)
    .stage_chisim_paddle/_matches.csv         (PaddleOCR en model, none)

Union by (cable, content_hash[:16]) key. When the same (cable, hash) appears
in multiple stages, the match_type is upgraded to the best tier seen:
    exact > normalized > confusion > levenshtein

PDF merge (default ON, use --no-pdf-merge to skip):
    For each (cable, content_hash) pair in the union, the actual PDF is
    copied from the first stage dir that has it into
    <WUHAN_DIR>/_matched_pdfs/<cable>/<pdf_stem>__<hash8>.pdf.

    Dedup rules:
      - Same content_hash from multiple stages → ONE PDF (skipped after first).
      - Same cable, different content_hash → multiple PDFs in the cable
        folder (each gets its own hash8 suffix).
      - Output filenames use ``<pdf_stem>__<hash8>.pdf`` to avoid
        collisions when different cables share similar original filenames.

The final _matches.csv is written to WUHAN_DIR/ with header:
    电缆编号, PDF文件名, 源相对路径, 匹配时间, 内容hash前16, 匹配方式

Usage:
    python scripts/merge_5stage_matches.py <WUHAN_DIR> [--no-pdf-merge] [--pdf-output <subdir>]

Examples:
    # Default: merge CSV + copy PDFs to <WUHAN_DIR>/_matched_pdfs/
    python scripts/merge_5stage_matches.py /Users/.../wuhan/pdf

    # Only merge CSV (no PDF file copies)
    python scripts/merge_5stage_matches.py /Users/.../wuhan/pdf --no-pdf-merge

    # Custom output dir name
    python scripts/merge_5stage_matches.py /Users/.../wuhan/pdf --pdf-output pdfs
"""
import csv
import hashlib
import shutil
import sys
from pathlib import Path

FIELDNAMES = ['电缆编号', 'PDF文件名', '源相对路径', '匹配时间', '内容hash前16', '匹配方式']
STAGE_DIRS = [
    '.stage_chieng_tess',
    '.stage_chieng_tess_gauss',
    '.stage_chisim_tess',
    '.stage_chisim_tess_gauss',
    '.stage_chieng_paddle',
    '.stage_chisim_paddle',
]
STAGE_LABELS = [
    'chieng+tess',       # chi_sim+eng, no preprocess
    'chieng+tess+gauss',  # chi_sim+eng, gauss_otsu
    'chisim+tess',       # chi_sim, no preprocess
    'chisim+tess+gauss',  # chi_sim, gauss_otsu
    'chieng+paddle',     # PaddleOCR ch model
    'chisim+paddle',     # PaddleOCR en model
]

# Tier ranking: lower is better. Used to keep the best match_type across stages.
TIER_RANK = {'exact': 0, 'normalized': 1, 'confusion': 2, 'levenshtein': 3, '': 4}

DEFAULT_PDF_OUTPUT_SUBDIR = '_matched_pdfs'


def load_csv(path):
    if not path.exists():
        return []
    out = []
    with open(path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            cable = (row.get('电缆编号') or '').strip()
            h = (row.get('内容hash前16') or '').strip()
            if cable and h:
                out.append((cable, h, row))
    return out


def merge_pdfs(stage_dirs, wuhan_dir, output_subdir=DEFAULT_PDF_OUTPUT_SUBDIR,
               verbose=True):
    """Copy each unique (cable, content_hash) PDF from the stages into
    ``<wuhan_dir>/<output_subdir>/<cable>/<pdf_stem>__<hash8>.pdf``.

    Dedup logic:
      - Same (cable, content_hash) from multiple stages → only the first
        source is copied; the rest are skipped.
      - Same cable with different content_hash → multiple PDFs in the
        cable folder, each with its own hash8 suffix.
      - Files already present in the output (same name + same size) are
        left alone (idempotent re-run).

    Returns: dict with stats: {copied, skipped, missing, errors}
    """
    output_root = wuhan_dir / output_subdir
    seen = set()  # (cable, content_hash) → already copied
    stats = {'copied': 0, 'skipped': 0, 'missing': 0, 'errors': 0}

    for sub in stage_dirs:
        stage_dir = wuhan_dir / sub
        if not stage_dir.exists():
            continue
        # Walk each cable folder inside the stage dir
        for cable_dir in sorted(stage_dir.iterdir()):
            if not cable_dir.is_dir() or cable_dir.name.startswith('.'):
                continue
            cable = cable_dir.name
            for pdf in sorted(cable_dir.iterdir()):
                if not pdf.is_file() or pdf.suffix.lower() != '.pdf':
                    continue
                # Compute the real content_hash from the file (not from
                # the CSV column, which is the first 16 chars of sha256).
                try:
                    h = hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]
                except OSError:
                    stats['errors'] += 1
                    continue
                key = (cable, h)
                if key in seen:
                    stats['skipped'] += 1
                    continue
                seen.add(key)

                # Build deterministic destination name:
                #   <pdf_stem>__<hash8>.pdf
                # This avoids collisions when different cables share similar
                # original filenames.
                target_dir = output_root / cable
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{pdf.stem}__{h[:8]}{pdf.suffix}"
                try:
                    if target.exists() and target.stat().st_size == pdf.stat().st_size:
                        stats['skipped'] += 1
                        continue
                    shutil.copy2(str(pdf), str(target))
                    stats['copied'] += 1
                except OSError as e:
                    print(f'    ERROR copying {pdf} -> {target}: {e}', file=sys.stderr)
                    stats['errors'] += 1
                    seen.discard(key)  # allow retry on next stage

    if verbose:
        print(f'PDF merge output: {output_root}')
        print(f'  copied:   {stats["copied"]}')
        print(f'  skipped:  {stats["skipped"]}  (already present or dup)')
        if stats['missing']:
            print(f'  missing:  {stats["missing"]}  (PDF not found in any stage)')
        if stats['errors']:
            print(f'  errors:   {stats["errors"]}')
    return stats


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0 if args else 1)

    wuhan_dir = Path(args[0]).expanduser()
    if not wuhan_dir.exists():
        print(f'ERROR: WUHAN_DIR does not exist: {wuhan_dir}', file=sys.stderr)
        sys.exit(1)

    # Parse simple flags
    do_pdf_merge = True
    pdf_output_subdir = DEFAULT_PDF_OUTPUT_SUBDIR
    i = 1
    while i < len(args):
        if args[i] == '--no-pdf-merge':
            do_pdf_merge = False
            i += 1
        elif args[i] == '--pdf-output' and i + 1 < len(args):
            pdf_output_subdir = args[i + 1]
            i += 2
        else:
            print(f'WARN: ignoring unknown arg: {args[i]}', file=sys.stderr)
            i += 1

    seen = {}  # (cable, hash) -> (stage_label, row)
    stats = {label: 0 for label in STAGE_LABELS}
    for sub, label in zip(STAGE_DIRS, STAGE_LABELS):
        csv_path = wuhan_dir / sub / '_matches.csv'
        rows = load_csv(csv_path)
        stats[label] = len(rows)
        for cable, h, row in rows:
            key = (cable, h)
            if key not in seen:
                seen[key] = (label, row)
            else:
                # Same (cable, hash) found in multiple stages — keep the better
                # match_type (exact > normalized > confusion > levenshtein).
                prev_label, prev_row = seen[key]
                prev_mt = (prev_row.get('匹配方式') or '').strip()
                new_mt = (row.get('匹配方式') or '').strip()
                if TIER_RANK.get(new_mt, 99) < TIER_RANK.get(prev_mt, 99):
                    seen[key] = (label, row)

    print(f'stage row counts:')
    for label, n in stats.items():
        print(f'  {label:<20} {n} rows')
    print(f'unique (cable, content_hash) pairs: {len(seen)}')

    rows_merged = [row for _, row in seen.values()]
    unique_cables = set((r.get('电缆编号') or '').strip() for r in rows_merged)
    unique_hashes = set((r.get('内容hash前16') or '').strip() for r in rows_merged)
    print(f'unique cable IDs: {len(unique_cables)} / 364 targets')
    print(f'unique PDFs:      {len(unique_hashes)}')

    by_source = {}
    for src_label, _ in seen.values():
        by_source[src_label] = by_source.get(src_label, 0) + 1
    print(f'unique rows by winning stage: {by_source}')

    by_match_type = {}
    for r in rows_merged:
        mt = (r.get('匹配方式') or '').strip() or '(empty)'
        by_match_type[mt] = by_match_type.get(mt, 0) + 1
    print(f'rows by 匹配方式: {by_match_type}')

    # Write merged CSV
    out_csv = wuhan_dir / '_matches.csv'
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for _, row in seen.values():
            w.writerow({k: row.get(k, '') for k in FIELDNAMES})
    print(f'merged CSV written to: {out_csv} ({len(seen)} rows)')

    # PDF merge
    if do_pdf_merge:
        print()
        print('=== PDF merge ===')
        merge_pdfs(STAGE_DIRS, wuhan_dir, output_subdir=pdf_output_subdir)
    else:
        print()
        print('PDF merge skipped (--no-pdf-merge)')


if __name__ == '__main__':
    main()
