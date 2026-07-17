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
| `PANEL_LAYOUT`（屏位布置图） | 48 | 无（仅查看器） |
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

## 7. TopologyStage（V8 CircuitLoopAnalyzer）

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

## 12. 已知限制

- **仅回路图填充 cable_info**：`cable_info`（型号及截面）仅由 `CircuitLoopAnalyzer`（回路图）填充。`TerminalStripAnalyzer` 和 `CableScheduleAnalyzer` 不产生 `wire_type`。
- **设备柜右端子缺失**：右侧进入设备柜（无 CIRCLE 图标）的电缆解析为 RIGHT=None。端子应改为 EQUNAME/EQUCODE 文本标签。
- **仅 DWG**：PDF 支持延期（暂无 RasterizeStage / OcrStage）。
- **匿名块文本**：匿名块内部的 TEXT/ATTRIB 仍不可见。
