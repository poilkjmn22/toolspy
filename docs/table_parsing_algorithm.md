# Table Parsing Algorithm

## 1. Overview

The table parsing subsystem (`cable_engine/layout/table/`) extracts structured row/column data from CAD-based tabular regions in multiple document types (PANEL_LAYOUT equipment tables, PANEL_POSITION usage tables, CABLE_SCHEDULE tables, 设备材料表). It replaces 6× duplicated entity-iteration patterns and 3× Y-bucketing/header-detection patterns with a unified `BaseTableParser` template method and shared `text_utils` module.

Four concrete parsers share one architecture:

| Parser | Document Type | Detection Strategy | Row Anchoring |
|--------|---------------|-------------------|---------------|
| `EquipmentTableParser` | PANEL_LAYOUT | A→B→C (DBSCAN/rect/title) | Y-bucket |
| `UsageTableParser` | PANEL_POSITION | C only (title "屏屏用途一览表") | Label-centered |
| `ScheduleParser` | CABLE_SCHEDULE | None (process all entities) | Y-bucket |
| `MaterialTableParser` | PANEL_POSITION | C only (title "设备材料表") | Y-bucket |

---

## 2. Core Modules

### 2.1 `text_utils.py` — Shared text primitives

`collect_texts(doc, bbox, max_len, is_noise)` — unified entity-iteration. Filters by bbox, strips whitespace, applies a noise predicate. The single `(x, y, text)` triple format feeds all downstream operations.

`y_bucket_rows(texts, tol)` — groups texts by rounding `y / tol * tol`. Sorted top-to-bottom (descending Y, CAD convention). Each row is a list of `(x, text)` sorted left-to-right. Returns empty if fewer than 2 buckets — noise rejection.

`y_bucket_rows_with_labels(texts, label_pattern, tol)` — anchored variant. Finds label-matching texts first, then folds non-label texts within `tol` into the nearest label's row. More robust than pure Y-bucketing when sibling cells in the same CAD row sit at scattered Y coordinates.

`find_header_row(rows, patterns)` — joins each row's texts with a space and tests against `(regex, role_name)` patterns. Returns the index of the first match.

`map_column_roles(header_cells, patterns)` — matches each header cell text against patterns to produce `{col_index: role}` dict.

`detect_gap_x(header_cells, col_roles, gap_role)` — detects the X-gap between column halves in a two-column split layout (used by UsageTableParser for "屏号 | 名称 | 数量 | 备注" × 2).

Noise filter protocol (`NoiseFilter`): a callable `(text: str) -> bool` returning `True` to skip a text. Default rejects empty texts, `\M+` prefix, and `KKS` prefix. Subclasses override for type-specific filtering (e.g. single-letter drawing annotations for material tables).

### 2.2 `base.py` — `BaseTableParser` ABC

The template method class provides:

**Detection strategies** (tried A→B→C in `detect_bboxes()`):

| Strategy | Confidence | Method |
|----------|------------|--------|
| **A** — DBSCAN grid clustering | 0.9 | `detect_table_clusters()`: runs DBSCAN on all rectangle centroids (small rects <200u), validates grid-likeness via `_is_grid_like()` (fill ratio ≥50%), keeps clusters with ≥6 rects and ≥4 texts |
| **B** — Single large rectangle | 0.6 | `detect_table_bbox_rect()`: finds rectangles ≥60w×80h with ≥4 texts inside |
| **C** — Title text → offset bbox | 0.4 | `detect_table_bbox_title()`: finds rightmost text ending with "表", returns `BBox(ex-200, ey-350, 350, 450)` |

Subclasses override `detect_bboxes()` entirely when they need a custom strategy (e.g., `ScheduleParser` returns a no-bbox sentinel; `UsageTableParser` uses only Strategy C with type-specific offset).

**`_is_grid_like(cxs, cys, tol=8.0)`** — groups centroids into row/column cells by rounding `x/tol` and `y/tol`. Checks `fill_ratio = actual / expected ≥ 0.5`. More lenient than `GridAnalyzer` (which requires exact `cols×rows == count`).

**Row anchoring** (`_anchor_rows`, two modes):
- `'y_bucket'`: pure `y_bucket_rows(texts, ROW_TOL)`.
- `'label_centered'`: `y_bucket_rows_with_labels(texts, LABEL_PATTERN, ROW_TOL)` — requires `LABEL_PATTERN` to be set.

**Template method `parse()`**:

```
detect_bboxes(doc)         → [(bbox, confidence)]
  ↓
for each bbox (sorted by descending confidence):
  _parse_at(doc, bbox, confidence)
    collect_texts(doc, bbox)
    _anchor_rows(texts)       → rows (list of Y-buckets)
    find_header_row(rows)     → header_idx
    map_column_roles(header)  → col_roles
    detect_gap_x(header)      → gap_x (optional)
    extract_data(...)         → domain output
  return first success
return None
```

**`extract_data()`** — abstract method subclasses implement to convert parsed rows + column roles into domain-specific output (e.g., `TableArea`, usage table dict, cable topology records).

---

## 3. Concrete Parsers

### 3.1 `EquipmentTableParser` (设备表/材料表)

For PANEL_LAYOUT drawings. `ROW_TOL=5.0` (higher than default 3.0 to handle 8-10 unit row spacing with 0.5-1 unit intra-row Y variation).

**Detection**: inherits A→B→C chain from base class. In practice, the pipeline in `detector.py` (`_detect_equipment_table`) runs a per-cabinet title search with `BBox(ex-120, ey-42, 240, 52)` — narrower and taller than Strategy C's defaults — and feeds the result directly to `parse_table_at()` (bypassing `BaseTableParser.detect_bboxes()`).

**Column assignment** (`_assign_by_position`): for each data row, texts are sorted by X and matched to header columns left-to-right. Each text finds the nearest header midpoint within ±3 columns of the current position. Texts more than 15 units left of the first header X are rejected (left-margin noise). Unmatched columns stay empty. Replaces the earlier absolute-column-gap approach that failed when remark text spans multiple column widths.

**Noise filter** (`_is_noise_cell`): skips cells with single-letter + limited punctuation texts (≤8 chars, e.g. `ZD`, `LP`, `FA`) and title texts (`设备表`, `材料表`, etc.).

**Integration**: `parse_table_at()` is the legacy entry point — called by `detector.py`'s `_detect_equipment_table()` and also directly importable via `cable_engine.layout.table.parse_table_at`.

### 3.2 `UsageTableParser` (屏屏用途一览表)

For PANEL_POSITION drawings. Detects via Strategy C only (`_find_table_bbox`: rightmost "表"-ending text → `BBox(ex-200, ey-350, 350, 450)`). Uses **label-centered anchoring** with `LABEL_PATTERN = r'^\d+[CF]$'`.

**Two-column gap detection**: `detect_gap_x()` finds the midpoint X between the first "屏号" column's right edge and the second "屏号" column's left edge. Each half gets its own half-role mapping.

**Row extraction** (`_extract_one_row`): for each label text (`1F`, `2C`, etc.), collects sibling texts within `ROW_TOL=6.0` of the label's Y. Assignment by relative column index within the half (skipping the label column). Supports `qty` as integer accumulation and `remark` as concatenated strings.

**Integration**: `parse_usage_table(doc, room)` is the legacy entry point, called by `position/builder.py`. Returns dict with `{'bbox': BBox, 'rows': list[dict]}`, which the caller converts to `UsageTable` domain model.

### 3.3 `ScheduleParser` (电缆清册)

For CABLE_SCHEDULE drawings. Overrides `detect_bboxes()` to return a no-bbox sentinel — processes the entire document. `MAX_TEXT_LEN=80` (cable descriptions can be long).

**Data extraction**: identifies the `cable_id` column from header patterns, then iterates data rows below the header. Each row produces a topology record dict. Falls back to `extract_cable_ids_fallback()` via regex `\b([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})\b` when header detection fails.

**Integration**: `parse_schedule_table(doc)` is the legacy entry point, called by `CableScheduleAnalyzer` in `graph/builder.py`.

### 3.4 `MaterialTableParser` (设备材料表)

For PANEL_POSITION drawings. Detects via title pattern matching "材料表" — finds the title text, returns `BBox(ex-140, ey-170, 300, 200)`. Uses Y-bucket row anchoring (`ROW_TOL=5.0`).

**Noise filter**: extends `_default_noise()` to also reject single uppercase letter texts (drawing annotations like `B`, `C`).

**Column assignment**: nearest-header-X matching (simple 1D distance, no lookahead). Texts more than 50 units from the nearest header X are rejected.

**Integration**: `parse_material_table(doc)` is the legacy entry point, called by `position/builder.py`. Returns dict with `{'bbox': BBox, 'rows': list[dict]}` where each row has `index`, `name`, `unit`, `qty`, `remark`.

---

## 4. Pipeline Integration

### 4.1 PANEL_LAYOUT (`detector.py`)

`_detect_equipment_table(doc, cab)` runs in the per-cabinet loop of `build_layout_tree()`:

```
search_bbox = BBox(cab.bbox.x, cab.bbox.y-100, cab.bbox.w+200, cab.bbox.h+100)
for each title (text.endswith('表'), len≤6) in search_bbox:
    title_bbox = BBox(ex-120, ey-42, 240, 52)
    table = parse_table_at(doc, title_bbox)
    if table and name_column_index >= 0:
        store table (suppressing duplicates by header+rows comparison)
```

Stored in `tree.meta['equipment_tables']` as `[{cabinet, header, col_map, rows}]`. `match_to_devices()` runs after `TextAssociator.associate_devices` but before DBSCAN clustering, injecting `features['table_info']` into matching DeviceCandidates.

### 4.2 PANEL_POSITION (`position/builder.py`)

`build_position_tree()`:
1. `detect_room()` → room bbox
2. `detect_cells(room)` → F-number cell grid
3. `parse_usage_table(doc, room)` → screen-number→equipment mapping
4. `cross_reference(cells, table_rows)` → merge
5. Optionally `parse_material_table(doc)` → material bill stored in meta

### 4.3 CABLE_SCHEDULE (`graph/builder.py`)

`CableScheduleAnalyzer` delegates to `ScheduleParser`:

```python
table_records = parse_schedule_table(doc)
for rec in (table_records or []):
    # emit cable_topology row with cable_id
```

---

## 5. Key Parameters

| Parameter | Default | Used By | Effect |
|-----------|---------|---------|--------|
| `ROW_TOL` | 3.0 | All parsers (override) | Y-bucket quantization; equipment tables use 5.0 |
| `MAX_TEXT_LEN` | 50 (varies) | `collect_texts` | Rejects entity texts exceeding this |
| `_CELL_EPS` | 30.0 | DBSCAN (Strategy A) | Neighbourhood radius for rect centroid clustering |
| `_MIN_GRID_RECTS` | 6 | DBSCAN (Strategy A) | Minimum rects to form a cluster |
| `_MIN_TABLE_W/H` | 60/80 | Strategy B | Minimum dimensions for a table-containing rectangle |
| `_MIN_TEXTS` | 4 | All detection strategies | Minimum text count inside a candidate region |
| `_is_grid_like tol` | 8.0 | Strategy A | Centroid rounding tolerance for grid validation |
| `first_col_x - 15` | — | `_assign_by_position` | Left-margin noise rejection threshold |
| `±3 lookahead` | — | `_assign_by_position` | Column search window for nearest-header matching |

---

## 6. Current Limitations

- **Strategy A (DBSCAN)**: Only detects tables with ≥6 individual cell rectangles. Usage tables and material tables (which have a single outer rect or no rects) fall to Strategy C.
- **Stacked sub-tables**: D0212-38 has two tables (设备材料表 + 附件材料表) sharing continuous horizontal grid lines. Current title-based bbox captures only the first.
- **`_is_grid_like`**: Fill ratio ≥50% rather than strict cols×rows matching. Sufficient for F-number grids; may produce false positives for sparse layouts.
- **Column shift with left-margin noise**: D0212-38 left-margin `ZD` text at X=7 is near the `first_col_x - 15` threshold; shifts data right by 1 column.
