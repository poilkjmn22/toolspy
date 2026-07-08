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

## Architecture (V5)

V5 is a **big-bang replacement** of the V4 cable_engine pipeline. The
core shift: the IR is now graph-first, not text-first.

```
┌─────────────────────────────────────────────────────────────────┐
│  cable_engine.cli scan                                          │
│                                                                 │
│   DWG file                                                       │
│     │                                                           │
│     ▼                                                           │
│   DWGLoader (dwgread -O JSON)                                   │
│     │                                                           │
│     ▼                                                           │
│   Document IR                                                   │
│     ├─ TextEntity                                               │
│     ├─ LineGeometry                                             │
│     ├─ CircleGeometry / ArcGeometry                             │
│     ├─ BlockRef                                                 │
│     └─ AttributeEntity                                           │
│                                                                 │
│     ▼                                                           │
│   GraphBuilderStage                                             │
│     ├─ 1 node per entity (in graph_nodes_v5)                    │
│     ├─ Edges: NEAR / SHARES_ENDPOINT / INSIDE / ATTRIB_OF       │
│     └─ Cable / terminal / loop STRING INDICES (for fast lookup)  │
│                                                                 │
│     ▼                                                           │
│   cable.db (SQLite)                                             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

Viewer (separate process, port 8003):

GET /                        — minimal HTML page
GET /api/cables              — list every cable (string index)
GET /api/cable/{id}          — on-demand graph traversal: terminal/loop
                              associations for this cable via 3-hop BFS
GET /api/document/{hash}     — document metadata
GET /api/document/{hash}/file — raw DWG (browser downloads)
```

**Source-agnostic IR.** PDF support is V5 P1; DWG is the V5 P0 focus
because DWG geometry is the actual data (lines, connections, blocks,
layers, coordinates, topology) that OCR can never recover.

**Graph IS the data.** The viewer never builds full topology at scan
time — it traverses the graph on demand when the user clicks a cable
(see `tools.cable_match_viewer/store.py:CableViewer._traverse_cable_neighborhood`).

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
  - `graph/` — V5 DocumentGraph
    - `types.py` — GraphNode, GraphEdge, DocumentGraph (with
      indices: by_type, by_layer, neighbors, in_neighbors, nodes_within)
    - `spatial.py` — uniform-grid spatial index for radius queries
    - `builder.py` — `GraphBuilderStage` (the V5 centerpiece)
  - `storage/` — single `cable.db` (CableStore + ensure_schema)
- `tools/cable_match_viewer/` — V5 minimal viewer
  - `server.py` — aiohttp app (one file, ~250 lines including HTML)
  - `store.py` — read-only CableViewer facade (does on-demand graph traversal)
- `docs/cable_engine_architecture.md` — V4 architecture reference
  (kept for history; V5 supersedes it). See `docs/v5_architecture.md`
  (TODO when written) for the new doc.

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
- `docs/cable_engine_architecture.md` — V4 architecture doc (superseded by V5 but kept for history).
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

## Progress

### Done
- **V5 big-bang release** (replaces V4 cable_engine/rules + match + stages + V4 viewer):
  - **Unified Document IR** — `cable_engine/ir/` with `Document`, `TextEntity`, `LineGeometry`, `CircleGeometry`, `ArcGeometry`, `BlockRef`, `AttributeEntity`. DWG Loader emits all of them via dwgread -O JSON.
  - **DocumentGraph as first-class artifact** — `cable_engine/graph/types.py:DocumentGraph` with pre-built indices (`by_type`, `by_layer`, neighbors, spatial).
  - **V5 SQLite schema** — `documents`, `cables`, `terminals`, `cable_topology`, `terminal_strips`, `scan_state`. V4 tables removed.
  - **V5 minimal viewer** (`tools/cable_match_viewer/`) — two-pane UI (cable list + cable detail) + bottom file preview. On-demand graph traversal at query time.
  - **Python 3.12** baseline (`myenv312/`).
- **V6.5.1 DocumentClassifier** — 7 business types (circuit_loop, terminal_strip, cable_schedule, protection_diagram, panel_layout, monitoring_system, unknown). Coverage 246 → 447 docs (+82%). `_ANALYZERS_BY_TYPE` dict dispatches per type.
- **V6.5.2 fixes** — shared-bus fix (WS y vs line y), gap-split fix (only when right_of_ws empty), x-distance tiebreaker, bucket widened ±2 → ±5 keys.
- **V6.5.3 NO-tag ownership filter** — pre-compute closest/2nd-closest WS distances per NO tag; 200-unit share threshold. Resolves 11037-387 cores 3-4 (X4:26-27 / VI:7-8) and 11037-384 cores 1-4 (X4:20-23 / VI:1-4).
- **V6.6 Cabinet Semantic Layer** (8 phases, all complete):
  - **V6.6.1 — DWG linetype extraction** — `DWGLoader` reads `ltype` handle + `ltype_flags` from dwgread JSON; resolves via LTYPE_CONTROL.entries + LTYPE object list (positional pairing).
  - **V6.6.2 — CabinetRegionAnalyzer** (`cable_engine/graph/cabinet.py`) — detects dashed-rectangle boundaries using `ltype ∈ {ACAD_ISO10W100, HIDDEN, DASHED}` + 4-corner axis-aligned check.
  - **V6.6.3 — name matching** — pairs each boundary with the nearest `EquName`/`EQUNAME` text above; sample names: `11003.ZXW-3号主变110kV电压互感器端子箱`, `42F-主变及无功继电器小室同步向量采集柜`, `4G-500kV第7串断路器测控柜`.
  - **V6.6.4 — containment** — `assign_terminals_to_cabinets()` maps NO/ObjTerm.Name ATTRIBs to smallest enclosing cabinet bbox.
  - **V6.6.5 — schema + persistence** — `cabinets` + `cabinet_terminals` tables; `TopologyStage` runs cabinet analysis BEFORE the analyzer and persists at the end.
  - **V6.6.6 — IR + GraphNode** — `CabinetRegion` is now a first-class IR entity; `NodeType.CABINET`, `EdgeType.CONTAINS` added.
  - **V6.6.7 — viewer APIs** — `/api/cabinets`, `/api/cabinet/{id}`, plus `get_document_topology()` now includes `cabinet_regions[]` for the cabinet-aware 图纸 tab.
  - **V6.6.8 — cabinet-restricted terminal search** — `CircuitLoopAnalyzer` rejects terminals in a different cabinet than the WS. Preserves V6.5.3 fixes; adds bbox containment as a stronger signal than the 200-unit share threshold.
  - **Critical fix**: cabinet id collision (`cab_001` collision across docs) — fixed by prefixing with `doc.content_hash[:12]`.

### In Progress
- (none currently)

### In Progress
- (none currently)

### Known Issues
- **`dwgread -O JSON` does not expand anonymous block definitions** for DWG AC1024+ files. The cable labels (e.g. `GY6-136` as ATTRIB) and terminal labels (e.g. `21CD:19`) that live inside unnamed blocks are partially visible to the loader (the ATTRIB reference is captured) but their spatial relationships inside the block are NOT. As a result:
  - D0210-38 `GY6-136` (an ATTRIB at x=319, y=79) finds 0 terminals — its expected terminals `10D:13` / `12D:3` (at x≈190, y≈175) are too far from the cable label for the 25-unit NEAR radius to bridge.
  - D0210-35 `110351-311` is invisible — neither the cable label text nor the `21CD:19` terminal labels appear in the model-space JSON output.
  - The 6 cables on D0210-35 (`70F-J701/901/903`, `GPS-70F`, `GPST-70F`) that had L-shape geometry inside anonymous blocks have no topology.
  - **Next step**: implement anonymous-block expansion in `DWGLoader` (the JSON exposes BLOCK / ENDBLK pairs but dwgread's `block_header` references the BLOCK TABLE RECORD, not the BLOCK entity — requires handle translation). Fallback: a third-party DWG reader (ezdxf-dwg add-on can't read AC1024 either).
- **V5 P0 only handles DWG**, not PDF. The PDF Loader exists but the pipeline skips it. PDF support returns in V5 P1 when the IR + GraphBuilder are extended for OCR-detected text.
- **`3T-YW` cable label not in `cables` index** — only `3T-YW-B+` / `3T-YW-B-` / `3T-YW-C+` / `3T-YW-C-` (the per-core labels) appear as TEXT entities. The bare `3T-YW` label is inside an anonymous block. V5's pattern (`[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}`) is correctly stricter than V4's, so this is correct behavior, not a bug.
- **Loop index over-matches** — `M1` / `M2` / `10D` / `-OF-12` are classified as loops because they match the broad `_LOOP_TEXT_PATTERN`. Better filtering (reject `M\d+`, `-…`, etc.) is a future tightening; doesn't affect the cable↔terminal chain that the viewer renders.

### Next Steps
- **V6.7 WireTracer** — now that V6.6 gives us Cabinet containment as a spatial index, run DFS for wire tracing INSIDE cabinet bboxes (not the whole document). Reduces search complexity and false-positive rate.
- **Cabinet graph viewer UI** — in the viewer "柜体" tab, click a cabinet to highlight its dashed boundary + contained terminals in the Flyfish CAD viewer.
- **CableScheduleAnalyzer** (电缆清册) — currently a stub. With V6.6's spatial index, the table parser is the next priority.
- **Anonymous block expansion** in `DWGLoader._parse_v5` — the data-availability root cause for D0210-35 `110351-311` and D0210-38 `GY6-136`'s missing terminals.
- **PDF support (V5 P1)** — add `RasterizeStage` (PDF → PNG) and `OcrStage` (PNG → TextEntity via Tesseract/PaddleOCR). Reuse the V5 GraphBuilder + viewer.
- **Knowledge merge (V5 P2)** — `knowledge_nodes` / `knowledge_sources` / `knowledge_edges` tables for cross-document "Cable B3-463 ↔ Terminal X4:3 ↔ Cabinet XX01" queries. Stubbed in the schema but not implemented.
- **Larger batch test** — run on a directory of 1000+ DWG files to validate performance (current: ~3-5s per file on Mac M1; 89 D0202 files completed in 55s).
- **Tighten loop-id regex** — exclude `M\d+`, `-prefix`, `DK\d+`, `M\d+`, block-name candidates from the loop index.