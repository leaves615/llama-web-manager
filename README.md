# Llama Manager

[![Apache License 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

一个简洁的 Web 管理工具，用于在 Windows 上启动和管理多个 `llama-server` 实例。

## 功能特性

- 启动和管理多个 `llama-server` 实例
- 实时查看实例状态（PID、启动命令、运行状态）
- 实例配置编辑，保存后自动重启
- 实例启用/禁用控制
- 实时日志查看，支持全屏放大
- 双模式参数配置：
  - 可视化方式（模型路径、Host、Port、Threads、Context Size、GPU Layers）
  - 自由文本方式（支持任意命令行参数）
- 自动扫描并列出可用 llama-server 版本
- 自动扫描并列出可用模型文件
- 支持局域网访问

## 快速开始

### 环境要求

- Python 3.10+
- Windows 系统（推荐）

### 安装

```bash
# 克隆项目
git clone https://github.com/leaves615/llama-manager.git
cd llama-manager

# 安装依赖
pip install -r requirements.txt
```

### 运行

```bash
# 启动应用（会自动检查并启动守护进程）
python app.py
```

**说明**：守护进程独立运行，重启 Web UI 不会影响正在运行的 llama-server 实例。

访问地址：
- 本机: http://127.0.0.1:8787
- 局域网: http://<你的IP>:8787

### 环境变量配置

```bash
# 自定义监听地址和端口
set LLAMA_MANAGER_HOST=0.0.0.0
set LLAMA_MANAGER_PORT=8787
python app.py
```

## 使用指南

### 创建实例

1. 点击左侧「添加实例」按钮
2. 填写 llama-server 所在目录或直接选择扫描到的版本
3. 选择模型文件或手动输入模型路径
4. 配置服务器参数（Host、Port、Threads、Context Size、GPU Layers）
5. 如需要可添加额外参数
6. 点击「命令预览」确认命令
7. 点击「创建实例」启动

### 管理实例

- **查看日志**: 点击实例卡片中的「查看日志」
- **编辑配置**: 点击「编辑」修改参数，保存后自动重启
- **启用/禁用**: 点击「启用/禁用」控制实例状态
- **日志放大**: 点击日志面板的 ⛶ 按钮全屏查看

## 配置说明

编辑 `config.yaml` 自定义扫描配置：

```yaml
# llama-server 版本扫描配置
scan_roots:
  - "C:\\llama"
  - "D:\\llama-builds"
scan_max_depth: 5
scan_interval_seconds: 30

# 模型文件扫描配置
model_scan_roots:
  - "D:\\models"
model_scan_max_depth: 5
model_extensions:
  - ".gguf"
  - ".bin"
```

| 配置项 | 说明 |
|--------|------|
| `scan_roots` | llama-server 版本扫描目录列表 |
| `scan_max_depth` | 递归扫描深度 |
| `scan_interval_seconds` | 自动扫描间隔（秒） |
| `model_scan_roots` | 模型文件扫描目录列表 |
| `model_scan_max_depth` | 模型扫描深度 |
| `model_extensions` | 模型文件后缀白名单 |

## 技术栈

- **后端**: Python Flask
- **前端**: 原生 HTML/CSS/JavaScript
- **数据库**: SQLite
- **协议**: Apache License 2.0

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源。

---

Copyright © 2026 leaves615