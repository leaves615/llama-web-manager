# Llama Manager - Windows PowerShell 启动脚本

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Llama Manager - 启动脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 切换到脚本目录
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# 检查 Python 是否安装
try {
    $pythonVersion = python --version 2>&1
    Write-Host "✓ $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ 错误: 未找到 Python" -ForegroundColor Red
    Write-Host "请先安装 Python 3.8 或更高版本" -ForegroundColor Red
    Write-Host "下载地址: https://www.python.org/downloads/" -ForegroundColor Yellow
    Read-Host "按 Enter 退出"
    exit 1
}

Write-Host ""

# 创建虚拟环境（如果不存在）
if (-not (Test-Path "venv")) {
    Write-Host "📦 创建虚拟环境..." -ForegroundColor Yellow
    python -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 虚拟环境创建失败" -ForegroundColor Red
        Read-Host "按 Enter 退出"
        exit 1
    }
}

# 激活虚拟环境
Write-Host "🔧 激活虚拟环境..." -ForegroundColor Yellow
& ".\venv\Scripts\Activate.ps1"

# 升级 pip
Write-Host "⬆️  升级 pip..." -ForegroundColor Yellow
python -m pip install --upgrade pip -q

# 安装依赖
if (Test-Path "requirements.txt") {
    Write-Host "📦 安装依赖..." -ForegroundColor Yellow
    pip install -r requirements.txt -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 依赖安装失败" -ForegroundColor Red
        Read-Host "按 Enter 退出"
        exit 1
    }
    Write-Host "✓ 依赖安装完成" -ForegroundColor Green
} else {
    Write-Host "❌ 错误: 找不到 requirements.txt" -ForegroundColor Red
    Read-Host "按 Enter 退出"
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "🚀 启动 Llama Manager..." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "访问地址: http://127.0.0.1:8787" -ForegroundColor Green
Write-Host ""
Write-Host "按 Ctrl+C 停止服务" -ForegroundColor Yellow
Write-Host ""

# 启动应用
python app.py

Read-Host "按 Enter 退出"
