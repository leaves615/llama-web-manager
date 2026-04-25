# llama-manager

一个简洁的 Web 管理工具，用于在 Windows 上启动和管理多个 `llama-server` 实例。

## 功能

- 启动多个 `llama-server` 实例
- 查看实例状态（PID、启动命令、运行状态）
- 支持编辑已有实例配置，保存后自动重启实例
- 支持启用/禁用实例（禁用后可再次启用）
- 实时查看实例日志
- 支持日志放大查看
- 参数配置支持：
  - 文本方式（自由输入命令参数）
  - 可视化方式（模型路径、host/port、线程、上下文、GPU layers、动态扩展参数）
- 支持在指定目录下扫描多个 llama-server 版本并在界面选择
- 支持在指定目录下扫描模型文件并在界面选择
- 同时支持手工输入目录或可执行文件路径
- 同时支持手工输入模型路径
- 版本和模型扫描由后台自动进行（无需手动触发）
- 支持局域网访问（默认监听 `0.0.0.0`）
- 界面简洁，适合本地运维管理

## 运行

> 建议在 Windows 上运行该工具，以便直接管理本机 `llama-server.exe` 进程。

1. 安装 Python 3.10+
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 启动服务：

```bash
python app.py
```

默认地址：

- 本机: `http://127.0.0.1:8787`
- 局域网: `http://<你的局域网IP>:8787`

可通过环境变量改监听地址和端口：

```bash
set LLAMA_MANAGER_HOST=0.0.0.0
set LLAMA_MANAGER_PORT=8787
python app.py
```

## 使用说明

1. 填写 `llama-server` 所在目录，例如：
  - `C:\\llama`
  - 或直接填写可执行文件路径：`C:\\llama\\llama-server-v2.exe`
2. 按需填写可视化参数（模型、host、port、线程等）
3. 在自由文本参数中补充高级参数
4. 点击 `命令预览` 确认最终命令
5. 点击 `创建实例` 启动
6. 主界面为左侧实例列表、右侧日志查看
7. 点击 `添加实例` 打开弹窗，填写参数后创建实例
8. 点击实例卡片 `编辑` 打开弹窗，修改后点击 `保存并重启`
8. 点击实例卡片中的 `启用/禁用` 可快速控制实例状态
9. 在日志面板点击 `放大查看` 可以全屏查看日志
10. llama 版本列表会由后台自动刷新，并在下拉框列出可选版本
11. 模型列表会由后台自动刷新，并在下拉框列出可选模型
12. 额外参数每一条都可单独启用/禁用

## 扫描目录配置

在项目根目录编辑 `config.yaml`：

```yaml
scan_roots:
  - "C:\\llama"
  - "D:\\llama-builds"
scan_max_depth: 5
scan_interval_seconds: 30
model_scan_roots:
  - "D:\\models"
model_scan_max_depth: 5
model_extensions:
  - ".gguf"
  - ".bin"
```

- `scan_roots`: 版本扫描根目录列表（可配置多个）
- `scan_max_depth`: 递归扫描深度
- `scan_interval_seconds`: 后台自动扫描间隔（秒）
- `model_scan_roots`: 模型扫描根目录列表（可配置多个）
- `model_scan_max_depth`: 模型递归扫描深度
- `model_extensions`: 模型文件后缀白名单

## 注意事项

- 首次使用局域网访问时，请确保 Windows 防火墙放行对应端口。
- `llama-server` 参数随版本可能变化，请根据你的版本调整参数。
- 当前日志采用内存缓存 + 文件落盘，日志文件位于 `logs/` 目录。
- 实例配置会持久化到 SQLite 数据库 `instances.db`，管理服务重启后仍可在实例列表中看到历史实例配置与状态。
