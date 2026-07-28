# ToolsPy

A collection of small, independent Python tools dispatched through a unified CLI.

## Run a tool

V5 focuses on `cable_engine` (graph-based document intelligence) and the
`cable_match_viewer`. Use Python 3.12 (the V5 baseline):

```bash
source myenv312/bin/activate              # or: source myenv/bin/activate for legacy 3.9

python -m cable_engine.cli scan \
    --input /path/to/dwg/folder \
    --db /path/to/cable.db

python -m tools.cable_match_viewer.server \
    --db /path/to/cable.db --port 8003
```

A bash wrapper at the repo root `./toolspy` does the same as `python -m tools`
but auto-locates `myenv/bin/python` and errors out with a setup hint if `myenv/` is missing.

## Architecture (V7.0)

See `docs/cable_engine_architecture.md` (English) / `docs/cable_engine_architecture_zh.md` (中文).

Key change from V5/V6: V7.0 replaces all V6 fallback methods (icon y-bucket, geometry U-top tracing, circle search, endpoint fallback, icon grouping, text y-bucket) with a single `_cabinet_path_trace()` algorithm that uses cabinet vertical edges as references for horizontal bus detection.

## Layout

- `cable_engine/` — multi-source document pipeline
  - `ir/` — source-agnostic Document / Entity IR
    - `entities.py` — base Entity + TextEntity/LineEntity/SymbolEntity
    - `geometry.py` — V5 geometry entities (LineGeometry, CircleGeometry,
       ArcGeometry, BlockRef, AttributeEntity)
    - `pdf.py` — PDF-specific (Page, PixelImage)
    - `document.py` — Document, DocumentType
  - `loaders/` — DWGLoader (dwgread -O JSON + ezdxf fallback)
  - `pipeline/` — Context + Stage + Pipeline
  - `classifier/` — ClassificationStage (early stage, runs before TopologyStage)
  - `graph/` — V7.0 TopologyStage (replaces V5 GraphBuilderStage)
    - `builder.py` — `TopologyStage` + `CircuitLoopAnalyzer` with `_cabinet_path_trace`
    - `cabinet.py` — `CabinetRegionAnalyzer` + `CabinetGridIndex`
    - `types.py` — Legacy DocumentGraph (kept for reference)
    - `spatial.py` — Legacy spatial index (kept for reference)
  - `storage/` — single `cable.db` (CableStore + ensure_schema)
  - `layout/structure/` — V9 spatial-structure analyzers
    - `column.py` — `ColumnAnalyzer` (x-aligned → VERTICAL_COLUMN)
    - `row.py` — `RowAnalyzer` (y-aligned → HORIZONTAL_ROW)
    - `grid.py` — `GridAnalyzer` (cols×rows → GRID)
  - `layout/table/` — V9 equipment table parser (PANEL_LAYOUT)
    - `model.py` — `TableArea`/`TableRow`/`TableCell` data classes
    - `detector.py` — `detect_table_regions` (rectangle-based search)
    - `parser.py` — `parse_table_at` (text clustering, header detection, column typing)
    - `matcher.py` — `match_to_devices` (name column → DeviceCandidate.features['table_info'])
  - `layout/spatial/` — V9 SpatialGraph (spatial relations between layout nodes)
    - `model.py` — `SpatialNode`/`SpatialEdge`/`SpatialGraph` + `SpatialRelation` enum
    - `bridge.py` — `lift(tree)` builds SpatialGraph from LayoutTree
  - `layout/semantics/` — V9 Semantic annotation (P4)
    - `evidence.py` — `EvidenceSource` base + 5 concrete sources
    - `fusion.py` — `SemanticScoreEngine` fusion engine
    - `group_type.py` — `GroupSemanticResolver` (thin wrapper)
  - `layout/position/` — V9 PANEL_POSITION parser (屏位布置图)
    - `model.py` — `PositionCell`/`PositionRow`/`UsageTable`/`UsageTableRow`
    - `detector.py` — `detect_room` (long-line boundary), `detect_cells` (F-number rects), `cluster_rows` (Y-grouping)
    - `parser.py` — `parse_usage_table` (右侧屏屏用途一览表)
    - `crossref.py` — `cross_reference` (F编号 ↔ 表格行)
    - `builder.py` — `build_position_tree` (full pipeline → LayoutTree with ROOM→POSITION_ROW→POSITION_CELL)
- `tools/cable_match_viewer/` — V5 minimal viewer
  - `server.py` — aiohttp app (~1368 lines including HTML + JS + handlers)
  - `store.py` — read-only CableViewer facade (get_document_position for V9 position tree)
- `docs/cable_engine_architecture.md` — V7.0 architecture reference
- `docs/cable_engine_architecture_zh.md` — V7.0 architecture reference (中文)

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
| `cable-match-viewer` | `tools.cable_match_viewer` | V5 minimal viewer for `cable.db`. Two-pane UI: cable list (left) + cable detail (right, on-demand graph traversal for terminals/loops) + bottom file preview (iframe). Default port 8003. See `tools/cable_match_viewer/server.py`. |

## Add a new tool

1. Create `tools/<name>/` with an `__init__.py` and an entry file exposing `main()`.
2. Register it in `tools/cli.py` `TOOLS` dict (single source of truth — TOOLS.md is documentation, not code).

## Setup gotchas (do not blindly trust the helper scripts)

- **`setup.sh` is broken three ways** — creates `venv/` (should be `myenv/`), runs `pip install python-docs` (typo, not a real package), then `pip freeze > requirements.txt` would clobber the checked-in list. Use the manual flow instead:
  ```bash
  python3 -m venv myenv
  myenv/bin/pip install -r requirements.txt
  ```
- **Python 3.12** is the V5 baseline (`myenv312/`). Use it for `cable_engine`. The legacy `myenv/` (3.9) still works for `tools/text_extractor` and the older `tools/ocr_engine`.
- **`requirements.txt` is now complete.** All V5 deps are pure Python (no numpy/paddleocr dep chain): `lxml`, `pillow`, `python-docx`, `typing_extensions`, `pdfplumber`, `openpyxl`, `xlrd`, `pytesseract`, `aiohttp`, `python-Levenshtein==0.25.1`. `pypdfium2` is a transitive dep of `pdfplumber` (>=4.18.0).
- **`setup.py` `entry_points` is wrong.** It points at `tools:cli` (a package, not a module); would need `tools.cli:main`. Don't rely on `pip install -e .` for a working `toolspy` console command — use the bash wrapper or `python -m tools`.
- **`text-extractor --ocr` and `pdf-organize` need Tesseract.** Install with `brew install tesseract tesseract-lang` (macOS) or `apt install tesseract-ocr tesseract-chi-sim` (Linux).

## Things to ignore at the repo root

- `http_server.py` — older standalone version of `text-sync`, no `main()`, not wired into the CLI.
- `src/main.py` — older version of `docx-merger`, not wired into the CLI.
- Other `docs/*` — reference material (React/TS snippets), not project docs.

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
- `myenv/`, `myenv312/`, `__pycache__/`, `*.pyc`, `*.docx`, `cable.db*` are gitignored.


### Quick validation

Use the small test DWG set for quick validation (full-scale scans take too long):

```bash
source myenv312/bin/activate
python -m cable_engine.cli scan \
    --input ~/Documents/work/nengzhong/testPdf/dwgAndPdf/ \
    --db /tmp/test_cable.db
```

Or test position tree + usage table for a single file:

```python
from cable_engine.loaders.dwg_loader import DWGLoader
from cable_engine.layout.position.builder import build_position_tree
from cable_engine.layout.stage import _layout_tree_to_dict

doc = DWGLoader().load('~/Documents/work/nengzhong/testPdf/dwgAndPdf/10-W978-B768ⅡZ-D0201-05.dwg')
tree = build_position_tree(doc)
d = _layout_tree_to_dict(tree)
```

## Known Issues
- **V5 P0 only handles DWG**, not PDF. The PDF Loader exists but the pipeline skips it. PDF support returns in V5 P1 when the IR + GraphBuilder are extended for OCR-detected text.
- **`3T-YW` cable label not in `cables` index** — only `3T-YW-B+` / `3T-YW-B-` / `3T-YW-C+` / `3T-YW-C-` (the per-core labels) appear as TEXT entities. The bare `3T-YW` label is inside an anonymous block. V5's pattern (`[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}`) is correctly stricter than V4's, so this is correct behavior, not a bug.
- **Loop index over-matches** — `M1` / `M2` / `10D` / `-OF-12` are classified as loops because they match the broad `_LOOP_TEXT_PATTERN`. Better filtering (reject `M\d+`, `-…`, etc.) is a future tightening; doesn't affect the cable↔terminal chain that the viewer renders.
- **Short bus segments** — when the horizontal bus line does not span the full distance, the far-side terminal may not be found (lies beyond x_tol).

### Next Steps
- **Validate PANEL_POSITION on real data** — run `build_position_tree` on D0201-05.dwg, verify room/cell/table/crossref output.
- **Improve room detection** — handle variable room structures (e.g., closed polylines instead of long lines).
- **Handle short bus segments** — when only a short segment is detected, relax x_tol or trace the neighbor cabinet.
- **PDF support (V5 P1)** — add `RasterizeStage` (PDF → PNG) and `OcrStage` (PNG → TextEntity via Tesseract/PaddleOCR). Reuse the V5 GraphBuilder + viewer.
- **Knowledge merge (V5 P2)** — `knowledge_nodes` / `knowledge_sources` / `knowledge_edges` tables for cross-document "Cable B3-463 ↔ Terminal X4:3 ↔ Cabinet XX01" queries. Stubbed in the schema but not implemented.
- **Tighten loop-id regex** — exclude `M\d+`, `-prefix`, `DK\d+`, `M\d+`, block-name candidates from the loop index.