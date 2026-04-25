# Llama Manager

[![Apache License 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![中文](https://img.shields.io/badge/中文文档-blue.svg)](README.md)

A lightweight web management tool for launching and managing multiple `llama-server` instances.

## Features

- Launch and manage multiple `llama-server` instances
- Real-time instance status (PID, startup command, running state)
- Real-time log viewing with auto-scroll and fullscreen mode
- Dual-mode parameter configuration:
  - Visual mode (model path, Host, Port, Threads, Context Size, GPU Layers)
  - Freeform text mode (supports any command-line arguments)
- Auto-scan and list available llama-server versions
- Auto-scan and list available model files
- Daemon mode - restarting Web UI won't affect running instances
- SSE real-time status updates
- LAN access support

## Quick Start

### Requirements

- Python 3.10+
- macOS / Linux / Windows

### Installation

```bash
# Clone the project
git clone https://github.com/leaves615/llama-manager.git
cd llama-manager

# Install dependencies
pip install -r requirements.txt
```

### Run

```bash
# Start the app (will auto-check and start the daemon)
python app.py
```

Access:
- Local: http://127.0.0.1:8787
- LAN: http://<your-ip>:8787

### Environment Variables

```bash
# Customize host and port
export LLAMA_MANAGER_HOST=0.0.0.0
export LLAMA_MANAGER_PORT=8787
python app.py
```

## Usage Guide

### Create Instance

1. Click "Add Instance" button on the left
2. Enter llama-server directory or select from scanned versions
3. Select model file or enter model path manually
4. Configure server parameters (Host, Port, Threads, Context Size, GPU Layers)
5. Add extra arguments if needed
6. Click "Preview Command" to confirm
7. Click "Create Instance" to save

### Manage Instances

- **Start/Stop**: Click the start/stop button on the instance card
- **View Logs**: Click "View Logs" on the instance card, auto-switches on start/stop
- **Auto-scroll**: Logs auto-scroll on startup, pauses when manually scrolled
- **Fullscreen Logs**: Click ⛶ button in log panel for fullscreen view
- **Edit Config**: Click "Edit" to modify parameters

## Configuration

Edit `config.yaml` for scan settings:

```yaml
# llama-server version scan config
scan_roots:
  - "/path/to/llama"
  - "/path/to/llama-builds"
scan_max_depth: 5
scan_interval_seconds: 30

# Model file scan config
model_scan_roots:
  - "/path/to/models"
model_scan_max_depth: 5
model_extensions:
  - ".gguf"
  - ".bin"
```

| Option | Description |
|--------|-------------|
| `scan_roots` | Directories to scan for llama-server versions |
| `scan_max_depth` | Max recursion depth for scanning |
| `scan_interval_seconds` | Auto-scan interval (seconds) |
| `model_scan_roots` | Directories to scan for model files |
| `model_scan_max_depth` | Model scan depth |
| `model_extensions` | Allowed model file extensions |

## Tech Stack

- **Backend**: Python Flask
- **Frontend**: Vue 3 (CDN)
- **Database**: SQLite
- **License**: Apache License 2.0

## License

This project is open source under [Apache License 2.0](LICENSE).

---

Copyright © 2026 leaves615