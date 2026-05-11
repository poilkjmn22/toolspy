# ToolsPy 工具集架构规范

## 目录结构

```
toolspy/
├── myenv/                  # Python 虚拟环境
├── tools/                  # 工具目录（核心）
│   ├── __init__.py         # CLI 统一入口
│   ├── docx_merger/        # 工具A：TSX → DOCX 合并
│   │   ├── __init__.py
│   │   └── main.py
│   └── text_sync/          # 工具B：实时文本同步服务器
│       ├── __init__.py
│       └── server.py
├── docs/                   # 文档/参考文件
├── src/                    # 旧版/归档代码
├── requirements.txt        # 依赖
├── setup.sh                # 环境安装脚本（已知有bug，建议用requirements.txt）
├── AGENTS.md               # OpenCode agent 指令
├── TOOLS.md                # 本规范文档
└── README.md               # 项目说明
```

## 核心设计原则

1. **每个工具独立目录** - 工具是完全独立的包，有自己的 `__init__.py`
2. **统一 CLI 入口** - 通过 `tools/__init__.py` 统一调度所有工具
3. **工具自包含** - 工具只依赖 `requirements.txt` 中的公共依赖，可独立运行
4. **明确的函数签名** - 每个工具模块必须有 `main()` 函数作为入口

## 新增工具流程

1. 在 `tools/` 下创建新目录，如 `tools/new_tool/`
2. 创建 `__init__.py`（可空）
3. 创建入口文件如 `main.py`，必须包含 `main()` 函数
4. 在 `tools/__init__.py` 的 `TOOLS` 字典中注册：

```python
TOOLS = {
    'docx-merger': {
        'module': 'tools.docx_merger',
        'help': '...',
    },
    'text-sync': {
        'module': 'tools.text_sync.server',
        'help': '...',
    },
    'new-tool': {
        'module': 'tools.new_tool',
        'help': '...',
    },
}
```

## 运行方式

```bash
# 方式1：通过虚拟环境直接运行单个工具
python tools/docx_merger/main.py <args>
python tools/text_sync/server.py -l 8000

# 方式2：通过统一入口（需先安装：pip install -e .）
python tools <tool-name> [args]
python tools docx-merger <source_folder>
python tools text-sync -l 8000

# 方式3：作为模块运行
python -m tools.docx_merger <args>
python -m tools.text_sync.server -l 8000

# 列出所有工具
python tools --list
```

## 工具规范

每个工具模块必须：
- 包含 `main()` 函数
- 使用 `argparse` 处理命令行参数
- 独立可运行（不依赖其他工具模块）

### 工具元信息（__init__.py 中定义）

```python
TOOLS = {
    'tool-name': {
        'module': 'tools.tool_dir.module_name',  # 导入路径
        'help': '简短描述',
    },
}
```

## 依赖管理

- 公共依赖放在根目录 `requirements.txt`
- 工具专用依赖可在工具目录内的 `requirements.txt`（可选）
- 安装：`pip install -r requirements.txt`

## 环境

- 虚拟环境位于 `myenv/`（不是 `venv/`）
- 安装依赖后直接使用 `python` 命令即可（假设已激活虚拟环境或通过 `myenv/bin/python` 调用）