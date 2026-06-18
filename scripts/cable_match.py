#!/usr/bin/env python3
"""One-off multi-target PDF organizer.

Reads cable numbers from a CSV's "电缆编号" column, OCRs each PDF in the input
folder once, checks all targets against the OCR text, copies matches to per-target
output folders.

Usage:
  python scripts/cable_match.py --csv <path> --input <folder> [--output <root>] [--list]
"""
import argparse
import csv
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Make the toolspy project importable regardless of where this script is run from
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from tools.text_extractor import extract_text


def normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()


def load_targets(csv_path: Path) -> list:
    targets = []
    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if '电缆编号' not in (reader.fieldnames or []):
            print(f"错误: CSV 缺少 '电缆编号' 列。当前列: {reader.fieldnames}",
                  file=sys.stderr)
            sys.exit(1)
        for row in reader:
            t = (row.get('电缆编号') or '').strip()
            if t and t not in targets:
                targets.append(t)
    return targets


def discover_pdfs(input_path: Path, target_set: set) -> list:
    pdfs = []
    for p in sorted(input_path.rglob('*.pdf')):
        if not p.is_file():
            continue
        rel = p.relative_to(input_path)
        # Skip files inside a target-named subdir (previous run's output)
        if len(rel.parts) > 1 and rel.parts[0] in target_set:
            continue
        pdfs.append(p)
    return pdfs


def _process_one(pdf, input_path, output_root, targets, args, matches_lock):
    """OCR one PDF and copy matches. Returns (rel, hit_list, status_str, dest_paths)."""
    rel = pdf.relative_to(input_path)
    try:
        text = extract_text(pdf, ocr=True, lang=args.lang, dpi=args.dpi, warn=False)
    except Exception as e:
        return (rel, [], f"错误: {e}", [])

    norm = normalize(text)
    if not norm:
        return (rel, [], "无文本", [])

    hit = [t for t in targets if normalize(t) in norm]
    dests = []
    if hit and not args.list:
        for t in hit:
            dest_dir = output_root / t
            with matches_lock:
                dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / pdf.name
            n = 1
            while dest.exists():
                dest = dest_dir / f"{pdf.stem}_{n}{pdf.suffix}"
                n += 1
            shutil.copy2(str(pdf), str(dest))
            dests.append(dest)
    return (rel, hit, "匹配" if hit else "不匹配", dests)


def main():
    parser = argparse.ArgumentParser(
        description='OCR PDFs once, match against many target strings, copy to per-target folders.',
    )
    parser.add_argument('--csv', required=True, help='CSV file with 电缆编号 column')
    parser.add_argument('--input', required=True, help='Folder to scan recursively for PDFs')
    parser.add_argument('--output', help='Output root (default: same as --input)')
    parser.add_argument('--dpi', type=int, default=300)
    parser.add_argument('--lang', default='chi_sim+eng')
    parser.add_argument('--workers', type=int, default=4, help='Parallel OCR workers (default: 4)')
    parser.add_argument('--list', action='store_true', help='Dry-run: show matches, no copying')
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.is_dir():
        print(f"错误: 输入文件夹不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_root = Path(args.output).expanduser() if args.output else input_path
    output_root.mkdir(parents=True, exist_ok=True)

    targets = load_targets(Path(args.csv).expanduser())
    target_set = set(targets)
    print(f"从 CSV 读取 {len(targets)} 个目标字符串")
    print(f"输入: {input_path}")
    print(f"输出: {output_root}")
    print(f"OCR:  Tesseract ({args.lang}, {args.dpi} dpi, {args.workers} workers)")
    print(f"模式: {'仅查看' if args.list else '复制匹配'}")
    print()

    pdfs = discover_pdfs(input_path, target_set)
    print(f"扫描 {len(pdfs)} 个 PDF")
    if not pdfs:
        return

    matches = {t: [] for t in targets}
    failures = []
    no_text = []
    matches_lock = threading.Lock()
    print_lock = threading.Lock()
    start = time.time()
    completed = 0

    def _on_done(future):
        nonlocal completed
        rel, hit, status, dests = future.result()
        completed += 1
        elapsed = time.time() - start
        with print_lock:
            extra = f": {', '.join(hit)}" if hit and status == "匹配" else ""
            print(f"[{completed}/{len(pdfs)}] {rel}  ({elapsed:.0f}s)  {status}{extra}", flush=True)
        with matches_lock:
            if status == "错误":
                failures.append(rel)
            elif status == "无文本":
                no_text.append(rel)
            elif hit:
                for t in hit:
                    matches[t].append(rel)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [
            ex.submit(_process_one, pdf, input_path, output_root, targets, args, matches_lock)
            for pdf in pdfs
        ]
        for f in as_completed(futures):
            _on_done(f)

    # Remove empty target dirs (cleanup)
    if not args.list:
        for t in targets:
            d = output_root / t
            if d.exists() and not any(d.iterdir()):
                d.rmdir()

    total = sum(len(v) for v in matches.values())
    duration = time.time() - start
    print()
    print("=== 完成 ===")
    print(f"扫描 PDF:  {len(pdfs)}")
    print(f"总匹配:   {total}")
    print(f"无文本:   {len(no_text)}")
    print(f"失败:     {len(failures)}")
    print(f"耗时:     {duration:.0f}s ({duration/60:.1f} min)")
    print()
    print("各目标命中数:")
    for t in targets:
        print(f"  {t:20s}  {len(matches[t])}")


if __name__ == '__main__':
    main()
