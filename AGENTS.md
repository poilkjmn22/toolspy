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

## Add a new tool

1. Create `tools/<name>/` with an `__init__.py` and an entry file exposing `main()`.
2. Register it in `tools/cli.py` `TOOLS` dict (single source of truth — TOOLS.md is documentation, not code).

## Setup gotchas (do not blindly trust the helper scripts)

- **`setup.sh` is broken three ways** — creates `venv/` (should be `myenv/`), runs `pip install python-docs` (typo, not a real package), then `pip freeze > requirements.txt` would clobber the checked-in list. Use the manual flow instead:
  ```bash
  python3 -m venv myenv
  myenv/bin/pip install -r requirements.txt
  ```
- **`requirements.txt` is now complete.** As of the latest update it includes all pip deps the tools need: `lxml`, `pillow`, `python-docx`, `typing_extensions`, `pdfplumber`, `openpyxl`, `xlrd`, `pytesseract`, `aiohttp`. `pypdfium2` is a transitive dep of `pdfplumber` (>=4.18.0) so it installs automatically. **System-level**: Tesseract must be on PATH for `text-extractor --ocr` and `pdf-organize` on scanned PDFs (see Tesseract note below).
- **`setup.py` `entry_points` is wrong.** It points at `tools:cli` (a package, not a module); would need `tools.cli:main`. Don't rely on `pip install -e .` for a working `toolspy` console command — use the bash wrapper or `python -m tools`.
- **`text-extractor --ocr` and `pdf-organize` need Tesseract.** `text-extractor` on image-only PDFs yields empty `.txt` files unless you pass `--ocr`. `pdf-organize` on a scanned-PDF corpus will skip those PDFs unless Tesseract is on PATH. Install with `brew install tesseract tesseract-lang` (macOS) or `apt install tesseract-ocr tesseract-ocr-chi-sim` (Linux). The `chi_sim` (Simplified Chinese) language pack comes with `tesseract-lang`. The tools print a clear install hint if Tesseract is missing.

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
