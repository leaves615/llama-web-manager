# CLAUDE.md

本文档为 Claude Code (claude.ai/code) 提供本仓库的代码操作指引。

## 项目简介

Llama Manager — 轻量级 Web UI，用于启动和管理多个 `llama-server`（Ollama 内置 HTTP 服务）实例。采用两进程架构：Flask 后端 + 后台守护进程管理实际的 llama-server 子进程。

## 快速开始

```bash
pip install -r requirements.txt   # flask, PyYAML, psutil
python app.py                      # 启动 Flask，自动创建守护进程
# 访问 http://127.0.0.1:8787
```

环境变量：`LLAMA_MANAGER_HOST`（默认 `0.0.0.0`）、`LLAMA_MANAGER_PORT`（默认 `8787`）。

## 架构

**两进程设计** — 重启 Web UI 不影响运行中的实例：

- **`app.py`** (Flask 服务) — 提供 SPA 页面，通过 REST API + SSE 流处理 CRUD 操作，自动发现 llama-server 二进制和模型文件。通过 TCP 与守护进程通信（端口记录在 `daemon.pid`）。
- **`daemon.py`** (后台守护进程) — 监听随机 TCP 端口，管理 llama-server 子进程生命周期，将 stdout 捕获到 `logs/` 下每个实例的独立日志文件。
- **`static/main.js`** (~1200 行) — 全部 Vue 3 前端代码，无构建步骤。组件内联注册：`DaemonPanel`、`InstanceList`、`LogViewer`、`InstanceForm`、`LogModal`。
- **`static/styles.css`** (~1100 行) — 深色毛玻璃风格主题。
- **`templates/index.html`** — SPA 外壳，通过 CDN 加载 Vue 3 和 `static/main.js`。
- **`config.yaml`** — 控制 llama-server 二进制和模型文件（.gguf、.bin）的自动扫描路径、深度和间隔。
- **`instances.db`** — SQLite 数据库（已加入 .gitignore）。

关键通信模式：
- REST API（`/api/` 下所有路由）处理 CRUD
- SSE 流（`/stream`、`/logs/stream`）用于实时状态和日志跟随
- Flask 与守护进程间通过 TCP socket 通信（二进制协议，端口由 `daemon.pid` 管理）

## Do Not Rules（来自 AGENTS.md）

- 不要直接运行 `app.py` — 用户会手动启动。

## 开发注意

- 无构建步骤、无 lint 工具、无测试。纯 Python + Vue 3 CDN。
- 前端使用 `main.js` 内联模板（非 SFC）。
- 日志查看器支持 ANSI 颜色渲染和历史日志加载（`/logs/before?offset=&limit=`）。
- 守护进程通过扫描 `config.yaml` 配置的目录自动发现 llama-server 版本。

## 重要约束

- 方案始终使用中文，不允许英文。
- 对话始终使用中文。