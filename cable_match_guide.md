# 多目标电缆号匹配工具 — 同事上手指南 (v2)

## 这是什么

`scripts/cable_match.py` 一次性脚本：从一个 CSV（必须含 `电缆编号` 列）读出 N 个目标字符串（如 `T2-R139`、`DK1-103`），然后**只对每个 PDF 做一次 OCR**，把命中的 PDF 复制到各自的目标名子目录下。

v2 相比 v1 的改进：
1. **SQLite OCR 缓存**（`.cable_match_cache.db`）— 重跑时已 OCR 的文件秒过
2. **单一 `_matches.csv`** 在输出根目录（之前每个目标子目录各一份）
3. **state.json 自动保存**（每 30s + SIGTERM）— 崩溃后可 `--resume` 续跑
4. **`--resume <state.json|auto>`** — 内置断点续跑
5. **multiprocessing 真并行** — 比 ThreadPoolExecutor 快 4-6x

它比 `pdf-organize` 强的地方是**多目标一次扫描**：29 个电缆号 × 450 个 PDF 的批次，单 OCR 扫描就能完成，不会把 OCR 跑 29 遍。

## 一、前置环境

同事的机器需要（macOS / Linux 都行）：

| 工具 | 用途 | 安装命令 |
|------|------|---------|
| Python 3.8+ | 运行环境 | 系统自带或 [python.org](https://python.org) |
| Homebrew（macOS）| 装 Tesseract | `brew install tesseract tesseract-lang` |
| `tesseract` 二进制 | OCR 引擎 | `brew install tesseract tesseract-lang`（含 `chi_sim` 中文包）|
| `pytesseract` | Python 调用 tesseract | `myenv/bin/pip install pytesseract` |

**为什么需要单独装 Tesseract**：它不是 pip 包，是系统二进制，~700MB（含中文语言包）。`requirements.txt` 里只有 `pytesseract` 包装器，binary 必须独立装。

验证：
```bash
tesseract --version
tesseract --list-langs | grep chi_sim
# 两个都有输出 = OK
```

## 二、项目准备

```bash
git clone <repo-url>
cd toolspy

python3 -m venv myenv
myenv/bin/pip install -r requirements.txt
myenv/bin/pip install pytesseract     # ← requirements.txt 里有，保险再装
```

## 三、CSV 准备

CSV 必须有一列叫 `电缆编号`（含表头），UTF-8 或 UTF-8-BOM 编码。例：

```csv
序号,电缆编号,电缆型号及截面,...
28,T2-R139,ZB-KYJYP2-23-1kV-4x2.5,...
75,DK1-103,ZB-KYJYP2-23-1kV-4X6,...
...
```

脚本会**自动去重**（保留首次出现的顺序），空值会被跳过。

## 四、运行

```bash
cd <project>/toolspy

# Dry-run
myenv/bin/python scripts/cable_match.py \
    --csv /path/to/cable_list.csv \
    --input /path/to/pdf/folder \
    --list

# 实际跑
myenv/bin/python scripts/cable_match.py \
    --csv /path/to/cable_list.csv \
    --input /path/to/pdf/folder
```

### 常用参数

| 参数 | 默认 | 说明 |
|------|-----|------|
| `--csv` | 必填 | 含 `电缆编号` 列的 CSV |
| `--input` | 必填 | 要扫描的 PDF 根目录（递归）|
| `--output` | =`--input` | 命中 PDF 的输出根目录 |
| `--dpi` | 300 | OCR 渲染 DPI（150-400）。**300 是技术图纸的甜点** |
| `--workers` | 4 | 并行 workers 数（**multiprocessing 进程**，不是线程）|
| `--list` | off | 只看匹配，不复制 |
| `--lang` | `chi_sim+eng` | Tesseract 语言包 |
| `--rotation` | auto | 强制 PDF 页面旋转（0/90/180/270，CW=正）|
| `--preprocess` | `none` | 图像预处理：`none`（原图）/`gauss_otsu`（灰度+高斯+Otsu）。见下方"已知 OCR 漏字"权衡 |
| `--resume` | off | 续跑：`--resume auto`（默认 state.json）或 `--resume <path>` |
| `--no-cache` | off | 禁用 SQLite 缓存 |
| `--no-state` | off | 不写 state.json（无法 resume）|

### 关于 OMP_THREAD_LIMIT（v2 不再需要）

v1 用 `ThreadPoolExecutor`，需要 `OMP_THREAD_LIMIT=1` 避免和 tesseract 内部线程抢资源。
v2 用 `multiprocessing.Pool`，每个 worker 是独立进程，**不需要**这个环境变量。直接：
```bash
myenv/bin/python scripts/cable_match.py --csv ... --input ... --workers 6
```

### 推荐：正式跑前先 --list 看 5 分钟

29 个目标 × 450 PDF 的批次大约 **1-1.5 小时**（取决于机器配置）。先 `--list` 跑一遍确认工具能识别出预期的匹配，再正式跑。

## 五、输出结构（v2）

每个命中的目标会在 `--output` 下创建一个以电缆号命名的子目录（用于存 PDF 副本）。**所有匹配记录**集中写入输出根目录的 `_matches.csv`：

```
<output>/
├── _matches.csv                            ← 单一分类记录（v2 新增）
├── .cable_match_cache.db                   ← SQLite OCR 缓存（v2 新增）
├── .cable_match_state.json                 ← 断点续跑状态（v2 新增）
├── T2-R139/
│   ├── 10-W978-B768ⅡZ-D0201-11.pdf
│   └── ...  (含 T2-R139 的 PDF 副本)
├── DK1-103/
│   ├── 10-W978-B768ⅡZ-D0203-42.pdf
│   └── ...
└── ZL-322ZB/
    └── (空目录会被自动删除)
```

`_matches.csv` 格式：

```csv
电缆编号,PDF文件名,源相对路径,匹配时间,内容hash前16
T2-R139,10-W978-B768ⅡZ-D0201-11.pdf,电气二次1/D0201_电气二次总的部分/PDF/10-W978-B768ⅡZ-D0201-11.pdf,2026-06-18 19:30:15,26e06b0e443a9213
DK1-103,10-W978-B768ⅡZ-D0203-42.pdf,电气二次1/D0203_1000kV第1串二次线/PDF/10-W978-B768ⅡZ-D0203-42.pdf,2026-06-18 19:31:02,a8b3c9d4e5f6a7b8
```

每行包含**内容 hash**（前 16 字符），用于在重跑时去重。同一个 (电缆号, content_hash) 组合只写一次。

跑的过程中可以 `tail -f <output>/_matches.csv` 实时看进度。

## 六、断点续跑（v2 新增）

脚本会在以下时机写 `state.json`：
- 每 30 秒一次（`STATE_FLUSH_INTERVAL`）
- 收到 SIGTERM/SIGINT 信号时
- 正常完成时

state.json 包含所有已处理/未匹配/失败的 PDF 列表。续跑时（`--resume auto` 或 `--resume <path>`）会跳过这些已处理的。

```bash
# 启动后按 Ctrl+C 或 kill -TERM <pid> 中断
# state.json 会保存当前进度

# 续跑
myenv/bin/python scripts/cable_match.py \
    --csv ... --input ... --resume auto
# 输出: "Resumed: 47 PDFs already processed, 12 matches loaded"
# 然后只处理剩余的
```

如果 `--resume auto` 找不到默认 state.json，会从头开始（不报错）。

## 七、SQLite OCR 缓存（v2 新增）

`.cable_match_cache.db` 存了每个 PDF 的 OCR 结果（按 content_hash 索引）。**重跑时直接查缓存**：
- 第一次跑：OCR 所有 PDF → 写缓存 → 几小时
- 第二次跑：查缓存 → 秒级完成（同样的 PDF 不再 OCR）

```bash
# 查看缓存大小
myenv/bin/python -c "
import sqlite3
c = sqlite3.connect('/path/to/output/.cable_match_cache.db')
print('cached:', c.execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0])
"
```

PDF 内容变化时，content_hash 会变，自动重新 OCR（旧的缓存项孤立但不浪费空间）。

## 八、性能参考

| 批次规模 | 首次跑（v2, 6 workers）| 重跑（命中缓存）|
|---------|---------|---------|
| 50 个 1 页 PDF | ~3-5 分钟 | 秒级 |
| 450 个混合 PDF | ~1-1.5 小时 | 秒级 |
| 1000+ 个 PDF | 2-3 小时 | 秒级 |

v2 比 v1 的加速（同样硬件）：
- multiprocessing 替代 ThreadPoolExecutor：**4-6x**（不需 `OMP_THREAD_LIMIT=1`）
- SQLite 缓存：重跑时**几乎瞬时**

**实际调优**：
- 多核机器可以 `--workers 6` 或 `8`（实测在 M-series 8 核上 6 workers 最优）
- 时间紧可以 `--dpi 200`（约快 30%，但小字可能漏）
- 仅跑子目录：用 `--input` 指向子目录

## 九、重要的坑

### 1. 路径要带空格、引号用 `shlex`

如果路径或目标名带空格：
```bash
myenv/bin/python scripts/cable_match.py \
    --csv "~/Documents/work/项目/电缆清单.csv" \
    --input "~/Documents/work/项目/电气二次1" \
    --list
```

### 2. 默认 DPI 300 是有原因的

**不要**默认用 `--dpi 200`。Tesseract 在 200 DPI 下读小字（设备编号、电缆号）会漏。**300 DPI 是技术图纸的甜点**，只比 200 慢 10%。

如果在某些图纸上漏掉了明确的电缆号，**优先试 `--preprocess gauss_otsu` 而不是升 DPI**——升 DPI 不一定能找回漏字（实测在 D0202-33 上 400/500 DPI 都找不回 3B-463，反而把其它编号也吃了），而 `--preprocess` 有针对性地改善字符级误读。

### 3. v2 重跑已自动去重（v1 没有）

v1 重跑会创建 `_1`, `_2` 后缀副本。v2 通过 `_matches.csv` 的内容 hash 去重，**重跑只写新的匹配**，不重复。

如果想完全从头开始（比如换了新 CSV），删掉 state.json 和 _matches.csv：
```bash
rm /path/to/output/.cable_match_state.json /path/to/output/_matches.csv
```

### 4. 旋转文字的 PDF

技术图纸常有文字需要顺时针 90° 才能正常读。用 `--rotation 90` 强制：
```bash
myenv/bin/python scripts/cable_match.py --csv ... --input ... --rotation 90
# 90=CW, 180=翻转, 270=CCW(=90 CW 反向)
```

不传 `--rotation` 时，工具会自动跑原向 + 90° CW，对比中文字符数选更优。

### 5. 已知 OCR 漏字：字符级误读 vs 整块丢失（3B-463 案例研究）

Tesseract 在密集端子图上有两类典型毛病：

**(a) 字符级误读**——`3 ↔ 9 / 3 ↔ J / B ↔ 8 / F ↔ 上` 这些字形相近的字符
**(b) 整块吞掉**——密集小字里 Tesseract 把整段识别成一个无意义块（`(CC柜主体变非电量保护装置异常告警` 之类的乱码）

实测案例：`10-W978-B768ⅡZ-D0202-33.pdf` 这张图上，肉眼能看到 `3B-228 / 229 / 463 / 464 / 465 / 466` 共 6 个电缆号。不同 pipeline 的表现：

| Pipeline | D0202-33 (3B-\*) | D0202-67 (1F-151) |
|----------|-----------------|-------------------|
| 默认 `chi_sim+eng`，无预处理 | **5/6**（漏 463）| ✓ |
| `chi_sim+eng` + `--preprocess gauss_otsu` | 2/6（463 误读为 `JB-463`）| ✓ |
| `chi_sim` + `--preprocess gauss_otsu` | **6/6** ✓ | ✗（`F` 字符被吞）|

**结论**：没有任何单一 recipe 同时在这两页都达到 100%。这是技术图纸 OCR 的固有限制，不是 bug。

**应对策略**（按推荐顺序）：
1. **先用默认 pipeline 跑完**——大多数页 5/6 已足够，剩下的少量漏字可以人工补
2. **对高优先级电缆号，单独验证**——在 PDF viewer 里肉眼找，确认 OCR 是否漏
3. **局部重 OCR 优化**：
   - 把目标 CSV 拆成两批：
     - 批 1（中文相关电缆号）`--lang chi_sim --preprocess gauss_otsu`
     - 批 2（纯英文相关电缆号）`--lang chi_sim+eng`（默认）
   - 用 `--input` 指向同一目录，工具会按 content_hash + preprocess 复合键复用缓存
4. **后处理规则**（如果只关心一个页）：发现 `3B-469` 且本图其他位置有 `3B-464/465/466`，把 `3B-469` 改 `3B-463`（基于"46X 序列不应跳过 463"上下文推理）

**`--preprocess gauss_otsu` 的实现细节**（仅供 debug）：
- 灰度化 → `ImageFilter.GaussianBlur(radius=1)` → Otsu 阈值（`-5` 偏移）
- 目的：把每个像素的噪声平均掉，让 `3` 不再被读成 `J`/`9`
- 副作用：会把 `F` 这种细笔画字符也平均掉，所以"1F-151"被破坏
- 缓存键已包含 preprocess 标签，切换 recipe 不会污染旧 cache

### 6. OCR 失败是正常的

少数扫描件质量差（倾斜、模糊、太小），OCR 会失败。脚本不会中止，会跳过并计入 `失败` 统计，结束时打印。如果某个文件特别重要，单独 OCR 它看看：
```bash
myenv/bin/python -c "
from tools.text_extractor import extract_text
print(extract_text('path/to/that.pdf', ocr=True, dpi=400, warn=False))
"
```

## 十、退出码

| 码 | 含义 |
|----|------|
| 0 | 正常完成（可能有匹配，可能 0 匹配）|
| 1 | 工具错误（CSV 没 `电缆编号` 列、文件夹不存在等）|

`grep "匹配:" log.txt | wc -l` 看匹配数。

## 十一、参考：实际命令

这次跑武汉项目的实际命令（v2）：
```bash
cd ~/Documents/WebDev/toolspy

myenv/bin/python scripts/cable_match.py \
    --csv ~/Documents/work/nengzhong/wuhan/pdf/filter_result_3hzb_ff.csv \
    --input ~/Documents/work/nengzhong/wuhan/pdf \
    --workers 6

# 如果中断，续跑：
myenv/bin/python scripts/cable_match.py \
    --csv ~/Documents/work/nengzhong/wuhan/pdf/filter_result_3hzb_ff.csv \
    --input ~/Documents/work/nengzhong/wuhan/pdf \
    --workers 6 --resume auto
```
