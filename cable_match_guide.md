# 多目标电缆号匹配工具 — 同事上手指南 (v3)

## 这是什么

`scripts/cable_match.py` 一次性脚本：从一个 CSV（必须含 `电缆编号` 列）读出 N 个目标字符串（如 `T2-R139`、`DK1-103`），然后**只对每个 PDF 做一次 OCR**，把命中的 PDF 复制到各自的目标名子目录下。

v2 相比 v1 的改进：
1. **SQLite OCR 缓存**（`.cable_match_cache.db`）— 重跑时已 OCR 的文件秒过
2. **单一 `_matches.csv`** 在输出根目录（之前每个目标子目录各一份）
3. **state.json 自动保存**（每 30s + SIGTERM）— 崩溃后可 `--resume` 续跑
4. **`--resume <state.json|auto>`** — 内置断点续跑
5. **multiprocessing 真并行** — 比 ThreadPoolExecutor 快 4-6x

v3 新增：
1. **可插拔 OCR 引擎**（`--engine {tesseract|paddleocr}`）— 默认 Tesseract，可选 PaddleOCR 提升小字+密集图表召回
2. **图像预处理**（`--preprocess {none|gauss_otsu|both}`）— 灰度+高斯+Otsu 救回 3B-463 这类漏字
3. **4 级递进式召回匹配**（exact → normalized → confusion → levenshtein）— 把 OCR 字符错（`3`↔`8` 等）捞回来
4. **Tesseract 微调**（`--psm` / `--oem`）— 实验性，多数情况默认即可
5. **`匹配方式` 列**写进 `_matches.csv` — 方便 review 哪些是 fuzzy 命中的

它比 `pdf-organize` 强的地方是**多目标一次扫描**：29 个电缆号 × 450 个 PDF 的批次，单 OCR 扫描就能完成，不会把 OCR 跑 29 遍。

## 一、选 OCR 引擎（Tesseract vs PaddleOCR）

同事的机器需要**至少装其中一个**。两个引擎的差异：

| 引擎 | 系统依赖 | 体积 | 中文小字+密集图表召回 | 安装难度 | 适用场景 |
|------|---------|------|----------------------|---------|---------|
| **Tesseract**(默认) | 系统二进制(~700MB) | 700MB | ~70-80% | ⭐ 简单 | 通用文档、PDF 文字层、英文 |
| **PaddleOCR** | pip 包装(~250MB) + 模型(~100MB) | 350MB | ~85-95% | ⭐⭐ 中等 | 电力图纸、二次图、扫描件 |

**经验值**:Tesseract 70~80% → PaddleOCR 85~95% **不是夸张**,特别是 `3B-463`、`1F-151` 这种小字密集场景。

**怎么选**:
- 文档以排版正文为主 → Tesseract
- 大量电力二次图、端子排、扫描件 → PaddleOCR
- 不知道 → 先 Tesseract 跑,看漏报情况,不满意再切 PaddleOCR

两个引擎可以共存,`cable_match.py` 的 SQLite cache 用 `ocr_engine` 列区分,**Tesseract 的结果不会被 PaddleOCR 覆盖**,反之亦然。

## 二、装 Tesseract(默认,先装这个)

| 平台 | 命令 |
|------|------|
| macOS | `brew install tesseract tesseract-lang` |
| Linux (Debian/Ubuntu) | `apt install tesseract-ocr tesseract-ocr-chi-sim` |
| Windows | 从 [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) 下载安装包,默认装到 `C:\Program Files\Tesseract-OCR\`(脚本会自动检测该路径)|

验证:
```bash
tesseract --version
tesseract --list-langs | grep chi_sim
# 两个都有输出 = OK
```

`requirements.txt` 已包含 `pytesseract` 包装器,**Tesseract 二进制必须独立装**(它不是 pip 包)。

## 三、可选:加装 PaddleOCR

```bash
myenv/bin/pip install -r requirements-paddleocr.txt
```

这会装 `paddleocr==2.7.3` + `paddlepaddle==2.6.1`(~250MB pip 依赖)。**第一次跑 PaddleOCR 时会自动下载模型**(~100MB,放在 `~/.paddleocr/` 下)。

**平台注意**:
- macOS Apple Silicon: paddlepaddle 没有 GPU wheel,**强制 CPU 模式**,单 PDF ~30s-1min
- Windows + NVIDIA GPU: 自动 CUDA 加速,单 PDF ~5-10s
- Linux + NVIDIA GPU: 同上,推荐
- 没 GPU 的 Linux/Windows: CPU 模式,Mac 类似速度

验证:
```bash
myenv/bin/python -c "from paddleocr import PaddleOCR; print('OK')"
# 第一次会下载模型,等 1-2 min
```

## 四、项目准备

```bash
git clone <repo-url>
cd toolspy

python3 -m venv myenv
myenv/bin/pip install -r requirements.txt
# 可选:加装 PaddleOCR
myenv/bin/pip install -r requirements-paddleocr.txt
```

## 五、CSV 准备

CSV 必须有一列叫 `电缆编号`（含表头），UTF-8 或 UTF-8-BOM 编码。例：

```csv
序号,电缆编号,电缆型号及截面,...
28,T2-R139,ZB-KYJYP2-23-1kV-4x2.5,...
75,DK1-103,ZB-KYJYP2-23-1kV-4X6,...
...
```

脚本会**自动去重**（保留首次出现的顺序），空值会被跳过。

## 六、运行

`scripts/cable_match.py` 的核心调用:

```bash
# 推荐:先 --list 看匹配,不复制
myenv/bin/python scripts/cable_match.py \
    --csv /path/to/cables.csv \
    --input /path/to/pdf_folder \
    --list

# 实际跑(Tesseract 默认,推荐起点)
myenv/bin/python scripts/cable_match.py \
    --csv /path/to/cables.csv \
    --input /path/to/pdf_folder

# 想要更高召回:切到 PaddleOCR
myenv/bin/python scripts/cable_match.py \
    --csv /path/to/cables.csv \
    --input /path/to/pdf_folder \
    --engine paddleocr
```

### 常用参数

| 参数 | 默认 | 说明 |
|------|-----|------|
| `--csv` | 必填 | 含 `电缆编号` 列的 CSV |
| `--input` | 必填 | 要扫描的 PDF 根目录（递归）|
| `--output` | =`--input` | 命中 PDF 的输出根目录 |
| `--engine` | `tesseract` | OCR 引擎：`tesseract`（默认，~700MB 系统包）或 `paddleocr`（~250MB pip + ~100MB 模型，小字密集场景召回更高）|
| `--use-gpu` | off | 启用 PaddleOCR GPU 推理（Win/Linux + CUDA only;macOS Apple Silicon 忽略）|
| `--dpi` | 300 | OCR 渲染 DPI（150-400）。**300 是技术图纸的甜点** |
| `--workers` | 4 | 并行 workers 数（**multiprocessing 进程**，不是线程）|
| `--list` | off | 只看匹配，不复制 |
| `--lang` | `chi_sim+eng` | OCR 语言包。PaddleOCR 自动映射 `chi_sim`→`ch`、`eng`→`en` |
| `--rotation` | auto | 强制 PDF 页面旋转（0/90/180/270，CW=正）。**PaddleOCR 忽略此参数**(自带角度分类器) |
| `--preprocess` | `none` | 图像预处理：`none`（原图）/`gauss_otsu`（灰度+高斯+Otsu）/ `both`（两者并集）。见下方"已知 OCR 漏字"权衡 |
| `--psm` | default | Tesseract PSM。**实测 psm=6 对电缆图纸是负优化（-30% recall），不要用**。**PaddleOCR 忽略此参数** |
| `--oem` | default | Tesseract OEM（默认 3 即可）。**PaddleOCR 忽略此参数** |
| `--levenshtein` | off | **实验性**：启用第 4 级 Levenshtein 距离匹配。D0202 实测产生 ~58 误报，**默认关闭** |
| `--resume` | off | 续跑：`--resume auto`（默认 state.json）或 `--resume <path>` |
| `--no-cache` | off | 禁用 SQLite 缓存 |
| `--no-state` | off | 不写 state.json（无法 resume）|

### OCR 引擎选择建议

- **先用默认 Tesseract 跑** — 不需要额外依赖
- **Tesseract 漏报多时**:`--engine paddleocr`(预期 +10-15% recall on 电力图纸)
- **两种都跑**:开 2 个输出目录,各跑一次,`merge_5stage_matches.py` 合并(同时合并 PDF 到 `_matched_pdfs/`)

### 关于 OMP_THREAD_LIMIT（v2 不再需要）

### 关于 OMP_THREAD_LIMIT（v2 不再需要）

v1 用 `ThreadPoolExecutor`，需要 `OMP_THREAD_LIMIT=1` 避免和 tesseract 内部线程抢资源。
v2 用 `multiprocessing.Pool`，每个 worker 是独立进程，**不需要**这个环境变量。直接：
```bash
myenv/bin/python scripts/cable_match.py --csv ... --input ... --workers 6
```

### 推荐：正式跑前先 --list 看 5 分钟

29 个目标 × 450 PDF 的批次大约 **1-1.5 小时**（取决于机器配置）。先 `--list` 跑一遍确认工具能识别出预期的匹配，再正式跑。

## 七、输出结构（v2）

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
电缆编号,PDF文件名,源相对路径,匹配时间,内容hash前16,匹配方式
T2-R139,10-W978-B768ⅡZ-D0201-11.pdf,电气二次1/D0201_电气二次总的部分/PDF/10-W978-B768ⅡZ-D0201-11.pdf,2026-06-18 19:30:15,26e06b0e443a9213,exact
DK1-103,10-W978-B768ⅡZ-D0203-42.pdf,电气二次1/D0203_1000kV第1串二次线/PDF/10-W978-B768ⅡZ-D0203-42.pdf,2026-06-18 19:31:02,a8b3c9d4e5f6a7b8,confusion
```

每行包含**内容 hash**（前 16 字符），用于在重跑时去重。同一个 (电缆号, content_hash) 组合只写一次。

`匹配方式` 列说明（v3 新增，4 级递进式召回）：

| 匹配方式 | 含义 | 例子 |
|----------|------|------|
| `exact` | 目标字符串在 OCR 文本中原样出现 | `3B-463` 文本里有 `3B-463` |
| `normalized` | 归一化后匹配（大写、统一分隔符） | OCR 文本 `3B_463` / `3B 463` / `3B.463` / `3B—463` 都能匹配 `3B-463` |
| `confusion` | OCR 混淆表 + 1 字符替换 | OCR 文本 `38-463` / `JB-463` 匹配 `3B-463`（3↔8、3↔J、B↔8 混淆） |
| `levenshtein` | 编辑距离 ≤ 1（**实验性，默认关闭**） | OCR 文本 `3B-46S` 匹配 `3B-463` |

**混淆表**（`CONFUSION` dict in `cable_match.py`）:
```python
{
    "3": ["8", "J"], "8": ["3", "B"],
    "0": ["O", "Q"], "O": ["0"],
    "1": ["I", "L", "7"], "I": ["1"],
    "5": ["S"], "S": ["5"],
    "G": ["6"], "B": ["8"],
    "-": ["_", ".", " ", ""],
}
```

D0202 baseline 实测（75 PDF）：
- 老 exact-only：140 unique cables
- 加 normalized + confusion：**167 unique cables（+27，+19% recall）**
- 加 Levenshtein（实验）：225 unique cables，**但 58 个是误报**（如 `3B-B41` 在 `3B-B4111` 端子排 ref 里匹配到 `3B-241`）→ 默认关闭

跑的过程中可以 `tail -f <output>/_matches.csv` 实时看进度。

## 八、断点续跑（v2 新增）

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

## 九、SQLite OCR 缓存（v2 新增）

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

## 十、性能参考

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

## 十一、重要的坑

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
3. **6 阶段 union 并行**（推荐，Win11 16 核 + GPU ~1.5h 跑 1882 PDF）——见下节
4. **局部重 OCR 优化**：
   - 把目标 CSV 拆成两批：
     - 批 1（中文相关电缆号）`--lang chi_sim --preprocess gauss_otsu`
     - 批 2（纯英文相关电缆号）`--lang chi_sim+eng`（默认）
   - 用 `--input` 指向同一目录，工具会按 content_hash + preprocess 复合键复用缓存
5. **后处理规则**（如果只关心一个页）：发现 `3B-469` 且本图其他位置有 `3B-464/465/466`，把 `3B-469` 改 `3B-463`（基于"46X 序列不应跳过 463"上下文推理）

**`--preprocess gauss_otsu` 的实现细节**（仅供 debug）：
- 灰度化 → `ImageFilter.GaussianBlur(radius=1)` → Otsu 阈值（`-5` 偏移）
- 目的：把每个像素的噪声平均掉，让 `3` 不再被读成 `J`/`9`
- 副作用：会把 `F` 这种细笔画字符也平均掉，所以"1F-151"被破坏
- 缓存键已包含 preprocess 标签，切换 recipe 不会污染旧 cache

## 十一之2、6 阶段 union 并行（高召回完整跑法 — 2 engines）

既然没有任何单一 OCR 配置在所有页都达到 100%，而且 **Tesseract 和 PaddleOCR 的失败模式互补**，干脆把 6 种组合（**2 OCR engines × 2 langs × 3 recipes**）都跑一遍取并集。脚本一次性启动 6 个后台进程。

### 为什么 6 个 stage

| 失败类型 | Tesseract 表现 | PaddleOCR 表现 | 互补? |
|----------|---------------|---------------|-------|
| 拉丁字符+数字（1F-151） | ✅ 强 | ❌ 弱（ch 模型当噪声） | 是 |
| 中文字符密集 | ⚠️ 一般 | ✅ 强 | 是 |
| 小字密集（3B-4xx） | ⚠️ gauss 能修 | ✅ 识别更准 | 是 |
| 端子排错位（3B-463） | ❌ Type A 漏字 | ⚠️ 试一下可能救 | 试一下 |
| 扫描模糊 | ⚠️ LSTM 撑住 | ✅ 视觉模型更鲁棒 | 是 |

**实测 (3 PDF A/B, Mac CPU)**:
- Tesseract chieng+none: **13** unique cables
- PaddleOCR chieng+none: **11** unique cables
- 2 路 union: **18** unique cables (+38-64% vs 单引擎)
- 详细:Tesseract 独有 7 个（1F-151, GPS-1F, GPST-1F 等）;PaddleOCR 独有 5 个（3B-437, 3B-503, 3B-508, 5071-142, 3B-136）

### 6 个 stage

| # | Stage | 命令 | 找什么 |
|---|-------|------|--------|
| 1 | `chieng_tess` | `chi_sim+eng + tesseract + none` | 1F-151 / GPS-1F 类（基线，v1 同等质量）|
| 2 | `chieng_tess_gauss` | `chi_sim+eng + tesseract + gauss_otsu` | 字符级误读修复（3B-414 类）|
| 3 | `chisim_tess` | `chi_sim + tesseract + none` | 中文密集页 |
| 4 | `chisim_tess_gauss` | `chi_sim + tesseract + gauss_otsu` | 3B-463 类尝试（Type A 边缘）|
| 5 | **`chieng_paddle`** | `chi_sim+eng + paddleocr + none` | PaddleOCR 中文 + ASCII（PP-OCRv4 ch 模型）|
| 6 | **`chisim_paddle`** | `chi_sim + paddleocr + none` | PaddleOCR 英文（PP-OCRv4 en 模型，与 stage 5 互补）|

每个 stage 写到自己独立的 `.stage_*` 子目录（cache + state + _matches.csv 都分开），互不干扰。OCR 完成后用 `merge_5stage_matches.py` 按 (cable, content_hash[:16]) 复合键去重并集。

**注意**:
- Stage 5 和 6 的 PaddleOCR 用**不同的模型**:chieng_paddle 用 `ch`（中文+ASCII）,chisim_paddle 用 `en`（英文 only,可能抓到 1F/GPS 类字符,因为 ch 模型会当噪声丢)
- Tesseract 失败页面（Type A: OCR 完全漏字）PaddleOCR 也救不了,但**字符错**（3B-463 → JB-463）和**布局漏**的场景 PaddleOCR 通常更准
- 任何 stage 失败不影响其他 stage（独立目录 + 独立 cache）

### 一键启动

**Mac / Linux / WSL**（bash）：
```bash
# 默认: 6 stages (4 Tesseract + 2 PaddleOCR)
bash scripts/run_union.sh 4    # 4 workers × 6 stages = 24 workers 总

# 只跑 4 个 Tesseract stage（不装 PaddleOCR 时）
ENGINE=tesseract bash scripts/run_union.sh 4

# 选 stage 子集（用 STAGES_FILTER 环境变量）
STAGES_FILTER="1-4" bash scripts/run_union.sh 4              # 只跑 Tesseract 4 stages
STAGES_FILTER="5,6" bash scripts/run_union.sh 4              # 只跑 PaddleOCR 2 stages
STAGES_FILTER="1-3,6" bash scripts/run_union.sh 4            # Tesseract 1-3 + PaddleOCR chisim
STAGES_FILTER="all" bash scripts/run_union.sh 4              # 等同默认
```

**Windows 原生**（PowerShell）：
```powershell
# 默认: 6 stages
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4

# 只跑 Tesseract 4 stages
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Engine tesseract

# 选 stage 子集（用 -Stages 参数）
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "1-4"   # Tesseract
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "5,6"   # PaddleOCR
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "1-3,6" # 混合
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "all"   # 默认
```

### 分批跑（先跑 Tesseract,GPU 准备好再跑 PaddleOCR）

**适用场景**:Mac 上先把 Tesseract 4 stages 跑完确认召回,等 Win11 GPU ready 再跑 PaddleOCR 2 stages。设计完全支持分批跑,6 stages **cache/state/_matches.csv 全部独立**:

```bash
# batch 1: Win11/Mac 上跑 Tesseract 4 stages
STAGES_FILTER="1-4" bash scripts/run_union.sh 4
# ... 等跑完 ...

# batch 2 (Win11 GPU ready):跑 PaddleOCR 2 stages
STAGES_FILTER="5-6" bash scripts/run_union.sh 4
# ... 等跑完 ...

# 合并:跑一次就够,自动读全部 6 stage CSVs,缺失的 skip
python scripts/merge_5stage_matches.py /path/to/wuhan/pdf
```

或 PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "1-4"
# 跑完后
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "5,6"
# 合并
python scripts\merge_5stage_matches.py "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf"
```

### Win11 GPU 加速 PaddleOCR（`-UseGpu` / `USE_GPU=1`）

**前提**:Win11 装了 NVIDIA 显卡 + CUDA 驱动,而且 venv 里装的是 **CUDA 版 paddlepaddle-gpu**(默认 `pip install -r requirements-paddleocr.txt` 装的是 CPU 版 paddlepaddle)。

**1. 装 CUDA 版 paddlepaddle-gpu**(替换 venv 里的 CPU 版):

```powershell
# 查 NVIDIA 驱动支持的最高 CUDA 版本(nvidia-smi 右上角 "CUDA Version" 行)
nvidia-smi

# 卸载 CPU 版 paddlepaddle
myenv\Scripts\pip uninstall -y paddlepaddle

# 安装 paddlepaddle-gpu==2.6.2,挑对应 CUDA 版本的 wheel index
#   CUDA 11.7(Win11 2022-2024 出厂的多半是这个):
myenv\Scripts\pip install paddlepaddle-gpu==2.6.2 -f https://www.paddlepaddle.org.cn/whl/windows/cu117/noavx
#   CUDA 11.8:
#     .../whl/windows/cu118/noavx
#   CUDA 12.x(新驱动):
#     .../whl/windows/cu123/noavx
# (/noavx 是给没有 AVX 指令集的 CPU 用的;有 AVX 的话去掉 /noavx 段)

# 验证
myenv\Scripts\python.exe -c "import paddle; print(paddle.device.is_compiled_with_cuda(), paddle.device.cuda.device_count())"
# 期望: True 1  (True = CUDA 编译过; 数字 = 可见 GPU 数)
```

**2. 启动时加 `-UseGpu`**(PowerShell)/ `USE_GPU=1`(bash):

```powershell
# 全部 6 stages + GPU
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -UseGpu

# 只跑 PaddleOCR 2 stages + GPU(分批跑工作流,先在 CPU 上跑完 Tesseract 4 stages)
powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "5,6" -UseGpu
```

```bash
# bash 等价(WSL/Linux/Mac,但 Mac 上 USE_GPU=1 会 hard-fail 见下)
USE_GPU=1 STAGES_FILTER="5-6" bash scripts/run_union.sh 4
```

**3. `-UseGpu` / `USE_GPU=1` 的行为**:
- **HARD-FAIL**:如果 paddlepaddle 没 CUDA 支持、CUDA 驱动版本对不上、或 GPU 不可见,**直接报错退出**(不会静默回退到 CPU 浪费 6 小时才发现)
- 错误信息会**打印具体的 paddlepaddle-gpu 装包指令**(cu117/cu118/cu123 三选一)
- Tesseract stages(1-4)**忽略** `-UseGpu`(Tesseract 没有 GPU 加速)
- macOS 上 `USE_GPU=1` **永远 hard-fail**(paddlepaddle macOS wheel 没有 CUDA)

**4. 跑起来后监控**:

```powershell
# 另开一个终端看 GPU 占用
nvidia-smi -l 2
# 期望:PaddleOCR stages 启动后,python.exe 进程在 "C" 列下有 ~1-3GB 显存占用,"GPU-Util" 应该有波动
```

**5. 不加 `-UseGpu` 跑 PaddleOCR**(CPU 模式):
- 仍然 work,只是慢(Win11 16 核 + 1882 PDF 估计 8-12h)
- cache schema 不变,后续切到 GPU 重跑会**直接读 CPU 跑过的缓存**(use_gpu 不在 cache key 里,输出文本一致)
- 所以建议:**先 CPU 跑一遍验证 pipeline 通**,再 GPU 重跑前清掉 PaddleOCR 阶段的缓存:
  ```powershell
  Remove-Item "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf\.stage_chieng_paddle\ocr_cache.db"
  Remove-Item "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf\.stage_chisim_paddle\ocr_cache.db"
  ```
  (Tesseract 4 stages 的缓存不用动)

**6. PaddleOCR 2.x vs 3.x 兼容**:`PaddleOCREngine.init()` 自适应 paddleocr 主版本号,从 `paddleocr.__version__` 推断。`requirements-paddleocr.txt` 锁的是 **2.7.3**(Win/Linux + paddlepaddle 2.6.2);但用户机器上可能装了 3.x(最新 3.7.0,走 PaddleX 框架)。两种都支持:
- **2.x 路径**: `PaddleOCR(use_angle_cls, lang, use_gpu, show_log)`(Win 用户碰到 `Unknown argument: use_gpu` 就是这个原因)
- **3.x 路径**: `PaddleOCR(lang, use_textline_orientation, use_doc_orientation_classify, use_doc_unwarping)`(3.x 移除了 use_gpu/show_log,GPU 通过 paddlepaddle-gpu 自动检测)
- **混合不行**(paddleocr 3.x + paddlepaddle 2.6.2 会因 PIR strides 失败;paddleocr 2.x + paddlepaddle 3.0+ 会因 set_optimization_level API 缺失失败)
- **验证安装**:
  ```powershell
  myenv\Scripts\python.exe -c "from paddleocr import PaddleOCR; import paddle; print('paddleocr', __import__('paddleocr').__version__, 'paddle', paddle.__version__)"
  # 期望: paddleocr 2.7.3 paddle 2.6.2  或  paddleocr 3.x paddle 3.x
  ```

**7. Silent-fallback detection**(关键!):如果 PaddleOCR init 失败(mismatch 版本,GPU 检测失败,模型下载失败,etc.),worker 会**fallback 到 Tesseract** 但**整个 PaddleOCR stage 都会变成 Tesseract 跑出来** — 等于 stage 5/6 跟 stage 1/3 没区别,**召回率原地踏步**。

完成时 main 会打印:
```
=== 完成 ===
扫描: 1947 (skip 0 already done)
总匹配 (含历史): 390
匹配方式分布:
  exact         120
  confusion     270
OCR cache: 1947 entries in .cable_match_cache.db
OCR engine distribution:
  paddleocr              1845     ← 真的 PaddleOCR 跑的
  tesseract_fallback     102      ← WARNING: 这一阶段其实跑的是 Tesseract!
```

**任何 `tesseract_fallback` 行 = 那个 stage 实际跑了 Tesseract**。常见原因:
- paddleocr 3.x + paddlepaddle 2.x 不兼容(`Type of attribute: strides is not right`)
- paddleocr 2.x + paddlepaddle 3.x 不兼容(`set_optimization_level` missing)
- 第一次跑没下完模型 + 网络断(从断点续跑就行)

**修复**:装匹配的 paddlepaddle + paddleocr 对:
- 2.x: `pip install paddlepaddle==2.6.2 paddleocr==2.7.3`
- 3.x: `pip install paddlepaddle==3.0+ paddleocr==3.0+` 然后清掉那个 stage 的 cache 重跑

**STAGES_FILTER / -Stages 语法**(1-based,索引见上表):
- `all` — 默认,跑所有可用 stage
- `1-4` — 范围(含两端)
- `1,3,5` — 列表
- `1-3,6` — 混合范围 + 列表
- 无效值报错退出(不会启动任何 stage)

**注意**:
- 所有 batch 用**相同的 `--dpi`(默认 300)**、**相同的 `--csv`**、**相同的 `--input`**
- 同一 stage 不会被重复启动(`cable_match.py` 会加载已有 `_matches.csv` 去重 + `--resume auto` 接续)
- 跑期间**不要修改源 PDF**(否则 content_hash 变化,产生重复文件)
- `merge_5stage_matches.py` 是幂等的,跑多少次都行——会读取当前的 CSVs 重建 union

### 监控

**Mac / Linux / WSL**：
```bash
bash scripts/wuhan_status.sh
```

**Windows 原生**：
```powershell
powershell -ExecutionPolicy Bypass -File scripts\wuhan_status.ps1
```

### 合并结果

6 个 stage 都完成后：
```bash
# 默认: 合并 CSV + 复制各 stage 命中的 PDF 到 <wuhan/pdf>/_matched_pdfs/
python scripts/merge_5stage_matches.py /path/to/wuhan/pdf

# 只要 CSV,不要 PDF 副本
python scripts/merge_5stage_matches.py /path/to/wuhan/pdf --no-pdf-merge

# 自定义 PDF 输出子目录
python scripts/merge_5stage_matches.py /path/to/wuhan/pdf --pdf-output pdfs
```

合并做两件事:
1. **CSV 去重并集** — 写 `<wuhan/pdf>/_matches.csv`,按 `(cable, content_hash)` 复合键合并 6 个 stage 的结果,`match_type` 自动升级到 best tier:`exact > normalized > confusion > levenshtein`
2. **PDF 物理合并** — 把所有 stage 命中过的 PDF **按内容去重**后,复制到 `<wuhan/pdf>/_matched_pdfs/<电缆编号>/<pdf_stem>__<hash8>.pdf`:
   - 相同 content_hash 的 PDF 跨多个 stage 只保留一份(`(cable, content_hash)` 一一对应)
   - 同一电缆编号但**不同内容**的 PDF(不同 content_hash,例如同一图号的不同版本)会全部归到该电缆编号文件夹下,每个 PDF 用 `<pdf_stem>__<hash8>.pdf` 命名避免冲突
   - **幂等**:重复运行 `merge_5stage_matches.py` 不会重复复制已存在的文件

**输出示例**:
```
<wuhan>/pdf/
├── _matches.csv                       # 合并后的去重并集(13-21+ 行)
└── _matched_pdfs/                     # 默认输出子目录(可改)
    ├── 3B-426/
    │   └── test_d0202_04__495aced2.pdf
    ├── 3B-463/
    │   └── test_d0202_33__2e17e019.pdf  # 跨 6 stages 唯一文件(去重)
    ├── 1F-151/
    │   └── test_d0202_67__a2bb91e9.pdf
    └── 3B-437/
        └── test_d0202_xx__b8c4f1a9.pdf  # 同一电缆多版本时都保留
```

**典型输出 stats** (实测 Mac 3 PDF + 6 stages):
```
stage row counts:
  chieng+tess          13 rows
  chieng+tess+gauss    17 rows
  chisim+tess          10 rows
  chisim+tess_gauss    14 rows
  chieng+paddle        12 rows
  chisim+paddle        10 rows
unique (cable, content_hash) pairs: 22
unique cable IDs: 21 / 364 targets
PDF merge output: <wuhan>/pdf/_matched_pdfs
  copied:   22
  skipped:  35  (3B-463 跨 stage 重复,etc.)
```

### 实际时间估算

| 机器 | engine 组合 | workers | 总时间(1882 PDF) |
|------|------------|---------|------------------|
| Mac M-series 8 核 | tesseract-only (4 stages) | 16 | **45-50h**（二次图 拖慢）|
| Mac M-series 8 核 | tesseract + paddleocr CPU | 24 | **50-60h** (PaddleOCR CPU 慢) |
| **Win11 16 核 + GPU** | **tesseract + paddleocr** (6 stages) | **24** | **~1.5-2h** ✨ |
| Win11 16 核 | tesseract-only (4 stages) | 16 | **~1-1.5h** |

**Win11 GPU 是关键**:PaddleOCR 在 GPU 上 ~5-10ms/page (CPU 上 ~50-100ms/page),5-10x 加速。所以 6 stages 整体只比 4 stages 多 30-50min,但召回率 +10-20pp(从 ~75% 估到 85-92%)。

**内存估算 (Win11, 6 stages × 4 workers = 24 进程)**:
- Tesseract 16 进程 × ~50MB = 800MB
- PaddleOCR 8 进程 × ~250MB = 2GB
- 总计 ~3GB,16GB+ 内存充裕

**磁盘**:
- 6 stage cache × ~700MB = ~4GB(分散在 `.stage_*/.cable_match_cache.db`)
- 首次跑后,任何参数微调都秒过(命中 cache)

差异主要在二次图 目录(多页 A1 加长图,渲染 5-10s/页)。Win11 16 核 + GPU 足够一锅端。

## 九之3、Windows 特定配置

`cable_match.py` 启动时会自动检测 Tesseract 路径（无需手动配置）：
```python
if sys.platform == 'win32' and not pytesseract.pytesseract.tesseract_cmd:
    # 自动尝试 C:\Program Files\Tesseract-OCR\tesseract.exe
    # 自动尝试 C:\Program Files (x86)\Tesseract-OCR\tesseract.exe
```

如果 Tesseract 装在非标准位置，需手动设：
```python
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r'D:\tools\tesseract\tesseract.exe'
```

**Windows 环境准备**：
```powershell
# 1. 装 Tesseract 5.5.x: https://github.com/UB-Mannheim/tesseract/wiki
#    安装时勾选 "Additional language data > Chinese (Simplified)"
# 2. 装 Python 3.11/3.12
# 3. 克隆 + venv
git clone https://github.com/poilkjmn22/toolspy.git
cd toolspy
py -3.11 -m venv myenv
myenv\Scripts\pip install -r requirements.txt
# 4. 验证
myenv\Scripts\python -c "import pytesseract; print(pytesseract.get_tesseract_version())"
```

**Windows 路径注意**：
- 路径有中文或空格：用双引号 `python scripts\cable_match.py --input "C:\Users\...\wuhan\pdf"`
- 临时文件位置：Tesseract 用 `%TEMP%\tess_*`，长跑后定期清 `Remove-Item "$env:TEMP\tess_*"`
- 大 PDF 内存：137MB 的 D0223-26 渲染 80MB/页 × 4 workers = 320MB 峰值 × 6 stage = 1.9GB(Tesseract)+ 8 workers × 250MB PaddleOCR = 2GB,总 ~4GB

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
