# cable_engine Architecture (V9)

## 1. System Overview

```
DWG file → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                    ↓
                                                  tools/cable_match_viewer/ (aiohttp)
                                                                    ↓
                                                             LayoutStage → panel_layout (SQLite)
```

A single `TopologyStage` orchestrates classification, cabinet analysis, and analyzer dispatch. V8 introduces a **GeometryGraph** — a pure graph layer — to replace the V7 procedural `_cabinet_path_trace()` algorithm. V8.2 introduces a **CandidatePool + DBSCAN** device detection pipeline in LayoutStage. V9 adds four layers on top: **Structure Analyzers** (replacing inline score functions), **TableParser** (injecting device business metadata), **SpatialGraph** (spatial relations between layout nodes), and **SemanticScore** (multi-evidence fusion for group semantic types).

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
| `PANEL_LAYOUT` (屏面布置图) | 48 | `LayoutStage` (layout tree) |
| `PANEL_POSITION` (屏位布置图) | — | `build_position_tree` (room → cells → table → crossref) |
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
                             ↓
                        LayoutStage.run()
                         └─ [PANEL_LAYOUT only] build_layout_tree()
                              └─ upsert_panel_layout (SQLite)
                              
                          tools/cable_match_viewer/
                           └─ GET /api/document/{hash}/layout
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

## 7. TopologyStage — Analyzers

### 7.1 CircuitLoopAnalyzer (回路图)

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

### 7.2 TerminalStripAnalyzer (端子排图)

`TerminalStripAnalyzer.analyze()` operates directly on the Document IR entities — no GeometryGraph involved. It is a V5-era algorithm that uses Extended Entity Data (EED) from the DWG to group line entities by cable, then extracts terminal-strip topology from the spatial layout.

**Algorithm flow**:

```
TerminalStripAnalyzer.analyze(doc)
│
├─ Phase 1: Entity separation
│   Separate LineGeometry → lines, TextEntity/AttributeEntity → texts
│   Find cabinet name from text containing "端子排图"
│
├─ Phase 2: Cable grouping via EED scan
│   For each line with EED matching _CABLE_ID_IN_EED pattern:
│     Classify as horizontal (|max_y - min_y| < 1.0)
│                  or vertical   (|max_x - min_x| < 1.0)
│     Group by cable_id → {cable_id: {horiz: [...], vert: [...]}}
│
└─ Phase 3: Per-cable analysis (_analyze_one_cable)
    For each cable_id:
    │
    ├─ Pick first horizontal line as anchor → h_y
    ├─ Find remote cabinet_name via rightmost EQUNAME ATTRIB near h_y
    │
    └─ For each vertical line (a cable column):
        ├─ Determine corner_y (which endpoint touches the horizontal)
        ├─ Determine end_y (the opposite endpoint)
        ├─ Skip if |dy| < MIN_VERTICAL_LENGTH (20 units) — too short
        ├─ Find conductor_no from NO ATTRIB at (vx ± 5, h_y ± 10)
        ├─ _collect_texts_along_vertical(vx, corner_y, end_y, texts)
        │   → texts within 3-unit x-corridor, sorted by distance from end_y
        ├─ _classify_column_text(column_texts)
        │   → (circuit_desc, terminal_no, loop_id, unknown_busi)
        ├─ If terminal_no found:
        │   _find_strip_name(terminal_x, terminal_y, texts)
        │   → nearest "1" marker left of terminal
        │   → nearest strip-name text left of that marker
        └─ Emit cable_topology record
```

**Key helper functions**:

| Function | Purpose |
|----------|---------|
| `_cable_id_from_eed(eed)` | Extract cable_id from first EED value matching `^[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}` |
| `_is_horizontal(entity)` | `max(ys) - min(ys) < 1.0` |
| `_is_vertical(entity)` | `max(xs) - min(xs) < 1.0` |
| `_find_conductor_no(vx, h_y, texts)` | NO ATTRIB at `(vx ± 5, h_y ± 10)` |
| `_find_remote_cabinet(h_y, texts)` | Rightmost EQUNAME ATTRIB near `h_y` |
| `_find_cabinet_name(texts)` | Text containing "端子排图", strip the prefix |
| `_collect_texts_along_vertical(vx, from_y, to_y, texts)` | All texts within 3-unit x-corridor, direction-ordered, excluding the anchor point (within 0.1 units of `to_y`) |
| `_classify_column_text(texts)` | Assign each text → `circuit_desc` / `terminal_no` / `loop_id` / `unknown_busi` by pattern matching |
| `_find_strip_name(tx, ty, texts)` | Nearest "1" marker left of terminal, then nearest pattern-matched strip name left of that |

**Column text classification** (`_classify_column_text`):

Each collected text along the vertical column is classified by priority:

| Priority | Rule | Classification |
|----------|------|---------------|
| 1 | `label.isdigit()` | `terminal_no` (first numeric wins) |
| 2 | Matches `^[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}$` or `^[A-Za-z]\d{1,4}$` or contains `-` + len ≥ 4 | `loop_id` (first) or `circuit_desc` (second) |
| 3 | Contains alphabetic characters | `circuit_desc` (first) or `unknown_busi` (second) |
| 4 | Fallthrough | `unknown_busi` (first) or `circuit_desc` (fallback) |

**Strip name detection** (`_find_strip_name`):

1. Find all `TextEntity` with text exactly `"1"` within 40 units y of the terminal.
2. Pick the closest "1" marker strictly to the left (`x < terminal_x - 1.0`).
3. If none found (handles `terminal_no=1` where the terminal IS the strip start), use the closest "1" by absolute distance.
4. From the "1" marker, search leftward (`x < marker_x - 3`) within 38 units y for text matching strip patterns (`^(\d{1,2}[A-Za-z]{1,4})$`, `^([A-Za-z]{1,2}\d{1,4})$`, `^([A-Za-z]{1,4})$`, `^(\+?[A-Za-z]{1,3}\d{1,4})$`, `^(\d{1,2}-[A-Za-z]{1,4})$`).
5. Pick the closest matching text → `strip_name`.

**Record structure** (`cable_topology` schema):

| Field | Source | Example |
|-------|--------|---------|
| `cable_id` | From EED | `GY6-136` |
| `conductor_no` | NO ATTRIB at junction | 1 |
| `strip_name` | From "1" marker → left text | `1D` |
| `terminal_no` | First numeric in column texts | 2 |
| `terminal_no_remote` | Always `None` | — |
| `cabinet_name` | Text containing "端子排图" | `1号继电器柜` |
| `cabinet_name_remote` | Rightmost EQUNAME near h_y | `X4` |
| `circuit_desc` | Classified from column texts | `直流电源+` |
| `loop_id` | Classified from column texts | `+KZ1` |
| `source_type` | Always `terminal_strip` | `terminal_strip` |

### 7.3 CableScheduleAnalyzer (电缆清册)

A stub analyzer for cable schedule documents (`CABLE_SCHEDULE` type). For each cable entry found in the schedule, it emits a single `cable_topology` record with `cable_id`, `source_type='cable_schedule'`, and empty terminal/loop fields. Used by the viewer for a browsable cable index.

### 7.4 LayoutStage — Panel Layout Tree (屏面布置图) (V9)

`LayoutStage` (`cable_engine/layout/stage.py`) runs **after** `TopologyStage` and only for `PANEL_LAYOUT` classified documents. It builds a hierarchical **LayoutTree** — a spatial containment tree capturing the physical structure of a panel face drawing — and persists it as JSON to the `panel_layout` table.

```
LayoutStage.run(ctx)
  │
  └─ if ctx.classification.primary == PANEL_LAYOUT
       └─ build_layout_tree(doc)
              ↓
           LayoutTree
           (JSON → SQLite panel_layout)
```

#### 7.4.1 LayoutTree Data Structures

```python
@dataclass
class LayoutTree:
    roots: list[LayoutNode]  # CABINET and TABLE root nodes

@dataclass
class LayoutNode:
    id: str
    node_type: LayoutNodeType
    bbox: BBox             # (x, y, w, h) — DWG bottom-left origin
    name: str
    children: list[LayoutNode]
    data: dict             # type-specific metadata (table rows, device names, etc.)
class LayoutNodeType(Enum):
    SHEET       = 'SHEET'        # Drawing boundary (reserved)
    CABINET     = 'CABINET'      # Panel cabinet (front or back face)
    PANEL_AREA  = 'PANEL_AREA'   # Device-mounting area (may form sub-groups)
    DEVICE      = 'DEVICE'       # Individual device symbol (rect + text)
    TEXT_BLOCK  = 'TEXT_BLOCK'   # Text cluster (reserved)
    TABLE       = 'TABLE'        # (reserved — no longer generated)
    TITLE_BLOCK = 'TITLE_BLOCK'  # Title block (reserved)
```

**Node tree structure** (V8.2 — includes GROUP nodes and front/back face labels):

```

CABINET "1号1000kV继电器小室高抗电能表柜" (face=front, named via header rect)
  ├── DEVICE "M1 / DTZ178 / 张北I线 / 电抗器 / 本期"
  ├── DEVICE "M2 / DTZ178 / 张北II线 / 电抗器 / 本期"
  ├── DEVICE "M3 / DTZ178 / 预留1"
  └── DEVICE "M4 / DTZ178 / 预留2"

CABINET "" (face=back, unnamed, no header rect)
  ├── PANEL_AREA "" (horizontal divider area)
  │   ├── GROUP [VERTICAL_COLUMN] "TERMINAL_COLUMN"
  │   │   ├── DEVICE "2D"
  │   │   ├── DEVICE "4D"
  │   │   └── DEVICE "6D"
  │   └── GROUP [VERTICAL_COLUMN] "TERMINAL_COLUMN"
  │       ├── DEVICE "1D"
  │       ├── DEVICE "3D"
  │       └── DEVICE "5D"
  └── DEVICE "GZ11"          (standalone, ungrouped)

```
#### 7.4.2 Detection Algorithm (`build_layout_tree`) — V8.2

The detector (`cable_engine/layout/detector.py`) uses the V8.2 **CandidatePool + DBSCAN** pipeline:

```

Document IR entities
    │
    ▼
Step 1 — Rectangle detection (detect_rectangles)
    │   LWPOLYLINE (4-5 pts)    → axis-aligned rect
    │   4× LINE segments (closed chain) → axis-aligned rect
    │   Output: list[DetectedRect]
    │
    ▼
Step 2 — Long line detection (detect_long_lines, min_length=50.0)
    │   Horizontal: |dy| < 2.0, length ≥ 50u
    │   Vertical:   |dx| < 2.0, length ≥ 50u
    │   Output: list[LongLine] (verts, hors)
    │
    ▼
Step 3 — Cabinet detection (detect_cabinets)
    │   A. Rectangle-based: area > 10,000 u², aspect-ratio 1.5-5.0
    │   B. Paired-vertical: dx 140-240, overlap > 50%
    │   C. Merge: rect-based > paired-vertical when overlapping
    │   Page-border: area > 90% of drawing → rejected
    │   Output: list[LayoutNode] (type=CABINET)
    │
    ▼
Step 4 — Front/back identification (_identify_front_back)
    │   Finds "正面"/"背面" text below each cabinet bottom edge
    │   (within 200u, within 60% of width). Falls back to y-sort.
    │   Stores face in cab.data.face: 'front' / 'back'
    │
    ▼
Step 5 — Per-cabinet interior + device pipeline
    │
    ├─ 5a. Interior (detect_cabinet_interior)
    │     Header rects (≥80% width, ≤15u high)
    │     Device area (≥50% width, ≥40% height)
    │
    ├─ 5b. Area detection (detect_areas_v2)
    │     Interior rect → PANEL_AREA, or horizontal dividers
    │
    └─ 5c. Device detection & grouping (_apply_grouping_v2)
         For each AREA (or whole CABINET):
          │
          ├─ build_device_candidates
          │   5-tier candidate generation + CandidatePool dedup
          │
          │   Score hierarchy:
          │     detect_closed_rects    → 0.95  (closed rectangles)
          │     detect_spine_devices   → 0.75  (open-rect, spine matching)
          │     detect_U_shapes        → 0.70  (3 segments, parallel ends)
          │     detect_L_shapes        → 0.50  (2 segments, 90° end-joined)
          │     detect_text_devices    → 0.40  (text-only fallback)
          │
          │   Dedup: CandidatePool retains higher-scoring candidate
          │     when overlap > 0.2 (lower score discarded)
          │
          ├─ TextAssociator.associate_devices
          │   Topmost text = name, rest = description
          │
          ├─ DBSCANClusterer (eps=30, min_samples=2)
          │   Feature vector: [cx, cy, w*0.1, h*0.1]
          │   → DeviceGroup[] (VERTICAL_COLUMN / HORIZONTAL_ROW /
          │                     GRID / FREEFORM)
          │
          └─ TextAssociator.associate_groups
                Position labels (left/right) for groups
    
Step 6 — Semantic annotation (_annotate_groups)
    SemanticScoreEngine fuses 5 evidence sources:
      LayoutShapeEvidence  ← group_type enum
      NamePatternEvidence  ← device name prefixes
      DeviceAttrEvidence   ← child node data['attributes']
      TableInfoEvidence    ← table_info metadata (P1)
      SpatialEvidence      ← SpatialGraph (stub)
    → GroupSemantic {type, confidence, evidence}
    │
    ▼
    LayoutTree { roots: [...] }
```

#### 7.4.3 Key Helper Functions (V8.2)

| Function | Module | Purpose |
|----------|--------|---------|
| `detect_rectangles(doc)` | `primitives/rectangle.py` | Axis-aligned rects from LINE/POLYLINE |
| `detect_long_lines(doc, min_length)` | `primitives/line.py` | Classify lines as H/V |
| `detect_cabinets(doc, rects, verts, hors)` | `detectors/cabinet.py` | Cabinet boundary candidates |
| `detect_cabinet_interior(cab, rects)` | `detectors/area.py` | Header rects + device area inside cabinet |
| `detect_areas_v2(doc, cab, hors, interior)` | `detectors/area.py` | Device mounting area creation |
| `_identify_front_back(cabinets, doc)` | `detector.py` | Text-based front/back matching |
| `build_device_candidates(doc, bbox)` | `candidate.py` | 5-tier candidate pipeline orchestrator |
| `detect_closed_rects(doc, bbox)` | `candidate.py` | Closed-rect devices (score 0.95) |
| `detect_spine_devices(doc, bbox)` | `candidate.py` | Spine-matched open-rect devices (0.75) |
| `detect_U_shapes(doc, bbox)` | `candidate.py` | U-shape device detection (0.70) |
| `detect_L_shapes(doc, bbox)` | `candidate.py` | L-shape device detection (0.50) |
| `detect_text_devices(doc, bbox)` | `candidate.py` | Text-only fallback (0.40) |
| `CandidatePool` | `candidate.py` | Multi-source candidate dedup |
| `TextAssociator` | `associator.py` | Text association (name/desc + group labels) |
| `DBSCANClusterer` | `clustering.py` | DBSCAN clustering (eps=30, min_samples=2; post-class delegates to structure/*) |
| `ColumnAnalyzer` | `structure/column.py` | Column scoring (x-align + size consistency + spacing) |
| `RowAnalyzer` | `structure/row.py` | Row scoring (y-align + size consistency + spacing) |
| `GridAnalyzer` | `structure/grid.py` | Grid scoring (cols×rows fill) |
| `detect_table_regions` | `table/detector.py` | Rectangle-based equipment table detection |
| `parse_table_at` | `table/parser.py` | Table row/column/header parsing |
| `match_to_devices` | `table/matcher.py` | TableRow → DeviceCandidate matching |
| `lift` | `spatial/bridge.py` | LayoutTree → SpatialGraph lift |

#### 7.4.4 Storage

The layout tree is serialized to JSON (`LayoutNode` dataclass → `asdict()` → JSON) and stored in the `panel_layout` table:

| Column | Type | Description |
|--------|------|-------------|
| `document_hash` | TEXT | Source DWG fingerprint (PK) |
| `layout_json` | TEXT | LayoutTree as JSON |
| `created_at` | TEXT | ISO 8601 timestamp |

```
Store methods:
  upsert_panel_layout(hash, layout_tree)  → INSERT OR REPLACE
  get_panel_layout(hash)                  → LayoutTree (deserialized)
  delete_panel_layout(hash)               → DELETE
  has_panel_layout(hash)                  → bool
```

#### 7.4.5 Viewer Integration

The layout tree is served via the viewer's REST API and rendered client-side as a cabinet-focused tree view:

```
GET /api/document/{hash}/layout
  → JSON { roots: [...] }

renderLayoutTree(layout)  → HTML
  └─ CABINET tree view:
       Front cabinet → cabinet name (from cab.data.face)
       Back cabinet  → "背面"
       Each PANEL_AREA → nested with orange label
       GROUP → purple border, semantic label + position
        DEVICE → label with name
```

### 7.5 V9 TableParser — Equipment Table Parser

See also the dedicated design doc [`docs/table_parsing_algorithm.md`](table_parsing_algorithm.md) — this section gives a high-level summary.

The `layout/table/` package extracts structured row data from **equipment tables (设备表 / 材料表)** on the right side of PANEL_LAYOUT drawings, and injects business metadata (model, description, quantity) into matching DeviceCandidates.

#### 7.5.1 Data Model

```
TableArea
├── bbox: BBox                          — Table region
├── rows: list[TableRow]                — Header + data rows
├── header_row: Optional[TableRow]      — Header row (Chinese keywords)
├── header_columns: list[str]           — Header cell texts
├── name_column_index: int              — Device name column
├── model_column_index: int             — Model number column
├── desc_column_index: int              — Description column
└── qty_column_index: int               — Quantity column

TableRow
├── cells: list[TableCell]              — Cells by col_index
├── y: float                            — CAD Y coordinate
└── header: bool

TableCell
├── text: str
├── x: float
├── col_index: int
└── row_index: int
```

#### 7.5.2 Pipeline

```
detect_table_regions(doc, container)
  │   Searches for rects ≥60w×80h with ≥4 texts inside container
  │   → list[BBox]
  ▼
parse_table_at(doc, table_bbox)
  │   Collects texts inside rect
  │   → Y-cluster (_ROW_TOL=3.0) into rows
  │   → Sort each row by X
  │   → Detect header row by Chinese keywords (序号/名称/型号/说明/数量)
  │   → Map column roles (name/model/desc/qty/index/position)
  │   → Optional[TableArea]
  ▼
match_to_devices(table, candidates)
  │   For each name-column text in data rows:
  │     Find candidate with matching name
  │     Inject candidate.features['table_info'] = {model, description, qty}
  │   → match_count
```

#### 7.5.3 Integration

`_detect_equipment_table(doc, cab)` called per-cabinet in `build_layout_tree`:
- Searches 200u to the right of the cabinet bbox for table rects
- First valid table (`name_column_index ≥ 0`) is passed to `_apply_grouping_v2`
- `match_to_devices` runs **after** `associate_devices`, **before** DBSCAN
- Makes `features['table_info']` available to GroupSemanticResolver and downstream stages

### 7.6 V9 SpatialGraph — Spatial Relations Graph

The `layout/spatial/` package builds a flat **spatial relations graph** parallel to the LayoutTree hierarchy, capturing geometric relationships between layout nodes. Strictly separate from GeometryGraph (`electrical/`).

#### 7.6.1 Data Model

```
SpatialGraph
├── nodes: dict[str, SpatialNode]       — Wrapping LayoutNode
└── edges: list[SpatialEdge]            — Spatial relations

SpatialNode
├── node_id: str                        → LayoutNode.id
├── node_type: str                      → LayoutNodeType value
├── bbox: BBox
├── name: str
└── data: dict

SpatialEdge
├── source_id / target_id
├── relation: SpatialRelation
├── distance: float
└── confidence: float
```

**Relation types** (`SpatialRelation`):

| Relation | Meaning | Criteria |
|----------|---------|----------|
| `CONTAINS` | Containment | LayoutTree parent→child |
| `LEFT_OF` | Left | vertical overlap ≥30%, A left of B |
| `RIGHT_OF` | Right | vertical overlap ≥30%, A right of B |
| `ABOVE` | Above | horizontal overlap ≥30%, A above B |
| `BELOW` | Below | horizontal overlap ≥30%, A below B |
| `ALIGNED_VERT` | Vertical alignment | cx diff ≤ 8u, same column |
| `ALIGNED_HORZ` | Horizontal alignment | cy diff ≤ 8u, same row |
| `NEAR` | Proximity | centroid distance ≤ 40u, no other relation |

#### 7.6.2 Bridge (`lift(tree)`)

```
lift(LayoutTree) → SpatialGraph
  │
  ├─ 1. Flatten tree (BFS)
  │     Each LayoutNode → SpatialNode
  │
  ├─ 2. CONTAINS edges
  │     Every parent → child
  │
  ├─ 3. Sibling spatial relations
  │     Sibling pairs under same parent:
  │       - Horizontal overlap ≥30% → ABOVE / BELOW
  │       - Vertical overlap ≥30% → LEFT_OF / RIGHT_OF
  │
  └─ 4. Cabinet-internal device relations
       All DEVICE pairs under same CABINET:
         - |cx diff| ≤ 8 → ALIGNED_VERT
         - |cy diff| ≤ 8 → ALIGNED_HORZ
         - centroid dist ≤ 40 → NEAR
```

#### 7.6.3 Query API

```python
graph.query_bbox(bbox: BBox) → list[SpatialNode]    # Intersecting nodes
graph.query_near((cx, cy), radius) → list[SpatialNode]  # Proximity
graph.neighbors(node_id) → list[(target_id, edge)]   # Adjacency
graph.relations_of(node_id, relation) → list[(target_id, edge)]  # Filtered
```

### 7.8 P4 SemanticScore — Multi-Evidence Fusion Engine

The `semantics/` package provides a **pluggable evidence fusion engine** that replaces the prefix-only `GroupSemanticResolver` with a configurable multi-source scorer.

#### 7.8.1 Architecture

```
SemanticScoreEngine.fuse(group_node)
  ├─ LayoutShapeEvidence   — LayoutGroupType (0.20/0.10/0.20)
  ├─ NamePatternEvidence   — Device name prefix match (weight × ratio)
  ├─ DeviceAttrEvidence    — Child data['attributes']['category'] (≥50% ratio)
  ├─ TableInfoEvidence     — table_info description keywords + model prefix
  └─ SpatialEvidence       — SpatialGraph (stub, ready for future extension)
      │
      ▼  weighted sum → pick max
   GroupSemantic {type, confidence, evidence_trail}
```

Each `EvidenceSource` returns `{semantic_type: score_contribution}`. The engine fuses by weighted sum, selects the best type, and records an evidence trail with all source contributions for debugging.

#### 7.8.2 Evidence Sources

| Source | Signal | Contribution |
|--------|--------|-------------|
| `LayoutShapeEvidence` | VERTICAL_COLUMN → TERMINAL_COLUMN 0.20; GRID → METER_GRID 0.20; HORIZONTAL_ROW → DEVICE_PANEL 0.10 | Fixed additive |
| `NamePatternEvidence` | Device name prefix match (e.g. `2D`/`4D`/`6D` → TERMINAL_COLUMN 0.40) | `weight × (matched/total)` |
| `DeviceAttrEvidence` | Child node `data['attributes']['category']` when ≥50% share a category | 0.20 per matching category |
| `TableInfoEvidence` | table_info description keywords (电能表/继电器/端子) + model prefix (DTZ/DK) | 0.25 description + 0.15 model |
| `SpatialEvidence` | SpatialGraph adjacency (not yet wired — returns `{}`) | — |

#### 7.8.3 Integration

`GroupSemanticResolver` is now a thin wrapper around `SemanticScoreEngine`. The `annotate_groups(tree)` function still works unchanged — backward compatible. Custom sources can be injected:

```python
from cable_engine.layout.semantics import SemanticScoreEngine, NamePatternEvidence

engine = SemanticScoreEngine(sources=[NamePatternEvidence()])
engine.fuse_tree(group_node)
```

#### 7.8.4 Key Files

- `semantics/evidence.py` — `EvidenceSource` base class + 5 concrete implementations
- `semantics/fusion.py` — `SemanticScoreEngine` + `GroupSemantic` dataclass
- `semantics/group_type.py` — `GroupSemanticResolver` (now a thin wrapper)
- `semantics/test_semantic_score.py` — 28 tests for evidence sources + fusion

### 7.9 V9 PANEL_POSITION — Panel Position Tree (屏位布置图)

The `layout/position/` package extracts a **position tree** from PANEL_POSITION drawings — drawings that lay out cabinet/frame F‑number positions (屏位) in a grid and reference a usage table (屏屏用途一览表) mapping each F‑number to the cabinet model assigned there.

#### 7.9.1 Pipeline

```
build_position_tree(doc)
  │
  ├─ 1. detect_room(doc)
  │      Long horizontals (spanning ≥60% of F‑text X‑range) define room boundary.
  │      Supports paired segments (left + right with gap) via union X‑range per Y.
  │      F‑number texts hint the approximate X‑range; multiple clusters → pick largest.
  │      → Room (bounding box)
  │
  ├─ 2. detect_cells(room)
  │      Find F‑number texts (pattern: r'^\d+[CF]$') inside room bbox.
  │      Cluster into rows by Y (_ROW_TOL=8.0).
  │      Sort each row by X, assign label.
  │      Detect column groups (gap ≥ 1.5× median → new group).
  │      → list[PositionRow] with row_index, col_index, group_index per cell
  │
  ├─ 3. detect_table_regions (reuses layout/table/detector.py)
  │      Find rects ≥60w×80h with ≥4 texts. Bbox widened (ex‑200, w=350, h=450).
  │      Filter by header texts containing 屏屏/用途/一览/编号/名称/数量/备注.
  │
  ├─ 4. parse_usage_table(doc, table_bbox)
  │      Label‑centered anchoring: search Y‑neighbourhood around each F‑number label
  │      for sibling cells. Index‑based role mapping — within each column half, sort
  │      cells by X → map relative index to header role at same position.
  │      → TableArea with 屏号/名称/数量/备注 columns
  │
  └─ 5. cross_reference(cells, table_rows)
        Match cells by F‑number text → inject equipment from table row.
        → LayoutTree(ROOM → POSITION_ROW → POSITION_CELL)
```

#### 7.9.2 Data Model

```
PositionCell
├── label: str                       — Screen number (e.g. "1F", "12C")
├── bbox: BBox                       — Cell boundary
├── row_index: int
├── col_index: int                   — Resets per group
├── group_index: int                 — Column group within row
└── equipment: Optional[str]         — From usage table crossref

PositionRow
├── cells: list[PositionCell]
├── y: float
└── row_index: int
```

#### 7.9.3 Table Parsing Strategy

Two‑column layout (D0201‑05 style):
```
[屏号 | 名称 | 数量 | 备注] [屏号 | 名称 | 数量 | 备注]
```
Headers detected by keyword; column halves identified by X‑gap. Row cells sorted by X, left half gets index 0‑3, right half 4‑7. Roles assigned by matching relative index in each half.

Single‑column layout (D0201‑07 when left headers inside bbox):
```
[屏号 | 名称 | 数量 | 备注 | 屏号 | 名称 | 数量 | 备注]
```
Two‑column split detected via natural text gap between column 4 and 5.

#### 7.9.4 Viewer Integration

Served via `/api/document/{hash}/position` in `cable_match_viewer`. Client‑side `renderPositionTree()` renders a grouped tree view:

```
ROOM (blue label)
├─ POSITION_ROW
│  ├─ "第 1 列组" header (gray)
│  │  ├─ 1F — equipment_name (cell box, 150px, ellipsis)
│  │  ├─ 2F — ...
│  └─ "第 2 列组" header
│     ├─ 12F — ...
```

Each cell box has `max‑width:150px`, `text‑overflow:ellipsis`, and a `title` attribute showing `排X列Y组Z`.

#### 7.9.5 Edge Cases Handled

- **Paired horizontal segments**: D0227‑03 draws grid horizontals as two segments (left + right with gap) — `detect_room` unions X‑ranges per Y level before checking the 60% span filter.
- **Variable column layouts**: `_detect_groups()` splits cells by X gap ≥ 1.5× median gap; `col_index` resets per group. Handles both single‑column and split‑column grids.
- **Label‑centered anchoring**: CAD text within a row may scatter vertically — anchoring on the screen‑number label and searching ±6px captures sibling cells that Y‑bucketing would lose.
- **Non‑usage tables**: D0227‑03's right‑side table is 设备材料表 (equipment/material list, columns 名称/单位/数量/备注, no 屏号 column) — `cross_reference` returns 0 rows but position cells remain available.

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
├── layout/                      # ← PANEL_LAYOUT LayoutTree (V9)
│   ├── __init__.py              # Public API exports
│   ├── types.py                 # LayoutTree, LayoutNode, LayoutNodeType
│   ├── model.py                 # LayoutNode, LayoutGroupType (canonical)
│   ├── detector.py              # V9 pipeline + _apply_grouping_v2
│   ├── stage.py                 # LayoutStage (post-TopologyStage)
│   ├── candidate.py             # DeviceCandidate + 5-tier generator + CandidatePool
│   ├── associator.py            # TextAssociator (name/desc + group labels)
│   ├── clustering.py            # DBSCANClusterer (post-class delegates to structure/*)
│   ├── cabinet.py               # PhysicalCabinet wrapper
│   ├── test_detector.py         # 30 unit tests
│   ├── demo.py                  # CLI demo
│   ├── detectors/               # Spatial detection modules
│   │   ├── __init__.py
│   │   ├── cabinet.py           # detect_cabinets, paired-vertical, merge
│   │   ├── area.py              # area detection + interior analysis
│   │   └── device.py            # legacy (unused, kept for reference)
│   ├── primitives/              # Primitive detectors
│   │   ├── __init__.py
│   │   ├── bbox.py              # BBox utilities
│   │   ├── line.py              # detect_long_lines / LongLine
│   │   └── rectangle.py         # DetectedRect / detect_rectangles
│   ├── structure/               # ← V9 Spatial-structure analyzers
│   │   ├── __init__.py
│   │   ├── column.py            # ColumnAnalyzer (VERTICAL_COLUMN scoring)
│   │   ├── row.py               # RowAnalyzer (HORIZONTAL_ROW scoring)
│   │   └── grid.py              # GridAnalyzer (GRID scoring)
│   ├── table/                   # ← V9 Equipment table parser
│   │   ├── __init__.py
│   │   ├── model.py             # TableArea / TableRow / TableCell
│   │   ├── detector.py          # detect_table_regions
│   │   ├── parser.py            # parse_table_at (text clustering + header detection)
│   │   └── matcher.py           # match_to_devices (candidate injection)
│   ├── spatial/                 # ← V9 Spatial graph
│   │   ├── __init__.py
│   │   ├── model.py             # SpatialNode / SpatialEdge / SpatialGraph
│   │   └── bridge.py            # lift(tree) → SpatialGraph
│   └── semantics/               # V9 semantic annotation (P4)
│       ├── __init__.py          # Public exports
│       ├── group_type.py        # GroupSemanticResolver (thin wrapper)
│       ├── device_type.py       # Device type classification
│       ├── evidence.py          # EvidenceSource base + 5 concrete sources
│       ├── fusion.py            # SemanticScoreEngine fusion engine
│       └── test_semantic_score.py  # 28 tests
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
| **LayoutStage after TopologyStage** | Layout tree needs classification decision first. Placing LayoutStage second avoids re-classifying in the detection pipeline. |
| **Area-based page-border rejection** | Page border rejection uses area ratio (>90%) not dimension-based (70% of max dimension). Prevents filtering of legitimate large cabinets that span most of the drawing. |
| **Cabinet aspect-ratio filter (1.5-5.0)** | Cabinet faces are tall/narrow (h/w≈3:1); rejects page borders (h/w≈0.7) and wide inner frames. |
| **Rect-based cabinet preferred over paired-vertical** | Merge picks rect-based when both sources produce overlapping candidates; paired-vertical is only fallback for open-face cabinets. |
| **CandidatePool score hierarchy** | closed_rect(0.95) > spine(0.75) > U(0.70) > L(0.50) > text(0.40). Dedup discards lower-score on overlap >0.2. Spine beats U so individual open-rects replace merged U-shapes. |
| **Spine device detection** | Replaces legacy `_detect_open_rect_devices`. Pairs short horizontals sharing a vertical spine. Score 0.75. Handles 6u-wide back-face devices (2D-12D). |
| **DBSCAN feature vector with scaled dimensions** | `[cx, cy, w*0.1, h*0.1]` — w/h scaled by 0.1x to prevent size dominance. Separates adjacent device columns with different widths. |
| **Post-classification stricter than DBSCAN** | DBSCAN clusters broadly (eps=30), post-class filters (w/h diff ≤8/6u). Label texts (e.g. "左侧" w=20) with spine devices (w=6) in same cluster → size mismatch → FREEFORM fallback. |
| **Text-based front/back matching** | Finds "正面"/"背面" text below cabinet bottom edge (dx ≤ 60% width, dy ≤ 200u). Replaces y-sort from V8.0. Fixes D0206-20 where both halves share the same y. |
| **No document-level table detection** | Removed. Focus is on front/back cabinet faces and their device rectangles. |
| **V8.2 GROUP node between AREA and DEVICE** | GROUP nodes carry `group_type` (spatial pattern) and semantic types. Plug into existing CABINET/AREA/DEVICE hierarchy via `parent.children`. |
| **DBSCAN replaces sweep-based clustering** | Legacy sweep-based clustering (`grouping/`) deleted. DBSCAN(eps=30) eliminates explicit GRID → COLUMN → ROW phase ordering. Post-classification assigns pattern type. |
| **GroupSemanticResolver scoring** | Each signal contributes weighted score; highest wins. Evidence list traces decision provenance. |
| **V9: Structure Analyzers replace inline score functions** | `_score_column`, `_score_row`, `_check_grid` extracted from `clustering.py` into independent classes (ColumnAnalyzer/RowAnalyzer/GridAnalyzer) with a uniform `analyze()` interface. Post-class order: GRID→COLUMN→ROW→FREEFORM. New analyzers plug in without touching clustering.py. |
| **V9: DBSCAN downgraded to proximity finder** | DBSCAN answers "which devices are near each other"; structure analyzers answer "what spatial pattern". Responsibilities separated. |
| **V9: TableParser injects business metadata** | Equipment table rows matched by name column → DeviceCandidate.features['table_info']. Runs after associate_devices, before DBSCAN so semantic resolvers can leverage model/description info. |
| **V9: SpatialGraph independent from GeometryGraph** | GeometryGraph (electrical/) operates on raw geometry entities (lines, circles, TAGs). SpatialGraph operates on layout nodes (CABINET/GROUP/DEVICE). The two graphs are strictly separate. |
| **V9: Table detection parametric** | Min table size 60w×80h, min 4 texts, search 200u right of cabinet. Header detected by Chinese keyword regex, column roles by keyword classification. |
| **P4: EvidenceSource pluggable interface** | Each evidence source implements `score(node) → {type: contrib}`. New sources can be registered without modifying the engine. |
| **P4: Fusion by weighted sum, not max-vote** | Weighted sum allows every signal to contribute proportionally. The evidence trail records ALL sources (including inactive ones marked `—`) for debuggability. |
| **P4: TableInfoEvidence consumes P1 output** | The first cross-stage evidence source: uses `table_info` injected by P1 TableParser's `match_to_devices`. Description keyword match + model prefix match provide 0.25 + 0.15 contributions. |
| **P4: SpatialEvidence stub ready for P2 hookup** | The SpatialGraph-based evidence source exists but returns `{}` until wired with an actual SpatialGraph instance. No tree changes needed when enabling it. |

## 12. Known Limitations

### CircuitLoop / General

- **CircuitLoopAnalyzer only**: `cable_info` (wire_type) is only populated by `CircuitLoopAnalyzer` (回路图). `TerminalStripAnalyzer` and `CableScheduleAnalyzer` do not produce `wire_type`.
- **Equipment-cabinet right terminal**: Cables whose right side enters an equipment cabinet (no CIRCLE icon) resolve RIGHT=None. The terminal should be the EQUNAME/EQUCODE text tag instead.
- **DWG only**: PDF support deferred (no RasterizeStage / OcrStage yet).
- **Anonymous block text**: TEXT/ATTRIB inside anonymous blocks remain invisible.

### Panel Layout (PANEL_LAYOUT) — V9

- **Cabinet detection fragile**: The paired-vertical approach (dx 140-240) is empirical. May miss short-vertical cabinets (e.g. D0206-20 front: verticals 65u < 100u threshold).
- **Device name completeness**: TextAssociator picks topmost text as name, rest as description. Unrelated text above a device produces noisy names.
- **Back-face device naming**: Spine devices require text strictly inside bbox. Floating-point edge cases may still discard valid devices (fixed for common cases via `_texts_in_bbox`).
- **DBSCAN label text interference**: Group labels (e.g. "左侧" w=20u) in same cluster as spine devices (w=6u) → consistency check fails → FREEFORM fallback. Mitigated by `_classify_group` sorted cy descending.
- **No sensor vs terminal discrimination**: `detect_closed_rects` finds all closed rectangles but does not distinguish sensors (temp/humidity) from terminal devices. Device-level semantic classification needed.
- **PANEL_POSITION missing drawings**: Some drawings (e.g. D0227-03) use a different table format (设备材料表 without 屏号 column) — the table parser skips them but grid cell data is still available.
- **No cross-validation**: Layout tree produced independently per drawing.
- **GRID post-class requires full fill**: GridAnalyzer requires device count to exactly equal `cols × rows`. Partial grids (e.g. 2×3 with 5 devices) are missed.
- **FREEFORM has no pattern scoring**: DBSCAN noise points become standalone DEVICE nodes; remaining ungrouped devices are not clustered.
- **P4 SemanticScore still prefix-based for names**: `NamePatternEvidence` inherits the same prefix pattern approach as the old `GroupSemanticResolver`. No suffix or regex matching.
- **detectors/device.py legacy**: Old `detect_devices`, `_detect_open_rect_devices`, `_merge_devices` remain in `detectors/device.py` but are no longer called. Pending cleanup.
- **Table detection requires rect border**: `detect_table_regions` depends on `detect_rectangles` to find closed-rect table borders. Tables without rectangular boundaries (e.g. text-only "invisible tables") are not detected.
- **SpatialGraph layout-node-only**: SpatialGraph currently derives relations only from LayoutTree nodes, not raw geometry entities (lines, circles). Fine-grained geometric queries requiring GeometryGraph are not supported.
- **O(N²) device pair enumeration not optimized**: `_add_device_relations` enumerates all device pairs within a cabinet. Large cabinets (>45 devices, >1000 pairs) risk performance. Guarded by `_MAX_DEVICE_PAIRS=2000`.
