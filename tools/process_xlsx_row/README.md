# process_xlsx_row

按规则把 xlsx 里命中表达式的整行染成指定背景色。

每个规则三要素：**(规则 id, 列号, 匹配函数)**，匹配函数可来自内置 (`equals/contains/startswith/endswith/regex`) 或 Python 自定义。运行时用布尔表达式 (`& | ! ()`) 把规则组合起来，整行命中即染色。

## 依赖

`openpyxl`（已在根目录 `requirements.txt`）。

## 运行

```bash
# 通过 dispatcher
python -m tools process-xlsx-row <args>

# 通过根目录 bash wrapper
./toolspy process-xlsx-row <args>
```

> 名字里虽然带 `xlsx`，对 `.xls` openpyxl 不能读；如需 `.xls`，先用 `text-extractor` 或 LibreOffice 转成 `.xlsx`。

## 命令行

```
process-xlsx-row [-h] --bgColor BGCOLOR --rules RULES
                 [--rules-file RULES_FILE]
                 [--rules-script RULES_SCRIPT]
                 [--sheet SHEET] [--header]
                 [--output OUTPUT] [--in-place] [--dry-run]
                 [--export-matches PATH]
                 xlsx
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `xlsx` (位置参数) | ✓ | 输入 xlsx 路径 |
| `--bgColor` | ✓ | 颜色（命名色 / `#RRGGBB` / `AARRGGBB`，见下） |
| `--rules` | ✓ | 规则 id 上的布尔表达式，如 `r1\|r2`、`r1&!r3` |
| `--rules-file` | | 规则定义的 JSON 文件 |
| `--rules-script` | | 规则定义的 Python 脚本 |
| `--sheet` | | 工作表名，默认活动表 |
| `--header` | | 把第一行当表头，跳过不参与匹配 |
| `--output` | | 输出 xlsx 路径，默认 `<输入>_colored.xlsx` |
| `--in-place` | | 直接覆盖输入文件（与 `--output` 互斥） |
| `--dry-run` | | 跑一遍但不写文件 |
| `--export-matches` | | 另存一份只含命中行的 xlsx（**保留高亮色**）；详见下方"导出命中行" |

`--rules-file` 和 `--rules-script` 可同时使用，规则按 id 合并；同名 id 会报错。

## 颜色

三种写法都行：

- 命名色：`yellow`、`red`、`blue`、`green`、`orange`、`purple`、`gold`、`navy`、`teal`、`gray`、`lightgray`、`darkgray` 等 24 种
- 6 位 hex：`#FFFF00`（自动补 alpha `FF`）
- 8 位 ARGB hex：`FFFFFF00`、`#80FFFF00`

最终值会打印在输出里确认，例如 `yellow -> FFFFFF00`。

## 规则定义

### 1) 内置匹配器

| 类型 | 用法 | 含义 |
|------|------|------|
| `equals` | `"equals:Active"` | 单元格字符串化后相等 |
| `contains` | `"contains:foo"` | 单元格字符串包含子串 |
| `startswith` | `"startswith:A"` | 单元格字符串以子串开头 |
| `endswith` | `"endswith:Z"` | 单元格字符串以子串结尾 |
| `regex` | `"regex:^[0-9]{6}$"` | 单元格字符串命中正则 |

所有匹配都先把单元格值 `str()` 化再比较；`None` 单元格永远不命中（除非规则本身检查 `None`）。

### 2) JSON 规则文件 (`--rules-file`)

```json
{
  "r1": {"column": 3, "match": "equals:Active"},
  "r2": {"column": 2, "match": "contains:Engineer"},
  "r3": {"column": "D", "match": {"type": "regex", "value": "^[0-9]{6}$"}},
  "r4": {"column": [3, 5], "match": "contains:Active"},
  "r5": {"column": "A,F", "match": {"type": "regex", "value": "^[AEIOU]"}},
  "r6": {"column": 1, "match": {"type": "py", "function": "is_vowel_name"}}
}
```

每条规则：

- `column`：1-based 列号，**支持单数字 / 单字母 / 列表 / 逗号分隔串 / 混合**——详见下方 `column` 字段格式章节
- `match`：可以是字符串简写（`"type:value"`）或 dict（`{"type": "...", "value": "..."}`），也可指向 Python 脚本里的函数（见下）

### 3) Python 规则脚本 (`--rules-script`)

脚本必须定义顶层变量 `RULES`，可以是 dict 或 list：

```python
from functools import partial

def _numeric_at_least(threshold):
    """工厂：返回单参数 matcher cell_value -> bool。"""
    def matcher(cell_value):
        if cell_value is None:
            return False
        try:
            return float(cell_value) >= threshold
        except (TypeError, ValueError):
            return False
    matcher.__name__ = f"numeric_at_least_{threshold}"
    return matcher

def is_engineering_dept(cell_value):
    return str(cell_value) == "Engineering"

RULES = {
    "high_salary":  {"column": 4, "match": _numeric_at_least(80000)},
    "engineer":     {"column": 2, "match": is_engineering_dept},
    "active":       {"column": 3, "match": "equals:Active"},
    "name_vowel":   {"column": 1, "match": {"type": "regex", "value": "^[AEIOU]"}},
    # 多列：状态列 (3) 或 备注列 (5) 包含 Active
    "active_anywhere": {"column": (3, 5), "match": "contains:Active"},
    # 多列：名字列 (A=1) 或 城市列 (F=6) 以元音开头
    "vowel_first":     {"column": ["A", "F"],
                        "match": {"type": "regex", "value": "^[AEIOUaeiou]"}},
}
```

- `match` 可以是 dict / 字符串简写 / **直接放 callable**（最灵活）
- 脚本里所有顶层可调用对象都会自动收集，JSON 里 `{"type": "py", "function": "my_fn"}` 即可引用

## `column` 字段格式

`column` 同时支持**单列**和**多列**；多列时该规则匹配 → OR 语义（任意一列命中即整条规则命中）。

| 写法 | 解析结果 |
|------|----------|
| `3` | `(3,)` — 单列，第 3 列 |
| `"C"` | `(3,)` — 单列字母（大小写均可：`"a"` / `"AA"` / `"AB"`） |
| `[1, 3, 6]` | `(1, 3, 6)` — 数字列表 |
| `["A", "C", "F"]` | `(1, 3, 6)` — 字母列表 |
| `[1, "C", 6]` | `(1, 3, 6)` — 数字和字母混合列表 |
| `"1,3,6"` | `(1, 3, 6)` — 逗号分隔的数字串 |
| `"A,C,F"` | `(1, 3, 6)` — 逗号分隔的字母串 |
| `"1,C,F"` | `(1, 3, 6)` — 数字+字母混合 |
| `" 1 , C "` | `(1, 3)` — 空白会被去掉 |

- 同一列出现多次会**自动去重**（保留首次出现的顺序），不会报错
- 空串 / 空列表 / `0` / `-1` / 含数字的非法字母如 `"A1"` 都会立刻报错
- 关键字只能用 `column`（单数），不能写 `columns` —— 多列直接传列表或逗号串
- 超出表实际范围的列不会报错，只是该列单元格值视为 `None`，按 `None` 处理（通常不命中）

示例：状态列 (col 3) **或** 备注列 (col 5) 包含 `Active` 的行命中：

```json
{
  "r1": {
    "column": [3, 5],
    "match": "contains:Active",
    "description": "Status or Notes contains 'Active'"
  }
}
```

## 表达式语法 (`--rules`)

只支持布尔运算，不允许函数调用、属性访问、算术、比较：

| 写法 | 含义 | 等价 |
|------|------|------|
| `r1\|r2` | 或 | `r1 or r2` |
| `r1&r2` | 与 | `r1 and r2` |
| `!r1` | 非 | `not r1` |
| `(r1\|r2)&!r3` | 括号、优先级 | — |

示例：

```bash
# 状态为 Active
--rules "r1"

# Active 或 Engineering
--rules "r1|r2"

# Active 且工资 >= 80000
--rules "r1&high_salary"

# Active 但不是 Engineer
--rules "(r1|name_vowel)&!engineer"

# 取反（什么都没匹配的）
--rules "!r1"
```

未列出的规则 id 在表达式里会立刻报错，启动时就能发现拼错。

## 输出

- 默认写到 `<输入>_colored.xlsx`，跟输入同目录
- **命中表达式的行整行染色**（列 1 到 `ws.max_column` 的所有单元格），**不只是规则涉及的列**——避免同一行里出现"半截黄半截白"的视觉割裂
- 行号 1 起；表头（如果 `--header`）跳过
- 同时打印每个规则的独立命中数、每个规则实际作用的列号，方便调试

```
input:        /.../sample.xlsx
sheet:        employees
rules:        r1|r2  (3 loaded: r1, r2, r3)
rule columns: r1=[3], r2=[2], r3=[4]
color:        yellow -> FFFFFF00
per-rule hits:r1=7, r2=4, r3=1
rows scanned: 10
rows matched: 9
sample rows:  2, 4, 5, 6, 7, ...
output:       /.../sample_colored.xlsx
```

## 导出命中行 (`--export-matches`)

除了给主输出文件整行染色，还可以指定 `--export-matches PATH` 单独存一份只含命中行的 xlsx，便于后续筛选/分发/邮件附件。

- 主输出（带染色的完整文件）照常写
- 导出文件包含：表头行（仅当 `--header` 指定时） + 所有命中数据行
- 命中行整行保留 `--bgColor` 指定的背景色，方便在导出文件里一眼看出哪些行是因为什么规则被抽出来的
- 列宽/字体/对齐/数字格式从源文件复制过来
- 没有命中时跳过写入，只在 stderr 打 `warning:`；`--dry-run` 模式下也只打印路径不写盘
- 路径冲突：`--export-matches` 和 `--output` 不能指向同一文件

**示例：把 Active 员工单独存一份**
```bash
python -m tools process-xlsx-row sample.xlsx \
  --bgColor yellow --rules "r1" \
  --rules-file rules.json --header \
  --export-matches /tmp/active_only.xlsx
```

```
output:       /.../sample_colored.xlsx
export:       /tmp/active_only.xlsx  (7 rows)
```

## 完整示例

示例 xlsx `sample.xlsx` 表 `employees` 4 列 (Name / Department / Status / Salary)。

**JSON 规则 `rules.json`：**
```json
{
  "r1": {"column": 3, "match": "equals:Active"},
  "r2": {"column": 2, "match": "contains:Engineer"},
  "r3": {"column": 4, "match": {"type": "regex", "value": "^[0-9]{6}$"}}
}
```

**1. Active 或 Engineer 的行染黄**
```bash
python -m tools process-xlsx-row sample.xlsx \
  --bgColor yellow --rules "r1|r2" \
  --rules-file rules.json --header
```

**2. (Active 或元音开头) 且不是 Engineer 的行染浅绿**
```bash
python -m tools process-xlsx-row sample.xlsx \
  --bgColor "#90EE90" \
  --rules "(active|name_vowel)&!engineer" \
  --rules-script rules.py --header
```

**3. 干跑 (不写文件)**
```bash
python -m tools process-xlsx-row sample.xlsx \
  --bgColor red --rules "r1" \
  --rules-file rules.json --header --dry-run
```

**4. 多列规则：状态列 (C) 或 备注列 (E) 任一含 Active 即命中**
```json
{
  "r1": {"column": [3, 5], "match": "contains:Active"},
  "r2": {"column": "A,F", "match": {"type": "regex", "value": "^[AEIOU]"}}
}
```
```bash
python -m tools process-xlsx-row sample.xlsx \
  --bgColor gold --rules "r1|r2" \
  --rules-file rules.json --header
```

**5. 把命中行单独抽出去另存（保留高亮色）**
```bash
python -m tools process-xlsx-row sample.xlsx \
  --bgColor gold --rules "r1|r2" \
  --rules-file rules.json --header \
  --export-matches /tmp/active_engineer.xlsx
```

## 注意事项

- **openpyxl 不会保留所有 Excel 特性**（如某些图表、条件格式、宏），跑完可以用 Excel 再打开看一眼。如果只读不改，可以用 `--dry-run` 先看匹配数
- 默认 `data_only=True` 加载：公式单元格取上次保存时的缓存值；想读公式原文请先用 LibreOffice/Excel 重存、或在外部预处理
- 规则 id 必须是合法 Python 标识符（字母/数字/下划线，不能以数字开头），因为它要直接出现在表达式里
- 表达式里所有出现的 id 必须在已加载规则里，否则启动报错（fail-fast，不会跑到一半才发现）
- 自定义 Python 函数抛异常会立刻终止并打印行号；通常意味着数据缺列或类型不对
- 表达式最大安全保证：只用 `ast` 解析，禁止函数调用/属性/算术/比较，只能引用规则 id 和布尔字面量
- **多列规则 = 任意列命中即整条规则命中**（OR 语义）。如果需要"所有列都命中才算规则命中"（AND 语义），定义多条单列规则再用 `&` 组合，例如 `r_status_active & r_notes_active`
- 多列规则的 per-rule 命中数 = **行数**（同一行多列同时命中仍只算 1），不是 `(行, 列)` 配对数
- 列号超出表实际范围不会报错，对应单元格值视为 `None`