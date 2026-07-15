# cable_engine 架构文档 (V7.0)

## 1. 系统概述

```
DWG 文件 → DWGLoader (dwgread -O JSON) → Document IR → TopologyStage → cable.db (SQLite)
                                                                               ↓
                                                             tools/cable_match_viewer/ (aiohttp)
```

单一 `TopologyStage` 替代了 V4/V5 的管线架构。

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
                         │   ├─ CircuitLoopAnalyzer（回路图）
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

## 6. CircuitLoopAnalyzer — 柜体路径追踪 (V7.0)

### 概述

V7.0 将所有 V6 回退方法（图标 y-bucket 搜索、几何路径追踪、圆圈搜索、端点回退、图标分组、文本 y-bucket）替换为**单一**算法：`_cabinet_path_trace()`。

### 算法流程

```
对于每个芯线（WireSerial 位置 wx, wy）：
  1. 在 ±30mm 范围内找到 core line（水平母线）
     - 第一遍：x 跨度覆盖 wx 的线条
     - 第二遍：x_min ≥ 200 且跨度 ≥ 50mm 的线条
  2. _cabinet_path_trace(side)：
     a. 收集 wy 附近的柜体垂直边
     b. 候选水平线评分：(crosses_cabinet_edge, spans_wx, dy)
     c. 选择最佳线条 → 指定侧的端点
     d. 在端点附近寻找端子图标（圆圈或 TERNO/BL/BR）
     e. 若无图标，沿 90° 转角（垂直线段）到另一端
     f. 若垂直端无图标，在另一端搜索水平线
     g. 在图标/端点附近寻找最近的 NO/ObjTerm.Name 标签
     h. 返回 (x, y, 文本, 标签类型) 或 None
```

### 关键参数

| 参数 | 值 | 用途 |
|------|-----|------|
| `_Y_TOL` | 30.0 | 水平线垂直搜索范围 |
| `_CROSS_TOL` | 2.0 | 柜体边缘交叉检测容差 |
| `_ENDPOINT_TOL` | 30.0 | WS 到导线端点的最大 x 距离 |
| `y_range` | 30（±15 y 单位） | 标签垂直搜索范围 |
| `x_tol` | 50.0 | 端点到标签的最大 x 距离 |

### 屏柜边界过滤

屏柜边界（虚线 LWPOLYLINE/LINE）在路径追踪前从 `core_lines` 中过滤掉，防止算法将柜体边误认为母线：

```
第一阶段：收集所有 CabinetRegion 的 boundary_handle + bbox 边缘
第二阶段：过滤 core_lines：
  a. handle 匹配柜体边界 → 跳过
  b. 线条 y 匹配柜体 bbox 顶/底 ±0.5 且 x 跨度匹配 → 跳过
```

### 标签侧过滤

标签根据相对于 WireSerial x 位置（`ws_x`）的侧面进行过滤：
- LEFT 侧：仅 `tag_x < ws_x` 的标签
- RIGHT 侧：仅 `tag_x > ws_x` 的标签

这防止了拾取 WS 列另一侧的标签。

## 7. 柜体语义层

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

### 性能优化

| 优化 | 影响 | 机制 |
|------|------|------|
| `CabinetGridIndex` | O(N) → O(1) 柜体查找 | 50 单位网格单元索引 |
| 芯线二分查找 | O(L) → O(log L + 窗口) 每芯 | 预排序线条，`bisect_left/right` |
| 批量 SQLite 写入 | I/O 减少约 5-10% | `executemany` |
| 非回路图跳过柜体 | 非回路图文档减少约 15% | 仅对 `CIRCUIT_LOOP` 运行 |

## 8. 查看器 (`tools/cable_match_viewer/`)

三面板 aiohttp Web UI，端口 8003：

- **左列（电缆列表）**：带搜索/过滤的电缆列表。
- **右列**：电缆详情 — 导体、端子、柜体、源文档。
- **底部覆盖层**：Flyfish CAD 查看器预览 DWG。
- **柜体选项卡**：实时柜体搜索。点击 → 详情面板。"在图纸中查看"使用 SVG 覆盖打开 DWG。

## 9. 文件映射

```
cable_engine/
├── cli.py                       # 入口：scan 子命令
├── classifier/
│   ├── composite.py             # CompositeClassifier
│   ├── keyword.py               # KeywordClassifier
│   ├── geometry.py              # GeometryClassifier
│   └── layout.py                # LayoutClassifier
├── graph/
│   ├── builder.py               # TopologyStage + 所有分析器
│   ├── cabinet.py               # CabinetRegionAnalyzer + CabinetGridIndex
│   ├── types.py                 # 遗留 DocumentGraph
│   └── spatial.py               # 遗留空间索引
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

## 10. 已知限制

- **仅 DWG**：PDF 支持延期（暂无 RasterizeStage / OcrStage）。
- **匿名块文本**：匿名块内部的 TEXT/ATTRIB 仍不可见。
- **回路索引过匹配**：`M1`/`M2`/`10D`/`-OF-12` 匹配回路文本模式。
- **短母线段**：当水平母线线未跨越左右端子之间的完整距离时，远端端子可能无法找到（超出 `x_tol=50`）。
