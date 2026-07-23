# cable_engine 架构文档 (V8.0)

## 1. 系统概述

```
DWG 文件 → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                          ↓
                                                        tools/cable_match_viewer/ (aiohttp)
```

单一 `TopologyStage` 负责文档分类、柜体分析和分析器分发。V8 引入了 **GeometryGraph**（纯几何图结构）来替代 V7 过程式的 `_cabinet_path_trace()` 算法。

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
| `PANEL_POSITION`（屏位布置图） | — | 无（仅查看器 – 分析器待开发） |
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

### 7.4 LayoutStage — 屏面布置图布局树

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
**节点树结构**（正面柜含子分组；背面柜含开放矩形设备）：

```

CABINET "1号1000kV继电器小室高抗电能表柜"（正面 — front face，通过标题矩形命名）
  ├── DEVICE "M1 / DTZ178 / 张北I线 / 电抗器 / 本期"
  ├── DEVICE "M2 / DTZ178 / 张北II线 / 电抗器 / 本期"
  ├── DEVICE "M3 / DTZ178 / 预留1"
  └── DEVICE "M4 / DTZ178 / 预留2"

CABINET ""（背面 — back face，无标题矩形，无名）
  ├── DEVICE "ZDK ... DK4"   （闭合矩形检测）
  ├── DEVICE "1D ... 12D"    （开放矩形，x=-64 脊柱）
  └── DEVICE "GZ11"          （闭合矩形，底部）

```

#### 7.4.2 检测算法（`build_layout_tree`）

检测器（`cable_engine/layout/detector.py`）是一个多阶段空间管线：

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
第2b步 — 全量短线检测 (detect_long_lines, min_length=3.0)
    │   相同逻辑，更细粒度——用于开放矩形设备检测
    │   输出: list[LongLine] (all_verts, all_hors)
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
第4步 — 逐柜分析（无文档级表格检测）
    │  （表格检测已移除——专注于柜面 + 设备）
    │
    ├─ 4a. 柜体内部结构 (_detect_cabinet_interior)
    │     宽度≥柜体80% 且 高度≤15u 的内部矩形 → 标题矩形
    │     宽度≥柜体50% 且 高度≥柜体40% 的内部矩形 → 设备区
    │     拒绝 >柜体 bbox 110% 的矩形（防止图幅边框被误用）
    │
    ├─ 4b. 区域检测 (detect_areas_v2)
    │     若找到设备区矩形: 使用其 bbox 作为 PANEL_AREA
    │     否则: 水平分隔线（跨度≥柜体宽度50%）
    │     → PANEL_AREA 节点（或空——设备直接挂 CABINET 下）
    │
    ├─ 4c. 设备子分组检测 (_detect_device_sub_groups)
    │     查找设备区内部的子矩形（最小30u，≤设备区70%）
    │     每个子矩形 → PANEL_AREA
    │     顶部边缘附近的标签文本 → 分组名称（"左侧"、"右侧"）
    │     过滤: 标签长度 >15 字符的分组被拒绝（避免版权声明）
    │
    ├─ 4d. 设备检测 — 两个来源合并:
    │
    │   来源 A — 闭合矩形 (detect_devices)
    │     小矩形（两侧 3-150u），内部有文本（中心点包含关系）
    │     下界 3.0u 处理窄背板设备（ZDK/DK 系列: 3.5×9）
    │     BlockRef 实体 → 最近的小矩形
    │     多行文本: 矩形内所有文本用 " / " 拼接
    │       如 "M1 / DTZ178 / 张北I线 / 电抗器 / 本期"
    │
    │   来源 B — 开放矩形 (_detect_open_rect_devices)
    │     检测三边 + 共享垂直脊柱的开放矩形设备（常见于背面布置）
    │     算法:
    │       a. 查找柜内短水平线（<50u）
    │       b. 查找长垂直脊柱（≥100u）作为参考边
    │       c. 对每个水平线跨度分组（按四舍五入的起止点 x 分组）:
    │          将脊柱匹配到任一端点 2u 范围内
    │          far_x = 对侧端点
    │          对每对连续的 y 层级（间距 ≥5）:
    │            检查 far_x 处是否存在覆盖该区间的垂直线
    │            使用 x1（四舍五入的跨度起点）创建设备 bbox
    │              以便与跨度边缘位置的文本对齐
    │            通过 _find_device_name_by_text 命名
    │
    │   合并 (_merge_devices):
    │     按 bbox 重叠 >0.4 去重；优先保留有名称的
    │
    │   设备分配:
    │     如有子分组: 通过 _bbox_contains(group, device) 分配
    │     否则: 设备直接挂到 AREA 或 CABINET 节点下
    │
    └─ 结果: CABINET 包含嵌套的 PANEL_AREA > DEVICE 树
    │
    ▼
第5步 — 树组装 (build_layout_tree)
    CABINET 节点 → 树根节点
    ↓
    LayoutTree { roots: [...] }
```

#### 7.4.3 关键辅助函数

| 函数 | 用途 | 核心逻辑 |
|------|------|----------|
| `detect_rectangles(doc)` | 从 LINE/POLYLINE 查找轴对齐矩形 | 闭合 4 点多段线或 4 段 LINE 链 |
| `detect_long_lines(doc, min_length)` | 将长线分类为水平/垂直 | `\|dy\| < 2` → 水平；`\|dx\| < 2` → 垂直 |
| `detect_cabinets(doc, rects, verts, hors)` | 柜体边界候选 | 矩形面积 >10k u² + 高宽比 1.5-5.0；配对垂直线 dx 140-240；矩形优先于配对垂直线；图幅边框 >90% 排除 |
| `_detect_cabinet_interior(cab, rects)` | 柜体内部标题矩形和设备区 | 全宽薄矩形 = 标题；大内部矩形 = 设备区；拒绝 >柜体 bbox 110% 的矩形 |
| `detect_areas_v2(doc, cab, hors, interior)` | 创建设备安装区域 | 优先使用内部矩形；分隔线回退 |
| `_detect_device_sub_groups(doc, area, rects)` | 设备区内部的子矩形 | 最小 30u，≤70% 面积；顶部边缘文本作为标签；过滤 >15 字符标签 |
| `detect_devices(doc, rects, container)` | 在容器内查找设备矩形 | 小矩形（3-150u）+ BlockRef 最近矩形；中心点包含关系 |
| `_detect_open_rect_devices(doc, container, verts, hors)` | 三边+脊柱共享边开放矩形设备 | 短水平线按跨度分组；长垂直脊柱作为参考；远 x 垂直线闭合检查；bbox x 使用四舍五入的 x1 |
| `_merge_devices(a, b)` | 合并两个设备列表，按重叠去重 | 重叠率 >0.4 → 重复 |
| `_find_device_name_by_text(doc, bbox)` | 多行文本聚合 | 矩形内所有文本，从上到下排序，用 " / " 拼接 |
| `_bbox_contains(outer, inner)` | 严格包含关系检查 | `outer.[x,y,w,h]` 完全包含 `inner` |
| `_bbox_contains_center(outer, inner)` | 中心点包含关系 | Inner 的质心在 outer 内（5u 边距） |

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

### 7.5 V8.2 LayoutGroup — 设备分组

V8.2 在 CABINET/PANEL_AREA 与 DEVICE 之间引入 **GROUP** 节点层。GROUP 节点表示空间排布模式——例如垂直排列的端子列、顶部一行设备或 2×2 电表网格。

```
CABINET (屏柜)
  ├── PANEL_AREA (安装区域)
  │   ├── GROUP [VERTICAL_COLUMN] "TERMINAL_COLUMN"   ← V8.2 新增
  │   │   ├── DEVICE "2D"
  │   │   ├── DEVICE "4D"
  │   │   └── DEVICE "6D"
  │   └── GROUP [GRID] "METER_GRID"                   ← V8.2 新增
  │       ├── DEVICE "M1"
  │       ├── DEVICE "M2"
  │       ├── DEVICE "DH1"
  │       └── DEVICE "DH2"
  └── DEVICE "GZ11" (独立设备，未分组)
```

#### 7.5.1 数据结构

`model.py` 新增类型：

| 类型 | 说明 |
|------|------|
| `LayoutNodeType.GROUP` | 表示空间设备集群的节点类型 |
| `LayoutGroupType` | 排布模式枚举：`VERTICAL_COLUMN` / `HORIZONTAL_ROW` / `GRID` / `FREEFORM` |

`LayoutNode` 新增可选字段 `group_type: Optional[LayoutGroupType]`，仅对 `node_type == GROUP` 的节点有意义。

`node.data` 中存储的元数据：

| 键 | 值类型 | 说明 |
|----|--------|------|
| `score` | float | 模式匹配置信度（0.0–1.0） |
| `evidence` | list[str] | 匹配证据（如 `x_align`, `spacing_std:0.0`） |
| `position` | str | 在柜体中的位置（`left`, `right`, `top`, `bottom`, `center`） |
| `grid_dims` | dict | 仅 GRID：`{cols: N, rows: N}` |
| `group_semantic` | dict | GroupSemanticResolver 输出的语义类型 |

#### 7.5.2 空间聚类管线

`cable_engine/layout/grouping/clustering.py` 中的 4 阶段聚类管线替代了 V8.0/V8.1 基于包围盒的纯包含关系。新算法采用**扫描分组法**（sweep-based），无需连通分量初始初筛：

```
detect_layout_groups(devices, cab_bbox)
  │
  ├─ Phase 1 — GRID 检测（对所有未分组设备）
  │   _find_all_grids → 尝试所有设备对作为锚点间距
  │   _match_grid     → 按间距匹配网格对齐的设备
  │   _score_grid     → 聚类 x/y 中心验证 Nx×Ny = total
  │   条件: ≥4 设备，cols≥2，rows≥2，总个数完全匹配
  │
  ├─ Phase 2 — COLUMN 扫描（剩余设备）
  │   _x_sweep        → 按 x 中心分组（tol=4.0）
  │   _split_gap_y    → y 间隙 >40u 处分割
  │   _score_column   → x 对齐 + 等宽 + 等高 + 等间距评分
  │   条件: ≥2 设备，评分 ≥0.4
  │
  ├─ Phase 3 — ROW 扫描（剩余设备）
  │   _y_sweep        → 按 y 中心分组（tol=4.0）
  │   _split_gap_x    → x 间隙 >40u 处分割
  │   _score_row      → y 对齐 + 等高 + 等宽 + 等间距评分
  │   条件: ≥2 设备，评分 ≥0.4
  │
  └─ Phase 4 — FREEFORM 回退（剩余设备，须空间连通）
      _connected_components → 50u 半径半径图 → DFS
      _build_freeform       → 最小 2 设备
      → FREEFORM 组
```

**关键参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `X_TOL` | 4.0 | x 中心对齐容差 |
| `Y_TOL` | 4.0 | y 中心对齐容差 |
| `W_DIFF_TOL` | 8.0 | 宽度一致性容差 |
| `H_DIFF_TOL` | 6.0 | 高度一致性容差 |
| `SPACING_STD_TOL` | 5.0 | 间距标准差容差 |
| `MIN_COUNT` | 2 | 形成分组的最少设备数 |
| `GAP_MAX` | 40.0 | 列/行内最大允许间隙 |
| `SCORE_THRESHOLD` | 0.40 | 形成分组的最少评分 |

**为什么 GRID 先于 COLUMN/ROW**：

GRID 检测在所有未分组设备上运行。如果先运行 COLUMN/ROW，2×2 网格（如 M1/M2/DH1/DH2）会被拆分成两个独立的垂直列（左列 M1-DH1，右列 M2-DH2），失去网格识别能力。

**为什么扫描法而非连通分量**：

V8.1 原型使用连通分量作为初始初筛（半径 50u），但背面柜中左右两列设备（如 1D-5D 列和 2D-12D 列）距离 <100u，被合并为一个连通分量，导致模式分类失败。扫描法通过直接检查 x/y 对齐避免了这一问题。

#### 7.5.3 基于文本的设备回退

`_detect_text_devices()`（`detectors/device.py`）为缺乏包围矩形的文本创建 DEVICE 节点：

```
_detect_text_devices(doc, container)
  │
  ├─ 收集容器内所有 TextEntity / AttributeEntity（≤20 字符）
  ├─ 排除与已有 DEVICE bbox 重复的文本（重叠率 >0.2）
  ├─ 估算 bbox：width = len(text) × 10u，height = 15u
  └─ 生成 source='text' 的 DEVICE 节点
```

该回退捕捉端子排名称（"端子排"、"DH1"、"DH2"）等无闭合矩形包围的文本。

#### 7.5.4 语义分类

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

#### 7.5.5 管线集成

`_apply_grouping()`（`detector.py`）在 `build_layout_tree` 构建完 CABINET → PANEL_AREA → DEVICE 树后运行：

1. 通过 `_collect_leaf_devices(parent)` 收集 PANEL_AREA 或 CABINET 的直接子 DEVICE。
2. 如设备数 ≥3，调用 `detect_layout_groups()` 进行 4 阶段聚类。
3. 将已分组的设备从 parent 中移除，替换为 GROUP 节点。
4. 对 GROUP 节点调用 `annotate_groups()`（GroupSemanticResolver），写入 `node.data['group_semantic']`。
5. `_apply_grouping` 在 PANEL_AREA 级和 CABINET 级分别调用——`_collect_leaf_devices` 仅返回直接子 DEVICE（避免递归进入子 PANEL_AREA）。

#### 7.5.6 存储与序列化

`LayoutStage._node_to_dict()` 为 GROUP 节点序列化 `group_type` 字段：

```json
{
  "id": "group_v",
  "type": "GROUP",
  "group_type": "VERTICAL_COLUMN",
  "name": "",
  "bbox": {"x": 50, "y": 10, "w": 30, "h": 120},
  "children": [...],
  "data": {
    "score": 1.0,
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

#### 7.5.7 查看器渲染

`tools/cable_match_viewer/server.py` 中的 `_renderNodes` 函数将 GROUP 节点渲染为：

| 视觉元素 | 说明 |
|----------|------|
| 紫色边框（`#7b1fa2`） | 与 CABINET（蓝色）、PANEL_AREA（橙色）、DEVICE（灰色/浅蓝）区分 |
| 语义标签 + 位置 | 如 `METER_GRID`，位置附加 `left`, `top`, `right`, `bottom`, `center` |
| 网格尺寸 | GRID 节点显示 `2×2` |
| 分组评分 | 数据中的 `score.round(2)` 作为可信度提示 |

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
├── layout/                      # ← 屏面布置图 LayoutTree (V8.2)
│   ├── __init__.py              # 公开 API 导出（含 LayoutGroupType）
│   ├── types.py                 # LayoutTree, LayoutNode, LayoutNodeType（遗留兼容）
│   ├── model.py                 # LayoutNode, LayoutNodeType, LayoutGroupType（规范）
│   ├── detector.py              # 6 步空间检测管线 + _apply_grouping
│   ├── stage.py                 # LayoutStage（序列化 group_type）
│   ├── cabinet.py               # PhysicalCabinet 包装器
│   ├── test_detector.py         # 15 个单元测试（分组、GRID、文本设备等）
│   ├── demo.py                  # CLI demo
│   ├── detectors/               # 空间检测模块
│   │   ├── __init__.py          # 初始化
│   │   └── device.py            # detect_devices, open-rect, BlockRef, text-device、merge
│   ├── grouping/                # ← V8.2 设备空间聚类
│   │   ├── __init__.py          # 导出 DeviceSpatialGraph, detect_layout_groups
│   │   ├── clustering.py        # 4 阶段扫描聚类（GRID→COLUMN→ROW→FREEFORM）
│   │   └── spatial_graph.py     # DeviceSpatialGraph 空间索引
│   ├── primitives/              # 矩形、线段原语
│   │   ├── __init__.py
│   │   ├── bbox.py              # BBox 工具函数
│   │   ├── line.py              # detect_long_lines / LongLine
│   │   └── rectangle.py         # DetectedRect / detect_rectangles
│   └── semantics/               # ← V8.2 弱语义标注层
│       ├── __init__.py          # 初始化
│       └── group_type.py        # GroupSemanticResolver + 语义模式
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
| **设备区矩形必须在柜体内** | 内部区域分析拒绝 >柜体 bbox 110% 的矩形，防止图幅边框被用作小型柜体的设备区。 |
| **设备最小尺寸 3.0u** | 从 8u 下调以处理窄背板设备（ZDK/DK 系列：3.5×9）。正面设备（17×28）不受影响。 |
| **开放矩形设备检测** | 与长垂直脊柱共享一条边的设备（3 条绘制边 + 脊柱作为第 4 条），通过短水平线 + far-x 垂直线检测。覆盖背面布置中设备沿公共垂直分隔线绘制的场景。 |
| **开放矩形 bbox x 使用四舍五入的 x1** | bbox 原点使用四舍五入的水平跨度起点（x1）而非原始脊柱坐标（sx）。修复了当脊柱坐标（-63.78）导致 bbox 位于文本锚点（-63.85）左侧时文本超出 bbox 的问题。 |
| **无文档级表格检测** | 已移除。专注于前后柜面及其设备矩形。设备表将在需要时重新添加。 |
| **设备子分组检测** | 设备区内部的子矩形被检测为设备分组（PANEL_AREA 子节点）。子矩形顶部边缘附近的文本标签成为分组名称（"左侧"、"右侧"）。标签 >15 字符的分组被过滤（避免版权声明）。 |
| **多行设备名称聚合** | 设备边界框内的所有文本实体用" / "分隔符合并。一个设备可能有多行文本（如"M1"+"DTZ178"+"张北I线"），共同标识它。 |
| **V8.2 GROUP 节点介于 AREA 与 DEVICE 之间** | GROUP 是空间集群节点，携带 `group_type`（排布模式）和语义类型。不改动现有 CABINET/AREA/DEVICE 层级，通过 `parent.children` 插入。 |
| **扫描法聚类优先于连通分量** | 连通分量在左右列设备距离 <100u 时合并它们，导致模式分类失败。扫描法（x-sweep、y-sweep）直接检查对齐，不受相邻列干扰。 |
| **GRID 先于 COLUMN/ROW** | 若先运行 COLUMN 扫描，2×2 网格（M1-M2-DH1-DH2）被拆分为两列。先运行 GRID 可将完整网格作为单个 GROUP 保留。 |
| **`min_count=2` + `SCORE_THRESHOLD=0.40`** | 覆盖 2 设备列/行（如 1D-3D 两段列）和 2×2 网格。较高的阈值（如 0.50）会漏检 3 设备列（1D-3D-5D 评分 ≈0.45）。 |
| **`GAP_MAX=40.0` 间隙分割** | 防止非同一列的设备（如 GZ11 距 1D-5D 列 ~100u）被归入同一列。 |
| **`_detect_text_devices` 仅作回退** | 基于矩形的设备检测优先。文本设备仅在文本插入点未被现有矩形设备包围时创建，避免重复。 |
| **`_collect_leaf_devices` 仅返回直接子 DEVICE** | 防止 `_apply_grouping` 在 AREA 级和 CABINET 级重复分组同一设备。AREA 级只分组本 AREA 的直接子设备，CABINET 级只分组直接挂在 CABINET 下的设备。 |
| **GroupSemanticResolver 评分制** | 与 DeviceSemanticResolver 一致——每个信号贡献权重分数，最高分胜出。证据列表可追溯决策过程。 |

## 12. 已知限制

### 回路图 / 通用

- **仅回路图填充 cable_info**：`cable_info`（型号及截面）仅由 `CircuitLoopAnalyzer`（回路图）填充。`TerminalStripAnalyzer` 和 `CableScheduleAnalyzer` 不产生 `wire_type`。
- **设备柜右端子缺失**：右侧进入设备柜（无 CIRCLE 图标）的电缆解析为 RIGHT=None。端子应改为 EQUNAME/EQUCODE 文本标签。
- **仅 DWG**：PDF 支持延期（暂无 RasterizeStage / OcrStage）。
- **匿名块文本**：匿名块内部的 TEXT/ATTRIB 仍不可见。

### 屏面布置图 (PANEL_LAYOUT)

- **柜体检测脆弱**：配对垂直线方法（相邻垂直线 dx 140-240）基于经验，可能漏检非标准宽度或仅用矩形绘制的柜体。
- **子分组依赖子矩形**：如果图纸使用视觉排布（设备组周围无显式矩形），`_detect_device_sub_groups` 将返回空，设备直接挂在主柜体节点下。
- **设备名称完整性**：多行文本聚合合并设备边界框内的所有文本。如果无关文本恰好空间重合，会产生杂音名称。
- **背面设备命名**：开放矩形设备依赖 `_find_device_name_by_text`，要求文本严格位于 bbox 内。如文本标签因浮点偏差略微超出 bbox，该设备将被丢弃（常见情况已通过使用四舍五入跨度 x1 修复，边界情况仍可能发生）。
- **无基于文本的设备分组**：当前算法完全依赖包含关系几何（矩形套矩形）。不通过文本相似度、邻近聚类或标签关联来分组设备。
- **PANEL_POSITION 分析器缺失**：屏位布置图已分类但尚无空间分析器。
- **无交叉验证**：布局树独立按图纸生成。没有跨文档验证（例如验证同一设备在正面/背面视图中一致出现）。

### V8.2 LayoutGroup

- **短母线列**：当水平母线线未跨越完整距离时，远程侧端子可能无法找到（超出 x_tol）。
- **仅 x=-349.3 列检测到母线**：目前 `_cabinet_path_trace` 仅在 30 单位范围内找到母线——仅一列符合条件。
- **GRID 检测要求完整填充**：网格设备数必须严格等于 `cols × rows`。非全填充网格（如 2×3 但只有 5 个设备）将被漏检。
- **FREEFORM 回退无评分**：连通分量回退分组不进行模式评分。任何 50u 半径内 ≥2 设备均被分组。
- **基于文本的设备尺寸粗糙**：`width = len(text) × 10u, height = 15u` 是经验值，非精确尺寸。长文本（>20 字符）被跳过。
- **语义分类仅匹配前缀**：`GroupSemanticResolver` 依赖设备名称前缀（`2D`、`DTZ`、`DK` 等），不支持后缀匹配或正则表达式。
- **无跨设备属性关联**：`Signal 3`（子设备属性）在当前场景下几乎为空——设备在分组时尚未被 `DeviceSemanticResolver` 标注。需管道重新排序才能生效。
