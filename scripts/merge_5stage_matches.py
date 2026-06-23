#!/usr/bin/env python3
"""merge_5stage_matches.py — merge _matches.csv from all 6 union stages.

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

The final _matches.csv is written to WUHAN_DIR/ with header:
    电缆编号, PDF文件名, 源相对路径, 匹配时间, 内容hash前16, 匹配方式

Usage:
    python scripts/merge_5stage_matches.py <WUHAN_DIR>
    # e.g.
    python scripts/merge_5stage_matches.py /Users/.../wuhan/pdf
"""
import csv
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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    wuhan_dir = Path(sys.argv[1]).expanduser()
    if not wuhan_dir.exists():
        print(f'ERROR: WUHAN_DIR does not exist: {wuhan_dir}', file=sys.stderr)
        sys.exit(1)

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

    # Write merged
    out_csv = wuhan_dir / '_matches.csv'
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for _, row in seen.values():
            w.writerow({k: row.get(k, '') for k in FIELDNAMES})
    print(f'merged written to: {out_csv} ({len(seen)} rows)')


if __name__ == '__main__':
    main()
