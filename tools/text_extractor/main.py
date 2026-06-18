#!/usr/bin/env python3
import argparse
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import openpyxl
import pdfplumber
import pypdfium2 as pdfium
import pytesseract
import xlrd


SUPPORTED_EXTS = {'.pdf', '.xls', '.xlsx'}
FILE_HEADER_TEMPLATE = "# Extracted from {name} by text-extractor\n"
OCR_HEADER_TEMPLATE = (
    "# Extracted from {name} by text-extractor "
    "(OCR via Tesseract, lang={lang}, dpi={dpi})\n"
)


def check_tesseract() -> Tuple[bool, str]:
    """Verify Tesseract binary is installed and return its version.

    Returns (ok, message). On failure, message contains install instructions.
    """
    if shutil.which('tesseract') is None:
        return False, (
            "Tesseract 未安装。请运行:\n"
            "  macOS:  brew install tesseract tesseract-lang\n"
            "  Linux:  apt install tesseract-ocr tesseract-ocr-chi-sim"
        )
    try:
        ver = pytesseract.get_tesseract_version()
        return True, f"Tesseract {ver}"
    except Exception as e:
        return False, f"Tesseract 检查失败: {e}"


def _row_is_empty(row) -> bool:
    for c in row:
        if c is None:
            continue
        if isinstance(c, str) and c.strip() == "":
            continue
        return False
    return True


def _pdfplumber_extract(path: Path, warn: bool = True) -> str:
    parts = [FILE_HEADER_TEMPLATE.format(name=path.name)]
    total_chars = 0
    page_count = 0
    with pdfplumber.open(str(path)) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            parts.append(f"\n=== Page {i} ===\n\n")
            text = page.extract_text() or ""
            parts.append(text)
            total_chars += len(text)
    if warn and total_chars == 0 and page_count > 0:
        print(
            f"⚠ {path}: no text layer ({page_count} pages, 0 chars); "
            f"use --ocr to extract via OCR",
            file=sys.stderr,
        )
    return "".join(parts)


def _ocr_extract(path: Path, lang: str, dpi: int) -> str:
    parts = [OCR_HEADER_TEMPLATE.format(name=path.name, lang=lang, dpi=dpi)]
    pdf = pdfium.PdfDocument(str(path))
    try:
        scale = dpi / 72.0
        for i, page in enumerate(pdf, 1):
            parts.append(f"\n=== Page {i} ===\n\n")
            pil = page.render(scale=scale).to_pil()
            try:
                text = pytesseract.image_to_string(pil, lang=lang) or ""
            finally:
                pil.close()
            parts.append(text)
    finally:
        pdf.close()
    return "".join(parts)


def _xlsx_extract(path: Path) -> str:
    parts = [FILE_HEADER_TEMPLATE.format(name=path.name)]
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            parts.append(f"\n=== Sheet: {sheet_name} ===\n\n")
            for row in ws.iter_rows(values_only=True):
                if _row_is_empty(row):
                    continue
                parts.append("\t".join("" if c is None else str(c) for c in row))
                parts.append("\n")
    finally:
        wb.close()
    return "".join(parts)


def _xls_extract(path: Path) -> str:
    parts = [FILE_HEADER_TEMPLATE.format(name=path.name)]
    book = xlrd.open_workbook(str(path))
    for sheet in book.sheets():
        parts.append(f"\n=== Sheet: {sheet.name} ===\n\n")
        for row_idx in range(sheet.nrows):
            row = sheet.row_values(row_idx)
            if _row_is_empty(row):
                continue
            parts.append("\t".join("" if c is None else str(c) for c in row))
            parts.append("\n")
    return "".join(parts)


def extract_text(
    path,
    ocr: bool = False,
    lang: str = 'chi_sim+eng',
    dpi: int = 300,
    warn: bool = True,
) -> str:
    """Extract text from a single file. Convenience function for use by other tools.

    Args:
        path: file path (PDF/XLS/XLSX)
        ocr: if True, use Tesseract OCR for PDFs (slower; works on scanned/image-only PDFs)
        lang: Tesseract language packs (e.g. 'chi_sim+eng', 'eng')
        dpi: render DPI for OCR (150-400)
        warn: if True, print a warning to stderr when a PDF has no text layer and ocr=False

    Returns:
        Extracted text with file header and per-page/per-sheet markers.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext == '.pdf':
        if ocr:
            return _ocr_extract(path, lang, dpi)
        return _pdfplumber_extract(path, warn=warn)
    if ext == '.xlsx':
        return _xlsx_extract(path)
    if ext == '.xls':
        return _xls_extract(path)
    raise ValueError(f"不支持的扩展名: {ext}")


class TextExtractor:
    def __init__(
        self,
        sources: List[str],
        output: Optional[str] = None,
        include_subfolders: bool = True,
        combine: bool = False,
        overwrite: bool = False,
        ocr: bool = False,
        lang: str = 'chi_sim+eng',
        dpi: int = 200,
    ):
        self.sources = [Path(s).expanduser() for s in sources]
        self.output = Path(output).expanduser() if output else None
        self.include_subfolders = include_subfolders
        self.combine = combine
        self.overwrite = overwrite
        self.ocr = ocr
        self.lang = lang
        self.dpi = dpi
        self.stats = {
            'total': 0,
            'processed': 0,
            'skipped': 0,
            'failed': 0,
            'start_time': datetime.now(),
        }
        self.failures: List[Tuple[Path, str]] = []

    def _default_output_dir(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self.sources[0].parent if self.sources else Path.cwd()
        return base / f"text_extracted_{ts}"

    def validate(self) -> Tuple[bool, str]:
        if not self.sources:
            return False, "至少需要一个源文件或文件夹"

        for s in self.sources:
            if not s.exists():
                return False, f"源路径不存在: {s}"

        if self.combine:
            if self.output is None:
                return False, "--combine 模式必须通过 -o/--output 指定输出 .txt 文件"
        else:
            if self.output is None:
                self.output = self._default_output_dir()
                print(f"输出目录: {self.output}")

        if not (150 <= self.dpi <= 400):
            return False, f"--dpi 必须在 150-400 之间，当前: {self.dpi}"

        return True, "验证通过"

    def _prepare_output(self) -> None:
        if self.combine:
            self.output.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.output.mkdir(parents=True, exist_ok=True)

    def discover(self) -> List[Tuple[Path, Path, Optional[Path]]]:
        items: List[Tuple[Path, Path, Optional[Path]]] = []
        for src in self.sources:
            if src.is_file():
                if src.suffix.lower() in SUPPORTED_EXTS:
                    items.append((src, self._output_path_for(src), None))
            elif src.is_dir():
                pattern = "**/*" if self.include_subfolders else "*"
                for p in sorted(src.glob(pattern)):
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                        items.append((p, self._output_path_for(p, base_folder=src), src))
        return items

    def _output_path_for(self, input_path: Path, base_folder: Optional[Path] = None) -> Path:
        if self.combine:
            return self.output
        base = base_folder if base_folder is not None else input_path.parent
        rel = input_path.relative_to(base)
        return self.output / rel.with_suffix('.txt')

    def _extract(self, path: Path) -> str:
        return extract_text(path, ocr=self.ocr, lang=self.lang, dpi=self.dpi, warn=True)

    def _write_output(self, out_path: Path, content: str) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8', errors='replace') as f:
            f.write(content)

    def convert(self) -> Tuple[bool, str]:
        valid, msg = self.validate()
        if not valid:
            return False, msg

        if self.ocr:
            ok, tmsg = check_tesseract()
            if not ok:
                return False, tmsg
            print(f"OCR 引擎: {tmsg}")

        items = self.discover()
        self.stats['total'] = len(items)
        if not items:
            return False, f"未找到可处理的文件 (支持: {', '.join(sorted(SUPPORTED_EXTS))})"

        self._prepare_output()
        print(f"找到 {self.stats['total']} 个待处理文件")

        combined_handle = None
        try:
            if self.combine:
                if self.output.exists() and not self.overwrite:
                    return False, f"输出文件已存在: {self.output} (使用 --overwrite 覆盖)"
                combined_handle = open(self.output, 'w', encoding='utf-8', errors='replace')

            for idx, (src, out, _base) in enumerate(items, 1):
                print(f"处理文件 {idx}/{self.stats['total']}: {src.name}")

                if not self.combine and out.exists() and not self.overwrite:
                    print(f"  跳过: 输出已存在 {out} (使用 --overwrite 覆盖)")
                    self.stats['skipped'] += 1
                    continue

                try:
                    content = self._extract(src)
                    if self.combine:
                        combined_handle.write(f"\n# === File: {src.name} ===\n")
                        combined_handle.write(content)
                        combined_handle.flush()
                    else:
                        self._write_output(out, content)
                    self.stats['processed'] += 1
                except Exception as e:
                    print(f"  错误: {e}")
                    self.failures.append((src, str(e)))
                    self.stats['failed'] += 1
                    continue
        finally:
            if combined_handle is not None:
                combined_handle.close()

        return self._report()

    def _report(self) -> Tuple[bool, str]:
        duration = (datetime.now() - self.stats['start_time']).total_seconds()
        print()
        print("=== 完成 ===")
        print(f"总文件:   {self.stats['total']}")
        print(f"成功:     {self.stats['processed']}")
        print(f"跳过:     {self.stats['skipped']}")
        print(f"失败:     {self.stats['failed']}")
        print(f"耗时:     {duration:.2f}s")
        if self.combine:
            print(f"输出:     {self.output}")
        if self.failures:
            print("\n失败列表:")
            for f, err in self.failures:
                print(f"  {f}: {err}")

        if self.stats['total'] == 0:
            return False, "未找到可处理的文件"
        if self.stats['processed'] > 0:
            return True, f"成功处理 {self.stats['processed']}/{self.stats['total']} 个文件"
        if self.stats['failed'] > 0:
            return False, f"所有文件处理失败 ({self.stats['failed']} 个)"
        return True, f"所有 {self.stats['total']} 个文件均已存在，未处理新文件"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Extract text from PDF/XLS/XLSX files to .txt files (originals untouched).',
    )
    parser.add_argument('source', nargs='*', help='Source file or folder(s) to process')
    parser.add_argument('-o', '--output', help='Output directory (per-file mode) or output file (--combine mode)')
    parser.add_argument('--no-subfolders', action='store_true', help='Do not recurse into subfolders')
    parser.add_argument('--combine', action='store_true', help='Concatenate all extracted text into a single .txt file')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing output files')
    parser.add_argument('--list', action='store_true', help='List discoverable files without extracting')
    parser.add_argument('--ocr', action='store_true', help='Use Tesseract OCR for image-only PDFs (needs `brew install tesseract tesseract-lang`)')
    parser.add_argument('--lang', default='chi_sim+eng', help='Tesseract language packs (default: chi_sim+eng)')
    parser.add_argument('--dpi', type=int, default=300, help='OCR render DPI, 150-400 (default: 300; use 300+ for small text in technical drawings)')
    args = parser.parse_args()
    if not (150 <= args.dpi <= 400):
        parser.error(f"--dpi must be 150-400, got {args.dpi}")
    return args


def main():
    args = parse_args()

    if not args.source:
        try:
            raw = input("请输入源文件或文件夹路径 (多个用空格分隔，可使用引号): ").strip()
        except EOFError:
            raw = ""
        args.source = shlex.split(raw) if raw else []

    if not args.source:
        print("错误: 必须提供源文件或文件夹路径")
        return

    extractor = TextExtractor(
        sources=args.source,
        output=args.output,
        include_subfolders=not args.no_subfolders,
        combine=args.combine,
        overwrite=args.overwrite,
        ocr=args.ocr,
        lang=args.lang,
        dpi=args.dpi,
    )

    if args.list:
        valid, msg = extractor.validate()
        if not valid:
            print(f"错误: {msg}")
            sys.exit(1)
        items = extractor.discover()
        if not items:
            print("未找到可处理的文件")
            return
        print(f"找到 {len(items)} 个可处理文件:")
        for src, out, base in items:
            base = base or src.parent
            rel = src.relative_to(base)
            print(f"  [{base}] {rel}  ->  {out}")
        return

    success, message = extractor.convert()
    if success:
        print(f"\n[OK] {message}")
    else:
        print(f"\n[FAIL] {message}")
        sys.exit(1)


if __name__ == '__main__':
    main()
