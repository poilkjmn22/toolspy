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
- **V6.6 Cabinet Semantic Layer** (8 phases + V6.6.1/2 refinements):
  - **V6.6.1 — DWG linetype extraction** — `DWGLoader` reads `ltype` handle + `ltype_flags` from dwgread JSON; resolves via LTYPE_CONTROL.entries + LTYPE object list (positional pairing).
  - **V6.6.2 — CabinetRegionAnalyzer** (`cable_engine/graph/cabinet.py`) — detects dashed-rectangle boundaries using `ltype ∈ {ACAD_ISO10W100, HIDDEN, DASHED}` + 4-corner axis-aligned check.
  - **V6.6.3 — name matching** — pairs each boundary with the nearest `EquName`/`EQUNAME` text above; sample names: `11003.ZXW-3号主变110kV电压互感器端子箱`, `42F-主变及无功继电器小室同步向量采集柜`, `4G-500kV第7串断路器测控柜`.
  - **V6.6.4 — containment** — `assign_terminals_to_cabinets()` maps NO/ObjTerm.Name ATTRIBs to smallest enclosing cabinet bbox.
  - **V6.6.5 — schema + persistence** — `cabinets` + `cabinet_terminals` tables; `TopologyStage` runs cabinet analysis BEFORE the analyzer and persists at the end.
  - **V6.6.6 — IR + GraphNode** — `CabinetRegion` is now a first-class IR entity; `NodeType.CABINET`, `EdgeType.CONTAINS` added.
  - **V6.6.7 — viewer APIs** — `/api/cabinets`, `/api/cabinet/{id}`, plus `get_document_topology()` now includes `cabinet_regions[]` for the cabinet-aware 图纸 tab.
  - **V6.6.8 (Phase 8) — cabinet-restricted terminal search** — REMOVED in V6.6.1 because legitimate cross-cabinet wire pairs were being filtered out. Example: D0210-15 has WS `11003-311(1)` at x=286 (in cab_002); its left terminal `21CD:1` at x=186 is in cab_001 — different cabinet from the WS, but legitimately the wire's left endpoint. Filter rejected it → empty terminal.
  - **V6.6.1 fix**: cabinet id collision (`cab_001` collision across docs) — fixed by prefixing with `doc.content_hash[:12]`. Also: removed Phase 8 cabinet bbox gate from terminal bucket; V6.5.3's 200-unit distance threshold is sufficient for noise removal. Spatial cabinet data still used for cabinet_name lookups (V6.6 spatial → V6.5 text-search fallback).
- **V6.6.2 same-side cabinet constraint** — re-introduces cabinet filter on terminal pairing at a different granularity: per-cable per-side, NOT per-WS. Two user axioms for 回路图:
  - Every terminal on the LEFT (or RIGHT) of a single cable's WS belongs to the same cabinet.
  - WS itself is sandwiched between the two endpoint cabinets and belongs to neither.
  Implementation tracks `left_side_cabinet` / `right_side_cabinet` separately per cable. Once the first core finds a terminal on a side, subsequent cores' same-side candidates must be in the same cabinet (or unknown-cabinet, see V6.6.2b). Two subtleties:
  - **Gap-split aware**: in the gap-split branch (no right-side tags), the cabinet filter is applied SEPARATELY to `local_side` and `remote_side` after the split, NOT on the raw `left_of_ws` pool. Otherwise a left-cabinet filter would wrongly drop candidates that are actually right-side terminals in a different cabinet (e.g. 11003-381 core 5 at x=449 has X1:10 in `left_of_ws` at x=429).
  - **V6.6.2b tolerance**: a candidate whose cabinet is UNKNOWN (`_ws_in_cabinet` returns None because the point sits in a geometric gap between detected cabinet bboxes) is ACCEPTED, not filtered. V6.6 detection is geometrically imperfect — terminal labels often sit in narrow gaps between adjacent cabinet rectangles (e.g. X5:8 at y=156 sits 9 units above cab_007's top edge). Filtering those candidates would falsely reject valid terminals; accepting them is safer per user preference "empty > wrong" (we don't know which outcome is "wrong" so accept).
  - Results on D0210-{15,16,35,36} (202 cable-core rows): only 1 regression (3B-380 core 1's `remote='IV:31'` filtered because core 4's `remote='III:28'` is in a different V6.6-detected cabinet). This is a user axiom-1 violation (3B-380 physically connects to multiple cabinets on the same side: `11037.MC` and `11003.ZXW`). All V6.5.3 noise fixes preserved (11037-387 cores 3-4 X4:26-27/VI:7-8, 11037-384 cores 1-4 X4:20-23/VI:1-4).

### In Progress
- (none currently)

### Done (this session)
- **V6.6.3 Cabinet viewer UI** — 柜体 tab now shows all detected cabinets grouped by document, with search/filter by name/location/path. Clicking a cabinet opens detail in the right panel (cabinet info, bbox, contained terminals table, boundary vertices). "在图纸中查看" button opens the document in Flyfish CAD viewer, zoomed to the cabinet's bbox (3x scale), with an SVG overlay showing the dashed boundary (red dashed line) and terminal markers (green circles with labels). The overlay is cleared when the Flyfish modal is closed. Known limitation: overlay position is calculated once on open and does not update on pan/zoom (reopen to refresh).
- **V6.6.4 Anonymous block expansion (BLOCK_HEADER-based)** — rewrote `DWGLoader._parse_v5` to use BLOCK_HEADER entity lists instead of BLOCK/ENDBLK nesting. Added `_build_block_header_entity_map`, `_build_block_name_handle_map`, `_json_handle_value`. Three-phase approach: Phase 1 filters model-space entities normally; Phase 2 buffers entities whose handle is in a non-Model_Space BLOCK_HEADER's `entities` array; Phase 3 resolves INSERT → BLOCK entity → BLOCK_HEADER, then emits buffered entities with coordinate transform. Also: `_emit_anonymous_block` accepts `base_pt`; `_emit_transformed_block` skips ATTDEF; `json.dumps` calls use `indent=2` to maintain compatibility with newline-based JSON helpers (`_json_int`, `_json_str`, `_json_first_float`). Result: D0210-16.dwg now detects 16 cabinets (up from 0 at same x-positions), including previously-missing 3B.DZX at x≈50, x≈290, and x≈805.

### Known Issues
- **BLOCK_HEADER-based expansion is geometric-only** — V6.6.4 expanded LWPOLYLINE/geometry entities from anonymous blocks (fixing cabinet detection for D0210-16) but does NOT yet expand TEXT/ATTRIB entities (cable labels, terminal labels). Cable labels like `GY6-136` (D0210-38) and `110351-311` (D0210-35) that live inside anonymous blocks are still unexpandable because their TEXT geometry is inside blocks whose INSERT references don't appear in Model_Space. The TEXT content is captured via the loader's ATTRIB handling (the ATTRIB reference is captured when it appears in entity form, but its spatial coordinate is the block-local one, not the model-space one).
- **V5 P0 only handles DWG**, not PDF. The PDF Loader exists but the pipeline skips it. PDF support returns in V5 P1 when the IR + GraphBuilder are extended for OCR-detected text.
- **`3T-YW` cable label not in `cables` index** — only `3T-YW-B+` / `3T-YW-B-` / `3T-YW-C+` / `3T-YW-C-` (the per-core labels) appear as TEXT entities. The bare `3T-YW` label is inside an anonymous block. V5's pattern (`[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}`) is correctly stricter than V4's, so this is correct behavior, not a bug.
- **Loop index over-matches** — `M1` / `M2` / `10D` / `-OF-12` are classified as loops because they match the broad `_LOOP_TEXT_PATTERN`. Better filtering (reject `M\d+`, `-…`, etc.) is a future tightening; doesn't affect the cable↔terminal chain that the viewer renders.

### Next Steps
- **V6.7 WireTracer** — now that V6.6 gives us Cabinet containment as a spatial index, run DFS for wire tracing INSIDE cabinet bboxes (not the whole document). Reduces search complexity and false-positive rate.
- **CableScheduleAnalyzer** (电缆清册) — currently a stub. With V6.6's spatial index, the table parser is the next priority.
- **Anonymous block TEXT expansion** in `DWGLoader._parse_v5` — V6.6.4 expanded geometry entities (LWPOLYLINE) from anonymous blocks but NOT TEXT/ATTRIB entities. D0210-35 `110351-311` and D0210-38 `GY6-136` remain invisible because their TEXT content lives inside anonymous blocks whose INSERT references don't appear in Model_Space. The fix requires `_emit_transformed_block` to also handle TEXT/MTEXT/ATTRIB entity types.
- **PDF support (V5 P1)** — add `RasterizeStage` (PDF → PNG) and `OcrStage` (PNG → TextEntity via Tesseract/PaddleOCR). Reuse the V5 GraphBuilder + viewer.
- **Knowledge merge (V5 P2)** — `knowledge_nodes` / `knowledge_sources` / `knowledge_edges` tables for cross-document "Cable B3-463 ↔ Terminal X4:3 ↔ Cabinet XX01" queries. Stubbed in the schema but not implemented.
- **Larger batch test** — run on a directory of 1000+ DWG files to validate performance (current: ~3-5s per file on Mac M1; 89 D0202 files completed in 55s).
- **Tighten loop-id regex** — exclude `M\d+`, `-prefix`, `DK\d+`, `M\d+`, block-name candidates from the loop index.