#!/usr/bin/env python3
"""merge_wuhan_matches.py — merge chieng+both and chisim+both _matches.csv files.

Both runs write to different output dirs, so their _matches.csv files are
independent. This script merges them by (cable, content_hash[:16]) key, with
the chieng result taking precedence on duplicates (both runs likely found the
same PDFs for the same cable IDs).

Usage:
    python scripts/merge_wuhan_matches.py
"""
import csv
import sys
from pathlib import Path

CHIENG_CSV = Path('/Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf/_matches.csv')
CHISIM_CSV = Path('/Users/fangqi-apple/Documents/work/nengzhong/wuhan_chisim/_matches.csv')
OUTPUT_CSV = Path('/Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf/_matches.csv')


def load_csv(path):
    """Return list of (cable, content_hash_16, row_dict) tuples."""
    if not path.exists():
        print(f'  WARN: {path} does not exist, skipping', file=sys.stderr)
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
    chieng = load_csv(CHIENG_CSV)
    chisim = load_csv(CHISIM_CSV)
    print(f'chieng rows: {len(chieng)}')
    print(f'chisim rows: {len(chisim)}')

    # Union by (cable, content_hash[:16])
    seen = {}
    for src_label, rows in [('chieng', chieng), ('chisim', chisim)]:
        for cable, h, row in rows:
            key = (cable, h)
            if key not in seen:
                seen[key] = (src_label, row)

    print(f'unique (cable, content_hash) pairs: {len(seen)}')
    by_source = {}
    for src_label, _ in seen.values():
        by_source[src_label] = by_source.get(src_label, 0) + 1
    print(f'  by source: {by_source}')

    # Stats
    unique_cables = set(c for c, h, _ in seen.values())
    unique_hashes = set(h for c, h, _ in seen.values())
    print(f'unique cable IDs: {len(unique_cables)}')
    print(f'unique PDFs: {len(unique_hashes)}')

    # Write merged
    fieldnames = ['电缆编号', 'PDF文件名', '源相对路径', '匹配时间', '内容hash前16']
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for _, row in seen.values():
            w.writerow({k: row.get(k, '') for k in fieldnames})

    print(f'merged written to: {OUTPUT_CSV} ({len(seen)} rows)')


if __name__ == '__main__':
    main()
