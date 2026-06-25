# ToolsPy

A collection of small, independent Python tools dispatched through a unified CLI.

## Run a tool

Activate `myenv/` first (the venv lives at `myenv/`, **not** `venv/`):

```bash
source myenv/bin/activate

python -m tools --list                    # show registered tools
python -m tools docx-merger <args>         # dispatch via CLI
python -m tools text-sync -l 8000          # server tool
python tools/docx_merger/main.py <args>    # direct entry, no dispatcher
```

A bash wrapper at the repo root `./toolspy` does the same as `python -m tools` but auto-locates `myenv/bin/python` and errors out with a setup hint if `myenv/` is missing.

## Architecture

- `tools/__main__.py` — entry for `python -m tools` (calls `cli.main`).
- `tools/cli.py` — dispatcher. The `TOOLS` dict maps CLI name → import path; each target module must expose `main()`.
- `tools/<name>/` — one directory per tool. Either the package's `__init__.py` re-exports `main` (e.g. `docx_merger`) or the entry submodule does (e.g. `text_sync.server`).
- `TOOLS.md` — Chinese-language architecture/contribution guide (slightly stale; says `tools/__init__.py` and lists ports the current code does not match).
- `tools/` is a **namespace package** (no `__init__.py`). `setup.py` uses `find_packages()` and will therefore NOT install it correctly; the bash `toolspy` wrapper is the real CLI.

## Registered tools

| name           | target module               | notes |
|----------------|-----------------------------|-------|
| `docx-merger`  | `tools.docx_merger`         | merges `.tsx` files → DOCX |
| `text-sync`    | `tools.text_sync.server`    | WebSocket text sync, default port 8000 |
| `file-share`   | `tools.file_share.server`   | LAN file sharing, 5GB/20-file limits, default port 8001 |
| `llm-chat`     | `tools.llm_chat.server`     | Ollama-backed chat, default port 8002 (expects `http://localhost:8081`) |
| `text-extractor` | `tools.text_extractor`    | PDF/XLS/XLSX → .txt extraction; originals untouched; per-file default, `--combine` for single .txt; `--ocr` for scanned/image-only PDFs |
| `pdf-organize`  | `tools.pdf_organize`      | Find PDFs containing a target string; copy/move matches to a new folder named after the target; recursive scan; OCR-aware |
| `process-xlsx-row` | `tools.process_xlsx_row` | Highlight xlsx rows that match a boolean expression (`& \| ! ()`) of cell rules. Rules defined in JSON (`--rules-file`) and/or Python (`--rules-script`); built-in matchers: `equals`, `contains`, `startswith`, `endswith`, `regex`; color by name (`yellow`) or hex (`#FFFF00` / `AARRGGBB`). Default writes `<input>_colored.xlsx`; `--in-place` to overwrite |
| `cable-match-viewer` | `tools.cable_match_viewer` | Web UI to browse a single stage's `cable_match_state.json` + `cable_match_cache.db`. 3-pane UI: cable tree (natural-sorted, matched first) → PDFs under cable → PDF.js preview + OCR text with cable-highlighted matches. Default port 8003. Path-traversal defense on `/file?path=` (must be in state.json processed list). See `cable_match_guide.md` for usage. |

## Add a new tool

1. Create `tools/<name>/` with an `__init__.py` and an entry file exposing `main()`.
2. Register it in `tools/cli.py` `TOOLS` dict (single source of truth — TOOLS.md is documentation, not code).

## Setup gotchas (do not blindly trust the helper scripts)

- **`setup.sh` is broken three ways** — creates `venv/` (should be `myenv/`), runs `pip install python-docs` (typo, not a real package), then `pip freeze > requirements.txt` would clobber the checked-in list. Use the manual flow instead:
  ```bash
  python3 -m venv myenv
  myenv/bin/pip install -r requirements.txt
  ```
- **`requirements.txt` is now complete.** As of the latest update it includes all pip deps the tools need: `lxml`, `pillow`, `python-docx`, `typing_extensions`, `pdfplumber`, `openpyxl`, `xlrd`, `pytesseract`, `aiohttp`, `numpy<2.0`, `python-Levenshtein==0.25.1`. `pypdfium2` is a transitive dep of `pdfplumber` (>=4.18.0) so it installs automatically. **System-level**: Tesseract must be on PATH for `text-extractor --ocr` and `pdf-organize` on scanned PDFs (see Tesseract note below). `python-Levenshtein` is used by `cable_match.py` for the experimental Levenshtein fuzzy tier (off by default).
- **NumPy must stay < 2.0** for the paddleocr 2.x stack (`paddlepaddle 2.6.2` + `paddleocr 2.7.3`). Those C extensions are compiled against NumPy 1.x ABI; NumPy 2.0 changed the C ABI (now `0x2000000` instead of `0x1000009`) and these extensions refuse to load. Symptoms: `ImportError: numpy.core.multiarray failed to import` or `RuntimeError: module compiled against ABI version 0x1000009 but this version of numpy is 0x2000000`. `requirements.txt` pins `numpy<2.0` to prevent this. If you hit it, `pip install 'numpy<2.0'` then `pip install --force-reinstall --no-deps paddlepaddle==2.6.2 paddleocr==2.7.3`. **The paddleocr 3.x stack (paddleocr 3.0+ + paddlepaddle 3.0+) does NOT have this restriction** — it runs fine on NumPy 2.x.
- **Transitive-dep pins in `requirements-paddleocr.txt` to block numpy-2-only versions.** `paddleocr 2.7.3` doesn't pin `scipy`/`matplotlib`/`shapely`/`scikit-image`/`Pillow`/`pdf2docx`/`imgaug`, so pip may resolve them to 2026-versions that hard-require `numpy>=2.0` and silently upgrade numpy to 2.x — breaking paddlepaddle. `requirements-paddleocr.txt` pins all the offenders to their last numpy-1.x-compatible major:

  | package | cap | first numpy-2-only version | cap reason |
  |---------|-----|-----------------------------|-----------|
  | `scipy` | `<1.15` | `1.15+` requires `numpy>=2.0` | pulled by `imgaug -> scipy` |
  | `scikit-image` | `<0.25` | `0.25+` requires `numpy>=2.0` | pulled by `paddleocr` |
  | `matplotlib` | `<3.10` | `3.10+` requires `numpy>=2.0` | pulled by `scikit-image` |
  | `shapely` | `<2.1` | `2.1+` requires `numpy>=2.0` | pulled by `paddleocr` |
  | `Pillow` | `<11` | `12+` requires `numpy>=2.0` | pulled by `paddleocr` |
  | `pdf2docx` | `<0.5.10` | `0.5.10+` requires `Py3.10+` AND pulls newer scipy | pulled by `paddleocr` |
  | `imgaug` | `==0.4.0` | (last release) | pulls `scipy`; cap above covers |

  Removing any of these caps is fine IF you also upgrade to the paddleocr 3.x + paddlepaddle 3.x stack. The 2.x stack is permanently tied to NumPy 1.x because paddlepaddle 2.6.2 is the last release with the 2.x-compatible PaddlePaddle C extensions.
- **`setup.py` `entry_points` is wrong.** It points at `tools:cli` (a package, not a module); would need `tools.cli:main`. Don't rely on `pip install -e .` for a working `toolspy` console command — use the bash wrapper or `python -m tools`.
- **`text-extractor --ocr` and `pdf-organize` need Tesseract.** `text-extractor` on image-only PDFs yields empty `.txt` files unless you pass `--ocr`. `pdf-organize` on a scanned-PDF corpus will skip those PDFs unless Tesseract is on PATH. Install with `brew install tesseract tesseract-lang` (macOS) or `apt install tesseract-ocr tesseract-chi-sim` (Linux). The `chi_sim` (Simplified Chinese) language pack comes with `tesseract-lang`. The tools print a clear install hint if Tesseract is missing.

- **OCR engine is pluggable.** All three tools (`text-extractor`, `pdf-organize`, `cable_match`) accept `--engine {tesseract|paddleocr}` (default `tesseract`). PaddleOCR typically reaches ~85-95% recall on Chinese small-text + dense terminal-block layouts vs Tesseract's ~70-80% on the same. To enable: `pip install -r requirements-paddleocr.txt` (~250 MB pip deps + ~100 MB model files downloaded on first use). On macOS Apple Silicon paddlepaddle runs CPU only — Win/Linux + CUDA recommended for speed. The `cable_match.py` cache schema stores an `ocr_engine` column so Tesseract and PaddleOCR caches coexist without collision.
- **PaddleOCR 2.x vs 3.x** — `PaddleOCREngine.init()` autodetects the major version from `paddleocr.__version__` and uses the correct kwargs:
  - **2.x** (pinned 2.7.3 in `requirements-paddleocr.txt`): `PaddleOCR(use_angle_cls=, lang=, use_gpu=, show_log=)`. GPU via `use_gpu=True` + paddlepaddle-gpu.
  - **3.x** (latest is 3.7.0): PaddleX-based pipeline, NO `use_gpu` / `show_log` kwargs. `PaddleOCR(lang=, use_textline_orientation=, use_doc_orientation_classify=, use_doc_unwarping=)`. GPU via paddlepaddle-gpu + PaddleX autodetect. Result shape: `ocr.predict(img)` returns list of `OCRResult` objects with `.json['rec_texts']` instead of 2.x's nested `result[0][1][0]` tuple.
  - Either path is fine; mixing (e.g. paddleocr 3.x + paddlepaddle 2.6.2) will NOT work — pick matching pair. Both report install hints when init fails.
  - **Silent-fallback detection**: if `init()` raises `EngineNotAvailable`, the worker prints `ERROR: ...` to stderr, falls back to Tesseract, and records `actual_engine='tesseract_fallback'` in the cache + `engine_used='tesseract_fallback'` in `state.json`. The main summary prints an `OCR engine distribution` table — any `tesseract_fallback` row is a stage that produced identical OCR text to a Tesseract-only stage. Re-run that stage with the matching paddleocr/paddlepaddle pair to get real PaddleOCR coverage.
- **Win11 GPU acceleration for PaddleOCR (`-UseGpu` / `USE_GPU=1`).** Default `pip install -r requirements-paddleocr.txt` installs the **CPU-only** paddlepaddle. To enable GPU on Win/Linux + NVIDIA boxes, swap to `paddlepaddle-gpu==2.6.2` from the matching CUDA wheel index:
  ```bash
  # 1. nvidia-smi  → note "CUDA Version" line (driver-supported max)
  # 2. pip uninstall -y paddlepaddle
  # 3. CUDA 11.7 (most Win11 boxes shipped 2022-2024):
  pip install paddlepaddle-gpu==2.6.2 -f https://www.paddlepaddle.org.cn/whl/windows/cu117/noavx
  #    CUDA 11.8: .../whl/windows/cu118/noavx
  #    CUDA 12.x:  .../whl/windows/cu123/noavx
  #    Linux: replace /windows/ with /linux/, drop /noavx
  # 4. python -c "import paddle; print(paddle.device.is_compiled_with_cuda(), paddle.device.cuda.device_count())"
  # 5. Relaunch with -UseGpu (PowerShell) or USE_GPU=1 (bash).
  ```
  The launcher **hard-fails** if `-UseGpu` is set without CUDA support — no silent CPU fallback. The error message prints the exact pip install line for cu117/cu118/cu123. Tesseract stages ignore `-UseGpu`. macOS `-UseGpu` always hard-fails (no CUDA wheel). See `cable_match_guide.md` § "Win11 GPU 加速 PaddleOCR" for the full workflow including the cache-invalidation gotcha when upgrading from CPU→GPU.

## Things to ignore at the repo root

- `http_server.py` — older standalone version of `text-sync`, no `main()`, not wired into the CLI.
- `src/main.py` — older version of `docx-merger`, not wired into the CLI.
- `docs/` — reference material (React/TS snippets), not project docs.

## Code graph

`.codegraph/` is initialized for this project. When investigating code, prefer the `codegraph_*` MCP tools before `grep` / `glob` / `Read`:

- `codegraph_explore` — first call for "how does X work / where is Y" questions.
- `codegraph_search` — find symbols by name (faster than grep for code).
- `codegraph_node` — get one symbol's full body + callers/callees.
- `codegraph_callers` / `codegraph_callees` / `codegraph_impact` — flow & refactor impact.
- `codegraph_files` — file tree with symbol counts (faster than `glob` for layout).
- `codegraph_status` — only if the index seems stale.

After meaningful code changes, refresh the index (verify the exact subcommand with `codegraph --help` — likely `codegraph init -i` or a refresh/incremental command).

## Conventions

- No tests, no lint, no typecheck, no CI (no `.github/`). Don't add a test runner without checking with the user first.
- Each tool uses `argparse` and is independently runnable (`python tools/<name>/...`).
- `myenv/`, `__pycache__/`, `*.pyc`, `*.docx` are gitignored.
