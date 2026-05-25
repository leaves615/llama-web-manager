#!/bin/bash

# Llama Manager - Linux/macOS 启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "Llama Manager - 启动脚本"
echo "========================================"
echo ""

# 检查 Python 是否安装
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到 Python 3"
    echo "请先安装 Python 3.8 或更高版本"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Python 版本: $PYTHON_VERSION"

# 创建虚拟环境（如果不存在）
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
echo "🔧 激活虚拟环境..."
source venv/bin/activate

# 升级 pip
echo "⬆️  升级 pip..."
pip install --upgrade pip -q

# 安装依赖
if [ -f "requirements.txt" ]; then
    echo "📦 安装依赖..."
    pip install -r requirements.txt -q
    echo "✓ 依赖安装完成"
else
    echo "❌ 错误: 找不到 requirements.txt"
    exit 1
fi

echo ""
echo "========================================"
echo "🚀 启动 Llama Manager..."
echo "========================================"
echo ""
echo "访问地址: http://127.0.0.1:8787"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""

# 启动应用
python app.py
