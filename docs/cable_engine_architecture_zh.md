# cable_engine 架构文档 (V9)

## 1. 系统概述

```
DWG 文件 → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                          ↓
                                                        tools/cable_match_viewer/ (aiohttp)
                                                                          ↓
                                                   LayoutStage → panel_layout (SQLite)
```

单一 `TopologyStage` 负责文档分类、柜体分析和分析器分发。V8 引入了 **GeometryGraph**（纯几何图结构）来替代 V7 过程式的 `_cabinet_path_trace()` 算法。V8.2 在 LayoutStage 中引入了基于 **CandidatePool** + **DBSCAN** 的设备检测管线。V9 在此基础上新增四层能力：**Structure Analyzers**（结构分析器，替代内联评分函数）、**TableParser**（表格解析器，注入设备业务元数据）、**SpatialGraph**（空间关系图，捕获节点间的几何关系）和 **SemanticScore**（多证据融合引擎，替代前缀匹配）。

## 2. 文档分类

`CompositeClassifier`（`cable_engine/classifier/`）对每个文档执行三个子分类器：

| 子分类器 | 权重 | 方法 |
|----------|------|------|
| `KeywordClassifier` | 0.55 | ATTRIB 标签提示（2倍权重）、强标记、关键词列表 |
| `GeometryClassifier` | 0.30 | 实体数量比率（线条、弧、圆、块、ATTRIB、文本） |
| `LayoutClassifier` | 0.15 | 文本位置分布（象限占用、y分桶聚类、边距密度） |

七种业务类型：

| 类型 | 数量 | 分析器 |
|------|------|--------|
| `CIRCUIT_LOOP`（回路图） | 269 | `CircuitLoopAnalyzer` |
| `TERMINAL_STRIP`（端子排图） | 185 | `TerminalStripAnalyzer` |
| `CABLE_SCHEDULE`（电缆清册） | 6 | `CableScheduleAnalyzer` |
| `PROTECTION_DIAGRAM`（保护原理图） | 166 | 无（仅查看器） |
| `PANEL_LAYOUT`（屏面布置图） | 48 | `LayoutStage`（布局树） |
| `PANEL_POSITION`（屏位布置图） | — | `build_position_tree`（房间→屏位格→表格→交叉引用） |
| `MONITORING_SYSTEM`（状态监测/通风） | 23 | 无（仅查看器） |
| `UNKNOWN`（目录/封面） | 260 | 无（仅查看器） |

## 3. 管线处理流程

```
加载器 → Document IR → TopologyStage.run()
                          ├─ 文档分类
                          ├─ [仅回路图] 柜体分析
                          ├─ 分析器分发
                          │   ├─ CircuitLoopAnalyzer（回路图） ← V8 GeometryGraph
                          │   ├─ TerminalStripAnalyzer（端子排图）
                          │   └─ CableScheduleAnalyzer（电缆清册）
                          └─ 批量 SQLite 写入
                              ↓
                         LayoutStage.run()
                          └─ [仅屏面布置图] build_layout_tree()
                               └─ upsert_panel_layout (SQLite)
                               
                           tools/cable_match_viewer/
                            └─ GET /api/document/{hash}/layout
```

`TopologyStage.run()`（`cable_engine/graph/builder.py`）：

1. **分类**：通过 `CompositeClassifier` 分类文档。
2. **删除**：删除此文档的现有拓扑 + 柜体数据。
3. **柜体分析**（仅回路图）：
   - `CabinetRegionAnalyzer.analyze()` 检测虚线矩形边界。
   - `assign_terminals_to_cabinets()` 将终端 ATTRIB 分配到柜体。
   - 将 `CabinetRegion` IR 实体注入文档。
4. **分析器分发**：通过 `_ANALYZERS_BY_TYPE` 字典。
5. **批量持久化**：通过 `executemany`。

## 4. 文档 IR

| 实体 | 来源 | 字段 |
|------|------|------|
| `TextEntity` | DWG TEXT/MTEXT | text, x, y |
| `LineGeometry` | DWG LINE/LWPOLYLINE | points, layer, handle, custom_fields.ltype |
| `CircleGeometry` | DWG CIRCLE | center, radius |
| `ArcGeometry` | DWG ARC | center, radius, angles |
| `BlockRef` | DWG INSERT | name, insert_point, rotation, scale |
| `AttributeEntity` | DWG ATTRIB | tag, text, x, y |
| `CabinetRegion` | 推导自分析器 | id, bbox, name, location, display_name, ltype |

## 5. 存储模式

### `cable_topology`

| 列 | 类型 | 描述 |
|----|------|------|
| cable_id | TEXT | 电缆 ID |
| conductor_no | INT | 芯线编号 |
| strip_name | TEXT | 端子排名称 |
| terminal_no | INT | 本地端子编号 |
| terminal_no_remote | TEXT | 远程端子 |
| cabinet_name | TEXT | 本地柜体显示名称 |
| cabinet_name_remote | TEXT | 远程柜体显示名称 |
| circuit_desc | TEXT | 回路描述 |
| loop_id | TEXT | 回路代码 |
| source_type | TEXT | `circuit_loop` / `terminal_strip` / `cable_schedule` |
| document_hash | TEXT | 来源 DWG 指纹 |

### `cable_info`（V8）

| 列 | 类型 | 描述 |
|----|------|------|
| cable_id | TEXT | 电缆 ID（PK） |
| document_hash | TEXT | 来源 DWG 指纹（PK） |
| wire_type | TEXT | 电缆型号及截面，如 `ZBN-KYJYP2-23-1kV-4x6` |

每电缆每文件一行。由 `CircuitLoopAnalyzer.analyze()` 通过空间匹配 WIRECODE→最近 WIRETYPE ATTRIB 填充。

### `cabinets`, `cabinet_terminals`, `text_entities`

标准化模式，支持柜体包含关系和全文搜索。

## 6. V8 GeometryGraph 架构

### 6.1 图结构（语义无关）

V8 用**纯几何图**（`cable_engine/electrical/`）替代了过程式的 `_cabinet_path_trace()`。图中没有任何业务语义——不包含 Terminal、Cabinet、Device 概念：

```
GeometryGraph
├── nodes: dict[int, GeoNode]      — (x, y, node_type, 可选 tag_name/tag_text)
├── edges: dict[int, GeoEdge]      — (node_a, node_b, edge_type, length)
├── adj: dict[int, list[(neighbor_id, edge_id)]]  — 邻接表
└── spatial: SpatialIndex           — 网格空间索引
```

**节点类型**（`GeoNodeType`）：

| 类型 | 说明 |
|------|------|
| `TAG` | ATTRIB 文本（tag_name=NO, ObjTerm.Name, WireSerial…） |
| `TEXT` | 普通 TEXT/MTEXT 节点 |
| `CIRCLE` | 端子图标标记（CIRCLE 实体） |
| `WIRE_VERTEX` | 线段端点 |

**边类型**（`GeoEdgeType`）：

| 类型 | 说明 |
|------|------|
| `SEGMENT` | 两个 WIRE_VERTEX 之间的原始线段（来自 GeometryBuilder） |
| `CONDUCTING` | 从 SEGMENT 升级的导线段（由 WireBuilder 升级） |
| (未来) | CONTAINMENT, LOGICAL |

### 6.2 构建管线

```
GeometryBuilder.build(doc)  ──  nodes (TAG, TEXT, CIRCLE, WIRE_VERTEX)
                                   + SEGMENT edges
          │
          ▼
      merge_close_nodes(tol=0.5)     ← 将 WIRE_VERTEX 合并到重合的 TAG
          │
          ▼
      CIRCLE→WIRE_VERTEX 边          ← SEGMENT 类型, 2 单位半径
          │
          ▼
WireBuilder.run()                    ← 将 SEGMENT 升级为 CONDUCTING
          │
          ▼
CabinetBuilder.run()                 ← 注入柜体节点
```

**`GeometryBuilder.build()`**（`cable_engine/electrical/geometry_graph.py`）：

1. **第一遍 — TAG 节点**：所有 `tag∈{'NO', 'ObjTerm.Name', 'WireSerial', …}` 的 AttributeEntity → `GeoNodeType.TAG`，保留 `tag_name`、`tag_text`。
2. **第一遍 b — TEXT 节点**：其余 TextEntity/AttributeEntity → `GeoNodeType.TEXT`。
3. **第二遍 — CIRCLE 节点**：所有 CircleGeometry → `GeoNodeType.CIRCLE`。
4. **第三遍 — WIRE_VERTEX + SEGMENT 边**：`_process_line` 逐段处理：
   - 每条 LWPOLYLINE 的每段独立评估（不再是整条多段线处理）。
   - **水平段**：`|dy| ≤ 3` 且 `dx > 2` → 两端 WIRE_VERTEX，中间 SEGMENT 边。
   - **垂直段**：`|dx| ≤ 3` 且 `|dy| > 2` → 两端 WIRE_VERTEX，中间 SEGMENT 边。
   - 移除了整条多段线的 Δy 过滤——使垂直线段得以保留。
5. **`merge_close_nodes(0.5)`**：将重合位置的 WIRE_VERTEX 合并到 TAG 节点中。合并后的节点保留 TAG 类型和原有的导线边。
6. **CIRCLE→WIRE_VERTEX 边**：2.0 单位距离内创建 SEGMENT 边。连接条件检查"是否有 CONDUCTING/SEGMENT 边"（而非节点类型），所以合并后的 TAG 节点（继承了导线边）也能正确连接。

### 6.3 图遍历

Visitor 模式——`trace(start, visitor)` 返回语义无关的 `GraphPath`：

```
GraphPath
├── nodes: list[int]
├── edges: list[int]
├── cost: float
├── stop_node: int
└── reason: TraceStopReason (DEAD_END / VISITOR_STOP / MAX_DEPTH / NO_PATH)
```

`GeometryGraph.trace()`：

1. 从 `start_id` 开始 BFS，在每个节点调用 `visitor.visit(node, depth)`。
2. `visitor.visit()` 返回 `VisitDecision(stop=True/False)`。
3. 当 `stop=True` 时，`_build_path()` 使用父指针重建从起点到停止节点的路径。结果包含节点 ID、边 ID 和累计几何长度（cost）。
4. 路径本身不含业务语义——业务含义由消费者（Resolvers）在查询时赋予。

### 6.4 导线边操作

**`nearest_wire_node(x, y, tol)`**：空间扫描，查找至少有 1 条 SEGMENT 或 CONDUCTING 边的节点。选择容差内最近的节点。

**`find_wire_near(x, y, tol, context_tags=None)`** — 优先基于边的导线查找（V8 关键改进）：

主要策略：**基于边**。扫描所有 CONDUCTING/SEGMENT 边；对每个 `|y_mean - y| ≤ tol` 的边计算 `score = dy + x_outside * 0.1`（x_outside = 查询点 x 到边 x 跨度的距离）。当提供 `context_tags`（WIRECODE/WIRETYPE 位置列表）时，任何 x 跨度包含标签且标签 y 距边 y_mean 40 单位内的边获得 3.0 分奖励——用于区分左右母线距离大致相等的电缆。

返回优胜边的端点（优先非 CIRCLE 端点以便母线行走）。**总是优先**选择边结果而非基于节点的回退——节点回退（`nearest_wire_node`）通常捕获附近的垂直导线，对母线遍历无用。

| 策略 | 方法 | 场景 |
|------|------|------|
| 基于边（主要） | `score = dy + x_outside * 0.1 [-3.0 context 奖励]` | 长水平母线；WS 可能在边 x 跨度之外 |
| 基于节点（回退） | `nearest_wire_node` 按欧几里得距离 | 无匹配的边 |

**`walk_to_endpoint(start_id, direction)`**：

沿度数为 2 的导线链向 x 增加（`'right'`）或 x 减少（`'left'`）方向行走。在分叉点（导线边度数 ≠ 2）处停止并返回该节点。

**`wire_endpoint(start_id, direction)`**：

简化步行——每一步选择方向上的最远邻居。适用于不需要处理分叉点的场景。

### 6.5 端子查询（`ElectricalQuery`）

`ElectricalQuery`（`cable_engine/electrical/query.py`）是高级查询接口：

```python
class ElectricalQuery:
    def find_terminal(wx, wy, side) → Optional[TerminalResult]
```

**算法流程**：

1. `find_wire_near(wx, wy)` — 定位覆盖查询点的母线边。
2. `walk_to_endpoint(wire_id, side)` — 沿度数为 2 的链走到端点。
3. 如果端点是 **CIRCLE** → 直接通过 `TerminalResolver` 解析。
4. 否则，**方向约束 DFS**（`_dfs_to_terminal`）— BFS 向外搜索，仅允许 x 增加（右）或 x 减少（左）的移动（5 单位余量）。遇到 CIRCLE 节点时停止。
5. 回退：在端点位置解析。

`_dfs_to_terminal` 方向约束：

| 侧 | 约束 |
|----|------|
| `'left'` | `nb_x ≤ current_x + 5`（优先 x 递减） |
| `'right'` | `nb_x ≥ current_x - 5`（优先 x 递增） |

### 6.6 端子解析（`TerminalResolver`）

`TerminalResolver.resolve_at(x, y)`（`cable_engine/electrical/resolvers/terminal.py`）：

**两步最近邻搜索**（V8 对比 V7 混合搜索的改进）：

```
第一步：8 单位内最近的 CIRCLE
  ↓ (anchor_x, anchor_y)
第二步：12 单位内最近的 NO/ObjTerm.Name 标签
  ↓ (柜体 bbox 过滤——排除相邻柜体的标签)
第三步：柜体包含查找
  ↓
TerminalResult(number, x, y, cabinet)
```

关键细节：
- **圆圈**：遍历半径内所有 CIRCLE 节点，选择最短欧几里得距离（不是 `circles[0]`）。
- **标签**：同上——根据到圆圈锚点的距离对所有候选标签评分。
- **柜体过滤**：当圆圈锚点落在柜体内时，排除该柜体 bbox 之外的标签。
- **半径**：圆圈搜索 8 单位 → 标签搜索 12 单位（不再在两个地方硬编码）。

### 6.7 图规模（D0202-31.dwg 示例）

| 指标 | 值 |
|------|-----|
| 总节点数 | 1255 |
| CIRCLE | 96 |
| WIRE_VERTEX | 399 |
| TAG | 606 |
| TEXT | 154 |
| 边数 | 421 |
| 输入 LineGeometry | 2095（逐段拆分后 3426 段） |
| 输出导线段 | 274（125 水平 + 149 垂直） |

## 7. TopologyStage — 分析器

### 7.1 CircuitLoopAnalyzer（回路图）

`CircuitLoopAnalyzer.analyze()` 现在构建 GeometryGraph 管线：

```python
def analyze(self, doc: Document) -> list[dict]:
    geo_graph = GeometryBuilder().build(doc)
    WireBuilder(geo_graph).run()
    CabinetBuilder(geo_graph).run()
    query = ElectricalQuery(geo_graph)

    # 预计算电缆型号：匹配 WIRECODE → 最近 WIRETYPE
    cable_wire_type: dict[str, str] = {}
    for each WIRECODE ATTRIB with cable_id:
        for each WIRETYPE ATTRIB with manhattan_dist < 100:
            cable_wire_type[cable_id] = wire_type

    for each cable (grouped by WireSerial ATTRIB at wx, wy):
        wire_type = cable_wire_type.get(cid)
        for each core:
            left  = query.find_terminal(wx, wy, 'left', cable_id=cid)
            right = query.find_terminal(wx, wy, 'right', cable_id=cid)
            # 去重、柜体检测、列文本分类
            → records with wire_type
```

`find_terminal` 现在接受 `cable_id`——由分析器传入——触发 `_get_context_tags(cable_id)` 查找 WIRECODE/WIRETYPE 标签位置。这些位置作为 `context_tags` 传入 `find_wire_near`，用于区分左右母线距离大致相等的电缆（如 5071-506 在 x=-349 介于两个母线列之间）。

`wire_type` 由 `TopologyStage.run()` 在每根电缆的第一芯上持久化到 `cable_info` 表。

旧的 `_cabinet_path_trace()` 保留在源码中作为参考，但不再被调用。

### 7.2 TerminalStripAnalyzer（端子排图）

`TerminalStripAnalyzer.analyze()` 直接操作 Document IR 实体，不涉及 GeometryGraph。这是一个 V5 时代的算法，利用 DWG 中的**扩展实体数据（EED）** 将线条实体按电缆分组，然后从空间布局中提取端子排拓扑。

**算法流程**：

```
TerminalStripAnalyzer.analyze(doc)
│
├─ 阶段 1：实体分离
│   将 LineGeometry → lines 列表，TextEntity/AttributeEntity → texts 列表
│   从包含"端子排图"的文本中查找柜体名称
│
├─ 阶段 2：通过 EED 扫描进行电缆分组
│   对每条包含 EED 且匹配 _CABLE_ID_IN_EED 模式的线条：
│     分类为水平线（|max_y - min_y| < 1.0）或垂直线（|max_x - min_x| < 1.0）
│     按 cable_id 分组 → {cable_id: {horiz: [...], vert: [...]}}
│
└─ 阶段 3：逐电缆分析（_analyze_one_cable）
    对每个 cable_id：
    │
    ├─ 取第一条水平线作为锚点 → h_y
    ├─ 通过 h_y 附近最右侧的 EQUNAME ATTRIB 查找远程柜体名称
    │
    └─ 对每条垂直线（电缆列）：
        ├─ 确定连接端 corner_y（与水平线接触的一端）
        ├─ 确定远端 end_y（另一端）
        ├─ 跳过 |dy| < MIN_VERTICAL_LENGTH（20 单位）的短线
        ├─ 通过 vx ± 5, h_y ± 10 范围内的 NO ATTRIB 查找 conductor_no
        ├─ _collect_texts_along_vertical(vx, corner_y, end_y, texts)
        │   → 3 单位 x 走廊内的文本，按距 end_y 的距离排序
        ├─ _classify_column_text(column_texts)
        │   → (circuit_desc, terminal_no, loop_id, unknown_busi)
        ├─ 如果找到 terminal_no：
        │   _find_strip_name(terminal_x, terminal_y, texts)
        │   → 端子左侧最近的"1"标记
        │   → 该标记左侧最近的端子排名称文本
        └─ 生成 cable_topology 记录
```

**关键辅助函数**：

| 函数 | 用途 |
|------|------|
| `_cable_id_from_eed(eed)` | 从第一个匹配 `^[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}` 的 EED 值提取电缆 ID |
| `_is_horizontal(entity)` | `max(ys) - min(ys) < 1.0` |
| `_is_vertical(entity)` | `max(xs) - min(xs) < 1.0` |
| `_find_conductor_no(vx, h_y, texts)` | `(vx ± 5, h_y ± 10)` 范围内的 NO ATTRIB |
| `_find_remote_cabinet(h_y, texts)` | 最近 h_y 的最右侧 EQUNAME ATTRIB |
| `_find_cabinet_name(texts)` | 包含"端子排图"的文本，去掉前缀 |
| `_collect_texts_along_vertical(vx, from_y, to_y, texts)` | 3 单位 x 走廊内所有文本，按方向排序，排除锚点 |
| `_classify_column_text(texts)` | 按模式匹配将文本分类为 circuit_desc/terminal_no/loop_id/unknown_busi |
| `_find_strip_name(tx, ty, texts)` | 端子左侧最近的"1"标记，再向左找最近的端子排名称文本 |

**列文本分类**（`_classify_column_text`）：

沿垂直列收集的每个文本按优先级分类：

| 优先级 | 规则 | 分类结果 |
|--------|------|----------|
| 1 | `label.isdigit()` | `terminal_no`（第一个数字获胜） |
| 2 | 匹配 `^[A-Za-z0-9]{2,8}-[A-Za-z0-9]{1,8}$` 或 `^[A-Za-z]\d{1,4}$` 或含 `-` 且长度≥4 | `loop_id`（第一个）或 `circuit_desc`（第二个） |
| 3 | 包含字母字符 | `circuit_desc`（第一个）或 `unknown_busi`（第二个） |
| 4 | 其他 | `unknown_busi`（第一个）或 `circuit_desc`（回退） |

**端子排名称检测**（`_find_strip_name`）：

1. 查找 terminal_y 40 单位范围内文本恰好为 `"1"` 的所有 `TextEntity`。
2. 选取严格位于左侧（`x < terminal_x - 1.0`）的最近"1"标记。
3. 如果未找到（处理 `terminal_no=1` 时端子本身就是排起点的情况），按绝对距离选取最近的"1"。
4. 从"1"标记出发，向左搜索（`x < marker_x - 3`），38 单位 y 范围内查找匹配端子排名称模式的文本（`^(\d{1,2}[A-Za-z]{1,4})$`、`^([A-Za-z]{1,2}\d{1,4})$`、`^([A-Za-z]{1,4})$`、`^(\+?[A-Za-z]{1,3}\d{1,4})$`、`^(\d{1,2}-[A-Za-z]{1,4})$`）。
5. 选取最近的匹配文本 → `strip_name`。

**记录结构**（`cable_topology` 表）：

| 字段 | 来源 | 示例 |
|------|------|------|
| `cable_id` | 来自 EED | `GY6-136` |
| `conductor_no` | 连接处的 NO ATTRIB | 1 |
| `strip_name` | 从"1"标记向左查找 | `1D` |
| `terminal_no` | 列文本中的第一个数字 | 2 |
| `terminal_no_remote` | 始终为 `None` | — |
| `cabinet_name` | 包含"端子排图"的文本 | `1号继电器柜` |
| `cabinet_name_remote` | h_y 附近最右侧的 EQUNAME | `X4` |
| `circuit_desc` | 从列文本分类 | `直流电源+` |
| `loop_id` | 从列文本分类 | `+KZ1` |
| `source_type` | 始终为 `terminal_strip` | `terminal_strip` |

### 7.3 CableScheduleAnalyzer（电缆清册）

电缆清册文档的存根分析器。对清册中每条电缆记录生成单行 `cable_topology` 条目，包含 `cable_id`、`source_type='cable_schedule'`，端子/回路字段为空。查看器使用这些记录构建可浏览的电缆索引。

### 7.4 LayoutStage — 屏面布置图布局树 (V9)

`LayoutStage`（`cable_engine/layout/stage.py`）在 **TopologyStage 之后**运行，仅对 `PANEL_LAYOUT` 分类的文档进行处理。其构建一个分层的 **LayoutTree**（空间包含树，描述屏面布置图的物理结构），以 JSON 格式持久化到 `panel_layout` 表。

```
LayoutStage.run(ctx)
  │
  └─ if ctx.classification.primary == PANEL_LAYOUT
       └─ build_layout_tree(doc)
              ↓
           LayoutTree
           (JSON → SQLite panel_layout)
```

#### 7.4.1 LayoutTree 数据结构

```python
@dataclass
class LayoutTree:
    roots: list[LayoutNode]  # CABINET 和 TABLE 根节点

@dataclass
class LayoutNode:
    id: str
    node_type: LayoutNodeType
    bbox: BBox             # (x, y, w, h) — DWG 左下角原点
    name: str
    children: list[LayoutNode]
    data: dict             # 按类型的元数据（表格行、设备名称等）

class LayoutNodeType(Enum):
    SHEET       = 'SHEET'        # 图幅边界（预留）
    CABINET     = 'CABINET'      # 屏柜（正面或背面）
    PANEL_AREA  = 'PANEL_AREA'   # 设备安装区域（可包含子分组）
    DEVICE      = 'DEVICE'       # 单个设备符号（矩形 + 文本）
    TEXT_BLOCK  = 'TEXT_BLOCK'   # 文本簇（预留）
    TABLE       = 'TABLE'        #（预留——不再生成）
    TITLE_BLOCK = 'TITLE_BLOCK'  # 标题栏（预留）
```
**节点树结构**（V8.2 — 含 GROUP 节点和正/背面标注）：

```

CABINET "1号1000kV继电器小室高抗电能表柜" (face=front，通过标题矩形命名)
  ├── DEVICE "M1 / DTZ178 / 张北I线 / 电抗器 / 本期"
  ├── DEVICE "M2 / DTZ178 / 张北II线 / 电抗器 / 本期"
  ├── DEVICE "M3 / DTZ178 / 预留1"
  └── DEVICE "M4 / DTZ178 / 预留2"

CABINET "" (face=back，无标题矩形，无名)
  ├── PANEL_AREA "" （水平分隔线创建的区域）
  │   ├── GROUP [VERTICAL_COLUMN] "TERMINAL_COLUMN"
  │   │   ├── DEVICE "2D"
  │   │   ├── DEVICE "4D"
  │   │   └── DEVICE "6D"
  │   └── GROUP [VERTICAL_COLUMN] "TERMINAL_COLUMN"
  │       ├── DEVICE "1D"
  │       ├── DEVICE "3D"
  │       └── DEVICE "5D"
  └── DEVICE "GZ11"          （独立设备，未分组）
```

#### 7.4.2 检测算法（`build_layout_tree`）

检测器（`cable_engine/layout/detector.py`）采用 V8.2 **CandidatePool + DBSCAN** 管线：

```
Document IR 实体
    │
    ▼
第1步 — 矩形检测 (detect_rectangles)
    │   LWPOLYLINE（4-5 点）   → 轴对齐矩形
    │   4 条 LINE 段（闭合链） → 轴对齐矩形
    │   输出: list[DetectedRect]
    │
    ▼
第2步 — 长线检测 (detect_long_lines, min_length=50.0)
    │   水平: |dy| < 2.0, 长度 ≥ 50u
    │   垂直: |dx| < 2.0, 长度 ≥ 50u
    │   输出: list[LongLine] (verts, hors)
    │
    ▼
第3步 — 柜体检测 (detect_cabinets)
    │   A. 基于矩形: 面积 > 10,000 u²，高宽比 1.5-5.0
    │      （屏柜高窄比 ~3:1；排除图幅边框 ~0.7）
    │   B. 配对垂直线: 相邻垂直线（dx 140-240，重叠度 > 50%）
    │      开放柜体的回退方案（无闭合矩形时）
    │   C. 合并: 重叠候选时优先基于矩形而非配对垂直线
    │      后处理移除与矩形候选重叠 >50% 的配对垂直线候选
    │   图幅边框排除: 面积 > 图幅 90% → 排除
    │   输出: list[LayoutNode] (type=CABINET)
    │
    ▼
第4步 — 正/背面识别 (_identify_front_back)
    │   在柜体底部下方查找"正面"/"背面"文本，按最近距离匹配
    │   无匹配文本时使用 y 排序回退（上为正面，下为背面）
    │   存入 cab.data.face: 'front' / 'back'
    │
    ▼
第5步 — 逐柜内部结构分析
    │
    ├─ 5a. 柜体内部矩形 (detect_cabinet_interior)
    │     宽度 ≥ 柜体 80% 且 高度 ≤ 15u → 标题矩形
    │     宽度 ≥ 柜体 50% 且 高度 ≥ 柜体 40% → 设备区
    │
    ├─ 5b. 区域检测 (detect_areas_v2)
    │     若找到设备区矩形: 使用其 bbox 作为 PANEL_AREA
    │     否则: 水平分隔线（跨度 ≥ 柜体宽度 50%）
    │     → PANEL_AREA 节点（或无——设备直接挂 CABINET 下）
    │
    └─ 5c. 设备检测与分组 (_apply_grouping_v2)
         对每个 AREA（或整个 CABINET）运行：
          │
          ├─ build_device_candidates
          │   5 级候选生成 + CandidatePool 去重
          │
          │   得分层级（Pipeline 顺序）:
          │     detect_closed_rects    → 0.95  闭合矩形
          │     detect_spine_devices   → 0.75  开放矩形（脊柱匹配）
          │     detect_U_shapes        → 0.70  U 形（3 段，平行端）
          │     detect_L_shapes        → 0.50  L 形（2 段，90° 端接）
          │     detect_text_devices    → 0.40  文本回退（无矩形包围）
          │
          │   去重: CandidatePool 按得分保留，新候选与已有候选
          │     重叠率 >0.2 时丢弃（低分者淘汰）
          │
          ├─ TextAssociator.associate_devices
          │   将文本关联到设备: 最高文本 = name，其余 = description
          │
          ├─ DBSCANClusterer (eps=30, min_samples=2)
          │   对 [cx, cy, w*0.1, h*0.1] 进行聚类
          │   → DeviceGroup[] (VERTICAL_COLUMN / HORIZONTAL_ROW /
          │                     GRID / FREEFORM)
          │
          └─ TextAssociator.associate_groups
               分组标签（区域内的位置描述）
    
第6步 — 语义标注 (_annotate_groups)
    SemanticScoreEngine 融合 5 个证据源:
      LayoutShapeEvidence  ← group_type 枚举
      NamePatternEvidence  ← 设备名称前缀匹配
      DeviceAttrEvidence   ← 子节点 data['attributes']
      TableInfoEvidence    ← table_info 元数据 (P1)
      SpatialEvidence      ← SpatialGraph (暂桩)
    → GroupSemantic {type, confidence, evidence}
    │
    ▼
    LayoutTree { roots: [...] }
```

#### 7.4.3 关键辅助函数 (V8.2)

| 函数 | 模块 | 用途 |
|------|------|------|
| `detect_rectangles(doc)` | `primitives/rectangle.py` | 从 LINE/POLYLINE 查找轴对齐矩形 |
| `detect_long_lines(doc, min_length)` | `primitives/line.py` | 将长线分类为水平/垂直 |
| `detect_cabinets(doc, rects, verts, hors)` | `detectors/cabinet.py` | 柜体边界候选（矩形优先 + 配对垂直线回退） |
| `detect_cabinet_interior(cab, rects)` | `detectors/area.py` | 柜体内部标题矩形和设备区 |
| `detect_areas_v2(doc, cab, hors, interior)` | `detectors/area.py` | 创建设备安装区域（优先内部矩形，分隔线回退） |
| `_identify_front_back(cabinets, doc)` | `detector.py` | 文本匹配正/背面，无匹配时 y 排序 |
| `build_device_candidates(doc, bbox)` | `candidate.py` | 5 级候选生成器（闭合 → 脊柱 → U → L → 文本） |
| `detect_closed_rects(doc, bbox)` | `candidate.py` | 闭合矩形设备（得分 0.95） |
| `detect_spine_devices(doc, bbox)` | `candidate.py` | 脊柱匹配开放矩形（得分 0.75） |
| `detect_U_shapes(doc, bbox)` | `candidate.py` | U 形设备检测（得分 0.70） |
| `detect_L_shapes(doc, bbox)` | `candidate.py` | L 形设备检测（得分 0.50） |
| `detect_text_devices(doc, bbox)` | `candidate.py` | 文本回退设备（得分 0.40） |
| `CandidatePool` | `candidate.py` | 多源候选去重（按得分 + 重叠率） |
| `TextAssociator` | `associator.py` | 文本关联（name/description + 分组标签） |
| `DBSCANClusterer` | `clustering.py` | DBSCAN 空间聚类（eps=30, min_samples=2，委托 structure/* 做后分类） |
| `ColumnAnalyzer` | `structure/column.py` | 列评分（x 对齐 + 尺寸一致性 + 等间距） |
| `RowAnalyzer` | `structure/row.py` | 行评分（y 对齐 + 尺寸一致性 + 等间距） |
| `GridAnalyzer` | `structure/grid.py` | 网格评分（cols×rows 完整性） |
| `detect_table_regions` | `table/detector.py` | 基于矩形的设备表区域检测 |
| `parse_table_at` | `table/parser.py` | 表格行/列/表头解析 |
| `match_to_devices` | `table/matcher.py` | 表行 → DeviceCandidate 匹配 |
| `lift` | `spatial/bridge.py` | LayoutTree → SpatialGraph 提升 |
#### 7.4.4 存储

布局树序列化为 JSON（`LayoutNode` 数据类 → `asdict()` → JSON），存储在 `panel_layout` 表中：

| 列 | 类型 | 描述 |
|----|------|------|
| `document_hash` | TEXT | 来源 DWG 指纹（PK） |
| `layout_json` | TEXT | LayoutTree 的 JSON 序列化 |
| `created_at` | TEXT | ISO 8601 时间戳 |

```
Store 方法:
  upsert_panel_layout(hash, layout_tree)  → INSERT OR REPLACE
  get_panel_layout(hash)                  → LayoutTree（反序列化）
  delete_panel_layout(hash)               → DELETE
  has_panel_layout(hash)                  → bool
```

#### 7.4.5 查看器集成

布局树通过查看器的 REST API 提供，在客户端渲染为面向柜体的树形视图：

```
GET /api/document/{hash}/layout
  → JSON { roots: [...] }

renderLayoutTree(layout)  → HTML
  └─ CABINET 树视图：
        正面柜 → 柜体名称
        背面柜 → "背面"
        每个 PANEL_AREA（子分组）→ 橙色标签嵌套
         DEVICE → 设备名称标签
         GROUP  → 分组颜色（紫色）、语义标签、网格尺寸、位置标签
```

### 7.5 V9 LayoutGroup — 设备分组

V8.2 在 CABINET/PANEL_AREA 与 DEVICE 之间引入 **GROUP** 节点层。GROUP 节点表示空间排布模式——例如垂直排列的端子列、顶部一行设备或 2×2 电表网格。

```
CABINET (屏柜)
  ├── PANEL_AREA (安装区域)
  │   ├── GROUP [VERTICAL_COLUMN] "TERMINAL_COLUMN"   ← V8.2 GROUP
  │   │   ├── DEVICE "2D"
  │   │   ├── DEVICE "4D"
  │   │   └── DEVICE "6D"
  │   └── GROUP [GRID] "METER_GRID"                   ← V8.2 GROUP
  │       ├── DEVICE "M1"
  │       ├── DEVICE "M2"
  │       ├── DEVICE "DH1"
  │       └── DEVICE "DH2"
  └── DEVICE "GZ11" (独立设备，未分组)
```

#### 7.5.1 数据结构

`model.py` 中的类型：

| 类型 | 说明 |
|------|------|
| `LayoutNodeType.GROUP` | 表示空间设备集群的节点类型 |
| `LayoutGroupType` | 排布模式枚举：`VERTICAL_COLUMN` / `HORIZONTAL_ROW` / `GRID` / `FREEFORM` |

`LayoutNode` 可选字段 `group_type: Optional[LayoutGroupType]`，仅对 `node_type == GROUP` 的节点有意义。

`node.data` 中存储的元数据：

| 键 | 值类型 | 说明 |
|----|--------|------|
| `score` | float | 模式匹配置信度（0.0–1.0） |
| `evidence` | list[str] | 匹配证据（如 `x_align`, `spacing_std:0.0`） |
| `position` | str | 在柜体中的位置（`left`, `right`, `top`, `bottom`, `center`） |
| `grid_dims` | dict | 仅 GRID：`{cols: N, rows: N}` |
| `group_semantic` | dict | GroupSemanticResolver 输出的语义类型 |

#### 7.5.2 DBSCAN 空间聚类

`_apply_grouping_v2()` 使用 sklearn DBSCAN 替代 V8.0/V8.1 的包围盒分组和 V8.2 原型的扫描分组法（`grouping/` 已删除）：

```
_apply_grouping_v2(parent, cab_bbox, doc)
  │
  ├─ build_device_candidates(doc, container)
  │   → 5 级候选（闭合 0.95 → 脊柱 0.75 → U 0.70 → L 0.50 → 文本 0.40）
  │   → CandidatePool 去重（重叠 >0.2 时低分淘汰）
  │
  ├─ TextAssociator.associate_devices
  │   → 每个候选获得 name + description
  │
  ├─ DBSCANClusterer.cluster(candidates, cab_bbox)
  │   特征向量: [cx, cy, w*0.1, h*0.1]
  │   参数: eps=30, min_samples=2
  │   │
  │   ├─ DBSCAN 聚类
  │   ├─ 后分类: 每组按几何特征分类为
  │   │   VERTICAL_COLUMN / HORIZONTAL_ROW / GRID / FREEFORM
  │   │   评分: x/y 对齐 + 尺寸一致性 + 等间距
  │   └─ _score_column: 按 cy 降序排序计算间距
  │       （修复: 排序前为未排序导致标签文本产生负间距）
  │
  ├─ TextAssociator.associate_groups
  │   → 分组位置标签（left / right 等）
  │
  └─ _build_group_node / _build_device_node
      → 未分组设备直接作为 DEVICE 节点
      → 已分组设备归入 GROUP 节点
```

**DBSCAN 关键参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `eps` | 30.0 | 邻域半径（DWG 单位） |
| `min_samples` | 2 | 形成核心点所需最少样本数 |

**特征向量设计**：

使用 `[cx, cy, w*0.1, h*0.1]` 而非原始 `[cx, cy]`：
- w/h 缩放因子 0.1 防止尺寸差异主导聚类（CAD 设备尺寸范围 6u–30u）
- 尺寸信息帮助分离相邻但尺寸不同的设备列

**后分类流程（委托给 Structure Analyzers）**：

DBSCAN 产生无标签簇 → 每簇委托给独立的结构分析器评分：
1. **`GridAnalyzer`**：cols×rows 完整填充检查（得分 ≥0.40 → GRID）
2. **`ColumnAnalyzer`**：x 对齐 + 宽度一致性 + 高度一致性 + 等间距（≥0.40 → VERTICAL_COLUMN）
3. **`RowAnalyzer`**：y 对齐 + 高度一致性 + 宽度一致性 + 等间距（≥0.40 → HORIZONTAL_ROW）
4. 均不达标 → **FREEFORM**

检查顺序：GRID → COLUMN → ROW → FREEFORM。GRID 优先防止 2×2 网格被误拆为列。
结构分析器独立于 `clustering.py`，存储在 `structure/` 包中，便于扩展（如 ladder、symmetry）。

#### 7.5.3 语义分类

`GroupSemanticResolver`（`semantics/group_type.py`）为 GROUP 节点分配语义类型：

| 语义类型 | 说明 | 触发条件 |
|----------|------|----------|
| `TERMINAL_COLUMN` | 端子列 | VERTICAL_COLUMN 模式（+0.2）+ 子设备名称匹配 `{N}D` 模式（2D、4D 等） |
| `METER_GRID` | 电表网格 | GRID 模式（+0.2）+ 子设备名称匹配 M{1-8} 或 DH{1-6} |
| `DEVICE_PANEL` | 设备面板 | HORIZONTAL_ROW 模式（+0.1） |
| `METER_GROUP` | 电表组 | 名称匹配 DTZ/DDZ/DSZ 前缀 |
| `RELAY_GROUP` | 继电器组 | 名称匹配 DK/ZDK/ZDF/GZ 前缀 |
| `MODULE_GROUP` | 模块组 | 名称匹配 FA/FU 前缀 |

评分机制按 `weight × match_ratio` 累加，最高分胜出。

#### 7.5.4 存储与序列化

`LayoutStage._node_to_dict()` 为 GROUP 节点序列化 `group_type` 字段：

```json
{
  "id": "dbscan_0",
  "type": "GROUP",
  "group_type": "VERTICAL_COLUMN",
  "name": "",
  "bbox": {"x": 50, "y": 10, "w": 30, "h": 120},
  "children": [...],
  "data": {
    "score": 0.9,
    "evidence": ["x_align", "w_consist", "h_consist", "count:6", "left_edge"],
    "position": "left",
    "group_semantic": {
      "type": "TERMINAL_COLUMN",
      "confidence": 0.6,
      "evidence": ["layout:VERTICAL_COLUMN", "TERMINAL_COLUMN:6/6"]
    }
  }
}
```

#### 7.5.5 查看器渲染

`tools/cable_match_viewer/server.py` 中的 `renderLayoutTree` 函数将 GROUP 节点渲染为：

| 视觉元素 | 说明 |
|----------|------|
| 紫色边框（`#7b1fa2`） | 与 CABINET（蓝色）、PANEL_AREA（橙色）、DEVICE（灰色/浅蓝）区分 |
| 语义标签 + 位置 | 如 `METER_GRID`，位置附加 `left`, `top`, `right`, `bottom`, `center` |
| 网格尺寸 | GRID 节点显示 `2×2` |
| 分组评分 | 数据中的 `score.round(2)` 作为可信度提示 |
| 正/背面标识 | 使用 `cab.data.face` — `front` = 柜体名称, `back` = "背面" |

### 7.6 V9 TableParser — 设备表解析器

`layout/table/` 包从 PANEL_LAYOUT 图纸右侧的**设备表（材料表）**中提取结构化行数据，并将业务元数据（型号、说明、数量）注入到匹配的 DeviceCandidate 中。

#### 7.6.1 数据模型

```
TableArea
├── bbox: BBox                          — 表格所在区域
├── rows: list[TableRow]                — Header + data 行
├── header_row: Optional[TableRow]      — 表头行（含中文关键词）
├── header_columns: list[str]           — 表头文本
├── name_column_index: int              — 设备名称列索引
├── model_column_index: int             — 型号列索引
├── desc_column_index: int              — 说明列索引
└── qty_column_index: int               — 数量列索引

TableRow
├── cells: list[TableCell]              — 单元格（按 col_index）
├── y: float                            — Y 位置（CAD 坐标）
└── header: bool                        — 是否为表头行

TableCell
├── text: str                           — 单元格文本
├── x: float                            — X 位置
├── col_index: int
└── row_index: int
```

#### 7.6.2 解析管线

```
detect_table_regions(doc, container)
  │   在容器内查找 ≥60w×80h 的矩形，且包含 ≥4 个文本实体
  │   → list[BBox]
  ▼
parse_table_at(doc, table_bbox)
  │   收集矩形内的文本
  │   → Y 聚类（_ROW_TOL=3.0）分组到行
  │   → 每行按 X 排序
  │   → 查找含中文关键词（序号/名称/型号/说明/数量）的表头行
  │   → 映射列角色（name/model/desc/qty/index/position）
  │   → Optional[TableArea]
  ▼
match_to_devices(table, candidates)
  │   对 TableRow.name_column 中的每个设备名称文本：
  │     查找 name 匹配的 DeviceCandidate
  │     注入 candidate.features['table_info'] = {model, description, qty}
  │   → match_count
```

#### 7.6.3 集成

在 `build_layout_tree` 的逐柜循环中调用 `_detect_equipment_table(doc, cab)`：
- 在柜体右侧 200u 范围内搜索表格矩形
- 找到首个有效表格（`name_column_index ≥ 0`）后传入 `_apply_grouping_v2`
- 在 `TextAssociator.associate_devices` **之后**、DBSCAN **之前**执行 `match_to_devices`
- 使 GroupSemanticResolver 可以访问候选设备的 `features['table_info']`

### 7.7 V9 SpatialGraph — 空间关系图

`layout/spatial/` 包建立了一个与 LayoutTree 平行的**扁平空间关系图**，捕获节点间的几何关系。与 `electrical/` 中的 GeometryGraph 保持严格分离。

#### 7.7.1 数据模型

```
SpatialGraph
├── nodes: dict[str, SpatialNode]       — 节点（包装 LayoutNode）
└── edges: list[SpatialEdge]            — 空间关系边

SpatialNode
├── node_id: str                        — 对应 LayoutNode.id
├── node_type: str                      — LayoutNodeType 值
├── bbox: BBox
├── name: str
└── data: dict

SpatialEdge
├── source_id / target_id               — 节点 ID
├── relation: SpatialRelation            — 关系类型
├── distance: float                     — 间距（单位）
└── confidence: float                   — 置信度
```

**关系类型**（`SpatialRelation`）：

| 关系 | 含义 | 判定条件 |
|------|------|----------|
| `CONTAINS` | 包含 | LayoutTree 父→子边 |
| `LEFT_OF` | 左侧 | 垂直重叠 ≥30%，A 在 B 左侧 |
| `RIGHT_OF` | 右侧 | 垂直重叠 ≥30%，A 在 B 右侧 |
| `ABOVE` | 上方 | 水平重叠 ≥30%，A 在 B 上方 |
| `BELOW` | 下方 | 水平重叠 ≥30%，A 在 B 下方 |
| `ALIGNED_VERT` | 垂直对齐 | |cx差| ≤ 8u，设备在同一列 |
| `ALIGNED_HORZ` | 水平对齐 | |cy差| ≤ 8u，设备在同一行 |
| `NEAR` | 邻近 | 质心距离 ≤ 40u，且无其他关系 |

#### 7.7.2 桥接（`lift(tree)`）

```
lift(LayoutTree) → SpatialGraph
  │
  ├─ 1. 展平 LayoutTree（BFS）
  │     每个 LayoutNode → SpatialNode
  │
  ├─ 2. CONTAINS 边
  │     每个父节点 → 子节点
  │
  ├─ 3. 兄弟节点空间关系
  │     同父节点下的兄弟对：
  │       - 水平重叠 ≥30% → ABOVE / BELOW
  │       - 垂直重叠 ≥30% → LEFT_OF / RIGHT_OF
  │
  └─ 4. 机柜内设备关系
       同一 CABINET 下的所有 DEVICE 对：
         - |cx差| ≤ 8  → ALIGNED_VERT
         - |cy差| ≤ 8  → ALIGNED_HORZ
         - 质心距离 ≤ 40 → NEAR
```

#### 7.7.3 查询接口

```python
graph.query_bbox(bbox: BBox) → list[SpatialNode]    # 空间相交节点
graph.query_near((cx, cy), radius) → list[SpatialNode]  # 邻近节点
graph.neighbors(node_id) → list[(target_id, edge)]   # 邻接点
graph.relations_of(node_id, relation) → list[(target_id, edge)]  # 指定关系
```

### 7.8 P4 SemanticScore — 多证据融合引擎

`semantics/` 包提供**可插拔的证据融合引擎**，替代仅依赖前缀匹配的 `GroupSemanticResolver`，采用可配置的多源评分器。

#### 7.8.1 架构

```
SemanticScoreEngine.fuse(group_node)
  ├─ LayoutShapeEvidence   — LayoutGroupType (0.20/0.10/0.20)
  ├─ NamePatternEvidence   — 设备名称前缀匹配 (weight × ratio)
  ├─ DeviceAttrEvidence    — 子节点 data['attributes']['category'] (≥50%)
  ├─ TableInfoEvidence     — table_info 描述关键词 + 型号前缀
  └─ SpatialEvidence       — SpatialGraph (暂桩，预留扩展)
      │
      ▼  加权求和 → 取最优
   GroupSemantic {type, confidence, evidence_trail}
```

每个 `EvidenceSource` 返回 `{semantic_type: score_contribution}`。引擎通过加权求和融合，选择最优类型，并记录包含所有源贡献的证据链（含非活跃源的 `—` 标记）。

#### 7.8.2 证据源

| 源 | 信号 | 贡献 |
|----|------|------|
| `LayoutShapeEvidence` | VERTICAL_COLUMN → TERMINAL_COLUMN 0.20; GRID → METER_GRID 0.20; HORIZONTAL_ROW → DEVICE_PANEL 0.10 | 固定加值 |
| `NamePatternEvidence` | 设备名称前缀匹配（如 `2D`/`4D`/`6D` → TERMINAL_COLUMN 0.40） | `weight × (匹配数/总数)` |
| `DeviceAttrEvidence` | 子节点 `data['attributes']['category']` 超过 50% 共享同一类别时 | 每类别 0.20 |
| `TableInfoEvidence` | table_info 描述关键词（电能表/继电器/端子）+ 型号前缀（DTZ/DK） | 描述 0.25 + 型号 0.15 |
| `SpatialEvidence` | SpatialGraph 邻接（尚未接入——返回 `{}`） | — |

#### 7.8.3 集成

`GroupSemanticResolver` 现在是 `SemanticScoreEngine` 的薄封装。`annotate_groups(tree)` 函数依旧保持向后兼容。可注入自定义证据源：

```python
from cable_engine.layout.semantics import SemanticScoreEngine, NamePatternEvidence

engine = SemanticScoreEngine(sources=[NamePatternEvidence()])
engine.fuse_tree(group_node)
```

#### 7.8.4 关键文件

- `semantics/evidence.py` — `EvidenceSource` 基类 + 5 个具体实现
- `semantics/fusion.py` — `SemanticScoreEngine` + `GroupSemantic` 数据类
- `semantics/group_type.py` — `GroupSemanticResolver`（现为薄封装）
- `semantics/test_semantic_score.py` — 28 个证据源 + 融合测试

### 7.9 V9 PANEL_POSITION — 屏位布置图

`layout/position/` 包从 PANEL_POSITION 图纸中提取**屏位树**——这类图纸以网格形式布置机柜/框架的 F 编号屏位，并引用一个屏屏用途一览表将 F 编号映射到该位置分配的柜体型号。

#### 7.9.1 处理流程

```
build_position_tree(doc)
  │
  ├─ 1. detect_room(doc)
  │      长水平线（跨度 ≥ F 文本 X 跨度的 60%）定义房间边界。
  │      支持成对线段（左侧 + 右侧有间隔）——按 Y 级别计算联合 X 跨度。
  │      F 编号文本提示近似 X 跨度；多个簇时选择最大/最密组。
  │      → Room（包围框）
  │
  ├─ 2. detect_cells(room)
  │      查找房间包围框内的 F 编号文本（模式：r'^\d+[CF]$'）。
  │      按 Y 聚类为行（_ROW_TOL=8.0）。
  │      每行按 X 排序，分配 label。
  │      检测列组（间隔 ≥ 1.5× 中位数间隔 → 新组）。
  │      → list[PositionRow]（每格带 row_index, col_index, group_index）
  │
  ├─ 3. detect_table_regions（复用 layout/table/detector.py）
  │      查找 ≥60w×80h 且含 ≥4 个文本的矩形。包围框加宽（ex‑200, w=350, h=450）。
  │      通过含 屏屏/用途/一览/编号/名称/数量/备注 的表头文本过滤。
  │
  ├─ 4. parse_usage_table(doc, table_bbox)
  │      标签居中锚定法：在每个 F 编号标签周围搜索 Y 邻域内的兄弟单元格。
  │      基于索引的角色映射——在每半列组内按 X 排序单元格，
  │      将相对索引映射到相同位置的表头角色。
  │      → TableArea（含 屏号/名称/数量/备注 列）
  │
  └─ 5. cross_reference(cells, table_rows)
        通过 F 编号文本匹配单元格 → 注入表格行的设备信息。
        → LayoutTree(ROOM → POSITION_ROW → POSITION_CELL)
```

#### 7.9.2 数据模型

```
PositionCell
├── label: str                       — 屏号（如 "1F", "12C"）
├── bbox: BBox                       — 单元格包围框
├── row_index: int
├── col_index: int                   — 每列组重新计数
├── group_index: int                 — 行内的列组编号
└── equipment: Optional[str]         — 从屏屏用途一览表交叉引用获得

PositionRow
├── cells: list[PositionCell]
├── y: float
└── row_index: int
```

#### 7.9.3 表格解析策略

两列布局（D0201‑05 风格）：
```
[屏号 | 名称 | 数量 | 备注] [屏号 | 名称 | 数量 | 备注]
```
表头通过关键词检测；列半组通过 X 间隔识别。行单元格按 X 排序，左半组索引 0‑3，右半组索引 4‑7。角色通过各半组内的相对索引位置匹配。

单列布局（左表头在包围框内的 D0201‑07）：
```
[屏号 | 名称 | 数量 | 备注 | 屏号 | 名称 | 数量 | 备注]
```
通过第 4、5 列之间的自然文本间隔检测两列拆分。

#### 7.9.4 查看器集成

通过 `cable_match_viewer` 中的 `/api/document/{hash}/position` 提供。客户端 `renderPositionTree()` 渲染分组树视图：

```
ROOM（蓝色标签）
├─ POSITION_ROW
│  ├─ "第 1 列组" 标题（灰色）
│  │  ├─ 1F — equipment_name（单元格盒子，150px，省略号）
│  │  ├─ 2F — ...
│  └─ "第 2 列组" 标题
│     ├─ 12F — ...
```

每个格盒有 `max‑width:150px`、`text‑overflow:ellipsis` 以及显示 `排X列Y组Z` 的 `title` 属性。

#### 7.9.5 边界情况处理

- **成对水平线段**：D0227‑03 的网格水平线绘制为两段（左侧 + 右侧有间隔）——`detect_room` 在检查 60% 跨度过滤前按 Y 级别联合 X 跨度。
- **可变列布局**：`_detect_groups()` 通过 X 间隔 ≥ 1.5× 中位数间隔拆分单元格；`col_index` 每列组重新计数。同时处理单列和拆分列网格。
- **标签居中锚定**：同一行的 CAD 文本可能在 Y 方向分散——以屏号标签为中心搜索 ±6px 可捕捉 Y 分桶会丢失的兄弟单元格。
- **非用途表格**：D0227‑03 右侧为设备材料表（列：名称/单位/数量/备注，无屏号列）——`cross_reference` 返回 0 行但屏位格数据仍可用。

## 8. 柜体语义层

### 处理阶段

| 阶段 | 模块 | 功能 |
|------|------|------|
| 1 — 线型 | `DWGLoader._maybe_set_ltype` | 填充 `LineGeometry.custom_fields['ltype']` |
| 2 — 多段线 | `_find_multi_segment_rects` | 将 4 条虚线 LINE 段分组为闭合矩形 |
| 3 — 虚线矩形 | `_find_dashed_rectangles` | 检测 4 角轴对齐虚线 LWPOLYLINE |
| 4 — 名称匹配 | `_match_boundary_text` | 将边界与最近关键词文本配对 |
| 5 — 包含 | `CabinetGridIndex` | 网格空间索引分配终端到柜体 |
| 6 — 持久化 | `TopologyStage` | 批量写入 `cabinets` + `cabinet_terminals` |

### 匿名块扩展

`DWGLoader._parse_v5` 使用 BLOCK_HEADER 实体列表：

- **第一阶段**：正常发射模型空间实体。
- **第二阶段**：缓冲非 Model_Space BLOCK_HEADER 的实体。
- **第三阶段**：解析 INSERT → BLOCK → BLOCK_HEADER → 缓冲实体，坐标变换发射。

## 9. 性能优化

| 优化 | 影响 | 机制 |
|------|------|------|
| `SpatialIndex` | O(N) → O(k) 最近邻 | 50 单位网格单元索引 |
| 基于边的 `find_wire_near` | 长母线：每根电缆 O(N_edges) | 单次边遍历；x 跨度 + y 匹配 |
| `context_tags` 奖励 | 零额外图遍历区分等距母线 | x 跨度包含 WIRECODE/WIRETYPE 的边获得 3.0 分奖励 |
| `merge_close_nodes` | 节点数量减少约 20-30% | 0.5 单位容差，低 ID 胜出 |
| 批量 SQLite 写入 | I/O 减少约 5-10% | `executemany` |
| 非回路图跳过柜体 | 非回路图文档减少约 15% | 仅对 `CIRCUIT_LOOP` 运行 |

## 10. 文件映射

```
cable_engine/
├── cli.py                       # 入口：scan 子命令
├── classifier/
│   ├── composite.py             # CompositeClassifier
│   ├── keyword.py               # KeywordClassifier
│   ├── geometry.py              # GeometryClassifier
│   └── layout.py                # LayoutClassifier
├── electrical/                  # ← V8: GeometryGraph + 查询
│   ├── __init__.py              # 公开 API 导出
│   ├── geometry_graph.py        # GeometryGraph, GeoNode, GeoEdge,
│   │                            #   GeometryBuilder, SpatialIndex,
│   │                            #   Visitor, GraphPath
│   ├── graph_path.py            # GraphPath, TraceStopReason
│   ├── query.py                 # ElectricalQuery, _dfs_to_terminal
│   ├── builders/
│   │   ├── __init__.py
│   │   ├── wire.py              # WireBuilder (SEGMENT→CONDUCTING)
│   │   └── cabinet.py           # CabinetBuilder (柜体节点)
│   ├── resolvers/
│   │   ├── __init__.py
│   │   └── terminal.py          # TerminalResolver.resolve_at
│   └── visitors/
│       ├── __init__.py
│       └── cabinet_entry.py     # (遗留代码，保留供参考)
├── graph/
│   ├── builder.py               # TopologyStage + 所有分析器
│   ├── cabinet.py               # CabinetRegionAnalyzer + CabinetGridIndex
│   ├── types.py                 # 遗留 DocumentGraph（保留供参考）
│   └── spatial.py               # 遗留空间索引（保留供参考）
├── ir/
│   ├── entities.py              # Entity, CabinetRegion, BBox, Point
│   ├── geometry.py              # LineGeometry, BlockRef, AttributeEntity
│   ├── document.py              # Document, DocumentType
│   └── pdf.py                   # Page, PixelImage（延期）
├── layout/                      # ← 屏面布置图 LayoutTree (V9)
│   ├── __init__.py              # 公开 API 导出（含 LayoutGroupType）
│   ├── types.py                 # LayoutTree, LayoutNode, LayoutNodeType（遗留兼容）
│   ├── model.py                 # LayoutNode, LayoutNodeType, LayoutGroupType（规范）
│   ├── detector.py              # V9 检测管线 + _apply_grouping_v2
│   ├── stage.py                 # LayoutStage（序列化）
│   ├── candidate.py             # DeviceCandidate + 5 级生成器 + CandidatePool
│   ├── associator.py            # TextAssociator（name/description + 分组标签）
│   ├── clustering.py            # DBSCANClusterer（后分类委托 structure/*）
│   ├── cabinet.py               # PhysicalCabinet 包装器
│   ├── test_detector.py         # 30 个单元测试
│   ├── demo.py                  # CLI demo
│   ├── detectors/               # 空间检测模块
│   │   ├── __init__.py          # 初始化
│   │   ├── cabinet.py           # detect_cabinets, 配对垂直线, 合并
│   │   └── area.py              # 区域检测 + 柜体内部结构
│   ├── primitives/              # 矩形、线段原语
│   │   ├── __init__.py
│   │   ├── bbox.py              # BBox 工具函数
│   │   ├── line.py              # detect_long_lines / LongLine
│   │   └── rectangle.py         # DetectedRect / detect_rectangles
│   ├── structure/               # ← V9 空间结构分析器
│   │   ├── __init__.py
│   │   ├── column.py            # ColumnAnalyzer（VERTICAL_COLUMN 评分）
│   │   ├── row.py               # RowAnalyzer（HORIZONTAL_ROW 评分）
│   │   └── grid.py              # GridAnalyzer（GRID 评分）
│   ├── table/                   # ← V9 设备表解析器
│   │   ├── __init__.py
│   │   ├── model.py             # TableArea / TableRow / TableCell
│   │   ├── detector.py          # detect_table_regions
│   │   ├── parser.py            # parse_table_at（文本聚类 + 表头检测）
│   │   └── matcher.py           # match_to_devices（候选注入）
│   ├── spatial/                 # ← V9 空间关系图
│   │   ├── __init__.py
│   │   ├── model.py             # SpatialNode / SpatialEdge / SpatialGraph
│   │   └── bridge.py            # lift(tree) → SpatialGraph
│   └── semantics/               # V9 弱语义标注层 (P4)
│       ├── __init__.py          # 公开导出
│       ├── group_type.py        # GroupSemanticResolver（薄封装）
│       ├── device_type.py       # 设备类型分类
│       ├── evidence.py          # EvidenceSource 基类 + 5 个具体证据源
│       ├── fusion.py            # SemanticScoreEngine 融合引擎
│       └── test_semantic_score.py  # 28 个测试
├── loaders/
│   ├── dwg_loader.py            # dwgread -O JSON + ezdxf 回退
│   └── pdf_loader.py            # pypdfium2（延期）
├── pipeline/
│   ├── stage.py                 # Stage 基类
│   └── __init__.py              # Context + Pipeline
└── storage/
    └── sqlite.py                # CableStore

tools/cable_match_viewer/
├── server.py                    # aiohttp 应用 + HTML UI
└── store.py                     # CableViewer 只读外观
```

## 11. 关键设计决策

| 决策 | 理由 |
|------|------|
| **图是纯几何结构** | GraphPath、GeoNode、GeoEdge 不承载 Terminal/Cabinet/Device 语义。业务映射延迟到查询时由 Resolvers 完成。 |
| **逐段处理线段** | 每条 LWPOLYLINE 的每段独立评估；移除了整条多段线的 Δy 过滤，垂直线段得以保留。 |
| **基于边的 `find_wire_near` 优先** | 当匹配到边时始终优先选择边结果。节点回退（`nearest_wire_node`）通常捕获附近的垂直导线，对母线遍历无用——旧的 `best_edge_dy ≤ node_dy` 比较将边的 y_mean 与节点的 y 位置相比较（苹果 vs 橙子）。 |
| **`context_tags` 分数奖励** | WIRECODE/WIRETYPE 标签位置以 `context_tags` 传入 `find_wire_near`。x 跨度内含标签（40y 内）的边获得 3.0 分惩罚，使母线选择偏向电缆物理正确的母线。修复 5071-506/5072-503 在 x=-349 处左右母线等距的关键问题。 |
| **`context_tags` y 阈值 = 40** | WIRECODE 标签放置在最深母线上方约 30-40 单位处。阈值必须覆盖整个垂直跨度（y=-29 标签 → y=-66 最深母线 = dy 37）。 |
| **方向约束 DFS** | 从母线端点出发，仅跟随 x 增加（右）或 x 减少（左）的移动，5 单位余量。防止穿越母线到达错误的侧柜。 |
| **最近标签解析** | 遍历所有候选 NO/ObjTerm.Name 标签，选择到 CIRCLE 中心欧几里得距离最短的——不是空间查找顺序（网格/插入顺序）。 |
| **按 ID 合并近邻节点** | 低 ID 的 WIRE_VERTEX（在 Pass 3 中创建，晚于 Pass 2 的 CIRCLE/TAG）合并到相同位置的高 ID TAG 中。合并后的节点保留 TAG 类型和导线边——CIRCLE 连接步骤检查"是否有导线边"而非节点类型。 |
| **柜体 bbox 标签过滤** | 当锚点圆圈位于柜体内时，排除该柜体 bbox 之外的标签——防止同一张图纸上相邻柜体的标签被误选。 |
| **LayoutStage 后于 TopologyStage** | 布局树需要先有分类结果。将 LayoutStage 放在第二顺位避免了在检测管线中重复分类。 |
| **基于面积的图幅边框排除** | 图幅边框排除使用面积比（>90%），而非基于尺寸（最大尺寸的 70%）。防止过滤掉占据图幅大部分的合理大型柜体。 |
| **柜体高宽比过滤 (1.5-5.0)** | 柜体正面高窄（h/w≈3:1）；排除图幅边框（h/w≈0.7）和宽内框。 |
| **基于矩形柜体优先于配对垂直线** | 当两种来源产生重叠候选时，合并优先选择基于矩形的；配对垂直线仅作为开放柜体的回退方案。 |
| **CandidatePool 得分层级** | 闭合矩形(0.95) > 脊柱开放(0.75) > U 形(0.70) > L 形(0.50) > 文本回退(0.40)。去重时按重叠率 >0.2 淘汰低分者。脊柱设备得分高于 U 形，确保开放矩形替换合并的 U 形候选。 |
| **脊柱设备检测** | 替代旧版 `_detect_open_rect_devices`。配对短水平线与共享垂直线，生成 DeviceCandidate(0.75)。dbscan 群内左侧有 6 个此类设备（2D-12D），宽度 6u。 |
| **DBSCAN 特征向量含缩放尺寸** | `[cx, cy, w*0.1, h*0.1]` 中 w/h 缩放因子 0.1 防止尺寸差异主导聚类。相邻而尺寸不同的设备列可被分离。 |
| **后分类一致性检查严于 DBSCAN** | DBSCAN 宽聚（eps=30），后分类筛选（w/h 差 ≤8/6u）。标签文本（如"左侧"宽 20u）与脊柱设备（宽 6u）在同一簇时，尺寸差导致 FREEFORM 回退。 |
| **文本匹配正/背面识别** | 在柜体底部下方查找"正面"/"背面"文本（dx ≤ 宽度 60%，dy ≤ 200u）。替代旧版 y 排序法，修复 D0206-20 两柜 y 相同的问题。 |
| **无文档级表格检测** | 已移除。专注于前后柜面及其设备矩形。 |
| **V8.2 GROUP 节点介于 AREA 与 DEVICE 之间** | GROUP 是空间集群节点，携带 `group_type`（排布模式）和语义类型。不改动现有 CABINET/AREA/DEVICE 层级。 |
| **DBSCAN 替代扫描聚类** | 旧版扫描聚类（`grouping/` 目录）已删除。DBSCAN(eps=30) 无需显式 GRID→COLUMN→ROW 阶段顺序，后分类逻辑分配模式类型。 |
| **GroupSemanticResolver 评分制** | 每个信号贡献权重分数，最高分胜出。证据列表可追溯决策过程。 |
| **V9: Structure Analyzers 替代内联评分函数** | `_score_column`、`_score_row`、`_check_grid` 从 `clustering.py` 提取为独立类（ColumnAnalyzer/RowAnalyzer/GridAnalyzer），统一 analyze(cxs,cys,widths,heights,cab_bbox) 接口。后分类顺序 GRID→COLUMN→ROW→FREEFORM。新增分析器可以即插即用。 |
| **V9: DBSCAN 降级为邻近发现** | DBSCAN 只回答"哪些设备彼此靠近"；结构分析器回答"它们处于什么空间模式"。两者职责分离。 |
| **V9: TableParser 注入业务元数据** | 设备表（序号/名称/型号/数量）的文本行通过名称列匹配注入 DeviceCandidate.features['table_info']。运行在 associate_devices 之后、DBSCAN 之前，使语义解析器可利用型号/描述信息。 |
| **V9: SpatialGraph 独立于 GeometryGraph** | GeometryGraph（electrical/）处理原始几何实体（线段、圆、TAG）；SpatialGraph 处理布局节点（CABINET/GROUP/DEVICE）。两个图保持严格分离，不混合。 |
| **V9: 表格检测参数化** | 最小表格尺寸 60w×80h、最少 4 个文本、搜索柜体右侧 200u 范围。表头通过中文关键词匹配（regex），列角色通过关键词分类。 |

## 12. 已知限制

### 回路图 / 通用

- **仅回路图填充 cable_info**：`cable_info`（型号及截面）仅由 `CircuitLoopAnalyzer`（回路图）填充。`TerminalStripAnalyzer` 和 `CableScheduleAnalyzer` 不产生 `wire_type`。
- **设备柜右端子缺失**：右侧进入设备柜（无 CIRCLE 图标）的电缆解析为 RIGHT=None。端子应改为 EQUNAME/EQUCODE 文本标签。
- **仅 DWG**：PDF 支持延期（暂无 RasterizeStage / OcrStage）。
- **匿名块文本**：匿名块内部的 TEXT/ATTRIB 仍不可见。

### 屏面布置图 (PANEL_LAYOUT) — V9

- **柜体检测脆弱**：配对垂直线方法（相邻垂直线 dx 140-240）基于经验，可能漏检非标准宽度或短竖线柜体（如 D0206-20 前柜竖线 65u < 100u 阈值）。
- **设备名称完整性**：TextAssociator 使用最高文本作为 name，其余为 description。如果无关文本恰好在设备上方，会产生杂音名称。
- **背面设备命名**：脊柱设备依赖文本严格位于 bbox 内。如文本标签因浮点偏差略微超出 bbox，该设备将被丢弃（已通过 `_texts_in_bbox` 修复常见情况，边界情况仍可能发生）。
- **DBSCAN 标签文本干扰**：分组标签（如"左侧"）与设备在同一簇内时，其较大宽度（~20u vs 6u）导致一致性检查失败 → FREEFORM 回退。`_classify_group` 通过 cy 降序排序缓解此问题。
- **传感器类型区分**：`detect_closed_rects` 发现所有闭合矩形，但未区分传感器（温湿度/烟雾）与端子排设备。需设备级语义分类。
- **PANEL_POSITION 缺少部分图纸**：部分图纸（如 D0227-03）使用不同的表格格式（无屏号列的设备材料表），表格解析跳过但屏位格数据仍可用。
- **无交叉验证**：布局树独立按图纸生成。没有跨文档验证。
- **GRID 后分类要求完整填充**：GridAnalyzer 要求设备数严格等于 `cols × rows`。非全填充网格（如 2×3 但只有 5 个设备）将被漏检。
- **FREEFORM 回退无模式评分**：DBSCAN 噪声点独立标注，未分组的剩余设备不做聚类。
- **P4 SemanticScore 名称匹配仍为前缀模式**：`NamePatternEvidence` 继承旧版 `GroupSemanticResolver` 的前缀匹配方式，不支持后缀或正则表达式。
- **detectors/device.py 遗留代码**：`detectors/device.py` 中的旧式 `detect_devices`、`_detect_open_rect_devices`、`_merge_devices` 不再被调用。待后续清理。
- **表格检测仅支持矩形边界**：`detect_table_regions` 依赖 `detect_rectangles` 找到的闭合矩形。无矩形边框的表格（如仅由文本网格构成的"隐形表格"）无法检测。
- **SpatialGraph 仅支持布局节点**：SpatialGraph 当前仅从 LayoutTree 节点推导关系，不涉及原始几何实体（线条、圆等）。需 GeometryGraph 的细粒度空间查询暂不支持。
- **O(N²) 设备对枚举未优化**：`_add_device_relations` 枚举 CABINET 内所有设备对。对于大型机柜（>45 设备，1000 对以上），有性能风险。当前以 `_MAX_DEVICE_PAIRS=2000` 为上限保护。
