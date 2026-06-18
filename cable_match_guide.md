# 多目标电缆号匹配工具 — 同事上手指南

## 这是什么

`scripts/cable_match.py` 一次性脚本：从一个 CSV（必须含 `电缆编号` 列）读出 N 个目标字符串（如 `T2-R139`、`DK1-103`），然后**只对每个 PDF 做一次 OCR**，把命中的 PDF 复制到各自的目标名子目录下。

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
# 1. 拉项目（如果还没有）
git clone <repo-url>
cd toolspy

# 2. 创建虚拟环境
python3 -m venv myenv
myenv/bin/pip install -r requirements.txt
myenv/bin/pip install pytesseract     # ← requirements.txt 里已有，但装一下保险
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
# 从项目根目录运行
cd <project>/toolspy

# Dry-run（先看下会匹配什么，不复制文件）
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
| `--workers` | 4 | 并行 OCR 线程数。建议 4-6；>8 会让 tesseract 内部线程打架 |
| `--list` | off | 只看匹配，不复制 |
| `--lang` | `chi_sim+eng` | Tesseract 语言包 |

### 推荐：正式跑前先 --list 看 5 分钟

29 个目标 × 450 PDF 的批次大约 **1.5-2 小时**（取决于机器配置）。先 `--list` 跑一遍确认工具能识别出预期的匹配，再正式跑。

## 五、输出结构

每个命中的目标会在 `--output` 下创建一个以电缆号命名的子目录：

```
<output>/
├── T2-R139/
│   ├── 10-W978-B768ⅡZ-D0201-11.pdf
│   └── ...  (含 T2-R139 的 PDF 副本)
├── DK1-103/
│   ├── 10-W978-B768ⅡZ-D0203-42.pdf
│   └── ...
└── ZL-322ZB/
    └── (空目录会被自动删除)
```

一个 PDF 可能同时命中多个电缆号，会被复制到多个目录。**原始 PDF 不会被修改**（用 `shutil.copy2` 复制）。

## 六、性能参考

| 批次规模 | 预估耗时（Mac M-series, 6 workers） |
|---------|---------|
| 50 个 1 页 PDF | ~5 分钟 |
| 450 个混合 PDF（85% 1 页，15% 多页）| ~1.5-2 小时 |
| 1000+ 个 PDF | 3-5 小时 |

**实际加速建议**：
- 多核机器可以 `--workers 6` 或 `8`（前提是 `OMP_THREAD_LIMIT=1` 已在环境里）
- 如果时间紧可以 `--dpi 200`（约快 30%，但小字可能漏）
- 想更激进可只跑某些子目录（用 `--input` 指向子目录）

## 七、重要的坑

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

如果在某些图纸上漏掉了明确的电缆号，可以加 `--dpi 400` 重跑该子目录（速度会慢 50%）。

### 3. 重新运行会重复创建 `_1`, `_2` 后缀副本

当前脚本**没有**内容哈希去重（`pdf-organize` 有，但这个一次性脚本没加）。如果跑了一半想重跑：

```bash
# 1. 先删掉所有已经生成的目标目录
rm -rf /path/to/output/{T2-R139,DK1-103,...}

# 2. 再重跑
myenv/bin/python scripts/cable_match.py ...
```

或者改用 `--list` 先确认无误，再正式跑。

### 4. OCR 失败是正常的

少数扫描件质量差（倾斜、模糊、太小），OCR 会失败。脚本不会中止，会跳过并计入 `失败` 统计，结束时打印。如果某个文件特别重要，单独 OCR 它看看：
```bash
myenv/bin/python -c "
from tools.text_extractor import extract_text
print(extract_text('path/to/that.pdf', ocr=True, dpi=400, warn=False))
"
```

## 八、退出码

| 码 | 含义 |
|----|------|
| 0 | 正常完成（可能有匹配，可能 0 匹配）|
| 1 | 工具错误（CSV 没 `电缆编号` 列、文件夹不存在等）|

`grep "匹配:" log.txt | wc -l` 看匹配数。

## 九、参考：原命令

这次跑武汉项目的实际命令：

```bash
cd ~/Documents/WebDev/toolspy

OMP_THREAD_LIMIT=1 myenv/bin/python scripts/cable_match.py \
    --csv ~/Documents/work/nengzhong/wuhan/filter_result_sj1x_ff.csv \
    --input ~/Documents/work/nengzhong/wuhan/电气二次1 \
    --workers 6
```

`OMP_THREAD_LIMIT=1` 是 macOS 关键：默认 tesseract 会启动多线程，与 Python 的 ThreadPoolExecutor 抢资源，加这行才能真正并行。
