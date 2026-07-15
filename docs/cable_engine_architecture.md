# cable_engine Architecture (V7.0)

## 1. System Overview

```
DWG file → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                             ↓
                                                           tools/cable_match_viewer/ (aiohttp)
```

A single `TopologyStage` replaces the V4/V5 pipeline.

## 2. Document Classification

`CompositeClassifier` (`cable_engine/classifier/`) applies three sub-classifiers:

| Sub-classifier | Weight | Method |
|----------------|--------|--------|
| `KeywordClassifier` | 0.55 | ATTRIB tag hints (2× weight), strong markers, keyword lists |
| `GeometryClassifier` | 0.30 | Entity count ratios (lines, arcs, circles, blocks, ATTRIBs, text) |
| `LayoutClassifier` | 0.15 | Text position distribution (quadrant occupancy, y-bucket clustering, margin density) |

Seven business types:

| Type | Count | Analyzer |
|------|-------|----------|
| `CIRCUIT_LOOP` (回路图) | 269 | `CircuitLoopAnalyzer` |
| `TERMINAL_STRIP` (端子排图) | 185 | `TerminalStripAnalyzer` |
| `CABLE_SCHEDULE` (电缆清册) | 6 | `CableScheduleAnalyzer` |
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

`TopologyStage.run()` (`cable_engine/graph/builder.py`):

1. **Classify** document via `CompositeClassifier`.
2. **Delete** existing topology + cabinet rows for this document hash.
3. **Cabinet analysis** (circuit_loop only):
   - `CabinetRegionAnalyzer.analyze()` detects dashed-rectangle boundaries.
   - `assign_terminals_to_cabinets()` assigns terminal ATTRIBs to cabinets.
   - Injects `CabinetRegion` IR entities into the document.
4. **Analyzer dispatch** via `_ANALYZERS_BY_TYPE` dict.
5. **Batch persistence** via `executemany`.

## 4. Document IR

| Entity | Source | Fields |
|--------|--------|--------|
| `TextEntity` | DWG TEXT/MTEXT | text, x, y |
| `LineGeometry` | DWG LINE/LWPOLYLINE | points, layer, handle, custom_fields.ltype |
| `CircleGeometry` | DWG CIRCLE | center, radius |
| `ArcGeometry` | DWG ARC | center, radius, angles |
| `BlockRef` | DWG INSERT | name, insert_point, rotation, scale |
| `AttributeEntity` | DWG ATTRIB | tag, text, x, y |
| `CabinetRegion` | Derived from analyzer | id, bbox, name, location, display_name, ltype |

## 5. Storage Schema

### `cable_topology`

| Column | Type | Description |
|--------|------|-------------|
| cable_id | TEXT | Cable ID |
| conductor_no | INT | Core number |
| strip_name | TEXT | Terminal strip |
| terminal_no | INT | Local terminal number |
| terminal_no_remote | TEXT | Remote terminal |
| cabinet_name | TEXT | Local cabinet display name |
| cabinet_name_remote | TEXT | Remote cabinet display name |
| circuit_desc | TEXT | WireDescription |
| loop_id | TEXT | Loop code |
| source_type | TEXT | `circuit_loop` / `terminal_strip` / `cable_schedule` |
| document_hash | TEXT | Source DWG fingerprint |

### `cabinets`, `cabinet_terminals`, `text_entities`

Standard schemas for cabinet containment and full-text search.

## 6. CircuitLoopAnalyzer — Cabinet Path Tracing (V7.0)

### Overview

V7.0 replaces all V6 fallback methods (icon y-bucket search, geometry U-top tracing, circle search, endpoint fallback, icon grouping, text y-bucket) with a **single** algorithm: `_cabinet_path_trace()`.

### Algorithm

```
for each core (WireSerial at position wx, wy):
  1. Find core line (horizontal bus) within ±30mm of wy
     - Pass 1: line whose x-span covers wx
     - Pass 2: line with x_min ≥ 200 and span ≥ 50mm
  2. _cabinet_path_trace(side):
     a. Collect cabinet vertical edges near wy
     b. Score candidate horizontal lines by:
        (crosses_cabinet_edge, spans_wx, dy)
     c. Select best line → endpoint on the specified side
     d. Find terminal icon (circle or TERNO/BL/BR) near endpoint
     e. If no icon, follow 90° turn (vertical segment) to other end
     f. If no icon at vertical end, search for horizontal at other end
     g. Find nearest NO/ObjTerm.Name tag near the icon/endpoint
     h. Return (x, y, text, tag_type) or None
```

### Key Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `_Y_TOL` | 30.0 | Vertical search range for horizontal lines |
| `_CROSS_TOL` | 2.0 | Tolerance for cabinet edge crossing detection |
| `_ENDPOINT_TOL` | 30.0 | Max x-distance from WS to wire endpoint |
| `y_range` | 30 (±15 y-units) | Tag search range in y |
| `x_tol` | 50.0 | Max x-distance from endpoint to tag |

### Cabinet Boundary Filtering

Cabinet boundary edges (dashed LWPOLYLINE/LINE) are filtered out of `core_lines` before path tracing, preventing the algorithm from tracing a cabinet edge as if it were a bus wire:

```
Phase 1: collect all CabinetRegion boundary_handle + bbox edges
Phase 2: filter core_lines:
  a. handle matches cabinet boundary → skip
  b. line y matches cabinet bbox top/bottom ±0.5 and x-span matches → skip
```

### Side Filter for Tag Selection

Tags are filtered by side relative to the WireSerial x-position (`ws_x`):
- LEFT side: only tags with `tag_x < ws_x`
- RIGHT side: only tags with `tag_x > ws_x`

This prevents picking up tags on the opposite side of the WS column.

## 7. Cabinet Semantic Layer

### Pipeline

| Phase | Module | What it does |
|-------|--------|-------------|
| 1 — Linetype | `DWGLoader._maybe_set_ltype` | Populates `LineGeometry.custom_fields['ltype']` |
| 2 — Multi-segment | `_find_multi_segment_rects` | Groups 4 dashed LINE segments into a closed rectangle |
| 3 — Dashed rects | `_find_dashed_rectangles` | Detects 4-corner axis-aligned LWPOLYLINEs with dashed ltype |
| 4 — Name match | `_match_boundary_text` | Pairs each boundary with nearest keyword text |
| 5 — Containment | `CabinetGridIndex` | Grid spatial index assigning terminals to cabinets |
| 6 — Persistence | `TopologyStage` | Batch writes to `cabinets` + `cabinet_terminals` tables |

### Anonymous Block Expansion

`DWGLoader._parse_v5` uses BLOCK_HEADER entity lists:

- **Phase 1**: Emit model-space entities normally.
- **Phase 2**: Buffer entities from non-Model_Space BLOCK_HEADERs.
- **Phase 3**: Resolve INSERT → BLOCK → BLOCK_HEADER → buffered entities with coordinate transform.

### Performance Optimizations

| Optimization | Impact | Mechanism |
|--------------|--------|-----------|
| `CabinetGridIndex` | O(N) → O(1) cabinet lookup | 50-unit grid cell index |
| Core line bisect | O(L) → O(log L + window) per core | Pre-sorted lines; `bisect_left/right` |
| Batch SQLite writes | ~5-10% I/O reduction | `executemany` |
| Cabinet skip for non-loop | ~15% reduction on non-CIRCUIT_LOOP docs | Only for `CIRCUIT_LOOP` |

## 8. Viewer (`tools/cable_match_viewer/`)

Three-pane aiohttp web UI on port 8003:

- **Left column (电缆列表)**: cable list with search/filter.
- **Right column**: cable detail — conductors, terminals, cabinets, source documents.
- **Bottom overlay**: DWG preview via Flyfish CAD viewer.
- **柜体 tab**: live cabinet search. Click → detail panel. "在图纸中查看" opens DWG with SVG overlay.

**Store layer** (`store.py`): `CableViewer` read-only facade. On-demand SQLite graph traversal.

## 9. File Map

```
cable_engine/
├── cli.py                       # Entry: scan subcommand
├── classifier/
│   ├── composite.py             # CompositeClassifier
│   ├── keyword.py               # KeywordClassifier
│   ├── geometry.py              # GeometryClassifier
│   └── layout.py                # LayoutClassifier
├── graph/
│   ├── builder.py               # TopologyStage + all analyzers
│   ├── cabinet.py               # CabinetRegionAnalyzer + CabinetGridIndex
│   ├── types.py                 # Legacy DocumentGraph
│   └── spatial.py               # Legacy spatial index
├── ir/
│   ├── entities.py              # Entity, CabinetRegion, BBox, Point
│   ├── geometry.py              # LineGeometry, BlockRef, AttributeEntity
│   ├── document.py              # Document, DocumentType
│   └── pdf.py                   # Page, PixelImage (deferred)
├── loaders/
│   ├── dwg_loader.py            # dwgread -O JSON + ezdxf fallback
│   └── pdf_loader.py            # pypdfium2 (deferred)
├── pipeline/
│   ├── stage.py                 # Stage base class
│   └── __init__.py              # Context + Pipeline
└── storage/
    └── sqlite.py                # CableStore

tools/cable_match_viewer/
├── server.py                    # aiohttp app + HTML UI
└── store.py                     # CableViewer read-only facade
```

## 10. Known Limitations

- **DWG only**: PDF support deferred (no RasterizeStage / OcrStage yet).
- **Anonymous block text**: TEXT/ATTRIB inside anonymous blocks remain invisible.
- **Loop index over-matches**: `M1`/`M2`/`10D`/`-OF-12` match the loop text pattern.
- **Short bus segments**: When the horizontal bus line does not span the full distance between left and right terminals (e.g., only a short segment is detected), the far-side terminal may not be found because it lies beyond `x_tol=50`.
