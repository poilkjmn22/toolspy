# cable_engine Architecture (V6.7)

## 1. System Overview

```
DWG file → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                             ↓
                                                           tools/cable_match_viewer/ (aiohttp)
```

A single `TopologyStage` replaces the V4/V5 pipeline (no rasterize, OCR, GraphBuilder, match, fusion, or rule engine stages).

## 2. Document Classification

`CompositeClassifier` (`cable_engine/classifier/`) applies three sub-classifiers to each document:

| Sub-classifier | Weight | Method |
|----------------|--------|--------|
| `KeywordClassifier` | 0.55 | ATTRIB tag hints (2× weight), strong markers (exact type names), keyword lists |
| `GeometryClassifier` | 0.30 | Entity count ratios (lines, arcs, circles, blocks, ATTRIBs, text) |
| `LayoutClassifier` | 0.15 | Text position distribution (quadrant occupancy, y-bucket clustering, margin density) |

Seven business types are detected:

| Type | Count (shengli) | Analyzer |
|------|-----------------|----------|
| `CIRCUIT_LOOP` (回路图) | 269 | `CircuitLoopAnalyzer` |
| `TERMINAL_STRIP` (端子排图) | 185 | `TerminalStripAnalyzer` |
| `CABLE_SCHEDULE` (电缆清册) | 6 | `CableScheduleAnalyzer` (stub) |
| `PROTECTION_DIAGRAM` (保护原理图) | 166 | None (viewer only) |
| `PANEL_LAYOUT` (屏位布置图) | 48 | None (viewer only) |
| `MONITORING_SYSTEM` (状态监测/通风) | 23 | None (viewer only) |
| `UNKNOWN` (目录/封面) | 260 | None (viewer only) |

## 3. Pipeline

```
Loader → Document IR → TopologyStage.run()
                         ├─ Classification
                         ├─ [CIRCUIT_LOOP only] Cabinet analysis
                         ├─ Analyzer dispatch
                         │   ├─ CircuitLoopAnalyzer (回路图)
                         │   ├─ TerminalStripAnalyzer (端子排图)
                         │   └─ CableScheduleAnalyzer (电缆清册)
                         └─ Batch SQLite write
```

`TopologyStage.run()` (`cable_engine/graph/builder.py:1120`):

1. **Classify** document via `CompositeClassifier`.
2. **Delete** existing topology + cabinet rows for this document hash.
3. **Cabinet analysis** (circuit_loop only — V6.7 optimization skips it for other types):
   - `CabinetRegionAnalyzer.analyze()` detects dashed-rectangle boundaries.
   - `assign_terminals_to_cabinets()` assigns terminal NO/ObjTerm.Name ATTRIBs to cabinets.
   - Injects `CabinetRegion` IR entities into the document so the analyzer can use them.
4. **Analyzer dispatch** via `_ANALYZERS_BY_TYPE` dict.
5. **Batch persistence** via `executemany` (V6.7 optimization).

## 4. Document IR

Source-agnostic entity types in `cable_engine/ir/`:

| Entity | Source | Fields |
|--------|--------|--------|
| `TextEntity` | DWG TEXT/MTEXT | text, x, y |
| `LineGeometry` | DWG LINE/LWPOLYLINE | points, layer, handle, custom_fields.ltype |
| `CircleGeometry` | DWG CIRCLE | center, radius |
| `ArcGeometry` | DWG ARC | center, radius, angles |
| `BlockRef` | DWG INSERT | name, insert_point, rotation, scale |
| `AttributeEntity` | DWG ATTRIB | tag, text, x, y |
| `CabinetRegion` | Derived from analyzer | id, bbox, name, location, display_name, ltype |
| Page, PixelImage | PDF (deferred) | — |

## 5. Storage Schema

### `cable_topology`

| Column | Type | Description |
|--------|------|-------------|
| cable_id | TEXT | Cable ID (e.g. `11003-311`) |
| conductor_no | INT | Core number |
| strip_name | TEXT | Terminal strip (e.g. `21CD`) |
| terminal_no | INT | Local terminal number |
| terminal_no_remote | TEXT | Remote terminal (e.g. `X1:9`) |
| cabinet_name | TEXT | Local cabinet display name |
| cabinet_name_remote | TEXT | Remote cabinet display name |
| circuit_desc | TEXT | WireDescription |
| loop_id | TEXT | Loop code |
| source_type | TEXT | `circuit_loop` / `terminal_strip` |
| document_hash | TEXT | Source DWG fingerprint |

### `cabinets`

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT | Globally unique `cab_<doc12>_<NNN>` |
| display_name | TEXT | e.g. `11003.ZXW-3号主变110kV电压互感器端子箱` |
| bbox_x/y/w/h | REAL | Bounding box |
| ltype | TEXT | Linetype (`ACAD_ISO10W100`, `DASHED`, `HIDDEN`) |
| points_json | TEXT | JSON array of corner vertices |

### `cabinet_terminals`

Maps terminals to cabinets. UNIQUE on `(cabinet_id, document_hash, terminal_id, terminal_kind)`.

## 6. Business Rules

### 6.1 CircuitLoopAnalyzer (回路图)

**Cable detection**: `WireSerial` ATTRIB matching `CABLE_ID(N)` — e.g. `11003-387(1)`. Cores ordered top-to-bottom by descending WS y.

**Core line detection** (V6.7: uses bisect on pre-sorted `core_lines`):
- Lines pre-sorted by y. Per-core search uses `bisect_left/right` to find lines within ±30mm of WS y (O(log L + window), down from O(L)).
- Pass 1: line whose x-span covers `wx` (typical WS-on-line layout).
- Pass 2: line with `x_min ≥ 200mm` and span ≥ 50mm (catches WS placed right of line).

**NO-tag ownership pre-computation** (V6.7: O(N) min-finding instead of sort):
- For every NO tag, track `(closest_dist, second_closest_dist)` to WireSerial x positions within ±5 y-keys.
- `_NO_TAG_SHARE_DISTANCE = 200.0`: tags with both closest and 2nd-closest WS within 200 units are "shared"; otherwise owned exclusively by the closest WS.
- ObjTerm.Name tags skip this and use the simpler WS-closest rule.

**Terminal pairing** (per core):
- Collect NO/ObjTerm.Name tags within ±5 y-keys of WS y.
- Split by WS x → `left_of_ws` (x < wx), `right_of_ws` (x > wx).
- Apply V6.5.3 ownership filter + V6.6.2 same-side cabinet filter.
- If `right_of_ws` not empty: closest tag on each side wins.
- If `right_of_ws` empty (WS at far right): gap-split — find largest x-gap ≥ 50mm, split into local/remote.

**V6.6.2 same-side cabinet constraint**:
- Per-cable per-side tracking: `left_side_cabinet` and `right_side_cabinet`.
- First core with valid terminals locks the side cabinet. Subsequent cores must find same-side terminals in the same cabinet.
- Tolerance: if a terminal's point is not inside any detected cabinet bbox (geometric gap), it's accepted (unknown-cabinet).

### 6.2 TerminalStripAnalyzer (端子排图)

- Cable detection via EED (first segment matching CABLE_ID pattern).
- Horizontal line (y-span < 1mm) → cable row.
- Vertical line (x-span < 1mm, length ≥ 20mm) → conductor column.
- Conductor number: NO ATTRIB near vertical-line top.
- Terminal number: digit text along vertical line.
- Strip name: find "1" text left of terminal, then STRIP_NAME text left of "1".
- Local cabinet: text containing "端子排图" minus suffix.
- Remote cabinet: rightmost EQUNAME ATTRIB within ±10mm y.

## 7. Cabinet Semantic Layer (V6.6+)

### 7.1 Pipeline

| Phase | Module | What it does |
|-------|--------|-------------|
| 1 — Linetype | `DWGLoader._maybe_set_ltype` | Populates `LineGeometry.custom_fields['ltype']` for explicit linetypes |
| 2 — Multi-segment | `_find_multi_segment_rects` | Groups 4 separate dashed LINE segments into a closed rectangle |
| 3 — Dashed rects | `_find_dashed_rectangles` | Detects 4-corner axis-aligned LWPOLYLINEs with dashed ltype |
| 4 — Name match | `_match_boundary_text` | Pairs each boundary with nearest keyword text above; location text left |
| 5 — Containment | `CabinetGridIndex` | Grid spatial index assigning terminals to cabinets (V6.7, replaces linear scan) |
| 6 — Persistence | `TopologyStage` | Batch writes to `cabinets` + `cabinet_terminals` tables |

### 7.2 Anonymous Block Expansion (V6.6.4)

`DWGLoader._parse_v5` rewritten to use BLOCK_HEADER entity lists:

- **Phase 1**: Emit model-space entities normally.
- **Phase 2**: Buffer entities whose handle belongs to a non-Model_Space BLOCK_HEADER.
- **Phase 3**: Resolve INSERT → BLOCK entity handle → BLOCK_HEADER handle → buffered entities, emit with coordinate transform.

Key functions:
- `_build_block_header_entity_map(raw)` — maps BLOCK_HEADER handle → `{entities[], base_pt, name}`.
- `_build_block_name_handle_map(raw)` — maps block name → BLOCK entity handle.
- `_emit_anonymous_block(ins, buf, doc, ltype_map, base_pt)` — transforms buffered geometry using INSERT origin, rotation, scale minus base_pt.
- `_emit_transformed_block(etype, orig_block, doc, ltype_map, tx)` — JSON round-trip now uses `indent=2` to maintain compatibility with line-based JSON helpers.

Result: D0210-16.dwg detects 16 cabinets (up from 0), including previously-missing 3B.DZX boundaries.

### 7.3 Performance Optimizations (V6.7)

| Optimization | Impact | Mechanism |
|--------------|--------|-----------|
| `CabinetGridIndex` | O(N) → O(1) cabinet lookup | 50-unit grid cell index, point → cell → smallest cabinet |
| NO sort removal | O(N log N) → O(N) per tag | Track closest+second closest inline instead of list+sort |
| Core line bisect | O(L) → O(log L+window) per core | Pre-sorted lines; `bisect_left/right` for ±30mm window |
| Batch SQLite writes | ~5-10% I/O reduction | `executemany` for topology, cabinets, terminals, strips |
| Cabinet skip for non-loop | ~15% reduction on non-CIRCUIT_LOOP docs | Only run cabinet analysis for `CIRCUIT_LOOP` |
| `json.dumps(indent=2)` | Correctness (not performance) | Compact JSON broke line-based `_json_int`/`_json_str` helpers |

## 8. Viewer (`tools/cable_match_viewer/`)

Three-pane aiohttp web UI on port 8003:

- **Left column (电缆列表)**: cable list with search/filter.
- **Right column**: cable detail — conductors, terminals, cabinets, source documents.
- **Bottom overlay**: DWG preview via Flyfish CAD viewer.
- **柜体 tab**: live cabinet search with filter by name/location/path. Click a cabinet → detail panel (bbox, terminals, boundary vertices). "在图纸中查看" button opens DWG with SVG overlay showing dashed boundary + terminal markers. Zoomed to 3× cabinet bbox.

**Store layer** (`store.py`): `CableViewer` read-only facade. On-demand graph traversal via `_traverse_cable_neighborhood()` uses the SQLite graph edges for terminal/loop associations.

## 9. Key Architectural Decisions

- **Single-stage pipeline**: `TopologyStage` handles everything inline — no V4/V5 stage composition.
- **Cabinet analysis runs BEFORE analyzer**: `CircuitLoopAnalyzer` receives `CabinetRegion` IR entities for spatial containment queries.
- **Batch writes at document level**: Each document's results are collected in lists, flushed via `executemany`, then committed.
- **Grid spatial index for cabinets**: 50-unit cells map points to cabinets in O(1). Replaces the O(N) linear scan.
- **Cabinet analysis is circuit_loop-only**: Other document types skip it entirely (no 3-second delay per document).

## 10. File Map

```
cable_engine/
├── cli.py                       # Entry: scan subcommand
├── classifier/
│   ├── composite.py             # CompositeClassifier (ensemble of 3)
│   ├── keyword.py               # KeywordClassifier
│   ├── geometry.py              # GeometryClassifier
│   └── layout.py                # LayoutClassifier
├── graph/
│   ├── builder.py               # TopologyStage + all analyzers
│   ├── cabinet.py               # CabinetRegionAnalyzer + CabinetGridIndex
│   ├── types.py                 # DocumentGraph (legacy, not used)
│   └── spatial.py               # Uniform-grid spatial index (legacy)
├── ir/
│   ├── entities.py              # Entity, CabinetRegion, BBox, Point
│   ├── geometry.py              # LineGeometry, BlockRef, AttributeEntity, etc.
│   ├── document.py              # Document, DocumentType
│   └── pdf.py                   # Page, PixelImage (deferred)
├── loaders/
│   ├── dwg_loader.py            # dwgread -O JSON + ezdxf fallback
│   └── pdf_loader.py            # pypdfium2 (deferred)
├── pipeline/
│   ├── stage.py                 # Stage base class
│   └── __init__.py              # Context + Pipeline
└── storage/
    └── sqlite.py                # CableStore (schema + CRUD + batch)

tools/cable_match_viewer/
├── server.py                    # aiohttp app + HTML UI
└── store.py                     # CableViewer read-only facade
```

## 11. Known Limitations

- **BLOCK_HEADER expansion is geometric-only**: V6.6.4 expands LWPOLYLINE/geometry from anonymous blocks but NOT TEXT/ATTRIB entities. Cable labels like `GY6-136` (D0210-38) and `110351-311` (D0210-35) remain invisible because their text geometry is inside anonymous blocks whose INSERT references don't appear in Model_Space.
- **3T-YW label**: Only per-core labels (`3T-YW-B+` etc.) appear as TEXT entities. The bare `3T-YW` label is inside an anonymous block.
- **Loop index over-matches**: `M1`/`M2`/`10D`/`-OF-12` match the loop text pattern but aren't loop IDs.
- **PDF support deferred**: RasterizeStage + OCRStage not yet implemented.
