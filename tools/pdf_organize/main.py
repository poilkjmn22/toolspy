#!/usr/bin/env python3
import argparse
import hashlib
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from tools.text_extractor import extract_text, check_tesseract


class PdfOrganizer:
    def __init__(
        self,
        input_folder: str,
        target: str,
        output: Optional[str] = None,
        move: bool = False,
        mirror: bool = False,
        no_ocr: bool = False,
        ignore_case: bool = False,
        no_normalize: bool = False,
        overwrite: bool = False,
        lang: str = 'chi_sim+eng',
        dpi: int = 300,
        rotation: Optional[int] = None,
        engine: str = 'tesseract',
        use_gpu: bool = False,
    ):
        self.input_folder = Path(input_folder).expanduser()
        self.target = target
        self.output = Path(output).expanduser() if output else (self.input_folder / target)
        self.move = move
        self.mirror = mirror
        self.no_ocr = no_ocr
        self.ignore_case = ignore_case
        self.no_normalize = no_normalize
        self.overwrite = overwrite
        self.lang = lang
        self.dpi = dpi
        self.rotation = rotation
        self.engine_name = engine
        self.use_gpu = use_gpu
        self._engine = None
        self.stats = {
            'total': 0,
            'matched': 0,
            'skipped': 0,
            'no_match': 0,
            'no_text': 0,
            'failed': 0,
            'start_time': datetime.now(),
        }
        self.failures: List[Tuple[Path, str]] = []

    def validate(self) -> Tuple[bool, str]:
        if not self.input_folder.exists():
            return False, f"输入文件夹不存在: {self.input_folder}"
        if not self.input_folder.is_dir():
            return False, f"输入路径不是文件夹: {self.input_folder}"
        if not self.target:
            return False, "目标字符串不能为空"
        if not (150 <= self.dpi <= 400):
            return False, f"--dpi 必须在 150-400 之间，当前: {self.dpi}"
        return True, "验证通过"

    def discover_pdfs(self) -> List[Path]:
        out_abs = self.output.resolve()
        pdfs = []
        for p in sorted(self.input_folder.rglob('*.pdf')):
            if not p.is_file():
                continue
            try:
                p_abs = p.resolve()
                if p_abs == out_abs or out_abs in p_abs.parents:
                    continue
            except (OSError, RuntimeError):
                pass
            pdfs.append(p)
        return pdfs

    @staticmethod
    def _normalize(s: str) -> str:
        return re.sub(r'\s+', ' ', s).strip()

    def _matches(self, text: str) -> bool:
        t = self._normalize(text) if not self.no_normalize else text
        q = self._normalize(self.target) if not self.no_normalize else self.target
        if self.ignore_case:
            t = t.lower()
            q = q.lower()
        return q in t

    def _output_dest(self, src: Path) -> Path:
        if self.mirror:
            rel = src.relative_to(self.input_folder)
            return self.output / rel
        return self.output / src.name

    def _unique_dest(self, dest: Path) -> Path:
        if not dest.exists() or self.overwrite:
            return dest
        parent = dest.parent
        stem = dest.stem
        suffix = dest.suffix
        n = 1
        while True:
            candidate = parent / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    def _get_engine(self):
        """Lazy-build the OCR engine on first use (PaddleOCR model load is ~10s)."""
        if self.no_ocr:
            return None
        if self._engine is None:
            try:
                from tools.ocr_engine import get_engine
                self._engine = get_engine(self.engine_name, lang=self.lang, use_gpu=self.use_gpu)
                self._engine.init()
            except Exception as e:
                print(f'Warning: failed to init engine {self.engine_name}: {e}', file=sys.stderr)
                from tools.ocr_engine import get_engine
                self._engine = get_engine('tesseract', lang=self.lang)
                self._engine.init()
        return self._engine

    def _output_has_same_content(self, src: Path) -> bool:
        """Check whether any file in the output dir has the same content as src.

        Used to make re-runs idempotent: if the source is already represented in
        the output (by content), don't copy it again.
        """
        if not self.output.exists():
            return False
        try:
            src_data = src.read_bytes()
        except (OSError, PermissionError):
            return False
        src_hash = hashlib.sha256(src_data).hexdigest()
        for existing in self.output.rglob('*'):
            if not existing.is_file() or existing.name.startswith('.'):
                continue
            try:
                if hashlib.sha256(existing.read_bytes()).hexdigest() == src_hash:
                    return True
            except (OSError, PermissionError):
                continue
        return False

    def scan(self) -> List[Tuple[Path, Path]]:
        pdfs = self.discover_pdfs()
        self.stats['total'] = len(pdfs)
        results = []
        for idx, pdf in enumerate(pdfs, 1):
            print(f"[{idx}/{len(pdfs)}] {pdf.name}  ...", end=' ', flush=True)
            try:
                text = extract_text(
                    pdf,
                    ocr=not self.no_ocr,
                    lang=self.lang,
                    dpi=self.dpi,
                    warn=False,
                    rotation=self.rotation,
                    engine=self._get_engine(),
                )
            except Exception as e:
                print(f"错误: {e}")
                self.failures.append((pdf, str(e)))
                self.stats['failed'] += 1
                continue

            if not text.strip():
                print("无文本")
                self.stats['no_text'] += 1
                continue

            if self._matches(text):
                intended_dest = self._output_dest(pdf)
                results.append((pdf, intended_dest))
                print(f"匹配 -> {intended_dest.name}")
                self.stats['matched'] += 1
            else:
                print("不匹配")
                self.stats['no_match'] += 1
        return results

    def organize(self) -> Tuple[bool, str]:
        valid, msg = self.validate()
        if not valid:
            return False, msg

        if not self.no_ocr:
            from tools.ocr_engine import check_engine, get_engine, EngineNotAvailable
            ok, tmsg = check_engine(self.engine_name)
            if not ok:
                return False, tmsg

        self.output.mkdir(parents=True, exist_ok=True)

        matches = self.scan()
        if not matches and self.stats['total'] == 0:
            return False, f"未找到 PDF 文件: {self.input_folder}"

        for src, intended_dest in matches:
            if self._output_has_same_content(src):
                print(f"  跳过: {src.name} (output 已包含此文件)")
                self.stats['skipped'] += 1
                self.stats['matched'] -= 1
                continue

            dest = self._unique_dest(intended_dest)
            if dest != intended_dest:
                print(f"  冲突: {intended_dest.name} -> {dest.name}")
            if dest.exists() and not self.overwrite:
                print(f"⚠ {dest.name} 已存在，跳过 (使用 --overwrite 覆盖)")
                self.stats['skipped'] += 1
                self.stats['matched'] -= 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.move:
                    shutil.move(str(src), str(dest))
                else:
                    shutil.copy2(str(src), str(dest))
            except Exception as e:
                print(f"  复制失败: {e}")
                self.failures.append((src, str(e)))
                self.stats['failed'] += 1
                self.stats['matched'] -= 1

        return self._report()

    def _report(self) -> Tuple[bool, str]:
        duration = (datetime.now() - self.stats['start_time']).total_seconds()
        print()
        print("=== 完成 ===")
        print(f"扫描总数:   {self.stats['total']}")
        print(f"匹配:       {self.stats['matched']}")
        print(f"  跳过:     {self.stats['skipped']}")
        print(f"不匹配:     {self.stats['no_match']}")
        print(f"无文本:     {self.stats['no_text']}")
        print(f"失败:       {self.stats['failed']}")
        print(f"耗时:       {duration:.2f}s")
        print(f"输出目录:   {self.output}")
        if self.failures:
            print("\n失败列表:")
            for f, err in self.failures:
                print(f"  {f}: {err}")

        if self.stats['total'] == 0:
            return False, "未找到 PDF 文件"
        if self.stats['matched'] > 0:
            return True, f"匹配 {self.stats['matched']} 个 PDF 到 {self.output}"
        if self.stats['skipped'] > 0:
            return True, f"所有 {self.stats['skipped']} 个匹配 PDF 均已在输出中（无新文件）"
        if self.stats['failed'] > 0:
            return False, f"所有匹配文件处理失败 ({self.stats['failed']} 个)"
        return False, "没有 PDF 包含目标字符串"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Find PDFs containing a target string; copy/move matches to a new folder.',
    )
    parser.add_argument('input', help='Input folder to scan recursively for PDFs')
    parser.add_argument('target', help='Target string to search for in each PDF')
    parser.add_argument('-o', '--output', help='Output folder (default: <input>/<target>)')
    parser.add_argument('--move', action='store_true', help='Move matching PDFs instead of copying (default: copy)')
    parser.add_argument('--mirror', action='store_true', help='Preserve subfolder structure under output (default: flat)')
    parser.add_argument('--no-ocr', action='store_true', help='Skip OCR; PDFs without a text layer will be skipped')
    parser.add_argument('--ignore-case', action='store_true', help='Case-insensitive match (default: case-sensitive)')
    parser.add_argument('--no-normalize', action='store_true', help='Disable whitespace normalization (default: normalize)')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite files with same name in output (default: skip)')
    parser.add_argument('--lang', default='chi_sim+eng', help='OCR language packs (Tesseract format; PaddleOCR maps chi_sim->ch)')
    parser.add_argument('--dpi', type=int, default=300, help='OCR render DPI, 150-400 (default: 300; use 300+ for small text in technical drawings)')
    parser.add_argument('--rotation', type=int, default=None, choices=[0, 90, 180, 270],
                        help='Force PDF page rotation in degrees (CW=positive). Default: auto-detect. Ignored by PaddleOCR.')
    parser.add_argument('--engine', default='tesseract', choices=['tesseract', 'paddleocr'],
                        help='OCR engine (default: tesseract; paddleocr needs `pip install -r requirements-paddleocr.txt`)')
    parser.add_argument('--use-gpu', action='store_true',
                        help='Enable GPU for PaddleOCR (Win/Linux + CUDA only).')
    parser.add_argument('--list', action='store_true', help='Dry-run: show matches without copying/moving')
    args = parser.parse_args()
    if not (150 <= args.dpi <= 400):
        parser.error(f"--dpi must be 150-400, got {args.dpi}")
    return args


def main():
    args = parse_args()

    organizer = PdfOrganizer(
        input_folder=args.input,
        target=args.target,
        output=args.output,
        move=args.move,
        mirror=args.mirror,
        no_ocr=args.no_ocr,
        ignore_case=args.ignore_case,
        no_normalize=args.no_normalize,
        overwrite=args.overwrite,
        lang=args.lang,
        dpi=args.dpi,
        rotation=args.rotation,
        engine=args.engine,
        use_gpu=args.use_gpu,
    )

    valid, msg = organizer.validate()
    if not valid:
        print(f"错误: {msg}")
        sys.exit(1)

    if not args.no_ocr:
        from tools.ocr_engine import check_engine
        ok, tmsg = check_engine(args.engine)
        if not ok:
            print(f"错误: {tmsg}")
            sys.exit(1)
        ocr_info = f"{args.engine} ({args.lang}, {args.dpi} dpi)"
    else:
        ocr_info = "off (text layer only)"

    print(f"搜索:   {args.target}")
    print(f"输入:   {organizer.input_folder}")
    print(f"输出:   {organizer.output}")
    print(f"OCR:    {ocr_info}")
    print(f"匹配:   {'大小写不敏感' if args.ignore_case else '大小写敏感'}, "
          f"{'无空白归一化' if args.no_normalize else '空白归一化'}")
    print()

    if args.list:
        matches = organizer.scan()
        print()
        print(f"匹配: {len(matches)}/{organizer.stats['total']}")
        for src, dest in matches:
            rel = src.relative_to(organizer.input_folder)
            print(f"  {rel}  ->  {dest.name}")
        return

    success, message = organizer.organize()
    if success:
        print(f"\n[OK] {message}")
    else:
        print(f"\n[FAIL] {message}")
        sys.exit(1)


if __name__ == '__main__':
    main()
