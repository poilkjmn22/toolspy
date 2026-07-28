# 表格解析算法

## 1. 概述

表格解析子系统（`cable_engine/layout/table/`）从多种文档类型的 CAD 表格区域中提取结构化的行/列数据（PANEL_LAYOUT 设备表、PANEL_POSITION 屏屏用途一览表、CABLE_SCHEDULE 电缆清册、设备材料表）。它将 6× 重复的实体迭代模式和 3× 重复的 Y 分桶/表头检测模式统一为 `BaseTableParser` 模板方法 + 共享 `text_utils` 模块。

四个具体解析器共享同一架构：

| 解析器 | 文档类型 | 检测策略 | 行锚定方式 |
|--------|----------|----------|-----------|
| `EquipmentTableParser` | PANEL_LAYOUT | A→B→C (DBSCAN/rect/title) | Y-bucket |
| `UsageTableParser` | PANEL_POSITION | 仅 C (标题"屏屏用途一览表") | 标签居中 |
| `ScheduleParser` | CABLE_SCHEDULE | 无 (处理全部实体) | Y-bucket |
| `MaterialTableParser` | PANEL_POSITION | 仅 C (标题"设备材料表") | Y-bucket |

---

## 2. 核心模块

### 2.1 `text_utils.py` — 共享文本原语

`collect_texts(doc, bbox, max_len, is_noise)` — 统一的实体迭代。按 bbox 过滤、剔除空白、应用噪声谓词。生成统一的 `(x, y, text)` 三元组供下游所有操作使用。

`y_bucket_rows(texts, tol)` — 按 `round(y / tol) * tol` 将文本分组。从上到下排序（Y 递减，CAD 惯例）。每行是按 X 排序的 `(x, text)` 列表。若少于 2 个桶则返回空列表（噪声抑制）。

`y_bucket_rows_with_labels(texts, label_pattern, tol)` — 锚定变体。先找到匹配标签模式的文本，然后将 `tol` 范围内非标签文本归入最近的标签行。当同一 CAD 行内的兄弟单元格散布在不同 Y 坐标时，比纯 Y 分桶更加稳健。

`find_header_row(rows, patterns)` — 将每行的文本用空格连接，然后逐个测试 `(regex, role_name)` 模式。返回第一个匹配行的索引。

`map_column_roles(header_cells, patterns)` — 将每个表头单元格文本与模式匹配，生成 `{col_index: role}` 字典。

`detect_gap_x(header_cells, col_roles, gap_role)` — 检测两列分割布局中列半组之间的 X 间隔（用于 `屏号 | 名称 | 数量 | 备注` × 2 布局）。

噪声过滤器协议（`NoiseFilter`）：可调用对象 `(text: str) -> bool`，返回 `True` 表示跳过该文本。默认过滤器拒绝空文本、`\M+` 前缀和 `KKS` 前缀。子类可覆盖以实现类型特定的过滤（如材料表的单字母绘图标注过滤）。

### 2.2 `base.py` — `BaseTableParser` ABC

模板方法类提供：

**检测策略**（在 `detect_bboxes()` 中按 A→B→C 依次尝试）：

| 策略 | 置信度 | 方法 |
|------|--------|------|
| **A** — DBSCAN 网格聚类 | 0.9 | `detect_table_clusters()`: 对所有矩形形心执行 DBSCAN（仅小矩形 <200u），通过 `_is_grid_like()` 验证网格性（填充率 ≥50%），保留 ≥6 个矩形且 ≥4 个文本的簇 |
| **B** — 单个大矩形 | 0.6 | `detect_table_bbox_rect()`: 查找 ≥60w×80h 且内部包含 ≥4 个文本的矩形 |
| **C** — 标题文本→偏移 bbox | 0.4 | `detect_table_bbox_title()`: 查找最右侧的以"表"结尾的文本，返回 `BBox(ex-200, ey-350, 350, 450)` |

子类可在需要自定义策略时完全覆盖 `detect_bboxes()`（例如 `ScheduleParser` 返回无 bbox 哨兵；`UsageTableParser` 仅使用策略 C 并带类型特定偏移）。

**`_is_grid_like(cxs, cys, tol=8.0)`** — 通过将 `x/tol` 和 `y/tol` 四舍五入将形心分组为行列单元格。检查 `fill_ratio = actual / expected ≥ 0.5`。比 `GridAnalyzer`（要求精确的 `cols×rows == count`）更宽松。

**行锚定**（`_anchor_rows`，两种模式）：
- `'y_bucket'`：纯 `y_bucket_rows(texts, ROW_TOL)`。
- `'label_centered'`：`y_bucket_rows_with_labels(texts, LABEL_PATTERN, ROW_TOL)`——需要设置 `LABEL_PATTERN`。

**模板方法 `parse()`**：

```
detect_bboxes(doc)         → [(bbox, confidence)]
  ↓
for each bbox (按置信度降序):
  _parse_at(doc, bbox, confidence)
    collect_texts(doc, bbox)
    _anchor_rows(texts)       → rows (Y 分桶列表)
    find_header_row(rows)     → header_idx
    map_column_roles(header)  → col_roles
    detect_gap_x(header)      → gap_x (可选)
    extract_data(...)         → 领域输出
  return first success
return None
```

**`extract_data()`** — 抽象方法，子类实现以将解析的行 + 列角色转换为领域特定输出（如 `TableArea`、用途表 dict、电缆拓扑记录）。

---

## 3. 具体解析器

### 3.1 `EquipmentTableParser` (设备表/材料表)

用于 PANEL_LAYOUT 图纸。`ROW_TOL=5.0`（高于默认的 3.0，以处理 8-10 单位的行间距和 0.5-1 单位的行内 Y 变化）。

**检测**：继承基类的 A→B→C 链。实际管线中 `detector.py` 的 `_detect_equipment_table` 执行基于柜体的标题搜索，使用 `BBox(ex-120, ey-42, 240, 52)`（比策略 C 默认更窄更高），结果直接传入 `parse_table_at()`（绕过 `BaseTableParser.detect_bboxes()`）。

**列分配**（`_assign_by_position`）：对每个数据行，文本按 X 排序后从左到右匹配到表头列。每个文本在当前位置 ±3 列范围内找到最近的表头中点。距离首个表头 X 大于 15 单位的文本被拒绝（左边距噪声）。未匹配的列保持空白。替换了早期当备注文本跨越多个列宽时失败的绝对列间隔方法。

**噪声过滤**（`_is_noise_cell`）：跳过单字母 + 受限标点的文本（≤8 字符，如 `ZD`、`LP`、`FA`）和标题文本（`设备表`、`材料表` 等）。

**集成**：`parse_table_at()` 是传统入口——被 `detector.py` 的 `_detect_equipment_table()` 调用，也可通过 `cable_engine.layout.table.parse_table_at` 直接导入。

### 3.2 `UsageTableParser` (屏屏用途一览表)

用于 PANEL_POSITION 图纸。仅使用策略 C 检测（`_find_table_bbox`：最右侧以"表"结尾的文本 → `BBox(ex-200, ey-350, 350, 450)`）。使用**标签居中锚定**，`LABEL_PATTERN = r'^\d+[CF]$'`。

**两列间隔检测**：`detect_gap_x()` 找到第一个"屏号"列右侧边缘与第二个"屏号"列左侧边缘之间的中点 X。每个半组获得自己的半角色映射。

**行提取**（`_extract_one_row`）：对每个标签文本（`1F`、`2C` 等），收集标签 Y 坐标 `ROW_TOL=6.0` 范围内的兄弟文本。在半组内按相对列索引分配（跳过标签列）。支持数量（qty）的整数累加和备注（remark）的字符串拼接。

**集成**：`parse_usage_table(doc, room)` 是传统入口，被 `position/builder.py` 调用。返回带有 `{'bbox': BBox, 'rows': list[dict]}` 的 dict，调用方将其转换为 `UsageTable` 领域模型。

### 3.3 `ScheduleParser` (电缆清册)

用于 CABLE_SCHEDULE 图纸。覆盖 `detect_bboxes()` 返回无 bbox 哨兵——处理整个文档。`MAX_TEXT_LEN=80`（电缆描述可能较长）。

**数据提取**：从表头模式中识别 `cable_id` 列，然后迭代表头下方的数据行。每行生成一条拓扑记录 dict。当表头检测失败时，通过正则 `\b([A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8})\b` 回退到 `extract_cable_ids_fallback()`。

**集成**：`parse_schedule_table(doc)` 是传统入口，被 `graph/builder.py` 中的 `CableScheduleAnalyzer` 调用。

### 3.4 `MaterialTableParser` (设备材料表)

用于 PANEL_POSITION 图纸。通过标题模式匹配"材料表"检测——找到标题文本，返回 `BBox(ex-140, ey-170, 300, 200)`。使用 Y 分桶行锚定（`ROW_TOL=5.0`）。

**噪声过滤**：扩展 `_default_noise()` 以同时拒绝单个大写字母文本（绘图标注如 `B`、`C`）。

**列分配**：最近表头 X 匹配（简单 1D 距离，无超前查找）。距离最近表头 X 大于 50 单位的文本被拒绝。

**集成**：`parse_material_table(doc)` 是传统入口，被 `position/builder.py` 调用。返回带有 `{'bbox': BBox, 'rows': list[dict]}` 的 dict，每行包含 `index`、`name`、`unit`、`qty`、`remark`。

---

## 4. 管线集成

### 4.1 PANEL_LAYOUT (`detector.py`)

`_detect_equipment_table(doc, cab)` 在 `build_layout_tree()` 的逐柜循环中执行：

```
search_bbox = BBox(cab.bbox.x, cab.bbox.y-100, cab.bbox.w+200, cab.bbox.h+100)
for each title (text.endswith('表'), len≤6) in search_bbox:
    title_bbox = BBox(ex-120, ey-42, 240, 52)
    table = parse_table_at(doc, title_bbox)
    if table and name_column_index >= 0:
        store table (通过 header+rows 比较去重)
```

存储在 `tree.meta['equipment_tables']` 中，格式为 `[{cabinet, header, col_map, rows}]`。`match_to_devices()` 在 `TextAssociator.associate_devices` 之后、DBSCAN 聚类之前运行，将 `features['table_info']` 注入到匹配的 DeviceCandidate 中。

### 4.2 PANEL_POSITION (`position/builder.py`)

`build_position_tree()`：
1. `detect_room()` → 房间 bbox
2. `detect_cells(room)` → F 编号单元格网格
3. `parse_usage_table(doc, room)` → 屏号→设备映射
4. `cross_reference(cells, table_rows)` → 合并
5. 可选 `parse_material_table(doc)` → 物料清单存储在 meta 中

### 4.3 CABLE_SCHEDULE (`graph/builder.py`)

`CableScheduleAnalyzer` 委托给 `ScheduleParser`：

```python
table_records = parse_schedule_table(doc)
for rec in (table_records or []):
    # 生成 cable_topology 行（含 cable_id）
```

---

## 5. 关键参数

| 参数 | 默认值 | 使用者 | 作用 |
|------|--------|--------|------|
| `ROW_TOL` | 3.0 | 所有解析器（可覆盖） | Y 分桶量化；设备表使用 5.0 |
| `MAX_TEXT_LEN` | 50（可变） | `collect_texts` | 拒绝超过此长度的实体文本 |
| `_CELL_EPS` | 30.0 | DBSCAN（策略 A） | 矩形形心聚类的邻域半径 |
| `_MIN_GRID_RECTS` | 6 | DBSCAN（策略 A） | 形成簇所需的最少矩形数 |
| `_MIN_TABLE_W/H` | 60/80 | 策略 B | 表格包含矩形的最小尺寸 |
| `_MIN_TEXTS` | 4 | 所有检测策略 | 候选区域内最少文本数 |
| `_is_grid_like tol` | 8.0 | 策略 A | 网格验证的形心四舍五入容差 |
| `first_col_x - 15` | — | `_assign_by_position` | 左边距噪声拒绝阈值 |
| `±3 lookahead` | — | `_assign_by_position` | 最近表头匹配的列搜索窗口 |

---

## 6. 当前限制

- **策略 A (DBSCAN)**：仅能检测包含 ≥6 个独立单元格矩形的表格。用途表和材料表（只有一个外矩形或无矩形）回退到策略 C。
- **堆叠子表**：D0212-38 包含两个表格（设备材料表 + 附件材料表）共享连续的水平网格线。当前基于标题的 bbox 只能捕获第一个。
- **`_is_grid_like`**：填充率 ≥50% 而非严格的 cols×rows 匹配。对 F 编号网格足够；可能对稀疏布局产生误报。
- **左边距噪声导致的列偏移**：D0212-38 左边距的 `ZD` 文本位于 X=7，接近 `first_col_x - 15` 阈值；导致数据右移一列。
