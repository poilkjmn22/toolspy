# cable_engine Architecture (V6)

## 1. System Overview

```
DWG file → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                            ↓
                                                          tools/cable_match_viewer/ (aiohttp)
```

Two document types handled by per-analyzer dispatch in `TopologyStage`:

| Type | Detected by | Analyzer | Schema |
|------|-------------|----------|--------|
| 回路图 | "回路图" in ATTRIB | `CircuitLoopAnalyzer` | cable_topology (source_type=circuit_loop) |
| 端子排图 | "端子排图" in ATTRIB | `TerminalStripAnalyzer` | cable_topology (source_type=terminal_strip) |

## 2. Pipeline

```
Loader → Document IR → TopologyStage → cable_topology
```

`cli.py:cli` builds a single `Pipeline([TopologyStage(store)])` — no rasterize, OCR, match, fusion, or rule engine stages. Those are V4 legacy.

## 3. Document IR

Source-agnostic entity types in `cable_engine/ir/`:

| Entity | Source | Fields |
|--------|--------|--------|
| `TextEntity` | DWG TEXT/MTEXT | text, x, y |
| `LineGeometry` | DWG LINE/LWPOLYLINE | points, layer |
| `CircleGeometry` | DWG CIRCLE | center, radius |
| `ArcGeometry` | DWG ARC | center, radius, angles |
| `BlockRef` | DWG INSERT | name, origin |
| `AttributeEntity` | DWG ATTRIB | tag, text, x, y |
| `Page`, `PixelImage` | PDF (V5 P1 deferred) | — |

## 4. TopologyStage (`cable_engine/graph/builder.py`)

`analyze(doc)` dispatches based on document type:

### 4.1 TerminalStripAnalyzer (端子排图)
- Scans vertical LINE entities associated with each cable (via EED cable_id)
- Collects `NO` ATTRIB tags along vertical column for terminal numbers
- Extracts strip name, circuit description, loop id
- Cabinet: text containing `端子排图` minus suffix; remote cabinet = rightmost `EQUNAME`

### 4.2 CircuitLoopAnalyzer (回路图)

Pipeline per document — see `CircuitLoopAnalyzer.analyze()` (`cable_engine/graph/builder.py:444`):

1. **ATTRIB flattening** — every `AttributeEntity`/`TextEntity` is projected into a flat list of `(tag, val, x, y)` dicts.
2. **Cable / core detection** — `WireSerial` ATTRIBs matching `CABLE_ID(N)` (e.g. `11003-387(1)`) seed `cable_cores[cable_id][core_no] = (wx, wy)`.
3. **Pre-scan core lines** — all `LineGeometry` with `max(ys) - min(ys) ≤ 3mm` are stored as `core_lines = [{y, x_min, x_max}]`.
4. **Per core** (sorted top-to-bottom by WS y):
   - **Step 1 — Core line** (two-pass within ±30mm of WS y):
     - Pass 1: line whose `x_min ≤ wx ≤ x_max` (covers WS).
     - Pass 2: line with `x_min ≥ 200` and span ≥ 50mm (catches WS placed right of line).
   - **Step 2 — Terminal pairing**: see Section 6.1.
   - **Step 2a — Cabinet detection** (once per cable, on the first core with valid terminals). Search upward from each terminal position for text containing `屏`/`柜`/`箱`; pick smallest `dx` (dy tiebreaker); merge location text to the left as `location-name` (dx ≤ 200mm, dy ≤ 3mm).
   - **Step 3 — WireDescription / LoopCode**: nearest ATTRIB within 80mm y of `wy`; x-distance to WS used as tiebreaker.
   - **Record emission**: split left terminal into `(strip_name, terminal_no)`; keep right terminal as `terminal_no_remote`.

## 5. Storage (`cable_engine/storage/sqlite.py`)

Single `cable_topology` table:

| Column | Type | Description |
|--------|------|-------------|
| cable_id | TEXT | Cable ID (e.g. 11003-311) |
| conductor_no | INT | Core number |
| strip_name | TEXT | Terminal strip (e.g. 21CD) |
| terminal_no | INT | Local terminal number |
| terminal_no_remote | TEXT | Remote terminal (e.g. X1:9) |
| cabinet_name | TEXT | Local cabinet (区域-名称) |
| cabinet_name_remote | TEXT | Remote cabinet |
| circuit_desc | TEXT | WireDescription |
| loop_id | TEXT | Loop code |
| source_type | TEXT | circuit_loop | terminal_strip |
| document_hash | TEXT | Source DWG fingerprint |

## 6. Business Rules

### 6.1 回路图 (CircuitLoopAnalyzer)

**Cable detection**: `WireSerial` ATTRIB matching `CABLE_ID(N)` — e.g. `11003-387(1)`. Cores ordered top-to-bottom by descending `wy`.

**Core line detection**: two-pass search within ±30mm of WireSerial y:
- Pass 1: line whose x-span covers `wx` (typical WS-on-line layout).
- Pass 2: line with `x_min ≥ 200mm` and span ≥ 50mm (catches WS placed to the right of the line, e.g. `110037-381(5)` at x=449 vs line x_max=414).
- **V6.5.2 fix**: the line's `y` is **NOT** used to overwrite `core_y` for ATTRIB search. When multiple cores share a bus line (e.g. `11003-381` cores 4-6 all touch the same y=355 bus), the line `y` is identical for all of them and would collapse every core onto the same `WireDescription` / `LoopCode`. ATTRIB search uses the per-core `wy` instead.

**Terminal candidate collection** (`cable_engine/graph/builder.py:498-503`):
- Two ATTRIB tag types feed the terminal bucket:
  - `NO` — terminal number on the local strip (e.g. `X4:20`, `VI:1`, `III:29`).
  - `ObjTerm.Name` — terminal block name from anonymous INSERT blocks (e.g. `XB:2`). These are the right-column XB terminals whose visible line geometry lives inside the anonymous block (see Known Limitations).
- Both must have `:` in the value. Indexed by `key = round(y × 2)` (0.5mm resolution).
- The bucket tuple now carries `(x, y, val, tag_type)` so the pairing logic can distinguish `NO` vs `ObjTerm.Name` ownership rules.

**Terminal tag ownership pre-computation** (`cable_engine/graph/builder.py:512-535`):
- For every `NO` tag, compute `(closest_dist, second_closest_dist)` across all `WireSerial` x positions in the same y-bucket.
- This drives the V6.5.3 ownership filter below.
- `ObjTerm.Name` tags skip this pre-computation and use the simpler WS-closest rule instead (see next section).

**Terminal pairing** (per core, within ±5 keys = ±2.5mm y-bucket of `wy`):
- Bucket splits on `wx`:
  - `left_of_ws` = tags with `x < wx`.
  - `right_of_ws` = tags with `x > wx`.
- **V6.5.3 ownership filter** (applied to `bucket_tags` before splitting by side):
  - **`ObjTerm.Name` tags** (XB column, past line end): owned exclusively by the WS at this y whose x is closest to the tag's x. Only that WS may include the tag. This excludes e.g. `XB:2` from a left-side WS like `11037-384` whose WS at x=686 is far from `XB:2` at x=807.5; `XB:2` is reserved for the right-side WS (`11003-387` at x=772).
  - **`NO` tags** (X4:N, VI:N, etc., within line geometry): shared between the closest and 2nd-closest WSs **iff** the 2nd-closest WS is within `_NO_TAG_SHARE_DISTANCE = 200` units of the tag; otherwise the tag is owned exclusively by its closest WS. This distinguishes genuine junction tags (e.g. `VI:1` shared between `11037-384` at x=686 and `11003-387` at x=772 — distances 30 and 116, both ≤ 200) from noise tags whose second-closest WS is far away (e.g. `X5:7` closest to `11037-383` at dist 47.4, second-closest `11037-387` at dist 619 — 619 > 200 → exclusive to `11037-383`).
  - The 200-unit threshold matches the typical column-to-WS spacing on D0210 drawings (X4 ≈ 551, VI ≈ 656, XB ≈ 807 — adjacent columns ~100-150 units apart).
- **Standard layout** (`right_of_ws` not empty — the common case): closest tag on each side wins.
  - `left_candidate = argmin |x - wx|` over `left_of_ws`.
  - `right_candidate = argmin |x - wx|` over `right_of_ws`.
  - With the ownership filter above, `VI:N` is correctly assigned as the local terminal even though both `X4:N` and `VI:N` lie left of WS (VI is shared; X4 is excluded from `11003-387` because its 2nd-closest WS distance exceeds 200).
  - For 3-WS rows in D0210-16 (e.g. `11037-383` + `11037-387` + `11003-387` at y=285.08), noise tags like `X5:7` (closest WS at dist 47.4 to `11037-383`, 2nd at dist 619 to `11037-387`) are correctly excluded from `11037-387` and `11003-387`'s buckets.
- **Gap-split fallback** (`right_of_ws` empty — WS at far right, all terminals on its left, e.g. `11037-384` after XB is filtered out):
  - Sort `left_of_ws` by `x`, find the largest x-gap (≥ 50mm).
  - `split_x = midpoint of largest gap`.
  - `local_side = tags with x ≤ split_x` (further from WS) → `left_candidate`.
  - `remote_side = tags with x > split_x` (closer to WS) → `right_candidate`.
  - If the largest gap is < 50mm, fall back to a single `left_candidate = closest to WS`.
  - Result for `11037-384`: bucket becomes `[X4:20, VI:1]` (XB excluded by ObjTerm.Name rule); gap-split picks `X4:20` as local (further) and `VI:1` as remote (closer to WS).
- **V6.5.2 fix**: the gap-split branch is **only** entered when `right_of_ws` is empty. Previously, a `split_x` filter was applied even when both sides had tags (e.g. `11003-383` core 2 with tags at x=65/226/312 and split_x=146 filtered out `X5:10` at x=312, leaving only `X1:13` at x=65). The standard closest-on-each-side rule is correct whenever both sides are populated.

**WireDescription / LoopCode**: nearest ATTRIB within 80mm `y` of `wy`. Tiebreaker: smaller `|x - wx|` (description column closer to WS wins when two ATTRIBs share the same y from different cables).

**Cabinet**: text containing `屏` / `柜` / `箱` above the terminal position (`y > ty`); sorted by `dx` (dy as tiebreaker). Location text left of cabinet name (same y, dy ≤ 3mm, dx ≤ 200mm) is prepended with `-` separator (e.g. `11003.ZXW-3号主变110kV电压互感器端子箱`). Computed once per cable on the first core with valid terminals — same cabinet applies to every core of a single cable.

**Record schema per core**:
```
{
  cable_id, conductor_no,
  strip_name, terminal_no,           # left terminal split on ':'
  terminal_no_remote,                # right terminal, full string
  cabinet_name, cabinet_name_remote, # computed once per cable
  circuit_desc, loop_id,             # from WireDescription / LoopCode
  source_type='circuit_loop',
}
```

### 6.2 端子排图 (TerminalStripAnalyzer)

**Cable detection**: EED (Extended Entity Data) on LINE entities. First segment of EED value matching `CABLE_ID` pattern identifies cable. LINEs grouped by cable_id → `{horiz: [...], vert: [...]}`.

**Horizontal line**: y-span < 1mm → identifies cable row.
**Vertical line**: x-span < 1mm, length ≥ 20mm → identifies conductor column.

**Conductor number**: NO ATTRIB with digit value within ±5mm x and ±10mm y of the vertical-line top intersection.

**Terminal number**: along the vertical line between corner (horizontal-top/vertical intersection) and end, collect text labels within ±3mm x-corridor. Digit text → terminal number; other texts classified by pattern:
- `CABLE_ID` pattern or `LETTER+DIGITS` → loop ID
- Remaining alphabetic text → circuit description

**Strip name**: search for digit "1" text within 40mm y of terminal, then find `STRIP_NAME`-pattern text (e.g. `21CD`, `YD`) within 38mm y and left of the "1".

**Local cabinet**: text containing `端子排图` minus that suffix (e.g. `主变及无功继电器小室3号主变110kV母线测控柜右侧端子排图` → `主变及无功继电器小室3号主变110kV母线测控柜右侧`).

**Remote cabinet**: rightmost EQUNAME ATTRIB within ±10mm y of the cable's horizontal line.

## 6.3 V6.6 Cabinet Semantic Layer

V6.6 introduces a **Cabinet Region** first-class spatial entity
(`cable_engine/graph/cabinet.py`). Before V6.6, "cabinet" was just a
`cabinet_name` TEXT field on `cable_topology`, populated by `_find_cabinet()`
searching upward from a terminal position for `屏/柜/箱` keywords. V6.6
identifies cabinet regions **directly** from the dashed-rectangle
geometry that the drawing already contains, and extracts them as a
spatial IR entity with bbox + name + location + a containment set
of every terminal the bbox covers.

### 6.3.1 Phased build

| Phase | Module | Output |
|------|--------|--------|
| 1 — linetype detect | `loaders/dwg_loader.py` | `LineGeometry.custom_fields['ltype']` populated for every LINE/LWPOLYLINE with an explicit linetype (resolving `ltype` handle via LTYPE_CONTROL.entries + LTYPE objects). |
| 2 — boundary scan | `graph/cabinet.py:CabinetRegionAnalyzer._find_dashed_rectangles` | `CabinetBoundary` records from each 4-corner axis-aligned ltype-`ACAD_ISO10W100` / `HIDDEN` / `DASHED` LWPOLYLINE. |
| 3 — name match | `graph/cabinet.py:CabinetRegionAnalyzer._match_boundary_text` | Pairs each boundary with the nearest `EquName` / `EQUNAME` / `屏/柜/箱` text above it (location-prefix parsed separately). |
| 4 — containment | `graph/cabinet.py:assign_terminals_to_cabinets` | For each NO / ObjTerm.Name ATTRIB, assigns it to the smallest-area enclosing cabinet by bbox. |
| 5 — persistence | `storage/sqlite.py` + `graph/builder.py:TopologyStage` | New `cabinets` + `cabinet_terminals` tables; analyzer runs BEFORE the per-classification dispatcher so CircuitLoopAnalyzer can already use them. |
| 6 — IR + graph | `ir/entities.py:CabinetRegion` + `graph/types.py:NodeType.CABINET` + `EdgeType.CONTAINS` | Cabinet as first-class IR entity + DocumentGraph node type. |
| 7 — viewer API | `tools/cable_match_viewer/store.py` | `list_cabinets()`, `get_cabinet()`, `get_document_topology()` returns `cabinet_regions[]` with bbox + terminals. |
| 8 — restricted search | `graph/builder.py:CircuitLoopAnalyzer.analyze` | Bucket filter rejects terminals that are in a different cabinet than the WS — eliminates the V6.5.3 cross-row noise (X5:7/III:29 polluting 11037-387 cores 3-4). |

### 6.3.2 Schema

```sql
CREATE TABLE cabinets (
    id              TEXT PRIMARY KEY,   -- "cab_<doc12>_<NNN>" — globally unique
    document_hash   TEXT NOT NULL,
    name            TEXT,
    location        TEXT,
    display_name    TEXT,               -- "11003.ZXW" or "11003.ZXW-3号主变110kV电压互感器端子箱"
    text_label      TEXT,               -- descriptive text below boundary
    bbox_x / bbox_y / bbox_w / bbox_h REAL,
    layer           TEXT,
    boundary_handle TEXT,
    ltype           TEXT,               -- ACAD_ISO10W100 / HIDDEN / DASHED
    points_json     TEXT                -- JSON array of polyline corners
);
CREATE INDEX idx_cabinets_doc ON cabinets(document_hash);
CREATE INDEX idx_cabinets_display ON cabinets(display_name);

CREATE TABLE cabinet_terminals (
    id              INTEGER PRIMARY KEY,
    cabinet_id      TEXT NOT NULL,
    document_hash   TEXT NOT NULL,
    terminal_id     TEXT NOT NULL,      -- NO / ObjTerm.Name tag value
    terminal_kind   TEXT NOT NULL,
    x / y           REAL
);
CREATE UNIQUE INDEX uq_cterm ON cabinet_terminals(cabinet_id, document_hash, terminal_id, terminal_kind);
```

### 6.3.3 IR entity

`cable_engine/ir/entities.py:CabinetRegion` is a dataclass subclass of
`Entity`. It's emitted into `doc.entities` BEFORE the analyzer runs so
`CircuitLoopAnalyzer` can iterate over the document's cabinets and use
them as a containment index:

```python
@dataclass
class CabinetRegion(Entity):
    name: str = ''
    location: str = ''
    display_name: str = ''
    text_label: str = ''
    boundary_handle: str = ''
    ltype: str = ''
    contained_terminal_ids: list[str] = field(default_factory=list)
```

### 6.3.4 Cabinet-restricted terminal search (Phase 8 → revised in V6.6.1)

V6.6 Phase 8 originally added a strict **cabinet bbox gate** that ran
BEFORE the y-bucket:

```python
ws_cabinet = _ws_in_cabinet(wx, wy, v66_cabinets)
# In the bucket loop:
if ws_cabinet is not None and v66_terminal_cab is not None:
    tc = v66_terminal_cab.get((tx, ty))
    if tc is not None and tc != ws_cabinet:
        continue   # skip — different cabinet
```

This turned out to be too aggressive: legitimate cross-cabinet wire
pairs were being filtered out. Example: D0210-15 has WS `11003-311(1)`
at x=286 (in `cab_002`); its left terminal `21CD:1` at x=186 is in
`cab_001` — a DIFFERENT cabinet from the WS, but legitimately the
wire's left endpoint. The filter rejected `21CD:1`, leaving the cable
with an empty left terminal (`term=None remote='III:13'`).

**V6.6.1 fix**: the cabinet bbox gate is REMOVED from the terminal
bucket. V6.5.3's distance-based NO-tag ownership (200-unit share
threshold) is sufficient for noise removal — when multiple WSs share
a row, NO tags with both closest + 2nd-closest WS within 200 units are
correctly marked as shared; tags with only one WS within 200 units are
owned by that WS alone.

The V6.6 spatial cabinet data is still used for:

1. **cabinet_name / cabinet_name_remote lookup** — replaces the V6.5
   `_find_cabinet()` text search. The terminal's position is checked
   against every detected cabinet bbox; the smallest enclosing cabinet
   contributes its `display_name`. Falls back to V6.5 text search
   when no spatial cabinet covers the terminal.
2. **Viewer UI** — cabinet-aware tabs and bbox highlighting.
3. **Future V6.7 WireTracer** — DFS within cabinet bboxes.

`_ws_in_cabinet()` and `_cabinet_for_terminal()` are kept as helpers
for these non-terminal-pairing uses.

### 6.3.5 Documents WITHOUT cabinet detection

The detector fires whenever the LineGeometry carries a `ltype` in
`ACAD_ISO10W100` / `HIDDEN` / `DASHED` AND the points form a
4-corner axis-aligned rectangle. Documents without dashed rectangles
(`端子排图` that group terminals into column strips, or 保护原理图)
will simply produce 0 cabinet rows. The CabinetRegion IR entity
list is empty → the analyzer falls back to the V6.5.3 logic.

### 6.3.6 Pre-existing limitations

- **Anonymous-block nested cabinets**: when one cabinet bbox
  encloses another (e.g. an inner power-supply box inside an outer
  control box), the inner boundary is also reported. Containment
  is detected by `assign_terminals_to_cabinets` (smallest-area wins).
  Future work could split into a `parent_cabinet_id` field.
- **DWG ltype coverage**: only ACAD_ISO10W100 / HIDDEN / DASHED
  are recognized. Adding new linetype names to the
  `_DASHED_LTYPES` frozenset is a one-line change.
- **Anonymous-block expansion**: still tracked as V5 P0 issue from
  earlier — linetype info inside unnamed blocks is invisible to
  dwgread, so cabinet boundaries drawn inside such blocks are not
  detected.

## 7. Viewer (`tools/cable_match_viewer/`)

Three-pane aiohttp web UI on port 8003:

- **Left column (电缆列表)**: cable list with search filter
- **Right column**: cable detail (conductors, terminals, cabinets, source docs)
- **Bottom overlay**: DWG preview via flyfish CAD viewer
- **柜体 tab**: live cabinet search (`/api/search-cabinets?q=`) — fuzzy match on cabinet_name / cabinet_name_remote across all documents

## 8. Capability Boundaries

### Supported
- **端子排图**: cable topology with conductors, terminals, loop IDs, circuit descriptions, strip names, local/remote cabinets
- **回路图**: cable core topology (120+ cores verified), wire descriptions, loop codes, terminals via `NO` + `ObjTerm.Name` ATTRIBs (covers both standard columns and anonymous-block XB terminals), cabinet association with location prefix
- **Mixed scenes**: 回路图 with 0-terminal cables (devices) correctly excluded; single-core cables with shared cabinets at cable level; terminal-free cores (naked wires) correctly getting `left_terminal = ""`
- **Viewer**: live search across cables, terminals, cabinets via API; DWG preview via flyfish CAD viewer

### Known Limitations
- **dwgread anonymous blocks**: cable labels/geometry inside unnamed BLOCK definitions are invisible, breaking topology for drawings that heavily use anonymous blocks (e.g. D0210-35). See `DWGLoader._parse_v5` for the expansion stub.
- **PDF**: RasterizeStage + OCRStage not yet implemented; PDF pipeline is skipped at scan time.
- **Overbroad loop classification**: `M1`/`M2`/`10D`/`-OF-12` match the loop text pattern but aren't loop IDs; tightened filtering deferred.
- **Batch performance**: ~3-5s per file on M1; 1000+ file scans not yet benchmarked.

## 9. File Map

```
cable_engine/
├── cli.py                       # entry: scan subcommand
├── graph/
│   ├── builder.py               # TopologyStage + TerminalStripAnalyzer + CircuitLoopAnalyzer
│   ├── types.py                 # DocumentGraph (spatial indices)
│   └── spatial.py               # Uniform-grid spatial index
├── ir/
│   ├── entities.py              # Entity base + TextEntity/LineEntity/SymbolEntity
│   ├── geometry.py              # LineGeometry, CircleGeometry, ArcGeometry, BlockRef, AttributeEntity
│   ├── document.py              # Document, DocumentType
│   └── pdf.py                   # Page, PixelImage (PDF-specific)
├── loaders/
│   ├── dwg_loader.py            # dwgread -O JSON + ezdxf fallback
│   └── pdf_loader.py            # pypdfium2 (V5 P1 deferred)
├── pipeline/
│   ├── stage.py                 # Stage base class
│   └── __init__.py              # Context + Pipeline
└── storage/
    └── sqlite.py                # CableStore (cable_topology CRUD + schema)

tools/cable_match_viewer/
├── server.py                    # aiohttp app + HTML UI (338 lines)
└── store.py                     # CableViewer (read-only facade)
```

## 10. V4 → V6 Migration Notes

- V4 `rules/` directory, `RuleEngineStage`, `MatchStage`, `FusionStage` — all removed
- V4 `graph_nodes` / `graph_edges` graph tables — removed; topology stored in `cable_topology`
- V5/V6: graph is built per-document at scan time for spatial queries (NEAR, INSIDE edges) but business topology is computed directly in analyzers
- PDF pipeline (RasterizeStage + OCRStage) deferred from V5 P0; not yet implemented
- Anonymous-block expansion in dwgread-json not available; geometry inside unnamed blocks invisible
