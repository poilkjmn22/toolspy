#!/bin/bash
# setup.sh - 一键设置虚拟环境

set -e  # 遇到错误立即退出

echo "正在创建 Python 虚拟环境..."

# 检查 Python 版本
python_version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "检测到 Python 版本: $python_version"

# 创建虚拟环境
python3 -m venv venv

# 激活虚拟环境
source venv/bin/activate

# 升级 pip
pip install --upgrade pip

# 安装基础包
pip install python-docs

# 保存依赖
pip freeze > requirements.txt

echo "虚拟环境设置完成！"
echo "使用以下命令激活环境: source venv/bin/activate"
echo "使用以下命令停用环境: deactivate"
