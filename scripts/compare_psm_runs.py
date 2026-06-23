#!/usr/bin/env python3
"""compare_psm_runs.py — diff two cable_match _matches.csv files.

Usage:
    python scripts/compare_psm_runs.py <csv_a> <csv_b> [--label-a baseline] [--label-b psm6]

Reports:
  - per-cable hit counts in A, B, and the symmetric difference
  - per-PDF hit counts in A, B, and the symmetric difference
  - which (cable, content_hash) pairs are unique to A and B
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path


FIELDNAMES = ['电缆编号', 'PDF文件名', '源相对路径', '匹配时间', '内容hash前16']


def load(path):
    """Return (set of (cable, hash16) keys, set of unique cables, set of unique hashes, raw rows)."""
    if not Path(path).exists():
        return set(), set(), set(), []
    keys = set()
    cables = set()
    hashes = set()
    rows = []
    with open(path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            cable = (row.get('电缆编号') or '').strip()
            h = (row.get('内容hash前16') or '').strip()
            if cable and h:
                keys.add((cable, h))
                cables.add(cable)
                hashes.add(h)
                rows.append((cable, h, row))
    return keys, cables, hashes, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('csv_a', help='Path to first _matches.csv (e.g. baseline)')
    ap.add_argument('csv_b', help='Path to second _matches.csv (e.g. psm=6)')
    ap.add_argument('--label-a', default='A', help='Label for A in output')
    ap.add_argument('--label-b', default='B', help='Label for B in output')
    args = ap.parse_args()

    keys_a, cables_a, hashes_a, rows_a = load(args.csv_a)
    keys_b, cables_b, hashes_b, rows_b = load(args.csv_b)

    print(f'=== {args.label_a} ({args.csv_a}) ===')
    print(f'  (cable, hash) pairs: {len(keys_a)}')
    print(f'  unique cables:       {len(cables_a)}')
    print(f'  unique PDFs:         {len(hashes_a)}')

    print(f'\n=== {args.label_b} ({args.csv_b}) ===')
    print(f'  (cable, hash) pairs: {len(keys_b)}')
    print(f'  unique cables:       {len(cables_b)}')
    print(f'  unique PDFs:         {len(hashes_b)}')

    only_a = keys_a - keys_b
    only_b = keys_b - keys_a
    shared = keys_a & keys_b

    print(f'\n=== Diff (by (cable, content_hash)) ===')
    print(f'  shared:           {len(shared)}')
    print(f'  only in {args.label_a}: {len(only_a)}')
    print(f'  only in {args.label_b}: {len(only_b)}')

    # Cable-level diff: which cables appear in A but not B (and vice versa)
    cables_only_a = cables_a - cables_b
    cables_only_b = cables_b - cables_a
    cables_shared = cables_a & cables_b
    print(f'\n=== Diff (by cable ID) ===')
    print(f'  cables in both:                {len(cables_shared)}')
    print(f'  cables only in {args.label_a}: {len(cables_only_a)}')
    print(f'  cables only in {args.label_b}: {len(cables_only_b)}')

    if cables_only_a:
        print(f'\n  --- cables only in {args.label_a} (i.e. {args.label_b} LOST these) ---')
        for c in sorted(cables_only_a):
            print(f'    {c}')

    if cables_only_b:
        print(f'\n  --- cables only in {args.label_b} (i.e. {args.label_b} FOUND these extra) ---')
        for c in sorted(cables_only_b):
            print(f'    {c}')

    # PDF-level diff
    pdfs_only_a = hashes_a - hashes_b
    pdfs_only_b = hashes_b - hashes_a
    print(f'\n=== Diff (by PDF content_hash) ===')
    print(f'  PDFs only in {args.label_a}: {len(pdfs_only_a)}')
    print(f'  PDFs only in {args.label_b}: {len(pdfs_only_b)}')

    # Hits per cable (for both runs)
    print(f'\n=== Cables with more than 1 hit ===')
    hits_a = defaultdict(int)
    hits_b = defaultdict(int)
    for c, _ in keys_a:
        hits_a[c] += 1
    for c, _ in keys_b:
        hits_b[c] += 1
    multi_a = {c: n for c, n in hits_a.items() if n > 1}
    multi_b = {c: n for c, n in hits_b.items() if n > 1}
    print(f'  multi-hit cables in {args.label_a}: {len(multi_a)}')
    print(f'  multi-hit cables in {args.label_b}: {len(multi_b)}')


if __name__ == '__main__':
    main()
