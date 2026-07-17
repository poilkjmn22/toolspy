# cable_engine Architecture (V8.0)

## 1. System Overview

```
DWG file → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                    ↓
                                                  tools/cable_match_viewer/ (aiohttp)
```

A single `TopologyStage` orchestrates classification, cabinet analysis, and analyzer dispatch. V8 introduces a **GeometryGraph** — a pure graph layer — to replace the V7 procedural `_cabinet_path_trace()` algorithm.

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
                         │   ├─ CircuitLoopAnalyzer (回路图)  ← V8 GeometryGraph
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

### `cable_info` (V8)

| Column | Type | Description |
|--------|------|-------------|
| cable_id | TEXT | Cable ID (PK) |
| document_hash | TEXT | Source DWG fingerprint (PK) |
| wire_type | TEXT | Cable model + cross-section, e.g. `ZBN-KYJYP2-23-1kV-4x6` |

One row per cable per document. Populated by `CircuitLoopAnalyzer.analyze()` via spatial matching of WIRECODE→nearest WIRETYPE ATTRIB.

### `cabinets`, `cabinet_terminals`, `text_entities`

Standard schemas for cabinet containment and full-text search.

## 6. V8 GeometryGraph Architecture

### 6.1 Graph Layer (Semantic-Free)

V8 replaces the procedural `_cabinet_path_trace()` with a **pure geometry graph** (`cable_engine/electrical/`). The graph has no business semantics — no Terminal, Cabinet, or Device concepts:

```
GeometryGraph
├── nodes: dict[int, GeoNode]      — (x, y, node_type, optional tag_name/tag_text)
├── edges: dict[int, GeoEdge]      — (node_a, node_b, edge_type, length)
├── adj: dict[int, list[(neighbor_id, edge_id)]]  — adjacency list
└── spatial: SpatialIndex           — grid-based spatial index
```

**Node types** (`GeoNodeType`):

| Type | Description |
|------|-------------|
| `TAG` | ATTRIB text with tag_name (NO, ObjTerm.Name, WireSerial, etc.) |
| `TEXT` | Ordinary TEXT/MTEXT node |
| `CIRCLE` | Terminal icon marker (CIRCLE entity) |
| `WIRE_VERTEX` | Line segment endpoint |

**Edge types** (`GeoEdgeType`):

| Type | Description |
|------|-------------|
| `SEGMENT` | Raw line segment between two WIRE_VERTEX nodes (from GeometryBuilder) |
| `CONDUCTING` | Wire segment upgraded from SEGMENT (by WireBuilder) |
| (Future) | CONTAINMENT, LOGICAL |

### 6.2 Build Pipeline

```
GeometryBuilder.build(doc)  ──  nodes (TAG, TEXT, CIRCLE, WIRE_VERTEX)
                                   + SEGMENT edges
          │
          ▼
      merge_close_nodes(tol=0.5)     ← merges WIRE_VERTEX into coincident TAG
          │
          ▼
      CIRCLE→WIRE_VERTEX edges       ← SEGMENT, 2-unit radius
          │
          ▼
WireBuilder.run()                    ← upgrades SEGMENT→CONDUCTING
          │
          ▼
CabinetBuilder.run()                 ← injects cabinet nodes
```

**`GeometryBuilder.build()`** (`cable_engine/electrical/geometry_graph.py`):

1. **Pass 1 — TAG nodes**: All `AttributeEntity` with `tag={'NO', 'ObjTerm.Name', 'WireSerial', ...}` → `GeoNodeType.TAG`. Retains `tag_name`, `tag_text`.
2. **Pass 1b — TEXT nodes**: All remaining `TextEntity`/`AttributeEntity` → `GeoNodeType.TEXT`.
3. **Pass 2 — CIRCLE nodes**: All `CircleGeometry` → `GeoNodeType.CIRCLE`.
4. **Pass 3 — WIRE_VERTEX + SEGMENT edges**: `_process_line` per-segment:
   - Each LWPOLYLINE segment evaluated independently (not as an atomic polyline).
   - **Horizontal segment**: `|dy| ≤ 3` and `dx > 2` → WIRE_VERTEX at both endpoints, SEGMENT edge.
   - **Vertical segment**: `|dx| ≤ 3` and `|dy| > 2` → WIRE_VERTEX at both endpoints, SEGMENT edge.
   - Whole-polyline Δy filter removed — enables vertical segments.
5. **`merge_close_nodes(0.5)`**: Merges WIRE_VERTEX into coincident TAG at same position. Merged node keeps TAG type but retains wire edges.
6. **CIRCLE→WIRE_VERTEX edges**: SEGMENT within 2.0 units. Condition checks "has any CONDUCTING/SEGMENT edge" (not node_type), so merged TAG nodes (with inherited wire edges) connect correctly.

### 6.3 Graph Traversal

Visitor pattern — `trace(start, visitor)` returns a semantic-free `GraphPath`:

```
GraphPath
├── nodes: list[int]
├── edges: list[int]
├── cost: float
├── stop_node: int
└── reason: TraceStopReason (DEAD_END / VISITOR_STOP / MAX_DEPTH / NO_PATH)
```

`GeometryGraph.trace()`:

1. BFS from `start_id`, calling `visitor.visit(node, depth)` at each node.
2. `visitor.visit()` returns `VisitDecision(stop=True/False)`.
3. When `stop=True`, `_build_path()` reconstructs the path from start to stop using parent pointers. Result includes node IDs, edge IDs, and accumulated geometric length (cost).
4. The path is semantic-free — business meaning is assigned by the consumer (Resolvers).

### 6.4 Wire Edge Operations

**`nearest_wire_node(x, y, tol)`**: Spatial scan for nodes with at least one SEGMENT or CONDUCTING edge. Picks closest within tolerance.

**`find_wire_near(x, y, tol, context_tags=None)`** — Edge-preferring wire lookup (V8 key improvement):

Primary strategy: **edge-based**. Scan all CONDUCTING/SEGMENT edges; for each with `|y_mean - y| ≤ tol`, compute `score = dy + x_outside * 0.1` (x_outside = distance from query x to edge's x-span). When `context_tags` is provided (list of `(x, y)` from WIRECODE/WIRETYPE positions), any edge whose x-span contains a tag within 40 units of its y_mean gets a 3.0 score bonus — this disambiguates cables whose WS is equidistant from left/right buses.

Returns the winning edge's endpoint (preferring non-CIRCLE endpoints for bus walking). **Always prefers** the edge result over the node-based fallback — the node fallback (`nearest_wire_node`) typically captures nearby vertical wires, which are not useful for bus traversal.

| Strategy | Method | Scene |
|----------|--------|-------|
| Edge-based (primary) | `score = dy + x_outside * 0.1 [-3.0 context bonus]` | Long horizontal bus; WS may be outside edge x-span |
| Node-based (fallback) | `nearest_wire_node` by Euclidean distance | No edge matched |

**`walk_to_endpoint(start_id, direction)`**:

Walks along degree-2 wire chain in x-increasing (`'right'`) or x-decreasing (`'left'`) direction. Stops at junctions (degree != 2 in wire edges) and returns that node.

**`wire_endpoint(start_id, direction)`**:

Simpler walk — follows the farthest neighbor in direction at each step. Used when junction handling is not needed.

### 6.5 Terminal Query (`ElectricalQuery`)

`ElectricalQuery` (`cable_engine/electrical/query.py`) is the high-level query interface:

```python
class ElectricalQuery:
    def find_terminal(wx, wy, side) → Optional[TerminalResult]
```

**Algorithm**:

1. `find_wire_near(wx, wy)` — locate the bus edge covering the query point.
2. `walk_to_endpoint(wire_id, side)` — follow degree-2 chain to the endpoint.
3. If endpoint is **CIRCLE** → resolve directly via `TerminalResolver`.
4. Otherwise, **direction-constrained DFS** (`_dfs_to_terminal`) — BFS outward following only x-increasing (right) or x-decreasing (left) moves (5-unit slop). Stops when a CIRCLE node is found.
5. Fallback: resolve at endpoint position.

Direction constraint `_dfs_to_terminal`:

| Side | Constraint |
|------|------------|
| `'left'` | `nb_x ≤ current_x + 5` (prefer x-decreasing) |
| `'right'` | `nb_x ≥ current_x - 5` (prefer x-increasing) |

### 6.6 Terminal Resolution (`TerminalResolver`)

`TerminalResolver.resolve_at(x, y)` (`cable_engine/electrical/resolvers/terminal.py`):

**Two-step nearest-neighbor search** (V8 improvement over V7's mixed search):

```
Step 1: Closest CIRCLE within 8 units
  ↓ (anchor_x, anchor_y)
Step 2: Closest NO/ObjTerm.Name tag within 12 units
  ↓ (cabinet bbox filter — excludes tags from neighboring cabinets)
Step 3: Cabinet containment lookup
  ↓
TerminalResult(number, x, y, cabinet)
```

Key details:
- **Circles**: Iterates all CIRCLE nodes in radius, picks shortest Euclidean distance (not `circles[0]`).
- **Tags**: Same — all candidate tags scored by distance to circle anchor.
- **Cabinet filter**: When the circle anchor falls inside a cabinet, tags outside that cabinet's bbox are excluded.
- **Radius**: Circle search 8 units → tag search 12 units (no longer hardcoded in both places).

### 6.7 Graph Size (D0202-31.dwg example)

| Metric | Value |
|--------|-------|
| Total nodes | 1255 |
| CIRCLE | 96 |
| WIRE_VERTEX | 399 |
| TAG | 606 |
| TEXT | 154 |
| Edges | 421 |
| LineGeometry in | 2095 (3426 segments after per-segment split) |
| Wire segments out | 274 (125 horizontal + 149 vertical) |

## 7. TopologyStage (V8 CircuitLoopAnalyzer)

`CircuitLoopAnalyzer.analyze()` now builds the GeometryGraph pipeline:

```python
def analyze(self, doc: Document) -> list[dict]:
    geo_graph = GeometryBuilder().build(doc)
    WireBuilder(geo_graph).run()
    CabinetBuilder(geo_graph).run()
    query = ElectricalQuery(geo_graph)

    # Pre-compute cable_type: match WIRECODE → nearest WIRETYPE
    cable_wire_type: dict[str, str] = {}
    for each WIRECODE ATTRIB with cable_id:
        for each WIRETYPE ATTRIB with manhattan_dist < 100:
            cable_wire_type[cable_id] = wire_type

    for each cable (grouped by WireSerial ATTRIB at wx, wy):
        wire_type = cable_wire_type.get(cid)
        for each core:
            left  = query.find_terminal(wx, wy, 'left', cable_id=cid)
            right = query.find_terminal(wx, wy, 'right', cable_id=cid)
            # dedup, cabinet detection, column text classification
            → records with wire_type
```

`find_terminal` now accepts `cable_id` — passed from the analyzer — which triggers `_get_context_tags(cable_id)` to look up WIRECODE/WIRETYPE tag positions. These are used as `context_tags` in `find_wire_near` to disambiguate buses for cables equidistant from left/right buses (e.g. 5071-506 at x=-349 between two bus columns).

`wire_type` is persisted to the `cable_info` table by `TopologyStage.run()` on the first conductor of each cable.

The old `_cabinet_path_trace()` is retained in source for reference but no longer called.

## 8. Cabinet Semantic Layer

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

## 9. Performance Optimizations

| Optimization | Impact | Mechanism |
|--------------|--------|-----------|
| `SpatialIndex` | O(N) → O(k) nearest-neighbor | 50-unit grid cell index |
| Edge-based `find_wire_near` | Long bus: O(N_edges) per cable | Single pass over edges; x-span + y-match |
| `context_tags` bonus | Disambiguates equidistant buses with zero extra graph passes | 3.0 score bonus for edges whose x-span contains WIRECODE/WIRETYPE |
| `merge_close_nodes` | Reduces node count ~20-30% | 0.5-unit tolerance, lower-ID wins |
| Batch SQLite writes | ~5-10% I/O reduction | `executemany` |
| Cabinet skip for non-loop | ~15% reduction on non-CIRCUIT_LOOP docs | Only for `CIRCUIT_LOOP` |

## 10. File Map

```
cable_engine/
├── cli.py                       # Entry: scan subcommand
├── classifier/
│   ├── composite.py             # CompositeClassifier
│   ├── keyword.py               # KeywordClassifier
│   ├── geometry.py              # GeometryClassifier
│   └── layout.py                # LayoutClassifier
├── electrical/                  # ← V8: GeometryGraph + Query
│   ├── __init__.py              # Public API exports
│   ├── geometry_graph.py        # GeometryGraph, GeoNode, GeoEdge,
│   │                            #   GeometryBuilder, SpatialIndex,
│   │                            #   Visitor, GraphPath
│   ├── graph_path.py            # GraphPath, TraceStopReason
│   ├── query.py                 # ElectricalQuery, _dfs_to_terminal
│   ├── builders/
│   │   ├── __init__.py
│   │   ├── wire.py              # WireBuilder (SEGMENT→CONDUCTING)
│   │   └── cabinet.py           # CabinetBuilder (cabinet nodes)
│   ├── resolvers/
│   │   ├── __init__.py
│   │   └── terminal.py          # TerminalResolver.resolve_at
│   └── visitors/
│       ├── __init__.py
│       └── cabinet_entry.py     # (legacy, kept for reference)
├── graph/
│   ├── builder.py               # TopologyStage + all analyzers
│   ├── cabinet.py               # CabinetRegionAnalyzer + CabinetGridIndex
│   ├── types.py                 # Legacy DocumentGraph (kept for reference)
│   └── spatial.py               # Legacy spatial index (kept for reference)
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

## 11. Critical Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Graph is pure geometry** | GraphPath, GeoNode, GeoEdge carry no Terminal/Cabinet/Device semantics. Business mapping is deferred to query-time Resolvers. |
| **Per-segment line processing** | Each LWPOLYLINE segment evaluated independently; removes whole-polyline Δy filter so vertical segments are preserved. |
| **Edge-based `find_wire_near` priority** | Always prefer the edge result when an edge matched. The node fallback (`nearest_wire_node`) typically captures nearby vertical wires that are not useful for bus traversal — the old `best_edge_dy ≤ node_dy` comparison compared edge y-mean to node y position (apples vs oranges). |
| **`context_tags` score bonus** | WIRECODE/WIRETYPE tag positions are passed to `find_wire_near` as `context_tags`. Edges whose x-span contains a tag (within 40y) get a 3.0 score penalty, biasing bus selection toward the cable's physically correct bus. Key fix for 5071-506/5072-503 at x=-349 equidistant from left/right buses. |
| **`context_tags` y-threshold=40** | The WIRECODE tag is placed ~30-40 units above the deepest bus line. Threshold must cover the full vertical span (y=-29 tag → y=-66 deepest bus = dy 37). |
| **Direction-constrained DFS** | From bus endpoint, follows only x-increasing (right) or x-decreasing (left) moves with 5-unit slop. Prevents crossing bus to wrong side cabinet. |
| **Closest-tag resolution** | Iterate all candidate NO/ObjTerm.Name tags, pick shortest Euclidean distance to CIRCLE center — not spatial-lookup order (which is grid/insertion order). |
| **`merge_close_nodes` by ID** | Lower-ID WIRE_VERTEX (created in Pass 3 after CIRCLE/TAG in Pass 2) merges into higher-ID TAG at same position. Merged node keeps TAG type but retains wire edges — CIRCLE connection step checks "has wire edges" not node_type. |
| **Cabinet bbox tag filter** | When anchor circle is inside a cabinet, tags outside that cabinet's bbox are excluded — prevents picking up tags from neighboring cabinets on the same drawing. |

## 12. Known Limitations

- **CircuitLoopAnalyzer only**: `cable_info` (wire_type) is only populated by `CircuitLoopAnalyzer` (回路图). `TerminalStripAnalyzer` and `CableScheduleAnalyzer` do not produce `wire_type`.
- **Equipment-cabinet right terminal**: Cables whose right side enters an equipment cabinet (no CIRCLE icon) resolve RIGHT=None. The terminal should be the EQUNAME/EQUCODE text tag instead.
- **DWG only**: PDF support deferred (no RasterizeStage / OcrStage yet).
- **Anonymous block text**: TEXT/ATTRIB inside anonymous blocks remain invisible.
